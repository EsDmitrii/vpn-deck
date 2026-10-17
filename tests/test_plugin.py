"""RPC layer: Plugin wired with a temp config store and fake commands."""

import asyncio
import json
import os
import unittest

from tests.fakes import FakeBinaries, FakeRunner, tmpdir
from tests.test_importer import CONF
from tests.test_tunnels import make_configs
from vpn_deck import Diagnostics, ServiceManager, Settings, SingBoxManager
from vpn_deck.singbox_manager import SingBoxPaths

from main import Deps, Plugin


def run(coro):
    return asyncio.run(coro)


class PluginTest(unittest.TestCase):
    def setUp(self) -> None:
        binaries = FakeBinaries({"awg": "/bin/awg", "awg-quick": None, "sing-box": None})
        self.runner = FakeRunner().on("/bin/awg", "show", "interfaces", stdout="")
        base = tmpdir("plugin")
        self.plugin = Plugin(Deps(
            binary_manager=binaries,
            config_manager=make_configs(),
            service_manager=ServiceManager(binaries, self.runner),
            singbox_manager=SingBoxManager(binaries, SingBoxPaths(run_dir=os.path.join(base, "run"), log_dir=base),
                                           self.runner),
            settings=Settings(os.path.join(base, "settings.json")),
            runner=self.runner,
            diagnostics=Diagnostics(),
        ))
        self.dir = base

    def file(self, name: str, content: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_import_flow(self):
        path = self.file("home.conf", CONF)
        inspected = run(self.plugin.inspect_import(path))
        self.assertEqual((inspected["success"], inspected["kind"], inspected["mtu_warning"]), (True, "awg", True))

        r = run(self.plugin.import_vpn_config("home", path, 1280))
        self.assertTrue(r["success"], r)
        self.assertIn("MTU = 1280", run(self.plugin.get_vpn_config("home")))

        link = "vless://11111111-2222-3333-4444-555555555555@h:443?type=ws&security=tls"
        run(self.plugin.import_vpn_config("proxy", self.file("p.txt", link)))
        listed = {c["name"]: c for c in run(self.plugin.list_configs_with_status())}
        self.assertEqual(listed["home"]["kind"], "awg")
        self.assertEqual(listed["proxy"]["label"], "VLESS TLS WS")
        self.assertFalse(listed["proxy"]["active"])

        self.assertTrue(run(self.plugin.delete_vpn_config({"name": "proxy"}))["success"])
        self.assertEqual([c["name"] for c in run(self.plugin.list_configs_with_status())], ["home"])

    def test_import_errors_are_plain_messages(self):
        r = run(self.plugin.import_vpn_config("x", self.file("x.txt", "hello")))
        self.assertFalse(r["success"])
        self.assertIn("[Interface]", r["error"])
        self.assertEqual(run(self.plugin.get_errors()), [])  # expected failures aren't "errors"
        self.assertEqual(run(self.plugin.inspect_import("/nope"))["error"], "Файл не найден")

    def test_start_failure_is_recorded(self):
        run(self.plugin.import_vpn_config("home", self.file("home.conf", CONF)))
        r = run(self.plugin.vpn_start_config({"config_name": "home"}))
        self.assertEqual((r["success"], r["interface"]), (False, "vd-home"))
        self.assertIn("awg-quick", r["error"])
        self.assertEqual(len(run(self.plugin.get_errors())), 1)
        self.assertTrue(run(self.plugin.clear_errors()))

    def test_empty_names(self):
        for call in (self.plugin.vpn_start_config, self.plugin.vpn_stop_config):
            for arg in ("", {"config_name": ""}):
                self.assertFalse(run(call(arg))["success"])

    def test_settings(self):
        self.assertEqual(set(run(self.plugin.get_settings())), {"restore_on_boot", "vless_dns_server", "vless_ipv4_only"})
        self.assertTrue(run(self.plugin.set_setting({"key": "restore_on_boot", "value": False}))["success"])
        self.assertFalse(run(self.plugin.get_settings())["restore_on_boot"])
        self.assertFalse(run(self.plugin.set_setting("last_active", "x"))["success"])  # internal
        self.assertFalse(run(self.plugin.set_setting("restore_on_boot", "x"))["success"])  # wrong type

    def test_no_status_when_nothing_runs(self):
        self.assertIsNone(run(self.plugin.get_connection_status()))

    def test_unexpected_exception_becomes_error_dict(self):
        def boom(name):
            raise RuntimeError("forced")
        self.plugin.controller.connect = boom  # type: ignore[method-assign]
        r = run(self.plugin.vpn_start_config("x"))
        self.assertEqual(r, {"success": False, "error": "RuntimeError: forced"})
        self.assertEqual(run(self.plugin.get_errors())[0]["operation"], "vpn_start_config")

    def test_lifecycle_starts_and_stops_watcher(self):
        run(self.plugin._main())
        self.assertIsNotNone(self.plugin.watcher._thread)
        run(self.plugin._unload())
        self.assertIsNone(self.plugin.watcher._thread)

    def test_results_are_json_serialisable(self):
        run(self.plugin.import_vpn_config("home", self.file("home.conf", CONF)))
        for result in (run(self.plugin.list_configs_with_status()), run(self.plugin.get_settings()),
                       run(self.plugin.scan_existing_configs())):
            json.dumps(result)


if __name__ == "__main__":
    unittest.main()
