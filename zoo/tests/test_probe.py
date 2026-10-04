"""Пробник: классификация, адреса в probe, .conf AWG, пакет экспорта, HTTP через SOCKS5,
прогон с фейковыми клиентами (сценарии цензора) и CLI. Без сети: всё на 127.0.0.1."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from zoolib.probe import clients, endpoints, engine, fetch, report, verdicts
from zoolib.probe.verdicts import Obs, classify

from tests.helpers import ZooEnv, manifest, needs_bash
from tests.test_cli import run_cli

V = verdicts


# ---------- классификация ----------

class ClassifyTest(unittest.TestCase):
    def ok_tcp(self, **kw) -> Obs:
        base = dict(layer="tcp", l4="ok", small_ok=True, large_ok=True, large_bytes=2_000_000,
                    speed_mbps=50.0, latency_ms=80.0)
        base.update(kw)
        return Obs(**base)

    def test_ok(self):
        self.assertEqual(classify(self.ok_tcp())[0], V.OK)

    def test_slow_speed_and_latency(self):
        self.assertEqual(classify(self.ok_tcp(speed_mbps=0.5))[0], V.SLOW)
        self.assertEqual(classify(self.ok_tcp(latency_ms=3000))[0], V.SLOW)
        th = V.Thresholds(min_mbps=0.1)
        self.assertEqual(classify(self.ok_tcp(speed_mbps=0.5), th=th)[0], V.OK)

    def test_freeze_16k(self):
        obs = self.ok_tcp(large_ok=False, large_bytes=9000, large_kind="stall", speed_mbps=None)
        verdict, reason = classify(obs)
        self.assertEqual(verdict, V.FREEZE_16K)
        self.assertIn("8 КБ", reason)
        # обрыв RST после малого объёма — тоже заморозка
        self.assertEqual(classify(self.ok_tcp(large_ok=False, large_bytes=0, large_kind="reset"))[0], V.FREEZE_16K)
        # встал до заголовков ответа (бюджет съели рукопожатия REALITY + TLS)
        obs = self.ok_tcp(large_ok=False, large_bytes=0, large_kind="header_timeout")
        self.assertEqual(classify(obs)[0], V.FREEZE_16K)

    def test_stall_late_is_slow(self):
        obs = self.ok_tcp(large_ok=False, large_bytes=900_000, large_kind="stall")
        self.assertEqual(classify(obs)[0], V.SLOW)

    def test_udp_stall_is_not_freeze(self):
        obs = self.ok_tcp(layer="udp", tunnel="ok", large_ok=False, large_bytes=9000, large_kind="stall")
        self.assertEqual(classify(obs)[0], V.SLOW)

    def test_target_unavailable_keeps_ok(self):
        obs = self.ok_tcp(large_ok=False, large_bytes=0, large_kind="http", large_error="HTTP 403",
                          speed_mbps=None)
        verdict, reason = classify(obs)
        self.assertEqual(verdict, V.OK)
        self.assertIn("тестовый сервер", reason)

    def test_tcp_connect_timeout(self):
        obs = Obs(layer="tcp", l4="timeout")
        self.assertEqual(classify(obs)[0], V.IP_BLOCKED)
        self.assertEqual(classify(obs, mode="local")[0], V.SERVER_DOWN)
        self.assertEqual(classify(obs, selftest=V.SERVER_DOWN)[0], V.SERVER_DOWN)
        # другие TCP-порты сервера отвечают — в причине блокировка порта, а не всего IP
        verdict, reason = classify(obs, tcp_reachable=True)
        self.assertEqual(verdict, V.IP_BLOCKED)
        self.assertIn("блокировку порта", reason)

    def test_refused(self):
        self.assertEqual(classify(Obs(layer="tcp", l4="refused"))[0], V.SERVER_DOWN)
        self.assertEqual(classify(Obs(layer="udp", l4="refused"))[0], V.SERVER_DOWN)

    def test_tcp_handshake_fail(self):
        obs = Obs(layer="tcp", l4="ok", small_ok=False, small_error="соединение сброшено (RST, tls)")
        verdict, reason = classify(obs)
        self.assertEqual(verdict, V.HANDSHAKE_FAIL)
        self.assertIn("RST", reason)
        self.assertEqual(classify(obs, selftest=V.HANDSHAKE_FAIL)[0], V.SERVER_DOWN)
        self.assertEqual(classify(obs, selftest=V.OK)[0], V.HANDSHAKE_FAIL)

    def test_udp_silent(self):
        obs = Obs(layer="udp", l4="unknown", tunnel="timeout", tunnel_error="нет рукопожатия")
        self.assertEqual(classify(obs, tcp_reachable=True)[0], V.UDP_BLOCKED)
        self.assertEqual(classify(obs, tcp_reachable=False)[0], V.IP_BLOCKED)
        self.assertEqual(classify(obs, tcp_reachable=None)[0], V.UDP_BLOCKED)
        self.assertEqual(classify(obs, selftest=V.SERVER_DOWN)[0], V.SERVER_DOWN)

    def test_udp_rejected_is_handshake(self):
        obs = Obs(layer="udp", tunnel="rejected", tunnel_error="hysteria: authentication error")
        self.assertEqual(classify(obs, tcp_reachable=True)[0], V.HANDSHAKE_FAIL)

    def test_udp_lazy_client_small_fail(self):
        # TUIC (sing-box) — без явного рукопожатия: молчание по UDP → UDP_BLOCKED
        obs = Obs(layer="udp", tunnel="unknown", small_ok=False, small_error="eof")
        self.assertEqual(classify(obs, tcp_reachable=True)[0], V.UDP_BLOCKED)

    def test_udp_local(self):
        obs = Obs(layer="udp", l4="ok", tunnel="timeout")
        self.assertEqual(classify(obs, mode="local")[0], V.HANDSHAKE_FAIL)
        obs.l4 = "unknown"
        self.assertEqual(classify(obs, mode="local")[0], V.SERVER_DOWN)

    def test_client_error_and_skip(self):
        self.assertEqual(classify(Obs(client_error="xray упал"))[0], V.CLIENT_ERROR)
        self.assertEqual(classify(Obs(skipped="нет клиента"))[0], V.SKIPPED)

    def test_compare(self):
        self.assertEqual(V.compare_one(V.OK, V.OK)[0], "works")
        cat, text = V.compare_one(V.OK, V.FREEZE_16K)
        self.assertEqual(cat, "blocked")
        self.assertIn("блокируется у вас (FREEZE_16K)", text)
        self.assertEqual(V.compare_one(V.SERVER_DOWN, V.IP_BLOCKED)[0], "server")
        self.assertEqual(V.compare_one(V.OK, V.SERVER_DOWN)[0], "blocked")
        self.assertEqual(V.compare_one(None, V.UDP_BLOCKED)[0], "blocked")
        self.assertEqual(V.compare_one(V.OK, V.SKIPPED)[0], "unknown")
        self.assertEqual(V.compare_one(V.OK, None)[0], "unknown")


# ---------- адреса в probe ----------

VLESS = {"kind": "xray", "outbound": {"protocol": "vless", "settings": {"vnext": [{"address": "1.2.3.4", "port": 443}]},
                                      "streamSettings": {"network": "tcp", "security": "reality"}}}
SS = {"kind": "xray", "outbound": {"protocol": "shadowsocks", "settings": {"servers": [
    {"address": "1.2.3.4", "port": 23456, "method": "2022-blake3-aes-128-gcm", "password": "a:b"}]}}}
HY = {"kind": "hysteria", "client": {"server": "1.2.3.4:443", "auth": "tok", "tls": {"sni": "x", "insecure": True}},
      "hop": {"server": "1.2.3.4:443,20000-30000"}}
TUIC = {"kind": "sing-box", "outbound": {"type": "tuic", "server": "1.2.3.4", "server_port": 34567}}
AWG_CONF = """[Interface]
PrivateKey = AAAA
Address = 10.66.66.2/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1280
Jc = 4
H1 = 123

