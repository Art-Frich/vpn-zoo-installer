import json
import re
import time
import unittest
from unittest import mock

from tests.helpers import needs_bash
from tests.test_live import seed_live
from tests.test_web import AppTestBase, Client, header
from zoolib import allowlist, groups, paths, users
from zoolib.web import groupviews

PROTOS = ("vless-reality", "hysteria2", "amneziawg")


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@needs_bash
class GroupWebBase(AppTestBase):
    def setUp(self):
        super().setUp()
        for pid in PROTOS:
            self.env.add_protocol(pid)
        users.bootstrap()
        self.c.login()

    def wiz(self, step, go="next", **fields):
        """Форма мастера: скаляры — как есть, списки — несколько значений."""
        form = {"step": str(step), "go": go}
        multi = {"step": [str(step)], "go": [go]}
        for k, v in fields.items():
            k = k.replace("__", ":")
            if isinstance(v, (list, tuple)):
                multi[k] = list(v)
                if v:
                    form[k] = v[-1]
            else:
                form[k] = v
                multi[k] = [v]
        form["csrf"] = self.c.csrf
        return self.c.req("POST", "/connect/new", form, multi={**multi, "csrf": [self.c.csrf]})

    def create_group(self, **kw):
        base = dict(name="Семья", proto=["vless-reality", "amneziawg"], client__android="happ", users_new="masha\nkolya",
                    existing=[], allow_mode="common")
        base.update(kw)
        return self.wiz(3, go="create", **base)

    def post(self, path, multi):
        """POST как от браузера: form — последние значения, multi — все."""
        multi = {**multi, "csrf": [self.c.csrf]}
        return self.c.req("POST", path, {k: v[-1] for k, v in multi.items() if v}, multi=multi)

    def groups_json(self):
        return json.loads(paths.groups_file().read_text(encoding="utf-8"))["groups"]


