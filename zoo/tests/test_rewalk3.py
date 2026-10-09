"""Третий обход персонами (D60): Mac и Windows в офисе, «Кому переслать» по устройствам, один человек через форму,
подсказки «не работает» по его приложениям, «только Telegram» и iPhone."""

import html
import re
import unittest

from tests.helpers import needs_bash
from tests.test_groupviews import GroupWebBase, text_of
from tests.test_web import header
from zoolib import clients, groups, protolib, support, users
from zoolib.web import clientviews, distviews, userviews

CACHE = {"checked": None, "versions": {}}
LINKS = clientviews.synth_links(["vless-xhttp", "vless-reality", "amneziawg"])


def pack(plat, ids, admin=False):
    return clientviews.build_pack(clients.load(), CACHE, plat, LINKS, [], {plat: ids},
                                  ["vless-xhttp", "vless-reality", "amneziawg"], True, None, None, admin)


class MessagesTest(unittest.TestCase):
    def test_windows_admin_steps_go_to_it(self):
        it = pack("windows", ["v2rayn"], admin=True).message
        self.assertTrue(it.startswith("{name}, VPN уже установлен. Включите его в «v2rayN»."), it)
        for gone in ("Перезапустить от имени администратора", "Устаревшая защита TUN", "права администратора"):
            self.assertNotIn(gone, it, "ставит ИТ — шаги с правами администратора делает ИТ")
        own = pack("windows", ["v2rayn"]).message
        self.assertIn("Дальше нужны права администратора компьютера (их нет — попросите ИТ). В «v2rayN»: «Настройки»", own)
        cat = clients.load()
        c = cat.client("v2rayn")
        memo = distviews._how({"client": c, "platform": "windows", "store": False, "links": c["platforms"]["windows"]}, cat)
        self.assertIn("Затем с правами администратора:", memo)
        self.assertIn("«Перезапустить от имени администратора»", memo)

    def test_mac_gatekeeper_and_brave_shortcut(self):
        msg = pack("macos", ["v2rayn"]).message
        self.assertIn("«Конфиденциальность и безопасность» → внизу «Всё равно открыть»", msg)
        self.assertIn("xattr -cr /Applications/v2rayN.app", msg)
        self.assertIn("~/Desktop/Brave-VPN.command", msg, "ярлык один раз, а не команда при каждом запуске")
        self.assertIn("двойным щелчком по «Brave-VPN»", msg)

    def test_reliable_android_keeps_happ_as_a_separate_fallback(self):
        msg = pack("android", ["amneziawg", "happ"]).message
        main, _, fb = msg.partition("\n\nЕсли «AmneziaWG» не подключается — запасное приложение «Happ»:\n")
        self.assertTrue(fb, msg)
        self.assertNotIn("Happ", main.split("\n", 2)[2], "основные шаги — без Happ")
        self.assertLessEqual(len(main.splitlines()), 7, "основное — короткое: Brave, AmneziaWG, импорт, правило, проверка")
        self.assertIn("Inbounds", fb)

    def test_common_file_is_labelled_by_his_devices(self):
        conf = protolib.Link("/x/amneziawg.conf", "", "amneziawg", "file")
        self.assertEqual(userviews._variant_label(conf, 0, ["macOS"])[0], "Файл macOS")
        self.assertEqual(userviews._variant_label(conf, 0, ["Android"])[0], "Файл")
        self.assertEqual(userviews._variant_label(conf, 0)[0], "Файл Windows, iPhone", "без сведений — как раньше")

    def test_lost_device_text_follows_his_devices(self):
        self.assertIn("Потерял компьютер", userviews.lost_text(["windows"], ["Windows"]))
        self.assertNotIn("телефон", userviews.lost_text(["windows"], ["Windows"]))
        self.assertIn("Потерял телефон —", userviews.lost_text(["ios"], ["iPhone"]))
        both = userviews.lost_text(["android", "windows"], ["Android", "Windows"])
        self.assertIn("Потерял телефон или компьютер", both)
        self.assertIn("(Android, Windows)", both)


