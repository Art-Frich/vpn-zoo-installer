import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zoolib import system

SS_SAMPLE = """\
tcp   LISTEN 0      4096        127.0.0.1:62789      0.0.0.0:*    users:(("xray-linux-amd6",pid=812,fd=3))
tcp   LISTEN 0      4096          0.0.0.0:22         0.0.0.0:*    users:(("sshd",pid=301,fd=3))
tcp   LISTEN 0      4096             [::]:22            [::]:*    users:(("sshd",pid=301,fd=4))
tcp   LISTEN 0      4096                *:443              *:*    users:(("xray-linux-amd6",pid=812,fd=7))
udp   UNCONN 0      0                   *:443              *:*    users:(("hysteria",pid=900,fd=6))
udp   UNCONN 0      0       127.0.0.53%lo:53         0.0.0.0:*    users:(("systemd-resolve",pid=200,fd=13))
tcp   LISTEN 0      511           0.0.0.0:2096       0.0.0.0:*    users:(("x-ui",pid=700,fd=9))
udp   UNCONN 0      0             0.0.0.0:51822      0.0.0.0:*
tcp   LISTEN 0      128   [::ffff:127.0.0.1]:9000        *:*
garbage line
"""

SHOW_SAMPLE = """\
Id=x-ui.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
MainPID=700

Id=nope.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=
MainPID=0
"""


