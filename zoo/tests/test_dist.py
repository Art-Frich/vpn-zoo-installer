"""Дистрибутивы клиентов для групп «ставит ИТ»: выбор файла из релиза, скачивание, хранение двух версий,
лимит, заявка «Обновить», раздача админке (вход, обход каталогов), блок на страницах групп."""

import hashlib
import io
import json
import os
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, ZooEnv, needs_bash
from tests.test_cli import run_cli
from tests.test_web import AppTestBase, Client, header
from zoolib import clients, dist, groups, storage, users
from zoolib.web import server


def asset(name, size=100, digest="", repo="hiddify/hiddify-app", tag="v4.1.1"):
    return {"name": name, "size": size, "digest": digest,
            "url": f"https://github.com/{repo}/releases/download/{tag}/{name}"}


HIDDIFY = [
    asset("Hiddify-Android-universal.apk"), asset("Hiddify-Android-arm64-v8a.apk"), asset("Hiddify-Android-x86_64.apk"),
    asset("Hiddify-Windows-Setup-x64.exe"), asset("Hiddify-Windows-Portable-x64.zip"), asset("Hiddify-Windows-x64.msix"),
    asset("Hiddify-Windows-Setup-x64.exe.sha256"), asset("Hiddify-MacOS.dmg"), asset("Hiddify-Linux-x64.AppImage"),
    asset("Hiddify-Debian-x64.deb"), asset("checksums.txt"),
]


def rel(version="4.1.1", assets=None):
    return {"tag": "v" + version, "version": version, "published": "2026-10-01", "assets": assets or HIDDIFY}


def fake_dl(url, dest, size):
    data = (url.rsplit("/", 1)[-1] + url.split("/")[-2]).encode().ljust(size, b".")[:size]
    dest.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def office(mode="admin", **sets):
    sets = sets or {"android": ["hiddify"], "windows": ["hiddify"], "ios": ["happ"]}
    return groups.Groups(Path("g.json"), [groups.Group("g1", "Офис", ["hysteria2"], sets, install_mode=mode)])


class PickAssetTest(unittest.TestCase):
    def pick(self, plat, assets):
        a = dist.pick_asset(plat, assets)
        return a["name"] if a else None

    def test_hiddify_release(self):
        self.assertEqual(self.pick("android", HIDDIFY), "Hiddify-Android-universal.apk")
        self.assertEqual(self.pick("windows", HIDDIFY), "Hiddify-Windows-Setup-x64.exe", "установщик раньше архива")
        self.assertEqual(self.pick("macos", HIDDIFY), "Hiddify-MacOS.dmg")
        self.assertEqual(self.pick("linux", HIDDIFY), "Hiddify-Linux-x64.AppImage")

    def test_android_prefers_universal_then_arm64_never_other_abi(self):
        self.assertEqual(self.pick("android", [asset("a-arm64-v8a.apk"), asset("a-armeabi-v7a.apk"), asset("a-x86.apk")]),
                         "a-arm64-v8a.apk")
        self.assertEqual(self.pick("android", [asset("a-universal.apk"), asset("a-arm64-v8a.apk")]), "a-universal.apk")
        self.assertEqual(self.pick("android", [asset("app-release.apk")]), "app-release.apk")
        self.assertIsNone(self.pick("android", [asset("a-x86_64.apk"), asset("a-armeabi-v7a.apk")]))

    def test_windows_x64_only(self):
        names = [asset("v2rayN-windows-64.zip"), asset("v2rayN-windows-64-SelfContained.zip"),
                 asset("v2rayN-windows-arm64.zip"), asset("v2rayN-macos-64.dmg"), asset("v2rayN-linux-64.zip")]
        self.assertEqual(self.pick("windows", names), "v2rayN-windows-64.zip", "при равных — короче имя")
        self.assertIsNone(self.pick("windows", [asset("app-win32.exe"), asset("app-windows-arm64.exe"),
                                                asset("app-x86.msi"), asset("app-darwin.zip")]))
        self.assertEqual(self.pick("windows", [asset("hysteria-windows-amd64.exe"), asset("hysteria-windows-386.exe")]),
                         "hysteria-windows-amd64.exe")

    def test_v2rayn_linux_and_amnezia_files(self):
        v2 = [asset(n) for n in ("v2rayN-linux-64.deb", "v2rayN-linux-64.zip", "v2rayN-linux-arm64.deb",
                                 "v2rayN-linux-loong64.deb", "v2rayN-linux-riscv64.deb", "v2rayN-linux-rhel-64.rpm")]
        self.assertEqual(self.pick("linux", v2), "v2rayN-linux-64.deb")
        am = [asset(n) for n in ("AmneziaVPN_5.0.3.0_android11+_arm64-v8a.apk", "AmneziaVPN_5.0.3.0_android9-10_arm64-v8a.apk",
                                 "AmneziaVPN_5.0.3.0_linux_x64.run")]
        self.assertEqual(self.pick("android", am), "AmneziaVPN_5.0.3.0_android9-10_arm64-v8a.apk",
                         "сборка «android11+» не ставится на Android 9–10")
        self.assertEqual(self.pick("linux", am), "AmneziaVPN_5.0.3.0_linux_x64.run")

    def test_nothing_suitable_is_none_not_a_wrong_file(self):
        junk = [asset("src.tar.gz"), asset("notes.txt"), asset("x.apk.sha256"), asset("a.msix"), asset("debug-symbols.zip")]
        for plat in dist.PLATFORMS:
            self.assertIsNone(self.pick(plat, junk), plat)
        self.assertIsNone(self.pick("ios", HIDDIFY))
        self.assertIsNone(dist.pick_asset("android", [{"size": 1}]))


