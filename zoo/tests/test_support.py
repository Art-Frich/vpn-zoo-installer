"""«У человека не работает» (D58): последнее подключение из коллектора и пункты проверки на странице человека."""

import re
import time
import unittest
from pathlib import Path

from tests.helpers import ZooEnv, needs_bash
from tests.test_traffic import S
from tests.test_web import AppTestBase
from zoolib import clients, groups, support, traffic, users
from zoolib.support import App, Contacts

DAY = 86400


def U(name, **kw):
    kw.setdefault("protocols", ["vless-reality", "vless-xhttp", "hysteria2", "amneziawg"])
    return users.User(name, **kw)


class LastContactTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.now = int(time.time())

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def snap(self, samples, now):
        con = traffic.connect()
        with con:
            deltas, new = traffic.compute_deltas(traffic.load_counters(con), samples, now)
            traffic.store(con, deltas, new, now)
        con.close()

    def test_latest_mark_wins_with_its_protocol(self):
        t0 = self.now - 3600
        self.snap([S("xray", "masha", 0, 0), S("amneziawg", "masha", 0, 0, seen=t0 - 50),
                   S("hysteria2", "kolya", 0, 0), S("xray", "zoo-probe", 0, 0)], t0)
        # через 5 минут у masha вырос счётчик Xray: отметка — время снятия; у kolya Hysteria «в сети» без трафика
        self.snap([S("xray", "masha", 10, 90), S("amneziawg", "masha", 0, 0, seen=t0 - 50),
                   S("hysteria2", "kolya", 0, 0, seen=t0 + 300), S("xray", "zoo-probe", 5, 5)], t0 + 300)
        last = traffic.last_contact()
        self.assertEqual(last["masha"], (t0 + 300, "xray"))
        self.assertEqual(last["kolya"], (t0 + 300, "hysteria2"))
        self.assertNotIn("zoo-probe", last, "служебный пробник — не человек")
        c = support.contacts()
        self.assertTrue(c.known)
        self.assertEqual(c.of("masha")[1], "xray")

    def test_unknown_before_first_collect(self):
        self.assertEqual(support.contacts(), Contacts(False, {}))

    def test_today_people_skips_service_user(self):
        self.snap([S("xray", "owner", 0, 0), S("xray", "zoo-probe", 0, 0)], self.now - 600)
        self.snap([S("xray", "owner", 10, 90), S("xray", "zoo-probe", 500, 500)], self.now - 300)
        self.assertEqual(traffic.today_people(), {"xray": 100})


class SilentTest(unittest.TestCase):
    def test_never_and_gone_skip_owner_and_disabled(self):
        now = 1_800_000_000
        reg = users.Registry(Path("users.json"), [
            U("owner"), U("masha"), U("kolya"), U("petya"), U("off", enabled=False), U("zoo-probe", system=True)])
        c = Contacts(True, {"masha": (now - 3600, "xray"), "kolya": (now - 8 * DAY, "hysteria2")})
        never, gone = support.silent(reg, c, now)
        self.assertEqual([u.name for u in never], ["petya"])
        self.assertEqual([u.name for u in gone], ["kolya"])
        self.assertEqual(support.silent(reg, Contacts(False, {}), now), ([], []), "сбора ещё не было — никого не винить")