class NavTest(GroupWebBase):
    def test_nav_and_users_page(self):
        resp, body = self.c.get("/users")
        self.assertIn('<a href="/groups">Группы</a>', body)
        self.assertRegex(body, r'<a href="/connect/new"[^>]*class="btn small primary"[^>]*>Новое подключение</a>')
        self.assertIn(">группа<", body)
        self.assertIn('<select name="group" id="group"', body)
        self.assertRegex(body, r'<option value="main" selected data-protos="[^"]*">Основная</option>')

    def test_anonymous_redirected(self):
        anon = Client(self.app)
        for path in ("/connect/new", "/connect/done", "/groups", "/groups/main"):
            resp, _ = anon.get(path)
            self.assertEqual(resp.status, 303, path)
            self.assertTrue(header(resp, "Location")[0].startswith("/login"), path)
        for path in ("/connect/new", "/groups/main", "/groups/main/move", "/groups/main/delete", "/groups/main/members"):
            resp, _ = anon.post(path, csrf=False)
            self.assertEqual(resp.status, 401, path)

    def test_post_needs_csrf(self):
        self.c.get("/groups")
        for path in ("/connect/new", "/groups/main", "/groups/main/move", "/groups/main/delete", "/groups/main/members"):
            resp, _ = self.c.req("POST", path, {"csrf": "wrong"})
            self.assertEqual(resp.status, 403, path)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])

    def test_foreign_origin_rejected(self):
        self.c.get("/groups")
        resp, _ = self.c.post("/groups/main", {"name": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)

    def test_bad_group_ids(self):
        for path in ("/groups/nope", "/groups/Main", "/groups/..%2fusers", "/groups/a%0d%0ab"):
            resp, _ = self.c.get(path)
            self.assertEqual(resp.status, 404, path)


class WizardTest(GroupWebBase):
    def test_wizard_and_overview_show_the_same_numbers_and_age(self):
        now = time.time()
        seed_live("hysteria2", now, rtt=31.4, mbps=52.0, age=900)
        seed_live("hysteria2", now, rtt=29.6, mbps=None, age=120)
        _, wizard = self.c.get("/connect/new")
        _, over = self.c.get("/")
        self.assertIn("с сервера: 30 мс · 52 Мбит/с · 2 мин назад", text_of(wizard))
        self.assertIn("30 мс · ±4 · 52 Мбит/с · 2 мин назад", re.sub(r"<[^>]+>", "", over))

    def test_step1_facts(self):
        now = time.time()
        seed_live("hysteria2", now, rtt=31.0, mbps=52.0, age=120)
        seed_live("amneziawg", now, ok=0, verdict="SERVER_DOWN", rtt=None, mbps=None, age=60)
        resp, body = self.c.get("/connect/new")
        self.assertEqual(resp.status, 200)
        self.assertIn("data-expanded", body, "живое обновление не должно сбрасывать мастер")
        self.assertIn('name="name" id="name" value="Группа 2"', body)
        for pid in PROTOS:
            self.assertRegex(body, rf'name="proto" value="{pid}"')
        self.assertIn("31 мс", body)
        self.assertIn("52 Мбит/с", body)
        self.assertIn("с сервера: сбой (SERVER_DOWN)", body)
        self.assertIn("с сервера: нет замера", body)
        self.assertIn("UDP", body)
        self.assertIn("TCP", body)
        # лежащий на сервере протокол не предвыбирается; остальные — да
        self.assertNotRegex(body, r'name="proto" value="amneziawg" checked')
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertIn("основной", body)
        self.assertIn("Подсказки без цифр — ориентир", body)
        self.assertIn('class="stepper"', body)

    def test_step1_client_probe_ranking(self):
        facts = {"hysteria2": {"n": 10, "ok": 9, "top": 2, "score": 160.0, "ctx": 2},
                 "vless-reality": {"n": 2, "ok": 1, "top": 0, "score": 0.0, "ctx": 0}}
        with mock.patch.object(groupviews, "_rank_facts", return_value=facts):
            _, body = self.c.get("/connect/new")
        self.assertIn("у клиентов: 9 из 10", body)
        self.assertIn("у клиентов: 1 из 2 · мало данных", body)
        # топ по пробам — предвыбран первым
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertLess(body.index('value="hysteria2"'), body.index('value="vless-reality"'))

    def test_step1_validation(self):
        for fields, msg in (({"name": "Основная", "proto": ["amneziawg"]}, "уже есть"),
                            ({"name": "Новая", "proto": []}, "хотя бы один протокол"),
                            ({"name": "Новая", "proto": ["nope"]}, "не включён"),
                            ({"name": "", "proto": ["amneziawg"]}, "название"),
                            ({"name": "x" * 41, "proto": ["amneziawg"]}, "длиннее")):
            resp, body = self.wiz(1, **fields)
            self.assertEqual(resp.status, 422, fields)
            self.assertIn(msg, body)
            self.assertIn("1. Протоколы", body)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])

    def test_step2_clients_per_platform(self):
        resp, body = self.wiz(1, name="Семья", proto=["vless-reality", "hysteria2"])
        self.assertEqual(resp.status, 200)
        self.assertIn("2. Клиенты", body)
        # состояние шага 1 — скрытыми полями
        self.assertIn('type="hidden" name="name" value="Семья"', body)
        self.assertRegex(body, r'type="hidden" name="proto" value="vless-reality"')
        self.assertRegex(body, r'type="checkbox" name="client:android" value="happ" checked')
        self.assertNotIn('type="radio" name="client:android"', body, "набор клиентов — галочки, не выбор одного")
        self.assertIn("рекомендуем", body)
        self.assertRegex(body, r'name="client:windows" value="v2rayn" checked')
        self.assertRegex(body, r'name="client:android" value="singbox"', "тянет только Hysteria2")
        self.assertIn("Набор: Happ — покрывает 2 из 2", text_of(body))
        self.assertIn("Платформа, где ничего не отмечено, не нужна", body)
        # карточка клиента — чипы протоколов, которые он умеет (из выбранных), а не «N из M»
        card = body[body.index('value="singbox"'):]
        card = card[:card.index("</label>")]
        self.assertIn(">Протокол hysteria2<", card)
        self.assertNotIn("Протокол vless-reality", card, "sing-box REALITY не умеет")
        # лишние клиенты — под «Другие клиенты», набор — сверху
        self.assertRegex(body, r"Другие клиенты \(\d+\)")
        self.assertLess(body.index('value="happ"'), body.index("Другие клиенты"))
        self.assertGreater(body.index('value="singbox"'), body.index("Другие клиенты"))
        self.assertIn("data-covers=", body)
        _, only = self.wiz(1, name="Семья", proto=["vless-reality"])
        self.assertNotRegex(only, r'name="client:android" value="singbox"', "sing-box-клиенты REALITY не проходят")
        self.assertIn("github.com/Happ-proxy", body)
        self.assertIn("Другие платформы", body)
        self.assertNotIn("style=", body)

    def test_clients_follow_protocol_change(self):
        # вернулись на шаг 1 и сменили протоколы: клиент, не умеющий новые, заменяется
        resp, body = self.wiz(1, name="Семья", proto=["amneziawg"], client__android="happ")
        self.assertRegex(body, r'name="client:android" value="amneziawg" checked')
        self.assertNotRegex(body, r'name="client:android" value="happ"')

    def checked(self, body, plat):
        """Отмеченные клиенты платформы на шаге 2, по порядку на странице."""
        return re.findall(rf'<input type="checkbox" name="client:{plat}" value="([^"]+)" checked', body)

    def test_step2_preselects_set_that_covers_all_protocols(self):
        resp, body = self.wiz(1, name="Семья", proto=["hysteria2", "vless-reality", "amneziawg"])
        self.assertEqual(self.checked(body, "android"), ["happ", "amneziawg"], "Happ — VLESS и Hysteria2, AmneziaWG — AWG")
        self.assertIn("Набор: Happ + AmneziaWG — покрывает 3 из 3", text_of(body))
        ios = self.checked(body, "ios")
        self.assertIn("incy", ios)
        self.assertNotIn("happ", ios, "Happ нет в российском App Store")
        self.assertEqual(self.checked(body, "windows"), ["v2rayn", "amneziavpn"])
        # платформа, которую набор целиком не закрыл, — предупреждением
        self.assertIn("Набор: hysteria (консоль) + AmneziaVPN — покрывает 2 из 3: для Протокол vless-reality нет клиента",
                      text_of(body))

    def test_step2_keeps_manual_set_and_skipped_platform(self):
        # вернулись с шага 3: у Android отмечен один Happ (2 из 3), у iPhone ничего — платформа не нужна
        resp, body = self.wiz(3, go="back", name="Семья", proto=["hysteria2", "vless-reality", "amneziawg"],
                              clients_for="hysteria2,vless-reality,amneziawg", client__android=["happ"],
                              client__windows=["v2rayn", "amneziavpn"], users_new="masha")
        self.assertIn("2. Клиенты", body)
        self.assertEqual(self.checked(body, "android"), ["happ"], "ничего не дозаполняется")
        self.assertEqual(self.checked(body, "ios"), [])
        text = text_of(body)
        self.assertRegex(text, r"Набор: Happ — покрывает 2 из 3: для Протокол amneziawg нет клиента")
        self.assertIn("Платформа не нужна: ничего не отмечено", text)
        self.assertIn("Набор: v2rayN + AmneziaVPN — покрывает 3 из 3", text)
        # смена протоколов на шаге 1 — набор пересчитывается под них
        resp, body = self.wiz(1, name="Семья", proto=["hysteria2", "amneziawg"], clients_for="hysteria2,vless-reality,amneziawg",
                              client__android=["happ"])
        self.assertEqual(self.checked(body, "android"), ["happ", "amneziawg"])
        self.assertEqual(self.checked(body, "ios"), ["singbox", "amneziavpn"])

    def test_set_is_saved_and_handed_off_as_sections(self):
        resp, body = self.create_group(proto=["hysteria2", "vless-reality", "amneziawg"],
                                       client__android=["happ", "amneziawg"], users_new="masha")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual(g["clients"], {"android": ["happ", "amneziawg"]})
        _, page = self.c.get("/users/masha")
        msg = re.search(r'<textarea id="msg-android"[^>]*>(.*?)</textarea>', page, re.S).group(1)
        self.assertIn("1) Happ — ", msg)
        self.assertIn("2) AmneziaWG — ", msg)
        self.assertLess(msg.index("Скачайте «Happ»"), msg.index("Скачайте «AmneziaWG»"))
        self.assertIn("Android · 2 приложения", text_of(page))
        self.assertRegex(text_of(page), r"1\) Happ — .*2\) AmneziaWG — .*amneziawg")
        self.assertNotIn('data-copy="msg-windows"', page, "для Windows клиенты не выбраны")
        _, done = self.c.get("/connect/done?group=g1&u=masha")
        self.assertIn("2) AmneziaWG", done)
        # страница группы: набор виден галочками, снять все — платформа не нужна
        _, gp = self.c.get("/groups/g1")
        self.assertEqual(self.checked(gp, "android"), ["happ", "amneziawg"])
        self.assertIn("Android: Happ + AmneziaWG", text_of(self.c.get("/groups")[1]))
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["hysteria2", "vless-reality", "amneziawg"]})
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0]["clients"], {})

    def test_step2_validation(self):
        for client, msg in (("happ", "не поддерживает выбранные протоколы"), ("ghost", "нет в каталоге")):
            resp, body = self.wiz(2, name="Семья", proto=["amneziawg"], client__android=client)
            self.assertEqual(resp.status, 422, client)
            self.assertIn(msg, body)
        resp, body = self.wiz(3, name="Семья", proto=["amneziawg"], client__android="happ", users_new="masha")
        self.assertEqual(resp.status, 422)
        self.assertIn("не поддерживает выбранные протоколы", body)
        resp, body = self.wiz(3, name="Семья", proto=["amneziawg"], client__ios8="happ", users_new="masha")
        self.assertEqual(resp.status, 422)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])

    def test_step3_and_back_keeps_state(self):
        resp, body = self.wiz(2, name="Семья", proto=["vless-reality"], client__android="happ")
        self.assertIn("3. Люди", body)
        self.assertIn('name="users_new"', body)
        self.assertIn('name="existing" value="owner"', body, "уже заведённые — можно добавить")
        self.assertIn('name="allow_mode" value="common" checked', body)
        self.assertIn('name="android" value="com.brave.browser"', body)
        self.assertIn("Создать подключение", body)
        self.assertRegex(body, r'type="hidden" name="client:android" value="happ"')
        # назад на шаг 2: введённое на шаге 3 не пропадает
        resp, body = self.wiz(3, go="back", name="Семья", proto=["vless-reality"], client__android="happ",
                              users_new="masha сестра", existing=["owner"], allow_mode="common")
        self.assertIn("2. Клиенты", body)
        self.assertIn('type="hidden" name="users_new" value="masha сестра"', body)
        self.assertIn('type="hidden" name="existing" value="owner"', body)

    def test_create_flow(self):
        resp, body = self.create_group(users_new="masha сестра\nkolya", existing=["owner"])
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        loc = header(resp, "Location")[0]
        self.assertEqual(loc, "/connect/done?group=g1&u=masha%2Ckolya%2Cowner")
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual((g["name"], g["protocols"], g["clients"], g["allowlist"]),
                         ("Семья", ["vless-reality", "amneziawg"], {"android": ["happ"]}, None))
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        for n in ("masha", "kolya", "owner"):
            self.assertEqual((reg[n]["group"], sorted(reg[n]["protocols"])), ("g1", ["amneziawg", "vless-reality"]), n)
        self.assertNotIn("masha", self.env.proto_users("hysteria2"))
        self.assertEqual(reg["masha"]["note"], "сестра")
        # раздача: пакет на каждого, ссылки на страницы
        resp, body = self.c.get(loc)
        self.assertEqual(resp.status, 200)
        self.assertIn("«Семья» создано", text_of(body).replace("«Семья»: подключение создано", "«Семья» создано"))
        for n in ("masha", "kolya", "owner"):
            self.assertIn(f"{n}: что отправить", body)
            self.assertIn(f'href="/users/{n}"', body)
            self.assertIn(f'data-copy="msg-{n}-android"', body)
        self.assertIn("не отправляйте через MAX и VK", body)
        self.assertNotIn("<svg", body)
        # id полей не повторяются между пакетами
        ids = re.findall(r'<textarea id="([^"]+)"', body)
        self.assertEqual(len(ids), len(set(ids)))
        # выбранный в группе клиент AmneziaWG на Android — WG Tunnel
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"], "client:android": ["wgtunnel"]})
        resp, body = self.c.get("/users/masha")
        self.assertIn("WG Tunnel", body)

    def test_create_with_own_allowlist(self):
        resp, body = self.create_group(allow_mode="own", android=["com.whatsapp", "com.example.app"],
                                       windows=["Discord.exe"], users_new="masha")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        self.assertEqual(self.groups_json()[1]["allowlist"],
                         {"android": ["com.whatsapp", "com.example.app"], "windows": ["Discord.exe"]})
        self.assertEqual(allowlist.Allowlist.load().effective("android", "masha"), ["com.whatsapp", "com.example.app"])
        resp, body = self.create_group(name="Друзья", allow_mode="own", android=["com.whatsapp"], windows=[], users_new="petya")
        self.assertEqual(resp.status, 422)
        self.assertIn("Windows", body)
        self.assertIn("com.whatsapp", body, "выбранное остаётся в форме")
        resp, body = self.create_group(name="Друзья", allow_mode="own", android=["bad id"], windows=["a.exe"], users_new="petya")
        self.assertEqual(resp.status, 422)
        self.assertNotIn("petya", self.env.proto_users("amneziawg"))

    def test_create_validation_changes_nothing(self):
        self.c.get("/connect/new")
        before = (paths.groups_file().read_text(encoding="utf-8"), paths.users_file().read_text(encoding="utf-8"))
        for fields, msg in (({"users_new": ""}, "хотя бы одного"),
                            ({"users_new": "bad!"}, "недопустимое имя"),
                            ({"users_new": "owner"}, "уже есть"),
                            ({"users_new": "a\na"}, "повторяется"),
                            ({"users_new": "zoo-probe"}, "зарезервировано"),
                            ({"users_new": "", "existing": ["ghost"]}, "нет в реестре"),
                            ({"users_new": "\n".join(f"u{i}" for i in range(25))}, "не больше")):
            resp, body = self.create_group(**fields)
            self.assertEqual(resp.status, 422, fields)
            self.assertIn(msg, body)
            self.assertIn("3. Люди", body)
        self.assertEqual(before, (paths.groups_file().read_text(encoding="utf-8"),
                                  paths.users_file().read_text(encoding="utf-8")))

    def test_partial_failure_reports_and_still_redirects(self):
        self.env.fail("amneziawg:user_add")
        resp, body = self.create_group(users_new="masha")
        self.assertEqual(resp.status, 303)
        _, page = self.c.get(header(resp, "Location")[0])
        self.assertIn("с ошибками", page)
        self.assertIn("искусственная ошибка", page)
        self.assertEqual(self.groups_json()[1]["name"], "Семья")

    def test_injection_is_escaped(self):
        evil = '"><script>alert(1)</script>'
        evil_user = '"><img src=x onerror=alert(2)>'
        resp, body = self.wiz(2, name=evil, proto=["vless-reality"], client__android="happ")
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;script&gt;", body)
        resp, body = self.wiz(3, name=evil, proto=["vless-reality"], client__android="happ", users_new=evil_user)
        self.assertEqual(resp.status, 422)
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img", body)
        self.assertIn("&lt;img src=x", body)
        # группа с таким названием (допустимо) безопасна во всех местах
        resp, body = self.create_group(name=evil, users_new="masha")
        self.assertEqual(resp.status, 303)
        for path in ("/groups", "/groups/g1", header(resp, "Location")[0], "/users/masha", "/users"):
            _, page = self.c.get(path)
            self.assertNotIn("<script>alert", page, path)
            self.assertNotIn("<img src=x", page, path)
        loc = header(resp, "Location")[0]
        self.assertNotIn("\n", loc)
        self.assertNotIn("<", loc)

    def test_note_with_html_is_escaped_everywhere(self):
        resp, _ = self.create_group(users_new="masha <b>x</b>")
        self.assertEqual(resp.status, 303)
        _, page = self.c.get(header(resp, "Location")[0])
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)

    def test_no_protocols_enabled(self):
        for pid in PROTOS:
            (self.env.etc / "protocols.d" / f"{pid}.json").unlink()
        resp, body = self.c.get("/connect/new")
        self.assertEqual(resp.status, 200)
        self.assertIn("Нет включённых протоколов", body)


