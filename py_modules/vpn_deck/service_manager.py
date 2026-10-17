"""
ServiceManager - Manages AmneziaWG interface lifecycle via awg-quick
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import decky

from ._utils import clean_env
from .models import BinaryLocator
from .runner import RC_NOT_FOUND, RC_TIMEOUT, CommandRunner, SubprocessRunner

MANAGED_PREFIX = "vd-"
DEFAULT_INTERFACE = "awg0"

# awg-quick up/down может долго выполняться (маршруты, DNS, резолвы)
START_STOP_TIMEOUT_SEC = 60
AWG_QUICK_LOG = os.path.join(decky.DECKY_PLUGIN_LOG_DIR, "awg-quick.log")

Legacy = Dict[str, Any]


class ServiceManager:
    def __init__(self, binary_manager: BinaryLocator, runner: Optional[CommandRunner] = None):
        self.binary_manager = binary_manager
        self.runner = runner or SubprocessRunner()

    def _run(self, cmd: Sequence[str], timeout: int = 10, quiet: bool = False) -> Tuple[int, str, str]:
        log = decky.logger.debug if quiet else decky.logger.info
        log(f"Running: {' '.join(cmd)}")
        r = self.runner.run(cmd, timeout=timeout)
        if r.returncode == RC_TIMEOUT:
            decky.logger.warning(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        return (r.returncode, r.stdout.strip(), r.stderr.strip())

    def _run_logged(self, cmd: Sequence[str], timeout: int = 60) -> Tuple[int, str, str]:
        """Run a long-lived command with output redirected to a log file.

        awg-quick spawns background processes that inherit pipe FDs,
        causing subprocess.run with capture_output=True to hang.
        Writing to a file + start_new_session avoids this.
        """
        cmd_str = " ".join(cmd)
        decky.logger.info(f"Running (logged): {cmd_str}")
        try:
            with open(AWG_QUICK_LOG, "a") as log_file:
                log_file.write(f"\n--- {datetime.now().isoformat()} | {cmd_str} ---\n")
                log_file.flush()
                result = subprocess.run(
                    list(cmd),
                    stdout=log_file,
                    stderr=log_file,
                    text=True,
                    timeout=timeout,
                    start_new_session=True,
                    env=clean_env(),
                )
                log_file.write(f"--- RC: {result.returncode} ---\n")

            stderr = ""
            if result.returncode != 0:
                try:
                    with open(AWG_QUICK_LOG, "r") as f:
                        stderr = "".join(f.readlines()[-20:]).strip()
                except OSError:
                    pass
            return (result.returncode, "", stderr)
        except subprocess.TimeoutExpired:
            decky.logger.warning(f"Command timed out after {timeout}s: {cmd_str}")
            return (RC_TIMEOUT, "", f"timeout after {timeout}s")
        except FileNotFoundError:
            decky.logger.warning(f"Command not found: {cmd[0]}")
            return (RC_NOT_FOUND, "", f"{cmd[0]}: command not found")

    # ── Why _run_logged instead of _run for start/stop ──────────────
    # awg-quick is a bash script that spawns `amneziawg-go` as a
    # background daemon.  The daemon inherits pipe FDs created by
    # subprocess.run(capture_output=True), so the parent process
    # blocks forever waiting for EOF on those pipes.
    #
    # _run_logged redirects output to a file and uses
    # start_new_session=True so child processes don't inherit our FDs.
    # ─────────────────────────────────────────────────────────────────

    def _awg_quick(self, action: str, interface: str) -> Legacy:
        awg_quick_path = self.binary_manager.get_binary_path("awg-quick")
        if awg_quick_path is None:
            return {"success": False, "interface": interface, "method": None, "error": "awg-quick binary not found"}
        rc, _, stderr = self._run_logged([awg_quick_path, action, interface], timeout=START_STOP_TIMEOUT_SEC)
        if rc == 0:
            decky.logger.info(f"awg-quick {action} {interface}: ok")
            return {"success": True, "interface": interface, "method": "awg-quick", "error": None}
        err = stderr or f"awg-quick {action} failed (rc={rc})"
        decky.logger.error(f"awg-quick {action} {interface}: {err}")
        return {"success": False, "interface": interface, "method": "awg-quick", "error": err}

    def start_interface(self, interface: str) -> Legacy:
        return self._awg_quick("up", interface)

    def stop_interface(self, interface: str) -> Legacy:
        return self._awg_quick("down", interface)

    # ── awg show ─────────────────────────────────────────────────────
    def _awg_show(self, *args: str) -> Optional[str]:
        awg_path = self.binary_manager.get_binary_path("awg")
        if awg_path is None:
            return None
        rc, stdout, _ = self._run([awg_path, "show", *args], quiet=True)
        return stdout if rc == 0 else None

    def interfaces(self) -> List[str]:
        out = self._awg_show("interfaces")
        return out.split() if out else []

    def _per_peer(self, interface: str, field: str) -> Dict[str, str]:
        out = self._awg_show(interface, field) or ""
        pairs = (line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        return {k: v.strip() for k, v in pairs}

    def latest_handshakes(self, interface: str) -> Dict[str, int]:
        """peer public key -> unix time of the latest handshake (0 = never)."""
        return {k: int(v) for k, v in self._per_peer(interface, "latest-handshakes").items() if v.isdigit()}

    def persistent_keepalives(self, interface: str) -> Dict[str, Optional[int]]:
        """peer public key -> keepalive interval in seconds, None when off."""
        return {k: int(v) if v.isdigit() else None
                for k, v in self._per_peer(interface, "persistent-keepalive").items()}

    def get_status(self, interface: str) -> Legacy:
        if self.binary_manager.get_binary_path("awg") is None:
            decky.logger.warning("awg binary not found, cannot get interface status")
            return {"interface": interface, "status": "unknown", "peers": []}
        out = self._awg_show(interface)
        return {
            "interface": interface,
            "status": "active" if out is not None else "inactive",
            "peers": self._parse_awg_show(out) if out is not None else [],
        }

    def _parse_awg_show(self, output: str) -> List[Legacy]:
        peers: List[Legacy] = []
        current: Optional[Legacy] = None
        for line in output.splitlines():
            if line.startswith("peer:"):
                if current is not None:
                    peers.append(current)
                current = {
                    "public_key": line[len("peer:"):].strip(),
                    "endpoint": None,
                    "latest_handshake": None,
                    "transfer_rx": None,
                    "transfer_tx": None,
                }
            elif current is not None:
                stripped = line.strip()
                if stripped.startswith("endpoint:"):
                    current["endpoint"] = stripped[len("endpoint:"):].strip()
                elif stripped.startswith("latest handshake:"):
                    current["latest_handshake"] = stripped[len("latest handshake:"):].strip()
                elif stripped.startswith("transfer:"):
                    # "1.23 MiB received, 456 KiB sent"
                    for part in stripped[len("transfer:"):].split(","):
                        part = part.strip()
                        if "received" in part:
                            current["transfer_rx"] = part.replace("received", "").strip()
                        elif "sent" in part:
                            current["transfer_tx"] = part.replace("sent", "").strip()
        if current is not None:
            peers.append(current)
        return peers

    def get_all_statuses(self) -> List[Legacy]:
        if self.binary_manager.get_binary_path("awg") is None:
            decky.logger.warning("awg binary not found, cannot list interfaces")
            return []
        return [self.get_status(iface) for iface in self.interfaces()]

    def stop_all_interfaces(self, only_managed: bool = False) -> Legacy:
        ifaces = self.interfaces()
        if only_managed:
            ifaces = [i for i in ifaces if i.startswith(MANAGED_PREFIX)]
        stopped: List[str] = []
        failed: List[str] = []
        for iface in ifaces:
            (stopped if self.stop_interface(iface)["success"] else failed).append(iface)
        return {"stopped": stopped, "failed": failed, "total": len(ifaces)}