[Peer]
PublicKey = BBBB
PresharedKey = CCCC
Endpoint = 1.2.3.4:51822
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""
AWG = {"kind": "awg", "conf": AWG_CONF, "endpoint": "1.2.3.4:51822"}


class EndpointTest(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(str(endpoints.endpoint_of(VLESS)), "1.2.3.4:443/tcp")
        self.assertEqual(str(endpoints.endpoint_of(SS)), "1.2.3.4:23456/tcp")
        self.assertEqual(str(endpoints.endpoint_of(HY)), "1.2.3.4:443/udp")
        self.assertEqual(str(endpoints.endpoint_of(TUIC)), "1.2.3.4:34567/udp")
        self.assertEqual(str(endpoints.endpoint_of(AWG)), "1.2.3.4:51822/udp")
        flat = {"kind": "xray", "outbound": {"settings": {"address": "5.6.7.8", "port": 8443}}}
        self.assertEqual(str(endpoints.endpoint_of(flat)), "5.6.7.8:8443/tcp")
        self.assertEqual(endpoints.split_hostport("[2001:db8::1]:443"), ("2001:db8::1", 443))
        with self.assertRaises(endpoints.EndpointError):
            endpoints.endpoint_of({"kind": "openvpn"})

    def test_with_host(self):
        for p in (VLESS, SS, HY, TUIC, AWG):
            q = endpoints.with_host(p, "127.0.0.1")
            self.assertEqual(endpoints.endpoint_of(q).host, "127.0.0.1")
            self.assertEqual(endpoints.endpoint_of(p).host, "1.2.3.4", "оригинал не трогаем")
        q = endpoints.with_host(AWG, "@gateway")
        self.assertIn("Endpoint = @gateway:51822", q["conf"])
        self.assertNotIn("hop", endpoints.with_host(HY, "127.0.0.1"))


class AwgConfTest(unittest.TestCase):
    def test_strip(self):
        s = clients.strip_conf(AWG_CONF)
        for gone in ("Address", "DNS", "MTU"):
            self.assertNotIn(gone, s)
        for kept in ("PrivateKey = AAAA", "Jc = 4", "H1 = 123", "PresharedKey = CCCC", "AllowedIPs = 0.0.0.0/0, ::/0"):
            self.assertIn(kept, s)
        self.assertEqual(clients.conf_value(AWG_CONF, "interface", "dns"), "1.1.1.1, 1.0.0.1")

    def test_tunnel_params(self):
        t = clients.AwgTunnel(AWG, Path("."), "bind")
        self.assertEqual((t.address, t.dns, t.mtu), ("10.66.66.2/32", "1.1.1.1", "1280"))
        self.assertLessEqual(len(t.iface), 15)
        self.assertLessEqual(len(t.veth_host), 15)

    def test_hysteria_config_keeps_only_connection_fields(self):
        probe = dict(HY, client=dict(HY["client"], tun={"name": "hy0"}, tcpTProxy={"listen": ":2500"},
                                     tcpForwarding=[{"listen": "0.0.0.0:6600", "remote": "x:22"}],
                                     http={"listen": "0.0.0.0:8080"}))
        cfg = clients.HysteriaClient(probe, Path("."), 8.0).config()
        for k in ("tun", "tcpTProxy", "tcpForwarding", "http"):
            self.assertNotIn(k, cfg)
        self.assertEqual(cfg["server"], HY["client"]["server"])
        self.assertEqual(cfg["socks5"]["listen"].split(":")[0], "127.0.0.1")

    def test_hysteria_failure(self):
        log = '2026-10-04T05:00:00Z\tFATAL\tfailed to initialize client\t{"error": "connect error: timeout: no recent network activity"}'
        self.assertEqual(clients.hysteria_failure(log)[0], "timeout")
        log = '2026\tFATAL\tfailed to initialize client\t{"error": "authentication error, HTTP status code: 404"}'
        self.assertEqual(clients.hysteria_failure(log)[0], "rejected")


# ---------- пакет экспорта ----------

class BundleTest(unittest.TestCase):
    def test_formats(self):
        from zoolib.probe import load_bundle
        ours = {"server_ip": "1.2.3.4", "user": "owner",
                "protocols": [{"id": "vless-reality", "layer": "tcp", "port": 443, "probe": VLESS},
                              {"id": "hysteria2", "layer": "udp", "port": 443, "probe": None}],
                "selftest": {"generated": "x", "verdicts": {"vless-reality": "OK"}}}
        entries, meta = load_bundle(ours)
        self.assertEqual([e["id"] for e in entries], ["hysteria2", "vless-reality"])
        self.assertIsNone(entries[0]["probe"])
        self.assertEqual(meta["selftest"]["verdicts"], {"vless-reality": "OK"})
        # {id: probe} и список манифестов
        entries, meta = load_bundle({"server": "1.2.3.4", "protocols": {"amneziawg": AWG}})
        self.assertEqual(entries[0]["probe"]["kind"], "awg")
        self.assertEqual(meta["server_ip"], "1.2.3.4")
        entries, _ = load_bundle([manifest("ss2022", probe=SS), manifest("off", enabled=False)])
        self.assertEqual([e["id"] for e in entries], ["ss2022"])
        # /etc/vpn-setup/probe-export.json фазы 99
        p99 = {"version": 1, "generated": "2026-10-04T06:00:00+00:00", "server_ip": "1.2.3.4", "label": "vpn",
               "protocols": [{"id": "hysteria2", "name": "Hysteria2", "layer": "udp", "port": 443,
                              "engine": "hysteria", "enabled": True, "probe": HY},
                             {"id": "tuic", "name": "TUIC", "layer": "udp", "port": 3, "engine": "x-ui",
                              "enabled": False, "probe": TUIC}]}
        entries, meta = load_bundle(p99)
        self.assertEqual([e["id"] for e in entries], ["hysteria2"])
        self.assertEqual((meta["server_ip"], meta["label"], meta["selftest"]), ("1.2.3.4", "vpn", None))


class SummaryTest(unittest.TestCase):
    def test_install_summary_and_next_steps(self):
        import contextlib
        import io
        rep = {"mode": "local", "server_ip": "1.2.3.4", "user": "zoo-probe", "results": [
            {"id": "vless-reality", "port": 443, "layer": "tcp", "verdict": "OK", "speed_mbps": 90.0},
            {"id": "tuic", "port": 3, "layer": "udp", "verdict": "SERVER_DOWN", "reason": "порт закрыт"},
            {"id": "amneziawg", "port": 5, "layer": "udp", "verdict": "SKIPPED", "reason": "нет клиента"}]}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            report.render_summary(rep)
            report.render_next_steps(rep, "/etc/vpn-setup/probe-export.json", ssh_port="2222")
        text = out.getvalue()
        self.assertIn("работает в принципе", text)
        self.assertIn("Работает в принципе: 1 из 3.", text)
        self.assertIn("НЕТ", text)
        self.assertIn("scp -P 2222 root@1.2.3.4:/etc/vpn-setup/probe-export.json", text)
        self.assertIn("docker run --rm --cap-add NET_ADMIN", text)
        self.assertIn("git clone https://github.com/Art-Frich/vpn-zoo-installer.git", text)
        self.assertIn('-v "${PWD}\probe:/data"', text)
        self.assertIn("tuic: НЕ РАБОТАЕТ на самом сервере (SERVER_DOWN) — порт закрыт", err.getvalue())
        self.assertIn("amneziawg: не проверено (SKIPPED)", err.getvalue())

    def test_repo_url_for_hint(self):
        from unittest import mock
        from zoolib import probe, upgrade
        cases = {
            "https://user:tok@github.com/Owner/vpn-zoo-installer.git": "https://github.com/Owner/vpn-zoo-installer.git",
            "git@github.com:Owner/vpn-zoo-installer.git": "https://github.com/Owner/vpn-zoo-installer.git",
            "/tmp/zoo.bundle": probe.DEFAULT_REPO_URL,
        }
        for origin, want in cases.items():
            with mock.patch.object(upgrade, "repo_dir", return_value=Path("/opt/vpn-zoo-src")),                     mock.patch.object(upgrade, "_git", return_value=(0, origin)):
                self.assertEqual(probe._repo_url(), want, origin)
        with mock.patch.object(upgrade, "repo_dir", return_value=None):
            self.assertEqual(probe._repo_url(), probe.DEFAULT_REPO_URL)


# ---------- HTTP через SOCKS5 ----------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/204"):
            self.send_response(204)
            self.end_headers()
        elif self.path.startswith("/big"):
            self.send_response(200)
            self.send_header("Content-Length", "300000")
            self.end_headers()
            self.wfile.write(b"x" * 300000)
        elif self.path.startswith("/freeze"):
            self.send_response(200)
            self.send_header("Content-Length", "300000")
            self.end_headers()
            self.wfile.write(b"x" * 12000)
            self.wfile.flush()
            time.sleep(3)
        elif self.path.startswith("/trace"):
            body = b"fl=1\nip=203.0.113.7\nts=1\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class _Socks5:
    """Минимальный SOCKS5 с паролем: CONNECT на 127.0.0.1:<порт> (имя хоста игнорируется)."""

    def __init__(self, target_port: int, user: str = "u", password: str = "p") -> None:
        self.target_port, self.user, self.password = target_port, user, password
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c: socket.socket):
        try:
            c.recv(3)
            c.sendall(b"\x05\x02")
            ver, ulen = c.recv(2)
            user = c.recv(ulen).decode()
            plen = c.recv(1)[0]
            pw = c.recv(plen).decode()
            if (user, pw) != (self.user, self.password):
                c.sendall(b"\x01\x01")
                return
            c.sendall(b"\x01\x00")
            head = c.recv(5)
            c.recv(head[4] + 2)
            up = socket.create_connection(("127.0.0.1", self.target_port))
            c.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", self.port))
            for a, b in ((c, up), (up, c)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()
        except OSError:
            c.close()

    @staticmethod
    def _pipe(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    def close(self):
        self.sock.close()


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.port = cls.http.server_address[1]
        cls.proxy = _Socks5(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.proxy.close()

    def url(self, path: str) -> str:
        return f"http://zoo.test:{self.port}{path}"

    def socks(self, **kw):
        return dict({"host": "127.0.0.1", "port": self.proxy.port, "user": "u", "password": "p"}, **kw)

    def test_small_and_big(self):
        r = fetch.fetch(self.url("/204"), socks=self.socks())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], 204)
        r = fetch.fetch(self.url("/big"), socks=self.socks())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["bytes"], 300000)
        r = fetch.fetch(self.url("/big"), socks=self.socks(), limit=100000)
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["bytes"], 100000)

    def test_stall(self):
        r = fetch.fetch(self.url("/freeze"), socks=self.socks(), stall=1.0)
        self.assertFalse(r["ok"])
        self.assertTrue(r["stalled"])
        self.assertEqual(r["error_kind"], "stall")
        self.assertEqual(r["bytes"], 12000)

    def test_trace_body_and_http_error(self):
        r = fetch.fetch(self.url("/trace"), socks=self.socks(), keep_body=100)
        self.assertRegex(r["body"], r"ip=203\.0\.113\.7")
        r = fetch.fetch(self.url("/nope"), socks=self.socks())
        self.assertEqual((r["ok"], r["error_kind"]), (False, "http"))

    def test_socks_auth_and_refused(self):
        r = fetch.fetch(self.url("/204"), socks=self.socks(password="bad"))
        self.assertEqual(r["error_kind"], "socks")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        r = fetch.fetch(f"http://127.0.0.1:{dead}/", connect_timeout=2)
        # Windows повторяет SYN после RST и упирается в таймаут — проверяем только на Linux
        if os.name != "nt":
            self.assertEqual(r["error_kind"], "refused")

    def test_dns_parse(self):
        q = struct.pack("!HHHHHH", 7, 0x8180, 1, 1, 0, 0) + fetch._qname("a.example") + struct.pack("!HH", 1, 1)
        ans = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("192.0.2.5")
        self.assertEqual(fetch.parse_dns_a(q + ans, 7), ["192.0.2.5"])
        with self.assertRaises(fetch.FetchError):
            fetch.parse_dns_a(q + ans, 8)


