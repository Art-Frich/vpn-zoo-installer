import json
import os
import re
import shutil
import subprocess
import unittest
from unittest import mock

from tests.helpers import BASH, REPO, ZooEnv, needs_bash
from tests.test_cli import run_cli
from tests.test_web import AppTestBase, header, visible_words
from zoolib import allowlist, paths, protolib, users

JQ = shutil.which("jq")


class AllowlistEnvTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def registry(self):
        return json.loads(paths.allowlist_file().read_text(encoding="utf-8"))


class RegistryTest(AllowlistEnvTest):
    def test_defaults_without_registry(self):
        al = allowlist.Allowlist.load()
        self.assertFalse(al.exists)
        self.assertEqual(al.android, ["com.brave.browser", "org.telegram.messenger"])
        self.assertEqual(al.windows, ["brave.exe", "Telegram.exe"])
        self.assertEqual(al.effective("android", "masha"), al.android)
        self.assertTrue(allowlist.ensure_file())
        self.assertFalse(allowlist.ensure_file())
        data = self.registry()
        self.assertEqual(data["schema"], allowlist.SCHEMA)
        self.assertEqual(data["android"], al.android)
        if os.name == "posix":
            self.assertEqual(paths.allowlist_file().stat().st_mode & 0o777, 0o600)

    def test_catalog_ids_valid_and_unique(self):
        for p in allowlist.PLATFORMS:
            ids = [getattr(a, p) for a in allowlist.CATALOG if getattr(a, p)]
            self.assertEqual(len(ids), len(set(ids)), p)
            for i in ids:
                self.assertTrue(allowlist.valid(p, i), i)
        for p, ids in allowlist.defaults().items():
            for i in ids:
                self.assertTrue(allowlist.title_of(p, i), f"{i} из пресета есть в каталоге")

    def test_resolve(self):
        self.assertEqual(allowlist.resolve(["brave"]), [("android", "com.brave.browser"), ("windows", "brave.exe")])
        self.assertEqual(allowlist.resolve(["Brave"], "windows"), [("windows", "brave.exe")])
        self.assertEqual(allowlist.resolve(["com.whatsapp", "Foo Bar.exe"]),
                         [("android", "com.whatsapp"), ("windows", "Foo Bar.exe")])
        for bad, plat in (("youtube", "windows"), ("rm -rf", None), ("com", None), ("a.b;c", None),
                          ("x.exe", "android"), ("com.example", "windows"), ("..\\evil.exe", None)):
            with self.assertRaises(allowlist.AllowlistError, msg=bad):
                allowlist.resolve([bad], plat)

    def test_invalid_entries_in_file_dropped(self):
        paths.allowlist_file().write_text(json.dumps({
            "android": ["com.brave.browser", "bad id", 5, "com.brave.browser"],
            "windows": ["ok.exe", "../x.exe"], "users": {"masha": {"android": ["org.x.y", "IncludedApplications=*"]}},
        }), encoding="utf-8")
        al = allowlist.Allowlist.load()
        self.assertEqual(al.android, ["com.brave.browser"])
        self.assertEqual(al.windows, ["ok.exe"])
        self.assertEqual(al.effective("android", "masha"), ["org.x.y"])
        self.assertEqual(al.effective("windows", "masha"), ["ok.exe"])

    def test_malformed_file_tolerated(self):
        # хвостовой \n, users не объект, свой список не списком — как в zoo_allowlist (lib.sh)
        f = paths.allowlist_file()
        f.write_text(json.dumps({"android": ["com.a.b\n", "com.ok.app"], "users": ["masha"]}), encoding="utf-8")
        al = allowlist.Allowlist.load()
        self.assertEqual(al.android, ["com.ok.app"])
        self.assertEqual(al.users, {})
        f.write_text(json.dumps({"android": ["com.ok.app"], "users": {"masha": {"android": None}}}),
                     encoding="utf-8")
        self.assertEqual(allowlist.Allowlist.load().effective("android", "masha"), ["com.ok.app"])
        f.write_text("", encoding="utf-8")
        al = allowlist.Allowlist.load()
        self.assertFalse(al.exists)
        self.assertEqual(al.android, ["com.brave.browser", "org.telegram.messenger"])

    def test_v2rayn_rules(self):
        rules = allowlist.v2rayn_rules(["brave.exe", "Telegram.exe"])
        self.assertEqual([r["outboundTag"] for r in rules], ["direct", "direct", "direct", "proxy", "direct"])
        self.assertEqual(rules[3]["process"], ["brave.exe", "Telegram.exe"])
        self.assertEqual(rules[-1], {"remarks": "Всё остальное напрямую", "outboundTag": "direct", "port": "0-65535"})
        self.assertIn("geoip:ru", rules[2]["ip"])
        for r in rules:  # поля RulesItem v2rayN 7.25.4
            self.assertLessEqual(set(r), {"remarks", "outboundTag", "ip", "domain", "process", "port", "network"})
            self.assertFalse(any(d.startswith("geosite:") for d in r.get("domain", [])))


