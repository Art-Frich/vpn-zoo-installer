import html
import json
import re
import time
import unittest
from unittest import mock

from tests.helpers import needs_bash
from tests.test_groups import no_hiddify_qr
from tests.test_live import seed_live
from tests.test_web import AppTestBase, Client, header
from zoolib import allowlist, clients, groups, paths, users
from zoolib.web import clientviews, groupviews

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

    def checked(self, body, plat):
        """Набор устройства, отмеченный радиокнопкой на шаге «Приложения»; нет строки или «не нужен» — пусто."""
        m = re.search(rf'<input type="radio" name="set:{plat}" value="([^"]+)" checked', body)
        return m.group(1).split("+") if m else []

    def heads(self, body):
        """Строки устройств без списка «сменить»: {заголовок: текст строки (приложения, чипы)}."""
        return {re.search(r'<strong class="dev-name">([^<]+)</strong>', r).group(1): text_of(re.search(r"<summary.*?</summary>", r, re.S).group(0)).strip()
                for r in re.findall(r'<div class="dev-row"><details.*?(?=<div class="dev-row">|<p class="dev-total"|<p class="muted">)', body, re.S)}

    def rows(self, body):
        """Строки устройств шага «Приложения»: {заголовок: текст строки}."""
        out = {}
        for r in re.findall(r'<div class="dev-row">.*?(?=<div class="dev-row">|<p class="dev-total"|<p class="muted">)', body, re.S):
            title = re.search(r'<strong class="dev-name">([^<]+)</strong>', r).group(1)
            out[title] = text_of(r)
        return out

    def step1(self):
        """Шаг «Протоколы»: со стартового экрана — «Свой набор»."""
        return self.wiz(0, go="custom")

    def create_group(self, **kw):
        base = dict(name="Семья", proto=["vless-reality", "amneziawg"], client__android="happ", users_new="masha\nkolya",
                    existing=[], allow_mode="common", confirm="1")
        base.update(kw)
        return self.wiz(3, go="create", **base)

    def post(self, path, multi):
        """POST как от браузера: form — последние значения, multi — все."""
        multi = {**multi, "csrf": [self.c.csrf]}
        return self.c.req("POST", path, {k: v[-1] for k, v in multi.items() if v}, multi=multi)

    def groups_json(self):
        return json.loads(paths.groups_file().read_text(encoding="utf-8"))["groups"]

    def msg(self, body, plat="android", user=""):
        """Текст сообщения в поле: на странице пользователя или в его блоке на шаге раздачи."""
        uid = f"{user}-" if user else ""
        return html.unescape(re.search(rf'<pre id="msg-{uid}{plat}"[^>]*>(.*?)</pre>', body, re.S).group(1))