class FetchReleaseTest(unittest.TestCase):
    def resp(self, body):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
        return r

    def test_parses_assets_digest_and_drops_foreign_urls(self):
        good = "a" * 64
        body = {"tag_name": "v4.1.1", "published_at": "2026-03-05T10:00:00Z", "assets": [
            {"name": "a.apk", "size": 5, "digest": "sha256:" + good,
             "browser_download_url": "https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/a.apk"},
            {"name": "b.apk", "size": 5, "digest": "md5:zzz",
             "browser_download_url": "https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/b.apk"},
            {"name": "evil.apk", "size": 5, "browser_download_url": "https://evil.example/evil.apk"},
            {"name": "other.apk", "size": 5,
             "browser_download_url": "https://github.com/other/repo/releases/download/v1/other.apk"},
            {"name": "empty.apk", "size": 0,
             "browser_download_url": "https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/empty.apk"},
            "junk"]}
        with mock.patch("urllib.request.urlopen", return_value=self.resp(body)) as op:
            r = dist.fetch_release("hiddify/hiddify-app")
        self.assertEqual(op.call_args.args[0].full_url, "https://api.github.com/repos/hiddify/hiddify-app/releases/latest")
        self.assertEqual((r["version"], r["published"]), ("4.1.1", "2026-03-05"))
        self.assertEqual([(a["name"], a["digest"]) for a in r["assets"]], [("a.apk", good), ("b.apk", "")])

    def test_failures_become_dist_error(self):
        for exc in (urllib.error.HTTPError("u", 403, "rate", {}, None), urllib.error.URLError("нет сети"), TimeoutError()):
            with mock.patch("urllib.request.urlopen", side_effect=exc), self.assertRaises(dist.DistError):
                dist.fetch_release("a/b")
        for bad in (b"not json", b"[]", b'{"tag_name": ""}'):
            with mock.patch("urllib.request.urlopen", return_value=self.resp(bad)), self.assertRaises(dist.DistError):
                dist.fetch_release("a/b")
        with self.assertRaises(dist.DistError):
            dist.fetch_release("../../etc/passwd")


class DownloadTest(unittest.TestCase):
    def stream(self, chunks):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.read.side_effect = list(chunks) + [b""]
        return r

    def test_streams_to_part_then_renames_and_hashes(self):
        with ZooEnv() as env:
            dest = env.root / "f.apk"
            with mock.patch("urllib.request.urlopen", return_value=self.stream([b"ab", b"cd"])):
                sha = dist.download("https://github.com/x/y/releases/download/v1/f.apk", dest, 4)
            self.assertEqual(sha, hashlib.sha256(b"abcd").hexdigest())
            self.assertEqual(dest.read_bytes(), b"abcd")
            self.assertEqual([f.name for f in env.root.iterdir() if f.name.startswith("f.")], ["f.apk"])

    def test_wrong_size_leaves_nothing(self):
        with ZooEnv() as env:
            dest = env.root / "f.apk"
            for chunks, size in (([b"ab"], 4), ([b"abcdef"], 4)):
                with mock.patch("urllib.request.urlopen", return_value=self.stream(chunks)), \
                        self.assertRaises(dist.DistError):
                    dist.download("https://github.com/x/y/releases/download/v1/f.apk", dest, size)
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("x")), \
                    self.assertRaises(dist.DistError):
                dist.download("https://github.com/x/y/releases/download/v1/f.apk", dest, 4)
            self.assertEqual(list(env.root.glob("f.*")), [])
            with self.assertRaises(dist.DistError):
                dist.download("https://github.com/x/y/releases/download/v1/f.apk", dest, dist.MAX_FILE + 1)