@needs_bash
class ChangeTest(AllowlistEnvTest):
    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        users.bootstrap()
        users.add_user("masha")
        users.ensure_probe_user()

    def test_common_add_del_apply(self):
        ch = allowlist.change("add", ["youtube", "Discord.exe"])
        self.assertEqual(ch.added, [("android", "com.google.android.youtube"), ("windows", "Discord.exe")])
        data = self.registry()
        self.assertEqual(data["android"][-1], "com.google.android.youtube")
        self.assertEqual(data["windows"], ["brave.exe", "Telegram.exe", "Discord.exe"])
        # AWG пересобран модулем, правила v2rayN — у видимых пользователей, без zoo-probe
        self.assertTrue((self.env.fake / "amneziawg.refreshed").exists())
        self.assertEqual(ch.applied["amneziawg"], "пересобран")
        written = sorted(os.path.basename(os.path.dirname(p)) for p in ch.applied["v2rayn"])
        self.assertEqual(written, ["masha", "owner"])
        rules = json.loads(allowlist.user_file("masha").read_text(encoding="utf-8"))
        self.assertEqual(rules[3]["process"], ["brave.exe", "Telegram.exe", "Discord.exe"])
        self.assertFalse(allowlist.user_file("zoo-probe").exists())
        # повтор и удаление без учёта регистра у процессов
        self.assertEqual(allowlist.change("add", ["youtube"]).message, "без изменений")
        ch = allowlist.change("del", ["discord.exe"])
        self.assertEqual(ch.removed, [("windows", "Discord.exe")])

    def test_empty_list_refused(self):
        allowlist.change("del", ["telegram"])
        with self.assertRaises(allowlist.AllowlistError):
            allowlist.change("del", ["brave"], platform="android")
        self.assertEqual(self.registry()["android"], ["com.brave.browser"])

    def test_set_lists_one_rebuild(self):
        base = allowlist.Allowlist.load()
        want = {"android": base.android + ["com.google.android.youtube", "com.example.app"],
                "windows": base.windows[:1] + ["Discord.exe"]}
        with mock.patch.object(protolib, "manifest_refresh") as refresh:
            ch = allowlist.set_lists(want, titles={"com.example.app": "Моя	прога", "x.y.z": "чужое"})
        self.assertEqual(refresh.call_count, 1, "одна пересборка на всё сохранение")
        self.assertEqual(sorted(ch.affected), ["masha", "owner"])
        self.assertEqual(ch.added, [("android", "com.google.android.youtube"), ("android", "com.example.app"),
                                    ("windows", "Discord.exe")])
        self.assertEqual(ch.removed, [("windows", "Telegram.exe")])
        data = self.registry()
        self.assertEqual(data["android"][-2:], ["com.google.android.youtube", "com.example.app"])
        self.assertEqual(data["windows"], ["brave.exe", "Discord.exe"])
        self.assertEqual(data["titles"], {"com.example.app": "Моя прога"})  # чужой id без списка — не хранится
        self.assertEqual(sorted(os.path.basename(os.path.dirname(p)) for p in ch.applied["v2rayn"]),
                         ["masha", "owner"])
        # то же самое ещё раз — без записи и без пересборки; порядок и регистр id не важны
        with mock.patch.object(protolib, "manifest_refresh") as refresh:
            ch = allowlist.set_lists({"android": list(reversed(want["android"])),
                                      "windows": ["discord.exe", "brave.exe"]})
        self.assertEqual((ch.message, ch.affected, refresh.call_count), ("без изменений", [], 0))
        # приложение убрали из списков — название забыто
        allowlist.set_lists({"android": base.android, "windows": base.windows})
        self.assertNotIn("titles", self.registry())

    def test_title_kept_while_a_group_list_uses_it(self):
        base = allowlist.Allowlist.load()
        with mock.patch.object(protolib, "manifest_refresh"):
            allowlist.set_lists({"android": base.android + ["ru.crm.app"], "windows": base.windows},
                                titles={"ru.crm.app": "CRM"})
            al = allowlist.Allowlist.load()
            al.groups = {"g": {"android": base.android + ["ru.crm.app"], "windows": base.windows}}
            al.save()
            allowlist.set_lists({"android": base.android, "windows": base.windows})
        self.assertEqual(allowlist.Allowlist.load().titles, {"ru.crm.app": "CRM"}, "группа ещё держит приложение")

    def test_set_lists_validates(self):
        base = allowlist.Allowlist.load()
        ok = {"android": base.android, "windows": base.windows}
        for bad in ({**ok, "android": []}, {**ok, "windows": ["", " "]}, {**ok, "android": ["rm -rf /"]},
                    {**ok, "windows": ["notepad"]}, {**ok, "android": ["com.exa mple"]}):
            with self.assertRaises(allowlist.AllowlistError, msg=bad):
                allowlist.set_lists(bad)
        with self.assertRaises(allowlist.AllowlistError):
            allowlist.set_lists({**ok, "android": [f"a.b{i}" for i in range(allowlist.LIST_MAX + 1)]})
        with self.assertRaises(users.UserError):
            allowlist.set_lists(ok, user=users.PROBE_USER)
        self.assertFalse(paths.allowlist_file().exists(), "после отказа реестр не тронут")

    def test_set_lists_user_override(self):
        base = allowlist.Allowlist.load()
        ch = allowlist.set_lists({"android": base.android + ["com.whatsapp"], "windows": base.windows}, user="masha")
        self.assertEqual(ch.affected, ["masha"])
        # своё хранит только платформу, где отличается от общего
        self.assertEqual(self.registry()["users"], {"masha": {"android": base.android + ["com.whatsapp"]}})
        # общий Windows дальше действует на masha, общий Android — нет
        allowlist.set_lists({"android": base.android + ["com.discord"], "windows": base.windows + ["Signal.exe"]})
        al = allowlist.Allowlist.load()
        self.assertIn("Signal.exe", al.effective("windows", "masha"))
        self.assertNotIn("com.discord", al.effective("android", "masha"))
        # список снова как общий — свой список удаляется
        ch = allowlist.set_lists({"android": al.common("android"), "windows": al.common("windows")}, user="masha")
        self.assertEqual(ch.affected, ["masha"])
        self.assertNotIn("masha", self.registry()["users"])

    def test_reset_reports_affected(self):
        allowlist.change("add", ["youtube"], user="masha")
        self.assertEqual(allowlist.reset("masha").affected, ["masha"])
        allowlist.change("add", ["youtube"])
        self.assertEqual(sorted(allowlist.reset().affected), ["masha", "owner"])

    def test_user_override_and_reset(self):
        ch = allowlist.change("del", ["telegram"], user="masha", platform="android")
        self.assertEqual(ch.removed, [("android", "org.telegram.messenger")])
        data = self.registry()
        self.assertEqual(data["users"], {"masha": {"android": ["com.brave.browser"]}})
        self.assertEqual(data["android"], ["com.brave.browser", "org.telegram.messenger"])  # общий не тронут
        al = allowlist.Allowlist.load()
        self.assertEqual(al.effective("windows", "masha"), ["brave.exe", "Telegram.exe"])
        # общий список дальше на masha не влияет (Android), но влияет на Windows
        allowlist.change("add", ["signal"])
        al = allowlist.Allowlist.load()
        self.assertNotIn("org.thoughtcrime.securesms", al.effective("android", "masha"))
        self.assertIn("Signal.exe", al.effective("windows", "masha"))
        self.assertEqual(allowlist.reset("masha").message, "сброшен на общий список")
        self.assertNotIn("masha", self.registry()["users"])
        self.assertIn("без изменений", allowlist.reset("masha").message)
        with self.assertRaises(users.UserError):
            allowlist.change("add", ["youtube"], user="nobody")
        allowlist.reset()
        self.assertEqual(self.registry()["android"], ["com.brave.browser", "org.telegram.messenger"])

    def test_system_user_refused(self):
        with self.assertRaises(users.UserError):
            allowlist.change("del", ["telegram"], user=users.PROBE_USER)
        with self.assertRaises(users.UserError):
            allowlist.reset(users.PROBE_USER)
        code, _, err = run_cli("allow", "list", "--user", users.PROBE_USER)
        self.assertEqual(code, 1)
        self.assertIn("служебный", err)

    def test_setup_writes_files_for_existing_users(self):
        # обновление со старой версии: реестра нет, файлов v2rayN у пользователей нет
        paths.allowlist_file().unlink(missing_ok=True)
        allowlist.user_file("masha").unlink(missing_ok=True)
        code, out, err = run_cli("setup", "--json")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(paths.allowlist_file().exists())
        self.assertTrue(allowlist.user_file("masha").exists())
        self.assertTrue(allowlist.user_file("owner").exists())

    def test_user_delete_forgets_override(self):
        allowlist.change("add", ["youtube"], user="masha")
        self.assertTrue(allowlist.user_file("masha").exists())
        rep = users.delete_user("masha")
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertNotIn("masha", self.registry()["users"])
        self.assertFalse((self.env.etc / "clients" / "masha").exists())

    def test_awg_error_reported(self):
        self.env.fail("amneziawg:manifest_refresh")
        ch = allowlist.change("add", ["youtube"])
        self.assertTrue(ch.applied["amneziawg"].startswith("ошибка"))
        code, out, err = run_cli("allow", "add", "instagram")
        self.assertEqual(code, 1)
        self.assertIn("zoo allow apply", err + out)

    def test_cli(self):
        code, out, _ = run_cli("allow", "list", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("com.brave.browser", data["android"])
        self.assertTrue(any(a["key"] == "youtube" for a in data["catalog"]))
        code, out, err = run_cli("allow", "add", "youtube", "--android", "--user", "masha")
        self.assertEqual(code, 0, err)
        self.assertIn("com.google.android.youtube", out + err)
        code, out, _ = run_cli("allow", "list", "--user", "masha", "--json")
        eff = json.loads(out)["effective"]
        self.assertTrue(eff["own"])
        self.assertIn("com.google.android.youtube", eff["android"])
        code, _, err = run_cli("allow", "add", "bad id")
        self.assertEqual(code, 1)
        self.assertIn("не ключ каталога", err)
        code, out, _ = run_cli("allow", "list", "--catalog")
        self.assertIn("org.thoughtcrime.securesms", out)
        code, out, err = run_cli("allow", "apply", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["amneziawg"], "пересобран")
        code, out, _ = run_cli("links", "masha")
        self.assertIn("v2rayN (Windows)", out)


@needs_bash
@unittest.skipUnless(JQ, "нет jq (на сервере и в стенде он есть)")
class AwgAndroidConfTest(AllowlistEnvTest):
    """Настоящий proto-amneziawg.sh: Android-вариант со списком, общий .conf без ключа."""

    def setUp(self):
        super().setUp()
        for f in ("proto-amneziawg.sh", "awg-params.sh"):
            shutil.copy(REPO / "scripts" / "lib" / f, self.env.scripts / "lib" / f)
        d = self.env.etc / "clients" / "masha"
        d.mkdir(parents=True)
        (d / "amneziawg.key").write_text("KEY=\n", encoding="utf-8")
        (d / "amneziawg.ip").write_text("10.66.66.5\n", encoding="utf-8")
        (d / "amneziawg.psk").write_text("PSK\n", encoding="utf-8")

    def conf(self, android: bool) -> str:
        script = ('. "$ZOO_SCRIPTS_DIR/lib.sh"; . "$ZOO_SCRIPTS_DIR/lib/proto-amneziawg.sh"\n'
                  'AWG_JC=4 AWG_JMIN=10 AWG_JMAX=50 AWG_S1=1 AWG_S2=2 AWG_S3=3 AWG_S4=4 AWG_H1=1 AWG_H2=2 '
                  'AWG_H3=3 AWG_H4=4 AWG_SERVER_PUB=PUB AWG_PORT=5000 SERVER_IP=10.0.0.1\n'
                  'export AWG_JC AWG_JMIN AWG_JMAX AWG_S1 AWG_S2 AWG_S3 AWG_S4 AWG_H1 AWG_H2 AWG_H3 AWG_H4\n'
                  + ('_awg_client_conf masha "$(zoo_allowlist android masha)"\n' if android
                     else '_awg_client_conf masha\n'))
        cp = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                            env={**os.environ, "SCRIPTS_DIR": self.env.scripts.as_posix()})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_variants(self):
        plain = self.conf(False)
        self.assertNotIn("IncludedApplications", plain)
        android = self.conf(True)
        iface = android.split("[Peer]")[0]
        self.assertIn("IncludedApplications = com.brave.browser, org.telegram.messenger\n", iface)
        self.assertEqual(re.sub(r"IncludedApplications = .*\n", "", android), plain)
        # свой список пользователя и отброс мусора: строка уходит в .conf как есть
        paths.allowlist_file().write_text(json.dumps({
            "android": ["com.brave.browser"],
            "users": {"masha": {"android": ["org.x.y", "evil\nPostUp = rm", "org.x.y", "org.z.w\n"]}}}),
            encoding="utf-8")
        self.assertIn("IncludedApplications = org.x.y\n", self.conf(True))
        self.assertNotIn("PostUp", self.conf(True))
        # мусорная структура: users не объект, свой список не список — общий список, как в Python
        for users_val in (["masha"], {"masha": {"android": None}}, {"masha": "x"}):
            paths.allowlist_file().write_text(json.dumps({"android": ["com.brave.browser"], "users": users_val}),
                                              encoding="utf-8")
            self.assertIn("IncludedApplications = com.brave.browser\n", self.conf(True), users_val)
            self.assertEqual(allowlist.Allowlist.load().effective("android", "masha"), ["com.brave.browser"])


