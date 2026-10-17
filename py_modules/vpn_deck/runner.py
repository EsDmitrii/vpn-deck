"""Command execution behind a protocol, so modules can be tested with a fake."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

from ._utils import clean_env

RC_TIMEOUT = 124
RC_NOT_FOUND = 127


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], timeout: float = 10) -> CommandResult: ...


class SubprocessRunner:
    """Runs short commands with captured output and a cleaned environment.

    Not for awg-quick up/down: its daemon inherits the pipes and the call
    would hang (see ServiceManager._run_logged).
    """

    def run(self, argv: Sequence[str], timeout: float = 10) -> CommandResult:
        try:
            r = subprocess.run(list(argv), capture_output=True, text=True,
                               timeout=timeout, env=clean_env(), check=False)
            return CommandResult(r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired:
            return CommandResult(RC_TIMEOUT, "", f"timeout after {timeout}s")
        except FileNotFoundError:
            return CommandResult(RC_NOT_FOUND, "", f"{argv[0]}: command not found")
