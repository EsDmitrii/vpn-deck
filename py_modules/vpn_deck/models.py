"""Domain types shared across modules.

Modules exchange these dataclasses; plain dicts appear only at the RPC
boundary, produced by `to_rpc`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Protocol, Tuple

Json = Dict[str, Any]


class ConfigKind(str, Enum):
    AWG = "awg"
    VLESS = "vless"
    SINGBOX = "singbox"

    @property
    def extension(self) -> str:
        """File suffix in the plugin's config store."""
        return _EXTENSIONS[self]

    @property
    def label(self) -> str:
        return _LABELS[self]


_EXTENSIONS = {
    ConfigKind.AWG: ".conf",
    ConfigKind.VLESS: ".vless",
    ConfigKind.SINGBOX: ".singbox.json",
}
_LABELS = {
    ConfigKind.AWG: "AWG",
    ConfigKind.VLESS: "VLESS",
    ConfigKind.SINGBOX: "sing-box",
}


@dataclass(frozen=True)
class StoredConfig:
    """A config in the plugin's store (~/.local/share/vpn-deck/configs)."""
    name: str
    kind: ConfigKind
    interface: str
    path: str


@dataclass(frozen=True)
class ImportPlan:
    """What the importer decided to store for a picked file."""
    kind: ConfigKind
    content: str
    label: str
    mtu: Optional[int] = None
    # True when an AWG config's MTU is absent or above the recommended value.
    mtu_warning: bool = False
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OpResult:
    success: bool
    error: Optional[str] = None

    @classmethod
    def ok(cls) -> OpResult:
        return cls(True)

    @classmethod
    def fail(cls, error: str) -> OpResult:
        return cls(False, error)


@dataclass(frozen=True)
class ExitInfo:
    """Public address the traffic leaves from, per Cloudflare's trace."""
    ip: str
    country: Optional[str]
    checked_at: float


@dataclass(frozen=True)
class ConnectionStatus:
    name: str
    kind: ConfigKind
    interface: str
    addresses: Tuple[str, ...] = ()
    latest_handshake: Optional[int] = None  # unix time, AWG only
    rx_bytes_per_sec: Optional[float] = None
    tx_bytes_per_sec: Optional[float] = None
    exit: Optional[ExitInfo] = None


def to_rpc(value: Any) -> Any:
    """Converts dataclasses / enums / tuples into JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_rpc(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [to_rpc(v) for v in value]
    if isinstance(value, dict):
        return {k: to_rpc(v) for k, v in value.items()}
    return value


class BinaryLocator(Protocol):
    def get_binary_path(self, name: str) -> Optional[str]: ...