# ---------- прогон с фейковыми клиентами: сценарии цензора ----------

class FakeClient:
    """Клиент без процессов: ответы fetch по сценарию."""

    def __init__(self, scenario: dict):
        self.s = scenario

    def start(self, timeout):
        if self.s.get("missing"):
            raise clients.ClientMissing("нет клиента fake")

    def handshake(self, timeout):
        return self.s.get("handshake", ("unknown", "", None))

    def run_jobs(self, jobs):
        out = []
        for j in jobs:
            url = j["url"]
            if "generate_204" in url:
                out.append(dict(self.s.get("small", {"ok": True, "seconds": 0.05}), url=url))
            elif "__down" in url or "10Mb" in url:
                out.append(dict(self.s.get("large", {"ok": True, "bytes": 2_000_000, "body_seconds": 0.4,
                                                     "seconds": 0.5}), url=url))
            else:
                out.append({"ok": True, "url": url, "body": "ip=198.51.100.1\n"})
        return out

    def log_tail(self, n=6):
        return "лог"

    def stop(self):
        pass


def _entry(pid, probe, layer):
    return {"id": pid, "name": pid, "layer": layer, "port": 443, "probe": probe}


ENTRIES = [_entry("vless-reality", VLESS, "tcp"), _entry("ss2022", SS, "tcp"),
           _entry("hysteria2", HY, "udp"), _entry("amneziawg", AWG, "udp")]


