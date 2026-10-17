"""Tunnel backends and the controller that switches between configs.

One config runs at a time: all configs are whole-device tunnels, and two of
them would fight over routing. The controller owns the lock that serializes
every start/stop, from RPC handlers and the watcher thread alike.

The user's intent is `settings.last_active`: set when they turn a config on,
cleared when they turn it off. The watcher and restore-on-boot only ever act
towards that intent.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Callable, Dict, List, Optional, Protocol

import decky

from .config_manager import ConfigManager
from .models import ConfigKind, OpResult, StoredConfig
from .service_manager import MANAGED_PREFIX, ServiceManager
from .settings import Settings
from .errors import ConfigImportError
from .singbox import VlessOptions, build_vless_config, is_ip, prepare_user_singbox, vless_server
from .singbox_manager import SingBoxManager
from .subscription import Fetcher, SubscriptionConfig, refresh

Resolver = Callable[[str], Optional[str]]


class TunnelBackend(Protocol):
    def start(self, cfg: StoredConfig) -> OpResult: ...
    def stop(self, interface: str) -> OpResult: ...
    def running_interfaces(self) -> List[str]: ...


class AwgBackend:
    def __init__(self, service: ServiceManager):
        self.service = service

    def start(self, cfg: StoredConfig) -> OpResult:
        r = self.service.start_interface(cfg.interface)
        return OpResult(r["success"], r["error"])

    def stop(self, interface: str) -> OpResult:
        r = self.service.stop_interface(interface)
        return OpResult(r["success"], r["error"])

    def running_interfaces(self) -> List[str]:
        return [i for i in self.service.interfaces() if i.startswith(MANAGED_PREFIX)]


def resolve_ipv4(host: str) -> Optional[str]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    return str(infos[0][4][0]) if infos else None


class SingBoxBackend:
    """Runs VLESS links and subscriptions (config generated at every start, so
    template fixes reach earlier imports) and user sing-box JSON."""

    def __init__(self, manager: SingBoxManager, configs: ConfigManager, settings: Settings,
                 fetch: Fetcher, resolver: Resolver = resolve_ipv4):
        self.manager = manager
        self.configs = configs
        self.settings = settings
        self.fetch = fetch
        self.resolver = resolver

    def _subscription_link(self, cfg: StoredConfig) -> str:
        """Server's current link; falls back to the stored one when offline
        or the subscription changed shape."""
        stored = SubscriptionConfig.loads(self.configs.read(cfg))
        try:
            fresh = refresh(stored, self.fetch)
        except ConfigImportError as e:
            decky.logger.warning(f"{cfg.name}: subscription refresh failed, using stored link: {e}")
            return stored.link
        if fresh != stored:
            self.configs.write(cfg.name, fresh.dumps(), ConfigKind.SUBSCRIPTION)
        return fresh.link

    def start(self, cfg: StoredConfig) -> OpResult:
        content = self.configs.read(cfg)
        if cfg.kind is ConfigKind.SUBSCRIPTION:
            content = self._subscription_link(cfg)
        if cfg.kind in (ConfigKind.VLESS, ConfigKind.SUBSCRIPTION):
            server = vless_server(content)
            server_ip = None if is_ip(server) else self.resolver(server)
            if not is_ip(server) and server_ip is None:
                return OpResult.fail(f"Не удалось получить адрес сервера {server}")
            options = VlessOptions(dns_server=self.settings.get("vless_dns_server"),
                                   ipv4_only=self.settings.get("vless_ipv4_only"))
            config = build_vless_config(content, cfg.interface, server_ip, options)
        else:
            config = prepare_user_singbox(json.loads(content), cfg.interface)
        return self.manager.start(cfg.interface, config)

    def stop(self, interface: str) -> OpResult:
        return self.manager.stop(interface)

    def running_interfaces(self) -> List[str]:
        return self.manager.running_interfaces()


class TunnelController:
    def __init__(self, configs: ConfigManager, backends: Dict[ConfigKind, TunnelBackend],
                 settings: Settings):
        self.configs = configs
        self.backends = backends
        self.settings = settings
        self.lock = threading.RLock()
        # Bumped on every user action; background tasks compare it to notice
        # the user stepped in and back off.
        self.generation = 0

    def _backend(self, cfg: StoredConfig) -> TunnelBackend:
        return self.backends[cfg.kind]

    def running_interfaces(self) -> List[str]:
        seen: List[str] = []
        for backend in {id(b): b for b in self.backends.values()}.values():
            seen.extend(i for i in backend.running_interfaces() if i not in seen)
        return seen

    def running(self) -> List[StoredConfig]:
        ifaces = set(self.running_interfaces())
        return [c for c in self.configs.list_managed() if c.interface in ifaces]

    # ── user actions ─────────────────────────────────────────────────
    def connect(self, name: str) -> OpResult:
        with self.lock:
            self.generation += 1
            cfg = self.configs.find(name)
            if cfg is None:
                return OpResult.fail(f"Конфиг {name} не найден")
            result = self._switch_to(cfg)
            if result.success:
                self.settings.set("last_active", cfg.name)
            return result

    def disconnect(self, name: str) -> OpResult:
        with self.lock:
            self.generation += 1
            cfg = self.configs.find(name)
            if cfg is None:
                return OpResult.fail(f"Конфиг {name} не найден")
            result = self._backend(cfg).stop(cfg.interface)
            self._forget(cfg)
            return result

    def release(self, name: str) -> None:
        """Before deleting a config: stop it if running and drop the intent."""
        with self.lock:
            self.generation += 1
            cfg = self.configs.find(name)
            if cfg is None:
                return
            if cfg.interface in self.running_interfaces():
                stopped = self._backend(cfg).stop(cfg.interface)
                if not stopped.success:
                    decky.logger.warning(f"Stop before delete failed (continuing): {stopped.error}")
            self._forget(cfg)

    def reload(self, name: str) -> Optional[OpResult]:
        """After a config was re-imported: restart it if it is running, so
        the new content (e.g. another server) takes effect. None if it's off."""
        with self.lock:
            cfg = self.configs.find(name)
            if cfg is None or cfg.interface not in self.running_interfaces():
                return None
            return self.restart(cfg)

    # ── background actions ───────────────────────────────────────────
    def restart(self, cfg: StoredConfig) -> OpResult:
        """down + up as one step; used by the watcher."""
        with self.lock:
            backend = self._backend(cfg)
            backend.stop(cfg.interface)
            return backend.start(cfg)

    def ensure_up(self, name: str, generation: int) -> Optional[OpResult]:
        """Starts `name` unless the user acted since `generation` or it is
        already running. Returns None when nothing was attempted."""
        with self.lock:
            if generation != self.generation or self.settings.get("last_active") != name:
                return None
            cfg = self.configs.find(name)
            if cfg is None or cfg.interface in self.running_interfaces():
                return None
            return self._switch_to(cfg)

    # ── internals ────────────────────────────────────────────────────
    def _forget(self, cfg: StoredConfig) -> None:
        if self.settings.get("last_active") == cfg.name:
            self.settings.set("last_active", None)

    def _switch_to(self, cfg: StoredConfig) -> OpResult:
        previous = [c for c in self.running() if c.name != cfg.name]
        for p in previous:
            stopped = self._backend(p).stop(p.interface)
            if not stopped.success:
                return OpResult.fail(f"Не удалось выключить {p.name}: {stopped.error}")

        result = self._backend(cfg).start(cfg)
        if not result.success and previous:
            decky.logger.warning(f"Starting {cfg.name} failed, bringing back {previous[0].name}")
            back = self._backend(previous[0]).start(previous[0])
            if not back.success:
                decky.logger.error(f"Rollback to {previous[0].name} failed: {back.error}")
        return result
