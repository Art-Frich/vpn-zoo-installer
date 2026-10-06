import json
import os
import re
import shutil
import subprocess
import unittest
from unittest import mock

from tests.helpers import BASH, REPO, ZooEnv, needs_bash
from tests.test_cli import run_cli
from tests.test_web import AppTestBase, header
from zoolib import allowlist, paths, users

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

    def test_page_and_actions(self):
        resp, body = self.c.get("/apps")
        self.assertEqual(resp.status, 200)
        self.assertIn('href="/apps"', body)
        self.assertIn("com.brave.browser", body)
        self.assertIn('value="com.google.android.youtube"', body)  # каталог в выпадающем списке
        self.assertNotIn("style=", body)
        resp, _ = self.c.post("/apps", {"action": "add", "platform": "android", "app": "com.google.android.youtube"})
        self.assertEqual(header(resp, "Location"), ["/apps"])
        self.assertIn("com.google.android.youtube", allowlist.Allowlist.load().android)
        _, body = self.c.get("/apps")
        self.assertIn("список изменён", body)
        # свой список пользователя из ручного поля
        resp, _ = self.c.post("/apps", {"action": "add", "platform": "windows", "custom": "Discord.exe",
                                        "user": "masha"})
        self.assertEqual(header(resp, "Location"), ["/apps?user=masha"])
        al = allowlist.Allowlist.load()
        self.assertEqual(al.users["masha"]["windows"][-1], "Discord.exe")
        _, body = self.c.get("/apps?user=masha")
        self.assertIn("свой список", body)
        self.assertIn("Вернуть общий список", body)
        self.c.post("/apps", {"action": "del", "platform": "android", "app": "org.telegram.messenger"})
        self.assertNotIn("org.telegram.messenger", allowlist.Allowlist.load().android)
        # ошибки: мусор, чужой пользователь, пустой список
        for form in ({"action": "add", "platform": "android", "custom": "rm -rf /"},
                     {"action": "add", "platform": "android", "app": "x.y", "user": "ghost"},
                     {"action": "del", "platform": "android", "app": "org.telegram.messenger",
                      "user": users.PROBE_USER},
                     {"action": "nope"}):
            resp, _ = self.c.post("/apps", form)
            self.assertEqual(resp.status, 303)
        _, body = self.c.get("/apps")
        self.assertIn("не ключ каталога", body)
        self.assertIn("нет в реестре", body)
        self.assertIn("служебный пользователь", body)
        self.assertNotIn(users.PROBE_USER, allowlist.Allowlist.load().users)
        self.c.post("/apps", {"action": "del", "platform": "android", "app": "com.google.android.youtube"})
        resp, _ = self.c.post("/apps", {"action": "del", "platform": "android", "app": "com.brave.browser"})
        _, body = self.c.get("/apps")
        self.assertIn("стал бы пустым", body)
        self.c.post("/apps", {"action": "reset", "user": "masha"})
        self.assertNotIn("masha", allowlist.Allowlist.load().users)
        resp, _ = self.c.post("/apps", {"action": "add", "app": "youtube"}, csrf=False)
        self.assertEqual(resp.status, 403)

    def test_user_page_offers_v2rayn_file(self):
        with mock.patch("zoolib.qr.svg", return_value="<svg></svg>"):
            resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertIn("Импорт правил из файла", body)
        self.assertIn('href="/apps?user=masha"', body)
        idx = [m for m in re.findall(r'href="/users/masha/file/(\d+)"', body)]
        downloads = {}
        for i in idx:
            resp, data = self.c.get(f"/users/masha/file/{i}")
            self.assertEqual(resp.status, 200)
            downloads[header(resp, "Content-Disposition")[0]] = data
        name = 'attachment; filename="masha-v2rayn-routing.json"'
        self.assertIn(name, downloads)
        self.assertEqual(json.loads(downloads[name])[3]["process"], ["brave.exe", "Telegram.exe"])


if __name__ == "__main__":
    unittest.main()
