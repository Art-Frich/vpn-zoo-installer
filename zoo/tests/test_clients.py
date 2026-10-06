"""Каталог клиентов, версии из GitHub, страница /clients и блок «Что отправить»."""

import copy
import json
import re
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, ZooEnv, needs_bash
from tests.test_cli import run_cli
from tests.test_web import SIX, AppTestBase
from zoolib import clients, paths, protolib
from zoolib.web import clientviews


def catalog() -> clients.Catalog:
    return clients.load()


def fake_fetch(repo: str) -> dict:
    return {"version": "9.9.9", "url": f"https://github.com/{repo}/releases/tag/v9.9.9", "published": "2026-10-01"}


class CatalogTest(unittest.TestCase):
    def test_loads_and_is_consistent(self):
        cat = catalog()
        self.assertGreaterEqual(len(cat.clients), 8)
        ids = [c["id"] for c in cat.clients]
        self.assertEqual(len(ids), len(set(ids)))
        for plat, by_proto in cat.raw["recommended"].items():
            for pid, cid in by_proto.items():
                c = cat.client(cid)
                self.assertIn(plat, c["platforms"], f"{plat}/{pid}")
                self.assertIn(c["protocols"][pid]["s"], ("ok", "warn"))

    def test_ships_with_zoo(self):
        # фаза 09 копирует zoo/ целиком, кроме tests: каталог лежит внутри пакета
        self.assertEqual(clients.CATALOG_FILE, paths.ZOO_PKG_ROOT / "data" / "clients.json")
        self.assertTrue(clients.CATALOG_FILE.is_file())
        script = (REPO / "scripts" / "09-zoo.sh").read_text(encoding="utf-8")
        self.assertNotIn("zoo/data", script)
        self.assertIn("--exclude='zoo/tests'", script)

    def test_sing_box_clients_fail_reality(self):
        cat = catalog()
        for cid in ("hiddify", "karing", "singbox"):
            for pid in ("vless-reality", "vless-xhttp"):
                self.assertEqual(cat.client(cid)["protocols"][pid]["s"], "no", f"{cid}/{pid}")
        for plat in cat.raw["recommended"].values():
            for pid in ("vless-reality", "vless-xhttp"):
                self.assertNotIn(plat.get(pid), ("hiddify", "karing", "singbox"))
        self.assertIn("X25519MLKEM768", cat.protocols["vless-reality"]["caveat"])

    def test_nothing_claims_device_check(self):
        # на телефонах и в приложениях ничего не проверяли: честность каталога
        for c in catalog().clients:
            self.assertFalse(c["verified"]["device"], c["id"])

    def test_names_for(self):
        cat = catalog()
        self.assertTrue(cat.names_for("vless-reality").startswith("Happ"))
        self.assertIn("v2rayN", cat.names_for("allowlist"))
        self.assertNotIn("Hiddify", cat.names_for("vless-reality"))
        self.assertIn("AmneziaWG", cat.names_for("amneziawg"))

    def test_validate_rejects_broken(self):
        base = json.loads(clients.CATALOG_FILE.read_text(encoding="utf-8"))

        def broken(fn):
            raw = copy.deepcopy(base)
            fn(raw)
            with self.assertRaises(clients.ClientsError):
                clients.validate(raw)

        broken(lambda r: r["recommended"]["android"].update({"vless-reality": "hiddify"}))
        broken(lambda r: r["recommended"]["ios"].update({"tuic": "nobody"}))
        broken(lambda r: r["clients"][0]["platforms"]["android"][0].update({"url": "http://x"}))
        broken(lambda r: r["clients"][0]["protocols"].update({"amneziawg": {"s": "maybe"}}))
        broken(lambda r: r["clients"][0].update({"repo": "no slash"}))
        broken(lambda r: r["clients"].append(copy.deepcopy(r["clients"][0])))
        broken(lambda r: r.pop("check"))
        clients.validate(base)

    def test_load_reports_unreadable(self):
        with self.assertRaises(clients.ClientsError):
            clients.load(Path("/nonexistent/clients.json"))