class NavTest(GroupWebBase):
    def test_nav_and_users_page(self):
        resp, body = self.c.get("/users")
        self.assertIn('<a href="/groups">Группы</a>', body)
        self.assertNotIn("Новое подключение", body)
        self.assertNotIn("/connect/new", body, "мастер — с «Обзора» и «Групп», не со списка пользователей")
        self.assertIn("Проверить учётки", body)
        self.assertIn(">группа<", body)
        self.assertIn('<select name="group" id="group"', body)
        self.assertRegex(body, r'<option value="main" selected data-protos="[^"]*">Основная</option>')

    def test_anonymous_redirected(self):
        anon = Client(self.app)
        for path in ("/connect/new", "/connect/done", "/groups", "/groups/main"):
            resp, _ = anon.get(path)
            self.assertEqual(resp.status, 303, path)
            self.assertTrue(header(resp, "Location")[0].startswith("/login"), path)
        for path in ("/connect/new", "/groups/main", "/groups/main/move", "/groups/main/delete", "/groups/main/members",
                     "/groups/main/message"):
            resp, _ = anon.post(path, csrf=False)
            self.assertEqual(resp.status, 401, path)

    def test_post_needs_csrf(self):
        self.c.get("/groups")
        for path in ("/connect/new", "/groups/main", "/groups/main/move", "/groups/main/delete", "/groups/main/members",
                     "/groups/main/message"):
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
    def test_wizard_and_overview_show_latency_and_age_but_no_live_speed(self):
        now = time.time()
        seed_live("hysteria2", now, rtt=31.4, mbps=52.0, age=900)
        seed_live("hysteria2", now, rtt=29.6, mbps=None, age=120)
        _, wizard = self.step1()
        _, over = self.c.get("/")
        self.assertIn("работает · 30 мс", text_of(wizard))
        self.assertIn("30 мс · ±4 · 2 мин назад", re.sub(r"<[^>]+>", "", over))
        for page in (wizard, over):
            self.assertNotIn("Мбит/с", re.sub(r"<[^>]+>", "", page), "живая скорость занижена в десятки раз: её не показываем")
    def test_step1_facts(self):
        now = time.time()
        seed_live("hysteria2", now, rtt=31.0, mbps=52.0, age=120)
        seed_live("amneziawg", now, ok=0, verdict="SERVER_DOWN", rtt=None, mbps=None, age=60)
        resp, body = self.step1()
        self.assertEqual(resp.status, 200)
        self.assertIn("data-expanded", body, "живое обновление не должно сбрасывать мастер")
        self.assertNotIn('id="name"', body, "название группы спрашивается один раз — на шаге «Люди»")
        for pid in PROTOS:
            self.assertRegex(body, rf'name="proto" value="{pid}"')
        text = text_of(body)
        self.assertIn("работает · 31 мс", text)
        self.assertIn("не отвечает", text)
        self.assertIn("нет замера", text)
        for gone in ("с сервера", "Мбит/с", "джиттер", "у клиентов", "мало данных"):
            self.assertNotIn(gone, text)
        self.assertNotIn('class="chip">UDP<', body, "UDP/TCP — во фразе назначения, не отдельным чипом")
        for line in ("UDP · быстрый на плохих сетях", "TCP · похож на обычный HTTPS"):
            self.assertIn(line, text)
        for gone in ("основной", "Запасной"):
            self.assertNotIn(gone, text, "роль протокола решает группа, а не список протоколов")
        # лежащий на сервере протокол не предвыбирается; остальные — да
        self.assertNotRegex(body, r'name="proto" value="amneziawg" checked')
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        for gone in ("запасные", "Подсказки без цифр", "ориентир"):
            self.assertNotIn(gone, body)
        self.assertIn('class="stepper"', body)
        self.assertRegex(body, r'<li class="cur" aria-current="step"><span class="n">2</span>Протоколы</li>')
    def test_step1_client_probe_ranking(self):
        facts = {"hysteria2": {"n": 10, "ok": 9, "top": 2, "score": 160.0, "ctx": 2, "down": 65.4, "latency": 76.0},
                 "vless-reality": {"n": 2, "ok": 1, "top": 0, "score": 0.0, "ctx": 0, "down": 40.0, "latency": 90.0}}
        with mock.patch.object(groupviews, "_rank_facts", return_value=facts):
            _, body = self.step1()
        text = text_of(body)
        self.assertIn("у людей ≈65 Мбит/с · 76 мс", text)
        self.assertIn('title="медианы замеров с устройств за 30 дней, замеров: 10, удачных: 9"', body)
        self.assertNotIn("у людей ≈40", text, "меньше трёх замеров — не «у людей»: показывается замер с сервера")
        self.assertNotIn("мало данных", text)
        # топ по пробам — предвыбран первым
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertLess(body.index('value="hysteria2"'), body.index('value="vless-reality"'))
    def test_step1_validation(self):
        for fields, msg in (({"proto": []}, "хотя бы один протокол"), ({"proto": ["nope"]}, "не включён")):
            resp, body = self.wiz(1, name="Новая", **fields)
            self.assertEqual(resp.status, 422, fields)
            self.assertIn(msg, body)
            self.assertIn("<h3>Протоколы</h3>", body)
        # название группы проверяется там, где его спрашивают, — на шаге «Люди»
        for name, msg in (("Основная", "уже есть"), ("", "назовите группу"), ("x" * 41, "длиннее")):
            resp, body = self.create_group(name=name, users_new="masha")
            self.assertEqual(resp.status, 422, name)
            self.assertIn(msg, body)
            self.assertIn("<h3>Люди</h3>", body)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])
    def test_step2_one_row_per_device_and_no_global_lists(self):
        resp, body = self.wiz(1, name="Семья", proto=["vless-reality", "hysteria2"])
        self.assertEqual(resp.status, 200)
        self.assertIn("<h3>Приложения</h3>", body)
        self.assertNotIn("3. Приложения", body, "номера нет: он есть в степпере")
        # состояние шага 1 — скрытыми полями
        self.assertIn('type="hidden" name="name" value="Семья"', body)
        self.assertRegex(body, r'type="hidden" name="proto" value="vless-reality"')
        for gone in ("Другие клиенты", "Другие платформы", "рекомендуем", "data-covers", "data-unify", "data-sum",
                     "Одно приложение на всех"):
            self.assertNotIn(gone, body)
        self.assertNotRegex(body, r'name="client:')
        # чипы: основные устройства включены, остальные — нет
        for plat in ("android", "ios", "windows"):
            self.assertRegex(body, rf'<input type="checkbox" name="dev" value="{plat}" checked')
        for plat in ("macos", "linux"):
            self.assertRegex(body, rf'<input type="checkbox" name="dev" value="{plat}" data-auto>')
        rows = self.heads(body)
        self.assertEqual(list(rows), ["Android", "iPhone", "Windows"], "ровно одна строка на включённое устройство")
        self.assertEqual(rows["Android"], "Android Happ все протоколы сменить")
        self.assertEqual(self.checked(body, "android"), ["happ"])
        self.assertEqual(self.checked(body, "ios"), ["incy"], "людям, которые ставят сами, — приложение из App Store РФ, не Happ")
        self.assertEqual(self.checked(body, "windows"), ["v2rayn"])
        self.assertEqual(rows["iPhone"], "iPhone INCY VLESS (TCP) сменить",
                         "один чип покрытия сразу после приложения: что берёт, а не «нет …» (четвёртый обход)")
        self.assertNotIn("! ", re.sub(r"<[^>]+>", "", body), "восклицательных префиксов нет")
        self.assertNotIn("ставится только из App Store", body, "про iPhone «ставит ИТ» — только в режиме ИТ")
        # «Всего»: каждое приложение один раз, со своими устройствами, одним форматом
        total = text_of(re.search(r'<p class="dev-total">(.*?)</p>', body).group(1)).strip()
        self.assertEqual(total, "Всего 3 приложения: Happ — Android · INCY — iPhone · v2rayN — Windows")
        self.assertIn('name="go" value="refresh"', body)
        self.assertRegex(body, r'<button type="submit" name="go" value="refresh" class="btn small" data-refresh formnovalidate>Пересчитать</button>')
        self.assertNotIn("style=", body)
        # «Кто ставит» — один переключатель первым, чипы протоколов и «сменить протоколы»
        self.assertLess(body.index('class="seg"'), body.index('class="proto-sum"'))
        self.assertLess(body.index('class="proto-sum"'), body.index('class="dev-chips"'))
        self.assertIn("сменить протоколы", body)
        self.assertEqual(body.count("Кто ставит:"), 1)
    def test_step2_change_options_are_ready_sets_only(self):
        _, body = self.wiz(1, name="Семья", proto=["hysteria2", "vless-reality", "amneziawg"])
        opts = re.findall(r'name="set:ios" value="([^"]+)"', body)
        self.assertEqual(opts[0], "amneziawg+incy", "первым — подбор: приложения App Store РФ, без Happ ради одного протокола")
        self.assertEqual(opts[-1], "none", "последним — «Не нужен»")
        sets = opts[:-1]
        self.assertLessEqual(len(sets), 4)
        self.assertTrue(all(1 <= len(s.split("+")) <= 2 for s in sets), "наборы из одного-двух приложений")
        self.assertEqual(len(sets), len(set(sets)))
        self.assertIn("happ+amneziawg", sets, "полное покрытие — вариантом в «сменить»")
        text = text_of(body)
        self.assertIn("все протоколы", text)
        self.assertIn("UDP и TCP", text, "берёт не все — что берёт, а не «нет Hysteria2» (четвёртый обход)")
        self.assertEqual(self.checked(body, "android"), ["happ", "amneziawg"])
        self.assertEqual(self.checked(body, "windows"), ["v2rayn", "amneziavpn"])
        # метка про магазин — только у невыбранных вариантов, объяснение — один раз под строками
        row = self.rows(body)["iPhone"]
        self.assertIn("нет в App Store РФ", row)
        self.assertEqual(text.count("нет в App Store РФ: нужен Apple ID другой страны"), 1)
        self.assertNotIn("нет в App Store РФ", self.heads(body)["iPhone"], "у выбранного набора иностранного приложения нет")
    def test_step2_refresh_keeps_manual_choice_and_none_drops_device(self):
        base = dict(name="Семья", proto=["hysteria2", "vless-reality"], clients_for="hysteria2,vless-reality|self", devs="1",
                    dev=["android", "ios", "windows"])
        # ручной выбор iPhone: Happ вместо INCY; Windows — «не нужен»; Android — как предложено
        resp, body = self.wiz(2, go="refresh", **base, set__android="happ", set__ios="happ", set__windows="none")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.checked(body, "ios"), ["happ"], "выбор не затирается подбором")
        self.assertEqual(list(self.rows(body)), ["Android", "iPhone"], "«не нужен» — устройство выключено")
        self.assertIn("нет в App Store РФ", self.heads(body)["iPhone"], "иностранное приложение — чип в строке устройства")
        self.assertNotRegex(body, r'name="dev" value="windows" checked')
        # включили чип macOS: появилась строка с подбором, остальное на месте
        resp, body = self.wiz(2, go="refresh", **{**base, "dev": ["android", "ios", "macos"]},
                              set__android="happ", set__ios="happ")
        self.assertEqual(list(self.rows(body)), ["Android", "iPhone", "macOS"])
        self.assertTrue(self.checked(body, "macos"))
        self.assertEqual(self.checked(body, "ios"), ["happ"])
        # смена протоколов на шаге 2 → набор пересчитывается под них, ручной не остаётся
        resp, body = self.wiz(1, name="Семья", proto=["hysteria2", "amneziawg"], clients_for="hysteria2,vless-reality|self",
                              devs="1", dev=["android", "ios"], set__android="happ")
        self.assertEqual(self.checked(body, "android"), ["happ", "amneziawg"], "вместо ручного «Happ» — набор, покрывающий оба")
        # смена «кто ставит» на этом же шаге — тоже подбор заново: ИТ берёт и иностранное, и файлы
        resp, body = self.wiz(2, go="refresh", **{**base, "mode": "admin"}, set__android="happ", set__ios="incy")
        self.assertEqual(self.checked(body, "ios"), ["happ"], "ИТ: Happ покрывает оба протокола одним приложением")
        self.assertRegex(body, r'<input type="radio" name="mode" value="admin" checked data-auto>')
    def test_step2_back_from_people_keeps_state(self):
        resp, body = self.wiz(3, go="back", name="Семья", proto=["hysteria2", "vless-reality", "amneziawg"],
                              clients_for="hysteria2,amneziawg,vless-reality|self", devs="1", dev=["android", "windows"],
                              set__android="happ", set__windows="v2rayn+amneziavpn", users_new="masha")
        self.assertIn("<h3>Приложения</h3>", body)
        self.assertEqual(self.checked(body, "android"), ["happ"], "ничего не дозаполняется")
        self.assertEqual(self.checked(body, "windows"), ["v2rayn", "amneziavpn"])
        self.assertEqual(list(self.rows(body)), ["Android", "Windows"], "iPhone выключен чипом")
        self.assertIn("UDP и TCP", self.heads(body)["Android"], "что берёт, а не «нет AmneziaWG»")
        self.assertIn("все протоколы", self.heads(body)["Windows"])
        self.assertIn('type="hidden" name="users_new" value="masha"', body)
    def test_step2_one_app_line_and_old_three_app_set(self):
        _, body = self.wiz(1, name="Семья", proto=["hysteria2"])
        text = text_of(body)
        self.assertIn("Всего 2 приложения: Happ — Android, iPhone · v2rayN — Windows", text)
        self.assertNotIn("Одно приложение на всех", text)
        self.assertIn("Happ ", self.heads(body)["iPhone"])
        self.assertIn("нет в App Store РФ", self.heads(body)["iPhone"], "замены из РФ-магазина для Hysteria2 нет — чип")
        # старая группа с тремя приложениями на устройстве не ломается: набор показан первым, как есть
        _, body = self.wiz(2, go="refresh", name="Семья", proto=["hysteria2", "vless-reality", "amneziawg"],
                           clients_for="hysteria2,amneziawg,vless-reality|self", devs="1", dev=["android"],
                           set__android="happ+amneziawg+wgtunnel")
        self.assertEqual(self.checked(body, "android"), ["happ", "amneziawg", "wgtunnel"])
        self.assertEqual(re.findall(r'name="set:android" value="([^"]+)"', body)[0], "happ+amneziawg+wgtunnel")
    def test_step2_unverified_footnote_once_and_ios_note_only_for_it(self):
        _, body = self.wiz(1, name="Семья", proto=["vless-reality"])
        self.assertEqual(body.count("на устройстве не проверялись"), 1)

    def test_step2_shows_one_row_and_set_only_for_device_without_apps(self):
        _, body = self.wiz(1, name="Семья", proto=["vless-reality", "amneziawg"], devs="1", dev=["macos"],
                           clients_for="")
        self.assertEqual(self.checked(body, "macos"), ["amneziavpn", "v2rayn"], "на Mac VLESS — v2rayN с Brave (D59)")
        self.assertIn("все протоколы", self.heads(body)["macOS"])
    def test_set_is_saved_and_handed_off_as_sections(self):
        resp, body = self.create_group(proto=["hysteria2", "vless-reality", "amneziawg"],
                                       client__android=["happ", "amneziawg"], users_new="masha")
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual(g["clients"], {"android": ["happ", "amneziawg"]})
        self.assertNotIn("install_mode", g, "по умолчанию — люди ставят сами, поле не пишется")
        _, page = self.c.get("/users/masha")
        msg = self.msg(page)
        self.assertIn("1) Установите браузер «Brave»", msg)
        self.assertIn("2) Установите «Happ»", msg)
        self.assertIn("\n\nЕсли «Happ» не подключается — запасное приложение «AmneziaWG»:\n9) Установите «AmneziaWG»", msg)
        self.assertIn("В «Happ» нажмите «+» → «Вставить из буфера»", msg)
        self.assertIn("Включённым держите одно приложение — «Happ».", msg, "два приложения: какое включать")
        self.assertNotIn("Happ (", msg, "названий протоколов в инструкции нет")
        android = page[page.index('data-pp="android"'):page.index('id="msg-android"')]
        self.assertLess(android.index("<strong>Happ</strong>"), android.index("<strong>AmneziaWG</strong>"))
        self.assertEqual(android.count('class="app-head"'), 2, "каждое приложение набора — со своими ключами")
        self.assertRegex(android, r'<img class="qr" src="/users/masha/qr/\d+\?p=[0-9a-f]{8}"')
        self.assertNotIn('data-pp="windows"', page, "для Windows приложения не выбраны")
        _, done = self.c.get("/connect/done?group=g1&u=masha")
        self.assertIn("9) Установите «AmneziaWG»", done)
        # страница группы: набор виден радиокнопкой, выключить устройство — платформа не нужна
        _, gp = self.c.get("/groups/g1")
        self.assertEqual(self.checked(gp, "android"), ["happ", "amneziawg"])
        self.assertIn("Happ — Android · AmneziaWG — Android", text_of(self.c.get("/groups")[1]))
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["hysteria2", "vless-reality", "amneziawg"],
                                 "devs": ["1"], "set:android": ["none"]})
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0]["clients"], {})
        _, page = self.c.get("/users/masha")
        self.assertIn("Приложения не выбраны", text_of(page), "пустой набор — не фолбэк на каталог, а «Настроить»")
        self.assertIn('href="/groups/g1#settings"', page)
        self.assertNotIn('data-pp="', page)
    def test_set_field_posts_save_the_pair_and_none_or_unchecked_device_has_no_key(self):
        self.post("/connect/new", {"step": ["3"], "go": ["create"], "name": ["Офис"], "proto": ["hysteria2", "vless-reality"],
                                   "devs": ["1"], "dev": ["android", "ios", "windows"], "set:android": ["happ"],
                                   "set:ios": ["none"], "set:windows": ["v2rayn"], "users_new": ["masha"],
                                   "allow_mode": ["common"], "mode": ["admin"], "confirm": ["1"]})
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual(g["clients"], {"android": ["happ"], "windows": ["v2rayn"]})
        self.assertEqual(g["install_mode"], "admin")
        # снятый чип: набор в форме есть, но устройство выключено
        self.post("/groups/g1", {"name": ["Офис"], "proto": ["hysteria2", "vless-reality"], "devs": ["1"],
                                 "dev": ["android"], "set:android": ["happ"], "set:windows": ["v2rayn"], "mode": ["admin"]})
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0]["clients"], {"android": ["happ"]})
        # прежняя форма (client:<платформа>) принимается
        self.post("/groups/g1", {"name": ["Офис"], "proto": ["hysteria2", "vless-reality"], "client:android": ["happ"],
                                 "client:windows": ["v2rayn"]})
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0]["clients"],
                         {"android": ["happ"], "windows": ["v2rayn"]})
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0].get("install_mode", "self"), "self")

    def test_self_mode_is_honest_about_github_on_computers(self):
        cat = clients.load()
        self.assertEqual(groupviews._set_flag(cat, "windows", ["v2rayn"], "self"), ("не из магазина", ["v2rayN"]))
        self.assertEqual(groupviews._set_flag(cat, "linux", ["v2rayn"], "self"), ("не из магазина", ["v2rayN"]),
                         "одна метка на «не из магазина» и «в магазине нет»: смысл один")
        self.assertEqual(groupviews._flag_legend([("не из магазина", ["v2rayN"]), ("не из магазина", ["AmneziaVPN"])]),
                         ["v2rayN, AmneziaVPN — не из магазина: ставится файлом с GitHub: в инструкции сказано, какой"])
        self.assertIsNone(groupviews._set_flag(cat, "windows", ["v2rayn"], "admin"))
        self.assertIsNone(groupviews._set_flag(cat, "windows", ["hiddify"], "self"), "Hiddify есть в Microsoft Store")
        self.assertEqual(groupviews._set_flag(cat, "ios", ["amneziavpn"], "self"), ("нет в App Store РФ", ["AmneziaVPN"]))
        # iPhone «ставится файлом» не бывает: нет в App Store РФ — так и сказано
        broken = clients.load()
        broken.client("incy")["platforms"]["ios"] = [{"kind": "site", "url": "https://example.org/", "checked": True}]
        flag = groupviews._set_flag(broken, "ios", ["incy"], "self")
        self.assertEqual(flag, ("нет в App Store РФ", ["INCY"]))
        self.assertNotIn("файл", flag[0])
        _, body = self.c.get("/connect/new?mode=self")
        self.assertIn("Приложения — из магазинов (v2rayN — с GitHub), ключи и файлы — в сообщении каждому.", body)
        self.assertIn("Рабочие телефоны тоже настраивает ИТ — «Всё ставит ИТ»", body)
    def test_wizard_device_chips_add_and_remove_devices(self):
        _, body = self.wiz(1, name="Офис", proto=["hysteria2", "vless-reality"])
        self.assertEqual(body.count('name="devs" value="1"'), 1, "шаг 2 сам говорит, что чипы устройств в форме")
        sets = {p: "+".join(self.checked(body, p)) for p in ("android", "ios", "windows") if self.checked(body, p)}
        self.assertEqual(set(sets), {"android", "ios", "windows"})
        cf = re.search(r'name="clients_for" value="([^"]*)"', body).group(1)
        fields = dict(name="Офис", proto=["hysteria2", "vless-reality"], clients_for=cf, devs="1",
                      dev=["android", "windows", "macos"], **{f"set__{p}": v for p, v in sets.items()})
        _, body = self.wiz(2, go="refresh", **fields)
        self.assertTrue(self.checked(body, "macos"), "отмеченный чип macOS добавил строку")
        self.assertFalse(self.checked(body, "ios"), "снятый чип iPhone убрал строку")
        fields["set__macos"] = "+".join(self.checked(body, "macos"))
        _, body = self.wiz(2, go="next", **fields)
        self.assertNotIn('name="dev" value="ios"', body)
        self.assertIn('name="dev" value="macos"', body)

    def test_disabled_protocol_is_shown_and_can_be_dropped(self):
        self.post("/connect/new", {"step": ["3"], "go": ["create"], "name": ["Офис"],
                                   "proto": ["hysteria2", "vless-reality"], "users_new": ["masha"],
                                   "allow_mode": ["common"], "confirm": ["1"]})
        self.env.add_manifest("hysteria2", enabled=False)
        _, gp = self.c.get("/groups/g1")
        self.assertIn('name="drop" value="hysteria2"', gp)
        self.post("/groups/g1", {"name": ["Офис"], "proto": ["vless-reality"]})
        self.assertIn("hysteria2", [x for x in self.groups_json() if x["id"] == "g1"][0]["protocols"])
        self.post("/groups/g1", {"name": ["Офис"], "proto": ["vless-reality"], "drop": ["hysteria2"]})
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0]["protocols"], ["vless-reality"])
        _, gp = self.c.get("/groups/g1")
        self.assertNotIn('name="drop"', gp)

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
        resp, body = self.wiz(2, name="Семья", proto=["vless-reality"], set__android="happ")
        self.assertIn("<h3>Люди</h3>", body)
        self.assertIn("Кого подключаем", body)
        self.assertIn('name="users_new"', body)
        self.assertEqual(body.count('id="name"'), 1, "название группы — только здесь")
        self.assertIn("<label for=\"name\">Название группы</label>", body)
        self.assertNotIn('name="existing" value="owner"', body, "owner в «уже существующих» не предлагается")
        self.assertIn("Через VPN: общий список", body)
        self.assertIn('name="allow_mode" value="own"', body, "список «через VPN» — до раздачи, а не после (D59)")
        self.assertRegex(body, r'<details class="more"><summary>Через VPN — общий список или свой для группы</summary>')
        self.assertIn("Проверить список →", body)
        self.assertNotIn("Создать группу</button>", body)
        self.assertRegex(body, r'type="hidden" name="set:android" value="happ"')
        self.assertRegex(body, r'type="hidden" name="dev" value="android"')
        self.assertRegex(body, r'type="hidden" name="mode" value="self"')
        # назад на шаг «Приложения»: введённое на шаге «Люди» не пропадает
        resp, body = self.wiz(3, go="back", name="Семья", proto=["vless-reality"], set__android="happ",
                              users_new="masha; сестра", existing=["owner"], allow_mode="common")
        self.assertIn("<h3>Приложения</h3>", body)
        self.assertIn('type="hidden" name="users_new" value="masha; сестра"', body)
        self.assertIn('type="hidden" name="existing" value="owner"', body)
    def test_create_flow(self):
        resp, body = self.create_group(users_new="masha; сестра\nkolya", existing=["owner"])
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        loc = header(resp, "Location")[0]
        self.assertEqual(loc, "/connect/done?group=g1&u=masha%2Ckolya%2Cowner")
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual((g["name"], g["protocols"], g["clients"], g["allowlist"]),
                         ("Семья", ["amneziawg", "vless-reality"], {"android": ["happ"]}, None))
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        for n in ("masha", "kolya"):
            self.assertEqual((reg[n]["group"], sorted(reg[n]["protocols"])), ("g1", ["amneziawg", "vless-reality"]), n)
        # владельцу — всё включённое (как в sync_users), иначе sync пересоздал бы его учётки
        self.assertEqual((reg["owner"]["group"], sorted(reg["owner"]["protocols"])),
                         ("g1", ["amneziawg", "hysteria2", "vless-reality"]))
        self.assertNotIn("masha", self.env.proto_users("hysteria2"))
        self.assertEqual(reg["masha"]["note"], "сестра")
        # раздача: пакет на каждого, ссылки на страницы
        resp, body = self.c.get(loc)
        self.assertEqual(resp.status, 200)
        self.assertIn("«Семья» создано", text_of(body).replace("«Семья»: группа создана", "«Семья» создано"))
        # порядок: «готова» → «Раздать доступы» с главной кнопкой → «Инструкция» (свёрнута); без текста с «{name}»
        self.assertLess(body.index("Группа «Семья» готова: 3 чел."), body.index("Раздать доступы"))
        self.assertLess(body.index("Раздать доступы"), body.index("Карточки (печать, ZIP, CSV)"))
        self.assertLess(body.index("Карточки (печать, ZIP, CSV)"), body.index("<summary>Инструкция</summary>"))
        self.assertEqual(body.count("<summary>Инструкция</summary>"), 1)
        self.assertRegex(body, r'<summary>Инструкция</summary>.*<pre id="msg-g-android" class="msg-pre">masha, VPN на Android')
        self.assertNotIn("{имя}", body, "сырой шаблон не показывается: имя первого человека")
        self.assertIn('href="/groups/g1#text"', body)
        self.assertEqual(len(re.findall(r'<details name="conn-user" class="urow">', body)), 3)
        for n in ("masha", "kolya", "owner"):
            self.assertIn(f"<strong>{n}</strong>", body)
            self.assertIn(f'href="/users/{n}"', body)
            self.assertIn('<a href="/groups/g1#text"', body)
            self.assertIn(f'data-copy="msg-{n}-android"', body)
            self.assertIn(f'src="/users/{n}/qr/', body, "QR именно этого человека")
            self.assertTrue(self.msg(body, user=n).startswith(f"{n}, VPN на Android"))
        self.assertNotIn("{name}", body)
        self.assertNotIn("можно править", body)
        self.assertEqual(body.count("не отправляйте через MAX и VK"), 1, "предупреждение — одно, а не в каждом блоке")
        self.assertNotIn("<svg", body)
        # id полей не повторяются между блоками
        ids = re.findall(r'\sid="([^"]+)"', body)
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
        resp, body = self.create_group(name="Друзья", allow_mode="own", android=["bad id"], windows=["a.exe"], users_new="petya")
        self.assertEqual(resp.status, 422)
        self.assertNotIn("petya", self.env.proto_users("amneziawg"))

    def test_create_validation_changes_nothing(self):
        self.c.get("/connect/new")
        before = (paths.groups_file().read_text(encoding="utf-8"), paths.users_file().read_text(encoding="utf-8"))
        for fields, msg in (({"users_new": ""}, "хотя бы одного"),
                            ({"users_new": "!!!"}, "нет ни букв, ни цифр"),
                            ({"users_new": "; заметка"}, "нет имени"),
                            ({"users_new": "", "existing": ["ghost"]}, "нет в реестре"),
                            ({"users_new": "\n".join(f"u{i}" for i in range(201))}, "не больше 200")):
            resp, body = self.create_group(**fields)
            self.assertEqual(resp.status, 422, fields)
            self.assertIn(msg, body)
            self.assertIn("<h3>Люди</h3>", body)
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
        resp, body = self.wiz(3, go="create", name=evil, proto=["vless-reality"], client__android="happ",
                              users_new=evil_user)
        self.assertEqual(resp.status, 200, "предпросмотр: ничего не создано")
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img", body)
        self.assertIn("&lt;img src=x", body)
        self.assertIn("<code>img-src-x-onerror-alert-2</code>", body)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])
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
        resp, _ = self.create_group(users_new="masha; <b>x</b>")
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
        self.assertIn("Happ — Android", text)
        self.assertRegex(body, r'<a href="/connect/new"[^>]*class="btn primary"[^>]*>Подключить людей</a>')
        self.assertNotIn("Новое подключение", body)
        self.assertNotIn(">Новая группа<", body)
        self.assertIn("общий", text)
        self.assertIn("ставят сами", text)
        head = re.search(r"<thead>(.*?)</thead>", body, re.S).group(1)
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", head), ["группа", "протоколы", "приложения", "через VPN", "ставит",
                                                                       "участники", ""])
        self.assertNotIn("клиент", text.lower())

    def test_edit_group_applies_once_and_reports_qr(self):
        resp, body = self.c.get("/groups/g1")
        self.assertEqual(resp.status, 200)
        self.assertIn('name="name" id="name" value="Семья"', body)
        self.assertIn('<label for="name">Название группы</label>', body)
        self.assertRegex(body, r'name="proto" value="vless-reality" checked')
        self.assertRegex(body, r'name="proto" value="amneziawg" checked')
        self.assertNotRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertRegex(body, r'name="set:android" value="happ" checked')
        self.assertIn('name="allow_mode" value="common" checked', body)
        # настройки: Кто ставит → Протоколы → Приложения → Через VPN, одна кнопка «Сохранить настройки» внизу
        settings = body[body.index('id="settings"'):]
        order = [settings.index(x) for x in ('class="seg"', "<h3 class=\"sub-h\">Протоколы", "<h3 class=\"sub-h\">Приложения",
                                             "<h3 class=\"sub-h\">Через VPN")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(re.findall(r'<button type="submit" class="btn primary">Сохранить настройки</button>', settings)), 1)
        self.assertEqual(len(re.findall(r'<button type="submit" hidden tabindex="-1">Сохранить настройки</button>', settings)), 1,
                         "скрытая — только чтобы Enter в названии сохранял, а не пересчитывал")
        calls = len(self.env.calls())
        resp, _ = self.post("/groups/g1", {"name": ["Родные"], "proto": ["hysteria2", "amneziawg"], "allow_mode": ["own"],
                                            "android": ["com.whatsapp"], "windows": ["Discord.exe"],
                                            "client:android": ["amneziawg"]})
        self.assertEqual(resp.status, 303, text_of(_)[:300])
        _, page = self.c.get("/groups/g1")
        self.assertRegex(page, r'Переслать: [^<]*\bmasha\b[^<]* <a href="/resend">кому и что</a>')
        self.assertRegex(page, r'Переслать: [^<]*\bkolya\b')
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertIn("drop:android:happ", reg["masha"]["resend"], "другое приложение на его Android — старое выключить")
        self.assertIn("app:android:amneziawg", reg["masha"]["resend"])
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
        self.assertNotIn("Переслать:", page)

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
        self.assertIn('Переслать: masha <a href="/resend">', page)
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertIn("app:android:amneziawg", reg["masha"]["resend"], "другая группа — что поменялось на его устройствах")
        self.assertIn("new:ios", reg["masha"]["resend"], "устройство, для которого раньше приложений не было")
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

    def reg(self):
        return {u["name"]: u for u in self.env.users_json()["users"]}

    def test_members_are_chips_and_one_multiselect(self):
        _, page = self.c.get("/groups/g1")
        card = page.split("<h3>Участники</h3>")[1].split("Добавить людей списком")[0]
        self.assertNotIn("<table", card, "участники — не длинный список строк")
        self.assertIn('<a href="/users/masha" class="chip">masha</a>', card)
        self.assertEqual(card.count('type="checkbox" name="user"'), 2, "один список с галочками на всех")
        self.assertIn("data-picker", card)
        self.assertIn('data-find="masha"', card)
        self.assertIn('name="act" value="move"', card)
        self.assertIn('name="act" value="remove"', card)
        self.assertIn("Объединить с…", page)
        self.assertRegex(page, r'<a href="/groups/g1/delete"[^>]*>Удалить группу</a>')

    def test_move_selected_in_one_go(self):
        resp, _ = self.post("/groups/g1/move", {"user": ["masha", "kolya"], "to": ["main"], "act": ["move"]})
        self.assertEqual(resp.status, 303)
        reg = self.reg()
        self.assertEqual((reg["masha"]["group"], reg["kolya"]["group"]), ("main", "main"))
        _, page = self.c.get("/groups/main")
        self.assertIn("переведено: masha, kolya", page)
        # «убрать из группы» — в «Основную»; из «Основной» убирать некуда
        self.post("/groups/main/move", {"user": ["masha"], "to": ["g1"], "act": ["move"]})
        self.assertEqual(self.reg()["masha"]["group"], "g1")
        self.post("/groups/g1/move", {"user": ["masha"], "act": ["remove"]})
        self.assertEqual(self.reg()["masha"]["group"], "main")
        self.post("/groups/main/move", {"user": ["masha"], "act": ["remove"]})
        _, page = self.c.get("/groups/main")
        self.assertIn("убирать некуда", page)

    def test_move_selected_validates(self):
        for multi, msg in (({"to": ["main"]}, "Никого не выбрано"),
                           ({"user": ["owner"], "to": ["main"]}, "не из этой группы"),
                           ({"user": ["masha", "<script>x</script>"], "to": ["main"]}, "не из этой группы"),
                           ({"user": ["masha"], "to": ["нет"]}, "нет")):
            resp, _ = self.post("/groups/g1/move", multi)
            self.assertEqual(resp.status, 303, multi)
            _, page = self.c.get("/groups/g1")
            self.assertIn(msg, page)
            self.assertNotIn("<script>x", page)
        self.assertEqual({self.reg()[n]["group"] for n in ("masha", "kolya")}, {"g1"}, "ничего не сдвинулось")

    def test_delete_group_confirm_page(self):
        resp, page = self.c.get("/groups/g1/delete")
        self.assertEqual(resp.status, 200)
        self.assertRegex(page, r'<input type="radio" name="members" value="move" checked>')
        self.assertIn('name="members" value="delete"', page)
        self.assertIn("Перевести в группу «Основная»", text_of(page))
        self.assertIn("(masha, kolya)", text_of(page), "кого удалим — названо")
        self.assertIn('action="/groups/g1/delete"', page)
        self.assertIn('name="csrf"', page)
        self.assertEqual(self.c.get("/groups/nope/delete")[0].status, 404)
        # «Основная» не удаляется: ни страница, ни кнопки
        _, main = self.c.get("/groups/main/delete")
        self.assertIn("не удаляется", main)
        self.assertNotIn('name="members"', main)
        _, page = self.c.get("/groups/main")
        self.assertRegex(page, r'<span class="btn danger" aria-disabled="true" title="[^"]*не удаляется[^"]*">Удалить группу</span>')
        self.assertNotIn("/groups/main/delete", page)

    def test_delete_confirm_posts_what_it_showed_and_lists_custom_sets(self):
        regs = users.Registry.load()
        regs.require("masha").custom = True
        regs.save()
        _, page = self.c.get("/groups/g1/delete")
        self.assertEqual(re.findall(r'<input type="hidden" name="expected" value="([^"]*)"', page), ["masha", "kolya"])
        self.assertIn('name="shown" value="1"', page)
        self.assertIn("Свой набор протоколов сохранится: masha", text_of(page))
        _, empty = self.c.get("/groups/main/delete")
        self.assertNotIn('name="expected"', empty)

    def test_delete_aborts_when_membership_changed_after_confirm_page(self):
        self.post("/groups/g1/members", {"users_new": ["petya"], "confirm": ["1"]})   # после показа страницы подтверждения
        for mode in ("delete", "move"):
            resp, _ = self.post("/groups/g1/delete", {"members": [mode], "shown": ["1"], "expected": ["masha", "kolya"]})
            self.assertEqual(header(resp, "Location"), ["/groups/g1/delete"], mode)
            _, page = self.c.get("/groups/g1/delete")
            self.assertIn("изменился", text_of(page))
            self.assertIn("стало: kolya, masha, petya", text_of(page))
        self.assertEqual([g["id"] for g in self.groups_json()], ["main", "g1"])
        self.assertEqual({self.reg()[n]["group"] for n in ("masha", "kolya", "petya")}, {"g1"})
        resp, _ = self.post("/groups/g1/delete", {"members": ["delete"], "shown": ["1"],
                                                  "expected": ["masha", "kolya", "petya"]})
        self.assertEqual(header(resp, "Location"), ["/groups"])
        self.assertNotIn("petya", self.reg())

    def test_delete_without_shown_marker_is_not_checked(self):
        resp, _ = self.post("/groups/g1/delete", {"members": ["move"]})
        self.assertEqual(header(resp, "Location"), ["/groups"])

    def test_delete_and_merge_keep_custom_sets(self):
        regs = users.Registry.load()
        regs.require("masha").custom = True
        regs.save()
        _, page = self.c.get("/groups/g1")
        self.assertRegex(page, r'data-confirm="[^"]*Свой набор протоколов сохранится: masha')
        self.post("/groups/g1/delete", {"members": ["move"], "shown": ["1"], "expected": ["masha", "kolya"]})
        reg = self.reg()
        self.assertEqual((reg["masha"]["group"], sorted(reg["masha"]["protocols"]), reg["masha"].get("custom")),
                         ("main", ["amneziawg", "vless-reality"], True))
        self.assertIn("Свой набор протоколов, группа его не тронула: masha", text_of(self.c.get("/groups")[1]))

    def test_delete_group_moves_members_by_default(self):
        resp, _ = self.c.post("/groups/g1/delete", {"members": "move"})
        self.assertEqual(header(resp, "Location"), ["/groups"])
        _, page = self.c.get("/groups")
        self.assertIn("удалена", page)
        self.assertIn("переведены в «Основная»: masha, kolya", page)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])
        reg = self.reg()
        self.assertEqual((reg["masha"]["group"], sorted(reg["masha"]["protocols"])), ("main", sorted(PROTOS)))
        self.assertEqual(self.c.get("/groups/g1")[0].status, 404)

    def test_delete_group_with_members_keeps_owner(self):
        self.post("/groups/g1/members", {"existing": ["owner"]})
        resp, _ = self.c.post("/groups/g1/delete", {"members": "delete"})
        self.assertEqual(header(resp, "Location"), ["/groups"])
        _, page = self.c.get("/groups")
        self.assertIn("удалены: masha, kolya", page)
        reg = self.reg()
        self.assertNotIn("masha", reg)
        self.assertNotIn("kolya", reg)
        self.assertEqual(reg["owner"]["group"], "main", "owner не удаляется — переведён")
        for pid in PROTOS:
            self.assertEqual(set(self.env.proto_users(pid)), {"owner"})

    def test_delete_group_failure_keeps_group(self):
        self.env.fail("amneziawg:user_del")
        resp, _ = self.c.post("/groups/g1/delete", {"members": "delete"})
        self.assertEqual(header(resp, "Location"), ["/groups/g1"])
        _, page = self.c.get("/groups/g1")
        self.assertRegex(page, r'class="bad"')
        self.assertIn("g1", [g["id"] for g in self.groups_json()])

    def test_delete_group_guards(self):
        resp, _ = self.c.post("/groups/main/delete", {"members": "move"})
        self.assertEqual(header(resp, "Location"), ["/groups/main"])
        _, page = self.c.get("/groups/main")
        self.assertIn("не удаляется", page)
        resp, _ = self.c.post("/groups/g1/delete", {"members": "everything"})
        self.assertEqual(header(resp, "Location"), ["/groups/g1"])
        resp, _ = self.c.req("POST", "/groups/g1/delete", {"csrf": "wrong", "members": "delete"})
        self.assertEqual(resp.status, 403)
        resp, _ = Client(self.app).get("/groups/g1/delete")
        self.assertEqual(resp.status, 303)
        resp, _ = Client(self.app).post("/groups/g1/merge", csrf=False)
        self.assertEqual(resp.status, 401)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main", "g1"])
        self.assertEqual(self.reg()["masha"]["group"], "g1")

    def test_list_has_delete_actions(self):
        _, body = self.c.get("/groups")
        self.assertRegex(body, r'<a href="/groups/g1/delete\?from=list"[^>]*>Удалить</a>')
        self.assertRegex(body, r'aria-disabled="true" title="[^"]*не удаляется')
        self.assertNotIn('href="/groups/main/delete"', body)

    def test_merge_groups_page(self):
        g2 = groups.create("Друзья", ["amneziawg"])
        before = self.groups_json()[2]
        _, page = self.c.get("/groups/g1")
        self.assertRegex(page, rf'<option value="{g2.id}">Друзья</option>')
        self.assertIn('action="/groups/g1/merge"', page)
        self.assertRegex(page, r'data-confirm="[^"]*masha[^"]*Семья[^"]*будет удалена')
        _, main = self.c.get("/groups/main")
        self.assertNotIn("/merge", main, "«Основную» нельзя объединить в другую: она не удаляется")
        resp, _ = self.c.post("/groups/g1/merge", {"to": g2.id})
        self.assertEqual(header(resp, "Location"), [f"/groups/{g2.id}"])
        _, page = self.c.get(f"/groups/{g2.id}")
        self.assertIn("«Семья» объединена с «Друзья»", page)
        reg = self.reg()
        self.assertEqual({reg[n]["group"] for n in ("masha", "kolya")}, {g2.id})
        self.assertEqual(reg["masha"]["protocols"], ["amneziawg"], "протоколы — как у Друзей")
        self.assertEqual([g["id"] for g in self.groups_json()], ["main", g2.id])
        self.assertEqual(self.groups_json()[1], before, "настройки целевой группы не изменились")

    def test_merge_errors(self):
        for gid, to in (("g1", "g1"), ("g1", "нет"), ("main", "g1"), ("g1", "")):
            resp, _ = self.c.post(f"/groups/{gid}/merge", {"to": to})
            self.assertEqual(resp.status, 303, (gid, to))
        self.assertEqual([g["id"] for g in self.groups_json()], ["main", "g1"])
        self.assertEqual(self.reg()["masha"]["group"], "g1")
        resp, _ = self.c.req("POST", "/groups/g1/merge", {"csrf": "wrong", "to": "main"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.reg()["masha"]["group"], "g1")

    def test_existing_users_are_a_searchable_multiselect(self):
        _, page = self.c.get("/groups/main")
        self.assertIn("data-picker", page)
        self.assertRegex(page, r'<input type="search" data-filter')
        self.assertIn('<details class="more"><summary>Перевести уже существующих (', page, "пикер свёрнут")
        self.assertIn('<label class="pick-item" data-find="masha семья"><input type="checkbox" name="existing" value="masha">'
                      '<span>masha</span><span class="muted small"><span class="chip">Семья</span></span></label>', page)
        self.assertEqual(page.count("Протоколы и приложения станут как у этой группы"), 1)
        self.assertNotIn('value="owner"', page.split("Перевести уже существующих")[1].split("</details>")[0],
                         "owner и служебные не переводятся")
        _, wiz = self.wiz(2, name="X", proto=["vless-reality"], client__android="happ")
        self.assertIn("data-picker", wiz)
        self.assertRegex(wiz, r'name="existing" value="masha"[^>]*><span>masha</span><span class="muted small"><span class="chip">Семья</span>')
        self.assertIn("Перевести уже существующих (", wiz)
        self.assertNotIn('class="checks"', wiz.split("Перевести уже существующих")[1].split("Через VPN")[0])
    def test_add_members(self):
        resp, _ = self.post("/groups/g1/members", {"users_new": ["petya; друг"], "existing": ["owner"], "confirm": ["1"]})
        self.assertEqual(resp.status, 303)
        _, page = self.c.get("/groups/g1")
        self.assertIn("petya", page)
        self.assertIn("создано: petya", page)
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["petya"]["group"], reg["owner"]["group"]), ("g1", "g1"))
        self.assertEqual(sorted(reg["petya"]["protocols"]), ["amneziawg", "vless-reality"])
        listing = "\n".join(f"worker{i}" for i in range(30)) + "\n!!!"
        resp, page = self.c.post("/groups/g1/members", {"users_new": listing, "confirm": "1"})
        self.assertEqual(resp.status, 422)
        self.assertIn("нет ни букв, ни цифр", page)
        self.assertIn("worker29", page, "одна плохая строка не выбрасывает весь вставленный список")
        self.assertRegex(page, r'<details class="card more" open id="add">\s*<summary>＋ Добавить людей списком')
        self.assertNotIn("worker0",{u["name"] for u in self.env.users_json()["users"]})

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
        msg = self.msg(body).partition("Ключи — только для вас")[0]   # ссылки человека в конце текста — с названиями
        self.assertIn("Установите «Happ»", msg)
        self.assertNotIn("AmneziaWG", msg)
        self.assertNotIn("Hysteria2", msg)
        self.assertNotIn("VLESS", msg, "названий протоколов в инструкции нет")
        self.assertNotIn("<strong>AmneziaWG</strong>", body)
        # «Не нужен» для остальных платформ: у группы клиент выбран только для Android
        self.assertNotIn('data-copy="msg-windows"', body)
        self.assertNotIn("<select", body.split(">Подключить<")[1].split("Все ссылки и QR")[0],
                         "платформа одна — выбирать нечего")
        # мастер: страница раздачи берёт те же клиент и порядок
        resp, done = self.c.get("/connect/done?group=g1&u=masha")
        self.assertIn('id="msg-masha-android"', done)
        self.assertIn("Установите «Happ»", self.msg(done, user="masha"))

    def test_group_page_edits_one_text_per_platform(self):
        _, page = self.c.get("/groups/g1")
        card = page[page.index('<section class="card" id="text">'):]
        card = card[:card.index("</section>")]
        self.assertIn("<h3>Инструкция</h3>", card)
        self.assertNotIn("Текст для", card)
        self.assertIn("«{имя}» заменится именем", card)
        self.assertNotIn("{name}", card)
        self.assertRegex(card, r'<summary>Android </summary>', "платформа без набора приложений группы инструкции не получает")
        self.assertNotIn("Windows", card)
        default = html.unescape(re.search(r"<textarea[^>]*>(.*?)</textarea>", card, re.S).group(1))
        self.assertTrue(default.startswith("{имя}, VPN на Android: что сделать\nЧерез VPN — только "), default)
        self.assertIn("\n2) Установите «Happ»", default)
        self.assertNotIn("Вернуть по умолчанию", card)
        # сохранить свою инструкцию: она у всех участников с их именем; «{имя}» и «{name}» — одно и то же
        resp, _ = self.post("/groups/g1/message", {"platform": ["android"], "text": ["{имя}, ставь Happ.\r\nПотом QR."]})
        self.assertEqual(header(resp, "Location"), ["/groups/g1?m=android#text"])
        saved = self.groups_json()[1]["messages"]["android"]
        self.assertEqual(saved["text"], "{name}, ставь Happ.\nПотом QR.", "внутри хранится «{name}»")
        self.assertTrue(saved["sig"], "вместе с текстом сохраняется подпись набора приложений")
        _, page = self.c.get("/groups/g1?m=android")
        self.assertIn("Инструкция сохранена", page)
        self.assertRegex(page, r'<details open class="more"><summary>Android <span class="badge info">своя</span>')
        self.assertIn("Вернуть по умолчанию", page)
        self.assertIn("{имя}, ставь Happ.", html.unescape(page))
        for n in ("masha", "kolya"):
            self.assertTrue(self.msg(self.c.get(f"/users/{n}")[1]).startswith(f"{n}, ставь Happ.\nПотом QR.\n\nКлючи"))
        _, done = self.c.get("/connect/done?group=g1&u=masha")
        self.assertIn("masha, ставь Happ.", html.unescape(done), "на шаге раздачи инструкция — с именем первого, не «{имя}»")
        self.assertNotIn("{имя}", html.unescape(done))
        self.assertNotIn("<textarea", done.split("Инструкция")[-1].split("</details>")[0], "править можно только на странице группы")
        resp, _ = self.post("/groups/g1/message", {"platform": ["android"], "text": ["{name} тоже годится"]})
        self.assertEqual(self.groups_json()[1]["messages"]["android"]["text"], "{name} тоже годится")
        # вернуть по умолчанию
        self.post("/groups/g1/message", {"platform": ["android"], "reset": ["1"]})
        self.assertNotIn("messages", self.groups_json()[1])
        self.assertIn("Установите «Happ»", self.msg(self.c.get("/users/masha")[1]))
    def test_group_text_warns_when_client_set_changed(self):
        self.post("/groups/g1/message", {"platform": ["android"], "text": ["Мой {name}"]})
        _, page = self.c.get("/groups/g1?m=android")
        self.assertNotIn(clientviews.STALE, page)
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"],
                                 "client:android": ["amneziawg"]})
        _, page = self.c.get("/groups/g1?m=android")
        self.assertIn(clientviews.STALE, page)
        self.assertIn("проверьте", page)
        self.post("/groups/g1/message", {"platform": ["android"], "text": ["Мой {имя}, новый"]})
        self.assertNotIn(clientviews.STALE, self.c.get("/groups/g1?m=android")[1], "после сохранения подпись свежая")
        # прежний формат (строка) — подписи нет, предупреждать не о чем
        data = json.loads(paths.groups_file().read_text(encoding="utf-8"))
        data["groups"][1]["messages"] = {"android": "Старый {name}"}
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        _, page = self.c.get("/groups/g1?m=android")
        self.assertNotIn(clientviews.STALE, page)
        self.assertIn("Старый {имя}", html.unescape(page), "в редакторе — «{имя}»")
        self.assertIsNone(groups.Groups.load().get("g1").msg_sigs.get("android"))

    def test_person_with_other_protocols_gets_own_text_with_note(self):
        groups.update("g1", clients={"android": ["happ", "amneziawg"]})
        g = groups.Groups.load().get("g1")
        ctx = clientviews.Ctx.load()
        groups.set_message("g1", "android", "Групповой {name}", ctx.group_sig(g, "android"))
        g = groups.Groups.load().get("g1")
        full = str(clientviews.connect_panel(clientviews.synth_links(["vless-reality", "amneziawg"]), "masha", ctx, g))
        self.assertIn("Групповой masha", full)
        self.assertNotIn(clientviews.MISMATCH, full)
        # у человека свой набор протоколов: приложения другие — инструкция группы не про него
        part = str(clientviews.connect_panel(clientviews.synth_links(["vless-reality"]), "kolya", ctx, g))
        self.assertNotIn("Групповой", part)
        self.assertIn(html.escape(clientviews.MISMATCH), part)
        self.assertIn("kolya, VPN на Android", html.unescape(part))
        self.assertNotIn("<textarea", part, "инструкция на странице человека — только для чтения")
        self.assertIn('<pre id="msg-android"', part)
        # группа без своей инструкции, но человек с другими приложениями: тоже свой пакет с пометкой
        groups.set_message("g1", "android", None)
        g = groups.Groups.load().get("g1")
        self.assertIn(html.escape(clientviews.MISMATCH),
                      str(clientviews.connect_panel(clientviews.synth_links(["vless-reality"]), "kolya", ctx, g)))
        self.assertNotIn(clientviews.MISMATCH,
                         str(clientviews.connect_panel(clientviews.synth_links(["vless-reality", "amneziawg"]),
                                                       "masha", ctx, g)))
        # а приложения, которых нет в наборе группы, человеку не раздаются: рекомендованных каталогом нет
        self.assertIsNone(clientviews.connect_panel(clientviews.synth_links(["hysteria2"]), "kolya", ctx, g))
    def test_message_equal_to_default_is_not_frozen(self):
        _, page = self.c.get("/groups/g1")
        default = html.unescape(re.search(r'<form[^>]*action="/groups/g1/message"[^>]*>.*?<textarea[^>]*>(.*?)</textarea>',
                                          page, re.S).group(1))
        self.post("/groups/g1/message", {"platform": ["android"], "text": [default]})
        self.assertNotIn("messages", self.groups_json()[1], "версии приложений в файл не замораживаем")
        self.post("/groups/g1/message", {"platform": ["android"], "text": [default + "\nP.S."]})
        self.assertIn("P.S.", self.groups_json()[1]["messages"]["android"]["text"])

    def test_message_errors(self):
        for multi, why in (({"platform": ["plan9"], "text": ["x"]}, "неизвестная платформа"),
                           ({"platform": ["android"], "text": ["я" * (groups.MESSAGE_MAX + 1)]}, "длиннее")):
            resp, _ = self.post("/groups/g1/message", multi)
            self.assertEqual(resp.status, 303)
            _, page = self.c.get("/groups/g1")
            self.assertIn(why, page)
            self.assertNotIn("messages", self.groups_json()[1])
        resp, _ = self.post("/groups/nope/message", {"platform": ["android"], "text": ["x"]})
        self.assertEqual(resp.status, 303)
        self.assertIn("группы «nope» нет", self.c.get("/groups/g1")[1])

    def test_saving_group_settings_keeps_texts(self):
        self.post("/groups/g1/message", {"platform": ["android"], "text": ["Мой {name}"]})
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"], "client:android": ["happ"]})
        self.assertEqual(self.groups_json()[1]["messages"]["android"]["text"], "Мой {name}")

    def test_users_form_does_not_make_group_member_custom(self):
        # протоколы в форме отмечены как у группы — only не передаётся, пользователь не «свой»
        _, page = self.c.get("/users")
        self.assertRegex(page, r'<option value="g1" data-protos="amneziawg vless-reality">Семья</option>')
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
        self.assertIn("<h3>Люди</h3>", body)
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
        self.assertIn("список группы", page)
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


