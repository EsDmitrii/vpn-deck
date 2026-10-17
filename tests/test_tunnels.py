import os
import threading
import unittest
from typing import List, Set

from tests.fakes import tmpdir
from vpn_deck.config_manager import ConfigManager
from vpn_deck.models import ConfigKind, OpResult, StoredConfig
from vpn_deck.restore import restore_last_active
from vpn_deck.settings import Settings
from vpn_deck.tunnels import TunnelController


class FakeBackend:
    def __init__(self, log: List[str]) -> None:
        self.log = log
        self.up: Set[str] = set()
        self.fail_start: Set[str] = set()

    def start(self, cfg: StoredConfig) -> OpResult:
        self.log.append(f"start {cfg.name}")
        if cfg.name in self.fail_start:
            return OpResult.fail(f"{cfg.name} failed")
        self.up.add(cfg.interface)
        return OpResult.ok()

    def stop(self, interface: str) -> OpResult:
        self.log.append(f"stop {interface}")
        self.up.discard(interface)
        return OpResult.ok()

    def running_interfaces(self) -> List[str]:
        return sorted(self.up)


def make_configs() -> ConfigManager:
    cm = ConfigManager()
    base = tmpdir("store")
    cm.config_dir = os.path.join(base, "configs")
    cm.system_config_dir = os.path.join(base, "system")
    os.makedirs(cm.config_dir)
    return cm


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.configs = make_configs()
        self.configs.write("home", "[Interface]\n[Peer]\n", ConfigKind.AWG)
        self.configs.write("work", "[Interface]\n[Peer]\n", ConfigKind.AWG)
        self.configs.write("proxy", "vless://u@h:1\n", ConfigKind.VLESS)
        self.log: List[str] = []
        self.awg = FakeBackend(self.log)
        self.singbox = FakeBackend(self.log)
        self.settings = Settings(os.path.join(tmpdir("settings"), "settings.json"))
        self.ctl = TunnelController(self.configs, {
            ConfigKind.AWG: self.awg, ConfigKind.VLESS: self.singbox, ConfigKind.SINGBOX: self.singbox,
        }, self.settings)

    def test_connect_records_intent(self):
        self.assertTrue(self.ctl.connect("home").success)
        self.assertEqual(self.settings.get("last_active"), "home")
        self.assertEqual([c.name for c in self.ctl.running()], ["home"])

    def test_switch_stops_previous_across_backends(self):
        self.ctl.connect("home")
        self.log.clear()
        self.assertTrue(self.ctl.connect("proxy").success)
        self.assertEqual(self.log, ["stop vd-home", "start proxy"])
        self.assertEqual(self.ctl.running_interfaces(), ["vd-proxy"])

    def test_failed_switch_rolls_back(self):
        self.ctl.connect("home")
        self.singbox.fail_start.add("proxy")
        result = self.ctl.connect("proxy")
        self.assertFalse(result.success)
        self.assertEqual(self.ctl.running_interfaces(), ["vd-home"])
        self.assertEqual(self.settings.get("last_active"), "home")  # intent unchanged

    def test_disconnect_clears_intent(self):
        self.ctl.connect("home")
        self.ctl.disconnect("home")
        self.assertIsNone(self.settings.get("last_active"))
        self.assertEqual(self.ctl.running_interfaces(), [])

    def test_unknown_config(self):
        self.assertFalse(self.ctl.connect("nope").success)
        self.assertFalse(self.ctl.disconnect("nope").success)

    def test_ensure_up_respects_user_actions(self):
        self.ctl.connect("home")
        gen = self.ctl.generation
        self.awg.up.clear()  # tunnel died
        self.assertTrue(self.ctl.ensure_up("home", gen).success)
        self.assertIsNone(self.ctl.ensure_up("home", gen))  # already running
        self.awg.up.clear()
        self.ctl.disconnect("work")  # any user action bumps generation
        self.assertIsNone(self.ctl.ensure_up("home", gen))

    def test_reload_restarts_only_running(self):
        self.assertIsNone(self.ctl.reload("home"))
        self.ctl.connect("home")
        self.log.clear()
        self.assertTrue(self.ctl.reload("home").success)
        self.assertEqual(self.log, ["stop vd-home", "start home"])

    def test_release_stops_and_forgets(self):
        self.ctl.connect("home")
        self.ctl.release("home")
        self.assertEqual(self.ctl.running_interfaces(), [])
        self.assertIsNone(self.settings.get("last_active"))


