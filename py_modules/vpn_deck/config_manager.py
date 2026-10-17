"""
ConfigManager - the plugin's config store.

Configs live in ~/.local/share/vpn-deck/configs as `<name><ext>` where the
extension encodes the kind (see ConfigKind.extension). AWG configs also get
a symlink in /etc/amnezia/amneziawg/ so awg-quick finds them by interface.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import decky

from .models import ConfigKind, StoredConfig

Legacy = Dict[str, Any]


class ConfigManager:
    def __init__(self):
        self.config_dir = os.path.expanduser("~/.local/share/vpn-deck/configs")
        self.system_config_dir = "/etc/amnezia/amneziawg"
        # Short to leave room for the name; IFNAMSIZ=16 incl. NUL.
        self.config_prefix = "vd-"
        self._ensure_directories()

    def _ensure_directories(self):
        try:
            os.makedirs(self.config_dir, mode=0o700, exist_ok=True)
        except OSError as e:
            decky.logger.error(f"Failed to create directory {self.config_dir}: {e}")

    def _sanitize_name(self, name: str) -> str:
        """Filesystem- and interface-safe name: a-z, 0-9, -, _."""
        name = os.path.basename(name)
        for kind in ConfigKind:
            if name.endswith(kind.extension):
                name = name[: -len(kind.extension)]
                break
        sanitized = re.sub(r"[^a-zA-Z0-9\-_]", "-", name)
        sanitized = re.sub(r"-+", "-", sanitized).strip("-").lower()
        return sanitized or "config"

    def get_interface_name(self, name: str) -> str:
        return f"{self.config_prefix}{self._sanitize_name(name)}"

    def _local_path(self, name: str, kind: ConfigKind) -> str:
        return os.path.join(self.config_dir, f"{name}{kind.extension}")

    def _system_path(self, interface: str) -> str:
        return os.path.join(self.system_config_dir, f"{interface}.conf")

    # ── typed API ────────────────────────────────────────────────────
    @staticmethod
    def _kind_of(filename: str) -> Optional[ConfigKind]:
        # Longest extension first: ".singbox.json" before a bare ".json".
        for kind in sorted(ConfigKind, key=lambda k: -len(k.extension)):
            if filename.endswith(kind.extension):
                return kind
        return None

    def list_managed(self) -> List[StoredConfig]:
        try:
            filenames = sorted(os.listdir(self.config_dir))
        except OSError:
            return []
        configs: List[StoredConfig] = []
        for filename in filenames:
            kind = self._kind_of(filename)
            if kind is None:
                continue
            name = filename[: -len(kind.extension)]
            configs.append(StoredConfig(
                name=name,
                kind=kind,
                interface=f"{self.config_prefix}{name}",
                path=os.path.join(self.config_dir, filename),
            ))
        return configs

    def find(self, name: str) -> Optional[StoredConfig]:
        sanitized = self._sanitize_name(name)
        return next((c for c in self.list_managed() if c.name == sanitized), None)

    def find_by_interface(self, interface: str) -> Optional[StoredConfig]:
        return next((c for c in self.list_managed() if c.interface == interface), None)

    @staticmethod
    def read(cfg: StoredConfig) -> str:
        with open(cfg.path) as f:
            return f.read()

    def write(self, name: str, content: str, kind: ConfigKind = ConfigKind.AWG) -> StoredConfig:
        """Stores a config, replacing any config of another kind with the same name."""
        sanitized = self._sanitize_name(name)
        interface = f"{self.config_prefix}{sanitized}"
        for other in ConfigKind:
            if other is not kind:
                self._remove_file(self._local_path(sanitized, other))

        local_path = self._local_path(sanitized, kind)
        fd = os.open(local_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(local_path, 0o600)
        decky.logger.info(f"Wrote config to {local_path}")

        if kind is ConfigKind.AWG:
            self._ensure_symlink(local_path, self._system_path(interface))
        else:
            self._remove_symlink(self._system_path(interface))
        return StoredConfig(name=sanitized, kind=kind, interface=interface, path=local_path)

    @staticmethod
    def _remove_file(path: str) -> None:
        try:
            os.unlink(path)
            decky.logger.info(f"Deleted {path}")
        except FileNotFoundError:
            pass

    @staticmethod
    def _remove_symlink(path: str) -> bool:
        if not os.path.islink(path):
            return False
        try:
            os.unlink(path)
            return True
        except OSError as e:
            decky.logger.warning(f"Failed to remove symlink {path}: {e}")
            return False

    def _ensure_symlink(self, local_path: str, system_path: str) -> Legacy:
        """Makes sure system_path is a symlink to local_path.

        SteamOS updates wipe /etc/amnezia/amneziawg/*, so this runs on import
        and on plugin load. Returns {"ok", "action": none|created|replaced|error, "error"}.
        """
        try:
            os.makedirs(os.path.dirname(system_path), mode=0o755, exist_ok=True)
            try:
                if os.readlink(system_path) == local_path:
                    return {"ok": True, "action": "none", "error": None}
                existed = True
            except OSError:
                existed = os.path.lexists(system_path)
            if existed:
                try:
                    os.unlink(system_path)
                except FileNotFoundError:
                    existed = False
            os.symlink(local_path, system_path)
            action = "replaced" if existed else "created"
            decky.logger.info(f"Symlink {action}: {system_path} -> {local_path}")
            return {"ok": True, "action": action, "error": None}
        except Exception as e:
            decky.logger.warning(f"Symlink failed for {system_path}: {e}")
            return {"ok": False, "action": "error", "error": str(e)}

    # ── dict API kept for RPC and existing callers ───────────────────
    async def scan_existing_configs(self) -> Dict[str, List[Legacy]]:
        """Managed configs plus user-created AWG configs in the system dir."""
        managed: List[Legacy] = []
        for c in self.list_managed():
            system_path = self._system_path(c.interface)
            managed.append({
                "name": c.name,
                "kind": c.kind.value,
                "interface": c.interface,
                "path": c.path,
                "system_path": system_path,
                "is_symlink": os.path.islink(system_path),
                "managed_by": "vpn-deck",
            })

        existing: List[Legacy] = []
        try:
            system_files = os.listdir(self.system_config_dir)
        except OSError:
            system_files = []
        for filename in system_files:
            if not filename.endswith(".conf") or filename.startswith(self.config_prefix):
                continue
            system_path = os.path.join(self.system_config_dir, filename)
            if os.path.islink(system_path) and os.readlink(system_path).startswith(self.config_dir):
                continue
            existing.append({
                "name": filename[:-5],
                "kind": ConfigKind.AWG.value,
                "interface": filename[:-5],
                "path": system_path,
                "managed_by": "user",
            })
        return {"managed": managed, "existing": existing}

    async def list_all_configs(self) -> List[Legacy]:
        scanned = await self.scan_existing_configs()
        return scanned["managed"] + scanned["existing"]

    async def get_config_content(self, name: str) -> Optional[str]:
        cfg = self.find(name)
        if cfg is None:
            return None
        try:
            return self.read(cfg)
        except OSError as e:
            decky.logger.error(f"Error reading config {cfg.path}: {e}")
            return None

    def write_config(self, name: str, content: str, kind: ConfigKind = ConfigKind.AWG) -> Legacy:
        cfg = self.write(name, content, kind)
        return {"success": True, "config_name": cfg.name, "interface_name": cfg.interface, "error": None}

    async def repair_symlinks(self) -> Legacy:
        """Re-creates missing/broken symlinks for managed AWG configs."""
        results: List[Legacy] = []
        for c in self.list_managed():
            if c.kind is not ConfigKind.AWG:
                continue
            r = self._ensure_symlink(c.path, self._system_path(c.interface))
            results.append({"name": c.name, "interface": c.interface, **r})
        repaired = sum(1 for r in results if r["ok"] and r["action"] in ("created", "replaced"))
        return {"total": len(results), "repaired": repaired, "results": results}

    async def delete_config(self, name: str) -> Legacy:
        sanitized = self._sanitize_name(name)
        result: Legacy = {"success": False, "config_name": sanitized, "symlink_removed": False, "error": None}
        cfg = self.find(sanitized)
        if cfg is None:
            result["error"] = f"Config '{sanitized}' not found"
            return result
        try:
            self._remove_file(cfg.path)
            result["symlink_removed"] = self._remove_symlink(self._system_path(cfg.interface))
            result["success"] = True
        except OSError as e:
            result["error"] = f"Deletion failed: {e}"
        return result
