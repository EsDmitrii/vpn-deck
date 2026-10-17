"""Settings - small JSON store in DECKY_PLUGIN_SETTINGS_DIR/settings.json."""

import json
import os
import threading
from typing import Any, Dict, Optional

import decky

from . import defaults

DEFAULTS: Dict[str, Any] = {
    "restore_on_boot": defaults.RESTORE_ON_BOOT,
    # Name of the config the user last turned on; None after turning it off.
    "last_active": None,
    "vless_dns_server": defaults.SINGBOX_DNS_SERVER,
    "vless_ipv4_only": defaults.SINGBOX_IPV4_ONLY,
}

# Settings the UI may change through set_setting().
USER_EDITABLE = {"restore_on_boot", "vless_dns_server", "vless_ipv4_only"}


def _valid(key: str, value: Any) -> bool:
    if key == "last_active":
        return value is None or isinstance(value, str)
    return isinstance(value, type(DEFAULTS[key]))


class Settings:
    def __init__(self, path: Optional[str] = None):
        settings_dir = getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", None) or os.path.expanduser(
            "~/.local/share/vpn-deck")
        self.path = path or os.path.join(settings_dir, "settings.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        data = dict(DEFAULTS)
        try:
            with open(self.path) as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                data.update({k: v for k, v in stored.items() if k in DEFAULTS and _valid(k, v)})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            decky.logger.warning(f"Ignoring unreadable settings {self.path}: {e}")
        return data

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, key: str) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key))

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        if not _valid(key, value):
            raise TypeError(f"{key}: unexpected type {type(value).__name__}")
        with self._lock:
            self._data[key] = value
            self._save()
