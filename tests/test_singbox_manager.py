"""SingBoxManager against a fake `sing-box` script. Linux only (/proc)."""

import os
import stat
import sys
import unittest

from tests.fakes import FakeBinaries, tmpdir
from vpn_deck.singbox_manager import SingBoxManager, SingBoxPaths

FAKE_SINGBOX = """#!/bin/sh
# check: fail when FAKE_FAIL=check. run: create the "interface" and wait.
case "$1" in
  check) [ "$FAKE_FAIL" = check ] && { echo "FATAL[0000] decode config: bad" >&2; exit 1; }; exit 0 ;;
  run)
    [ "$FAKE_FAIL" = run ] && { echo "FATAL[0000] start service: tun: permission denied"; exit 1; }
    [ "$FAKE_FAIL" = noiface ] || mkdir -p "$FAKE_SYSNET/$FAKE_IFACE"
    [ "$FAKE_FAIL" = ignore_term ] && trap '' TERM || trap 'rm -rf "$FAKE_SYSNET/$FAKE_IFACE"; exit 0' TERM
    while :; do sleep 0.05; done ;;
esac
"""


@unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
class SingBoxManagerTest(unittest.TestCase):
    def setUp(self):
        base = tmpdir("sb")
        self.bin = os.path.join(base, "sing-box")
        with open(self.bin, "w") as f:
            f.write(FAKE_SINGBOX)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.paths = SingBoxPaths(run_dir=os.path.join(base, "run"), log_dir=base,
                                  sys_net_dir=os.path.join(base, "sysnet"))
        os.environ.update(FAKE_SYSNET=self.paths.sys_net_dir, FAKE_IFACE="vd-x", FAKE_FAIL="")
        self.mgr = SingBoxManager(FakeBinaries({"sing-box": self.bin}), self.paths)

    def tearDown(self):
        os.environ["FAKE_FAIL"] = ""
        self.mgr.stop("vd-x")

    def test_start_stop(self):
        self.assertTrue(self.mgr.start("vd-x", {"secret": "uuid"}).success)
        self.assertTrue(self.mgr.is_running("vd-x"))
        self.assertEqual(self.mgr.running_interfaces(), ["vd-x"])
        self.assertEqual(oct(os.stat(self.paths.config("vd-x")).st_mode & 0o777), "0o600")
        self.assertTrue(self.mgr.start("vd-x", {}).success)  # idempotent
        self.assertTrue(self.mgr.stop("vd-x").success)
        self.assertFalse(self.mgr.is_running("vd-x"))
        self.assertFalse(os.path.exists(self.paths.config("vd-x")))  # credential removed

    def test_survives_manager_restart(self):
        self.mgr.start("vd-x", {})
        fresh = SingBoxManager(FakeBinaries({"sing-box": self.bin}), self.paths)  # plugin reload
        self.assertTrue(fresh.is_running("vd-x"))
        self.assertTrue(fresh.stop("vd-x").success)
        self.assertFalse(self.mgr.is_running("vd-x"))

    def test_check_failure(self):
        os.environ["FAKE_FAIL"] = "check"
        r = self.mgr.start("vd-x", {})
        self.assertFalse(r.success)
        self.assertIn("decode config", r.error)

    def test_fatal_line_is_reported(self):
        os.environ["FAKE_FAIL"] = "run"
        r = self.mgr.start("vd-x", {})
        self.assertFalse(r.success)
        self.assertIn("permission denied", r.error)
        self.assertEqual(self.mgr.running_interfaces(), [])

    def test_stale_pid_file_is_ignored(self):
        os.makedirs(self.paths.run_dir, exist_ok=True)
        with open(self.paths.pid("vd-x"), "w") as f:
            f.write(str(os.getpid()))  # a live process that isn't sing-box
        self.assertFalse(self.mgr.is_running("vd-x"))
        self.assertTrue(self.mgr.stop("vd-x").success)  # and it is not signalled


if __name__ == "__main__":
    unittest.main()
