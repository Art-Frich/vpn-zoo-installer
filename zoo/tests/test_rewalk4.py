"""Четвёртый обход персонами (D61): «потерял телефон» — новые ключи только ему и «поставьте заново» на новом; Excel;
сколько получат после правки — как «Кому переслать», «добавьте ещё один ключ»; «Ставит ИТ» — Brave и список в памятке;
кто ставит — по устройствам; короткое сообщение при новом списке; запасное приложение при сбое на «Обзоре»."""

import html
import io
import re
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests.helpers import needs_bash
from tests.test_groupviews import GroupWebBase, text_of
from tests.test_web import header
from zoolib import allowlist, clients, groups, resend, support, users
from zoolib.web import clientviews, distviews, userviews

XUI = mock.patch.object(support, "xui_protocols", return_value=["vless-reality"])   # в стенде теста все модули — «xui»


def msg(page, plat):
    return html.unescape(re.search(rf'<pre id="msg-{plat}"[^>]*>(.*?)</pre>', page, re.S).group(1))


@needs_bash
class LostPhoneTest(GroupWebBase):
    def setUp(self):
        super().setUp()
        XUI.start()
        self.addCleanup(XUI.stop)

    def make(self, android):
        resp, body = self.create_group(name="Офис", proto=["vless-reality", "amneziawg"], client__android=android,
                                       client__windows="v2rayn", client__ios="incy",
                                       users_new="Иван Петров; ; android, windows")
        self.assertEqual(resp.status, 303, text_of(body)[:300])

    def calls(self, start):
        return [c.split()[:2] for c in self.env.calls()[start:] if c.split()[1] in ("user_add", "user_del")]

    def test_lost_phone_changes_only_its_keys_and_says_install_again(self):
        self.make("amneziawg")
        _, page = self.c.get("/users/ivan-petrov")
        text = text_of(page)
        self.assertIn("Потерял телефон или компьютер — новые ключи только для него", text)
        self.assertIn("Android: сменятся AmneziaWG, остальное не тронем", text)
        self.assertIn(">Потерял Android</button>", page)
        start = len(self.env.calls())
        resp, _ = self.c.post("/users/ivan-petrov/lost", {"dev": "android"})
        self.assertEqual(header(resp, "Location"), ["/users/ivan-petrov"])
        self.assertEqual(self.calls(start), [["amneziawg", "user_del"], ["amneziawg", "user_add"]],
                         "ключ VLESS у компьютера не тронут")
        u = users.list_users().require("ivan-petrov")
        self.assertEqual(u.resend, ["keys:amneziawg", "lost:android"])
        _, page = self.c.get("/users/ivan-petrov")
        android = msg(page, "android")
        self.assertIn("Это настройка нового телефона: приложения и ключи — по шагам ниже", android)
        self.assertNotIn("уже стоят", android, "на новом телефоне ничего не стоит (четвёртый обход)")
        self.assertNotIn("Это ", msg(page, "windows").split("\n")[1], "компьютеру пересылать нечего")
        self.assertIn("Переслать: Android: новый телефон — сообщение целиком (поставить приложения)", html.unescape(page))
        # новый телефон другой: iPhone вместо Android — «поставьте заново» и у него, и в ZIP
        groups.set_devices(["ivan-petrov"], ["ios", "windows"])
        _, page = self.c.get("/users/ivan-petrov")
        self.assertIn("Это настройка нового телефона", msg(page, "ios"))
        resp, _ = self.c.post("/handoff/export", {"u": "ivan-petrov", "fmt": "zip"})
        text = zipfile.ZipFile(io.BytesIO(resp.body)).read("ivan-petrov/instruction.txt").decode("utf-8")
        self.assertIn("Это настройка нового телефона", text)
        self.assertNotIn("Приложения уже стоят", text)

    def test_shared_vless_key_tells_which_computer_to_redo(self):
        self.make(["amneziawg", "happ"])
        cat = clients.load()
        u = users.list_users().require("ivan-petrov")
        g = groups.Groups.load().get(u.group)
        plan = resend.lost_plan(cat, u, g, "android")
        self.assertEqual(plan.mods, ["amneziawg", "vless-reality"], "у Happ на телефоне был ключ VLESS")
        self.assertEqual(plan.others, {"windows": ["v2rayN"]})
        _, page = self.c.get("/users/ivan-petrov")
        self.assertIn("До встречи можно «Отключить»", text_of(page))
        self.c.post("/users/ivan-petrov/lost", {"dev": "android"})
        _, page = self.c.get("/users/ivan-petrov")
        self.assertTrue(msg(page, "windows").split("\n")[1].startswith(
            "Это новые ключи взамен старых. В «v2rayN» сначала удалите старые подключения"), msg(page, "windows")[:200])
        self.assertIn("Это настройка нового телефона", msg(page, "android"))


