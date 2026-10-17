import asyncio
import functools
import os
import threading
import time
import traceback as _traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from vpn_deck import (
    USER_EDITABLE,
    AwgBackend,
    BinaryManager,
    CommandRunner,
    ConfigImportError,
    ConfigKind,
    ConfigManager,
    Json,
    StoredConfig,
    SubscriptionConfig,
    Diagnostics,
    NetworkWatcher,
    ServiceManager,
    Settings,
    SingBoxBackend,
    SingBoxManager,
    StatusProbe,
    SubprocessRunner,
    TunnelController,
    is_online,
    plan_import,
    read_import_file,
    read_routes,
    restore_last_active,
    curl_fetcher,
    parse_subscription,
    to_rpc,
)
from vpn_deck.singbox import vless_label

import decky

Rpc = Callable[..., Awaitable[Any]]


def _rpc(func: Rpc) -> Rpc:
    @functools.wraps(func)
    async def wrapper(self: "Plugin", *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(self, *args, **kwargs)
        except ConfigImportError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            tb = _traceback.format_exc()
            decky.logger.error(f"{func.__name__} exception: {tb}")
            self.record_error(func.__name__, type(e).__name__, str(e), {"traceback": tb})
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    return wrapper


def _arg(value: Any, key: str) -> Any:
    """The frontend calls some endpoints with {key: value}; accept both."""
    return value.get(key, "") if isinstance(value, dict) else value


@dataclass
class Deps:
    """Everything Plugin talks to; tests pass fakes."""
    binary_manager: Any
    config_manager: ConfigManager
    service_manager: ServiceManager
    singbox_manager: SingBoxManager
    settings: Settings
    runner: CommandRunner
    diagnostics: Diagnostics

    @classmethod
    def real(cls) -> "Deps":
        binaries = BinaryManager()
        runner = SubprocessRunner()
        return cls(
            binary_manager=binaries,
            config_manager=ConfigManager(),
            service_manager=ServiceManager(binaries, runner),
            singbox_manager=SingBoxManager(binaries, runner=runner),
            settings=Settings(),
            runner=runner,
            diagnostics=Diagnostics(),
        )


class Plugin:
    def __init__(self, deps: Optional[Deps] = None):
        """Инициализация плагина"""
        self.errors: List[Json] = []
        self.max_errors = 50

        d = deps or Deps.real()
        self.binary_manager = d.binary_manager
        self.config_manager = d.config_manager
        self.service_manager = d.service_manager
        self.settings = d.settings
        self.runner = d.runner
        self.diagnostics = d.diagnostics

        self.fetch = curl_fetcher(d.runner)
        singbox = SingBoxBackend(d.singbox_manager, d.config_manager, d.settings, self.fetch)
        self.controller = TunnelController(
            d.config_manager,
            {ConfigKind.AWG: AwgBackend(d.service_manager),
             ConfigKind.VLESS: singbox,
             ConfigKind.SINGBOX: singbox,
             ConfigKind.SUBSCRIPTION: singbox},
            d.settings,
        )
        self.status_probe = StatusProbe(d.runner, d.service_manager)
        self.watcher = NetworkWatcher(self.controller, d.service_manager, d.runner,
                                      report_error=lambda op, msg: self.record_error(op, "NetworkWatch", msg))
        self._stop = threading.Event()
        self._restore_thread: Optional[threading.Thread] = None

    def record_error(self, operation: str, error_type: str, message: str, details: Optional[Json] = None):
        """Добавляет ошибку в историю"""
        self.errors.append({
            "timestamp": time.time(),
            "operation": operation,
            "error_type": error_type,
            "message": message,
            "details": details or {},
        })
        if len(self.errors) > self.max_errors:
            self.errors = self.errors[-self.max_errors:]
        decky.logger.error(f"VPN Error [{error_type}] in {operation}: {message}")

    def _online(self) -> bool:
        return is_online(read_routes(self.runner))

    def _restore(self) -> None:
        result = restore_last_active(self.controller, self.settings, self._online, self._stop)
        if result is not None and not result.success:
            self.record_error("restore", "ServiceError", result.error or "unknown")

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        decky.logger.info("VPN Deck plugin initialized")
        try:
            repair = await self.config_manager.repair_symlinks()
            if repair["repaired"]:
                decky.logger.info(f"Auto-repaired {repair['repaired']}/{repair['total']} symlinks on startup")
        except Exception as e:
            decky.logger.error(f"Symlink auto-repair failed: {e}")
        self._stop.clear()
        self.watcher.start()
        self._restore_thread = threading.Thread(target=self._restore, name="vpn-deck-restore", daemon=True)
        self._restore_thread.start()

    # Function called first during the unload process, utilize this to handle your plugin being stopped, but not
    # completely removed
    async def _unload(self):
        decky.logger.info("VPN Deck plugin unloading")
        self._stop.set()
        await asyncio.to_thread(self.watcher.stop)
        if self._restore_thread is not None:
            await asyncio.to_thread(self._restore_thread.join, 90)

    # Function called after `_unload` during uninstall, utilize this to clean up processes and other remnants of your
    # plugin that may remain on the system
    async def _uninstall(self):
        decky.logger.info("VPN Deck plugin uninstalling")

    # Migrations that should be performed before entering `_main()`.
    async def _migration(self):
        decky.logger.info("VPN Deck plugin migrating")

    # ── tunnels ──────────────────────────────────────────────────────

    @_rpc
    async def vpn_status_all(self) -> List[Json]:
        """Возвращает статус всех активных VPN интерфейсов"""
        return self.service_manager.get_all_statuses()

    @_rpc
    async def vpn_stop_all(self, only_managed: bool = False) -> Json:
        """Останавливает все (или только managed) VPN интерфейсы"""
        return self.service_manager.stop_all_interfaces(only_managed)

    def _label(self, cfg: StoredConfig) -> str:
        try:
            if cfg.kind is ConfigKind.VLESS:
                return vless_label(self.config_manager.read(cfg))
            if cfg.kind is ConfigKind.SUBSCRIPTION:
                return f"Подписка · {SubscriptionConfig.loads(self.config_manager.read(cfg)).server}"
        except (OSError, ValueError, KeyError, ConfigImportError):
            pass
        return cfg.kind.label

    def _subscription(self, name: str) -> Optional[SubscriptionConfig]:
        cfg = self.config_manager.find(name)
        if cfg is None or cfg.kind is not ConfigKind.SUBSCRIPTION:
            return None
        try:
            return SubscriptionConfig.loads(self.config_manager.read(cfg))
        except (OSError, ValueError, KeyError):
            return None

    @_rpc
    async def list_configs_with_status(self) -> List[Json]:
        running = set(await asyncio.to_thread(self.controller.running_interfaces))
        result: List[Json] = []
        for c in self.config_manager.list_managed():
            sub = self._subscription(c.name) if c.kind is ConfigKind.SUBSCRIPTION else None
            result.append({
                "name": c.name,
                "kind": c.kind.value,
                "label": self._label(c),
                "interface": c.interface,
                "managed_by": "vpn-deck",
                "active": c.interface in running,
                "server": sub.server if sub else None,
            })
        return result

    @_rpc
    async def get_subscription_servers(self, name: Any) -> Json:
        """Server names currently in a subscription (fetched now)."""
        sub = self._subscription(_arg(name, "name"))
        if sub is None:
            return {"success": False, "error": "Это не подписка"}
        servers = parse_subscription(await asyncio.to_thread(self.fetch, sub.url))
        return {"success": True, "error": None, "servers": [s.name for s in servers], "current": sub.server}

    @_rpc
    async def set_subscription_server(self, name: Any, server: str = "") -> Json:
        """Switches a subscription to another server; reconnects if it is on."""
        if isinstance(name, dict):
            name, server = name.get("name", ""), name.get("server", "")
        sub = self._subscription(name)
        if sub is None:
            return {"success": False, "error": "Это не подписка"}
        servers = parse_subscription(await asyncio.to_thread(self.fetch, sub.url))
        picked = next((s for s in servers if s.name == server), None)
        if picked is None:
            return {"success": False, "error": f"Сервера «{server}» нет в подписке"}
        updated = SubscriptionConfig(url=sub.url, server=picked.name, link=picked.link)
        self.config_manager.write(name, updated.dumps(), ConfigKind.SUBSCRIPTION)
        reloaded = await asyncio.to_thread(self.controller.reload, name)
        if reloaded is not None and not reloaded.success:
            return {"success": False, "error": f"Сервер сменён, но не переподключился: {reloaded.error}"}
        return {"success": True, "error": None}

    async def _toggle(self, config_name: Any, action: Callable[[str], Any], op: str) -> Json:
        name = _arg(config_name, "config_name")
        if not name:
            return {"success": False, "error": "config_name is required", "interface": ""}
        interface = self.config_manager.get_interface_name(name)
        result = await asyncio.to_thread(action, name)
        if not result.success:
            self.record_error(op, "ServiceError", result.error or "unknown", {"interface": interface})
        self.status_probe.forget(interface)
        return {"success": result.success, "error": result.error, "interface": interface}

    @_rpc
    async def vpn_start_config(self, config_name: Any) -> Json:
        return await self._toggle(config_name, self.controller.connect, "start")

    @_rpc
    async def vpn_stop_config(self, config_name: Any) -> Json:
        return await self._toggle(config_name, self.controller.disconnect, "stop")

    @_rpc
    async def get_connection_status(self) -> Optional[Json]:
        """Status of the running config, or None when nothing is on."""
        running = await asyncio.to_thread(self.controller.running)
        if not running:
            return None
        status = await asyncio.to_thread(self.status_probe.status, running[0])
        return to_rpc(status)

    # ── settings ─────────────────────────────────────────────────────

    @_rpc
    async def get_settings(self) -> Json:
        return {k: v for k, v in self.settings.all().items() if k in USER_EDITABLE}

    @_rpc
    async def set_setting(self, key: str, value: Any = None) -> Json:
        if isinstance(key, dict):
            key, value = key.get("key", ""), key.get("value")
        if key not in USER_EDITABLE:
            return {"success": False, "error": f"Unknown setting: {key}"}
        self.settings.set(key, value)
        return {"success": True, "error": None}

    # ── errors ───────────────────────────────────────────────────────

    @_rpc
    async def get_errors(self) -> List[Json]:
        """Возвращает историю ошибок"""
        return list(self.errors)

    @_rpc
    async def clear_errors(self) -> bool:
        """Очищает историю ошибок"""
        self.errors = []
        decky.logger.info("Error history cleared")
        return True

    # ── binaries ─────────────────────────────────────────────────────

    @_rpc
    async def get_binaries_info(self) -> Dict[str, Dict[str, Optional[str]]]:
        return self.binary_manager.get_binaries_info()

    @_rpc
    async def check_binaries(self) -> Dict[str, bool]:
        return {name: path is not None for name, path in self.binary_manager.detect_binaries().items()}

    # ── configs ──────────────────────────────────────────────────────

    @_rpc
    async def list_all_configs(self) -> List[Json]:
        return await self.config_manager.list_all_configs()

    @_rpc
    async def scan_existing_configs(self) -> Dict[str, List[Json]]:
        return await self.config_manager.scan_existing_configs()

    @_rpc
    async def inspect_import(self, path: Any = "") -> Json:
        """What importing `path` would do, without storing anything."""
        path = _arg(path, "path")
        if not os.path.isfile(path):
            return {"success": False, "error": "Файл не найден"}
        plan = await asyncio.to_thread(plan_import, read_import_file(path), fetch=self.fetch)
        return {"success": True, "error": None, "kind": plan.kind.value, "label": plan.label,
                "mtu": plan.mtu, "mtu_warning": plan.mtu_warning, "warnings": list(plan.warnings),
                "servers": list(plan.servers)}

    @_rpc
    async def import_vpn_config(self, name: str, path: str = "", mtu: Optional[int] = None,
                                server: Optional[str] = None) -> Json:
        decky.logger.info(f"import_vpn_config: name={name}, path={path}, mtu={mtu}, server={server}")
        if not os.path.isfile(path):
            return {"success": False, "error": "Файл не найден"}
        plan = await asyncio.to_thread(plan_import, read_import_file(path), mtu_override=mtu,
                                       server=server, fetch=self.fetch)
        cfg = self.config_manager.write(name, plan.content, plan.kind)
        reloaded = await asyncio.to_thread(self.controller.reload, cfg.name)
        if reloaded is not None and not reloaded.success:
            self.record_error("reload", "ServiceError", reloaded.error or "unknown", {"interface": cfg.interface})
            return {"success": False, "error": f"Конфиг сохранён, но не переподключился: {reloaded.error}"}
        return {"success": True, "error": "", "kind": plan.kind.value, "warnings": list(plan.warnings)}

    @_rpc
    async def delete_vpn_config(self, name: Any) -> Json:
        """Останавливает интерфейс, если он поднят, и удаляет конфиг (без бэкапа)."""
        name = _arg(name, "name")
        if not name:
            return {"success": False, "config_name": None, "error": "name is required"}
        await asyncio.to_thread(self.controller.release, name)
        return await self.config_manager.delete_config(name)

    @_rpc
    async def repair_symlinks(self) -> Json:
        """Re-creates missing symlinks in /etc/amnezia/amneziawg/ (SteamOS updates reset /etc)."""
        return await self.config_manager.repair_symlinks()

    @_rpc
    async def diagnose_connectivity(self, targets: Optional[List[Json]] = None) -> List[Json]:
        """Runs ping/HTTP probes against default or custom targets."""
        return await asyncio.to_thread(self.diagnostics.check, targets)

    @_rpc
    async def get_vpn_config(self, name: str) -> Optional[str]:
        return await self.config_manager.get_config_content(name)
