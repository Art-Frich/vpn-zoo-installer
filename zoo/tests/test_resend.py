"""Ежедневные операции (D57): новые ключи, «Кому переслать», «Через VPN» по группам, добавление в существующую группу."""

import html
import io
import json
import re
import unittest
import zipfile

from tests.helpers import needs_bash
from tests.test_groupviews import GroupWebBase, text_of
from tests.test_web import header
from zoolib import allowlist, groups, paths, resend, users

IVAN, OLGA = "ivan-petrov", "olga"


@needs_bash
class ResendTest(GroupWebBase):
    def setUp(self):
        super().setUp()
        resp, body = self.create_group(name="Офис", client__android="amneziawg", client__windows="v2rayn",
                                       client__ios="incy", users_new="Иван Петров; ; android, windows\nОльга; ; iphone")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        self.gid = next(g["id"] for g in json.loads(paths.groups_file().read_text(encoding="utf-8"))["groups"]
                        if g["name"] == "Офис")

    def user(self, name):
        return users.list_users().require(name)

    def calls(self, start):
        return [c.split()[:2] for c in self.env.calls()[start:] if c.split()[1] in ("user_add", "user_del")]

    # ---------- новые ключи ----------

    def test_rekey_person_then_sent(self):
        _, page = self.c.get(f"/users/{IVAN}")
        self.assertRegex(page, r'data-confirm="Новые ключи для «Иван Петров»\? Старые ссылки, QR и файлы перестанут '
                               r'работать сразу[^"]*компьютер придётся настроить заново[^"]*"[^>]*>.*?>Новые ключи</button>')
        self.assertIn("Отключить «Иван Петров»?", page, "в подтверждениях — имя, а не логин")
        self.assertIn("«Включить» вернёт доступ с теми же ключами", page)
        start = len(self.env.calls())
        resp, _ = self.c.post(f"/users/{IVAN}/rekey")
        self.assertEqual(header(resp, "Location"), [f"/users/{IVAN}"])
        done = self.calls(start)
        protos = self.user(IVAN).protocols
        self.assertEqual(done, [[p, "user_del"] for p in protos] + [[p, "user_add"] for p in protos],
                         "сначала удаление везде, потом новые: у Xray один клиент на все протоколы")
        self.assertEqual(self.user(IVAN).resend, ["keys"])
        _, page = self.c.get(f"/users/{IVAN}")
        self.assertIn("новые ключи выданы, старые не работают", page)
        self.assertIn("Переслать: новые ключи — сообщение целиком", page)
        self.assertIn(f'href="/handoff?u={IVAN}"', page)
        self.assertIn("Смените «устройства» в «Профиле»", page, "новый телефон другой — куда идти")
        msg = html.unescape(re.search(r'<pre id="msg-android"[^>]*>(.*?)</pre>', page, re.S).group(1))
        self.assertTrue(msg.startswith("Иван Петров, VPN на Android: что сделать\nЭто новые ключи взамен старых. "
                                       "В «AmneziaWG», «v2rayN» сначала удалите старые подключения"), msg[:200])
        resp, _ = self.c.post("/handoff/export", {"u": IVAN, "fmt": "zip"})
        text = zipfile.ZipFile(io.BytesIO(resp.body)).read(f"{IVAN}/instruction.txt").decode("utf-8")
        self.assertIn("Это новые ключи взамен старых", text, "и в ZIP этого человека")
        resp, _ = self.c.post("/resend", {"names": IVAN, "back": f"/users/{IVAN}"})
        self.assertEqual(header(resp, "Location"), [f"/users/{IVAN}"])
        self.assertEqual(self.user(IVAN).resend, [])
        _, page = self.c.get(f"/users/{IVAN}")
        self.assertNotIn("Переслать:", page)
        self.assertNotIn("Это новые ключи взамен старых", html.unescape(page), "отправили — сообщение снова обычное")

    def test_rekey_keeps_disabled_and_repeats_after_failure(self):
        users.set_enabled(IVAN, False)
        self.env.fail("amneziawg:user_add")
        rep = users.rekey(IVAN)
        self.assertFalse(rep.ok)
        self.assertIn("новые не выданы в: amneziawg", rep.message)
        self.assertIn("amneziawg", self.user(IVAN).protocols, "повтор доведёт")
        self.assertNotIn(IVAN, self.env.proto_users("amneziawg"))
        self.env.fail()
        self.assertTrue(users.rekey(IVAN).ok)
        for pid in self.user(IVAN).protocols:
            self.assertEqual(self.env.proto_users(pid)[IVAN], "false", "отключённый остаётся отключённым")

    def test_owner_has_no_rekey(self):
        _, page = self.c.get("/users/owner")
        self.assertNotIn(">Новые ключи<", page)
        start = len(self.env.calls())
        self.c.post("/users/owner/rekey")
        self.assertEqual(self.calls(start), [])
        _, page = self.c.get("/users/owner")
        self.assertIn("у owner ключи не меняются", page)

    def test_bulk_rekey_asks_first(self):
        start = len(self.env.calls())
        resp, page = self.c.post("/users/bulk", {"action": "rekey"}, multi={"action": ["rekey"], "names": [IVAN, OLGA]})
        self.assertEqual(resp.status, 200)
        self.assertIn("Выдать новые ключи: 2?", page)
        self.assertEqual(self.calls(start), [], "до подтверждения ничего не меняется")
        resp, _ = self.c.post("/users/bulk", {"action": "rekey", "confirm": "1"},
                              multi={"action": ["rekey"], "confirm": ["1"], "names": [IVAN, OLGA]})
        self.assertEqual(resp.status, 303)
        self.assertEqual((self.user(IVAN).resend, self.user(OLGA).resend), (["keys"], ["keys"]))
        _, page = self.c.get("/users")
        self.assertIn('<a href="/resend">кому переслать</a>', page)
        self.assertIn("Кому переслать: 2", page)
        _, page = self.c.get("/resend")
        self.assertIn("<h3>новые ключи — сообщение целиком (старые подключения удалить)</h3>", page)
        self.assertIn(f'href="/handoff?u={IVAN}%2C{OLGA}"', page)
        _, page = self.c.get("/")
        self.assertIn("Кому переслать: 2 →", page)

    # ---------- список «через VPN» ----------

    def test_list_change_marks_only_where_it_acts(self):
        base = allowlist.Allowlist.load()
        self.c.post("/apps", {"action": "save"}, multi={"action": ["save"], "android": base.android + ["com.whatsapp"],
                                                        "windows": base.windows})
        self.assertEqual(self.user(IVAN).resend, ["apps:android"])
        self.assertEqual(self.user(OLGA).resend, [], "на iPhone список не действует — пересылать нечего")
        _, page = self.c.get("/resend")
        self.assertIn("<h3>Android · «AmneziaWG»: новый файл или QR (старый туннель в «AmneziaWG» удалить, новый файл "
                      "импортировать)</h3>", page)
        self.assertNotIn(f'value="{OLGA}"', page)
        self.c.post("/apps", {"action": "save"}, multi={"action": ["save"], "android": base.android + ["com.whatsapp"],
                                                        "windows": base.windows + ["Discord.exe"]})
        self.assertEqual(self.user(IVAN).resend, ["apps:android", "apps:windows"])
        _, page = self.c.get("/resend")
        self.assertIn("<h3>Windows · «v2rayN»: новый файл правил (в «v2rayN» старый набор правил удалить, новый "
                      "импортировать и сделать активным)</h3>", page)
        resp, _ = self.c.post("/resend", {}, multi={"names": [IVAN, IVAN]})
        self.assertEqual(header(resp, "Location"), ["/resend"])
        _, page = self.c.get("/resend")
        self.assertIn("Пересылать никому не нужно", page)

    def test_apps_tabs_are_groups(self):
        _, page = self.c.get("/apps")
        nav = re.search(r'<nav class="seg" aria-label="Чей список">(.*?)</nav>', page).group(1)
        self.assertIn(f'href="/apps?group={self.gid}"', nav)
        self.assertNotIn("?user=", nav, "люди — не вкладками: свой список — со страницы человека")
        _, page = self.c.get(f"/apps?group={self.gid}")
        self.assertIn("Список группы «Офис»", page)
        self.assertIn("как общий", page)
        where = text_of(re.search(r"<summary>Где список действует</summary>(.*?)</details>", page, re.S).group(1))
        self.assertIn("Android: AmneziaWG — список из нашего файла; после изменения — новый файл или QR", where)
        self.assertIn("Windows: v2rayN — список из файла правил; после изменения — новый файл правил", where)
        self.assertIn("iPhone: INCY — список не действует: через VPN всё, кроме российских сайтов", where)
        base = allowlist.Allowlist.load()
        resp, _ = self.c.post("/apps", {"action": "save", "group": self.gid},
                              multi={"action": ["save"], "group": [self.gid], "android": base.android,
                                     "windows": base.windows + ["Discord.exe"]})
        self.assertEqual(header(resp, "Location"), [f"/apps?group={self.gid}"])
        g = groups.Groups.load().get(self.gid)
        self.assertEqual(g.allowlist["windows"], base.windows + ["Discord.exe"])
        self.assertEqual(allowlist.Allowlist.load().effective("windows", IVAN), base.windows + ["Discord.exe"])
        self.assertEqual(self.user(IVAN).resend, ["apps:windows"])
        _, page = self.c.get(f"/apps?group={self.gid}")
        self.assertIn("Сохранено. Переслать: Иван Петров", page)
        self.assertIn("свой список группы", page)
        self.c.post("/apps", {"action": "reset", "group": self.gid})
        self.assertIsNone(groups.Groups.load().get(self.gid).allowlist)
        # совпал с общим — группа снова на общем, своего списка нет
        self.c.post("/apps", {"action": "save", "group": self.gid},
                    multi={"action": ["save"], "group": [self.gid], "android": base.android, "windows": base.windows})
        self.assertIsNone(groups.Groups.load().get(self.gid).allowlist)

    def test_person_tab_from_person_page(self):
        _, page = self.c.get(f"/apps?user={IVAN}")
        nav = re.search(r'<nav class="seg" aria-label="Чей список">(.*?)</nav>', page).group(1)
        self.assertRegex(nav, rf'<a href="/apps\?user={IVAN}" class="active">Иван Петров</a>')
        where = text_of(re.search(r"<summary>Где список действует</summary>(.*?)</details>", page, re.S).group(1))
        self.assertNotIn("iPhone", where, "только его устройства")

    # ---------- в существующую группу ----------

    def test_connect_asks_where_first(self):
        _, page = self.c.get("/connect/new")
        self.assertLess(page.index("В существующую группу"), page.index('class="stepper"'))
        self.assertRegex(page, rf'<option value="{self.gid}">Офис · 2 чел\.</option>')
        self.assertIn('<option value="" selected disabled>— выберите группу —</option>', page,
                      "группа заранее не выбрана: бухгалтер не уйдёт в самую большую")
        resp, _ = self.c.get(f"/connect/new?to={self.gid}")
        self.assertEqual(header(resp, "Location"), [f"/groups/{self.gid}?add=1#add"])
        _, page = self.c.get(f"/groups/{self.gid}?add=1")
        self.assertRegex(page, r'<details class="card more" open id="add">\s*<summary>＋ Добавить людей списком')
        resp, page = self.c.get("/connect/new?to=nope")
        self.assertEqual(resp.status, 200)
        self.assertIn('class="stepper"', page)

    def test_first_run_goes_straight_to_new_group(self):
        for n in (IVAN, OLGA):
            users.delete_user(n)
        _, page = self.c.get("/connect/new")
        self.assertNotIn("В существующую группу", page)


class ResendCoreTest(unittest.TestCase):
    def test_kinds_merge(self):
        self.assertEqual(users.clean_resend(["apps:android", "keys", "apps:windows", "keys"]), ["keys"])
        self.assertEqual(users.clean_resend(["apps:android", "junk", "apps:android"]), ["apps:android"])
        self.assertEqual(resend.list_changes({"a": {"android": ["X"]}}, {"a": {"android": ["x"], "windows": []}}), {})
