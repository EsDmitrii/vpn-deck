"""sing-box configs: VLESS link -> config, and safety checks for user JSON.

sing-box (like Xray) only takes JSON; `vless://` links are a client-side
convention, so the mapping below is ours. It is deliberately thin: share-link
params are copied into the outbound, and `sing-box check` validates the
result at start. We only refuse what would otherwise silently turn into a
broken config (transports/features sing-box lacks).

Targets the sing-box >= 1.12 schema (typed DNS servers, rule actions).
"""

from __future__ import annotations

import copy
import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import defaults
from .errors import ConfigImportError
from .models import Json

PROXY_TAG = "proxy"
DIRECT_TAG = "direct"
# Top-level sections that could open listeners or write files as root.
FORBIDDEN_SECTIONS = ("experimental", "services")
LOOPBACK_LISTEN = frozenset({"127.0.0.1", "::1", "localhost"})
# Xray transports sing-box has no implementation for.
UNSUPPORTED_TRANSPORTS = frozenset({"xhttp", "splithttp", "kcp", "mkcp"})
# REALITY needs uTLS in sing-box; xray clients default to this fingerprint.
DEFAULT_REALITY_FINGERPRINT = "chrome"


@dataclass(frozen=True)
class VlessOptions:
    """User-tunable parts of the generated config (see Settings)."""
    dns_server: str = defaults.SINGBOX_DNS_SERVER
    ipv4_only: bool = defaults.SINGBOX_IPV4_ONLY


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


# ── vless:// ────────────────────────────────────────────────────────

def _params(query: str) -> Dict[str, str]:
    return {k: unquote(v[0]).strip() for k, v in parse_qs(query, keep_blank_values=True).items()}


def _transport(p: Dict[str, str]) -> Optional[Json]:
    net = p.get("type", "tcp").lower()
    path = p.get("path") or "/"
    host = p.get("host")

    if net in UNSUPPORTED_TRANSPORTS:
        raise ConfigImportError(f"Транспорт {net} не поддерживается sing-box")
    if net in ("tcp", "raw"):
        if p.get("headerType", "none") not in ("", "none"):
            raise ConfigImportError("Обфускация TCP (headerType) не поддерживается sing-box")
        return None
    if net == "ws":
        t: Json = {"type": "ws", "path": path}
        # xray encodes early data as `?ed=N` inside the path.
        ed = re.search(r"[?&]ed=(\d+)", path)
        if ed:
            t["path"] = re.sub(r"[?&]ed=\d+", "", path) or "/"
            t["max_early_data"] = int(ed.group(1))
            t["early_data_header_name"] = "Sec-WebSocket-Protocol"
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        return {"type": "grpc", "service_name": p.get("serviceName", "")}
    if net == "httpupgrade":
        return {"type": "httpupgrade", "path": path, **({"host": host} if host else {})}
    if net in ("http", "h2"):
        hosts = [h.strip() for h in (host or "").split(",") if h.strip()]
        return {"type": "http", "path": path, **({"host": hosts} if hosts else {})}
    # Unknown to us: pass through, `sing-box check` reports it at start.
    return {"type": net}


def _tls(p: Dict[str, str], server: str) -> Optional[Json]:
    security = p.get("security", "none").lower()
    if security in ("", "none"):
        return None
    tls: Json = {"enabled": True, "server_name": p.get("sni") or server}
    if p.get("alpn"):
        tls["alpn"] = [a.strip() for a in p["alpn"].split(",") if a.strip()]
    if p.get("allowInsecure") in ("1", "true"):
        tls["insecure"] = True
    fingerprint = p.get("fp")
    if security == "reality":
        tls["reality"] = {"enabled": True, "public_key": p.get("pbk", ""), "short_id": p.get("sid", "")}
        fingerprint = fingerprint or DEFAULT_REALITY_FINGERPRINT
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    return tls


