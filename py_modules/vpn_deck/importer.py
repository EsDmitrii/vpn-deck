"""Importer - turns a user-picked file into an ImportPlan.

Supported inputs:
  - AmneziaWG / WireGuard `.conf`
  - AmneziaVPN share link `vpn://...` (or a file containing one)
  - VLESS share link `vless://...` (or a file containing one)
  - sing-box JSON config with a single `tun` inbound

Pure apart from `read_import_file`: no writes, no subprocesses.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .defaults import (
    AMNEZIA_FALLBACK_DNS,
    MAX_IMPORT_BYTES,
    MTU_MAX,
    MTU_MIN,
    RECOMMENDED_AWG_MTU,
)
from .errors import ConfigImportError
from .models import ConfigKind, ImportPlan
from .singbox import validate_user_singbox, vless_label
from .subscription import SubscriptionConfig, find_server, is_subscription_url, parse_subscription

# awg-quick runs these through `eval` as root.
HOOK_KEYS = {"preup": "PreUp", "postup": "PostUp", "predown": "PreDown", "postdown": "PostDown"}


def check_import_text(text: str) -> str:
    """Limits for text pasted into the import field."""
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ConfigImportError("Текст больше 1 МБ, это не похоже на конфиг")
    if not text.strip():
        raise ConfigImportError("Вставьте ссылку, URL подписки или конфиг")
    return text


def unflatten_conf(text: str) -> str:
    """A .conf pasted into a single-line field loses its line breaks:
    "[Interface] PrivateKey = … Address = … [Peer] …". Restore them before
    sections and `Key =` pairs."""
    if "\n" in text.strip() or "[Interface]" not in text:
        return text
    text = re.sub(r"\s+(?=\[\w+\])", "\n", text.strip())
    return re.sub(r"\s+(?=[A-Za-z][A-Za-z0-9]*\s*=\s)", "\n", text) + "\n"


def read_import_file(path: str) -> str:
    """Reads a file to import, enforcing size and encoding limits."""
    with open(path, "rb") as f:
        raw = f.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise ConfigImportError("Файл больше 1 МБ, это не похоже на конфиг")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigImportError("Файл не в кодировке UTF-8")
    if not text.strip():
        raise ConfigImportError("Файл пустой")
    return text


# ── AmneziaVPN vpn:// links ─────────────────────────────────────────

def _b64url_decode(data: str) -> bytes:
    data = re.sub(r"\s+", "", data)
    data += "=" * (-len(data) % 4)
    try:
        return base64.b64decode(data, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ConfigImportError("Ссылка vpn:// повреждена: не base64")


def _inflate_limited(data: bytes) -> bytes:
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, MAX_IMPORT_BYTES + 1)
    except zlib.error:
        raise ConfigImportError("Ссылка vpn:// повреждена: не удалось распаковать")
    if len(out) > MAX_IMPORT_BYTES or d.unconsumed_tail:
        raise ConfigImportError("Ссылка vpn:// распаковывается в слишком большой объём")
    return out


def _json_object(data: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _decode_vpn_payload(blob: bytes) -> Dict[str, Any]:
    """AmneziaVPN packs the JSON with Qt qCompress (4-byte big-endian length +
    zlib stream); older links carry plain JSON."""
    if len(blob) > 4 and blob[4:5] == b"\x78":  # zlib header
        obj = _json_object(_inflate_limited(blob[4:]))
    else:
        obj = _json_object(blob)
    if obj is None:
        raise ConfigImportError("Ссылка vpn:// повреждена: внутри не JSON")
    return obj


def _is_ipv4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


@dataclass(frozen=True)
class _Container:
    name: str
    config: str


def _containers(obj: Dict[str, Any]) -> List[_Container]:
    """AWG/WireGuard configs in the link, in the order AmneziaVPN picks them."""
    found: List[_Container] = []
    raw = obj.get("containers")
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        for proto in ("awg", "wireguard"):
            section = c.get(proto)
            last = section.get("last_config") if isinstance(section, dict) else None
            if isinstance(last, str):
                last = _json_object(last.encode())
            if isinstance(last, dict) and isinstance(last.get("config"), str):
                found.append(_Container(str(c.get("container", "")), str(last["config"])))
                break
    default = obj.get("defaultContainer")
    return sorted(found, key=lambda c: c.name != default)


def decode_vpn_uri(uri: str) -> str:
    """Returns the AmneziaWG/WireGuard config carried by a vpn:// link."""
    uri = uri.strip()
    if not uri.lower().startswith("vpn://"):
        raise ConfigImportError("Ссылка должна начинаться с vpn://")
    obj = _decode_vpn_payload(_b64url_decode(uri[len("vpn://"):]))
    containers = _containers(obj)
    if not containers:
        raise ConfigImportError(
            "В ссылке vpn:// нет конфига AmneziaWG/WireGuard (другие протоколы Amnezia не поддерживаются)")

    dns1 = obj["dns1"] if _is_ipv4(obj.get("dns1")) else AMNEZIA_FALLBACK_DNS[0]
    dns2 = obj["dns2"] if _is_ipv4(obj.get("dns2")) else AMNEZIA_FALLBACK_DNS[1]
    return containers[0].config.replace("$PRIMARY_DNS", dns1).replace("$SECONDARY_DNS", dns2)


# ── .conf ───────────────────────────────────────────────────────────

Section = Tuple[str, List[Tuple[str, str]]]


def parse_conf_sections(content: str) -> List[Section]:
    """[(section_lower, [(key_lower, value)])] in file order."""
    sections: List[Section] = []
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        header = re.match(r"^\[(\w+)\]$", line)
        if header:
            sections.append((header.group(1).lower(), []))
        elif "=" in line and sections:
            key, value = line.split("=", 1)
            sections[-1][1].append((key.strip().lower(), value.strip()))
    return sections