class AllowWebTest(AppTestBase):
    def setUp(self):
        super().setUp()
        if not BASH:
            self.skipTest("нет bash")
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        users.bootstrap()
        users.add_user("masha")
        users.ensure_probe_user()
        self.c.login()

    def save(self, android, windows, user=None, **extra):
        form = {"action": "save", **({"user": user} if user else {}), **extra}
        return self.c.post("/apps", form, multi={"android": android, "windows": windows})

    def test_page_is_one_table_by_app(self):
        for path in ("/apps", "/apps?user=masha"):
            resp, body = self.c.get(path)
            self.assertEqual(resp.status, 200)
            self.assertNotIn("<select", body)
            self.assertNotIn("из каталога", body)
            self.assertNotIn("style=", body)
            self.assertNotIn(">Показать<", body)
            self.assertLessEqual(visible_words(body), 100, path)
            self.assertIn("Через VPN идут только отмеченные приложения", body)
            self.assertIn('aria-label="Чей список"', body)
            self.assertIn(">Общий<", body)
            self.assertEqual(body.count('<form method="post" action="/apps" class="stack"'), 1,
                             "одна форма на весь список")
            self.assertIn("data-draft", body)
            self.assertIn('name="csrf"', body)
            # весь каталог виден: включённые отмечены, выключенные — нет; платформы без id — прочерк
            for a in allowlist.CATALOG:
                for plat in allowlist.PLATFORMS:
                    ident = getattr(a, plat)
                    if ident:
                        self.assertRegex(body, rf'name="{plat}" value="{re.escape(ident)}"( checked)? data-was="[01]"')
            self.assertRegex(body, r'name="android" value="com.brave.browser" checked data-was="1"')
            self.assertRegex(body, r'name="android" value="com.whatsapp" data-was="0"')
            self.assertIn('<span class="muted">—</span>', body)
            self.assertEqual(len(re.findall(r'name="(?:android|windows)" value=', body)),
                             sum(1 for a in allowlist.CATALOG for p in allowlist.PLATFORMS if getattr(a, p)))
            self.assertIn("<summary>Своё приложение</summary>", body)
            self.assertNotIn('<details class="custom" open', body)
            self.assertIn("data-save", body)
            self.assertIn("data-cancel", body)
        self.assertIn('href="/apps?user=masha"', body)
        self.assertNotIn(users.PROBE_USER, body)

    def test_save_one_post_one_rebuild_one_flash(self):
        base = allowlist.Allowlist.load()
        with mock.patch.object(protolib, "manifest_refresh") as refresh:
            resp, _ = self.save(base.android + ["com.google.android.youtube", "com.whatsapp"],
                                base.windows + ["Discord.exe"])
        self.assertEqual(header(resp, "Location"), ["/apps"])
        self.assertEqual(refresh.call_count, 1)
        al = allowlist.Allowlist.load()
        self.assertEqual(al.android[-2:], ["com.google.android.youtube", "com.whatsapp"])
        self.assertEqual(al.windows[-1], "Discord.exe")
        _, body = self.c.get("/apps")
        self.assertEqual(body.count('class="alerts flash"'), 1, "одна плашка")
        self.assertRegex(body, r'Сохранено\. Новые QR/файлы нужны: <a href="/users/owner">owner</a>, '
                               r'<a href="/users/masha">masha</a> →')
        self.assertNotIn("Разошлите", body)
        self.assertRegex(body, r'name="android" value="com.whatsapp" checked data-was="1"')
        # флеш показывается один раз; без изменений — короткая строка без ссылок
        _, body = self.c.get("/apps")
        self.assertNotIn("Сохранено", body)
        self.save(al.android, al.windows)
        _, body = self.c.get("/apps")
        self.assertIn("Без изменений", body)
        self.assertNotIn("Новые QR", body)

    def test_save_user_list_and_reset(self):
        base = allowlist.Allowlist.load()
        resp, _ = self.save(base.android, base.windows + ["Discord.exe"], user="masha")
        self.assertEqual(header(resp, "Location"), ["/apps?user=masha"])
        self.assertEqual(allowlist.Allowlist.load().users, {"masha": {"windows": base.windows + ["Discord.exe"]}})
        _, body = self.c.get("/apps?user=masha")
        self.assertIn("свой (отличается: +1 −0)", body)
        self.assertIn('<a href="/users/masha">masha</a> →', body)
        self.assertNotIn('<a href="/users/owner">', body.split("<main>")[1].split("<form")[0], "затронут только masha")
        self.assertRegex(body, r'data-confirm="[^"]+" data-swap>(<input[^>]*>)*<input type="hidden" '
                               r'name="action" value="reset"')
        _, body = self.c.get("/apps")
        self.assertIn("Свои списки пользователей", body)
        self.assertIn("+1 −0", body)
        self.assertIn("Сбросить к пресету", body)
        resp, _ = self.c.post("/apps", {"action": "reset", "user": "masha"})
        self.assertEqual(header(resp, "Location"), ["/apps?user=masha"])
        self.assertNotIn("masha", allowlist.Allowlist.load().users)
        _, body = self.c.get("/apps?user=masha")
        self.assertIn("как общий", body)
        self.assertEqual(body.count('class="alerts flash"'), 1)
        self.assertNotIn("Вернуть общий", body)

    def test_custom_app_with_title(self):
        base = allowlist.Allowlist.load()
        resp, _ = self.save(base.android, base.windows, custom_title="Мой банк", custom_android="ru.bank.app",
                            custom_windows="Bank.exe")
        self.assertEqual(resp.status, 303)
        al = allowlist.Allowlist.load()
        self.assertEqual((al.android[-1], al.windows[-1]), ("ru.bank.app", "Bank.exe"))
        self.assertEqual(al.titles, {"ru.bank.app": "Мой банк", "bank.exe": "Мой банк"})
        _, body = self.c.get("/apps")
        self.assertEqual(body.count("<span>Мой банк</span>"), 1, "одна строка на оба id")
        self.assertEqual(body.count('name="title:'), 2)
        self.assertRegex(body, r'<span class="sub">ru.bank.app · Bank.exe</span>')
        self.assertRegex(body, r'name="android" value="ru.bank.app" checked data-was="1"')
        self.assertRegex(body, r'name="windows" value="Bank.exe" checked data-was="1"')
        # страница, отправленная как есть (поля title: приходят из формы), название не теряет
        self.save(al.android, al.windows, **{"title:ru.bank.app": "Мой банк", "title:Bank.exe": "Мой банк"})
        self.assertEqual(allowlist.Allowlist.load().titles["bank.exe"], "Мой банк")
        # выключили своё приложение — оно уходит из списка вместе с названием
        self.save(base.android, base.windows)
        al = allowlist.Allowlist.load()
        self.assertNotIn("ru.bank.app", al.android)
        self.assertEqual(al.titles, {})

    def test_save_errors_keep_draft(self):
        base = allowlist.Allowlist.load()
        resp, body = self.save(["com.google.android.youtube"], [])  # Windows пуст
        self.assertEqual(resp.status, 422)
        self.assertIn("стал бы пустым", body)
        self.assertRegex(body, r'name="android" value="com.google.android.youtube" checked data-was="0"')
        self.assertRegex(body, r'name="android" value="com.brave.browser" data-was="1"')  # снят, но не потерян
        self.assertFalse(paths.allowlist_file().exists())
        # мусор в поле «своё»: ошибка, значение остаётся в поле, раздел раскрыт
        resp, body = self.save(base.android, base.windows, custom_android="rm -rf /", custom_title="Т")
        self.assertEqual(resp.status, 422)
        self.assertIn("не пакет Android", body)
        self.assertIn('value="rm -rf /"', body)
        self.assertIn('<details open class="custom">', body)
        resp, body = self.save(base.android, base.windows, custom_title="Только название")
        self.assertEqual(resp.status, 422)
        self.assertIn("нужен пакет Android или процесс Windows", body)
        # подделанная форма: недопустимый id не попадает ни в реестр, ни обратно на страницу
        resp, body = self.save(base.android + ["x; reboot"], base.windows)
        self.assertEqual(resp.status, 422)
        self.assertNotIn('value="x; reboot"', body)
        self.assertFalse(paths.allowlist_file().exists())
        # чужой и служебный пользователь, неизвестное действие, старое действие, форма без CSRF
        for form in ({"action": "save", "user": "ghost"}, {"action": "save", "user": users.PROBE_USER},
                     {"action": "nope"}, {"action": "add", "platform": "android", "app": "youtube"}):
            resp, _ = self.c.post("/apps", form)
            self.assertEqual(resp.status, 303, form)
        _, body = self.c.get("/apps")
        self.assertIn("служебный пользователь", body)
        self.assertNotIn(users.PROBE_USER, allowlist.Allowlist.load().users)
        resp, _ = self.c.post("/apps", {"action": "save"}, csrf=False, multi={"android": base.android,
                                                                              "windows": base.windows})
        self.assertEqual(resp.status, 403)
        self.assertFalse(paths.allowlist_file().exists())

    def test_awg_error_adds_a_line_only_on_failure(self):
        base = allowlist.Allowlist.load()
        self.env.fail("amneziawg:manifest_refresh")
        self.save(base.android + ["com.whatsapp"], base.windows)
        _, body = self.c.get("/apps")
        self.assertIn("AmneziaWG: ошибка", body)
        self.assertIn("Сохранено", body)

    def test_user_page_offers_v2rayn_file(self):
        with mock.patch("zoolib.qr.svg", return_value="<svg></svg>"):
            resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertIn("«Импорт правил из файла», файл v2rayn-routing.json", body, "тот же шаг, что в инструкции")
        self.assertNotIn("Маршрутизация → Импорт из файла", body)
        self.assertIn('href="/apps?user=masha"', body)
        idx = [m for m in re.findall(r'href="/users/masha/file/([\w.-]+)"', body)]
        downloads = {}
        for i in idx:
            resp, data = self.c.get(f"/users/masha/file/{i}")
            self.assertEqual(resp.status, 200)
            downloads[header(resp, "Content-Disposition")[0].split(";")[1].strip()] = data
        name = 'filename="masha-v2rayn-routing.json"'
        self.assertIn(name, downloads)
        self.assertEqual(json.loads(downloads[name])[3]["process"], ["brave.exe", "Telegram.exe"])


if __name__ == "__main__":
    unittest.main()