class SsTest(unittest.TestCase):
    def test_parse(self):
        socks = system.parse_ss(SS_SAMPLE)
        self.assertEqual(len(socks), 9)
        api = socks[0]
        self.assertEqual((api.proto, api.addr, api.port, api.process, api.pid),
                         ("tcp", "127.0.0.1", 62789, "xray-linux-amd6", 812))
        self.assertTrue(api.loopback)
        self.assertFalse(api.public)
        self.assertEqual(socks[2].addr, "::")
        self.assertTrue(socks[2].public)
        self.assertEqual(socks[5].addr, "127.0.0.53")
        self.assertEqual(socks[7].process, "")
        self.assertTrue(socks[8].loopback)

    def test_unexpected_public(self):
        socks = system.parse_ss(SS_SAMPLE)
        allowed = {("tcp", 443), ("udp", 443), ("udp", 51822)} | {("tcp", p) for p in system.ssh_ports(socks)}
        bad = system.unexpected_public(socks, allowed)
        self.assertEqual([(s.proto, s.port) for s in bad], [("tcp", 2096)])

    def test_udp_flow_sockets_of_proxies_are_not_listens(self):
        # живой сервер: Xray открывает сокет на каждый UDP-поток клиента — порт из диапазона ядра
        socks = system.parse_ss(
            'udp UNCONN 0 0 *:37928 *:* users:(("xray-linux-amd6",pid=5,fd=30))\n'
            'udp UNCONN 0 0 0.0.0.0:45001 0.0.0.0:* users:(("x-ui",pid=6,fd=31))\n'
            'udp UNCONN 0 0 *:52000 *:* users:(("hysteria",pid=7,fd=9))\n'
            'udp UNCONN 0 0 *:5353 *:* users:(("xray-linux-amd6",pid=5,fd=32))\n'
            'udp UNCONN 0 0 0.0.0.0:40000 0.0.0.0:* users:(("dnsmasq",pid=8,fd=4))\n'
            'tcp LISTEN 0 4096 *:41000 *:* users:(("xray-linux-amd6",pid=5,fd=33))\n')
        eph = (32768, 60999)
        bad = system.unexpected_public(socks, set(), ufw_open=None, ephemeral=eph)
        self.assertEqual([(s.proto, s.port) for s in bad], [("udp", 5353), ("udp", 40000), ("tcp", 41000)],
                         "UFW выключен: чужой UDP и UDP прокси вне диапазона — тревога")
        bad = system.unexpected_public(socks, set(), ufw_open={("udp", 40000)}, ephemeral=eph)
        self.assertEqual([(s.proto, s.port) for s in bad], [("udp", 40000), ("tcp", 41000)],
                         "UFW включён: UDP — только если UFW его пропускает; TCP вне реестра — всегда")
        self.assertEqual(len(system.unexpected_public(socks, set())), 6, "без диапазона и UFW — как раньше")

    def test_parse_ufw_allowed(self):
        text = ("Status: active\n\nTo                         Action      From\n"
                "--                         ------      ----\n"
                "Anywhere                   REJECT      80.94.92.55                # by Fail2Ban\n"
                "22/tcp                     LIMIT       Anywhere\n"
                "443/udp                    ALLOW       Anywhere                   # hysteria2\n"
                "30925/udp (v6)             ALLOW       Anywhere (v6)              # vpn-zoo tuic\n"
                "8080                       ALLOW IN    Anywhere\n"
                "20000:20002/udp            ALLOW       Anywhere\n"
                "9000/tcp                   DENY        Anywhere\n"
                "OpenSSH                    ALLOW       Anywhere\n")
        self.assertEqual(system.parse_ufw_allowed(text), {
            ("tcp", 22), ("udp", 443), ("udp", 30925), ("tcp", 8080), ("udp", 8080),
            ("udp", 20000), ("udp", 20001), ("udp", 20002)})
        with mock.patch.object(system, "_ufw_status", return_value=(0, "Status: inactive\n")):
            self.assertIsNone(system.ufw_allowed())
            self.assertFalse(system.ufw_active())
        with mock.patch.object(system, "_ufw_status", return_value=(0, text)):
            self.assertIn(("udp", 443), system.ufw_allowed())
            self.assertTrue(system.ufw_active())
        with mock.patch.object(system, "_ufw_status", return_value=(127, "")):
            self.assertIsNone(system.ufw_allowed())
            self.assertIsNone(system.ufw_active())

    def test_ephemeral_range(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sys" / "net" / "ipv4" / "ip_local_port_range"
            self.assertEqual(system.ephemeral_range(Path(d)), (32768, 60999), "нет файла — умолчание ядра")
            f.parent.mkdir(parents=True)
            f.write_text("10000\t20000\n", encoding="ascii")
            self.assertEqual(system.ephemeral_range(Path(d)), (10000, 20000))
            f.write_text("junk\n", encoding="ascii")
            self.assertEqual(system.ephemeral_range(Path(d)), (32768, 60999))

    def test_registry_ports(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ports.tsv"
            f.write_text("443/tcp\tvless\t04\tallow\n20000:20003/udp\thop\t05\tallow\nbad\n", encoding="utf-8")
            ports = system.registry_ports(f)
            self.assertIn(("tcp", 443), ports)
            self.assertIn(("udp", 20003), ports)
            self.assertEqual(len(ports), 5)
            self.assertEqual(system.registry_ports(Path(d) / "missing"), set())


class SystemdTest(unittest.TestCase):
    def test_parse_show(self):
        blocks = system.parse_systemctl_show(SHOW_SAMPLE)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1]["LoadState"], "not-found")

    def test_unit_states(self):
        with mock.patch.object(system, "run", return_value=(0, SHOW_SAMPLE, "")) as run:
            st = system.unit_states(["x-ui.service", "nope.service", "x-ui.service"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[-2:], ["x-ui.service", "nope.service"])
        self.assertEqual(st["x-ui.service"]["active"], "active")
        self.assertEqual(st["nope.service"]["load"], "not-found")

    def test_unit_states_without_systemctl(self):
        with mock.patch.object(system, "run", return_value=(127, "", "нет команды systemctl")):
            st = system.unit_states(["a.service"])
        self.assertEqual(st["a.service"]["active"], "unknown")


class ProcTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        p = Path(self.dir.name)
        (p / "net").mkdir()
        (p / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n", encoding="ascii")
        (p / "meminfo").write_text("MemTotal:  2048 kB\nMemFree: 100 kB\nMemAvailable: 1024 kB\n"
                                   "SwapTotal: 0 kB\nSwapFree: 0 kB\n", encoding="ascii")
        (p / "net" / "dev").write_text(
            "Inter-|   Receive |  Transmit\n face |bytes packets ...\n"
            "    lo: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0\n"
            "  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n", encoding="ascii")
        (p / "uptime").write_text("3725.5 100.0\n", encoding="ascii")
        (p / "loadavg").write_text("0.10 0.20 0.30 1/100 1234\n", encoding="ascii")
        self.patch = mock.patch.object(system, "PROC", p)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_metrics(self):
        self.assertEqual(system.meminfo()["used"], 1024 * 1024)
        self.assertEqual(system.net_counters(), {"eth0": {"rx_bytes": 1000, "rx_packets": 10,
                                                          "tx_bytes": 2000, "tx_packets": 20}})
        self.assertIn("lo", system.net_counters(include_lo=True))
        self.assertEqual(system.uptime_seconds(), 3725.5)
        self.assertEqual(system.loadavg(), (0.1, 0.2, 0.3))
        self.assertEqual(system.cpu_percent(0), 0.0)  # два одинаковых снимка
        m = system.host_metrics(0)
        self.assertEqual(m["mem"]["total"], 2048 * 1024)


@unittest.skipUnless(shutil.which("openssl"), "нет openssl")
class CertTest(unittest.TestCase):
    def test_cert_expiry(self):
        with tempfile.TemporaryDirectory() as d:
            crt, key = Path(d) / "c.crt", Path(d) / "c.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-days", "30", "-subj", "/CN=test", "-keyout", str(key), "-out", str(crt)],
                           check=True, capture_output=True)
            info = system.cert_info(crt)
            self.assertIn(info["days_left"], (29, 30))
            self.assertIsNone(system.cert_info(key)["days_left"])


class VersionsTest(unittest.TestCase):
    def test_pinned(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "versions.env"
            f.write_text("XUI_VERSION=v3.9.0\nXUI_SHA256_amd64=abc\nAWG_GO_REF=v3.1\nGEO_TAG='2026'\n",
                         encoding="utf-8")
            self.assertEqual(system.pinned_versions(f),
                             {"XUI_VERSION": "v3.9.0", "AWG_GO_REF": "v3.1", "GEO_TAG": "2026"})


if __name__ == "__main__":
    unittest.main()