@needs_bash
class CountsTest(GroupWebBase):
    def test_add_protocol_count_equals_resend_and_says_add_one_key(self):
        resp, body = self.create_group(name="Офис", proto=["hysteria2"], client__android="happ", client__windows="v2rayn",
                                       users_new="Анна; ; android, windows\nБорис; ; windows\nВера; ; iphone")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        g = groups.Groups.load().get("g1")
        anna = users.list_users().get("anna")
        card, _ = userviews.trouble_card(anna, g, support.Contacts(True, {}))
        text = text_of(str(card))
        self.assertIn("Добавьте группе VLESS Vision: «Happ» его берёт.", text)
        self.assertIn("Правка — на всю группу «Офис»: ещё один ключ получат 2 чел., старые подключения у них работают",
                      text, "Вере с iPhone без приложений пересылать нечего")
        rep = groups.update("g1", protocols=["hysteria2", "vless-reality"])
        self.assertEqual(sorted(rep.resend), ["anna", "boris"], "столько же, сколько обещала подсказка")
        reg = users.list_users()
        self.assertTrue(all(k.startswith("add:") for k in reg.get("anna").resend), reg.get("anna").resend)
        _, page = self.c.get("/users/boris")
        self.assertIn("Добавлен ещё один ключ: в «v2rayN» добавьте только «VLESS Vision» по шагам ниже. Остальные "
                      "подключения не трогайте", msg(page, "windows"), "не «удалите всё»")
        _, page = self.c.get("/resend")
        self.assertIn("ещё один ключ «VLESS Vision» — сообщение (добавить только его, старые работают)", html.unescape(page))

    def test_switch_app_message_turns_old_one_off(self):
        resp, body = self.create_group(name="Склад", proto=["amneziawg"], client__android="amneziawg",
                                       client__windows="amneziavpn", users_new="Глеб; ; windows")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        g = groups.Groups.load().get("g1")
        gleb = users.list_users().get("gleb")
        card, _ = userviews.trouble_card(gleb, g, support.Contacts(True, {}))
        self.assertIn("новое сообщение получат 1 чел.", text_of(str(card)))
        groups.update("g1", protocols=["amneziawg", "vless-reality"], clients={**g.clients, "windows": ["v2rayn"]})
        _, page = self.c.get("/users/gleb")
        line = msg(page, "windows").split("\n")[1]
        self.assertTrue(line.startswith("Приложение сменилось: в «AmneziaVPN» выключите VPN и удалите подключение"), line)
        self.assertIn("Дальше — «v2rayN» по шагам ниже", line)