def vless_outbound(link: str, server_ip: Optional[str] = None) -> Json:
    """Maps a vless:// link to a sing-box outbound.

    `server_ip` replaces a hostname server (the name stays in TLS SNI);
    see build_vless_config for why.
    """
    link = link.strip()
    if not link.lower().startswith("vless://"):
        raise ConfigImportError("Ссылка должна начинаться с vless://")
    try:
        url = urlsplit(link)
        server, port = url.hostname, url.port
    except ValueError:
        raise ConfigImportError("Ссылка vless:// повреждена")
    if not url.username or not server or not port:
        raise ConfigImportError("В ссылке vless:// нет UUID, адреса или порта")

    p = _params(url.query)
    if p.get("encryption", "none") not in ("", "none"):
        raise ConfigImportError("VLESS encryption не поддерживается sing-box")

    out: Json = {
        "type": "vless",
        "tag": PROXY_TAG,
        "server": server_ip if server_ip and not is_ip(server) else server,
        "server_port": port,
        "uuid": unquote(url.username),
        "packet_encoding": "xudp",
    }
    if p.get("flow"):
        out["flow"] = p["flow"]
    transport = _transport(p)
    if transport is not None:
        out["transport"] = transport
    tls = _tls(p, server)
    if tls is not None:
        out["tls"] = tls
    return out


def vless_server(link: str) -> str:
    return vless_outbound(link)["server"]


def vless_label(link: str) -> str:
    """Short description for the UI, e.g. `VLESS REALITY` or `VLESS TLS WS`."""
    out = vless_outbound(link)
    parts = ["VLESS"]
    if "tls" in out:
        parts.append("REALITY" if "reality" in out["tls"] else "TLS")
    if "transport" in out:
        parts.append(str(out["transport"]["type"]).upper())
    return " ".join(parts)


def build_vless_config(link: str, interface: str, server_ip: Optional[str] = None,
                       options: VlessOptions = VlessOptions()) -> Json:
    """Whole-device TUN config: everything via the proxy, private ranges
    direct, DNS hijacked to DoH through the proxy.

    The plugin resolves the server name before start and passes `server_ip`:
    resolving through the system resolver once the TUN is up would loop the
    query back into the tunnel.
    """
    dns: Json = {
        "servers": [{"type": "https", "tag": "remote", "server": options.dns_server}],
        "final": "remote",
    }
    if options.ipv4_only:
        dns["strategy"] = "ipv4_only"

    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": dns,
        "inbounds": [{
            "type": "tun",
            "tag": "tun-in",
            "interface_name": interface,
            "address": list(defaults.SINGBOX_TUN_ADDRESSES),
            "mtu": defaults.SINGBOX_TUN_MTU,
            "auto_route": True,
            "strict_route": True,
            "stack": defaults.SINGBOX_TUN_STACK,
        }],
        "outbounds": [vless_outbound(link, server_ip), {"type": "direct", "tag": DIRECT_TAG}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": DIRECT_TAG},
            ],
            "final": PROXY_TAG,
            "auto_detect_interface": True,
        },
    }


# ── user-supplied sing-box JSON ─────────────────────────────────────

def _dicts(value: Any) -> List[Json]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def validate_user_singbox(obj: Any) -> Json:
    """Safety checks only; schema validation is `sing-box check`'s job."""
    if not isinstance(obj, dict):
        raise ConfigImportError("JSON-конфиг должен быть объектом")
    cfg: Json = obj

    inbounds = _dicts(cfg.get("inbounds"))
    if sum(1 for i in inbounds if i.get("type") == "tun") != 1:
        raise ConfigImportError("В конфиге sing-box должен быть ровно один inbound типа tun")

    for key in FORBIDDEN_SECTIONS:
        if key in cfg:
            raise ConfigImportError(
                f"Секция «{key}» не поддерживается: она может открывать порты или писать файлы от root")

    for inbound in inbounds:
        if inbound.get("type") != "tun" and str(inbound.get("listen", "")).strip() not in LOOPBACK_LISTEN:
            name = inbound.get("tag") or inbound.get("type")
            raise ConfigImportError(
                f"Inbound «{name}» слушает не только localhost. Разрешены только listen 127.0.0.1 или ::1")
    return cfg


def prepare_user_singbox(obj: Any, interface: str) -> Json:
    """Copy of a validated user config, ready to run as `interface`.
    `log.output` is dropped so sing-box never writes to a path as root."""
    cfg = copy.deepcopy(validate_user_singbox(obj))
    tun = next(i for i in _dicts(cfg.get("inbounds")) if i.get("type") == "tun")
    tun["interface_name"] = interface
    log = cfg.get("log")
    level = log.get("level", "warn") if isinstance(log, dict) else "warn"
    cfg["log"] = {"level": level, "timestamp": True}
    return cfg