class StartStepTest(GroupWebBase):
    """Шаг 0 мастера: кто ставит приложения и готовые варианты."""

    def test_start_screen_has_mode_and_three_variants(self):
        now = time.time()
        seed_live("hysteria2", now, rtt=31.4, mbps=52.0, age=120)
        resp, body = self.c.get("/connect/new")
        self.assertEqual(resp.status, 200)
        self.assertIn("<h1>Подключить людей</h1>", body)
        self.assertIn("<h3>Вариант</h3>", body)
        self.assertNotIn("1. Кто ставит приложения?", body)
        self.assertRegex(body, r'<li class="cur" aria-current="step"><span class="n">1</span>Вариант</li>')
        self.assertEqual(re.findall(r'<li[^>]*><span class="n">\d</span>([^<]*)</li>', body),
                         ["Вариант", "Приложения", "Люди", "Раздача"], "«Протоколы» — подшаг только у «Своего набора»")
        # «Кто ставит» — один переключатель из radio, а не ссылки
        self.assertIn("Кто ставит:", body)
        self.assertRegex(body, r'<input type="radio" name="mode" value="mixed" checked data-auto><span>Телефоны — сами, '
                               r'компьютеры — ИТ</span>', "по умолчанию офис: личные телефоны — сами, компьютеры — ИТ (D61)")
        self.assertRegex(body, r'<input type="radio" name="mode" value="admin" data-auto><span>Всё ставит ИТ</span>')
        self.assertRegex(body, r'<input type="radio" name="mode" value="self" data-auto><span>Всё ставят сами</span>')
        self.assertNotIn("?mode=admin", body)
        for name in ("Просто", "Надёжно", "Свой набор"):
            self.assertIn(f"<strong>{name}</strong>", body)
        self.assertEqual(len(re.findall(r'name="go" value="(?:simple|reliable|custom)"', body)), 3)
        self.assertEqual(len(re.findall(r'class="btn primary">Выбрать</button>', body)), 1, "один primary — у варианта без оговорок")
        self.assertRegex(body, r'<strong>Надёжно</strong><span class="chip info">рекомендуем</span>')
        self.assertRegex(body, r'name="go" value="reliable" class="btn primary"')
        self.assertRegex(body, r'name="go" value="simple" class="btn"')
        simple, reliable = (re.search(rf'<strong>{n}</strong>(.*?)name="go" value="', body, re.S).group(1)
                            for n in ("Просто", "Надёжно"))
        self.assertNotIn("с оговорками", reliable, "«рекомендуем» и «с оговорками» на одном варианте не живут")
        self.assertNotIn("рекомендуем", simple)
        self.assertIn("Happ — Android", reliable, "«Надёжно» — Happ на Android, если режут UDP")
        text = text_of(body)
        self.assertIn("AmneziaWG — Android · INCY — iPhone · v2rayN — Windows", text, "приложения — одной строкой одного вида")
        self.assertNotIn("приложение на устройство", text)
        self.assertNotIn("Мбит/с", text, "скорость в пресетах — только по замерам с устройств")
        self.assertEqual(text.count(groupviews.NO_PEOPLE_DATA), 1, "про отсутствие замеров — одна строка на шаг")
        self.assertNotIn("с сервера", text)
        self.assertNotRegex(body, r'name="proto"')
        self.assertIn("data-expanded", body)
        self.assertNotIn("style=", body)
        self.assertNotIn("Далее", body, "у экрана выбора своих кнопок «Далее» нет: выбор — кнопки вариантов")
        self.assertNotIn("! ", re.sub(r"<[^>]+>", "", body))
        self.assertIn(groupviews.MODE_HINTS["mixed"], text)
        self.assertIn(groupviews.MODE_PICK, text, "как выбрать «Кто ставит» офису — одной строкой")

    def test_start_screen_why_line_comes_from_client_probes(self):
        facts = {"hysteria2": {"n": 10, "ok": 9, "top": 2, "score": 160.0, "ctx": 2, "down": 65.4, "latency": 76.0},
                 "vless-reality": {"n": 12, "ok": 12, "top": 1, "score": 100.0, "ctx": 2, "down": 30.0, "latency": 80.0}}
        with mock.patch.object(groupviews, "_rank_facts", return_value=facts):
            _, body = self.c.get("/connect/new")
        text = text_of(body)
        self.assertIn("у людей: VLESS Vision ≈30 Мбит/с", text, "быстрее всего — Hysteria2, а в вариантах её нет")
        self.assertNotIn("замеров с устройств пока нет", text)
        facts["vless-reality"]["down"] = 90.0
        with mock.patch.object(groupviews, "_rank_facts", return_value=facts):
            _, body = self.c.get("/connect/new")
        self.assertIn("быстрее всего у людей: ≈90 Мбит/с", text_of(body))
    def test_mode_switch_is_a_form_control_that_keeps_the_draft(self):
        _, admin = self.c.get("/connect/new?mode=admin")
        self.assertRegex(admin, r'<input type="radio" name="mode" value="admin" checked data-auto>')
        self.assertIn("«Дистрибутивов»", admin)
        self.assertNotIn('type="hidden" name="mode"', admin, "режим — переключатель, скрытого поля рядом нет")
        _, junk = self.c.get("/connect/new?mode=zzz")
        self.assertRegex(junk, r'<input type="radio" name="mode" value="mixed" checked data-auto>')
        # смена режима — отправка формы: введённое на других шагах не теряется, варианты пересчитаны
        resp, body = self.wiz(0, go="refresh", mode="admin", name="Мой офис", users_new="masha")
        self.assertEqual(resp.status, 200)
        self.assertRegex(body, r'<input type="radio" name="mode" value="admin" checked data-auto>')
        self.assertIn('type="hidden" name="name" value="Мой офис"', body)
        self.assertIn('type="hidden" name="users_new" value="masha"', body)
        self.assertIn('data-refresh', body)
        self.assertIn("<h3>Вариант</h3>", body)
    def test_simple_preset_goes_straight_to_apps_with_one_app_per_device(self):
        resp, body = self.wiz(0, go="simple", mode="self")
        self.assertEqual(resp.status, 200)
        self.assertIn("<h3>Приложения</h3>", body)
        self.assertEqual(re.findall(r'type="hidden" name="proto" value="([^"]+)"', body), ["amneziawg", "vless-reality"])
        self.assertEqual(self.checked(body, "android"), ["amneziawg"], "список приложений — внутри QR")
        self.assertEqual(self.checked(body, "ios"), ["incy"], "«Просто» людям, которые ставят сами: на iPhone INCY из App Store РФ")
        self.assertEqual(self.checked(body, "windows"), ["v2rayn"])
        self.assertNotIn("Hiddify", text_of(body))
        self.assertNotIn("Оговорки", body, "ни у одного приложения набора оговорок нет")
        self.assertNotIn("ставится только из App Store", body, "ИТ-заметка про iPhone — только в режиме «ставит ИТ»")
        self.assertIn('type="hidden" name="preset" value="simple"', body)
        # название группы — на шаге «Люди», без подстановки: «Группа N» потом не отличить
        self.assertNotIn('id="name"', body)
        resp, body = self.wiz(2, mode="self", proto="vless-reality", clients_for="vless-reality|self", devs="1",
                              dev=["android", "ios", "windows"], set__android="happ", set__ios="incy",
                              set__windows="v2rayn", preset="simple")
        self.assertIn("<h3>Люди</h3>", body)
        self.assertRegex(body, r'<input type="text" name="name" id="name" value="" required maxlength="40" '
                               r'autocomplete="off" placeholder="например, Бухгалтерия">')
        resp, _ = self.wiz(3, go="create", mode="self", name="Офис", proto="vless-reality", devs="1",
                           dev=["android", "windows"], set__android="happ", set__windows="v2rayn",
                           users_new="masha\nkolya", allow_mode="common", confirm="1")
        self.assertEqual(resp.status, 303)
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual((g["name"], g["protocols"], g["clients"]), ("Офис", ["vless-reality"],
                                                                     {"android": ["happ"], "windows": ["v2rayn"]}))
        self.assertNotIn("install_mode", g)
    def test_admin_mode_is_saved_and_final_step_has_distributions_and_iphone_note(self):
        resp, body = self.wiz(0, go="reliable", mode="admin")
        self.assertIn("<h3>Приложения</h3>", body)
        self.assertIn("AmneziaWG", body)
        self.assertIn("iPhone: приложение ставится только из App Store.", body, "в режиме ИТ — честная строка про iPhone")
        self.assertRegex(body, r'<details class="more inline"><summary>варианты</summary>')
        self.assertNotIn("<details class=\"more inline\" open", body)
        protos = re.findall(r'type="hidden" name="proto" value="([^"]+)"', body)
        self.assertEqual(len(protos), 2)
        resp, _ = self.wiz(3, go="create", mode="admin", name="Офис", proto=protos, devs="1", dev=["android", "windows"],
                           set__android="happ", set__windows="v2rayn", users_new="masha", allow_mode="common",
                           confirm="1")
        self.assertEqual(resp.status, 303, text_of(_)[:300])
        g = [x for x in self.groups_json() if x["id"] == "g1"][0]
        self.assertEqual(g["install_mode"], "admin")
        loc = header(resp, "Location")[0]
        _, done = self.c.get(loc)
        # порядок: «готова» → «Раздать доступы» (главная кнопка) → «Инструкция» → «Дистрибутивы»
        marks = ["Группа «Офис» готова: 1 чел.", "Раздать доступы", "Карточки (печать, ZIP, CSV)", "<summary>Инструкция</summary>",
                 "<h3>Дистрибутивы</h3>"]
        positions = [done.index(m) for m in marks]
        self.assertEqual(positions, sorted(positions), marks)
        self.assertNotIn("Скачать дистрибутивы", done)
        self.assertIn("Файла пока нет", done, "файлов ещё нет: честно так и пишем и даём ссылку на GitHub")
        self.assertIn(">Скачать на сервер<", done)
        self.assertNotIn("sha256", done.split("<h3>Дистрибутивы</h3>")[1].split("Как установить")[0],
                         "пока ничего не скачано — пустых столбцов версии, размера и sha256 нет")
        self.assertIn("Как установить (для ИТ)", done)
        self.assertIn('action="/dist/refresh"', done)
        # человеку — ни GitHub, ни «Assets», ни «Установите»: приложения уже стоят
        text = self.msg(done, user="masha")
        self.assertTrue(text.startswith("masha, VPN уже установлен. Включите его в «Happ»."), text)
        for gone in ("Установите", "releases", "Assets"):
            self.assertNotIn(gone, text)
        _, user = self.c.get("/users/masha")
        panel = user[user.index('data-pp="android"'):user.index('id="msg-android"')]
        self.assertNotIn("GitHub", panel, "ИТ уже поставил: ссылок на магазины в «Подключить» нет")
    def test_custom_goes_to_protocols_with_defaults_by_mode(self):
        resp, body = self.wiz(0, go="custom", mode="self")
        self.assertIn("<h3>Протоколы</h3>", body)
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertIn('type="hidden" name="mode" value="self"', body)
        self.assertIn('type="hidden" name="custom" value="1"', body)
        self.assertEqual(re.findall(r'<li[^>]*><span class="n">\d</span>([^<]*)</li>', body),
                         ["Вариант", "Протоколы", "Приложения", "Люди", "Раздача"], "у «Своего набора» подшаг виден")
        resp, body = self.wiz(0, go="custom", mode="admin")
        self.assertRegex(body, r'name="proto" value="hysteria2" checked')
        self.assertIn('type="hidden" name="mode" value="admin"', body)
    def test_self_mode_defaults_skip_protocols_without_one_qr_import(self):
        self.env.add_protocol("tuic")
        self.env.add_manifest("hysteria2-obfs", layer="udp", users_backend="hysteria-command", engine="hysteria",
                              name="Hysteria2 + Salamander", short="HY2 + Salamander")
        with mock.patch.object(groupviews, "_rank_facts", return_value={
                "tuic": {"n": 9, "ok": 9, "top": 3, "score": 300.0, "ctx": 3}}),                 mock.patch.object(groupviews.clients, "load", no_hiddify_qr):
            _, own = self.wiz(0, go="custom", mode="self")
            _, admin = self.wiz(0, go="custom", mode="admin")
        self.assertNotRegex(own, r'name="proto" value="tuic" checked', "TUIC: людям без QR из магазина не предвыбирается")
        self.assertRegex(admin, r'name="proto" value="tuic" checked')

    def test_preset_unknown_or_gone_is_an_error_not_a_crash(self):
        resp, body = self.wiz(0, go="nope", mode="self")
        self.assertEqual(resp.status, 422)
        self.assertIn("Такого варианта нет", body)
        self.assertIn("<h3>Вариант</h3>", body)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])

    def test_back_walks_every_step(self):
        # после готового варианта «Назад» с приложений ведёт на «Вариант», выбор сохранён
        _, body = self.wiz(2, go="back", mode="admin", name="Семья", proto=["hysteria2"], preset="reliable")
        self.assertIn("<h3>Вариант</h3>", body)
        self.assertRegex(body, r'<input type="radio" name="mode" value="admin" checked data-auto>', "режим не теряется при возврате")
        self.assertRegex(body, r'class="opt preset sel"><span class="opt-body"><span class="opt-title"><strong>Надёжно</strong>')
        self.assertEqual(body.count("opt preset sel"), 1)
        self.assertEqual(re.findall(r'<li[^>]*><span class="n">\d</span>([^<]*)</li>', body),
                         ["Вариант", "Приложения", "Люди", "Раздача"], "«Протоколы» степпер не показывает: их не показывали")
        # после «Своего набора» — на «Протоколы»; оттуда — на «Вариант»
        _, body = self.wiz(2, go="back", mode="admin", name="Семья", proto=["hysteria2"], custom="1", preset="custom")
        self.assertIn("<h3>Протоколы</h3>", body)
        _, body = self.wiz(1, go="back", mode="admin", name="Семья", proto=["hysteria2"], custom="1", preset="custom")
        self.assertIn("<h3>Вариант</h3>", body)
        self.assertIn("opt preset sel", body)
        self.assertEqual(re.findall(r'<li[^>]*><span class="n">\d</span>([^<]*)</li>', body)[1], "Протоколы")
        # «сменить протоколы» с приложений — на подшаг «Протоколы», который появляется в степпере
        _, body = self.wiz(2, go="protocols", mode="self", name="Семья", proto=["hysteria2"], preset="simple")
        self.assertIn("<h3>Протоколы</h3>", body)
        self.assertEqual(re.findall(r'<li[^>]*><span class="n">\d</span>([^<]*)</li>', body)[1], "Протоколы")
        self.assertNotIn("← Назад", self.c.get("/connect/new")[1], "на первом шаге назад некуда")
    def test_no_presets_only_custom_when_nothing_fits(self):
        for pid in PROTOS:
            (self.env.etc / "protocols.d" / f"{pid}.json").unlink()
        self.env.add_protocol("tuic")
        with mock.patch.object(groupviews.clients, "load", no_hiddify_qr):
            _, body = self.c.get("/connect/new")
            _, admin = self.c.get("/connect/new?mode=admin")
        self.assertIn("<strong>Свой набор</strong>", body)
        self.assertNotIn("<strong>Просто</strong>", body, "людям TUIC без QR из магазина не предлагаем")
        self.assertIn("<strong>Просто</strong>", admin)
        self.assertNotIn("<strong>Надёжно</strong>", admin, "пары протоколов нет")

    def test_group_page_has_mode_and_saves_it(self):
        self.create_group()
        _, page = self.c.get("/groups/g1")
        self.assertRegex(page, r'<input type="radio" name="mode" value="self" checked data-auto>')
        self.assertRegex(page, r'<input type="radio" name="mode" value="admin" data-auto>')
        self.assertNotIn("Дистрибутивы", page)
        # «Пересчитать» — пересборка блока без записи
        before = paths.groups_file().read_text(encoding="utf-8")
        resp, body = self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"], "devs": ["1"],
                                              "dev": ["android", "ios"], "mode": ["admin"], "go": ["refresh"],
                                              "set:android": ["happ"]})
        self.assertEqual(resp.status, 200)
        self.assertIn("ставится только из App Store", body, "в режиме ИТ строка про iPhone видна и до сохранения")
        self.assertEqual(before, paths.groups_file().read_text(encoding="utf-8"))
        resp, _ = self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"], "devs": ["1"],
                                           "dev": ["android"], "mode": ["admin"], "set:android": ["happ"]})
        self.assertEqual(resp.status, 303)
        self.assertEqual([x for x in self.groups_json() if x["id"] == "g1"][0]["install_mode"], "admin")
        _, page = self.c.get("/groups/g1")
        self.assertIn("<h3>Дистрибутивы</h3>", page)
        self.assertRegex(text_of(self.c.get("/groups")[1]), r"Семья .* общий ставит ИТ ")
    def test_new_device_chip_gets_a_proposed_set_on_group_page(self):
        self.create_group()
        _, body = self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"], "devs": ["1"],
                                           "dev": ["android", "macos"], "go": ["refresh"], "set:android": ["happ"]})
        self.assertEqual(self.checked(body, "macos"), ["v2rayn"], "добавленному устройству — как в варианте: v2rayN, "
                                                                   "AmneziaVPN (весь компьютер) — только через «сменить»")
        self.assertEqual(self.checked(body, "android"), ["happ"], "остальное не тронуто")