class NeededTest(unittest.TestCase):
    def test_only_admin_groups_and_clients_with_a_github_build(self):
        cat = clients.load()
        gs = office(android=["hiddify", "amneziawg"], ios=["happ"], windows=["v2rayn"])
        self.assertEqual(dist.needed(gs, cat), {"hiddify": ["android"], "amneziawg": ["android"], "v2rayn": ["windows"]})
        self.assertEqual(dist.needed(office("self"), cat), {}, "людям, которые ставят сами, дистрибутивы не нужны")
        gs.groups.append(groups.Group("g2", "ИТ-2", ["hysteria2"], {"android": ["hiddify"], "macos": ["hiddify"]},
                                      install_mode="admin"))
        self.assertEqual(dist.needed(gs, cat)["hiddify"], ["android", "macos"])
        self.assertNotIn("incy", dist.needed(office(android=["incy"]), cat), "INCY без репозитория: только магазин")


class FetchAllTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.cat = clients.load()
        self.clock = [1000]

    def now(self):
        self.clock[0] += 10
        return self.clock[0]

    def run_fetch(self, version="4.1.1", assets=None, gs=None, fetch=None, dl=None):
        calls = []

        def default_fetch(repo):
            calls.append(repo)
            return rel(version, assets)

        res = dist.fetch_all(self.cat, gs or office(), fetch or default_fetch, dl or fake_dl, self.now)
        res["calls"] = calls
        return res

    def test_downloads_chosen_files_with_sha_and_meta(self):
        res = self.run_fetch()
        self.assertEqual(res["errors"], {})
        self.assertEqual(sorted(res["downloaded"]), ["hiddify/android", "hiddify/windows"])
        self.assertEqual(res["calls"], ["hiddify/hiddify-app"], "ios: Happ из App Store — не качаем; репозиторий опрошен раз")
        vdir = dist.root() / "hiddify" / "4.1.1"
        meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
        files = {f["platform"]: f for f in meta["files"]}
        self.assertEqual(files["android"]["name"], "Hiddify-Android-universal.apk")
        self.assertEqual(files["windows"]["name"], "Hiddify-Windows-Setup-x64.exe")
        for f in files.values():
            data = (vdir / f["name"]).read_bytes()
            self.assertEqual((f["size"], f["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))
            self.assertFalse(f["verified"], "digest GitHub не пришёл — sha256 посчитан у нас")
        self.assertEqual(dist.status()["errors"], {})
        self.assertIsNotNone(dist.status()["checked"])
        self.assertEqual(dist.total_size(), sum(f.stat().st_size for f in dist.root().rglob("*") if f.is_file()))

    def test_digest_from_github_is_checked(self):
        data = fake_dl("https://github.com/hiddify/hiddify-app/releases/download/v4.1.1/Hiddify-Android-universal.apk",
                       self.env.root / "probe.bin", 100)
        ok = [asset("Hiddify-Android-universal.apk", digest=data)]
        res = self.run_fetch(assets=ok, gs=office(android=["hiddify"]))
        self.assertEqual(res["downloaded"], ["hiddify/android"])
        f = dist.versions("hiddify")[0][1]["files"][0]
        self.assertTrue(f["verified"])
        # неверный digest: файл не остаётся
        bad = [asset("Hiddify-Android-universal.apk", digest="0" * 64)]
        res = self.run_fetch(version="4.2.0", assets=bad, gs=office(android=["hiddify"]))
        self.assertIn("sha256", res["errors"]["hiddify/android"])
        self.assertFalse((dist.root() / "hiddify" / "4.2.0").exists())
        self.assertEqual([m["version"] for _, m in dist.versions("hiddify")], ["4.1.1"])

    def test_same_version_is_not_downloaded_twice(self):
        self.run_fetch()
        dl = mock.Mock(side_effect=fake_dl)
        res = self.run_fetch(dl=dl)
        dl.assert_not_called()
        self.assertEqual(sorted(res["kept"]), ["hiddify/android", "hiddify/windows"])

    def test_keeps_two_latest_versions(self):
        for v in ("4.1.0", "4.1.1", "4.2.0"):
            self.run_fetch(version=v, gs=office(android=["hiddify"]))
        self.assertEqual([m["version"] for _, m in dist.versions("hiddify")], ["4.2.0", "4.1.1"])
        self.assertFalse((dist.root() / "hiddify" / "4.1.0").exists())

    def test_no_suitable_asset_is_reported_not_guessed(self):
        res = self.run_fetch(assets=[asset("Hiddify-Windows-x64.msix"), asset("notes.txt")], gs=office(windows=["hiddify"]))
        self.assertEqual(res["downloaded"], [])
        self.assertEqual(res["errors"], {"hiddify/windows": "в релизе нет подходящего файла"})
        self.assertEqual(list(dist.root().rglob("*.exe")), [])
        self.assertFalse((dist.root() / "hiddify" / "4.1.1").exists(), "пустой каталог версии не остаётся")
        self.assertEqual(dist.status()["errors"], res["errors"])

    def test_one_client_failing_does_not_stop_others_and_keeps_old_files(self):
        self.run_fetch(gs=office(android=["hiddify", "amneziawg"]))

        def fetch(repo):
            if repo.startswith("hiddify"):
                raise dist.DistError("HTTP 403 (лимит GitHub)")
            return rel("2.0.0", [asset("amneziawg-universal.apk", repo=repo, tag="v2.0.0")])

        res = self.run_fetch(gs=office(android=["hiddify", "amneziawg"]), fetch=fetch)
        self.assertIn("403", res["errors"]["hiddify"])
        self.assertEqual(res["downloaded"], ["amneziawg/android"])
        self.assertEqual(dist.versions("hiddify")[0][1]["version"], "4.1.1", "прежняя версия на месте")

    def test_size_cap_prunes_older_versions_first_then_refuses(self):
        big = lambda v: [asset("Hiddify-Android-universal.apk", size=600)]
        with mock.patch.object(dist, "CAP", 2000):
            def dl(url, dest, size):
                dest.write_bytes(b"x" * size)
                return hashlib.sha256(b"x" * size).hexdigest()
            for v in ("1.0", "1.1"):
                res = self.run_fetch(version=v, assets=big(v), gs=office(android=["hiddify"]), dl=dl)
                self.assertEqual(res["errors"], {})
            self.assertEqual(len(dist.versions("hiddify")), 2)
            res = self.run_fetch(version="1.2", assets=big("1.2"), gs=office(android=["hiddify"]), dl=dl)
            self.assertEqual(res["errors"], {}, "место освободили, убрав более старые версии")
            self.assertEqual([m["version"] for _, m in dist.versions("hiddify")], ["1.2", "1.1"])
            self.assertLessEqual(dist.total_size(), 2000)
            res = self.run_fetch(version="1.3", assets=[asset("Hiddify-Android-universal.apk", size=5000)],
                                 gs=office(android=["hiddify"]), dl=dl)
            self.assertIn("лимит", res["errors"]["hiddify/android"])

    def test_listing_gives_file_or_links_and_error(self):
        gs = office(android=["hiddify"], windows=["hiddify"], ios=["happ"])
        self.run_fetch(assets=[asset("Hiddify-Android-universal.apk")], gs=gs)
        rows = {(r["client"]["id"], r["platform"]): r for r in dist.listing(gs, self.cat)}
        a = rows[("hiddify", "android")]["file"]
        self.assertEqual((a["name"], a["version"], a["path"]),
                         ("Hiddify-Android-universal.apk", "4.1.1", "/dist/hiddify/4.1.1/Hiddify-Android-universal.apk"))
        w = rows[("hiddify", "windows")]
        self.assertIsNone(w["file"])
        self.assertIn("подходящего файла", w["error"])
        self.assertTrue(any(ln["kind"] == "github" for ln in w["links"]), "ссылка на GitHub вместо файла")
        self.assertTrue(rows[("happ", "ios")]["store"])
        self.assertEqual(dist.listing(gs, self.cat, "other"), [])
        self.assertEqual(dist.listing(office("self"), self.cat), [])


class FilePathTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        dist.fetch_all(clients.load(), office(android=["hiddify"]), lambda r: rel(), fake_dl, lambda: 5)
        self.name = "Hiddify-Android-universal.apk"

    def test_listed_file_resolves(self):
        f = dist.file_path("hiddify", "4.1.1", self.name)
        self.assertEqual(f, dist.root() / "hiddify" / "4.1.1" / self.name)

    def test_traversal_and_unlisted_names_are_refused(self):
        (dist.root() / "secret.txt").write_text("s", encoding="utf-8")
        (dist.root() / "hiddify" / "4.1.1" / "stray.bin").write_bytes(b"x")
        for args in (("hiddify", "..", "secret.txt"), ("..", "4.1.1", self.name), ("hiddify", "4.1.1", "../../secret.txt"),
                     ("hiddify", "4.1.1", "..%2fsecret.txt"), ("hiddify", "4.1.1", "meta.json"),
                     ("hiddify", "4.1.1", "stray.bin"), ("hiddify", "4.1.1", self.name + ".part"),
                     ("hiddify", "9.9.9", self.name), ("nope", "4.1.1", self.name), ("Hiddify", "4.1.1", self.name),
                     ("hiddify", "4.1.1", ""), ("hiddify", "4.1.1", "a/b"), ("hiddify", "4.1.1", "a\\b"),
                     ("hiddify", "4.1.1", "x\x00y")):
            self.assertIsNone(dist.file_path(*args), args)

    def test_symlinks_are_refused(self):
        vdir = dist.root() / "hiddify" / "4.1.1"
        link = vdir / self.name
        link.unlink()
        try:
            link.symlink_to(self.env.root / "state" / "zoo.lock")
        except (OSError, NotImplementedError):
            self.skipTest("символические ссылки недоступны")
        self.assertIsNone(dist.file_path("hiddify", "4.1.1", self.name))


class RequestTest(unittest.TestCase):
    def test_request_is_a_file_and_rate_limited(self):
        with ZooEnv():
            ok, _ = dist.request_fetch(now=1000)
            self.assertTrue(ok)
            if os.name != "nt":
                self.assertEqual(dist.req_file().stat().st_mode & 0o777, 0o600)
            ok, why = dist.request_fetch(now=1100)
            self.assertFalse(ok)
            self.assertIn("уже заказано", why)
            dist.clear_request()
            dist.fetch_all(clients.load(), office("self"), now=lambda: 1200)   # прогон записал время
            ok, why = dist.request_fetch(now=1250)
            self.assertFalse(ok)
            self.assertIn("2 минут", why)
            self.assertTrue(dist.request_fetch(now=1200 + dist.RATE_LIMIT + 1)[0])


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

    def test_section_counts_versions_and_trims_oldest_non_latest_first(self):
        gs = office(android=["hiddify", "amneziawg"])
        for i, v in enumerate(("1.0", "1.1")):
            dist.fetch_all(clients.load(), gs, lambda r, v=v: rel(v, [asset("Hiddify-Android-universal.apk", repo=r),
                                                                      asset("amneziawg-universal.apk", repo=r)]),
                           fake_dl, lambda i=i: 100 + i)
        sec = storage.section("dist")
        self.assertEqual(sec.rows(), 4)
        self.assertGreater(sec.size(), 0)
        self.assertEqual(sec.path(), str(dist.root()))
        self.assertIsNotNone(sec.oldest())
        self.assertEqual(sec.delete_oldest(1), 1)
        left = {(c, m["version"]) for c in ("hiddify", "amneziawg") for _, m in dist.versions(c)}
        self.assertEqual(len(left), 3)
        sec.delete_oldest(10)
        self.assertEqual(sec.rows(), 0)
        self.assertEqual(sec.size(), 0)

    def test_download_in_progress_and_only_windows_file_survive_trim(self):
        gs = office(android=["hiddify"], windows=["hiddify"])
        dist.fetch_all(clients.load(), gs, lambda r: rel("1.0", [asset("Hiddify-Android-universal.apk", repo=r),
                                                                 asset("Hiddify-Windows-Setup-x64.exe", repo=r)]),
                       fake_dl, lambda: 100)
        # следующие версии: Android скачался, Windows — нет; прежняя с Windows остаётся и при KEEP_VERSIONS=2
        for i, v in enumerate(("1.1", "1.2")):
            dist.fetch_all(clients.load(), gs, lambda r, v=v: rel(v, [asset("Hiddify-Android-universal.apk", repo=r)]),
                           fake_dl, lambda i=i: 200 + i)
        # идёт скачивание следующей: каталог есть, meta.json ещё нет
        partial = dist.root() / "hiddify" / "1.3"
        partial.mkdir()
        (partial / "Hiddify-Android-universal.apk.part").write_bytes(b"x" * 10)
        sec = storage.section("dist")
        self.assertEqual(sec.rows(), 3, "незаконченная версия в чистку не попадает")
        self.assertEqual(sec.delete_oldest(1), 1)
        self.assertTrue(partial.is_dir(), "каталог, куда сейчас качают, не трогается")
        self.assertEqual(sorted(m["version"] for _, m in dist.versions("hiddify")), ["1.0", "1.2"])
        plats = {f["platform"] for _, m in dist.versions("hiddify") for f in m["files"]}
        self.assertEqual(plats, {"android", "windows"}, "версия с единственным файлом Windows — последняя для Windows")

    def test_empty_and_clear(self):
        sec = storage.section("dist")
        self.assertEqual((sec.size(), sec.rows(), sec.oldest()), (0, 0, None))
        self.assertEqual(storage.clear("dist")["removed"], 0)
        self.assertIn("dist", storage.SHARES)


class CliTest(unittest.TestCase):
    def test_fetch_dist_command_uses_groups_and_reports(self):
        with ZooEnv():
            dist.req_file().write_text("1", encoding="utf-8")
            with mock.patch.object(dist, "fetch_release", side_effect=lambda r: rel()), \
                    mock.patch.object(dist, "download", side_effect=fake_dl), \
                    mock.patch.object(groups.Groups, "load", return_value=office()):
                code, out, err = run_cli("clients", "--fetch-dist")
            self.assertEqual(code, 0, out + err)
            self.assertIn("скачано 2", out + err)
            self.assertFalse(dist.req_file().exists(), "заявка снимается первым делом")
            self.assertTrue(list(dist.root().rglob("*.apk")))

    def test_nothing_to_fetch_is_fine(self):
        with ZooEnv(), mock.patch.object(groups.Groups, "load", return_value=office("self")):
            code, out, _ = run_cli("clients", "--fetch-dist")
        self.assertEqual(code, 0, out)

    def test_total_failure_is_a_failed_unit_but_partial_is_not(self):
        with ZooEnv(), mock.patch.object(dist, "fetch_release", side_effect=dist.DistError("HTTP 500")), \
                mock.patch.object(groups.Groups, "load", return_value=office()):
            code, out, _ = run_cli("clients", "--fetch-dist")
        self.assertEqual(code, 1)

    def test_with_check_upstream_both_run(self):
        with ZooEnv(), mock.patch.object(clients, "fetch_latest", side_effect=clients.ClientsError("нет сети")), \
                mock.patch.object(dist, "fetch_release", side_effect=lambda r: rel()), \
                mock.patch.object(dist, "download", side_effect=fake_dl), \
                mock.patch.object(groups.Groups, "load", return_value=office()):
            code, out, _ = run_cli("clients", "--check-upstream", "--fetch-dist")
            self.assertEqual(code, 1, "сбой проверки версий — худший итог юнита")
            self.assertTrue(list(dist.root().rglob("*.apk")), "но файлы скачаны")


class UnitsTest(unittest.TestCase):
    def test_service_fetches_and_path_watches_both_requests(self):
        d = REPO / "zoo" / "systemd"
        svc = (d / "zoo-clients.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/bin/zoo clients --check-upstream --fetch-dist", svc)
        self.assertIn("ReadWritePaths=/var/lib/vpn-zoo", svc)
        self.assertRegex(svc, r"TimeoutStartSec=\d+min")
        path = (d / "zoo-clients.path").read_text(encoding="utf-8")
        self.assertIn(f"PathExists=/var/lib/vpn-zoo/{dist.REQ_NAME}", path)
        self.assertIn(f"PathExists=/var/lib/vpn-zoo/{clients.REQ_NAME}", path)


class SendFileTest(unittest.TestCase):
    """server.Handler._send_file без сокета: заголовки и тело потоком, без чтения в память."""

    def stub(self):
        h = types.SimpleNamespace(sent=[], headers_out=[], wfile=io.BytesIO(), close_connection=False)
        h.send_response = lambda code: h.sent.append(code)
        h.send_header = lambda k, v: h.headers_out.append((k, v))
        h.end_headers = lambda: None
        h._send = lambda resp, head=False: h.sent.append(("fallback", resp.status))
        return h

    def test_streams_file_with_length_and_disposition(self):
        from zoolib.web.app import Response
        with ZooEnv() as env:
            f = env.root / "a.apk"
            f.write_bytes(b"x" * (server.FILE_CHUNK + 5))
            h = self.stub()
            server.Handler._send_file(h, Response(200, b"", "application/octet-stream", file=f,
                                                  headers=[("Content-Disposition", 'attachment; filename="a.apk"')]), False)
            self.assertEqual(h.sent, [200])
            self.assertEqual(dict(h.headers_out)["Content-Length"], str(server.FILE_CHUNK + 5))
            self.assertIn("attachment", dict(h.headers_out)["Content-Disposition"])
            self.assertEqual(len(h.wfile.getvalue()), server.FILE_CHUNK + 5)
            h = self.stub()
            server.Handler._send_file(h, Response(200, b"", "application/octet-stream", file=f), True)
            self.assertEqual(h.wfile.getvalue(), b"", "HEAD без тела")

    def test_vanished_file_is_404(self):
        from zoolib.web.app import Response
        with ZooEnv() as env:
            h = self.stub()
            server.Handler._send_file(h, Response(200, b"", file=env.root / "gone.apk"), False)
            self.assertEqual(h.sent, [("fallback", 404)])


@needs_bash
class DistWebTest(AppTestBase):
    NAME = "Hiddify-Android-universal.apk"

    def setUp(self):
        super().setUp()
        self.env.add_protocol("hysteria2")
        users.bootstrap()
        self.c.login()
        groups.create("Офис", ["hysteria2"], {"android": ["hiddify"], "windows": ["hiddify"], "ios": ["happ"]},
                      install_mode="admin")
        self.gid = "g1"
        dist.fetch_all(clients.load(), groups.Groups.load(), lambda r: rel(assets=[asset(self.NAME, size=120)]),
                       fake_dl, lambda: time.time() - 30)

    def test_download_needs_login_and_streams_the_file(self):
        url = f"/dist/hiddify/4.1.1/{self.NAME}"
        resp, _ = Client(self.app).get(url)
        self.assertEqual(resp.status, 303)
        resp, _ = self.c.get(url)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.file, dist.root() / "hiddify" / "4.1.1" / self.NAME)
        self.assertEqual(resp.body, b"", "тело не в памяти: файл отдаёт сервер потоком")
        self.assertEqual(header(resp, "Content-Disposition"), [f'attachment; filename="{self.NAME}"'])
        self.assertEqual(resp.content_type, "application/octet-stream")
        self.assertIn("no-store", header(resp, "Cache-Control")[0])
        self.assertEqual(header(resp, "X-Content-Type-Options"), ["nosniff"])

    def test_traversal_and_unknown_paths_404(self):
        (dist.root() / "secret.txt").write_text("s", encoding="utf-8")
        for path in ("/dist/hiddify/4.1.1/meta.json", "/dist/hiddify/4.1.1/nope.apk", "/dist/hiddify/9.9/x.apk",
                     "/dist/hiddify/../secret.txt", "/dist/hiddify/4.1.1/../../secret.txt",
                     "/dist/hiddify/%2e%2e/secret.txt", "/dist/hiddify/4.1.1/..%2fsecret.txt",
                     "/dist/hiddify/4.1.1/%2e%2e%2f%2e%2e%2fsecret.txt", "/dist/..%2fusers.json/x/y",
                     "/dist/hiddify/4.1.1", "/dist"):
            resp, body = self.c.get(path)
            self.assertEqual(resp.status, 404, path)
            self.assertIsNone(resp.file, path)
            self.assertNotIn("secret", body)

    def test_refresh_is_post_csrf_and_a_request_file(self):
        resp, _ = self.c.get("/dist/refresh")
        self.assertEqual(resp.status, 405)
        resp, _ = self.c.post("/dist/refresh", csrf=False)
        self.assertEqual(resp.status, 403)
        self.assertFalse(dist.req_file().exists())
        with mock.patch.object(dist, "status", return_value={"checked": time.time() - 600, "errors": {}}):
            resp, _ = self.c.post("/dist/refresh", {"back": f"/groups/{self.gid}"})
        self.assertEqual(header(resp, "Location"), [f"/groups/{self.gid}"])
        self.assertTrue(dist.req_file().exists())
        _, body = self.c.get(f"/groups/{self.gid}")
        self.assertIn("Обновление заказано", body)
        self.assertNotIn('action="/dist/refresh"', body, "пока заявка не забрана, кнопка неактивна")

    def test_refresh_back_is_checked(self):
        with mock.patch.object(dist, "status", return_value={"checked": None, "errors": {}}):
            for bad in ("https://evil.example/", "//evil.example", "/groups/../users", "/users", "javascript:1", ""):
                dist.clear_request()
                resp, _ = self.c.post("/dist/refresh", {"back": bad})
                self.assertEqual(header(resp, "Location"), ["/groups"], bad)
        with mock.patch.object(dist, "status", return_value={"checked": None, "errors": {}}):
            dist.clear_request()
            resp, _ = self.c.post("/dist/refresh", {"back": "/connect/done?group=g1&u=masha%2Ckolya"})
            self.assertEqual(header(resp, "Location"), ["/connect/done?group=g1&u=masha%2Ckolya"])
            team = "/connect/done?group=g1&u=" + "%2C".join(f"user{i:02d}.familiya-dlinnaya" for i in range(33))
            dist.clear_request()
            resp, _ = self.c.post("/dist/refresh", {"back": team})
            self.assertEqual(header(resp, "Location"), [team], "команда из 33 человек возвращается на свою страницу")

    def test_group_page_lists_files_with_size_sha_and_link_instead_of_missing(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("страница ходит в сеть")):
            resp, body = self.c.get(f"/groups/{self.gid}")
        self.assertEqual(resp.status, 200, body[-300:])
        card = body[body.index("Скачать дистрибутивы"):]
        card = card[:card.index("</section>")]
        sha = hashlib.sha256((dist.root() / "hiddify" / "4.1.1" / self.NAME).read_bytes()).hexdigest()
        self.assertIn(f'href="/dist/hiddify/4.1.1/{self.NAME}"', card)
        self.assertIn(sha, card)
        self.assertIn("120 Б", card)
        self.assertIn("4.1.1", card)
        self.assertIn('class="btn small primary">Скачать', card)
        self.assertIn("Обновить", card)
        # Windows: подходящего файла в релизе не оказалось → ссылка на GitHub, не выдуманный файл
        self.assertIn("не скачан: в релизе нет подходящего файла", card)
        self.assertIn('href="https://github.com/hiddify/hiddify-app/releases"', card)
        self.assertIn("ставится из магазина", card, "iPhone: приложение из App Store")
        self.assertNotIn("style=", body)
        self.assertNotRegex(body, r"\son\w+=")

    def test_ios_note_is_one_line_with_collapsed_details_and_no_untested_claims(self):
        _, body = self.c.get(f"/groups/{self.gid}")
        self.assertEqual(body.count("iPhone: приложение ставится только из App Store."), 2,
                         "строка в блоке дистрибутивов и в строке устройства")
        self.assertRegex(body, r"<details class=\"more inline\"><summary>варианты</summary>")
        self.assertIn("Apple Business Manager и MDM", body)
        self.assertIn("мы это не проверяли", body)
        self.assertIn("корпоративный Apple ID", body)
        self.assertNotIn("<details class=\"more inline\" open", body)

    def test_self_group_has_no_distribution_block_or_ios_note(self):
        groups.update(self.gid, install_mode="self")
        _, body = self.c.get(f"/groups/{self.gid}")
        self.assertNotIn("Скачать дистрибутивы", body)
        self.assertNotIn("ставится только из App Store", body)
        _, main = self.c.get("/groups/main")
        self.assertNotIn("Скачать дистрибутивы", main)

    def test_wizard_final_page_has_the_block_for_admin_group(self):
        with mock.patch.object(dist, "status", return_value={"checked": time.time() - 900, "errors": {}}):
            _, body = self.c.get(f"/connect/done?group={self.gid}&u=owner")
        self.assertIn("Скачать дистрибутивы", body)
        self.assertIn(f'href="/dist/hiddify/4.1.1/{self.NAME}"', body)
        self.assertIn(f'name="back" value="/connect/done?group={self.gid}&amp;u=owner"', body)
        groups.update(self.gid, install_mode="self")
        _, body = self.c.get(f"/connect/done?group={self.gid}&u=owner")
        self.assertNotIn("Скачать дистрибутивы", body)

    def test_page_is_light_without_network_and_csp_clean(self):
        resp, body = self.c.get(f"/groups/{self.gid}")
        self.assertIn("script-src 'self'", header(resp, "Content-Security-Policy")[0])
        self.assertNotIn("<script>", body)


if __name__ == "__main__":
    unittest.main()
