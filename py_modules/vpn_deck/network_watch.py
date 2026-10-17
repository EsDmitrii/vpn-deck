"""Keeps AWG tunnels alive across sleep and network changes.

Two problems, each with a pure decision function and a small executor:

1. Endpoint pin goes stale. For split tunnels awg-quick pins a host route to
   the peer endpoint through the gateway seen at `up` (endpoint-direct-route
   patch). After moving to another network that gateway is gone. We re-pin
   through the current underlay. Full tunnels are not pinned at all (their
   packets carry the fwmark and follow the main table's default route).

2. Peer goes silent. With persistent keepalive a healthy peer handshakes at
   least every ~2 minutes. If it hasn't for STALE_HANDSHAKE_SEC while we're
   online, the tunnel is restarted (down/up), rate-limited.

sing-box handles network changes itself (auto_detect_interface).
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Union

import decky

from . import defaults
from .models import ConfigKind
from .runner import CommandRunner
from .service_manager import MANAGED_PREFIX, ServiceManager
from .tunnels import TunnelController

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
ENDPOINT_ROUTE_STATE_DIR = "/var/run/awg-quick"


# ── routes ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Route:
    dst: IPNetwork
    dev: str
    gateway: Optional[str] = None
    metric: int = 0


def _after(tokens: List[str], key: str) -> Optional[str]:
    """Value following `key`; `ip -o` mixes "key value" pairs with bare flags."""
    try:
        return tokens[tokens.index(key) + 1]
    except (ValueError, IndexError):
        return None


def parse_routes(output: str, family: int) -> List[Route]:
    """Parses `ip -o -4|-6 route show table main`. Multipath routes (no
    single dev) are skipped; on such networks we don't touch anything."""
    routes: List[Route] = []
    for line in output.splitlines():
        tokens = line.split()
        if not tokens or tokens[0] in ("unreachable", "blackhole", "prohibit", "local", "broadcast"):
            continue
        dst_token = tokens[0]
        if dst_token == "default":
            dst_token = "0.0.0.0/0" if family == 4 else "::/0"
        try:
            dst = ipaddress.ip_network(dst_token, strict=False)
        except ValueError:
            continue
        dev = _after(tokens, "dev")
        if dev is None:
            continue
        metric = _after(tokens, "metric")
        routes.append(Route(dst=dst, dev=dev, gateway=_after(tokens, "via"),
                            metric=int(metric) if metric and metric.isdigit() else 0))
    return routes


def read_routes(runner: CommandRunner) -> List[Route]:
    routes: List[Route] = []
    for family in (4, 6):
        r = runner.run(["ip", "-o", f"-{family}", "route", "show", "table", "main"])
        if r.ok:
            routes.extend(parse_routes(r.stdout, family))
    return routes


def is_tunnel_dev(dev: str) -> bool:
    return dev.startswith(MANAGED_PREFIX) or dev.startswith("awg") or dev.startswith("wg")


def best_underlay(routes: Iterable[Route], host: str, ignore: Optional[IPNetwork] = None) -> Optional[Route]:
    """Route the kernel would use for `host` without tunnels and without the
    pin itself: longest prefix, then lowest metric."""
    addr = ipaddress.ip_address(host)
    candidates = [
        r for r in routes
        if r.dst.version == addr.version and addr in r.dst
        and not is_tunnel_dev(r.dev) and r.dst != ignore
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (-r.dst.prefixlen, r.metric))


def is_online(routes: Iterable[Route]) -> bool:
    return any(r.dst.prefixlen == 0 and not is_tunnel_dev(r.dev) for r in routes)


@dataclass(frozen=True)
class RepinAction:
    cidr: str
    family: int
    # None means the endpoint became on-link: drop the pin.
    via: Optional[Route]


def plan_repin(cidr: str, routes: Sequence[Route]) -> Optional[RepinAction]:
    pin = ipaddress.ip_network(cidr, strict=False)
    current = next((r for r in routes if r.dst == pin), None)
    underlay = best_underlay(routes, str(pin.network_address), ignore=pin)
    if underlay is None:
        return None  # offline: nothing sensible to pin to
    if underlay.gateway is None:
        return RepinAction(cidr, pin.version, None) if current is not None else None
    if current is not None and current.gateway == underlay.gateway and current.dev == underlay.dev:
        return None
    return RepinAction(cidr, pin.version, underlay)


def read_pins(state_dir: str, interface: str) -> List[str]:
    """CIDRs awg-quick pinned for `interface` (lines like "-4 1.2.3.4/32")."""
    try:
        with open(os.path.join(state_dir, f"{interface}.endpoint-routes")) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return [parts[1] for parts in (l.split() for l in lines) if len(parts) == 2]


# ── peer health ─────────────────────────────────────────────────────