class DesignRulesTest(GroupWebBase):
    """Стили и скрипт мастера: переключатель из radio, пересчёт без JS, строки устройств, пресеты, предпросмотр."""

    def test_css_rules_of_the_design_review(self):
        from zoolib.web import assets
        css = re.sub(r"\s+", " ", assets.CSS)
        for rule in (".js button[data-refresh] { display: none; }",                       # кнопка «Пересчитать» с JS не видна
                     ".dev-change { flex: 0 0 auto; margin-left: auto;",                  # «сменить» справа в той же строке
                     ".row-warn .chip.warn, .row-bad .chip.bad { border: 1px solid currentColor; }",
                     ".wiz-nav { position: sticky; bottom: 0;",                           # кнопки мастера прилипают к низу
                     ".seg label:has(input:checked)",                                     # переключатель из radio
                     "@media (max-width: 480px) { .seg { display: grid; grid-template-columns: 1fr 1fr; }",
                     ".preset .btn { width: 100%; }"):
            self.assertIn(rule, css, rule)
        self.assertNotIn(".plat-sum", css, "статус покрытия — чипом")

    def test_js_marks_the_page_and_adds_links_to_a_read_only_instruction(self):
        from zoolib.web import assets
        self.assertIn("document.documentElement.classList.add('js')", assets.JS)
        self.assertNotIn("data-addlinks", assets.JS, "ссылки человека — в тексте сообщения с сервера, без галочки")
        self.assertIn("el.form.querySelector('button[data-refresh]')", assets.JS, "смена radio отправляет форму сама")

    def test_refresh_button_is_in_the_dom_and_named_for_no_js(self):
        _, body = self.wiz(0, go="refresh", mode="admin")
        self.assertRegex(body, r'<button type="submit" name="go" value="refresh" class="btn small" data-refresh formnovalidate>Пересчитать</button>')
        _, gp = self.c.get("/groups/main")
        self.assertRegex(gp, r'<button type="submit" name="go" value="refresh" class="btn small" data-refresh formnovalidate>Пересчитать</button>')
        self.assertNotIn(">Обновить<", gp)

    def test_via_vpn_line_in_the_wizard(self):
        _, body = self.wiz(2, name="X", proto=["vless-reality"], devs="1", dev=["android", "windows"], set__android="happ",
                           set__windows="v2rayn")
        line = re.search(r'<p class="hint">(Через VPN[^<]*)', body)
        self.assertTrue(line, "одна строка вместо таблицы")
        self.assertEqual(line.group(1), "Через VPN: общий список (Brave, Telegram) · Android 2 · Windows 2",
                         "платформы с заглавной, числа видны сразу (не в подсказке)")
        self.assertIn('name="allow_mode" value="own"', body, "свой список — здесь же, до раздачи (D59)")
        self.assertNotIn("<table", body.split("<summary>Через VPN —")[0], "таблица приложений — только в свёрнутом выборе")
        # Hiddify на Windows списка не умеет: через VPN идёт всё
        _, body = self.wiz(2, name="X", proto=["hysteria2"], devs="1", dev=["windows"], set__windows="hiddify")
        self.assertIn("В «Hiddify» через VPN идёт всё.", body)
        self.assertNotIn("Через VPN: общий список", body)
        _, body = self.wiz(2, name="X", proto=["hysteria2"], devs="1", dev=["android", "windows"], set__android="happ",
                           set__windows="hiddify")
        self.assertIn("Через VPN: общий список", body)
        self.assertIn("В «Hiddify» через VPN идёт всё.", body, "а там, где список работает, — строка про список")

    def test_existing_picker_skips_owner_and_names_the_source_group(self):
        _, body = self.wiz(2, name="X", proto=["vless-reality"], set__android="happ")
        self.assertNotIn("Перевести уже существующих", body, "только owner: переводить некого")
        self.assertNotIn('name="existing"', body)