class GroupsPagesTest(GroupWebBase):
    def setUp(self):
        super().setUp()
        resp, _ = self.create_group(users_new="masha\nkolya")
        self.assertEqual(resp.status, 303)

    def test_list(self):
        resp, body = self.c.get("/groups")
        self.assertEqual(resp.status, 200)
        text = text_of(body)
        self.assertIn("Основная", text)
        self.assertIn("Семья", text)
        self.assertIn('href="/groups/g1"', body)
        self.assertIn('href="/users/masha"', body)
        self.assertIn("Android: Happ", text)
        self.assertIn("Новое подключение", body)
        self.assertIn("общий", text)

    def test_edit_group_applies_once_and_reports_qr(self):
        resp, body = self.c.get("/groups/g1")
        self.assertEqual(resp.status, 200)
        self.assertIn('name="name" id="name" value="Семья"', body)
        self.assertRegex(body, r'name="proto" value="vless-reality" checked')
        self.assertRegex(body, r'name="proto" value="amneziawg" checked')
        self.assertNotRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertRegex(body, r'name="client:android" value="happ" checked')
        self.assertIn('name="allow_mode" value="common" checked', body)
        calls = len(self.env.calls())
        resp, _ = self.post("/groups/g1", {"name": ["Родные"], "proto": ["hysteria2", "amneziawg"], "allow_mode": ["own"],
                                            "android": ["com.whatsapp"], "windows": ["Discord.exe"],
                                            "client:android": ["amneziawg"]})
        self.assertEqual(resp.status, 303, text_of(_)[:300])
        _, page = self.c.get("/groups/g1")
        self.assertIn("Новые QR/файлы нужны:", page)
        self.assertIn('href="/users/masha"', page)
        self.assertIn('href="/users/kolya"', page)
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual(sorted(reg["masha"]["protocols"]), ["amneziawg", "hysteria2"])
        self.assertNotIn("masha", self.env.proto_users("vless-reality"))
        self.assertEqual(self.groups_json()[1]["name"], "Родные")
        self.assertEqual(allowlist.Allowlist.load().effective("windows", "kolya"), ["Discord.exe"])
        self.assertGreater(len(self.env.calls()), calls)
        # без изменений — без новых QR
        resp, _ = self.post("/groups/g1", {"name": ["Родные"], "proto": ["hysteria2", "amneziawg"], "allow_mode": ["own"],
                                            "android": ["com.whatsapp"], "windows": ["Discord.exe"],
                                            "client:android": ["amneziawg"]})
        _, page = self.c.get("/groups/g1")
        self.assertNotIn("Новые QR/файлы нужны", page)

    def test_edit_errors_keep_form(self):
        for multi, msg in (({"name": ["Основная"], "proto": ["amneziawg"]}, "уже есть"),
                           ({"name": ["Х"], "proto": []}, "хотя бы один протокол"),
                           ({"name": ["Х"], "proto": ["nope"]}, "не включён"),
                           ({"name": ["Х"], "proto": ["amneziawg"], "allow_mode": ["own"], "android": ["a.b.c"],
                             "windows": []}, "Windows")):
            resp, body = self.post("/groups/g1", multi)
            self.assertEqual(resp.status, 422, multi)
            self.assertIn(msg, body)
        self.assertEqual(self.groups_json()[1]["name"], "Семья")

    def test_main_keeps_following_all_protocols(self):
        _, body = self.c.get("/groups/main")
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        self.post("/groups/main", {"name": ["Основная"], "proto": list(PROTOS)})
        self.assertEqual(self.groups_json()[0]["protocols"], ["*"])
        self.post("/groups/main", {"name": ["Основная"], "proto": ["vless-reality"]})
        self.assertEqual(self.groups_json()[0]["protocols"], ["vless-reality"])

    def test_move_between_groups_and_reset_custom(self):
        resp, _ = self.c.post("/groups/g1/move", {"user": "masha", "to": "main"})
        self.assertEqual(resp.status, 303)
        _, page = self.c.get("/groups/main")
        self.assertIn("Новые QR/файлы нужны:", page)
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["masha"]["group"], sorted(reg["masha"]["protocols"])), ("main", sorted(PROTOS)))
        self.assertIn('<option value="g1">Семья</option>', page)
        # свой набор: плашка и «Как у группы»
        regs = users.list_users()
        regs.require("masha").custom = True
        regs.save()
        _, page = self.c.get("/groups/main")
        self.assertIn("свой набор", page)
        self.assertIn("Как у группы", page)
        self.c.post("/groups/main/move", {"user": "masha", "to": "main"})
        self.assertNotIn("custom", {u["name"]: u for u in self.env.users_json()["users"]}["masha"])
        # ошибки — сообщением, ничего не ломают
        for form in ({"user": "ghost", "to": "main"}, {"user": "masha", "to": "нет"}, {"user": "zoo-probe", "to": "main"}):
            resp, _ = self.c.post("/groups/main/move", form)
            self.assertEqual(resp.status, 303)
            _, page = self.c.get("/groups/main")
            self.assertRegex(page, r'class="bad"')
        _, page = self.c.get("/users/masha")
        self.assertIn(">группа<", page)
        self.assertIn('href="/groups/main"', page)

    def test_delete_only_empty(self):
        _, page = self.c.get("/groups/g1")
        self.assertIn("Удалить можно только пустую группу", page)
        self.assertNotIn("/groups/g1/delete", page)
        resp, _ = self.c.post("/groups/g1/delete")
        self.assertEqual(header(resp, "Location"), ["/groups/g1"])
        _, page = self.c.get("/groups/g1")
        self.assertIn("masha", page)
        self.assertIn("сначала переведите", page)
        for n in ("masha", "kolya"):
            self.c.post("/groups/g1/move", {"user": n, "to": "main"})
        _, page = self.c.get("/groups/g1")
        self.assertIn('action="/groups/g1/delete"', page)
        self.assertIn("data-confirm", page)
        resp, _ = self.c.post("/groups/g1/delete")
        self.assertEqual(header(resp, "Location"), ["/groups"])
        _, page = self.c.get("/groups")
        self.assertIn("удалена", page)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])
        resp, _ = self.c.get("/groups/g1")
        self.assertEqual(resp.status, 404)

    def test_add_members(self):
        resp, _ = self.post("/groups/g1/members", {"users_new": ["petya друг"], "existing": ["owner"]})
        self.assertEqual(resp.status, 303)
        _, page = self.c.get("/groups/g1")
        self.assertIn("petya", page)
        self.assertIn("создано: petya", page)
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["petya"]["group"], reg["owner"]["group"]), ("g1", "g1"))
        self.assertEqual(sorted(reg["petya"]["protocols"]), ["amneziawg", "vless-reality"])
        resp, _ = self.c.post("/groups/g1/members", {"users_new": "bad!"})
        _, page = self.c.get("/groups/g1")
        self.assertIn("недопустимое имя", page)

    def test_users_page_adds_into_group(self):
        resp, _ = self.c.post("/users", {"name": "vasya", "group": "g1"})
        self.assertEqual(header(resp, "Location"), ["/users/vasya"])
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["vasya"]["group"], sorted(reg["vasya"]["protocols"])), ("g1", ["amneziawg", "vless-reality"]))
        resp, _ = self.c.post("/users", {"name": "lena", "group": "nope"})
        _, page = self.c.get("/users")
        self.assertIn("нет", page)
        self.assertNotIn("lena", {u["name"] for u in self.env.users_json()["users"]})

    def test_handoff_follows_group_client_and_primary_protocol(self):
        # группа выбрала Happ для Android и поставила VLESS первым: пакет — Happ, не рекомендуемый для AWG клиент
        _, body = self.c.get("/users/masha")
        msg = re.search(r'<textarea id="msg-android"[^>]*>(.*?)</textarea>', body, re.S).group(1)
        self.assertIn("Скачайте «Happ»", msg)
        self.assertNotIn("AmneziaWG", msg)
        self.assertIn("Android · <strong>Happ</strong>", body)
        # «Не нужен» для остальных платформ: у группы клиент выбран только для Android
        self.assertNotIn('data-copy="msg-windows"', body)
        # мастер: страница раздачи берёт те же клиент и порядок
        resp, done = self.c.get("/connect/done?group=g1&u=masha")
        self.assertIn('id="msg-masha-android"', done)
        self.assertIn("Скачайте «Happ»", done)

    def test_users_form_does_not_make_group_member_custom(self):
        # протоколы в форме отмечены как у группы — only не передаётся, пользователь не «свой»
        _, page = self.c.get("/users")
        self.assertRegex(page, r'<option value="g1" data-protos="vless-reality amneziawg">Семья</option>')
        self.assertRegex(page, r'<option value="main" selected data-protos="[^"]*">')
        resp, _ = self.post("/users", {"name": ["vasya"], "group": ["g1"], "proto": ["vless-reality", "amneziawg"]})
        self.assertEqual(header(resp, "Location"), ["/users/vasya"])
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["vasya"]["group"], sorted(reg["vasya"]["protocols"])), ("g1", ["amneziawg", "vless-reality"]))
        self.assertNotIn("custom", reg["vasya"])
        # владелец снял протокол — набор «свой»
        self.post("/users", {"name": ["lena"], "group": ["g1"], "proto": ["amneziawg"]})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["lena"]["protocols"], reg["lena"]["custom"]), (["amneziawg"], True))
        # «Основная» (все включённые): все отмечены — не «свой», меньше — «свой»
        self.post("/users", {"name": ["olga"], "group": ["main"], "proto": list(PROTOS)})
        self.post("/users", {"name": ["igor"], "group": ["main"], "proto": ["hysteria2"]})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertNotIn("custom", reg["olga"])
        self.assertTrue(reg["igor"]["custom"])
        # набор «Основной» при другой группе — форма без JS не перерисовала галочки: пользователь не «свой»
        self.post("/users", {"name": ["pasha"], "group": ["g1"], "proto": list(PROTOS)})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["pasha"]["group"], sorted(reg["pasha"]["protocols"])), ("g1", ["amneziawg", "vless-reality"]))
        self.assertNotIn("custom", reg["pasha"])
        # а набор, не совпадающий ни с группой, ни с группой по умолчанию, — отличие
        self.post("/users", {"name": ["nina"], "group": ["g1"], "proto": ["hysteria2", "vless-reality"]})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((sorted(reg["nina"]["protocols"]), reg["nina"]["custom"]), (["hysteria2", "vless-reality"], True))

    def test_wizard_crash_without_members_shows_error_and_allows_retry(self):
        with mock.patch.object(groups, "add_members", side_effect=RuntimeError("сломалось")):
            resp, body = self.create_group(name="Друзья", users_new="petya")
        self.assertEqual(resp.status, 422)
        self.assertIn("сломалось", body)
        self.assertIn("3. Люди", body)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main", "g1"], "пустая группа не осталась")
        resp, _ = self.create_group(name="Друзья", users_new="petya")
        self.assertEqual(resp.status, 303)

    def test_wizard_crash_midway_redirects_to_group_page(self):
        with mock.patch.object(groups, "move_many", side_effect=RuntimeError("упал перенос")):
            resp, _ = self.create_group(name="Друзья", users_new="petya", existing=["owner"])
        self.assertEqual(resp.status, 303)
        self.assertEqual(header(resp, "Location"), ["/groups/g2"])
        _, page = self.c.get("/groups/g2")
        self.assertIn("упал перенос", page)
        self.assertIn("petya", page)
        self.assertIn("с ошибками", page)

    def test_apps_pages_know_group_list(self):
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"], "allow_mode": ["own"],
                                 "android": ["com.whatsapp"], "windows": ["Discord.exe"]})
        _, page = self.c.get("/apps?user=masha")
        self.assertIn("как у группы", page)
        self.assertNotIn("как общий", page)
        _, page = self.c.get("/users/masha")
        self.assertIn("список приложений группы", page)
        # свой список поверх группы: отличия считаются от списка группы, не от общего
        allowlist.set_lists({"android": ["com.whatsapp", "org.telegram.messenger"], "windows": ["Discord.exe"]},
                            "masha", apply_now=False)
        _, page = self.c.get("/apps?user=masha")
        self.assertIn("свой (отличается: +1 −0)", page)
        self.assertIn("Вернуть как у группы", page)
        self.assertNotIn("Вернуть общий", page)
        self.assertIn("список группы? Его свой список будет удалён", page)
        _, page = self.c.get("/apps")
        self.assertIn("+1 −0", page)
        # сброс: сообщение про группу, не про общий
        ch = allowlist.reset("masha", apply_now=False)
        self.assertEqual(ch.message, "сброшен на список группы")
        # участник без списка у группы — по-старому
        _, page = self.c.get("/apps?user=owner")
        self.assertIn("как общий", page)

    def test_live_stamp_changes_with_groups(self):
        s1 = self.c.get("/api/stamp?page=/groups")[1]
        groups.create("Ещё", ["amneziawg"])
        time.sleep(0.01)
        s2 = self.c.get("/api/stamp?page=/groups")[1]
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(self.c.get("/api/stamp?page=/users")[1], "")


if __name__ == "__main__":
    unittest.main()
