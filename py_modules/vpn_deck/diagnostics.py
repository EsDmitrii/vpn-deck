"""Diagnostics - connectivity probes for VPN troubleshooting."""

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import decky

from . import defaults
from ._utils import clean_env
from .status import parse_trace


# What a Deck owner actually uses; YouTube and Discord also show at a glance
# whether traffic really leaves through the VPN (throttled or blocked without
# it in some regions).
DEFAULT_TARGETS: List[Dict[str, Any]] = [
    {"name": "Сеть (ping 1.1.1.1)", "kind": "ping", "host": "1.1.1.1"},
    {"name": "Cloudflare", "kind": "trace", "url": defaults.EXIT_IP_URL},
    {"name": "Steam", "kind": "http", "url": "https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/"},
    {"name": "YouTube", "kind": "http", "url": "https://www.youtube.com"},
    {"name": "Discord", "kind": "http", "url": "https://discord.com/api/v10/gateway"},
    {"name": "Twitch", "kind": "http", "url": "https://www.twitch.tv"},
]


class Diagnostics:
    def check(self, targets: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        probes = targets if targets else DEFAULT_TARGETS
        if not probes:
            return []
        with ThreadPoolExecutor(max_workers=len(probes)) as pool:
            return list(pool.map(self._probe, probes))

    def _probe(self, t: Dict[str, Any]) -> Dict[str, Any]:
        kind = t.get("kind")
        name = t.get("name") or t.get("host") or t.get("url") or "?"
        if kind == "ping":
            return self._ping(name, t["host"])
        if kind == "http":
            return self._http(name, t["url"])
        if kind == "trace":
            return self._trace(name, t["url"])
        return {"name": name, "kind": kind or "unknown", "ok": False, "detail": f"unknown kind: {kind}", "target": "", "latency_ms": None}

    @staticmethod
    def _ping(name: str, host: str) -> Dict[str, Any]:
        try:
            r = subprocess.run(
                ["ping", "-c", "3", "-W", "2", "-n", host],
                capture_output=True, text=True, timeout=12, check=False,
                env=clean_env(),
            )
            ok = r.returncode == 0
            avg_ms = None
            if ok:
                m = re.search(r"min/avg/max/\S+\s*=\s*[\d.]+/([\d.]+)/", r.stdout)
                if m:
                    avg_ms = float(m.group(1))
            detail = f"avg {avg_ms:.1f} ms" if avg_ms is not None else (r.stderr.strip() or "no response")
            return {"name": name, "kind": "ping", "target": host, "ok": ok, "detail": detail, "latency_ms": avg_ms}
        except subprocess.TimeoutExpired:
            return {"name": name, "kind": "ping", "target": host, "ok": False, "detail": "timeout", "latency_ms": None}
        except FileNotFoundError:
            decky.logger.warning("ping not found for diagnostics")
            return {"name": name, "kind": "ping", "target": host, "ok": False, "detail": "ping not found", "latency_ms": None}

    @staticmethod
    def _trace(name: str, url: str) -> Dict[str, Any]:
        """Cloudflare /cdn-cgi/trace: reachability plus the exit country."""
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "6", "-w", "\n%{time_total}", url],
                capture_output=True, text=True, timeout=10, check=False, env=clean_env(),
            )
        except subprocess.TimeoutExpired:
            return {"name": name, "kind": "trace", "target": url, "ok": False, "detail": "timeout", "latency_ms": None}
        except FileNotFoundError:
            return {"name": name, "kind": "trace", "target": url, "ok": False, "detail": "curl not found", "latency_ms": None}
        body, _, total = r.stdout.rpartition("\n")
        info = parse_trace(body, 0) if r.returncode == 0 else None
        if info is None:
            err = r.stderr.strip().splitlines()
            return {"name": name, "kind": "trace", "target": url, "ok": False,
                    "detail": err[-1] if err else "no response", "latency_ms": None}
        try:
            latency = float(total) * 1000
        except ValueError:
            latency = None
        where = f"выход: {info.country}" if info.country else "выход: ?"
        detail = where + (f", {latency / 1000:.2f}s" if latency is not None else "")
        return {"name": name, "kind": "trace", "target": url, "ok": True, "detail": detail, "latency_ms": latency}

    @staticmethod
    def _http(name: str, url: str) -> Dict[str, Any]:
        try:
            r = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", "6", "-L", url],
                capture_output=True, text=True, timeout=10, check=False,
                env=clean_env(),
            )
            out = r.stdout.strip()
            parts = out.split()
            code = parts[0] if parts else "0"
            time_s = float(parts[1]) if len(parts) > 1 else None
            # Any HTTP answer means the site is reachable; blocking shows up
            # as a timeout or reset (code 000), not as a status code.
            ok = code != "000"
            detail = f"HTTP {code}" + (f", {time_s:.2f}s" if time_s is not None else "")
            if not ok and r.stderr:
                detail += f" ({r.stderr.strip().splitlines()[-1]})"
            return {"name": name, "kind": "http", "target": url, "ok": ok, "detail": detail, "latency_ms": time_s * 1000 if time_s else None}
        except subprocess.TimeoutExpired:
            return {"name": name, "kind": "http", "target": url, "ok": False, "detail": "timeout", "latency_ms": None}
        except FileNotFoundError:
            decky.logger.warning("curl not found for diagnostics")
            return {"name": name, "kind": "http", "target": url, "ok": False, "detail": "curl not found", "latency_ms": None}