@dataclass(frozen=True)
class AwgConfInfo:
    mtu: Optional[int]
    hooks: Tuple[str, ...]  # canonical names, e.g. ("PostUp",)


def inspect_awg_conf(content: str) -> AwgConfInfo:
    """Reads what the plugin acts on (hooks, MTU). Full validation is left to
    `awg setconf` at connect time; we only make sure this is a WireGuard-style
    file at all, so a random .txt isn't stored as a config."""
    sections = parse_conf_sections(content)
    iface_kv = next((kv for name, kv in sections if name == "interface"), None)
    if iface_kv is None or not any(name == "peer" for name, _ in sections):
        raise ConfigImportError("Это не конфиг AmneziaWG/WireGuard: нужны секции [Interface] и [Peer]")

    iface = dict(iface_kv)
    mtu: Optional[int] = None
    if "mtu" in iface:
        try:
            mtu = int(iface["mtu"])
        except ValueError:
            raise ConfigImportError(f"Некорректный MTU: {iface['mtu']}")
    return AwgConfInfo(mtu=mtu, hooks=tuple(HOOK_KEYS[k] for k in HOOK_KEYS if k in iface))


def set_conf_mtu(content: str, mtu: int) -> str:
    """Sets MTU in [Interface], replacing an existing value."""
    out: List[str] = []
    in_iface = False
    written = False
    for line in content.splitlines():
        stripped = line.strip()
        header = re.match(r"^\[(\w+)\]", stripped)
        if header:
            if in_iface and not written:
                out.append(f"MTU = {mtu}")
                written = True
            in_iface = header.group(1).lower() == "interface"
        elif in_iface and re.match(r"^mtu\s*=", stripped, re.IGNORECASE):
            if not written:
                out.append(f"MTU = {mtu}")
                written = True
            continue
        out.append(line)
    if in_iface and not written:
        out.append(f"MTU = {mtu}")
    return "\n".join(out) + "\n"


# ── detection ───────────────────────────────────────────────────────

def _single_link(text: str, scheme: str) -> Optional[str]:
    links = [l.strip() for l in text.splitlines() if l.strip().lower().startswith(scheme)]
    if len(links) > 1:
        raise ConfigImportError(f"В файле несколько ссылок {scheme}, оставьте одну")
    return links[0] if links else None


def _plan_awg(content: str, from_link: bool, mtu_override: Optional[int]) -> ImportPlan:
    info = inspect_awg_conf(content)
    warnings: List[str] = []
    if info.hooks:
        hooks = ", ".join(info.hooks)
        if from_link:
            raise ConfigImportError(
                f"Ссылка vpn:// содержит команды {hooks}. Такие ссылки не импортируются: "
                "команды выполнились бы от root")
        warnings.append(f"В конфиге есть {hooks}: эти команды выполнятся от root при подключении")

    mtu = info.mtu
    if mtu_override is not None:
        if not MTU_MIN <= mtu_override <= MTU_MAX:
            raise ConfigImportError(f"MTU должен быть в диапазоне {MTU_MIN}–{MTU_MAX}")
        content = set_conf_mtu(content, mtu_override)
        mtu = mtu_override

    if not content.endswith("\n"):
        content += "\n"
    return ImportPlan(
        kind=ConfigKind.AWG,
        content=content,
        label=ConfigKind.AWG.label,
        mtu=mtu,
        mtu_warning=mtu is None or mtu > RECOMMENDED_AWG_MTU,
        warnings=tuple(warnings),
    )


def _plan_subscription(url: str, server: Optional[str], fetch: Optional[Callable[[str], str]]) -> ImportPlan:
    if fetch is None:
        raise ConfigImportError("Подписки здесь не поддерживаются")
    servers = parse_subscription(fetch(url))
    names = tuple(s.name for s in servers)
    picked = find_server(servers, server) if server else servers[0]
    if picked is None:
        raise ConfigImportError(f"Сервера «{server}» нет в подписке")
    return ImportPlan(
        kind=ConfigKind.SUBSCRIPTION,
        content=SubscriptionConfig(url=url, server=picked.name, link=picked.link).dumps(),
        label=f"Подписка, серверов: {len(servers)}",
        servers=names,
    )


def plan_import(text: str, mtu_override: Optional[int] = None, server: Optional[str] = None,
                fetch: Optional[Callable[[str], str]] = None) -> ImportPlan:
    """Detects what `text` is and returns what to store.

    The stored content is: the .conf text for AWG, the link itself for VLESS
    (the sing-box config is generated at every start, so template fixes reach
    old imports), the normalised JSON for sing-box, or the subscription URL
    with the picked `server` (first one by default) for a subscription.
    """
    stripped = text.strip()

    if is_subscription_url(stripped):
        return _plan_subscription(stripped, server, fetch)

    vless = _single_link(stripped, "vless://")
    if vless:
        return ImportPlan(kind=ConfigKind.VLESS, content=vless + "\n", label=vless_label(vless))

    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError as e:
            raise ConfigImportError(f"Некорректный JSON: {e}")
        cfg = validate_user_singbox(obj)
        return ImportPlan(kind=ConfigKind.SINGBOX, content=json.dumps(cfg, indent=2) + "\n",
                          label=ConfigKind.SINGBOX.label)

    vpn = _single_link(stripped, "vpn://")
    if vpn:
        return _plan_awg(decode_vpn_uri(vpn), from_link=True, mtu_override=mtu_override)
    return _plan_awg(unflatten_conf(text), from_link=False, mtu_override=mtu_override)
