import unittest

import tests.fakes  # noqa: F401
from vpn_deck.errors import ConfigImportError
from vpn_deck.singbox import (
    VlessOptions,
    build_vless_config,
    prepare_user_singbox,
    validate_user_singbox,
    vless_label,
    vless_outbound,
)

UUID = "11111111-2222-3333-4444-555555555555"


def link(query: str, host: str = "vpn.example.com", port: int = 443) -> str:
    return f"vless://{UUID}@{host}:{port}?{query}#name"


class VlessOutboundTest(unittest.TestCase):
    def test_reality_vision(self):
        out = vless_outbound(link("type=tcp&security=reality&pbk=PUB&sid=ab&sni=www.microsoft.com"
                                  "&flow=xtls-rprx-vision"))
        self.assertEqual(out["server"], "vpn.example.com")
        self.assertEqual(out["server_port"], 443)
        self.assertEqual(out["uuid"], UUID)
        self.assertEqual(out["flow"], "xtls-rprx-vision")
        self.assertNotIn("transport", out)
        self.assertEqual(out["tls"], {
            "enabled": True,
            "server_name": "www.microsoft.com",
            "reality": {"enabled": True, "public_key": "PUB", "short_id": "ab"},
            "utls": {"enabled": True, "fingerprint": "chrome"},  # REALITY default
        })

    def test_ws_tls_with_early_data(self):
        out = vless_outbound(link("type=ws&security=tls&path=%2Fws%3Fed%3D2048&host=cdn.example&fp=firefox"
                                  "&alpn=h2,http%2F1.1"))
        self.assertEqual(out["transport"], {
            "type": "ws", "path": "/ws", "max_early_data": 2048,
            "early_data_header_name": "Sec-WebSocket-Protocol", "headers": {"Host": "cdn.example"},
        })
        self.assertEqual(out["tls"]["server_name"], "vpn.example.com")  # no sni -> server
        self.assertEqual(out["tls"]["alpn"], ["h2", "http/1.1"])
        self.assertEqual(out["tls"]["utls"]["fingerprint"], "firefox")

    def test_other_transports(self):
        cases = {
            "type=grpc&serviceName=svc": {"type": "grpc", "service_name": "svc"},
            "type=httpupgrade&path=/up&host=h": {"type": "httpupgrade", "path": "/up", "host": "h"},
            "type=h2&path=/p&host=a,b": {"type": "http", "path": "/p", "host": ["a", "b"]},
            "type=quic": {"type": "quic"},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(vless_outbound(link(query))["transport"], expected)

    def test_plain_has_no_tls(self):
        self.assertNotIn("tls", vless_outbound(link("type=tcp&security=none")))

    def test_server_ip_replaces_hostname_only(self):
        self.assertEqual(vless_outbound(link("type=tcp"), "1.2.3.4")["server"], "1.2.3.4")
        self.assertEqual(vless_outbound(link("type=tcp", host="5.6.7.8"), "1.2.3.4")["server"], "5.6.7.8")

    def test_ipv6_server(self):
        self.assertEqual(vless_outbound(f"vless://{UUID}@[2001:db8::1]:443?type=tcp")["server"], "2001:db8::1")

    def test_refused(self):
        for query in ("type=xhttp", "type=splithttp", "type=kcp", "encryption=mlkem768",
                      "type=tcp&headerType=http"):
            with self.subTest(query=query):
                with self.assertRaises(ConfigImportError):
                    vless_outbound(link(query))
        for bad in ("vmess://x", f"vless://{UUID}@host?type=tcp", "vless://@host:443", "vless://u@host:99999"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigImportError):
                    vless_outbound(bad)

    def test_label(self):
        self.assertEqual(vless_label(link("security=reality&pbk=k")), "VLESS REALITY")
        self.assertEqual(vless_label(link("type=ws&security=tls")), "VLESS TLS WS")
        self.assertEqual(vless_label(link("type=tcp")), "VLESS")


class BuildConfigTest(unittest.TestCase):
    def test_full_tunnel_config(self):
        cfg = build_vless_config(link("security=reality&pbk=k"), "vd-test", "1.2.3.4")
        tun = cfg["inbounds"][0]
        self.assertEqual(tun["type"], "tun")
        self.assertEqual(tun["interface_name"], "vd-test")
        self.assertTrue(tun["auto_route"] and tun["strict_route"])
        self.assertEqual(cfg["outbounds"][0]["server"], "1.2.3.4")
        self.assertEqual(cfg["outbounds"][0]["tls"]["server_name"], "vpn.example.com")
        self.assertEqual(cfg["route"]["final"], "proxy")
        self.assertTrue(cfg["route"]["auto_detect_interface"])
        actions = [r.get("action") for r in cfg["route"]["rules"]]
        self.assertEqual(actions[:2], ["sniff", "hijack-dns"])  # DNS hijack before private->direct
        self.assertEqual(cfg["dns"]["strategy"], "ipv4_only")

    def test_options(self):
        cfg = build_vless_config(link("type=tcp"), "vd-x", options=VlessOptions(dns_server="9.9.9.9", ipv4_only=False))
        self.assertEqual(cfg["dns"]["servers"][0]["server"], "9.9.9.9")
        self.assertNotIn("strategy", cfg["dns"])


class UserJsonTest(unittest.TestCase):
    BASE = {"inbounds": [{"type": "tun", "interface_name": "tun0"}], "outbounds": [{"type": "direct"}]}

    def test_valid(self):
        self.assertIs(validate_user_singbox(self.BASE), self.BASE)

    def test_tun_count(self):
        for inbounds in ([], [{"type": "tun"}, {"type": "tun"}]):
            with self.subTest(n=len(inbounds)):
                with self.assertRaises(ConfigImportError):
                    validate_user_singbox({**self.BASE, "inbounds": inbounds})

    def test_forbidden_sections(self):
        for key in ("experimental", "services"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigImportError):
                    validate_user_singbox({**self.BASE, key: {}})

    def test_listeners_must_be_loopback(self):
        ok = {**self.BASE, "inbounds": self.BASE["inbounds"] + [{"type": "mixed", "listen": "127.0.0.1"}]}
        validate_user_singbox(ok)
        for listen in ("0.0.0.0", "", "::"):
            with self.subTest(listen=listen):
                bad = {**self.BASE, "inbounds": self.BASE["inbounds"] + [{"type": "mixed", "listen": listen}]}
                with self.assertRaises(ConfigImportError):
                    validate_user_singbox(bad)

    def test_prepare(self):
        cfg = prepare_user_singbox({**self.BASE, "log": {"level": "debug", "output": "/etc/passwd"}}, "vd-a")
        self.assertEqual(cfg["inbounds"][0]["interface_name"], "vd-a")
        self.assertEqual(cfg["log"], {"level": "debug", "timestamp": True})
        self.assertEqual(self.BASE["inbounds"][0]["interface_name"], "tun0")  # original untouched


if __name__ == "__main__":
    unittest.main()