@dataclass
class PeerHealth:
    """Per-interface restart decision state."""
    first_seen: float
    stale_since: Optional[float] = None
    last_restart: Optional[float] = None

    def should_restart(self, now: float, handshakes: Dict[str, int],
                       keepalives: Dict[str, Optional[int]], online: bool) -> bool:
        watched = [k for k, ka in keepalives.items() if ka]
        if not watched or not online:
            self.stale_since = None
            return False
        # 0 = never handshaked; count from when we first saw the tunnel.
        latest = max(handshakes.get(k, 0) or self.first_seen for k in watched)
        if now - latest < defaults.STALE_HANDSHAKE_SEC:
            self.stale_since = None
            return False
        if self.stale_since is None:
            self.stale_since = now
            return False
        if now - self.stale_since < defaults.RESTART_GRACE_SEC:
            return False
        if self.last_restart is not None and now - self.last_restart < defaults.MIN_RESTART_INTERVAL_SEC:
            return False
        self.last_restart = now
        self.stale_since = None
        return True


# ── executor ────────────────────────────────────────────────────────

ErrorSink = Callable[[str, str], None]


@dataclass
class _State:
    health: Dict[str, PeerHealth] = field(default_factory=dict)
    # Config to bring back after a failed restart, with the generation the
    # restart happened in and the last attempt time.
    pending_up: Optional[str] = None
    pending_generation: int = 0
    last_retry: float = 0.0
    reported: Set[str] = field(default_factory=lambda: set())


class NetworkWatcher:
    def __init__(self, controller: TunnelController, service: ServiceManager, runner: CommandRunner,
                 report_error: ErrorSink, state_dir: str = ENDPOINT_ROUTE_STATE_DIR,
                 clock: Callable[[], float] = time.time):
        self.controller = controller
        self.service = service
        self.runner = runner
        self.report_error = report_error
        self.state_dir = state_dir
        self.clock = clock
        self._state = _State()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vpn-deck-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 90) -> None:
        """Waits for a restart in progress (awg-quick can take up to 60 s)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(defaults.WATCH_INTERVAL_SEC):
            try:
                self.tick()
            except Exception as e:  # keep the thread alive
                decky.logger.error(f"network watch tick failed: {e}")

    # one iteration
    def _report_once(self, operation: str, message: str) -> None:
        if message not in self._state.reported:
            self._state.reported.add(message)
            self.report_error(operation, message)

    def _apply_repin(self, action: RepinAction) -> None:
        fam = f"-{action.family}"
        if action.via is None:
            argv = ["ip", fam, "route", "del", action.cidr]
        else:
            argv = ["ip", fam, "route", "replace", action.cidr, "via", str(action.via.gateway),
                    "dev", action.via.dev]
        r = self.runner.run(argv)
        if r.ok:
            decky.logger.info(f"re-pinned endpoint: {' '.join(argv)}")
        else:
            self._report_once("network_watch", f"{' '.join(argv)}: {r.stderr.strip()}")

    def tick(self) -> None:
        now = self.clock()
        routes = read_routes(self.runner)
        online = is_online(routes)
        ifaces = [i for i in self.service.interfaces() if i.startswith(MANAGED_PREFIX)]

        for gone in set(self._state.health) - set(ifaces):
            del self._state.health[gone]

        for iface in ifaces:
            if online:
                for cidr in read_pins(self.state_dir, iface):
                    action = plan_repin(cidr, routes)
                    if action is not None:
                        self._apply_repin(action)

            health = self._state.health.setdefault(iface, PeerHealth(first_seen=now))
            if health.should_restart(now, self.service.latest_handshakes(iface),
                                     self.service.persistent_keepalives(iface), online):
                self._restart(iface, now)

        self._retry_pending(now, online)

    def _restart(self, iface: str, now: float) -> None:
        cfg = self.controller.configs.find_by_interface(iface)
        if cfg is None or cfg.kind is not ConfigKind.AWG:
            return
        decky.logger.warning(f"{iface}: no handshake for {defaults.STALE_HANDSHAKE_SEC}s, restarting")
        generation = self.controller.generation
        result = self.controller.restart(cfg)
        if not result.success:
            self._report_once("network_watch", f"restart {iface}: {result.error}")
            self._state.pending_up = cfg.name
            self._state.pending_generation = generation
            self._state.last_retry = now

    def _retry_pending(self, now: float, online: bool) -> None:
        name = self._state.pending_up
        if name is None or not online or now - self._state.last_retry < defaults.RETRY_UP_INTERVAL_SEC:
            return
        self._state.last_retry = now
        result = self.controller.ensure_up(name, self._state.pending_generation)
        if result is None or result.success:
            self._state.pending_up = None  # user stepped in, or it's back