@needs_bash
class OfficeTest(GroupWebBase):
    def test_mixed_mode_phone_self_computer_it_and_memo_has_brave(self):
        resp, body = self.create_group(name="Офис", proto=["vless-reality", "amneziawg"], client__android="amneziawg",
                                       client__windows="v2rayn", mode="mixed", users_new="Иван; ; android, windows")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        g = groups.Groups.load().get("g1")
        self.assertEqual((g.mode("android"), g.mode("windows")), ("self", "admin"))
        _, page = self.c.get("/users/ivan")
        self.assertIn("Установите «AmneziaWG»", msg(page, "android"), "личный телефон — ставит сам")
        self.assertTrue(msg(page, "windows").startswith("Иван, VPN уже установлен."), "рабочий компьютер — ИТ")
        _, page = self.c.get("/groups/g1")
        memo = text_of(re.search(r"<summary>Как установить \(для ИТ\)</summary>(.*?)</details>", page, re.S).group(1))
        self.assertIn("Windows — «v2rayN»: скачайте архив «.zip» с «windows-64»", memo, "тот же файл, что у людей")
        self.assertIn("Brave — Windows: с brave.com", memo, "Brave людям при «Ставит ИТ» никто бы не поставил")
        self.assertIn("Через VPN пойдут и Telegram (Windows): поставьте их тоже", memo)
        self.assertNotIn("Android", memo, "телефоны люди ставят сами")
        self.assertIn("Ставит: телефоны — сами, компьютеры — ИТ", text_of(self.c.get("/connect/done?group=g1&u=ivan")[1]))

    def test_list_change_gets_short_message(self):
        resp, body = self.create_group(name="Офис", proto=["amneziawg"], client__android="amneziawg",
                                       users_new="Анна; ; android")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        base = allowlist.Allowlist.load()
        self.post("/apps", {"action": ["save"], "android": base.android + ["com.whatsapp"], "windows": base.windows})
        self.assertEqual(users.list_users().get("anna").resend, ["apps:android"])
        _, page = self.c.get("/users/anna")
        text = msg(page, "android")
        self.assertTrue(text.startswith("Анна, изменился список приложений, которые идут через VPN."), text[:120])
        self.assertIn("В «AmneziaWG» удалите старый туннель и импортируйте вложенный файл anna-", text)
        for gone in ("Установите", "Скопируйте"):
            self.assertNotIn(gone, text, "без шагов установки и импорта ключей — иначе подключения задвоятся")

    def test_excel_preview_skips_header_and_flags_empty_device(self):
        resp, body = self.wiz(3, go="create", name="Офис", proto=["vless-reality", "amneziawg"], client__android="amneziawg",
                              client__windows="v2rayn", existing=[], allow_mode="common",
                              users_new="Имя\tОтдел\tТелефон\tКомпьютер\nЕгор Ким\tИТ\tAndroid\tWindows\nОля Бо\tсклад\t\t")
        self.assertEqual(resp.status, 200)
        text = text_of(body)
        self.assertIn("Строка «Имя | Отдел | Телефон | Компьютер» — заголовок таблицы, пропущена", text)
        self.assertIn("Будет создано: 2", text)
        self.assertIn("устройство не указано — будут устройства группы", text)
        self.assertIn("Android, Windows", text)

    def test_quick_start_is_the_main_app(self):
        resp, body = self.create_group(name="Офис", proto=["vless-reality", "amneziawg"], client__android=["amneziawg", "happ"],
                                       users_new="Анна; ; android")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        _, page = self.c.get("/users/anna")
        quick = text_of(re.search(r'<div class="quick">(.*?)</div></div>', page, re.S).group(1))
        self.assertIn("Android: AmneziaWG", quick, "основное приложение из сообщения, а не запасной Happ")


