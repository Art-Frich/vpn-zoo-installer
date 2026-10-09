"""Повторный обход админки (D59): Mac и Linux, «Основная» как рекомендованный вариант, список людей, раздача новым."""

import json
import unittest

from tests.test_groups import GroupsBase
from tests.test_groupviews import GroupWebBase, text_of
from zoolib import clients, groups, paths, userguide, users
from zoolib.web import logviews, views


class MainPresetTest(GroupsBase):
    def test_old_main_with_hiddify_gets_the_preset_once_and_members_are_marked(self):
        users.bootstrap()
        groups.ensure()
        users.add_user("masha")
        data = json.loads(paths.groups_file().read_text(encoding="utf-8"))
        data["groups"][0]["clients"] = {"android": ["hiddify"], "windows": ["hiddify"]}
        data.pop("main_preset")
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        main = groups.ensure().get("main")
        self.assertNotIn("hiddify", {a for ids in main.clients.values() for a in ids})
        self.assertEqual(main.clients["ios"], ["incy"])
        self.assertEqual(main.protocols, ["*"], "протоколы не трогаются: у людей уже есть ключи")
        self.assertEqual(self.registry()["masha"].get("resend"), ["all"], "новое сообщение — целиком")
        self.assertNotIn("resend", self.registry()["owner"])
        # один раз: дальше осознанный выбор админа не перетирается
        groups.update("main", clients={"android": ["hiddify"]})
        self.assertEqual(groups.ensure().get("main").clients, {"android": ["hiddify"]})

    def test_main_from_variants_is_left_alone(self):
        users.bootstrap()
        groups.ensure()
        data = json.loads(paths.groups_file().read_text(encoding="utf-8"))
        data["groups"][0]["clients"] = {"android": ["amneziawg"]}
        data.pop("main_preset")
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(groups.ensure().get("main").clients, {"android": ["amneziawg"]})

    def test_extra_device_only_for_those_who_have_it(self):
        users.bootstrap()
        g = groups.create("Офис", ["vless-reality", "amneziawg"], {"android": ["amneziawg"], "windows": ["v2rayn"]})
        groups.add_devices(g.id, groups.lacking_plans(g.app_devices, g.protocols, ["macos", "android"]))
        g = groups.Groups.load().get(g.id)
        self.assertEqual(g.clients["macos"], ["v2rayn"])
        self.assertEqual((g.devices, g.app_devices), (["android", "windows"], ["android", "windows", "macos"]),
                         "Mac — не устройство по умолчанию: остальным пересылать нечего")
        self.assertEqual(json.loads(paths.groups_file().read_text(encoding="utf-8"))["groups"][-1]["extra_devices"],
                         ["macos"])


class LinuxMacGuideTest(unittest.TestCase):
    def test_guide_has_mac_and_linux_from_the_catalog(self):
        if not userguide.GUIDE_FILE.is_file():
            self.skipTest("нет docs/")
        text = userguide.GUIDE_FILE.read_text(encoding="utf-8")
        mac = text[text.index("## Шаг 2. macOS"):text.index("## Шаг 2. Linux")]
        self.assertIn("Через VPN — только браузер Brave.", mac)
        self.assertIn('open -na "Brave Browser"', mac)
        self.assertNotIn("Шаг 2. macOS", text[text.index("<!-- /zoo docs: platforms -->"):], "раздел Mac — из каталога")
        self.assertIn("Один способ из двух, не оба", text)
        self.assertIn(clients.load().raw["both_guide"], text)


class PeopleWebTest(GroupWebBase):
    def test_preview_asks_about_unknown_words_and_offers_mac(self):
        resp, body = self.wiz(3, go="create", name="Офис", proto=["vless-reality", "amneziawg"], devs="1",
                              dev=["android", "windows"], set__android="amneziawg", set__windows="v2rayn",
                              users_new="Анна; айфон, планшет\nДима; macbook\nПетров, Иван", allow_mode="common")
        self.assertEqual(resp.status, 200)
        text = text_of(body)
        self.assertIn("не понял: планшет", text)
        self.assertIn("уберите запятую", text)
        self.assertIn("у группы нет приложений для macOS", text)
        self.assertRegex(body, r'<input type="checkbox" name="add_dev" value="macos" checked>')
        self.assertIn("Дать группе приложение для macOS: v2rayN", text)
        resp, _ = self.wiz(3, go="create", name="Офис", proto=["vless-reality", "amneziawg"], devs="1",
                           dev=["android", "windows"], set__android="amneziawg", set__windows="v2rayn",
                           users_new="Дима; macbook", allow_mode="common", confirm="1", add_dev=["macos"])
        self.assertEqual(resp.status, 303)
        g = [x for x in self.groups_json() if x["name"] == "Офис"][0]
        self.assertEqual((g["clients"]["macos"], g["extra_devices"]), (["v2rayn"], ["macos"]))
        _, card = self.c.get("/handoff?u=dima")
        self.assertIn("v2rayN", card)
        self.assertNotIn("Ключей нет", card)

    def test_card_says_which_device_lacks_apps_instead_of_no_protocols(self):
        self.wiz(3, go="create", name="Офис", proto=["vless-reality", "amneziawg"], devs="1", dev=["android"],
                 set__android="amneziawg", users_new="Дима; macbook", allow_mode="common", confirm="1")
        _, card = self.c.get("/handoff?u=dima")
        self.assertIn("Для macOS у группы нет приложений.", text_of(card))
        self.assertNotIn("нет протоколов", card)

    def test_new_people_in_existing_group_get_their_own_cards_link(self):
        self.create_group()
        resp, _ = self.post("/groups/g1/members", {"users_new": ["Сергей Иванов"], "confirm": ["1"]})
        self.assertEqual(resp.status, 303)
        _, page = self.c.get("/groups/g1")
        self.assertIn('href="/handoff?u=sergey-ivanov"', page, "раздать только новым")


class OverviewTest(GroupWebBase):
    def test_stranded_group_named_when_its_only_protocol_is_down(self):
        self.create_group(name="Бухгалтерия", proto=["hysteria2"], client__android="happ", users_new="masha\nkolya")
        st = {"protocols": [{"id": "hysteria2", "enabled": True, "ok": False},
                            {"id": "vless-reality", "enabled": True, "ok": True}]}
        kind, msg = views.stranded_alert(st)
        self.assertEqual(kind, "bad")
        self.assertIn("Бухгалтерия (2 чел.)", text_of(str(msg)))


class LogNotesTest(unittest.TestCase):
    def test_xui_remove_line_is_explained(self):
        from zoolib import logread
        lines = [logread.Line("INFO - Remove Inbound User vasy due to expiration or traffic limit", "1")]
        self.assertTrue(logviews._known_notes(lines))
        self.assertEqual(logviews._known_notes([logread.Line("ok", "2")]), [])


if __name__ == "__main__":
    unittest.main()
