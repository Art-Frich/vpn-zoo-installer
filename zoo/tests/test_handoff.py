import csv
import io
import re
import time
import unittest
import zipfile
from unittest import mock

from tests.test_groupviews import GroupWebBase, text_of
from tests.test_web import Client, header
from zoolib import paths, protolib, qr, traffic, users
from zoolib.web import handoffviews, userviews

PNG = bytes([0x89]) + b"PNG fake "
OFFICE = ["Иванов Иван", "Петрова Анна", "Сидоров Пётр", "Смирнова Ольга"]


def fake_png(text, size=8):
    return PNG + text.encode("utf-8")[:12]


class Base(GroupWebBase):
    def setUp(self):
        super().setUp()
        for target, fn in (("png", fake_png), ("svg", lambda text, size=6: "<svg>" + text[:5] + "</svg>")):
            m = mock.patch.object(qr, target, side_effect=fn)
            m.start()
            self.addCleanup(m.stop)

    def reg(self):
        return {u["name"]: u for u in self.env.users_json()["users"]}

    def zip_of(self, path_or_form, **form):
        form.setdefault("fmt", "zip")
        resp, _ = self.post("/handoff/export", {k: v if isinstance(v, list) else [v] for k, v in form.items()})
        self.assertEqual(resp.status, 200, resp.body[:200])
        return resp, zipfile.ZipFile(io.BytesIO(resp.body))

    def seed_traffic(self, name, up=100, down=200):
        con = traffic.connect()
        try:
            with con:
                traffic.store(con, [traffic.Delta("xray", name, up, down)], {}, int(time.time()))
        finally:
            con.close()


