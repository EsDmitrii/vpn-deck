import os
import unittest
from typing import Dict, List, Optional

from tests.fakes import FakeRunner, tmpdir
from vpn_deck import defaults
from vpn_deck.models import ConfigKind, OpResult, StoredConfig
from vpn_deck.network_watch import (
    NetworkWatcher,
    PeerHealth,
    best_underlay,
    is_online,
    parse_routes,
    plan_repin,
    read_pins,
)

HOME_V4 = """default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.50 metric 600
1.2.3.4 via 192.168.1.1 dev wlan0
10.0.0.0/8 dev vd-work scope link
192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.50 metric 600
"""
HOTSPOT_V4 = """default via 172.20.10.1 dev wlan0 proto dhcp src 172.20.10.2 metric 600
1.2.3.4 via 192.168.1.1 dev wlan0
10.0.0.0/8 dev vd-work scope link
172.20.10.0/28 dev wlan0 proto kernel scope link src 172.20.10.2 metric 600
"""
OFFLINE_V4 = """1.2.3.4 via 192.168.1.1 dev wlan0 linkdown
10.0.0.0/8 dev vd-work scope link
"""
MULTIPATH_V4 = """default proto static metric 100
\tnexthop via 192.168.1.1 dev eth0 weight 1
\tnexthop via 192.168.2.1 dev wlan0 weight 1
"""


class RoutesTest(unittest.TestCase):
    def test_parse(self):
        routes = parse_routes(HOME_V4, 4)
        self.assertEqual(len(routes), 4)
        d = routes[0]
        self.assertEqual((str(d.dst), d.gateway, d.dev, d.metric), ("0.0.0.0/0", "192.168.1.1", "wlan0", 600))
        self.assertIsNone(routes[3].gateway)

    def test_multipath_is_skipped(self):
        self.assertEqual(parse_routes(MULTIPATH_V4, 4), [])

    def test_ipv6(self):
        r = parse_routes("default via fe80::1 dev wlan0 proto ra metric 600 pref medium\n", 6)[0]
        self.assertEqual((str(r.dst), r.gateway), ("::/0", "fe80::1"))

    def test_best_underlay_ignores_tunnels_and_pin(self):
        routes = parse_routes(HOME_V4, 4)
        pin = parse_routes("1.2.3.4 via 192.168.1.1 dev wlan0", 4)[0].dst
        self.assertEqual(str(best_underlay(routes, "1.2.3.4", ignore=pin).dst), "0.0.0.0/0")
        self.assertEqual(str(best_underlay(routes, "192.168.1.7").dst), "192.168.1.0/24")
        self.assertEqual(str(best_underlay(routes, "10.1.1.1").dst), "0.0.0.0/0")  # vd-work skipped

    def test_online(self):
        self.assertTrue(is_online(parse_routes(HOME_V4, 4)))
        self.assertFalse(is_online(parse_routes(OFFLINE_V4, 4)))


class RepinTest(unittest.TestCase):
    def test_same_network_leaves_pin(self):
        self.assertIsNone(plan_repin("1.2.3.4/32", parse_routes(HOME_V4, 4)))

    def test_new_gateway_repins(self):
        action = plan_repin("1.2.3.4/32", parse_routes(HOTSPOT_V4, 4))
        self.assertEqual((action.via.gateway, action.via.dev), ("172.20.10.1", "wlan0"))

    def test_missing_pin_is_restored(self):
        routes = [r for r in parse_routes(HOME_V4, 4) if str(r.dst) != "1.2.3.4/32"]
        self.assertEqual(plan_repin("1.2.3.4/32", routes).via.gateway, "192.168.1.1")

    def test_offline_does_nothing(self):
        self.assertIsNone(plan_repin("1.2.3.4/32", parse_routes(OFFLINE_V4, 4)))

    def test_endpoint_now_on_link_drops_pin(self):
        routes = parse_routes(HOTSPOT_V4.replace("1.2.3.4 via", "172.20.10.5 via"), 4)
        action = plan_repin("172.20.10.5/32", routes)
        self.assertIsNotNone(action)
        self.assertIsNone(action.via)

    def test_read_pins(self):
        d = tmpdir("pins")
        with open(os.path.join(d, "vd-a.endpoint-routes"), "w") as f:
            f.write("-4 1.2.3.4/32\n-6 2001:db8::1/128\ngarbage\n")
        self.assertEqual(read_pins(d, "vd-a"), ["1.2.3.4/32", "2001:db8::1/128"])
        self.assertEqual(read_pins(d, "vd-none"), [])