class LeftoversTest(GroupWebBase):
    """Остатки дизайн-ревью: быстрый QR по набору группы, имена вместо логинов, пресеты, кнопки и сводка раздачи."""

    def setUp(self):
        super().setUp()
        self.env.add_protocol("vless-xhttp")

    def made(self, **kw):
        base = dict(name="Офис", proto=["hysteria2", "vless-xhttp"], devs="1", dev=["android", "ios", "windows"],
                    set__android="happ", set__ios="happ", set__windows="v2rayn", users_new="Иван Петров; бух\nМария",
                    allow_mode="common", confirm="1")
        base.update(kw)
        resp, body = self.wiz(3, go="create", **base)
        self.assertEqual(resp.status, 303, text_of(body)[:300])
        return header(resp, "Location")[0]

    def quick(self, body):
        start = body.index('<div class="quick">')
        end = body.find('<div class="ptiles"', start)
        return body[start:end if end > 0 else body.index("</details>", start)]

    # --- быстрый старт на странице человека ---

    def test_quick_start_caption_and_link_come_from_the_group_app_set(self):
        groups.update("main", protocols=["amneziawg", "hysteria2"], clients={"android": ["happ"]})
        users.add_user("masha", group="main")
        _, body = self.c.get("/users/masha")
        quick = self.quick(body)
        self.assertIn("Android: Happ → «+» → «Сканировать QR»", quick, "подпись — приложение набора, а не вшитый Happ")
        self.assertIn('title="Hysteria2"', quick, "под Happ — ссылка, которую он открывает (не AmneziaWG), по-человечески")
        self.assertNotIn('title="amneziawg"', quick)
        self.assertNotIn("AmneziaWG", quick)
        groups.update("main", clients={"android": ["amneziawg"]})
        _, body = self.c.get("/users/masha")
        quick = self.quick(body)
        self.assertIn("Android: AmneziaWG → «+» → «Сканировать QR»", quick)
        self.assertIn('title="AmneziaWG"', quick)

    def test_quick_start_prefers_phone_over_computer_and_skips_apps_without_qr(self):
        groups.update("main", protocols=["hysteria2"], clients={"windows": ["v2rayn"], "ios": ["happ"]})
        users.add_user("masha", group="main")
        _, body = self.c.get("/users/masha")
        quick = self.quick(body)
        self.assertIn("iPhone: Happ", quick, "v2rayN QR не берёт, Windows — не телефон")
        self.assertNotIn("v2rayN", quick)
        groups.update("main", clients={"windows": ["v2rayn"]})
        _, body = self.c.get("/users/masha")
        self.assertNotIn('<div class="quick">', body, "нет приложения с QR — пустого «Быстрого старта» нет")
        adv = body[body.index("<summary>Все ссылки и QR</summary>"):]
        self.assertIn("Скопировать всё", adv[:adv.index('<div class="ptiles"')])

    def test_quick_start_without_apps_says_so_and_links_to_settings(self):
        groups.update("main", clients={})
        users.add_user("masha", group="main")
        _, body = self.c.get("/users/masha")
        quick = self.quick(body)
        self.assertIn("Приложения не выбраны", quick)
        self.assertIn('href="/groups/main#settings"', quick)
        self.assertNotIn('<img class="qr"', quick)
        self.assertNotIn("Happ", quick, "вшитого Happ нет")

    # --- имена вместо логинов ---

    def test_users_list_shows_display_name_first_sorted_by_it(self):
        self.made(users_new="Яков Бом\nАнна Белая; бух\nvasya")
        _, body = self.c.get("/users")
        rows = re.findall(r'<a href="/users/([^"]+)"><strong>([^<]+)</strong></a>(?:[^<]*<span class="badge[^>]*>[^<]*</span>)?'
                          r'(?:<span class="sub">([^<]*)</span>)?', body)
        by = {n: (label, sub) for n, label, sub in rows}
        self.assertEqual(by["anna-belaya"], ("Анна Белая", "anna-belaya · бух"), "имя жирным, логин мелко, заметка после него")
        self.assertEqual(by["vasya"], ("vasya", ""), "нет имени — логин")
        order = [label for n, label, _ in rows if n in ("anna-belaya", "yakov-bom", "vasya")]
        self.assertEqual(order, ["vasya", "Анна Белая", "Яков Бом"],
                         "сортировка по имени (без регистра), а не по логину: vasya < Анна < Яков")
        _, found = self.c.get("/users?q=anna-bel")
        self.assertIn("Анна Белая", found, "по логину тоже ищется")

    def test_users_add_form_has_name_and_login_and_titles_not_ids(self):
        _, body = self.c.get("/users")
        form = body[body.index('<form method="post" action="/users"'):]
        form = form[:form.index("</form>")]
        self.assertRegex(form, r'<label for="display">Имя</label><input type="text" name="display" id="display"[^>]* required')
        self.assertRegex(form, r'<label for="name">Логин</label><input type="text" name="name" id="name"(?![^>]*required)')
        self.assertIn('placeholder="из имени"', form)
        for pid, title in (("vless-reality", "VLESS Vision"), ("hysteria2", "Hysteria2"), ("amneziawg", "AmneziaWG")):
            self.assertIn(f'value="{pid}"', form)
            self.assertIn(f">{title}</span>", form, "подпись — название протокола, не id")
        self.assertNotIn(">vless-reality<", form)

    def test_users_add_login_from_name_by_translit_like_the_wizard(self):
        resp, _ = self.c.post("/users", {"display": "Иван Петров", "name": "", "note": "бух"})
        self.assertEqual(header(resp, "Location"), ["/users/ivan-petrov"])
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["ivan-petrov"]["display"], reg["ivan-petrov"]["note"]), ("Иван Петров", "бух"))
        resp, _ = self.c.post("/users", {"display": "Иван Петров", "name": ""})
        self.assertEqual(header(resp, "Location"), ["/users/ivan-petrov-2"], "занято — номер, как в списке людей")
        resp, _ = self.c.post("/users", {"display": "Мария", "name": "  Masha "})
        self.assertEqual(header(resp, "Location"), ["/users/masha"], "логин задан — берётся он, регистр не важен")
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual(reg["masha"]["display"], "Мария")
        resp, _ = self.c.post("/users", {"display": "Kolya", "name": ""})
        self.assertEqual(header(resp, "Location"), ["/users/kolya"])
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual(reg["kolya"]["display"], "Kolya", "имя с заглавной — имя, логин — строчными")
        resp, _ = self.c.post("/users", {"display": "petya", "name": ""})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertNotIn("display", reg["petya"], "имя совпало с логином — отдельно не хранится")
        resp, _ = self.c.post("/users", {"name": "lena"})
        self.assertEqual(header(resp, "Location"), ["/users/lena"], "прежний вызов только с логином работает")
        for bad in ({"display": "", "name": ""}, {"display": "!!!", "name": ""}):
            resp, _ = self.c.post("/users", bad)
            self.assertEqual(header(resp, "Location"), ["/users"], bad)
            _, page = self.c.get("/users")
            self.assertRegex(page, "Введите имя или логин|нет ни букв, ни цифр")

    def test_done_flash_lists_display_names_and_no_logins(self):
        loc = self.made()
        _, body = self.c.get(loc)
        self.assertIn("создано: Иван Петров, Мария", text_of(body))
        self.assertNotIn("создано: ivan-petrov", text_of(body))
        resp, _ = self.post("/groups/g1/move", {"user": ["ivan-petrov"], "act": ["remove"]})
        _, page = self.c.get("/groups/main")
        self.assertIn("переведено: Иван Петров", text_of(page))

    # --- «Раздача» ---

    def test_done_has_one_primary_button_and_no_empty_traffic_chip(self):
        loc = self.made(mode="admin")
        _, body = self.c.get(loc)
        main = body[body.index("<main"):]
        self.assertEqual(len(re.findall(r'class="[^"]*\bprimary\b', main)), 1, "главная кнопка одна — «Карточки…»")
        self.assertIn('class="btn primary" data-swap>Карточки (печать, ZIP, CSV)</a>', main)
        self.assertIn(">Скачать на сервер</button>", main)
        self.assertNotIn("кто подключился — видно через", main, "только что созданной группе рано про трафик")
        self.assertNotIn("подключились 0 из", main)

    def test_done_summary_labels_match_the_wizard_and_link_to_settings(self):
        loc = self.made(mode="admin")
        _, body = self.c.get(loc)
        card = body[body.index("Группа «Офис» готова"):body.index("Раздать доступы")]
        text = text_of(card)
        for label in ("Протоколы:", "Приложения:", "Через VPN:", "Ставит:"):
            self.assertIn(label, text, label)
        self.assertIn("Ставит: ИТ", text)
        self.assertIn("Через VPN: общий список (Android 2 · Windows 2)", text, "числа видны сразу")
        self.assertRegex(card, r'<a href="/groups/g1#settings" data-swap>изменить →</a>')
        self.assertIn("Hysteria2", text)
        self.assertNotIn("ставит ИТ", text)
        loc = self.made(name="Дом", mode="self", users_new="Петя")
        _, body = self.c.get(loc)
        self.assertIn("Ставит: сами", text_of(body))

    # --- кнопки и предпросмотр ---

    def test_preview_actions_are_a_sticky_bar_inside_the_form_with_the_table(self):
        users_new = "\n".join(f"Человек {i}" for i in range(33))
        resp, body = self.wiz(3, go="create", name="Все", proto=["vless-reality"], devs="1", dev=["android"],
                              set__android="happ", users_new=users_new, allow_mode="common")
        self.assertEqual(resp.status, 200)
        form = body[body.index('<form method="post" action="/connect/new"'):]
        form = form[:form.index("</form>")]
        self.assertIn('<table class="preview', form, "таблица внутри формы: прилипание ограничено формой, а не только её панелью")
        self.assertEqual(form.count('class="wiz-nav"'), 1)
        self.assertGreater(form.index('class="wiz-nav"'), form.rindex("</table>"), "панель — после последней строки")
        self.assertIn(">Создать группу и 33 чел.</button>", form)
        self.assertIn(">← Изменить список</button>", form)
        self.assertRegex(form[form.index('class="wiz-nav"'):], r'class="btn primary">Создать группу')

    def test_buttons_count_moved_existing_users_too(self):
        users.add_user("masha")
        resp, body = self.wiz(3, go="create", name="Все", proto=["vless-reality"], devs="1", dev=["android"],
                              set__android="happ", users_new="Иван\nМария\nОльга", existing=["owner", "masha"],
                              allow_mode="common")
        self.assertEqual(resp.status, 200, text_of(body)[:300])
        self.assertIn(">Создать группу: 3 новых + 2 перевести</button>", body)
        self.assertNotIn("Создать группу и 3 чел.", body)
        resp, body = self.post("/groups/main/members", {"users_new": ["Петя"], "existing": ["owner"]})
        self.assertIn(">Добавить: 1 новый + 1 перевести</button>", body)
        form = body[body.index('<form method="post" action="/groups/main/members"'):]
        form = form[:form.index("</form>")]
        self.assertIn('<table class="preview', form)
        self.assertIn('class="wiz-nav"', form)
        self.assertNotIn('class="actions"', form, "на группе тоже липкая панель, а не .actions")

    # --- пресеты ---

    def test_self_start_recommends_the_clean_variant_only(self):
        _, body = self.c.get("/connect/new")
        simple, reliable = (re.search(rf'<strong>{n}</strong>(.*?)name="go" value="', body, re.S).group(1)
                            for n in ("Просто", "Надёжно"))
        self.assertIn("рекомендуем", reliable, "советуется вариант, где Android не только на UDP")
        self.assertNotIn("с оговорками", reliable)
        self.assertNotIn("рекомендуем", simple)
        self.assertIn("с оговорками", simple)
        self.assertIn("Android — только UDP", text_of(body))
        self.assertIn("VLESS XHTTP", simple)
        self.assertNotIn("Hysteria2", simple)
        self.assertIn("VLESS XHTTP", reliable)
        self.assertIn("AmneziaWG", reliable)

    def test_admin_start_is_the_same_allowlist_presets_without_hiddify(self):
        _, body = self.c.get("/connect/new?mode=admin")
        text = text_of(body)
        simple = text[text.index("Просто"):text.index("Надёжно")]
        reliable = text[text.index("Надёжно"):text.index("Свой набор")]
        self.assertIn("AmneziaWG — Android · INCY — iPhone · v2rayN — Windows", simple)
        self.assertIn("рекомендуем", reliable, "у «Просто» Android только на UDP (D59)")
        self.assertNotIn("Hiddify", text, "Hiddify ведёт через VPN всё устройство — в готовых вариантах его нет")
        self.assertEqual(text.count("Через VPN: Android, Windows — только приложения из списка · iPhone — всё, кроме "
                                    "российских сайтов"), 1, "у обоих вариантов одинаково — одна строка")
        self.assertEqual(len(re.findall(r'class="btn primary">Выбрать</button>', body)), 1)

    def test_admin_simple_carries_the_preset_apps_to_the_apps_step(self):
        resp, body = self.wiz(0, go="simple", mode="admin")
        self.assertEqual(resp.status, 200)
        self.assertEqual(re.findall(r'type="hidden" name="proto" value="([^"]+)"', body),
                         ["vless-xhttp", "amneziawg", "vless-reality"])
        for plat, ids in (("android", ["amneziawg"]), ("ios", ["incy"]), ("windows", ["v2rayn"])):
            self.assertEqual(self.checked(body, plat), ids, plat)
        heads = self.heads(body)
        self.assertNotIn("Apple ID", heads["iPhone"], "INCY есть в App Store РФ")
        self.assertEqual(body.count("iPhone: приложение ставится только из App Store."), 1, "на шаге «Приложения» — один раз")

    def test_ios_note_once_per_screen_on_group_page(self):
        self.made(mode="admin")
        _, page = self.c.get("/groups/g1")
        self.assertEqual(page.count("iPhone: приложение ставится только из App Store."), 1,
                         "блок «Дистрибутивы», не строка устройства")
        self.assertEqual(page.count("Как установить (для ИТ)"), 1)
        card = page[page.index("<h3>Дистрибутивы</h3>"):]
        self.assertNotIn("iPhone —", card[:card.index("</section>")], "в памятке для ИТ про iPhone ничего нет")

    # --- инструкция ИТ на iPhone ---

    def test_admin_iphone_text_says_foreign_apple_id_instead_of_installed(self):
        self.made(mode="admin")
        _, page = self.c.get("/users/ivan-petrov")
        ios = self.msg(page, "ios")
        self.assertTrue(ios.startswith("Иван Петров. Установите «Happ» с иностранного Apple ID."), ios)
        self.assertNotIn("VPN уже установлен", ios)
        android = self.msg(page, "android")
        self.assertTrue(android.startswith("Иван Петров, VPN уже установлен. Включите его в «Happ»."), android)
        windows = self.msg(page, "windows")
        self.assertIn("VPN уже установлен", windows)
        self.assertIn("нужен иностранный Apple ID", page[page.index('data-pp="ios"'):page.index('id="msg-ios"')])
        _, cards = self.c.get("/handoff?group=g1")
        self.assertIn("нужен иностранный Apple ID", cards)
        self.assertNotIn("нет в App Store РФ", cards)

    def test_self_iphone_text_keeps_the_install_step(self):
        self.made(mode="self", proto=["vless-xhttp"], set__ios="incy", set__android="happ")
        _, page = self.c.get("/users/ivan-petrov")
        ios = self.msg(page, "ios")
        self.assertIn("Установите «INCY»", ios)
        self.assertNotIn("Apple ID", ios)


