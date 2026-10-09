"""«У человека не работает» (D58): последнее подключение из коллектора и пункты проверки на странице человека."""

import re
import time
import unittest
from pathlib import Path
from unittest import mock

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
        c = Contacts(True, {"masha": (now - 60, "xray"), "kolya": (now - 30 * DAY, "xray"),
                            "olya": (now - 2 * DAY, "hysteria2")})
        self.assertEqual(support.contact_tip(U("masha"), c, now)[0], "ok")
        self.assertIn("дольше 7 дней", support.contact_tip(U("kolya"), c, now)[1])
        kind, text = support.contact_tip(U("olya"), c, now)
        self.assertEqual(kind, "warn", "два дня тишины при жалобе «сейчас не работает» — не «доходит»")
        self.assertIn("Последний раз — 2 д 0 ч назад: сейчас до сервера не доходит", text)
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
        happ = App("Android", "Happ", [("hysteria2", "ok")], "apps", {"vless-xhttp", "hysteria2"}, "android")
        (kind, text, fix), = support.transport_tips([happ], ["hysteria2"], ["vless-reality", "vless-xhttp", "hysteria2"])
        self.assertEqual((kind, fix), ("warn", "android"), "правка группы — по устройству: счётчик людей с Android")
        self.assertIn("Android: только UDP (Hysteria2)", text)
        self.assertIn("Добавьте группе VLESS XHTTP: «Happ» его берёт.", text, "VLESS Vision Happ не берёт по каталогу теста")

    def test_tcp_only_is_not_an_alarm_and_names_the_udp_the_app_lacks(self):
        """Случай из обхода: у группы есть AmneziaWG, но v2rayN его не берёт — зачем ещё UDP, сказано прямо."""
        v2 = App("Windows", "v2rayN", [("vless-reality", "ok")], "apps", {"vless-reality", "hysteria2"}, "windows")
        (kind, text, _), = support.transport_tips([v2], ["vless-reality", "amneziawg"],
                                                  ["vless-reality", "amneziawg", "hysteria2"])
        self.assertEqual(kind, "info", "только TCP у iPhone и Windows — так задумано, не тревога")
        self.assertIn("AmneziaWG у группы есть, но «v2rayN» его не берёт — добавьте группе Hysteria2", text)

    def test_both_transports_per_device(self):
        happ = App("Android", "Happ", [("vless-xhttp", "ok")], "apps", plat="android")
        awg = App("Android", "AmneziaWG", [("amneziawg", "ok")], "apps", plat="android")
        incy = App("iPhone", "INCY", [("vless-xhttp", "ok")], "ru-direct", plat="ios")
        tips = support.transport_tips([awg, happ, incy], ["vless-xhttp", "amneziawg"], [], clients.load())
        self.assertEqual([k for k, _, _ in tips], ["info", "info"])
        self.assertIn("Android: не подключается — пусть включит TCP (VLESS XHTTP) в «Happ»", tips[0][1])
        self.assertTrue(tips[1][1].startswith("iPhone: только TCP (VLESS XHTTP) — обычно хватает."))

    def test_app_tips_unverified_and_full_tunnel_name_the_replacement(self):
        cat = clients.load()
        found = [App("Android", "Happ", [("hysteria2", "unk")], "apps", plat="android"),
                 App("Windows", "AmneziaVPN", [("amneziawg", "ok")], "all", plat="windows"),
                 App("Windows", "X", [], "apps", plat="windows")]
        tips = support.app_tips(found, cat, ["amneziawg", "vless-reality"])
        texts = [t for _, t, _ in tips]
        self.assertIn("«Happ» (Android) с Hysteria2 не проверено: не работает — дайте другое приложение.", texts)
        self.assertIn("«X» (Windows) не берёт ни один его протокол: смените приложение или протоколы группы.", texts)
        self.assertIn("Медленно в «AmneziaVPN» (Windows): через VPN идёт всё устройство — смените Windows в группе "
                      "на «v2rayN» (ему достанется VLESS Vision).", texts, "замена называет, какой протокол ей достанется")
        found.append(App("Windows", "v2rayN", [("vless-reality", "ok")], "apps", plat="windows"))
        self.assertIn("Медленно в «AmneziaVPN» (Windows): через VPN идёт всё устройство — пусть включит «v2rayN».",
                      [t for _, t, _ in support.app_tips(found, cat, ["amneziawg", "vless-reality"])])

    def test_server_tip_first(self):
        g = groups.Group("g2", "Бухгалтерия", protocols=["hysteria2"], clients={"android": ["happ"]})
        u = U("masha", group="g2")
        now = 1_800_000_000
        c = Contacts(True, {"masha": (now - 2 * DAY, "hysteria2")})
        tips = support.checklist(u, g, c, clients.load(), ["vless-xhttp", "hysteria2"], {}, now, broken={"hysteria2"})
        self.assertEqual(tips[0][0], "bad")
        self.assertIn("На сервере не работает Hysteria2 — другого у человека нет", tips[0][1])
        self.assertNotEqual(tips[1][0], "ok")

    def test_server_tip_only_suggests_what_his_app_takes(self):
        """Третий обход (D60): у группы AmneziaWG и Hysteria2, у Дины Happ — AmneziaWG он не берёт: «другого нет»."""
        cat = clients.load()
        g = groups.Group("g3", "Склад", protocols=["amneziawg", "hysteria2"], clients={"android": ["happ"],
                                                                                     "windows": ["amneziavpn"]})
        dina = U("dina", group="g3", devices=["android"])
        tips = support.checklist(dina, g, Contacts(True, {}), cat, ["amneziawg", "hysteria2", "vless-xhttp"], {},
                                 broken={"hysteria2"})
        self.assertEqual(tips[0][0], "bad")
        self.assertIn("На сервере не работает Hysteria2 — другого у человека нет", tips[0][1])
        self.assertNotIn("AmneziaWG", tips[0][1], "Happ его не берёт — не советовать")
        gleb = U("gleb", group="g3", devices=["windows"])
        tips = support.checklist(gleb, g, Contacts(True, {}), cat, ["amneziawg", "hysteria2", "vless-xhttp"], {},
                                 broken={"hysteria2"})
        self.assertFalse(any("На сервере не работает" in t for _, t, _ in tips),
                         "AmneziaVPN не берёт Hysteria2: его сбой Глеба не касается")
        slow = next(t for _, t, _ in tips if t.startswith("Медленно в «AmneziaVPN»"))
        self.assertIn("смените Windows в группе на «v2rayN» и добавьте группе VLESS XHTTP (сейчас ему достанется только "
                      "Hysteria2 — он не работает).", slow, "замена не ведёт на упавший протокол")
        reg = users.Registry(Path("users.json"), [dina, gleb])
        gs = groups.Groups(Path("groups.json"), [g])
        with mock.patch.object(users, "selectable_protocols", return_value=["amneziawg", "hysteria2", "vless-xhttp"]), \
                mock.patch.object(users, "variant_modules", return_value={}):
            lost = support.stranded([{"id": "hysteria2", "enabled": True, "ok": False}], reg, gs)
        self.assertEqual(lost, {"Склад": ("g3", 1)}, "без VPN — Дина (Happ), не Глеб (AmneziaWG)")

    def test_speed_tip_names_the_backup(self):
        happ = App("Android", "Happ", [("vless-xhttp", "ok"), ("vless-reality", "ok")], "apps", plat="android")
        self.assertIn("Android: пусть включит запасной ключ VLESS Vision", support.speed_tip([happ], [], []))
        v2 = App("Windows", "v2rayN", [("vless-xhttp", "ok")], "apps", {"vless-xhttp", "hysteria2"}, "windows")
        text = support.speed_tip([v2], ["vless-xhttp"], ["vless-xhttp", "hysteria2"])
        self.assertIn("добавьте группе Hysteria2 («v2rayN» его берёт)", text)
        self.assertIn("Медленно у всех — «Обзор»", text)

    def test_checklist_for_happ_hysteria_group(self):
        """Случай из обхода: у группы один Hysteria2, у человека Happ на Android — UDP и непроверенная связка."""
        cat = clients.load()
        g = groups.Group("g2", "Группа 2", protocols=["hysteria2"], clients={"android": ["happ"]})
        u = U("masha", group="g2")
        tips = support.checklist(u, g, Contacts(True, {}), cat, ["vless-reality", "vless-xhttp", "hysteria2"], {})
        self.assertEqual(tips[0][0], "warn", "ни разу не подключалась")
        self.assertTrue(any("Android: только UDP (Hysteria2)" in t for _, t, _ in tips))
        self.assertTrue(any("Добавьте группе VLESS Vision: «Happ» его берёт." in t
                            or "Добавьте группе VLESS XHTTP: «Happ» его берёт." in t for _, t, _ in tips))


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
        from unittest import mock
        from zoolib.web import userviews
        with mock.patch.object(userviews, "_broken", return_value=set()):   # стенд теста не поднимает сервисы
            _, body = self.c.get("/users/masha")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        self.assertIn("подключался 10 мин назад · Hysteria2", text)
        self.assertIn('<summary>Если у человека не работает</summary>', body)
        self.assertIn("до сервера доходит", text)
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