class TipsTest(unittest.TestCase):
    def test_contact_tip(self):
        now = 1_800_000_000
        c = Contacts(True, {"masha": (now - 60, "xray"), "kolya": (now - 30 * DAY, "xray")})
        self.assertEqual(support.contact_tip(U("masha"), c, now)[0], "ok")
        self.assertIn("дольше 7 дней", support.contact_tip(U("kolya"), c, now)[1])
        kind, text = support.contact_tip(U("petya"), c, now)
        self.assertEqual(kind, "warn")
        self.assertIn("Ни разу", text)
        self.assertEqual(support.contact_tip(U("masha", enabled=False), c, now)[0], "bad")
        self.assertEqual(support.contact_tip(U("masha"), Contacts(), now)[0], "info")

    def test_via_title_names_his_xray_protocols(self):
        u = U("masha", protocols=["vless-xhttp", "hysteria2"])
        self.assertEqual(support.via_title("xray", u, ["vless-xhttp", "vless-reality"]), "VLESS XHTTP")
        self.assertEqual(support.via_title("xray", U("x"), ["vless-xhttp", "vless-reality"]),
                         "VLESS XHTTP или VLESS Vision")
        self.assertEqual(support.via_title("hysteria2-obfs"), "Hysteria2 + Salamander")

    def test_only_udp_suggests_tcp_his_app_takes(self):
        happ = App("Android", "Happ", [("hysteria2", "ok")], "apps", {"vless-xhttp", "hysteria2"})
        kind, text = support.transport_tip(["hysteria2"], [happ], ["vless-reality", "vless-xhttp", "hysteria2"])
        self.assertEqual(kind, "warn")
        self.assertIn("только UDP (Hysteria2)", text)
        self.assertIn("Добавьте группе VLESS XHTTP (TCP)", text, "VLESS Vision Happ не берёт по каталогу этого теста")
        none = App("Android", "X", [("hysteria2", "ok")], "apps", {"hysteria2"})
        self.assertIn("смените приложение", support.transport_tip(["hysteria2"], [none], ["vless-xhttp", "hysteria2"])[1])

    def test_both_transports_say_where(self):
        happ = App("Android", "Happ", [("vless-xhttp", "ok"), ("hysteria2", "ok")], "apps")
        awg = App("Android", "AmneziaWG", [("amneziawg", "ok")], "apps")
        kind, text = support.transport_tip(["vless-xhttp", "hysteria2", "amneziawg"], [happ, awg], [])
        self.assertEqual(kind, "info")
        self.assertIn("TCP: VLESS XHTTP в «Happ»", text)
        self.assertIn("UDP: Hysteria2, AmneziaWG", text)

    def test_protocols_his_apps_do_not_take_are_ignored(self):
        hid = App("Windows", "Hiddify", [("hysteria2", "warn")], "all", {"hysteria2", "ss2022"})
        _, text = support.transport_tip(["vless-reality", "hysteria2"], [hid], ["vless-reality", "hysteria2", "ss2022"])
        self.assertIn("только UDP", text, "VLESS Vision Hiddify не берёт — на него не надеяться")
        self.assertIn("Shadowsocks (TCP)", text)

    def test_app_tips_unverified_and_full_tunnel_merged(self):
        found = [App("Android", "Happ", [("hysteria2", "unk")], "apps"),
                 App("iPhone", "Hiddify", [("ss2022", "ok")], "all"),
                 App("Windows", "Hiddify", [("ss2022", "ok")], "all"),
                 App("Windows", "v2rayN", [], "apps")]
        tips = support.app_tips(found)
        texts = [t for _, t in tips]
        self.assertIn("«Happ» (Android) с Hysteria2 не проверено: не работает — дайте другое приложение.", texts)
        self.assertIn("«v2rayN» (Windows) не берёт ни один его протокол: смените приложение или протоколы группы.", texts)
        slow = [t for t in texts if t.startswith("Медленно")]
        self.assertEqual(slow, ["Медленно в «Hiddify» (iPhone, Windows): через VPN идёт всё устройство — "
                                "дайте приложение, где идёт не всё."], "одно приложение без замены — одна строка")
        found.append(App("iPhone", "INCY", [("vless-xhttp", "ok")], "ru-direct"))
        self.assertIn("Медленно в «Hiddify» (iPhone): через VPN идёт всё устройство — пусть включит «INCY».",
                      [t for _, t in support.app_tips(found)])

    def test_checklist_for_happ_hysteria_group(self):
        """Случай из обхода: у группы один Hysteria2, у человека Happ на Android — UDP и непроверенная связка."""
        cat = clients.load()
        g = groups.Group("g2", "Группа 2", protocols=["hysteria2"], clients={"android": ["happ"]})
        u = U("masha", group="g2")
        happ_hy = cat.status(cat.client("happ"), "hysteria2", "android")
        tips = support.checklist(u, g, Contacts(True, {}), cat, ["vless-reality", "vless-xhttp", "hysteria2"], {})
        kinds = [k for k, _ in tips]
        self.assertEqual(kinds[0], "warn", "ни разу не подключалась")
        self.assertTrue(any("только UDP (Hysteria2)" in t for _, t in tips))
        self.assertTrue(any("Добавьте группе VLESS Vision (TCP)" in t or "Добавьте группе VLESS XHTTP (TCP)" in t
                            for _, t in tips))
        self.assertEqual(any("не проверено" in t for _, t in tips), happ_hy == "unk")


@needs_bash
class PagesTest(AppTestBase):
    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "hysteria2"):
            self.env.add_protocol(pid)
        users.bootstrap()
        users.add_user("masha")
        users.add_user("kolya")
        users.add_user("petya")
        now = int(time.time())
        con = traffic.connect()
        with con:
            traffic.store(con, [], {("hysteria2", "masha"): traffic.Counter(0, 0, "", now, now - 600),
                                    ("xray", "kolya"): traffic.Counter(0, 0, "", now, now - 9 * DAY)}, now)
        con.close()
        self.c.login()

    def test_person_page_says_when_and_how(self):
        _, body = self.c.get("/users/masha")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        self.assertIn("подключался 10 мин назад · Hysteria2", text)
        self.assertIn('<details class="card more trouble"><summary>Если у него не работает</summary>', body)
        self.assertIn("До сервера доходит", text)
        _, body = self.c.get("/users/petya")
        self.assertIn("подключался ни разу", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))
        self.assertIn('href="/handoff?u=petya"', body, "ни разу — переслать сообщение")

    def test_overview_names_silent_people(self):
        _, body = self.c.get("/")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        self.assertIn("Ни разу не подключались: 1 — petya", text)
        self.assertIn("Не подключались дольше 7 дней: 1 — kolya", text)
        self.assertNotIn("owner", text.split("Ни разу")[1].split("Протоколы")[0], "ключи админа — не «человек»")

    def test_users_table_column(self):
        _, body = self.c.get("/users")
        self.assertIn(">ни разу<", body)
        self.assertIn('title="Hysteria2"', body)


if __name__ == "__main__":
    unittest.main()
