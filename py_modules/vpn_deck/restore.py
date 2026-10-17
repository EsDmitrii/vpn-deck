"""Brings back the config that was on before a reboot.

Waits for a default route (Wi-Fi may come up after the plugin), retrying for
RESTORE_WAIT_SEC. Any user action in the meantime cancels it (the
controller's generation changes).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import decky

from . import defaults
from .models import OpResult
from .settings import Settings
from .tunnels import TunnelController

RETRY_EVERY_SEC = 5


def restore_last_active(controller: TunnelController, settings: Settings, online: Callable[[], bool],
                        stop: threading.Event, clock: Callable[[], float] = time.monotonic) -> Optional[OpResult]:
    """Blocking; run in a thread. Returns the start result, or None if
    nothing was attempted (disabled, nothing to restore, cancelled, offline)."""
    name = settings.get("last_active")
    if not settings.get("restore_on_boot") or not name:
        return None
    generation = controller.generation
    deadline = clock() + defaults.RESTORE_WAIT_SEC
    last: Optional[OpResult] = None
    while clock() < deadline and not stop.is_set():
        if controller.generation != generation:
            return None
        if online():
            last = controller.ensure_up(name, generation)
            if last is None or last.success:
                if last is not None:
                    decky.logger.info(f"Restored {name} after start")
                return last
            decky.logger.warning(f"Restore {name} failed, will retry: {last.error}")
        stop.wait(RETRY_EVERY_SEC)
    return last