class FetchTest(unittest.TestCase):
    def _resp(self, body):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
        return r

    def test_clean_tag(self):
        for tag, want in (("v7.25.4", "7.25.4"), ("app/v2.12.3", "2.12.3"), ("2.2.6", "2.2.6"), ("V1.0", "1.0"),
                          ("release-3", "release-3")):
            self.assertEqual(clients.clean_tag(tag), want)

    def test_request_shape_and_parse(self):
        body = {"tag_name": "v7.25.4", "html_url": "https://github.com/2dust/v2rayN/releases/tag/7.25.4",
                "published_at": "2026-09-30T10:00:00Z"}
        with mock.patch("urllib.request.urlopen", return_value=self._resp(body)) as op:
            res = clients.fetch_latest("2dust/v2rayN")
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/2dust/v2rayN/releases/latest")
        self.assertEqual(op.call_args.kwargs["timeout"], 10)
        self.assertNotIn("authorization", {k.lower() for k in req.headers}, "без токена")
        self.assertEqual(res, {"version": "7.25.4", "url": body["html_url"], "published": "2026-09-30"})

    def test_failures_become_clients_error(self):
        cases = [
            urllib.error.HTTPError("u", 403, "rate", {}, None),
            urllib.error.HTTPError("u", 404, "nf", {}, None),
            urllib.error.URLError("нет сети"),
            TimeoutError("timed out"),
        ]
        for exc in cases:
            with mock.patch("urllib.request.urlopen", side_effect=exc), self.assertRaises(clients.ClientsError):
                clients.fetch_latest("a/b")
        for bad in (b"not json", b"[]", b'{"tag_name": ""}', b'{"tag_name": "a b; rm"}'):
            with mock.patch("urllib.request.urlopen", return_value=self._resp(bad)), \
                    self.assertRaises(clients.ClientsError):
                clients.fetch_latest("a/b")
        with self.assertRaises(clients.ClientsError):
            clients.fetch_latest("../../etc/passwd")

    def test_foreign_url_dropped(self):
        body = {"tag_name": "v1", "html_url": "https://evil.example/x", "published_at": ""}
        with mock.patch("urllib.request.urlopen", return_value=self._resp(body)):
            self.assertEqual(clients.fetch_latest("a/b")["url"], "")


class CheckUpstreamTest(unittest.TestCase):
    def test_writes_cache_for_repo_clients_only(self):
        with ZooEnv():
            self.assertEqual(clients.load_cache(), {"checked": None, "versions": {}})
            res = clients.check_upstream(fetch=fake_fetch, now=lambda: 1000.0)
            self.assertEqual(res["errors"], {})
            cache = clients.load_cache()
            self.assertEqual(cache["checked"], 1000.0)
            self.assertEqual(clients.version_of(cache, "v2rayn"), "9.9.9")
            self.assertNotIn("incy", cache["versions"], "у закрытого клиента нет репозитория")
            self.assertIsNone(clients.version_of(cache, "incy"))

    def test_error_keeps_previous_version(self):
        def flaky(repo):
            if repo == "2dust/v2rayN":
                raise clients.ClientsError("HTTP 403 (лимит GitHub)")
            return fake_fetch(repo)

        with ZooEnv():
            clients.check_upstream(fetch=fake_fetch)
            res = clients.check_upstream(fetch=flaky)
            self.assertEqual(list(res["errors"]), ["v2rayn"])
            v = clients.load_cache()["versions"]["v2rayn"]
            self.assertEqual(v["version"], "9.9.9")
            self.assertIn("403", v["error"])

    def test_broken_cache_file_is_empty(self):
        with ZooEnv():
            clients.cache_file().write_text("{oops", encoding="utf-8")
            self.assertEqual(clients.load_cache()["versions"], {})