class RestoreTest(ControllerTest):
    def test_restores_when_online(self):
        self.settings.set("last_active", "work")
        result = restore_last_active(self.ctl, self.settings, online=lambda: True, stop=threading.Event())
        self.assertTrue(result is not None and result.success)
        self.assertEqual(self.ctl.running_interfaces(), ["vd-work"])

    def test_disabled_or_nothing_to_restore(self):
        stop = threading.Event()
        self.assertIsNone(restore_last_active(self.ctl, self.settings, lambda: True, stop))
        self.settings.set("last_active", "work")
        self.settings.set("restore_on_boot", False)
        self.assertIsNone(restore_last_active(self.ctl, self.settings, lambda: True, stop))
        self.assertEqual(self.log, [])

    def test_gives_up_at_deadline_when_offline(self):
        self.settings.set("last_active", "work")
        ticks = iter(range(0, 10_000, 100))
        stop = threading.Event()
        stop.wait = lambda timeout=None: False  # type: ignore[method-assign]
        result = restore_last_active(self.ctl, self.settings, lambda: False, stop, clock=lambda: next(ticks))
        self.assertIsNone(result)
        self.assertEqual(self.log, [])


class ConfigStoreTest(unittest.TestCase):
    def test_kinds_and_interfaces(self):
        cm = make_configs()
        cm.write("Home VPN", "[Interface]\n", ConfigKind.AWG)
        cm.write("x", "vless://\n", ConfigKind.VLESS)
        cm.write("y", "{}", ConfigKind.SINGBOX)
        got = {(c.name, c.kind, c.interface) for c in cm.list_managed()}
        self.assertEqual(got, {("home-vpn", ConfigKind.AWG, "vd-home-vpn"), ("x", ConfigKind.VLESS, "vd-x"),
                               ("y", ConfigKind.SINGBOX, "vd-y")})
        self.assertEqual(oct(os.stat(cm.find("x").path).st_mode & 0o777), "0o600")

    def test_symlink_only_for_awg_and_replaced_on_kind_change(self):
        cm = make_configs()
        cm.write("a", "[Interface]\n", ConfigKind.AWG)
        link = os.path.join(cm.system_config_dir, "vd-a.conf")
        self.assertTrue(os.path.islink(link))
        cm.write("a", "vless://\n", ConfigKind.VLESS)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual([c.kind for c in cm.list_managed()], [ConfigKind.VLESS])

    def test_delete(self):
        cm = make_configs()
        cm.write("a", "[Interface]\n", ConfigKind.AWG)
        import asyncio
        r = asyncio.run(cm.delete_config("a"))
        self.assertTrue(r["success"] and r["symlink_removed"])
        self.assertEqual(cm.list_managed(), [])
        self.assertFalse(asyncio.run(cm.delete_config("a"))["success"])

    def test_sanitize(self):
        cm = ConfigManager()
        for raw, expected in (("My VPN", "my-vpn"), ("work_vpn", "work_vpn"), ("../x.conf", "x"),
                              ("a.singbox.json", "a"), ("!!!", "config")):
            with self.subTest(raw=raw):
                self.assertEqual(cm._sanitize_name(raw), expected)


class SettingsTest(unittest.TestCase):
    def test_persist_and_validate(self):
        path = os.path.join(tmpdir("settings"), "settings.json")
        s = Settings(path)
        self.assertTrue(s.get("restore_on_boot"))
        s.set("restore_on_boot", False)
        s.set("last_active", "home")
        self.assertEqual(Settings(path).all()["last_active"], "home")
        self.assertFalse(Settings(path).get("restore_on_boot"))
        with self.assertRaises(TypeError):
            s.set("restore_on_boot", "yes")
        with self.assertRaises(KeyError):
            s.set("nope", 1)

    def test_bad_file_falls_back_to_defaults(self):
        path = os.path.join(tmpdir("settings"), "settings.json")
        with open(path, "w") as f:
            f.write('{"restore_on_boot": "maybe", "vless_dns_server": 5, "last_active": "x"}')
        s = Settings(path)
        self.assertTrue(s.get("restore_on_boot"))
        self.assertEqual(s.get("vless_dns_server"), "1.1.1.1")
        self.assertEqual(s.get("last_active"), "x")


if __name__ == "__main__":
    unittest.main()
