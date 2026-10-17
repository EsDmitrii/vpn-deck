"""Test doubles shared by the unit tests."""

from __future__ import annotations

import os
import sys
import tempfile
import types
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="vpn-deck-tests-")


class _Logger:
    def __init__(self) -> None:
        self.records: List[Tuple[str, str]] = []

    def _log(self, level: str, msg: str) -> None:
        self.records.append((level, msg))

    def debug(self, msg: str) -> None: self._log("debug", msg)
    def info(self, msg: str) -> None: self._log("info", msg)
    def warning(self, msg: str) -> None: self._log("warning", msg)
    def error(self, msg: str) -> None: self._log("error", msg)


def install() -> None:
    if "decky" in sys.modules and getattr(sys.modules["decky"], "_is_fake", False):
        return
    decky = types.ModuleType("decky")
    decky._is_fake = True  # type: ignore[attr-defined]
    decky.logger = _Logger()  # type: ignore[attr-defined]
    for name in ("DECKY_PLUGIN_LOG_DIR", "DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR"):
        path = os.path.join(TMP, name.lower())
        os.makedirs(path, exist_ok=True)
        setattr(decky, name, path)
    decky.DECKY_PLUGIN_DIR = ROOT  # type: ignore[attr-defined]
    sys.modules["decky"] = decky
    for p in (os.path.join(ROOT, "py_modules"), ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)


install()

from vpn_deck.runner import CommandResult  # noqa: E402  (needs install())

Response = Union[CommandResult, Callable[[Sequence[str]], CommandResult]]


class FakeRunner:
    """Returns canned results by argv prefix; records every call."""

    def __init__(self, responses: Optional[Dict[Tuple[str, ...], Response]] = None):
        self.responses: Dict[Tuple[str, ...], Response] = dict(responses or {})
        self.calls: List[List[str]] = []

    def on(self, *prefix: str, stdout: str = "", rc: int = 0, stderr: str = "") -> "FakeRunner":
        self.responses[prefix] = CommandResult(rc, stdout, stderr)
        return self

    def run(self, argv: Sequence[str], timeout: float = 10) -> CommandResult:
        self.calls.append(list(argv))
        best: Optional[Tuple[str, ...]] = None
        for prefix in self.responses:
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            return CommandResult(1, "", f"no fake for {' '.join(argv)}")
        r = self.responses[best]
        return r(argv) if callable(r) else r

    def called(self, *prefix: str) -> List[List[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


class FakeBinaries:
    def __init__(self, paths: Optional[Dict[str, Optional[str]]] = None):
        self.paths = paths or {"awg": "/bin/awg", "awg-quick": "/bin/awg-quick", "sing-box": "/bin/sing-box"}

    def get_binary_path(self, name: str) -> Optional[str]:
        return self.paths.get(name)


def tmpdir(name: str) -> str:
    path = tempfile.mkdtemp(prefix=f"{name}-", dir=TMP)
    return path