class PeerHealthTest(unittest.TestCase):
    KA = {"peer": 25}

    def test_fresh_handshake_is_healthy(self):
        self.assertFalse(PeerHealth(first_seen=0).should_restart(1000, {"peer": 990}, self.KA, True))

    def test_restart_after_grace_then_rate_limited(self):
        h = PeerHealth(first_seen=0)
        stale = {"peer": 1}
        t = 2.0 + defaults.STALE_HANDSHAKE_SEC
        self.assertFalse(h.should_restart(t, stale, self.KA, True))  # stale first seen
        self.assertFalse(h.should_restart(t + defaults.RESTART_GRACE_SEC - 1, stale, self.KA, True))
        t += defaults.RESTART_GRACE_SEC
        self.assertTrue(h.should_restart(t, stale, self.KA, True))
        self.assertFalse(h.should_restart(t + 1, stale, self.KA, True))
        self.assertFalse(h.should_restart(t + defaults.RESTART_GRACE_SEC + 2, stale, self.KA, True))
        later = t + defaults.MIN_RESTART_INTERVAL_SEC + defaults.RESTART_GRACE_SEC + 2
        self.assertTrue(h.should_restart(later, stale, self.KA, True))

    def test_never_without_keepalive_or_offline(self):
        h = PeerHealth(first_seen=0)
        for _ in range(5):
            self.assertFalse(h.should_restart(10_000, {"peer": 1}, {"peer": None}, True))
            self.assertFalse(h.should_restart(10_000, {"peer": 1}, self.KA, False))

    def test_never_handshaked_counts_from_first_seen(self):
        h = PeerHealth(first_seen=1000)
        self.assertFalse(h.should_restart(1000 + defaults.STALE_HANDSHAKE_SEC - 1, {"peer": 0}, self.KA, True))
        self.assertIsNone(h.stale_since)


# ── executor with fakes ─────────────────────────────────────────────

class FakeService:
    def __init__(self) -> None:
        self.ifaces = ["vd-a"]
        self.handshakes: Dict[str, int] = {"peer": 0}
        self.keepalives: Dict[str, Optional[int]] = {"peer": 25}

    def interfaces(self) -> List[str]:
        return list(self.ifaces)

    def latest_handshakes(self, iface: str) -> Dict[str, int]:
        return dict(self.handshakes)

    def persistent_keepalives(self, iface: str) -> Dict[str, Optional[int]]:
        return dict(self.keepalives)


class FakeController:
    def __init__(self, cfg: StoredConfig) -> None:
        self.cfg = cfg
        self.configs = self
        self.generation = 0
        self.restarts: List[str] = []
        self.ensure_calls: List[str] = []
        self.restart_result = OpResult.ok()
        self.ensure_result: Optional[OpResult] = OpResult.ok()

    def find_by_interface(self, iface: str) -> Optional[StoredConfig]:
        return self.cfg if iface == self.cfg.interface else None

    def restart(self, cfg: StoredConfig) -> OpResult:
        self.restarts.append(cfg.name)
        return self.restart_result

    def ensure_up(self, name: str, generation: int) -> Optional[OpResult]:
        self.ensure_calls.append(name)
        return self.ensure_result


class WatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        state_dir = tmpdir("state")
        with open(os.path.join(state_dir, "vd-a.endpoint-routes"), "w") as f:
            f.write("-4 1.2.3.4/32\n")
        self.runner = FakeRunner().on("ip", "-o", "-4", "route", stdout=HOTSPOT_V4).on("ip", "-4", "route", "replace")
        self.service = FakeService()
        self.controller = FakeController(StoredConfig("a", ConfigKind.AWG, "vd-a", "/x/a.conf"))
        self.errors: List[str] = []
        self.now = 1000.0
        self.watcher = NetworkWatcher(self.controller, self.service, self.runner,  # type: ignore[arg-type]
                                      report_error=lambda op, msg: self.errors.append(msg),
                                      state_dir=state_dir, clock=lambda: self.now)

    def test_repins_after_network_change(self):
        self.watcher.tick()
        self.assertEqual(self.runner.called("ip", "-4", "route", "replace"),
                         [["ip", "-4", "route", "replace", "1.2.3.4/32", "via", "172.20.10.1", "dev", "wlan0"]])

    def test_failed_restart_is_retried_until_user_steps_in(self):
        self.controller.restart_result = OpResult.fail("boom")
        self.controller.ensure_result = OpResult.fail("still down")
        for _ in range(3):  # stale -> grace -> restart
            self.now += defaults.STALE_HANDSHAKE_SEC
            self.watcher.tick()
        self.assertEqual(self.controller.restarts, ["a"])
        self.assertEqual(self.errors, ["restart vd-a: boom"])

        self.service.ifaces = []
        self.now += defaults.RETRY_UP_INTERVAL_SEC
        self.watcher.tick()
        self.watcher.tick()  # within the retry interval
        self.assertEqual(self.controller.ensure_calls, ["a"])

        self.controller.ensure_result = None  # user acted: controller refuses
        self.now += defaults.RETRY_UP_INTERVAL_SEC
        self.watcher.tick()
        self.now += defaults.RETRY_UP_INTERVAL_SEC
        self.watcher.tick()
        self.assertEqual(self.controller.ensure_calls, ["a", "a"])  # pending dropped

    def test_errors_reported_once(self):
        self.runner.on("ip", "-4", "route", "replace", rc=2, stderr="RTNETLINK: Network unreachable")
        self.watcher.tick()
        self.watcher.tick()
        self.assertEqual(len(self.errors), 1)


if __name__ == "__main__":
    unittest.main()