class UnitTest(unittest.TestCase):
    def test_telegram_twice_is_one_name(self):
        names = clientviews.via_vpn_names(None, "android", ["org.telegram.messenger", "org.telegram.messenger.web"])
        self.assertEqual(names, ["Telegram"], "«Telegram и Telegram (APK с telegram.org)» — одно приложение")

    def test_mac_install_line_has_a_full_stop(self):
        cat = clients.load()
        links = clientviews.synth_links(["vless-reality"])
        p = clientviews.build_pack(cat, {"checked": None, "versions": {}}, "macos", links, [], {"macos": ["v2rayn"]},
                                   ["vless-reality"], True, None, None)
        self.assertIn("для Mac на Intel. macOS не открывает v2rayN", p.message)

    def test_it_memo_takes_happ_setup(self):
        cat = clients.load()
        c = cat.client("happ")
        line = distviews._how({"client": c, "platform": "android", "store": False, "links": c["platforms"]["android"]},
                              cat, "Brave, Telegram")
        self.assertIn("Настройте: Приложения через VPN в «Happ»", line)
        self.assertIn("отметьте Brave, Telegram", line)
        self.assertIn("Inbounds", line)

    def test_speed_check_is_in_brave(self):
        cat = clients.load()
        links = clientviews.synth_links(["vless-reality"])
        p = clientviews.build_pack(cat, {"checked": None, "versions": {}}, "windows", links, [], {"windows": ["v2rayn"]},
                                   ["vless-reality"], True, None, None)
        self.assertIn("speedtest.net в Brave при включённом VPN и в обычном браузере", p.message)

    def test_switch_to_udp_only_app_adds_tcp_in_one_go(self):
        cat = clients.load()
        text, fix = support._switch(cat, "windows", "Windows", ["hysteria2"], ["hysteria2", "vless-reality"],
                                    lambda p: False)
        self.assertIn("смените Windows в группе на «v2rayN» и добавьте группе VLESS Vision — одна правка и одна пересылка",
                      text)
        self.assertEqual((fix, fix.add, fix.switch), ("windows", ("vless-reality",), ("v2rayn",)))

    def test_dead_protocol_is_not_offered_as_backup(self):
        awg = support.App("Android", "AmneziaWG", [("amneziawg", "ok")], "apps", plat="android")
        happ = support.App("Android", "Happ", [("vless-reality", "ok")], "apps", plat="android")
        tips = support.transport_tips([awg, happ], ["vless-reality", "amneziawg"], [], clients.load(),
                                      lambda p: p == "amneziawg")
        self.assertFalse(any("UDP (AmneziaWG)" in t for _, t, _ in tips), tips)

    def test_overview_names_who_should_turn_on_the_backup(self):
        g = groups.Group("g1", "Офис", protocols=["amneziawg", "vless-reality"],
                         clients={"android": ["amneziawg", "happ"], "windows": ["v2rayn"]})
        reg = users.Registry(Path("users.json"), [users.User("anna", group="g1", devices=["android"],
                                                             protocols=["amneziawg", "vless-reality"]),
                                                  users.User("boris", group="g1", devices=["windows"],
                                                             protocols=["amneziawg", "vless-reality"])])
        with mock.patch.object(users, "selectable_protocols", return_value=["amneziawg", "vless-reality"]), \
                mock.patch.object(users, "variant_modules", return_value={}):
            fbs = support.fallbacks([{"id": "amneziawg", "enabled": True, "ok": False}], reg,
                                    groups.Groups(Path("groups.json"), [g]))
        self.assertEqual([(f.group, f.device, f.app, [u.name for u in f.people]) for f in fbs],
                         [("Офис", "Android", "Happ", ["anna"])], "у Бориса v2rayN — сбой AmneziaWG его не касается")

    def test_view_kinds(self):
        old = {"android": (("happ", ("hysteria2",)),), "windows": (("amneziavpn", ("amneziawg",)),)}
        new = {"android": (("happ", ("hysteria2", "vless-reality")),), "windows": (("v2rayn", ("vless-reality",)),),
               "ios": (("incy", ("vless-reality",)),)}
        self.assertEqual(resend.view_kinds(old, new), ["add:android:vless-reality", "drop:windows:amneziavpn",
                                                       "app:windows:v2rayn", "new:ios"])
        self.assertTrue(users.RESEND_RE.match("lost:android") and users.RESEND_RE.match("keys:vless-reality"))
        self.assertEqual(users.clean_resend(["keys", "lost:android", "apps:windows"]), ["keys", "lost:android"])


if __name__ == "__main__":
    unittest.main()