class BulkCreateTest(Base):
    def wizard_text(self, text, **kw):
        base = dict(name="Офис", proto=["vless-reality", "amneziawg"], client__android="happ", users_new=text,
                    existing=[], allow_mode="common")
        base.update(kw)
        return self.wiz(3, go="create", **base)

    def test_wizard_previews_then_creates_whole_office(self):
        names = OFFICE * 8 + ["Кузнецов Алексей"]
        text = "\n".join(f"{n}; офис" for n in names)
        resp, body = self.wizard_text(text)
        self.assertEqual(resp.status, 200)
        self.assertIn("Будет создано: 33", body)
        self.assertIn("Совпали имена, добавлен номер: 28", body)
        self.assertIn("<code>ivanov-ivan</code>", body)
        self.assertIn("<code>ivanov-ivan-2</code>", body)
        self.assertIn("<strong>Иванов Иван</strong>", body, "имя — как написано, логин — отдельной колонкой")
        head = re.search(r"<thead>(.*?)</thead>", body, re.S).group(1)
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", head), ["№", "Имя", "Логин", "Заметка", ""])
        self.assertNotIn("латиницей</span>", body, "серых чипов «латиницей» нет: про латиницу сказано один раз в сводке")
        self.assertEqual(body.count("Логин — имя латиницей"), 1)
        self.assertIn("row-warn", body, "совпавшие имена подсвечены")
        self.assertIn("повтор в списке", body)
        self.assertIn("Создать группу и 33 чел.", body)
        self.assertEqual(body.count("<tr class="), 28)
        self.assertIn('name="confirm" value="1"', body)
        self.assertEqual(len(self.reg()), 1, "предпросмотр ничего не создаёт")
        self.assertEqual([g["id"] for g in self.groups_json()], ["main"])
        # создание — одна блокировка на всех, один итог
        real = users._lock
        with mock.patch.object(users, "_lock", side_effect=real) as lock:
            resp, _ = self.wizard_text(text, confirm="1")
        self.assertEqual(resp.status, 303)
        self.assertLessEqual(lock.call_count, 4, "пачка — под одной блокировкой, не по одной на человека")
        reg = self.reg()
        self.assertEqual(len(reg), 34)
        self.assertEqual({u["group"] for n, u in reg.items() if n != "owner"}, {"g1"})
        self.assertEqual((reg["ivanov-ivan"]["note"], reg["ivanov-ivan"]["display"]), ("офис", "Иванов Иван"))
        self.assertEqual((reg["kuznetsov-aleksey"]["note"], reg["kuznetsov-aleksey"]["display"]), ("офис", "Кузнецов Алексей"))
        self.assertNotIn("display", reg["owner"])
        loc = header(resp, "Location")[0]
        _, page = self.c.get(loc)
        self.assertIn("создано: 33", text_of(page))
        self.assertIn("Совпали имена, добавлен номер: 28: Иванов Иван → ivanov-ivan-2", text_of(page))
        self.assertIn("Группа «Офис» готова: 33 чел.", text_of(page))
        self.assertIn("Карточки (печать, ZIP, CSV)", page)
        self.assertIn('href="/handoff?group=g1"', page)
        self.assertNotIn('name="conn-user"', page, "строка с QR на каждого не рисуется")

    def test_edit_returns_to_step_with_text(self):
        resp, body = self.wiz(3, go="edit", name="Офис", proto=["vless-reality"], client__android="happ",
                              users_new="Иван Петров; бух", existing=[], allow_mode="common")
        self.assertEqual(resp.status, 200)
        self.assertIn("<h3>Люди</h3>", body)
        self.assertRegex(body, r'<textarea[^>]*name="users_new"[^>]*>Иван Петров; бух</textarea>')

    def test_group_page_adds_list_with_preview(self):
        self.create_group(users_new="masha\nkolya")
        text = "Иван Петров; бух\nmasha; тёзка\nМария"
        resp, body = self.post("/groups/g1/members", {"users_new": [text]})
        self.assertEqual(resp.status, 200)
        self.assertIn("Проверьте список", body)
        self.assertIn("<code>masha-2</code>", body)
        self.assertIn("занято: добавлен номер", body)
        self.assertIn(">Добавить 3 чел.</button>", body)
        self.assertNotIn("ivan-petrov", self.reg())
        resp, body = self.post("/groups/g1/members", {"users_new": [text], "go": ["edit"]})
        self.assertEqual(resp.status, 200)
        self.assertIn("Иван Петров; бух", body)
        self.assertIn("Проверить список →", body)
        resp, _ = self.post("/groups/g1/members", {"users_new": [text], "confirm": ["1"]})
        self.assertEqual(resp.status, 303)
        reg = self.reg()
        for n in ("ivan-petrov", "masha-2", "mariya"):
            self.assertEqual(reg[n]["group"], "g1", n)
        self.assertEqual(reg["masha-2"]["note"], "тёзка")
        self.assertEqual((reg["ivan-petrov"]["note"], reg["ivan-petrov"]["display"]), ("бух", "Иван Петров"))
        _, page = self.c.get("/groups/g1")
        self.assertIn("создано: Иван Петров, masha, Мария", text_of(page), "во flash — имена из списка, не логины")
        self.assertNotIn("ivan-petrov,", text_of(page).split("создано:")[1].split(".")[0])
        self.assertIn("Совпали имена, добавлен номер: masha → masha-2", text_of(page))

    def test_group_page_only_existing_skips_preview(self):
        self.create_group(users_new="masha")
        resp, _ = self.post("/groups/g1/members", {"users_new": [""], "existing": ["owner"]})
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.reg()["owner"]["group"], "g1")

    def test_limits(self):
        self.create_group(users_new="masha")
        text = "\n".join(f"u{i}" for i in range(201))
        resp, page = self.post("/groups/g1/members", {"users_new": [text], "confirm": ["1"]})
        self.assertEqual(resp.status, 422, "список остаётся в форме")
        self.assertIn("не больше 200", page)
        self.assertNotIn("u0", self.reg())
        resp, body = self.wizard_text(text, confirm="1")
        self.assertEqual(resp.status, 422)
        self.assertIn("не больше 200", body)
        self.assertEqual([g["id"] for g in self.groups_json()], ["main", "g1"])

    def test_csrf_auth_and_origin(self):
        self.create_group(users_new="masha")
        before = self.reg()
        resp, _ = self.c.req("POST", "/groups/g1/members", {"csrf": "wrong", "users_new": "x", "confirm": "1"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.c.post("/groups/g1/members", {"users_new": "x", "confirm": "1"},
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, _ = Client(self.app).post("/groups/g1/members", {"users_new": "x"}, csrf=False)
        self.assertEqual(resp.status, 401)
        resp, _ = self.c.req("POST", "/connect/new", {"csrf": "wrong", "step": "3", "go": "create", "confirm": "1"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(before, self.reg())

    def test_one_failure_does_not_stop_the_rest_and_flash_is_short(self):
        self.create_group(users_new="masha")
        real = protolib.user_add

        def flaky(pid, name):
            if name.startswith("bad"):
                raise protolib.ProtoError(pid, "user_add", 1, "", "отказ модуля")
            return real(pid, name)

        text = "\n".join(["ok1", *[f"bad{i}" for i in range(9)], "ok2"])
        with mock.patch.object(protolib, "user_add", side_effect=flaky):
            resp, _ = self.post("/groups/g1/members", {"users_new": [text], "confirm": ["1"]})
        self.assertEqual(resp.status, 303)
        reg = self.reg()
        self.assertEqual((reg["ok1"]["group"], reg["ok2"]["group"]), ("g1", "g1"))
        self.assertFalse([n for n in reg if n.startswith("bad")])
        _, page = self.c.get("/groups/g1")
        self.assertIn("создано: ok1, ok2", text_of(page))
        self.assertIn("bad0", page)
        self.assertIn("…и ещё ошибок:", page)
        self.assertLessEqual(page.count('<li class="bad">'), 7)

    def test_time_budget_reports_the_rest(self):
        self.create_group(users_new="masha")
        reps = users.add_many([("t1", ""), ("t2", "")], "g1", budget=-1)
        self.assertEqual([r.ok for r in reps], [False, False])
        self.assertIn("не успели", reps[0].message)
        self.assertNotIn("t1", self.reg())

    def test_add_many_lists_each_protocol_once(self):
        self.create_group(users_new="masha")
        self.env.calls()
        before = len([c for c in self.env.calls() if c.endswith(" user_list")])
        users.add_many([(f"p{i}", "") for i in range(5)], "g1")
        lists = len([c for c in self.env.calls() if c.endswith(" user_list")]) - before
        self.assertLessEqual(lists, 2 * 2, "по одному user_list на протокол, а не на человека")


class DisplayNameTest(Base):
    """«Иван Петров; бухгалтерия»: к человеку обращаются по имени, логин — мелкой подписью."""

    def setUp(self):
        super().setUp()
        resp, _ = self.create_group(users_new="Иван Петров; бухгалтерия\nmasha")
        self.assertEqual(resp.status, 303)

    def test_card_instruction_and_files_use_the_name(self):
        _, body = self.c.get("/handoff?group=g1")
        card = body[body.index('data-name="ivan-petrov"'):]
        card = card[:card.index("</article>")]
        self.assertIn('<header class="hhead"><strong>Иван Петров</strong><span class="hnote">ivan-petrov</span>'
                      '<span class="hnote">бухгалтерия</span>', card)
        self.assertNotIn("Иван Петров · ", body)
        self.assertIn('<strong>masha</strong>', body, "без имени из списка — логин")
        _, z = self.zip_of("", group="g1")
        text = z.read("ivan-petrov/instruction.txt").decode("utf-8")
        self.assertTrue(text.startswith("Иван Петров (ivan-petrov) — бухгалтерия\nГруппа: Семья\n"), text[:90])
        rows = list(csv.reader(io.StringIO(z.read("index.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertEqual({r[0] for r in rows[1:]}, {"Иван Петров", "masha"})
        self.assertIn("ivan-petrov/qr-vless-vision.png", z.namelist(), "папка — по логину")
        _, user = self.c.get("/users/ivan-petrov")
        self.assertTrue(self.msg(user).startswith("Иван Петров, VPN на Android"))
        self.assertIn("<h1>Иван Петров</h1>", user)

    def test_group_members_and_list_show_name_and_login_once(self):
        _, page = self.c.get("/groups/g1")
        card = page.split("<h3>Участники</h3>")[1].split("Добавить людей списком")[0]
        self.assertEqual(card.count(">Иван Петров (ivan-petrov)<"), 2, "чип и строка выбора, без третьего повтора")
        _, lst = self.c.get("/groups")
        self.assertIn(">Иван Петров</a>", lst)
        self.assertIn('title="ivan-petrov"', lst)


class CardsPageTest(Base):
    def setUp(self):
        super().setUp()
        resp, _ = self.create_group(users_new="masha; сестра\nkolya")
        self.assertEqual(resp.status, 303)

    def test_cards_for_group(self):
        resp, body = self.c.get("/handoff?group=g1")
        self.assertEqual(resp.status, 200)
        self.assertIn("Карточки для раздачи", body)
        self.assertEqual(len(re.findall(r'<article class="hcard"', body)), 2)
        for n in ("masha", "kolya"):
            self.assertIn(f'data-name="{n}"', body)
            self.assertIn(f'src="/users/{n}/qr/', body, "QR именно этого человека")
        self.assertIn("сестра", body)
        self.assertIn("Установите «Happ»", body)
        self.assertNotIn("{name}", body)
        self.assertNotIn("я пришлю", body)
        self.assertIn("камеру на QR с этой карточки", body)
        self.assertIn('class="hcards per-3"', body)
        self.assertIn("data-print", body)
        self.assertIn("не отправляйте их через MAX и VK", body)
        self.assertIn("data-expanded", body, "живое обновление не перерисовывает страницу при печати")
        self.assertRegex(body, r'<button type="submit" class="btn">Скачать ZIP</button>')
        _, body2 = self.c.get("/handoff?group=g1&per=2")
        self.assertIn('class="hcards per-2"', body2)
        _, body3 = self.c.get("/handoff?group=g1&per=9")
        self.assertIn('class="hcards per-3"', body3, "чужое значение отбрасывается")

    def test_same_app_on_several_devices_is_one_block(self):
        self.post("/groups/g1", {"name": ["Семья"], "proto": ["hysteria2"], "client:android": ["hiddify"],
                                "client:ios": ["hiddify"], "client:windows": ["hiddify"], "client:macos": ["hiddify"]})
        _, body = self.c.get("/handoff?group=g1")
        # у телефонов свои магазины и шаги, у компьютеров — свой файл релиза: блоки не сливаются
        blocks = dict(re.findall(r'<h4 class="plat-title">([^<]+)</h4>(.*?)</section>', body, re.S)[:4])
        self.assertEqual(sorted(blocks), ["Android", "Windows", "iPhone", "macOS"])
        self.assertIn("apps.microsoft.com/detail/9pdfnl3qv2s5", blocks["Windows"], "на Windows у Hiddify есть Microsoft Store")
        self.assertIn("файл «.dmg»", blocks["macOS"])
        self.assertIn("App Store", blocks["iPhone"])
        self.assertIn("нет в App Store РФ", blocks["iPhone"])
        self.assertNotIn("App Store", blocks["Android"])
        self.assertIn("Google Play", blocks["Android"])
        self.assertIn("Прокси для приложений", blocks["Android"])
        self.assertNotIn("Прокси для приложений", blocks["iPhone"] + blocks["Windows"] + blocks["macOS"])
        # люди ставят сами: на Android — из Google Play, а не APK с GitHub (D49)
        self.assertRegex(blocks["Android"], r"Установите «Hiddify»[^<]*play\.google\.com")
        self.assertIn('src="/users/masha/qr/', blocks["Android"] + blocks["iPhone"], "на телефоне — QR")

    def test_long_link_is_printed_whole(self):
        uri = "hysteria2://" + "a" * 64 + "@1.2.3.4:443/?sni=x&insecure=1&pinSHA256=" + "b" * 64 + "#main"
        html_ = str(handoffviews._key_html(handoffviews.CardKey("HY2", uri=uri), "masha"))
        self.assertIn(uri.replace("&", "&amp;"), html_, "обрезанная ссылка на печати не работает")
        self.assertNotIn("…", html_)

    def test_selection_by_names_is_checked_against_registry(self):
        users.add_user("lena", group="g1")
        users.add_user("off", group="g1")
        users.set_enabled("off", False)
        resp, body = self.c.get("/handoff?u=masha,ghost,zoo-probe,off,BAD%20NAME,masha,lena")
        self.assertEqual(resp.status, 200)
        self.assertEqual(sorted(re.findall(r'data-name="([^"]+)"', body)), ["lena", "masha"])
        self.assertIn("«ghost» нет в реестре", body)
        self.assertIn("отключённые не включены: off", body)
        self.assertNotIn("zoo-probe</strong>", body)
        resp, body = self.c.get("/handoff?u=" + ",".join(f"u{i}" for i in range(201)))
        self.assertEqual(resp.status, 200, "лишние имена не из реестра отбрасываются")
        resp, _ = self.c.get("/handoff?group=nope")
        self.assertEqual(resp.status, 404)
        resp, body = self.c.get("/handoff?u=ghost")
        self.assertIn("Раздавать некому", body)

    def test_more_than_limit_is_refused(self):
        names = [f"w{i}" for i in range(handoffviews.HANDOFF_MAX + 1)]
        reg = users.Registry.load()
        for n in names:
            reg.users.append(users.User(n, enabled=True, protocols=["vless-reality"], group="g1"))
        reg.save()
        resp, body = self.c.get("/handoff?group=g1")
        self.assertEqual(resp.status, 422)
        self.assertIn("не больше 200", body)

    def test_status_connected_by_traffic(self):
        _, body = self.c.get("/handoff?group=g1")
        self.assertIn("подключения по трафику не видны", body, "нет данных — не врём «ещё нет»")
        self.assertNotIn(">ещё нет<", body)
        self.seed_traffic("masha")
        _, body = self.c.get("/handoff?group=g1")
        self.assertIn("подключились 1 из 2", body)
        self.assertRegex(body, r'class="chip ok" title="[^"]*">подключился</span>')
        self.assertIn(">ещё нет</span>", body)
        _, body = self.c.get("/handoff?group=g1&only=pending")
        self.assertEqual(re.findall(r'data-name="([^"]+)"', body), ["kolya"])
        _, page = self.c.get("/groups/g1")
        self.assertIn("подключились 1 из 2", text_of(page))
        self.assertIn('<a href="/users/masha" class="chip ok" title="уже подключился">masha</a>', page)
        self.assertIn('<a href="/users/kolya" class="chip">kolya</a>', page)
        self.assertIn("Ещё не подключились: 1", page)
        self.assertIn('href="/handoff?group=g1"', page)

    def test_status_by_last_seen(self):
        self.seed_traffic("owner")
        with mock.patch.object(traffic, "last_seen", return_value={"kolya": int(time.time()) - 86400,
                                                                  "masha": int(time.time()) - 40 * 86400}):
            st = handoffviews.connection(["masha", "kolya", "x"])
        self.assertEqual(st.on, {"kolya"})
        self.assertEqual(st.counter, "подключились 1 из 3")

    def test_pending_only_when_all_connected(self):
        self.seed_traffic("masha")
        self.seed_traffic("kolya")
        _, body = self.c.get("/handoff?group=g1&only=pending")
        self.assertIn("все уже подключились", body)

    def test_bulk_bar_hands_off_selected(self):
        _, page = self.c.get("/users")
        self.assertIn('name="action" value="handoff"', page)
        resp, _ = self.post("/users/bulk", {"action": ["handoff"], "names": ["masha", "ghost", "zoo-probe", "kolya"]})
        self.assertEqual(resp.status, 303)
        loc = header(resp, "Location")[0]
        self.assertEqual(loc, "/handoff?u=masha%2Ckolya")
        resp, body = self.c.get(loc)
        self.assertEqual(sorted(re.findall(r'data-name="([^"]+)"', body)), ["kolya", "masha"])
        resp, _ = self.post("/users/bulk", {"action": ["handoff"], "names": ["ghost"]})
        self.assertEqual(header(resp, "Location"), ["/users"])
        _, page = self.c.get("/users")
        self.assertIn("Раздавать некому", page)

    def test_slow_links_leave_pending_cards(self):
        real = userviews._cached_links

        def slow(app, name):
            if name == "kolya":
                time.sleep(0.6)
            return real(app, name)

        with mock.patch.object(userviews, "_cached_links", side_effect=slow):
            t0 = time.monotonic()
            cards = handoffviews.build_cards(self.app, ["masha", "kolya"], 0.1)
            self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual([(c.user.name, c.pending) for c in cards], [("masha", False), ("kolya", True)])
        self.assertTrue(cards[0].blocks)

    def test_auth_csrf_origin(self):
        anon = Client(self.app)
        resp, _ = anon.get("/handoff?group=g1")
        self.assertEqual(resp.status, 303)
        self.assertTrue(header(resp, "Location")[0].startswith("/login"))
        resp, _ = anon.post("/handoff/export", {"group": "g1", "fmt": "zip"}, csrf=False)
        self.assertEqual(resp.status, 401)
        resp, _ = self.c.req("POST", "/handoff/export", {"csrf": "wrong", "group": "g1", "fmt": "zip"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.c.post("/handoff/export", {"group": "g1", "fmt": "zip"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.c.get("/handoff/export")
        self.assertEqual(resp.status, 405, "ключи — только POST")

    def test_note_and_name_are_escaped(self):
        users.add_user("evil", note='<script>alert(1)</script> "><img src=x onerror=alert(2)>', group="g1")
        _, body = self.c.get("/handoff?group=g1")
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;script&gt;alert(1)", body)


class ExportTest(Base):
    def setUp(self):
        super().setUp()
        resp, _ = self.create_group(users_new="masha; сестра\nkolya")
        self.assertEqual(resp.status, 303)

    def test_zip_has_folder_per_person(self):
        resp, z = self.zip_of("", group="g1")
        self.assertEqual(resp.content_type, "application/zip")
        disp = header(resp, "Content-Disposition")[0]
        self.assertRegex(disp, r'^attachment; filename="vpn-g1-\d{8}\.zip"$')
        self.assertIn(("Cache-Control", "no-store"), resp.headers)
        names = sorted(z.namelist())
        self.assertEqual({n.split("/")[0] for n in names}, {"README.txt", "index.csv", "masha", "kolya"})
        for n in ("masha", "kolya"):
            self.assertIn(f"{n}/instruction.txt", names)
            self.assertTrue([x for x in names if x.startswith(f"{n}/qr-") and x.endswith(".png")], n)
        self.assertEqual(z.testzip(), None)
        info = z.getinfo("masha/instruction.txt")
        self.assertEqual(info.external_attr >> 16, 0o600)
        text = z.read("masha/instruction.txt").decode("utf-8")
        self.assertTrue(text.startswith("masha — сестра\nГруппа: Семья\n"), text[:80])
        self.assertIn("Установите «Happ»", text)
        self.assertIn("ссылка: vless://masha@10.0.0.1:443", text)
        self.assertIn("QR: qr-", text)
        self.assertIn("не отправляйте через MAX и VK", text)
        self.assertNotIn("{name}", text)
        self.assertIn("masha/qr-vless-vision.png", names)
        self.assertNotIn("я пришлю", text, "ключ уже у человека: «пришлю» на карточке и в папке не нужно")
        self.assertIn("камеру на QR из этой папки", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("kolya", text, "в инструкции — ключи только этого человека")
        png = [x for x in names if x.startswith("masha/qr-")][0]
        self.assertTrue(z.read(png).startswith(PNG))
        readme = z.read("README.txt").decode("utf-8")
        self.assertIn("не отправляйте их через MAX и VK", readme)

    def test_index_csv_columns(self):
        _, z = self.zip_of("", group="g1")
        raw = z.read("index.csv")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 с BOM: Excel читает кириллицу")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0], ["имя", "заметка", "протокол", "ссылка"])
        body = {(r[0], r[2]): r for r in rows[1:]}
        key = ("masha", "VLESS Vision")
        self.assertIn(key, body)
        self.assertEqual(body[key][1], "сестра")
        self.assertTrue(body[key][3].startswith("vless://masha@"))
        self.assertEqual(len({r[0] for r in rows[1:]}), 2)

    def test_conf_file_and_awg_qr(self):
        resp, _ = self.create_group(name="Склад", proto=["amneziawg"], client__android="wgtunnel", users_new="lena",
                                    confirm="1")
        self.assertEqual(resp.status, 303)
        _, z = self.zip_of("", group="g2")
        names = z.namelist()
        self.assertIn("lena/amneziawg.conf", names)
        self.assertEqual(z.read("lena/amneziawg.conf").decode("utf-8").strip(), "[Interface]")
        self.assertTrue([n for n in names if n.startswith("lena/qr-") and n.endswith(".png")])
        self.assertNotIn("секрет", z.read("lena/amneziawg.conf").decode("utf-8"))
        self.assertNotIn("amneziawg.key", " ".join(names), "ключевые файлы модулей не выдаются")
        rows = list(csv.reader(io.StringIO(z.read("index.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertIn("lena/amneziawg.conf", [r[3] for r in rows])
        self.assertIn("файл: amneziawg.conf", z.read("lena/instruction.txt").decode("utf-8"))

    def test_v2rayn_rules_file_goes_into_zip(self):
        resp, _ = self.create_group(name="Надёжно", proto=["vless-reality"], client__windows="v2rayn",
                                    users_new="ivan", confirm="1")
        self.assertEqual(resp.status, 303)
        rules = paths.clients_dir() / "ivan" / "v2rayn-routing.json"
        self.assertTrue(rules.is_file())
        _, z = self.zip_of("", group="g2")
        self.assertIn("ivan/v2rayn-routing.json", z.namelist())
        self.assertEqual(z.read("ivan/v2rayn-routing.json"), rules.read_bytes())
        text = z.read("ivan/instruction.txt").decode("utf-8")
        self.assertIn("файл: v2rayn-routing.json", text)
        rows = list(csv.reader(io.StringIO(z.read("index.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertTrue(set(r[3] for r in rows[1:] if not r[3].startswith(("vless://", "hysteria2://")))
                        <= set(z.namelist()), "строки CSV с файлами указывают только на то, что в архиве")
        rules.unlink()
        _, z = self.zip_of("", group="g2")
        self.assertNotIn("ivan/v2rayn-routing.json", z.namelist())
        rows = list(csv.reader(io.StringIO(z.read("index.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertNotIn("ivan/v2rayn-routing.json", [r[3] for r in rows])

    def test_per_app_step_uses_group_list(self):
        resp, _ = self.post("/groups/g1", {"name": ["Семья"], "proto": ["vless-reality", "amneziawg"],
                                            "allow_mode": ["own"], "android": ["com.whatsapp"],
                                            "windows": ["Discord.exe"], "client:android": ["happ"]})
        self.assertEqual(resp.status, 303)
        _, z = self.zip_of("", group="g1")
        text = z.read("masha/instruction.txt").decode("utf-8")
        self.assertIn("отметьте WhatsApp.", text)
        self.assertNotIn("Brave", text, "Brave нет в списке группы")
        _, body = self.c.get("/handoff?group=g1")
        self.assertIn("отметьте WhatsApp.", body)

    def test_failed_qr_reported_once_per_key(self):
        resp, _ = self.create_group(name="Везде", proto=["hysteria2"], client__android="hiddify",
                                    client__ios="hiddify", client__windows="hiddify", users_new="ivan", confirm="1")
        self.assertEqual(resp.status, 303)
        with mock.patch.object(qr, "png", side_effect=qr.QrError("нет")), \
                mock.patch.object(qr, "svg", side_effect=qr.QrError("нет qrencode")):
            _, z = self.zip_of("", group="g2")
        readme = z.read("README.txt").decode("utf-8")
        self.assertEqual(readme.count("ivan: QR «Hysteria2» не построен"), 1, readme)
        text = z.read("ivan/instruction.txt").decode("utf-8")
        self.assertEqual(text.count("  Hysteria2\n"), 1, text)

    def test_png_falls_back_to_svg_then_skips(self):
        with mock.patch.object(qr, "png", side_effect=qr.QrError("нет PNG")):
            _, z = self.zip_of("", group="g1")
        names = z.namelist()
        self.assertTrue([n for n in names if n.startswith("masha/qr-") and n.endswith(".svg")])
        self.assertFalse([n for n in names if n.endswith(".png")])
        with mock.patch.object(qr, "png", side_effect=qr.QrError("нет")), \
                mock.patch.object(qr, "svg", side_effect=qr.QrError("нет qrencode")):
            _, z = self.zip_of("", group="g1")
        names = z.namelist()
        self.assertFalse([n for n in names if n.startswith("masha/qr-")])
        self.assertIn("ссылка: vless://", z.read("masha/instruction.txt").decode("utf-8"), "ссылка остаётся и без QR")
        self.assertIn("QR", z.read("README.txt").decode("utf-8"))

    def test_csv_download_and_formula_guard(self):
        users.add_user("evil", note="=HYPERLINK(\"http://x\")", group="g1")
        users.add_user("plus", note="+1 2 3", group="g1")
        resp, _ = self.post("/handoff/export", {"group": ["g1"], "fmt": ["csv"]})
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.content_type.startswith("text/csv"))
        self.assertRegex(header(resp, "Content-Disposition")[0], r'filename="vpn-g1-\d{8}\.csv"')
        rows = list(csv.reader(io.StringIO(resp.body.decode("utf-8-sig")), delimiter=";"))
        notes = {r[0]: r[1] for r in rows[1:]}
        self.assertEqual(notes["evil"], "'=HYPERLINK(\"http://x\")")
        self.assertEqual(notes["plus"], "'+1 2 3")
        self.assertEqual(rows[0], ["имя", "заметка", "протокол", "ссылка"])

    def test_selection_export_and_validation(self):
        resp, z = self.zip_of("", u="masha,ghost,zoo-probe")
        self.assertEqual({n.split("/")[0] for n in z.namelist()} - {"README.txt", "index.csv"}, {"masha"})
        self.assertIn("«ghost» нет в реестре", z.read("README.txt").decode("utf-8"))
        resp, _ = self.post("/handoff/export", {"u": ["ghost"], "fmt": ["zip"]})
        self.assertEqual(resp.status, 303)
        resp, _ = self.post("/handoff/export", {"group": ["g1"], "fmt": ["exe"]})
        self.assertEqual(resp.status, 303)
        resp, _ = self.post("/handoff/export", {"group": ["nope"], "fmt": ["zip"]})
        self.assertEqual(resp.status, 303)
        self.assertEqual(header(resp, "Location"), ["/groups"])

    def test_owner_keys_never_handed_out(self):
        users.add_user("vera")
        self.assertEqual(self.reg()["owner"]["group"], "main")
        sel = handoffviews.select("main", "", "")
        self.assertEqual(sel.names, ["vera"])
        self.assertEqual(handoffviews.select("", "owner,vera", "").names, ["vera"])
        self.assertEqual(handoffviews.select("", "owner,vera", "").query["u"], "vera")
        _, z = self.zip_of("", group="main")
        self.assertEqual({n.split("/")[0] for n in z.namelist()} - {"README.txt", "index.csv"}, {"vera"})
        self.assertNotIn("owner", z.read("index.csv").decode("utf-8"))
        resp, _ = self.post("/handoff/export", {"group": ["main"], "fmt": ["csv"]})
        self.assertEqual(resp.status, 200)
        self.assertNotIn(b"owner", resp.body)
        _, body = self.c.get("/handoff?group=main")
        self.assertNotIn('data-name="owner"', body)

    def test_zip_names_cannot_escape(self):
        _, z = self.zip_of("", group="g1")
        for n in z.namelist():
            self.assertFalse(n.startswith("/") or ".." in n.split("/") or "\\" in n, n)

    def test_time_and_size_limits(self):
        cards = handoffviews.build_cards(self.app, ["masha", "kolya"], 30)
        data = handoffviews.build_zip(self.app, cards, [], budget=-1)
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(sorted(z.namelist()), ["README.txt", "index.csv"])
        self.assertIn("Не вошли (не хватило времени, повторите): masha, kolya", z.read("README.txt").decode("utf-8"))
        with mock.patch.object(handoffviews, "ZIP_MAX", 10):
            z = zipfile.ZipFile(io.BytesIO(handoffviews.build_zip(self.app, cards, [])))
        readme = z.read("README.txt").decode("utf-8")
        self.assertIn("Не вошли", readme)
        self.assertIn("kolya", readme)

    def test_pending_cards_are_reported_not_exported(self):
        cards = handoffviews.build_cards(self.app, ["masha", "kolya"], 30)
        cards[1].pending = True
        z = zipfile.ZipFile(io.BytesIO(handoffviews.build_zip(self.app, cards, [])))
        self.assertNotIn("kolya/instruction.txt", z.namelist())
        self.assertIn("kolya: ссылок нет (не успели собрать)", z.read("README.txt").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