class ClientsAndListsTest(GroupWebBase):
    def test_clients_page_uses_the_wizard_words(self):
        _, body = self.c.get("/clients")
        self.assertNotIn("✓!", body)
        self.assertNotIn("! с оговоркой", body)
        self.assertNotIn("Почему ✕ и !", body)
        self.assertIn("с оговоркой", body)
        self.assertIn(">нет в App Store РФ</span>", body)
        for gone in ("SFA", "SFI"):
            self.assertNotIn(gone, body, "в заметках — sing-box")
        self.assertRegex(body, r'<a href="/connect/new" data-swap>Что ставить группе')

    def test_groups_list_orders_protocols_by_priority(self):
        self.env.add_protocol("vless-xhttp")
        groups.update("main", protocols=["vless-reality", "amneziawg", "hysteria2", "vless-xhttp"])
        resp, body = self.c.get("/groups")
        row = body[body.index('href="/groups/main"'):]
        names = re.findall(r'<span class="chip">([^<]+)</span>', row[:row.index("</tr>")])
        self.assertEqual(names[:4], ["Hysteria2", "VLESS XHTTP", "AmneziaWG", "VLESS Vision"])
        groups.update("main", protocols=["*"])
        _, body = self.c.get("/groups")
        row = body[body.index('href="/groups/main"'):]
        names = re.findall(r'<span class="chip">([^<]+)</span>', row[:row.index("</tr>")])
        self.assertEqual(names[:4], ["Hysteria2", "VLESS XHTTP", "AmneziaWG", "VLESS Vision"], "«все включённые» — тоже по PRIORITY")


