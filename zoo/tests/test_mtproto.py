"""MTProxy для Telegram (D63): рукопожатие Fake-TLS пробника против фейкового mtg, каталог (Telegram — встроенный
прокси, у остальных mtproto: no), группы и вариант «Только Telegram», инструкция без шага установки, переключатель."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import socket
import struct
import threading
import time
import unittest
from pathlib import Path

from zoolib import clients, groups, manifests, protoctl, protolib
from zoolib.probe import clients as pclients
from zoolib.probe import endpoints, engine, mtproto, verdicts
from zoolib.web import clientviews, logs

KEY = bytes(range(16))
DOMAIN = "www.example.com"
SECRET = "ee" + KEY.hex() + DOMAIN.encode().hex()


def _recv(conn: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise ConnectionError("клиент закрыл соединение")
        out += chunk
    return out


def _read_record(conn: socket.socket) -> bytes:
    head = _recv(conn, 5)
    return head + _recv(conn, int.from_bytes(head[3:5], "big"))


class FakeMtg:
    """Сервер как mtg (mtglib/internal/tls/fake): своему секрету — ServerHello + CCS + ApplicationData с подписанным
    random; чужому — «настоящий сайт» (ответ той же формы без подписи). skew — сдвиг часов сервера."""

    def __init__(self, key: bytes = KEY, skew: float = 0.0, close: bool = False) -> None:
        self.key, self.skew, self.close = key, skew, close
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.accepted: list[bool] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _accepts(self, hello: bytes) -> bool:
        off = mtproto.RANDOM_OFFSET
        zeroed = hello[:off] + bytes(32) + hello[off + 32:]
        mac = bytearray(hmac.new(self.key, zeroed, hashlib.sha256).digest())
        got = hello[off:off + 32]
        x = bytes(a ^ b for a, b in zip(mac, got))
        if x[:28] != bytes(28):
            return False
        return abs(time.time() + self.skew - struct.unpack("<I", x[28:])[0]) <= 3

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            try:
                hello = _read_record(conn)
            except OSError:
                return   # проверка порта движком: подключилась и закрыла
            if self.close:
                return
            ok = self._accepts(hello)
            self.accepted.append(ok)
            sh_body = b"\x03\x03" + bytes(32) + b"\x20" + os.urandom(32) + b"\x13\x01\x00"
            sh = b"\x02" + len(sh_body).to_bytes(3, "big") + sh_body
            packet = bytearray(b"\x16\x03\x03" + struct.pack(">H", len(sh)) + sh
                               + b"\x14\x03\x03\x00\x01\x01" + b"\x17\x03\x03\x00\x40" + os.urandom(64))
            off = mtproto.RANDOM_OFFSET
            if ok:
                mac = hmac.new(self.key, hello[off:off + 32] + bytes(packet), hashlib.sha256).digest()
                packet[off:off + 32] = mac
            else:
                packet[off:off + 32] = os.urandom(32)
            conn.sendall(bytes(packet))

    def stop(self) -> None:
        self.sock.close()


class HandshakeTest(unittest.TestCase):
    def test_secret_parsing(self):
        self.assertEqual(mtproto.parse_secret(SECRET), (KEY, DOMAIN))
        for bad in ("dd" + KEY.hex(), "ee" + KEY.hex(), "eezz"):
            with self.assertRaises(ValueError):
                mtproto.parse_secret(bad)

    def test_client_hello_carries_sni_and_signed_random(self):
        hello = mtproto.client_hello(KEY, DOMAIN, now=1_700_000_000)
        self.assertEqual(hello[:3], b"\x16\x03\x01", "запись Handshake версии 3,1 — так её ждёт mtg")
        self.assertIn(DOMAIN.encode(), hello)
        off = mtproto.RANDOM_OFFSET
        mac = hmac.new(KEY, hello[:off] + bytes(32) + hello[off + 32:], hashlib.sha256).digest()
        x = bytes(a ^ b for a, b in zip(mac, hello[off:off + 32]))
        self.assertEqual(x[:28], bytes(28))
        self.assertEqual(struct.unpack("<I", x[28:])[0], 1_700_000_000)

    def test_accepted_secret_is_ok(self):
        srv = FakeMtg()
        self.addCleanup(srv.stop)
        st, err, ms = mtproto.handshake("127.0.0.1", srv.port, SECRET, 5)
        self.assertEqual((st, err), ("ok", ""))
        self.assertIsNotNone(ms)
        self.assertEqual(srv.accepted, [True])

    def test_foreign_secret_gets_the_real_site_and_is_rejected(self):
        srv = FakeMtg(key=bytes(16))
        self.addCleanup(srv.stop)
        st, err, _ = mtproto.handshake("127.0.0.1", srv.port, SECRET, 5)
        self.assertEqual(st, "rejected")
        self.assertIn("секрет не принят", err)

    def test_clock_skew_beyond_three_seconds_is_rejected(self):
        srv = FakeMtg(skew=30)
        self.addCleanup(srv.stop)
        self.assertEqual(mtproto.handshake("127.0.0.1", srv.port, SECRET, 5)[0], "rejected")

    def test_silent_close_is_fail(self):
        srv = FakeMtg(close=True)
        self.addCleanup(srv.stop)
        self.assertEqual(mtproto.handshake("127.0.0.1", srv.port, SECRET, 5)[0], "fail")


class EngineTest(unittest.TestCase):
    def probe(self, port: int) -> dict:
        return {"kind": "mtproto", "user": "owner", "server": "127.0.0.1", "server_port": port, "secret": SECRET}

    def test_endpoint_and_host_swap(self):
        p = self.probe(4433)
        self.assertEqual(str(endpoints.endpoint_of(p)), "127.0.0.1:4433/tcp")
        self.assertEqual(endpoints.with_host(p, "10.0.0.1")["server"], "10.0.0.1")
        self.assertTrue(engine.uses_tls(p), "соединение начинается с ClientHello")
        self.assertIn("mtproto", manifests.PROBE_KINDS)

    def test_handshake_only_verdict(self):
        srv = FakeMtg()
        self.addCleanup(srv.stop)
        st = engine.Settings(mode="remote", timeout=5, latency_samples=0, progress=None)
        res = engine.run([{"id": "mtproto", "name": "MTProxy", "layer": "tcp", "port": srv.port,
                           "probe": self.probe(srv.port)}], st)[0]
        self.assertEqual(res["verdict"], verdicts.OK)
        self.assertIn(pclients.MtprotoClient.NOTE, res["notes"])
        self.assertEqual(res["handshake"]["status"], "ok")

    def test_rejected_secret_is_handshake_fail(self):
        srv = FakeMtg(key=bytes(16))
        self.addCleanup(srv.stop)
        st = engine.Settings(mode="remote", timeout=5, latency_samples=0, progress=None)
        res = engine.run([{"id": "mtproto", "layer": "tcp", "port": srv.port, "probe": self.probe(srv.port)}], st)[0]
        self.assertEqual(res["verdict"], verdicts.HANDSHAKE_FAIL)

    def test_bad_secret_is_client_error(self):
        st = engine.Settings(mode="remote", timeout=2, latency_samples=0, progress=None)
        srv = FakeMtg()
        self.addCleanup(srv.stop)
        p = {**self.probe(srv.port), "secret": "dd00"}
        res = engine.run([{"id": "mtproto", "layer": "tcp", "port": srv.port, "probe": p}], st)[0]
        self.assertEqual(res["verdict"], verdicts.CLIENT_ERROR)


TG = protolib.Link("tg://proxy?server=1.2.3.4&port=24443&secret=" + SECRET, "", "mtproto", "uri")
HY2 = protolib.Link("hysteria2://tok@1.2.3.4:443?sni=bing.com#x", "", "hysteria2", "uri")


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.cat = clients.load()

    def test_telegram_is_the_only_mtproto_client(self):
        tg = self.cat.client("telegram")
        self.assertTrue(self.cat.builtin(tg))
        self.assertEqual(set(tg["platforms"]), set(self.cat.platforms))
        for c in self.cat.clients:
            want = "ok" if c["id"] == "telegram" else "no"
            self.assertEqual(c["protocols"].get("mtproto", {}).get("s"), want, c["id"])
        self.assertEqual(self.cat.names_for("mtproto"), "Telegram")

    def test_builtin_needs_own_check_and_no_via(self):
        raw = clients.load().raw
        tg = next(c for c in raw["clients"] if c["id"] == "telegram")
        tg["via"] = {"android": "all"}
        with self.assertRaises(clients.ClientsError):
            clients.validate(raw)


class PackTest(unittest.TestCase):
    def setUp(self):
        self.cat = clients.load()

    def pack(self, plat, links, prefer, order, admin=False):
        return clientviews.build_pack(self.cat, {"checked": None, "versions": {}}, plat, links, [], prefer, order,
                                      admin=admin)

    def test_telegram_only_message_has_no_install_and_no_vpn_words(self):
        for admin in (False, True):
            p = self.pack("android", [TG], {"android": ["telegram"]}, ["mtproto"], admin)
            self.assertTrue(p.telegram_only)
            msg = p.message
            self.assertTrue(msg.startswith("{name}, прокси для Telegram на Android: что сделать"), msg)
            self.assertNotIn("Установите", msg)
            self.assertNotIn("Через VPN", msg)
            self.assertIn("«Подключить прокси»", msg)
            self.assertIn(self.cat.client("telegram")["check"], msg)
            self.assertEqual(p.sections[0].method, "link")

    def test_telegram_next_to_vpn_is_not_a_fallback_app(self):
        p = self.pack("android", [HY2, TG], {"android": ["telegram", "happ"]}, ["hysteria2", "mtproto"])
        self.assertEqual([s.client["id"] for s in p.sections], ["happ", "telegram"], "Telegram — после VPN")
        main, head, more = p.parts()
        self.assertEqual((head, more), ("", []), "запасного приложения нет: Telegram — не запасной VPN")
        joined = "\n".join(main)
        self.assertLess(joined.index("Happ"), joined.index("«Подключить прокси»"))
        self.assertIn("Через VPN", p.message)

    def test_paper_card_scans_tg_link(self):
        p = self.pack("ios", [TG], {"ios": ["telegram"]}, ["mtproto"])
        paper, head, more = p.parts(paper=True)
        self.assertTrue(any("камеру" in x for x in paper), paper)
        self.assertEqual(p.paper_rest, [])
        win = self.pack("windows", [TG], {"windows": ["telegram"]}, ["mtproto"])
        self.assertEqual(win.paper_rest, ["Telegram"], "QR с бумаги открыл бы Telegram на телефоне, а не на компьютере")


class GroupsTest(unittest.TestCase):
    def setUp(self):
        self.cat = clients.load()

    def test_telegram_preset_only_when_mtproto_is_on(self):
        every = ["hysteria2", "vless-xhttp", "amneziawg", "vless-reality"]
        ids = [p["id"] for p in groups.presets(self.cat, every, "mixed")]
        self.assertNotIn(groups.TELEGRAM, ids)
        prs = groups.presets(self.cat, every + ["mtproto"], "mixed")
        by = {p["id"]: p for p in prs}
        self.assertEqual(by[groups.TELEGRAM]["protocols"], ["mtproto"])
        self.assertEqual({tuple(v) for v in by[groups.TELEGRAM]["plan"].values()}, {("telegram",)})
        for pid in ("simple", "reliable"):
            self.assertNotIn("mtproto", by[pid]["protocols"], "MTProxy — не VPN-вариант")
        self.assertEqual(groups.recommended_preset(prs), "reliable", "«Только Telegram» не советуется вместо VPN")
        self.assertEqual(groups.via_line(self.cat, by[groups.TELEGRAM]["plan"]), "", "через VPN тут ничего не идёт")

    def test_all_protocols_group_skips_mtproto(self):
        g = groups.Group("main", "Основная")
        self.assertEqual(g.resolve(["hysteria2", "mtproto"], {}), ["hysteria2"])
        self.assertEqual(g.offered(["hysteria2", "mtproto"]), ["hysteria2"])
        tg = groups.Group("tg", "Telegram", protocols=["mtproto"])
        self.assertEqual(tg.resolve(["hysteria2", "mtproto"], {}), ["mtproto"])
        self.assertEqual(tg.offered(["hysteria2", "mtproto"]), ["mtproto"])


class ProtoctlTest(unittest.TestCase):
    def test_static_and_last_protocol(self):
        self.assertEqual((protoctl.STATIC["mtproto"].phase, protoctl.STATIC["mtproto"].var),
                         ("04e-mtproto", "ENABLE_MTPROTO"))
        ctls = {"hysteria2": protoctl.Ctl("hysteria2", "Hysteria2", "05-hysteria2", "ENABLE_HY2", enabled=True),
                "mtproto": protoctl.Ctl("mtproto", "MTProxy", "04e-mtproto", "ENABLE_MTPROTO", enabled=True)}
        with self.assertRaises(protoctl.JobError):
            protoctl.check("hysteria2", "disable", ctls)   # MTProxy доступа к VPN не даёт
        self.assertEqual(protoctl.check("mtproto", "disable", ctls).id, "mtproto")


class LogsTest(unittest.TestCase):
    def test_tg_link_is_masked(self):
        out = logs.sanitize("ссылка owner: tg://proxy?server=1.2.3.4&port=24443&secret=" + SECRET)
        self.assertNotIn(SECRET, out)
        self.assertNotIn(KEY.hex(), out)


MODULE = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "proto-mtproto.sh"


@unittest.skipUnless(MODULE.is_file(), "нет scripts/lib/proto-mtproto.sh (установленная копия)")
class ModuleTest(unittest.TestCase):
    def test_module_has_contract_functions(self):
        text = MODULE.read_text(encoding="utf-8")
        for fn in ("user_add", "user_del", "user_enable", "user_list", "links", "probe", "manifest_refresh", "traffic",
                   "disable"):
            self.assertIn(f"proto_mtproto_{fn}()", text)


if __name__ == "__main__":
    unittest.main()


from tests.helpers import needs_bash  # noqa: E402
from tests.test_groupviews import GroupWebBase, text_of  # noqa: E402
from tests.test_no_duplicates import Page, norm  # noqa: E402


@needs_bash
class WizardTest(GroupWebBase):
    """Мастер с включённым MTProxy: вариант «Только Telegram» → группа без шага «Через VPN»."""

    def setUp(self):
        super().setUp()
        self.env.add_protocol("mtproto", probe={"kind": "mtproto", "server": "1.2.3.4", "server_port": 24443,
                                                "secret": SECRET},
                              links=[{"user": "owner", "uri": TG.uri}])

    def test_telegram_preset_card_and_flow(self):
        resp, body = self.c.get("/connect/new")
        self.assertEqual(resp.status, 200)
        text = text_of(body)
        self.assertIn("Только Telegram", text)
        self.assertIn("VPN-приложение не нужно", text)
        self.assertEqual(text.count("рекомендуем"), 1)
        p = Page()
        p.feed(body)
        for tag, title, shown in p.titles:
            self.assertNotEqual(norm(title), norm(shown), (tag, title))
        resp, body = self.wiz(0, go="telegram", mode="mixed")
        self.assertIn("<h3>Приложения</h3>", body)
        self.assertEqual(re.findall(r'type="hidden" name="proto" value="([^"]+)"', body), ["mtproto"])
        self.assertEqual(self.checked(body, "android"), ["telegram"])
        resp, body = self.wiz(2, mode="mixed", proto="mtproto", clients_for="mtproto|mixed", devs="1",
                              dev=["android", "ios", "windows"], set__android="telegram", set__ios="telegram",
                              set__windows="telegram", preset="telegram")
        self.assertIn("<h3>Люди</h3>", body)
        self.assertNotIn("Через VPN — общий список или свой для группы", body, "у прокси Telegram списка нет")
        resp, _ = self.wiz(3, go="create", mode="mixed", name="Телеграм", proto="mtproto", devs="1",
                           dev=["android", "ios", "windows"], set__android="telegram", set__ios="telegram",
                           set__windows="telegram", users_new="masha", allow_mode="common", confirm="1")
        self.assertEqual(resp.status, 303)
        g = next(x for x in self.groups_json() if x["name"] == "Телеграм")
        self.assertEqual(g["protocols"], ["mtproto"])
        self.assertEqual(self.env.proto_users("mtproto").keys() >= {"masha"}, True)
        self.assertNotIn("masha", self.env.proto_users("hysteria2"), "в VPN-протоколы человек не заведён")


@needs_bash
class UsersTest(unittest.TestCase):
    """Вне групп и в «Основной» (*) MTProxy не выдаётся сам: только owner и служебный пробник."""

    def test_sync_and_add_skip_mtproto_outside_explicit_groups(self):
        from tests.helpers import ZooEnv
        from zoolib import users
        with ZooEnv() as env:
            for pid in ("hysteria2", "mtproto"):
                env.add_protocol(pid)
            users.bootstrap()
            users.add_user("masha")                 # «Основная» (*)
            users.add_user("kolya", group="")       # вне групп
            users.sync_users()
            self.assertIn("mtproto", users.list_users().get("owner").protocols)
            for name in ("masha", "kolya"):
                self.assertEqual(users.list_users().get(name).protocols, ["hysteria2"], name)
                self.assertNotIn(name, env.proto_users("mtproto"))