class CliTest(unittest.TestCase):
    def test_list_json_and_text(self):
        with ZooEnv():
            code, out, _ = run_cli("clients", "--json")
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertIsNone(data["checked"])
            row = next(r for r in data["clients"] if r["id"] == "hiddify")
            self.assertEqual(row["protocols"]["vless-reality"], "no")
            code, out, _ = run_cli("clients")
            self.assertEqual(code, 0)
            self.assertIn("v2rayN", out)
            self.assertIn("не проверялись", out)

    def test_check_upstream(self):
        with ZooEnv(), mock.patch.object(clients, "fetch_latest", side_effect=fake_fetch):
            code, out, _ = run_cli("clients", "--check-upstream", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(next(r for r in json.loads(out)["clients"] if r["id"] == "happ")["version"], "9.9.9")

    def test_check_upstream_exit_code(self):
        boom = clients.ClientsError("URLError: нет сети")
        with ZooEnv(), mock.patch.object(clients, "fetch_latest", side_effect=boom):
            code, _, err = run_cli("clients", "--check-upstream")
            self.assertEqual(code, 1, "все запросы упали: юнит должен показать сбой")
            self.assertIn("нет сети", err)
        calls = []

        def one_bad(repo):
            calls.append(repo)
            if len(calls) == 1:
                raise boom
            return fake_fetch(repo)

        with ZooEnv(), mock.patch.object(clients, "fetch_latest", side_effect=one_bad):
            code, _, _ = run_cli("clients", "--check-upstream")
            self.assertEqual(code, 0, "частичный сбой сети — не провал юнита")


class UnitsTest(unittest.TestCase):
    def test_daily_timer_is_enabled_and_sandboxed(self):
        d = REPO / "zoo" / "systemd"
        enabled = [ln.split()[0] for ln in (d / "enable.list").read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.startswith("#")]
        self.assertIn("zoo-clients.timer", enabled)
        for name in enabled:
            self.assertTrue((d / name).is_file(), name)
        svc = (d / "zoo-clients.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/bin/zoo clients --check-upstream", svc)
        self.assertIn("ProtectSystem=strict", svc)
        self.assertIn("ReadWritePaths=/var/lib/vpn-zoo", svc)
        timer = (d / "zoo-clients.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        for f in d.iterdir():
            self.assertNotIn(b"\r", f.read_bytes(), f"{f.name}: CRLF")


class ClientsPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.c.login()

    def test_page_renders_without_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("страница ходит в сеть")):
            resp, body = self.c.get("/clients")
        self.assertEqual(resp.status, 200, body[-400:])
        self.assertIn('href="/clients"', body)
        for want in ("Android", "iPhone", "Windows", "Happ", "AmneziaWG", "INCY", "v2rayN", "Что умеют клиенты",
                     "X25519MLKEM768", "ещё не проверялись"):
            self.assertIn(want, body)
        self.assertNotIn("style=", body)
        self.assertNotRegex(body, r"\son\w+=")
        self.assertNotIn("<script>", body)

    def test_versions_from_cache_and_links(self):
        clients.check_upstream(fetch=fake_fetch)
        _, body = self.c.get("/clients")
        self.assertIn("9.9.9", body)
        self.assertIn("проверены", body)
        self.assertIn('href="https://github.com/2dust/v2rayNG/releases"', body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertIn("в магазине", body, "у INCY версий нет: только магазин")
        self.assertIn("не проверена", body, "ссылки, собранные по id пакета, помечены")

    def test_error_shown_but_old_version_kept(self):
        clients.check_upstream(fetch=fake_fetch)
        clients.check_upstream(fetch=lambda r: (_ for _ in ()).throw(clients.ClientsError("HTTP 403")))
        _, body = self.c.get("/clients")
        self.assertIn("9.9.9", body)
        self.assertIn("не удалось:", body)

    def test_filters_by_enabled_protocols(self):
        self.env.add_manifest("amneziawg")
        self.env.add_manifest("hysteria2", enabled=False)
        _, body = self.c.get("/clients")
        matrix = body[body.index("Что умеют клиенты"):]
        self.assertIn(">AmneziaWG<", matrix)
        self.assertNotIn(">TUIC v5<", matrix)
        self.assertNotIn(">Hysteria2<", matrix)

    def test_matrix_marks(self):
        _, body = self.c.get("/clients")
        self.assertRegex(body, r'class="badge bad" title="не работает: [^"]*X25519MLKEM768')
        self.assertIn('class="badge warn" title="с оговоркой: игнорирует пин', body)

    def test_broken_catalog_does_not_break_page(self):
        with mock.patch.object(clients, "load", side_effect=clients.ClientsError("clients.json: нет ключа")):
            resp, body = self.c.get("/clients")
        self.assertEqual(resp.status, 200)
        self.assertIn("нет ключа", body)


def link(proto: str, uri: str, kind: str = "uri") -> protolib.Link:
    return protolib.Link(uri=uri, label="", proto_id=proto, kind=kind)


VLESS = link("vless-reality", "vless://u@1.2.3.4:443?type=tcp#x")
AWG_ANDROID = link("amneziawg", "/etc/vpn-setup/clients/masha/masha-amneziawg-android.conf", "file")
AWG_COMMON = link("amneziawg", "/etc/vpn-setup/clients/masha/masha-amneziawg.conf", "file")
AWG_KEY = link("amneziawg", "vpn://AAAA")
RULES = link("allowlist", "/etc/vpn-setup/clients/masha/v2rayn-routing.json", "file")


class PackTest(unittest.TestCase):
    def pack(self, plat, links, cache=None):
        return clientviews.build_pack(catalog(), cache or {"checked": None, "versions": {}}, plat, links, [])

    def test_android_prefers_awg_qr_with_android_tab(self):
        p = self.pack("android", [VLESS, AWG_ANDROID, AWG_COMMON, AWG_KEY])
        self.assertEqual((p.proto, p.client["id"], p.method, p.tab), ("amneziawg", "amneziawg", "qr", "Android"))
        self.assertIn("QR — плитка «AmneziaWG», вкладка «Android»", p.sends[0])
        self.assertTrue(p.steps[0].startswith("Скачайте «AmneziaWG»"))
        self.assertIn("github.com/amnezia-vpn/amneziawg-android", p.steps[0], "проверенные ссылки — раньше")
        self.assertIn("Brave", p.steps[-1])
        self.assertIn("2ip.ru", p.steps[-1], "у AWG echo-правила нет: адрес сервера виден")

    def test_android_without_awg_uses_happ(self):
        p = self.pack("android", [VLESS])
        self.assertEqual((p.client["id"], p.method), ("happ", "qr"))
        self.assertIn("Приложения через VPN:", " ".join(p.steps))
        self.assertNotIn("вкладка", p.sends[0])
        self.assertIn("не откроется", p.steps[-1], "echo-сервисы на сервере блокируются для Xray-протоколов")

    def test_windows_link_and_rules_file(self):
        p = self.pack("windows", [VLESS, RULES])
        self.assertEqual((p.client["id"], p.method), ("v2rayn", "link"))
        self.assertEqual(len(p.sends), 2)
        self.assertIn("«Приложения через VPN»", p.sends[1])
        self.assertIn("v2rayn-routing.json", " ".join(p.steps))
        self.assertEqual(len(self.pack("windows", [VLESS]).sends), 1, "без файла правил — один пункт")

    def test_ios_and_no_match(self):
        p = self.pack("ios", [VLESS])
        self.assertEqual(p.client["id"], "incy")
        self.assertIn("любом браузере", p.steps[-1])
        self.assertIsNone(self.pack("ios", [link("tuic", "tuic://x")]), "для TUIC на iPhone клиента не выбрано")
        self.assertIsNone(self.pack("macos", [VLESS]), "на macOS для VLESS проверенного клиента нет")
        self.assertIsNone(self.pack("android", []))

    def test_amneziavpn_takes_vpn_key_not_conf_qr(self):
        p = self.pack("macos", [AWG_COMMON, AWG_KEY])
        self.assertEqual((p.client["id"], p.method), ("amneziavpn", "link"))
        self.assertEqual(p.tab, "Компьютер, iPhone")
        # без vpn:// остаётся файл
        self.assertEqual(self.pack("macos", [AWG_COMMON]).method, "file")

    def test_conf_never_offered_as_link_and_vpn_key_not_to_wg_clients(self):
        p = self.pack("android", [AWG_KEY])
        self.assertIsNone(p, "vpn:// клиентам AmneziaWG/WG Tunnel не подходит")

    def test_version_only_for_github_clients(self):
        cache = {"checked": 1.0, "versions": {"amneziawg": {"version": "2.1"}, "incy": {"version": "7"}}}
        self.assertIn("(версия 2.1)", self.pack("android", [AWG_ANDROID], cache).steps[0])
        self.assertNotIn("версия", self.pack("ios", [VLESS], cache).steps[0])

    def test_message_has_no_secrets(self):
        p = self.pack("android", [VLESS, AWG_ANDROID])
        self.assertNotIn("vless://", p.message)
        self.assertNotIn(".conf", p.message)
        self.assertTrue(p.message.startswith("VPN на Android: что сделать\n1. "))


@needs_bash
class HandoffPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        for pid in SIX:
            self.env.add_protocol(pid)
        from zoolib import users
        users.bootstrap()
        users.add_user("masha")
        self.c.login()

    def test_block_on_user_page(self):
        clients.check_upstream(fetch=fake_fetch)
        resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200, body[-300:])
        main = body[body.index("<main"):]
        self.assertLess(main.index("Подключение"), main.index("Что отправить"))
        self.assertLess(main.index("Что отправить"), main.index(">Профиль<"))
        self.assertIn("Скопировать сообщение", body)
        self.assertIn('data-copy="msg-android"', body)
        self.assertIn('data-copy="msg-windows"', body)
        self.assertLess(body.index("Другие платформы"), body.index('data-copy="msg-macos"'))
        self.assertLess(body.index('data-copy="msg-windows"'), body.index("Другие платформы"))
        self.assertIn("не отправляйте через MAX и VK", body)
        self.assertNotIn("style=", body)
        msg = re.search(r'<textarea id="msg-android"[^>]*>(.*?)</textarea>', body, re.S).group(1)
        self.assertIn("1. Скачайте", msg)
        self.assertNotIn("vless://", msg, "в сообщении только инструкция, ключи — отдельно")
        # плитки протоколов по-прежнему со списком клиентов из каталога
        self.assertIn("Клиенты: ", body)

    def test_disabled_catalog_hides_block_only(self):
        with mock.patch.object(clients, "load", side_effect=clients.ClientsError("x")):
            resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertNotIn("Что отправить", body)
        self.assertIn("Подключение", body)

    def test_page_stays_light(self):
        _, body = self.c.get("/users/masha")
        self.assertLessEqual(len(body.encode("utf-8")), 24 * 1024)


if __name__ == "__main__":
    unittest.main()
