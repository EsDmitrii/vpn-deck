"""End-to-end scenarios; run by tests/e2e/run.sh inside a privileged container.

The plugin runs for real (real binaries, routes, TUN); calls go through the
RPC methods exactly as the frontend makes them. "Where did traffic exit" is
checked with the echo server in the srv netns, which answers with the
client address it sees:
  10.99.0.1 / 10.98.0.1  direct (no VPN)
  10.66.66.2             through AWG
  198.51.100.1           through the VLESS server (its direct outbound
                         reaches the echo address inside the same netns)
"""

import asyncio
import base64
import os
import subprocess
import time
import unittest
from typing import Callable, List, Optional

import tests.fakes  # noqa: F401  (fake decky)
from main import Plugin

CFG = os.environ.get("E2E_CONFIGS", "/work/e2e-configs")
ECHO = "http://198.51.100.1:8080/"
DIRECT = {"10.99.0.1", "10.98.0.1"}  # home / hotspot address
VIA_AWG = "10.66.66.2"
VIA_VLESS = "198.51.100.1"


def run(coro):
    return asyncio.run(coro)


def sh(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


def exit_ip(timeout: float = 5) -> Optional[str]:
    r = subprocess.run(["curl", "-sS", "--max-time", str(timeout), ECHO], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def wait_for(fn: Callable[[], bool], timeout: float = 30, step: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(step)
    return False


def ping(target: str = "10.66.66.1") -> bool:
    return subprocess.run(["ping", "-c1", "-W2", target], capture_output=True).returncode == 0


def wait_exit(expected: str, timeout: float = 30) -> Optional[str]:
    last: List[Optional[str]] = [None]

    def check() -> bool:
        last[0] = exit_ip(3)
        return last[0] == expected
    wait_for(check, timeout)
    return last[0]


HOME = ("10.99.0.1/24", "10.99.0.2")
HOTSPOT = ("10.98.0.1/24", "10.98.0.2")


def switch_network(to_hotspot: bool) -> None:
    """Join another Wi-Fi: same wlan0, new address and gateway."""
    (old_addr, _), (new_addr, new_gw) = (HOME, HOTSPOT) if to_hotspot else (HOTSPOT, HOME)
    sh("ip", "addr", "del", old_addr, "dev", "wlan0")
    sh("ip", "addr", "replace", new_addr, "dev", "wlan0")
    sh("ip", "route", "replace", "default", "via", new_gw, "dev", "wlan0", "metric", "100")


def switch_router(to_new: bool) -> None:
    """Another Wi-Fi with the *same* subnet but a different router
    (both 10.99.0.0/24): the kernel keeps routes via the old gateway."""
    if to_new:
        sh("ip", "-n", "srv", "addr", "add", "10.99.0.254/24", "dev", "s0")
        sh("ip", "-n", "srv", "addr", "del", "10.99.0.2/24", "dev", "s0")
        sh("ip", "route", "replace", "default", "via", "10.99.0.254", "dev", "wlan0", "metric", "100")
    else:
        sh("ip", "-n", "srv", "addr", "add", "10.99.0.2/24", "dev", "s0")
        sh("ip", "-n", "srv", "addr", "del", "10.99.0.254/24", "dev", "s0")
        sh("ip", "route", "replace", "default", "via", "10.99.0.2", "dev", "wlan0", "metric", "100")
    sh("ip", "-n", "srv", "route", "replace", "10.99.0.0/24", "dev", "s0", "src", "203.0.113.1")
    sh("ip", "neigh", "flush", "dev", "wlan0")


class E2E(unittest.TestCase):
    plugin: Plugin

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin = Plugin()
        for name, file in (("full", "full.conf"), ("split", "split.conf"),
                           ("vtcp", "vless-tcp.txt"), ("vws", "vless-ws.txt")):
            r = run(cls.plugin.import_vpn_config(name, os.path.join(CFG, file)))
            assert r["success"], r

    def setUp(self) -> None:
        switch_network(to_hotspot=False)
        self.assertIn(exit_ip(), DIRECT, "baseline: traffic must go direct before each test")

    def tearDown(self) -> None:
        for c in run(self.plugin.list_configs_with_status()):
            if c["active"]:
                run(self.plugin.vpn_stop_config(c["name"]))
        if "10.99.0.254" in sh("ip", "-n", "srv", "addr", "show", "dev", "s0"):
            switch_router(to_new=False)
        switch_network(to_hotspot=False)

    def start(self, name: str) -> None:
        r = run(self.plugin.vpn_start_config({"config_name": name}))
        self.assertTrue(r["success"], r.get("error"))

    def active(self):
        return [c["name"] for c in run(self.plugin.list_configs_with_status()) if c["active"]]

    # ── AWG ──────────────────────────────────────────────────────────
    def test_awg_full_tunnel(self):
        self.start("full")
        self.assertEqual(self.active(), ["full"])
        self.assertEqual(wait_exit(VIA_AWG), VIA_AWG)
        # Full tunnel: no endpoint pin (it would break after a network change).
        self.assertFalse(os.path.exists("/var/run/awg-quick/vd-full.endpoint-routes"))

        status = run(self.plugin.get_connection_status())
        self.assertEqual(status["name"], "full")
        self.assertIn("10.66.66.2/32", status["addresses"])
        self.assertIsNotNone(status["latest_handshake"])

        run(self.plugin.vpn_stop_config("full"))
        self.assertIn(exit_ip(), DIRECT)

    def test_awg_full_tunnel_survives_network_change(self):
        self.start("full")
        self.assertEqual(wait_exit(VIA_AWG), VIA_AWG)
        switch_network(to_hotspot=True)
        # WireGuard roams by itself as long as nothing is pinned to the old gateway.
        self.assertEqual(wait_exit(VIA_AWG, timeout=40), VIA_AWG)

    def test_awg_full_tunnel_survives_router_change(self):
        """Regression for the endpoint pin: with a pin via the old router
        the full tunnel's own packets go nowhere."""
        self.start("full")
        self.assertEqual(wait_exit(VIA_AWG), VIA_AWG)
        switch_router(to_new=True)
        self.assertEqual(wait_exit(VIA_AWG, timeout=40), VIA_AWG)

    def test_awg_split_tunnel_repinned_after_router_change(self):
        self.start("split")
        self.assertTrue(wait_for(ping, 20))
        switch_router(to_new=True)
        self.assertIn("via 10.99.0.2", sh("ip", "route", "show", "203.0.113.1/32"))  # stale pin kept
        self.assertFalse(ping())
        self.plugin.watcher.tick()
        self.assertIn("203.0.113.1 via 10.99.0.254 dev wlan0", sh("ip", "route", "show", "203.0.113.1/32"))
        self.assertTrue(wait_for(ping, 40), "tunnel did not recover after re-pin")

    def test_awg_split_tunnel_repinned_after_network_change(self):
        self.start("split")
        pins = "/var/run/awg-quick/vd-split.endpoint-routes"
        self.assertTrue(os.path.exists(pins))
        self.assertIn("203.0.113.1 via 10.99.0.2 dev wlan0", sh("ip", "route", "show", "203.0.113.1/32"))
        self.assertTrue(wait_for(ping, 20))

        switch_network(to_hotspot=True)
        # The pin still points at the home gateway (or is gone and the endpoint
        # falls into 203.0.113.0/24 -> the tunnel itself). Either way: dead.
        self.assertNotIn("via 10.98.0.2", sh("ip", "route", "show", "203.0.113.1/32"))
        self.assertFalse(ping(), "expected the tunnel to be broken before the watcher runs")
        self.plugin.watcher.tick()
        self.assertIn("203.0.113.1 via 10.98.0.2 dev wlan0", sh("ip", "route", "show", "203.0.113.1/32"))
        self.assertTrue(wait_for(ping, 40), "tunnel did not recover after re-pin")

    # ── VLESS ────────────────────────────────────────────────────────
    def test_vless_tcp_by_hostname(self):
        self.start("vtcp")  # server given as vless.test -> resolved before the TUN is up
        self.assertTrue(os.path.exists("/sys/class/net/vd-vtcp"))
        self.assertEqual(wait_exit(VIA_VLESS), VIA_VLESS)
        run(self.plugin.vpn_stop_config("vtcp"))
        self.assertTrue(wait_for(lambda: not os.path.exists("/sys/class/net/vd-vtcp"), 15))
        self.assertIn(exit_ip(), DIRECT)

    def test_vless_ws_with_early_data(self):
        self.start("vws")
        self.assertEqual(wait_exit(VIA_VLESS), VIA_VLESS)

    def test_subscription_pick_server_and_follow_rotation(self):
        path = os.path.join(CFG, "subscription.txt")
        inspected = run(self.plugin.inspect_import(path))
        self.assertEqual(inspected["servers"], ["Amsterdam TCP", "Frankfurt WS"])
        self.assertTrue(run(self.plugin.import_vpn_config("sub", path, None, "Frankfurt WS"))["success"])
        label = {c["name"]: c["label"] for c in run(self.plugin.list_configs_with_status())}["sub"]
        self.assertEqual(label, "Подписка · Frankfurt WS")

        self.start("sub")
        self.assertEqual(wait_exit(VIA_VLESS), VIA_VLESS)
        run(self.plugin.vpn_stop_config("sub"))

        # The panel rotates the server: same name, now the TCP inbound.
        sub_file = os.path.join(CFG, "sub", "s")
        with open(sub_file) as f:
            original = f.read()
        try:
            links = base64.b64decode(original).decode().splitlines()
            rotated = [links[0], links[0].split("#")[0] + "#Frankfurt WS"]
            with open(sub_file, "w") as f:
                f.write(base64.b64encode("\n".join(rotated).encode()).decode())
            self.start("sub")
            self.assertEqual(wait_exit(VIA_VLESS), VIA_VLESS)
            stored = run(self.plugin.get_vpn_config("sub"))
            self.assertIn(":8443?", stored)  # picked up the rotated link
        finally:
            with open(sub_file, "w") as f:
                f.write(original)

    # ── switching / lifecycle ───────────────────────────────────────
    def test_switching_between_awg_and_vless(self):
        self.start("full")
        self.assertEqual(wait_exit(VIA_AWG), VIA_AWG)
        self.start("vtcp")
        self.assertEqual(self.active(), ["vtcp"])
        self.assertEqual(wait_exit(VIA_VLESS), VIA_VLESS)
        self.start("split")
        self.assertEqual(self.active(), ["split"])
        self.assertIn(exit_ip(), DIRECT)  # split tunnel: echo server isn't in AllowedIPs

    def test_singbox_survives_plugin_reload(self):
        self.start("vtcp")
        reloaded = Plugin()
        self.assertEqual([c["name"] for c in run(reloaded.list_configs_with_status()) if c["active"]], ["vtcp"])
        self.assertTrue(run(reloaded.vpn_stop_config("vtcp"))["success"])
        self.assertIn(exit_ip(), DIRECT)

    def test_delete_running_config(self):
        r = run(self.plugin.import_vpn_config("tmp", os.path.join(CFG, "full.conf")))
        self.assertTrue(r["success"])
        self.start("tmp")
        self.assertTrue(run(self.plugin.delete_vpn_config({"name": "tmp"}))["success"])
        self.assertNotIn("vd-tmp", sh("ip", "link"))
        self.assertIn(exit_ip(), DIRECT)


if __name__ == "__main__":
    unittest.main()
