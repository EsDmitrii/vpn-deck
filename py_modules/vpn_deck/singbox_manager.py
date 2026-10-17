"""SingBoxManager - runs sing-box as a detached TUN tunnel process.

The process outlives a plugin reload (like an awg-quick tunnel), so state
lives in PID files under run_dir and is verified via /proc on every access.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import decky

from . import defaults
from ._utils import clean_env
from .models import BinaryLocator, Json, OpResult
from .runner import CommandRunner, SubprocessRunner

BINARY = "sing-box"


@dataclass(frozen=True)
class SingBoxPaths:
    run_dir: str = defaults.SINGBOX_RUN_DIR
    log_dir: str = ""
    sys_net_dir: str = "/sys/class/net"
    proc_dir: str = "/proc"

    def pid(self, iface: str) -> str:
        return os.path.join(self.run_dir, f"{iface}.pid")

    def config(self, iface: str) -> str:
        return os.path.join(self.run_dir, f"{iface}.json")

    def log(self, iface: str) -> str:
        return os.path.join(self.log_dir, f"sing-box-{iface}.log")

    def net_device(self, iface: str) -> str:
        return os.path.join(self.sys_net_dir, iface)

    def cmdline(self, pid: int) -> str:
        return os.path.join(self.proc_dir, str(pid), "cmdline")


class SingBoxManager:
    def __init__(self, binaries: BinaryLocator, paths: Optional[SingBoxPaths] = None,
                 runner: Optional[CommandRunner] = None):
        self.binaries = binaries
        self.paths = paths or SingBoxPaths(log_dir=decky.DECKY_PLUGIN_LOG_DIR)
        self.runner = runner or SubprocessRunner()
        # Children started by this process, kept so exited ones get reaped.
        self._children: Dict[str, subprocess.Popen[bytes]] = {}

    # ── process state ────────────────────────────────────────────────
    def _read_pid(self, iface: str) -> Optional[int]:
        try:
            with open(self.paths.pid(iface)) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _pid_matches(self, pid: int, iface: str) -> bool:
        """True if `pid` is a live sing-box started with our config.
        Guards against signalling an unrelated process after PID reuse."""
        child = self._children.get(iface)
        if child is not None and child.pid == pid and child.poll() is not None:
            return False
        try:
            with open(self.paths.cmdline(pid), "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            return False
        if not argv[0]:
            return False  # zombie
        # argv[1] covers an interpreter running a script (used by tests).
        names = {os.path.basename(a.decode(errors="replace")) for a in argv[:2]}
        return BINARY in names and self.paths.config(iface).encode() in argv

    def is_running(self, iface: str) -> bool:
        pid = self._read_pid(iface)
        return pid is not None and self._pid_matches(pid, iface)

    def running_interfaces(self) -> List[str]:
        try:
            names = os.listdir(self.paths.run_dir)
        except OSError:
            return []
        ifaces = (n[: -len(".pid")] for n in names if n.endswith(".pid"))
        return sorted(i for i in ifaces if self.is_running(i))

    def _log_tail(self, iface: str, lines: int = 20) -> str:
        try:
            with open(self.paths.log(iface), errors="replace") as f:
                tail = f.readlines()[-lines:]
        except OSError:
            return ""
        fatal = [l for l in tail if "FATAL" in l or "ERROR" in l]
        return (fatal[-1] if fatal else "".join(tail)).strip()

    def _cleanup(self, iface: str) -> None:
        for path in (self.paths.pid(iface), self.paths.config(iface)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        child = self._children.pop(iface, None)
        if child is not None:
            child.poll()  # reap it if another manager instance stopped it

    def _write_config(self, iface: str, config: Json) -> str:
        os.makedirs(self.paths.run_dir, mode=0o700, exist_ok=True)
        path = self.paths.config(iface)
        # The config holds the VLESS UUID, i.e. the credential.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
        return path

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self, iface: str, config: Json) -> OpResult:
        binary = self.binaries.get_binary_path(BINARY)
        if binary is None:
            return OpResult.fail("sing-box binary not found")
        if self.is_running(iface):
            return OpResult.ok()
        self._cleanup(iface)  # leftovers of a process that is gone

        cfg_path = self._write_config(iface, config)
        check = self.runner.run([binary, "check", "-c", cfg_path], timeout=20)
        if not check.ok:
            self._cleanup(iface)
            lines = (check.stderr or check.stdout).strip().splitlines()
            return OpResult.fail(f"sing-box check: {lines[-1] if lines else 'failed'}")

        with open(self.paths.log(iface), "wb") as log:
            proc = subprocess.Popen([binary, "run", "-c", cfg_path], stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, env=clean_env())
        self._children[iface] = proc
        with open(self.paths.pid(iface), "w") as f:
            f.write(str(proc.pid))

        deadline = time.monotonic() + defaults.SINGBOX_START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = self._log_tail(iface) or f"sing-box exited with code {proc.returncode}"
                self._cleanup(iface)
                return OpResult.fail(err)
            if os.path.exists(self.paths.net_device(iface)):
                decky.logger.info(f"sing-box {iface} up (pid {proc.pid})")
                return OpResult.ok()
            time.sleep(0.2)

        self.stop(iface)
        return OpResult.fail(f"sing-box: интерфейс {iface} не появился")

    def stop(self, iface: str) -> OpResult:
        pid = self._read_pid(iface)
        if pid is None or not self._pid_matches(pid, iface):
            self._cleanup(iface)
            return OpResult.ok()

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._cleanup(iface)
            return OpResult.ok()

        deadline = time.monotonic() + defaults.SINGBOX_STOP_TIMEOUT_SEC
        while time.monotonic() < deadline and self._pid_matches(pid, iface):
            time.sleep(0.2)
        if self._pid_matches(pid, iface):
            decky.logger.warning(f"sing-box {iface} ignored SIGTERM, killing")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child = self._children.get(iface)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._cleanup(iface)
        return OpResult.ok()