class EngineTest(unittest.TestCase):
    def run_scenario(self, l4_tcp="ok", udp=("ok", "", 30.0), small=None, large=None, mode="remote",
                     selftest=None, l4_by_host=None):
        def fake_make(probe, workdir, mode_, stall):
            sc = {"handshake": udp if probe["kind"] in ("hysteria", "awg") else ("unknown", "", None)}
            if small and probe["kind"] == "xray":
                sc["small"] = small
            if large and probe["kind"] == "xray":
                sc["large"] = large
            return FakeClient(sc)

        def fake_tcp(host, port, timeout, tries=3):
            st = (l4_by_host or {}).get(host, l4_tcp)
            return st, (12.0 if st == "ok" else None)

        st = engine.Settings(mode=mode, progress=None)
        with mock.patch.object(clients, "make_client", fake_make), \
                mock.patch.object(engine, "tcp_check", fake_tcp), \
                mock.patch.object(engine, "udp_check", lambda h, p, timeout=1.5: "unknown"), \
                mock.patch.object(engine, "udp_listening", lambda p: "ok"):
            res = engine.run(ENTRIES, st, server_ip="198.51.100.1", selftest=selftest)
        return {r["id"]: r["verdict"] for r in res}, res

    def test_clean(self):
        got, res = self.run_scenario()
        self.assertEqual(set(got.values()), {V.OK})
        self.assertEqual(res[0]["egress"], "server")
        self.assertEqual(res[0]["speed_mbps"], 40.0)

    def test_drop_udp(self):
        got, _ = self.run_scenario(udp=("timeout", "нет рукопожатия", None))
        self.assertEqual(got, {"vless-reality": V.OK, "ss2022": V.OK, "hysteria2": V.UDP_BLOCKED,
                               "amneziawg": V.UDP_BLOCKED})

    def test_ip_block(self):
        got, _ = self.run_scenario(l4_tcp="timeout", udp=("timeout", "нет рукопожатия", None))
        self.assertEqual(set(got.values()), {V.IP_BLOCKED})

    def test_freeze(self):
        got, res = self.run_scenario(large={"ok": False, "bytes": 11000, "stalled": True, "error_kind": "stall",
                                            "error": "нет данных 8 с"})
        self.assertEqual(got["vless-reality"], V.FREEZE_16K)
        self.assertEqual(got["hysteria2"], V.OK)
        self.assertIn("client_log", res[0])

    def test_rst_tls(self):
        got, _ = self.run_scenario(small={"ok": False, "error_kind": "reset", "error": "RST"})
        self.assertEqual(got["vless-reality"], V.HANDSHAKE_FAIL)

    def test_selftest_marks_server_down(self):
        got, _ = self.run_scenario(small={"ok": False, "error_kind": "eof", "error": "eof"},
                                   selftest={"vless-reality": V.HANDSHAKE_FAIL, "ss2022": V.OK})
        self.assertEqual(got["vless-reality"], V.SERVER_DOWN)
        self.assertEqual(got["ss2022"], V.HANDSHAKE_FAIL)

    def test_local_fallback_to_loopback(self):
        # публичный IP недоступен с самого сервера (нет hairpin), loopback — работает
        got, res = self.run_scenario(mode="local", l4_by_host={"1.2.3.4": "timeout", "127.0.0.1": "ok"})
        self.assertEqual(got["vless-reality"], V.OK)
        r = next(x for x in res if x["id"] == "vless-reality")
        self.assertEqual(r["target"], "loopback")
        self.assertTrue(any("hairpin" in n for n in r["notes"]))

    def test_skipped(self):
        st = engine.Settings(progress=None)
        res = engine.run([_entry("x", None, "tcp")], st)
        self.assertEqual(res[0]["verdict"], V.SKIPPED)

    def test_report_render(self):
        _, res = self.run_scenario(udp=("timeout", "нет рукопожатия", None))
        rep = {"mode": "remote", "server_ip": "1.2.3.4", "user": "owner", "results": res,
               "compare": report.compare({"hysteria2": V.OK}, {"results": res})}
        md = report.markdown(rep)
        self.assertIn("UDP_BLOCKED", md)
        self.assertIn("блокируется у вас (UDP_BLOCKED)", md)
        with mock.patch("sys.stdout"):
            report.render(rep)


