"""Connection status for the running config: addresses, speed, handshake,
exit IP/country.

Computed on demand while the panel polls; the exit lookup (a request to
Cloudflare through the tunnel) runs in the background and is cached, so it
only happens while the user is looking at the panel.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import defaults
from .models import ConfigKind, ConnectionStatus, ExitInfo, StoredConfig
from .runner import CommandRunner
from .service_manager import ServiceManager


def parse_trace(text: str, now: float) -> Optional[ExitInfo]:
    """Parses Cloudflare /cdn-cgi/trace output (key=value lines)."""
    fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
    ip = fields.get("ip")
    if not ip:
        return None
    loc = fields.get("loc")
    return ExitInfo(ip=ip, country=loc if loc and loc != "XX" else None, checked_at=now)


def parse_addresses(output: str) -> Tuple[str, ...]:
    """Addresses from `ip -o addr show dev X`, e.g. ("10.8.1.2/32",)."""
    out: List[str] = []
    for line in output.splitlines():
        tokens = line.split()
        for key in ("inet", "inet6"):
            if key in tokens:
                i = tokens.index(key)
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("fe80:"):
                    out.append(tokens[i + 1])
    return tuple(out)


@dataclass
class _Sample:
    rx: int
    tx: int
    at: float


class StatusProbe:
    def __init__(self, runner: CommandRunner, service: ServiceManager,
                 sys_net_dir: str = "/sys/class/net", clock: Callable[[], float] = time.time):
        self.runner = runner
        self.service = service
        self.sys_net_dir = sys_net_dir
        self.clock = clock
        self._samples: Dict[str, _Sample] = {}
        self._exit: Dict[str, ExitInfo] = {}
        self._exit_busy: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def _counter(self, iface: str, name: str) -> Optional[int]:
        try:
            with open(os.path.join(self.sys_net_dir, iface, "statistics", name)) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _speed(self, iface: str, now: float) -> Tuple[Optional[float], Optional[float]]:
        rx, tx = self._counter(iface, "rx_bytes"), self._counter(iface, "tx_bytes")
        if rx is None or tx is None:
            return None, None
        prev = self._samples.get(iface)
        self._samples[iface] = _Sample(rx, tx, now)
        if prev is None or now <= prev.at or rx < prev.rx or tx < prev.tx:
            return None, None
        dt = now - prev.at
        return (rx - prev.rx) / dt, (tx - prev.tx) / dt

    def _fetch_exit(self, iface: str) -> None:
        try:
            r = self.runner.run(["curl", "-sS", "--max-time", "6", defaults.EXIT_IP_URL], timeout=10)
            info = parse_trace(r.stdout, self.clock()) if r.ok else None
            with self._lock:
                if info is not None:
                    self._exit[iface] = info
        finally:
            with self._lock:
                self._exit_busy[iface] = False

    def _exit_info(self, iface: str, now: float) -> Optional[ExitInfo]:
        with self._lock:
            cached = self._exit.get(iface)
            stale = cached is None or now - cached.checked_at >= defaults.EXIT_IP_REFRESH_SEC
            if stale and not self._exit_busy.get(iface):
                self._exit_busy[iface] = True
                threading.Thread(target=self._fetch_exit, args=(iface,), daemon=True).start()
            return cached

    def forget(self, iface: str) -> None:
        """Drop cached exit info, e.g. after switching configs."""
        with self._lock:
            self._exit.pop(iface, None)
        self._samples.pop(iface, None)

    def status(self, cfg: StoredConfig) -> ConnectionStatus:
        now = self.clock()
        r = self.runner.run(["ip", "-o", "addr", "show", "dev", cfg.interface])
        handshake: Optional[int] = None
        if cfg.kind is ConfigKind.AWG:
            handshakes = [t for t in self.service.latest_handshakes(cfg.interface).values() if t > 0]
            handshake = max(handshakes) if handshakes else None
        rx, tx = self._speed(cfg.interface, now)
        return ConnectionStatus(
            name=cfg.name,
            kind=cfg.kind,
            interface=cfg.interface,
            addresses=parse_addresses(r.stdout) if r.ok else (),
            latest_handshake=handshake,
            rx_bytes_per_sec=rx,
            tx_bytes_per_sec=tx,
            exit=self._exit_info(cfg.interface, now),
        )