@needs_bash
class OfficeWebTest(GroupWebBase):
    def make(self, **kw):
        resp, body = self.create_group(**kw)
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        return next(g["id"] for g in self.groups_json() if g["name"] == kw.get("name", "Семья"))

    def test_app_change_marks_only_people_with_that_device(self):
        gid = self.make(name="Склад", client__android="amneziawg", client__windows="amneziavpn",
                        users_new="Дина; ; android\nГлеб; ; windows")
        g = groups.Groups.load().get(gid)
        rep = groups.update(gid, clients={**g.clients, "windows": ["v2rayn"]})
        self.assertEqual(rep.resend, ["gleb"], "у Дины только Android: правка Windows её не касается")
        self.assertEqual(users.list_users().get("dina").resend, [])
        self.assertEqual(users.list_users().get("gleb").resend, ["all"])

    def test_add_one_person_asks_devices(self):
        _, page = self.c.get("/users")
        self.assertIn('name="devs" value="1"', page)
        resp, _ = self.post("/users", {"display": ["Пётр Новый"], "devs": ["1"]})
        self.assertEqual(header(resp, "Location"), ["/users"])
        self.assertIsNone(users.list_users().get("petr-novyy"), "без устройств не заводим")
        _, page = self.c.get("/users")
        self.assertIn("Отметьте устройства человека", page)
        resp, _ = self.post("/users", {"display": ["Пётр Новый"], "devs": ["1"], "dev": ["ios", "windows"]})
        self.assertEqual(header(resp, "Location"), ["/users/petr-novyy"])
        self.assertEqual(users.list_users().get("petr-novyy").devices, ["ios", "windows"])
        _, page = self.c.get("/users/petr-novyy")
        self.assertIn("Пётр Новый: пользователь создан", html.unescape(page))
        self.assertNotIn("(vless-reality", page, "без id протоколов")
        self.assertIn('href="/handoff?u=petr-novyy"', page, "раздать — как после списка")
        self.assertNotIn('data-pp="android"', page)

    def test_trouble_card_counts_by_device_and_says_group_once(self):
        self.make(name="Офис", proto=["hysteria2"], client__android="happ", client__windows="v2rayn",
                  users_new="Анна\nБорис; ; windows")
        g = groups.Groups.load().get("g1")
        anna = users.list_users().get("anna")
        card, _ = userviews.trouble_card(anna, g, support.Contacts(True, {}))
        text = text_of(str(card))
        self.assertIn("Устройства не отмечены — подсказки по всем устройствам группы", text)
        self.assertEqual(text.count("Правка — на всю группу"), 1, "одна строка про всю группу, а не у каждой подсказки")
        self.assertIn("Потерял телефон или компьютер", text)
        self.assertIn("Режет ли сеть у человека", text)
        self.assertIn("Медленно: с VPN намного медленнее, чем без", text)
        boris = users.list_users().get("boris")
        card, _ = userviews.trouble_card(boris, g, support.Contacts(True, {}))
        text = text_of(str(card))
        self.assertNotIn("Устройства не отмечены", text)
        self.assertNotIn("Android", text, "у Бориса только Windows — подсказки только по нему")
        self.assertIn('title="новое сообщение получат 2 чел. с Windows"', str(card), "Анна «как у группы» — тоже с Windows")

    def test_only_telegram_warns_about_the_site_version_and_iphone_dead_end(self):
        gid = self.make(name="Бухгалтерия", client__android="amneziawg", client__ios="incy",
                        users_new="Елена; ; iphone\nОлег; ; android")
        resp, _ = self.post("/apps", {"action": ["save"], "group": [gid], "android": ["org.telegram.messenger"],
                                      "windows": ["Telegram.exe"]})
        self.assertEqual(header(resp, "Location"), [f"/apps?group={gid}"])
        _, page = self.c.get(f"/apps?group={gid}")
        text = html.unescape(text_of(page))
        self.assertIn("Telegram отмечен только из Google Play", text)
        self.assertIn("Список не действует: Елена (iPhone — всё, кроме российских сайтов)", text)
        self.assertIn("уберите iPhone из устройств человека", text, "тупик — с выходом")
        self.assertIn("отметьте обе", text)

    def test_existing_group_is_not_preselected(self):
        self.make(name="Офис", users_new="Иван")
        _, page = self.c.get("/connect/new")
        sel = re.search(r'<select name="to"[^>]*>(.*?)</select>', page, re.S).group(1)
        self.assertNotRegex(sel, r'value="g1" selected')
        self.assertIn("— выберите группу —", sel)


if __name__ == "__main__":
    unittest.main()
