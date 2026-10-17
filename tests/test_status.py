import os
import unittest

from tests.fakes import FakeBinaries, FakeRunner, tmpdir
from vpn_deck.models import ConfigKind, StoredConfig, to_rpc
from vpn_deck.service_manager import ServiceManager
from vpn_deck.status import StatusProbe, parse_addresses, parse_trace

TRACE = "fl=1\nh=cloudflare.com\nip=203.0.113.7\nts=1\nloc=NL\nwarp=off\n"
ADDR = """5: vd-a    inet 10.8.1.2/32 scope global vd-a\\       valid_lft forever preferred_lft forever
5: vd-a    inet6 fe80::1/64 scope link \\       valid_lft forever preferred_lft forever
5: vd-a    inet6 fd00::2/128 scope global \\       valid_lft forever preferred_lft forever
"""


class ParseTest(unittest.TestCase):
    def test_trace(self):
        info = parse_trace(TRACE, now=5)
        self.assertEqual((info.ip, info.country, info.checked_at), ("203.0.113.7", "NL", 5))
        self.assertIsNone(parse_trace("garbage", 0))
        self.assertIsNone(parse_trace("ip=1.1.1.1\nloc=XX", 0).country)

    def test_addresses_skip_link_local(self):
        self.assertEqual(parse_addresses(ADDR), ("10.8.1.2/32", "fd00::2/128"))


class ProbeTest(unittest.TestCase):
    def test_status(self):
        sysnet = tmpdir("sysnet")
        stats = os.path.join(sysnet, "vd-a", "statistics")
        os.makedirs(stats)

        def counters(rx: int, tx: int) -> None:
            for name, value in (("rx_bytes", rx), ("tx_bytes", tx)):
                with open(os.path.join(stats, name), "w") as f:
                    f.write(str(value))

        runner = (FakeRunner()
                  .on("ip", "-o", "addr", stdout=ADDR)
                  .on("/bin/awg", "show", "vd-a", "latest-handshakes", stdout="peerA\t1700000000\npeerB\t0\n"))
        now = [100.0]
        probe = StatusProbe(runner, ServiceManager(FakeBinaries(), runner), sys_net_dir=sysnet, clock=lambda: now[0])
        cfg = StoredConfig("a", ConfigKind.AWG, "vd-a", "/x")

        counters(1000, 500)
        first = probe.status(cfg)
        self.assertIsNone(first.rx_bytes_per_sec)  # needs two samples
        self.assertEqual(first.latest_handshake, 1700000000)
        self.assertEqual(first.addresses, ("10.8.1.2/32", "fd00::2/128"))

        counters(3000, 1500)
        now[0] = 102.0
        second = probe.status(cfg)
        self.assertEqual((second.rx_bytes_per_sec, second.tx_bytes_per_sec), (1000.0, 500.0))
        rpc = to_rpc(second)
        self.assertEqual(rpc["kind"], "awg")
        self.assertIsInstance(rpc["addresses"], list)


if __name__ == "__main__":
    unittest.main()