# ---------- CLI ----------

class ProbeCliTest(unittest.TestCase):
    def test_export_and_compare(self):
        with ZooEnv() as env:
            env.add_manifest("vless-reality", probe=dict(VLESS, user="owner"))
            env.add_manifest("hysteria2", layer="udp", probe=dict(HY, user="owner"), users_backend="none")
            code, out, err = run_cli("export-probe")
            self.assertEqual(code, 0, err)
            bundle = json.loads(out)
            self.assertEqual(bundle["type"], "zoo-probe-export")
            self.assertEqual(bundle["server_ip"], "10.0.0.1")
            self.assertEqual({e["id"] for e in bundle["protocols"]}, {"vless-reality", "hysteria2"})
            self.assertIn("ключи", err)
            out_file = env.root / "bundle.json"
            code, _, _ = run_cli("export-probe", "--out", str(out_file))
            self.assertEqual(code, 0)
            local = env.root / "l.json"
            remote = env.root / "r.json"
            local.write_text(json.dumps({"results": [{"id": "vless-reality", "verdict": "OK"}]}), encoding="utf-8")
            remote.write_text(json.dumps({"results": [{"id": "vless-reality", "verdict": "FREEZE_16K"}]}),
                              encoding="utf-8")
            code, out, _ = run_cli("--json", "probe", "--compare", str(local), str(remote))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["compare"][0]["category"], "blocked")

    @needs_bash
    def test_export_other_user_via_module(self):
        from zoolib.probe import collect_entries
        with ZooEnv() as env:
            env.add_protocol("hysteria2", users=("owner", "masha"), layer="udp", probe=dict(HY, user="owner"))
            env.add_manifest("hysteria2-obfs", layer="udp", probe=dict(HY, user="owner"))
            env.add_protocol("ss2022", users=("owner",), probe=dict(SS, user="owner"))
            env.fail("ss2022:probe")
            got = {e["id"]: e for e in collect_entries("masha")}
            self.assertEqual(got["hysteria2"]["probe"]["outbound"]["user"], "masha")
            # инстанс модуля: proto_hysteria2_probe masha obfs
            self.assertIn("hysteria2 probe masha obfs", env.calls())
            # модуль не дал probe masha — проба кредами owner из манифеста, с пометкой
            self.assertEqual(got["ss2022"]["probe"]["user"], "owner")
            self.assertIn("искусственная ошибка", got["ss2022"]["note"])
            self.assertIn("кредами owner", got["ss2022"]["note"])
            # owner — прямо из манифеста, без вызова модуля
            calls = len(env.calls())
            collect_entries("owner")
            self.assertEqual(len(env.calls()), calls)

    def test_export_empty(self):
        with ZooEnv():
            code, _, err = run_cli("export-probe")
            self.assertEqual(code, 1)
            self.assertIn("пакет пуст", err)

    def test_remote_missing_file(self):
        with ZooEnv() as env:
            code, _, err = run_cli("probe", "--remote", str(env.root / "nope.json"))
            self.assertEqual(code, 1)
            self.assertIn("нет файла", err)


if __name__ == "__main__":
    unittest.main()