class ProtocolRowsTest(GroupWebBase):
    """Шаг 1 и страница группы: все семь протоколов сервера — отдельными строками."""

    def setUp(self):
        super().setUp()
        for pid in ("vless-xhttp", "tuic"):
            self.env.add_protocol(pid)
        self.env.add_manifest("hysteria2-obfs", layer="udp", users_backend="hysteria-command", engine="hysteria",
                              name="Hysteria2 + Salamander", short="HY2 + Salamander")
        self.env.add_manifest("ss2022", enabled=False, name="Shadowsocks-2022", short="SS-2022")

    def rows(self, body):
        return re.findall(r'<label class="opt(?: off)?">.*?</label>|<div class="opt off">.*?</div></div>', body, re.S)

    def test_wizard_lists_all_seven(self):
        _, body = self.step1()
        titles = [re.sub(r"<[^>]+>", " ", r) for r in self.rows(body)]
        text = " ".join(titles)
        for name in ("VLESS Vision", "VLESS XHTTP", "Hysteria2", "Hysteria2 + Salamander", "AmneziaWG", "TUIC", "Shadowsocks"):
            self.assertIn(name, text, name)
        for gone in ("HY2", "SS-2022", "Протокол ", "REALITY", "3.1", "v5"):
            self.assertNotIn(gone, text, "одно название протокола на всех экранах")
        self.assertNotIn('title="Протокол vless-reality"', body, "подсказка с названием протокола ничего не добавляет")
        self.assertEqual(len(self.rows(body)), 7)

    def test_salamander_is_an_independent_row(self):
        _, body = self.step1()
        hy = re.search(r'<input type="checkbox" name="proto" value="hysteria2" checked>', body)
        sal = re.search(r'<input type="checkbox" name="proto" value="hysteria2-obfs">', body)
        self.assertTrue(hy and sal, "своя строка со своим значением; предвыбор — только Hysteria2")
        self.assertLess(hy.start(), sal.start(), "порядок строк — по приоритету")
        row = body[sal.start():]
        row = row[:row.index("</label>")]
        self.assertIn("Hysteria2 + Salamander", row)
        for gone in ("снимается и отмечается", "Общая учётка", "основной", "disabled", "data-variant"):
            self.assertNotIn(gone, row)
        self.assertNotIn("data-variant", body)
        # выключенный — серой строкой, без галочки
        off = body[body.index('class="opt off"'):]
        self.assertIn("Shadowsocks", off)
        self.assertNotIn('value="ss2022"', body)

    @staticmethod
    def submitted(body):
        """Что отправит браузер: отмеченные чекбоксы proto, как нарисованы сервером."""
        tags = re.findall(r'<input type="checkbox" name="proto"[^>]*>', body)
        return [re.search(r'value="([^"]+)"', tag).group(1) for tag in tags if " checked" in tag]

    def test_either_one_or_both_are_selectable(self):
        for chosen in (["hysteria2"], ["hysteria2-obfs"], ["hysteria2", "hysteria2-obfs"]):
            resp, body = self.wiz(1, name="Семья", proto=chosen)
            self.assertEqual(resp.status, 200, chosen)
            self.assertEqual(sorted(re.findall(r'type="hidden" name="proto" value="([^"]+)"', body)), sorted(chosen))
            resp, body = self.wiz(2, go="back", name="Семья", proto=chosen, custom="1")
            self.assertEqual(sorted(self.submitted(body)), sorted(chosen), "шаг 1 показывает ровно выбранное")
        resp, body = self.wiz(1, name="Семья", proto=["hysteria2", "hysteria2", "amneziawg"])
        self.assertEqual(body.count('type="hidden" name="proto" value="hysteria2"'), 1)

    def test_obfs_only_group_is_created_with_hysteria2_credentials(self):
        resp, _ = self.create_group(proto=["hysteria2-obfs"], client__android="v2rayng", users_new="masha")
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.groups_json()[1]["protocols"], ["hysteria2-obfs"])
        self.assertEqual(users.list_users().get("masha").protocols, ["hysteria2"], "учётка — модуля Hysteria2")
        _, page = self.c.get("/groups/g1")
        self.assertEqual(self.submitted(page), ["hysteria2-obfs"])
        self.assertIn("Hysteria2 + Salamander", text_of(self.c.get("/groups")[1]))

    def test_step1_and_group_page_have_no_primary_or_reserve_wording(self):
        _, body = self.step1()
        for gone in ("запасные", "Первый отмеченный", "Подсказки без цифр", "обычно 2–3", "ориентир"):
            self.assertNotIn(gone, text_of(body))
        self.assertEqual(self.rows(body)[0].count("badge"), 0)
        _, page = self.c.get("/groups/main")
        for gone in ("Первый отмеченный", "Подсказки без цифр", "запасные", "ориентир"):
            self.assertNotIn(gone, text_of(page))

    def test_off_without_hysteria2_no_salamander_row(self):
        (self.env.etc / "protocols.d" / "hysteria2.json").unlink()
        _, body = self.step1()
        self.assertNotIn("hysteria2-obfs", body)

    def test_group_page_has_the_same_rows(self):
        _, body = self.c.get("/groups/main")
        self.assertIn('name="proto" value="hysteria2-obfs"', body)
        self.assertIn("Shadowsocks", body)

    def test_js_does_not_link_rows(self):
        from zoolib.web import assets
        self.assertNotIn("o.value === box.value", assets.JS)
        self.assertIn("[data-picker]", assets.JS)


if __name__ == "__main__":
    unittest.main()
