import base64
import os
import unittest
from typing import List

from tests.fakes import FakeRunner, tmpdir
from tests.test_tunnels import make_configs
from vpn_deck.errors import ConfigImportError
from vpn_deck.importer import plan_import
from vpn_deck.models import ConfigKind, OpResult
from vpn_deck.settings import Settings
from vpn_deck.subscription import SubscriptionConfig, curl_fetcher, parse_subscription, refresh
from vpn_deck.tunnels import SingBoxBackend

UUID = "11111111-2222-3333-4444-555555555555"
NL = f"vless://{UUID}@nl.example:443?type=tcp&security=reality&pbk=k&flow=xtls-rprx-vision#%F0%9F%87%B3%F0%9F%87%B1%20Amsterdam"
DE = f"vless://{UUID}@de.example:443?type=tcp&security=reality&pbk=k#Frankfurt"
URL = "https://sub.example/s/token"


def b64(text: str, urlsafe: bool = False) -> str:
    enc = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return enc(text.encode()).decode()


class ParseTest(unittest.TestCase):
    def test_base64_and_plain(self):
        body = f"{NL}\nvmess://eyJhZGQiOiJ4In0=\n{DE}\n"
        for variant in (body, b64(body), b64(body, urlsafe=True), b64(body)[:40] + "\n" + b64(body)[40:]):
            with self.subTest(variant=variant[:20]):
                servers = parse_subscription(variant)
                self.assertEqual([s.name for s in servers], ["🇳🇱 Amsterdam", "Frankfurt"])  # vmess skipped
                self.assertEqual(servers[1].link, DE)

    def test_unnamed_servers_get_numbers(self):
        self.assertEqual(parse_subscription(f"vless://{UUID}@h:1?type=tcp")[0].name, "Сервер 1")

    def test_errors(self):
        for body in ("<html>login</html>", b64("vmess://x\ntrojan://y")):
            with self.subTest(body=body[:20]):
                with self.assertRaises(ConfigImportError):
                    parse_subscription(body)


class ImportTest(unittest.TestCase):
    def fetch(self, url: str) -> str:
        self.assertEqual(url, URL)
        return b64(f"{NL}\n{DE}")

    def test_plan_defaults_to_first_server(self):
        plan = plan_import(URL + "\n", fetch=self.fetch)
        self.assertIs(plan.kind, ConfigKind.SUBSCRIPTION)
        self.assertEqual(plan.servers, ("🇳🇱 Amsterdam", "Frankfurt"))
        self.assertEqual(SubscriptionConfig.loads(plan.content), SubscriptionConfig(URL, "🇳🇱 Amsterdam", NL))

    def test_plan_with_picked_server(self):
        plan = plan_import(URL, server="Frankfurt", fetch=self.fetch)
        self.assertEqual(SubscriptionConfig.loads(plan.content).link, DE)
        with self.assertRaises(ConfigImportError):
            plan_import(URL, server="Tokyo", fetch=self.fetch)

    def test_curl_fetcher(self):
        runner = FakeRunner().on("curl", stdout="BODY")
        self.assertEqual(curl_fetcher(runner)(URL), "BODY")
        argv = runner.calls[0]
        self.assertIn("v2rayN/7.0", argv)
        self.assertEqual(argv[-1], URL)
        with self.assertRaises(ConfigImportError):
            curl_fetcher(runner)("file:///etc/shadow")
        with self.assertRaises(ConfigImportError):
            curl_fetcher(FakeRunner().on("curl", rc=22, stderr="404"))(URL)


class RefreshTest(unittest.TestCase):
    def test_refresh_picks_current_link(self):
        rotated = DE.replace(UUID, "99999999-2222-3333-4444-555555555555")
        cfg = SubscriptionConfig(URL, "Frankfurt", DE)
        self.assertEqual(refresh(cfg, lambda _: b64(rotated)).link, rotated)
        with self.assertRaises(ConfigImportError):
            refresh(cfg, lambda _: b64(NL))  # server gone


class FakeManager:
    def __init__(self) -> None:
        self.started: List[dict] = []

    def start(self, iface, config):
        self.started.append(config)
        return OpResult.ok()


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.configs = make_configs()
        self.cfg = self.configs.write("blanc", SubscriptionConfig(URL, "Frankfurt", DE).dumps(), ConfigKind.SUBSCRIPTION)
        self.manager = FakeManager()
        self.settings = Settings(os.path.join(tmpdir("s"), "settings.json"))

    def backend(self, fetch):
        return SingBoxBackend(self.manager, self.configs, self.settings, fetch,  # type: ignore[arg-type]
                              resolver=lambda host: "203.0.113.9")

    def test_start_uses_fresh_link_and_stores_it(self):
        rotated = DE.replace("de.example", "de2.example")
        self.assertTrue(self.backend(lambda _: b64(rotated)).start(self.cfg).success)
        out = self.manager.started[0]["outbounds"][0]
        self.assertEqual((out["server"], out["tls"]["server_name"]), ("203.0.113.9", "de2.example"))
        self.assertEqual(SubscriptionConfig.loads(self.configs.read(self.cfg)).link, rotated)

    def test_subscription_host_bypasses_the_proxy(self):
        self.backend(lambda _: b64(DE)).start(self.cfg)
        rules = self.manager.started[0]["route"]["rules"]
        self.assertIn({"domain": ["sub.example"], "outbound": "direct"}, rules)

    def test_offline_falls_back_to_stored_link(self):
        def offline(_):
            raise ConfigImportError("no network")
        self.assertTrue(self.backend(offline).start(self.cfg).success)
        self.assertEqual(self.manager.started[0]["outbounds"][0]["tls"]["server_name"], "de.example")


if __name__ == "__main__":
    unittest.main()
