"""Каталог клиентов, версии из GitHub, страница /clients и блок «Подключить»."""

import copy
import json
import html
import re
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, ZooEnv, needs_bash
from tests.test_cli import run_cli
from tests.test_web import SIX, AppTestBase, header
from zoolib import clients, groups, paths, protolib
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

    def test_hiddify_checked_on_stand(self):
        # прогон ядра hiddify-core 4.1.0 на стенде 07.10.2026 (research/2026-10-07/hiddify-compat_07-10-26.md)
        cat = catalog()
        h = cat.client("hiddify")
        for pid in ("ss2022", "tuic"):
            self.assertEqual(h["protocols"][pid]["s"], "ok", pid)
        for pid in ("hysteria2", "hysteria2-obfs"):
            self.assertEqual(h["protocols"][pid]["s"], "warn", pid)
            self.assertIn("пин", h["protocols"][pid]["note"], "работает, но пин сертификата не проверяется")
        for pid in ("vless-reality", "vless-xhttp", "amneziawg"):
            self.assertEqual(h["protocols"][pid]["s"], "no", pid)
        for pid, st in h["protocols"].items():
            self.assertIn("07.10.2026", st["note"], pid)
            self.assertIn("hiddify-core 4.1.0", st["note"], pid)
        self.assertEqual(h["verified"]["date"], "2026-10-07")
        self.assertFalse(h["verified"]["device"])
        self.assertEqual(sorted(h["platforms"]), ["android", "ios", "linux", "macos", "windows"])
        self.assertTrue(cat.no_ru_store(h, "ios"), "в App Store РФ Hiddify нет")
        self.assertFalse(cat.no_ru_store(h, "android"))
        self.assertEqual(h["per_app"], "ui")
        self.assertIn("Прокси для приложений", cat.per_app_steps(h, "android"))
        self.assertIsNone(cat.per_app_steps(h, "windows"), "режим приложений в Hiddify только на Android")
        # рекомендован только там, где проверен и нет лучшего: TUIC на Android; VLESS — никогда
        by = {(plat, pid) for plat, m in cat.raw["recommended"].items() for pid, cid in m.items() if cid == "hiddify"}
        self.assertEqual(by, {("android", "tuic")})
        for plat in ("windows", "macos", "linux"):
            self.assertNotIn("tuic", cat.raw["recommended"][plat], "на десктопе рекомендованного клиента TUIC нет")

    def test_singbox_checked_on_stand(self):
        cat = catalog()
        sb = cat.client("singbox")
        for pid in ("ss2022", "hysteria2", "hysteria2-obfs", "tuic"):
            self.assertEqual(sb["protocols"][pid]["s"], "ok", pid)
            self.assertIn("07.10.2026", sb["protocols"][pid]["note"], pid)
        for pid in ("vless-reality", "vless-xhttp", "amneziawg"):
            self.assertEqual(sb["protocols"][pid]["s"], "no", pid)
        self.assertIn("xhttp", sb["protocols"]["vless-xhttp"]["note"], "у sing-box нет транспорта XHTTP")
        self.assertEqual(sb["verified"]["date"], "2026-10-07")

    def test_no_unify_clients(self):
        cat = catalog()
        self.assertEqual(cat.client("amneziavpn")["no_unify"], ["android"])
        self.assertEqual(cat.client("incy")["no_unify"], ["android"])
        raw = json.loads(clients.CATALOG_FILE.read_text(encoding="utf-8"))
        raw["clients"][0]["no_unify"] = ["plan9"]
        with self.assertRaises(clients.ClientsError):
            clients.validate(raw)

    def test_per_app_steps_are_per_platform(self):
        cat = catalog()
        for c in cat.clients:
            self.assertIsNone(cat.per_app_steps(c, "ios"), f"{c['id']}: на iOS приложения через VPN невозможны")
        av = cat.client("amneziavpn")
        self.assertIsNone(cat.per_app_steps(av, "windows"), "на Windows у AmneziaVPN только исключение приложений")
        self.assertIn("только приложения из списка", cat.per_app_steps(av, "android"))
        self.assertIn("{apps}", cat.per_app_steps(cat.client("happ"), "android"), "приложения — из списка группы")

    def test_store_flag(self):
        cat = catalog()
        self.assertTrue(cat.no_ru_store(cat.client("happ"), "ios"))
        self.assertFalse(cat.no_ru_store(cat.client("happ"), "android"))
        self.assertFalse(cat.no_ru_store(cat.client("incy"), "ios"))

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

        def happ(r):
            return next(c for c in r["clients"] if c["id"] == "happ")

        broken(lambda r: happ(r).update({"per_app_steps": "строка вместо словаря"}))
        broken(lambda r: happ(r).update({"per_app_steps": {"ios": "нельзя"}}))
        broken(lambda r: happ(r).update({"per_app_steps": {"windows": "у клиента нет такой платформы"}}))
        broken(lambda r: happ(r).update({"no_ru_store": ["windows"]}))
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


class RequestTest(unittest.TestCase):
    def test_request_is_a_file_and_rate_limited(self):
        with ZooEnv():
            self.assertEqual(clients.check_state(1000.0), (True, ""))
            ok, why = clients.request_check(1000.0)
            self.assertTrue(ok, why)
            self.assertEqual(clients.req_file().name, "clients-req")
            ok, why = clients.request_check(1100.0)
            self.assertFalse(ok)
            self.assertIn("уже заказана", why)
            self.assertFalse(clients.check_state(1100.0)[0])
            # заявку никто не забрал за 15 минут — считаем потерянной и принимаем новую
            self.assertTrue(clients.check_state(1000.0 + clients.REQ_STALE + 1)[0])

    def test_recent_check_blocks_for_ten_minutes(self):
        with ZooEnv():
            clients.check_upstream(fetch=fake_fetch, now=lambda: 5000.0)
            ok, why = clients.request_check(5000.0 + clients.RATE_LIMIT - 1)
            self.assertFalse(ok)
            self.assertIn("10 минут", why)
            self.assertFalse(clients.req_file().exists(), "отказ заявку не создаёт")
            self.assertTrue(clients.request_check(5000.0 + clients.RATE_LIMIT + 1)[0])

    def test_check_upstream_command_takes_the_request(self):
        with ZooEnv(), mock.patch.object(clients, "fetch_latest", side_effect=fake_fetch):
            clients.request_check(1.0)
            self.assertTrue(clients.req_file().exists())
            code, _, _ = run_cli("clients", "--check-upstream", "--json")
            self.assertEqual(code, 0)
            self.assertFalse(clients.req_file().exists(), "заявка снята: path-юнит не гоняет сервис по кругу")
            clients.request_check(2.0)
            run_cli("clients", "--json")
            self.assertTrue(clients.req_file().exists(), "просмотр заявку не трогает")


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
        # «Проверить сейчас»: файл-заявка → .path → тот же сервис (у админки нет права на systemctl)
        self.assertIn("zoo-clients.path", enabled)
        path = (d / "zoo-clients.path").read_text(encoding="utf-8")
        self.assertIn(f"PathExists=/var/lib/vpn-zoo/{clients.REQ_NAME}", path)
        self.assertIn("Unit=zoo-clients.service", path)
        for f in d.iterdir():
            self.assertNotIn(b"\r", f.read_bytes(), f"{f.name}: CRLF")


class CatalogFactsTest(unittest.TestCase):
    """Каталог по проверке App Store (12 витрин), Google Play и Microsoft Store от 07.10.2026."""

    def test_app_store_and_store_links(self):
        cat = catalog()
        awg = cat.client("amneziawg")
        self.assertEqual(awg["platforms"]["ios"], [{"kind": "appstore", "checked": True,
                                                   "url": "https://apps.apple.com/ru/app/amneziawg/id6478942365"}])
        self.assertFalse(cat.no_ru_store(awg, "ios"), "AmneziaWG в App Store РФ есть")
        self.assertEqual(cat.status(awg, "amneziawg", "ios"), "warn", "импорт формата 3.1 не проверен на стенде")
        self.assertEqual(cat.status(awg, "amneziawg", "android"), "ok")
        self.assertTrue(next(ln for ln in awg["platforms"]["android"] if ln["kind"] == "play")["checked"],
                        "Play org.amnezia.awg проверен")
        vpn = cat.client("amneziavpn")
        self.assertEqual(vpn["platforms"]["ios"][0]["url"], "https://apps.apple.com/us/app/amneziavpn/id1600529900")
        self.assertTrue(cat.no_ru_store(vpn, "ios"))
        self.assertIn("org.amnezia.vpn", [ln["url"] for ln in vpn["platforms"]["android"]][1])
        self.assertEqual(cat.recommended("ios", "amneziawg")["id"], "amneziawg", "на iPhone уступает AmneziaWG из РФ-магазина")
        sb = cat.client("singbox")
        self.assertEqual(sb["name"], "sing-box")
        self.assertEqual(sb["platforms"]["ios"][0], {"kind": "appstore", "checked": True,
                                                    "url": "https://apps.apple.com/ru/app/sing-box-mt/id6785326793"})
        self.assertIn("play.google.com/store/apps/details?id=io.nekohasekai.sfa", sb["platforms"]["android"][1]["url"])
        self.assertEqual(sb["import"], {}, "ссылок не берёт: JSON-профили мы не выдаём")
        self.assertNotIn("TestFlight", sb["notes"])
        hid = cat.client("hiddify")
        self.assertIn({"kind": "msstore", "url": "https://apps.microsoft.com/detail/9pdfnl3qv2s5", "checked": True},
                      hid["platforms"]["windows"])
        self.assertIn("кроме РФ", hid["notes"])
        self.assertTrue(cat.no_ru_store(hid, "ios"))
        happ = cat.client("happ")
        self.assertTrue(all(ln["checked"] for plat in ("android", "ios") for ln in happ["platforms"][plat]))
        self.assertIn("6791998089", happ["notes"], "подделки в RU-поиске")
        self.assertTrue(all(ln["checked"] for ln in cat.client("wgtunnel")["platforms"]["android"]))
        self.assertEqual(cat.client("incy")["platforms"]["ios"][0]["url"], "https://apps.apple.com/ru/app/incy/id6756943388")
        for fake in ("v2raytun", "foxray"):
            self.assertIsNone(cat.client(fake), "по id ничего нет, в поиске подделки")

    def test_names_of_protocols_and_the_rules_key(self):
        cat = catalog()
        self.assertEqual({p: cat.protocols[p]["title"] for p in cat.protocols if p != "allowlist"},
                         {"vless-reality": "VLESS Vision", "vless-xhttp": "VLESS XHTTP", "ss2022": "Shadowsocks",
                          "hysteria2": "Hysteria2", "hysteria2-obfs": "Hysteria2 + Salamander", "amneziawg": "AmneziaWG",
                          "tuic": "TUIC"})
        self.assertEqual(cat.protocols["allowlist"]["title"], "Правила маршрутизации v2rayN")
        self.assertEqual(set(cat.raw["check"]), {"brave", "apps", "device"})
        for text in cat.raw["check"].values():
            self.assertNotIn("как обычно", text, "про банки — только там, где это правда (via)")
        for c in cat.clients:   # каждый шаг импорта начинается с названия приложения или говорит, что скопировать
            for how, text in c.get("import", {}).items():
                self.assertTrue(f"«{c['name']}»" in text or c["id"] == "hysteria", (c["id"], how, text))

    def test_client_without_import_is_never_picked_and_not_listed_on_tiles(self):
        cat = catalog()
        for plat in cat.platforms:
            ids = [o["client"]["id"] for o in groups.client_options(cat, plat, cat.real_protocols())]
            self.assertNotIn("singbox", ids, plat)
        self.assertNotIn("sing-box", cat.names_for("hysteria2"))


class ClientsPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.c.login()

    def test_page_renders_without_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("страница ходит в сеть")):
            resp, body = self.c.get("/clients")
        self.assertEqual(resp.status, 200, body[-400:])
        self.assertIn('href="/clients"', body)
        for want in ("Android", "iPhone", "Windows", "Happ", "AmneziaWG", "INCY", "v2rayN", "Все приложения",
                     "X25519MLKEM768", "ещё не проверялись"):
            self.assertIn(want, body)
        self.assertNotIn("style=", body)
        self.assertNotRegex(body, r"\son\w+=")
        self.assertNotIn("<script>", body)
        self.assertNotIn("<h3>Ставить</h3>", body, "что ставить группе — в «Подключить людей», а не второй подбор здесь")
        self.assertIn("Что ставить группе — в «Подключить людей» и на странице группы →", body)
        self.assertIn('<a href="/clients" class="active" aria-current="page">Приложения</a>', body)
        self.assertIn('<a href="/apps">Через VPN</a>', body)
        self.assertNotIn(">Клиенты<", body)

    def test_versions_from_cache_and_links(self):
        clients.check_upstream(fetch=fake_fetch)
        _, body = self.c.get("/clients")
        self.assertIn("9.9.9", body)
        self.assertIn("предложены последние версии · ", body)
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
        matrix = body[body.index("Все приложения"):]
        self.assertIn(">AmneziaWG<", matrix)
        self.assertNotIn(">TUIC<", matrix)
        self.assertNotIn(">Hysteria2<", matrix)

    def test_matrix_marks(self):
        _, body = self.c.get("/clients")
        table = body[body.index("<h3>Все приложения</h3>"):]
        table = table[:table.index("</section>")]
        self.assertNotIn('class="chip bad"', table, "не работающие протоколы — одной строкой, без красных чипов")
        self.assertIn("Не работает: VLESS Vision, VLESS XHTTP, AmneziaWG", table)
        self.assertIn("в исследовании не проверяли — INCY: ", table, "одинаковая заметка — одной строкой под таблицей")
        self.assertNotIn('title="в исследовании не проверяли', table, "и не подсказкой у каждого чипа")
        self.assertRegex(body, r"X25519MLKEM768", "причины — в «Почему «с оговоркой» и «не работает»»")

    def table_rows(self, body):
        """Строки таблицы «Все приложения»: {имя приложения: html строки}."""
        table = body[body.index("<h3>Все приложения</h3>"):]
        table = table[:table.index("</section>")]
        rows = re.findall(r"<tr><td[^>]*><span class=\"app-cell\"><strong>(.*?)</strong>(.*?)</tr>", table, re.S)
        return {name: row for name, row in rows}

    def test_one_row_per_app_no_per_platform_duplicates(self):
        _, body = self.c.get("/clients")
        cat = catalog()
        rows = self.table_rows(body)
        self.assertEqual(list(sorted(rows)), sorted(c["name"] for c in cat.clients), "строка = приложение, ни одного дубля")
        self.assertNotIn("<h3>Android</h3>", body, "карточек по платформам больше нет")
        self.assertNotIn("Что умеют клиенты", body)
        # столбцы устройств: есть / есть, но не из РФ-магазина / нет
        happ = rows["Happ"]
        self.assertEqual(happ.count('class="chip ok">✓</span>'), 1, "Android")
        self.assertNotIn("В российском App Store его нет", happ, "объяснение — один раз под таблицей, не подсказкой в строке")
        self.assertIn(">нет в App Store РФ</span>", happ, "iPhone: Happ нет в App Store РФ")
        self.assertNotIn("✓!", body, "«!» заменён словами, как в мастере")
        self.assertIn(">—</span>", happ, "Windows: Happ нет")
        # протоколы — по каталогу: зелёный заявлен, жёлтый с оговоркой; не работающие — строкой
        hid = rows["Hiddify"]
        self.assertRegex(hid, r'class="chip warn" title="работает[^"]*">Hysteria2.{1,4}стенд</span>')
        self.assertRegex(hid, r'class="chip ok">TUIC.{0,3}стенд</span>', "заметка общая с Shadowsocks: строкой под таблицей")
        self.assertNotIn("Не работает", hid)
        self.assertIn("Не работает: VLESS Vision, VLESS XHTTP, AmneziaWG — sing-box, Hiddify", body)
        self.assertNotIn('class="chip bad"', hid)
        self.assertIn(">Hysteria2</span>", rows["Happ"], "без отметки «стенд»: Happ только по документации")
        self.assertNotIn("стенд", rows["Happ"])
        # ссылки и заметка — внутри строки, под спойлером
        self.assertIn("<summary>ссылки</summary>", happ)
        self.assertIn("github.com/Happ-proxy/happ-android", happ)
        self.assertIn("App Store", happ)
        self.assertIn("Названия пунктов", happ, "заметка клиента — в строке")
        self.assertLess(list(rows).index("Happ"), list(rows).index("Hiddify"), "рекомендованные — первыми")

    def test_there_is_no_second_install_picker_here(self):
        _, body = self.c.get("/clients")
        self.assertNotIn("Ставить</h3>", body)
        self.assertNotIn('class="dev-row"', body)
        self.assertNotIn("В мастере «Новая группа» то же считается", body)
        self.assertRegex(body, r'<a href="/connect/new" data-swap>Что ставить группе')
    def test_dev_filter_is_a_link_not_js_and_filters_the_table(self):
        _, body = self.c.get("/clients?dev=windows")
        self.assertIn('href="/clients?dev=windows" class="active"', body)
        rows = self.table_rows(body)
        self.assertIn("v2rayN", rows)
        self.assertNotIn("INCY", rows, "INCY на Windows нет")
        self.assertNotIn("Happ", rows)
        _, body = self.c.get("/clients?dev=macos")
        self.assertIn("<strong>AmneziaVPN</strong>", body)
        _, body = self.c.get("/clients?dev=nope")
        self.assertEqual(len(self.table_rows(body)), len(catalog().clients), "неизвестное устройство — без фильтра")
        self.assertNotIn("<script>", body)

    def test_check_now_button_and_request(self):
        clients.check_upstream(fetch=fake_fetch, now=lambda: time.time() - 7500)
        _, body = self.c.get("/clients")
        self.assertIn("предложены последние версии · 2 ч", body)
        self.assertRegex(body, r'<form method="post" action="/clients/check"[^>]*>.*?Проверить сейчас')
        resp, _ = self.c.post("/clients/check")
        self.assertEqual(header(resp, "Location"), ["/clients"])
        self.assertTrue(clients.req_file().exists(), "заявка — файл для zoo-clients.path")
        _, body = self.c.get("/clients")
        self.assertIn("Проверка заказана", body)
        self.assertNotIn('action="/clients/check"', body, "пока заявка не забрана, кнопка неактивна")
        self.assertRegex(body, r'<button type="button" class="btn small" disabled title="проверка уже заказана')
        self.c.post("/clients/check")
        _, body = self.c.get("/clients")
        self.assertIn("Проверка уже заказана", body)
        resp, _ = self.c.post("/clients/check", csrf=False)
        self.assertEqual(resp.status, 403)

    def test_recent_check_disables_button(self):
        clients.check_upstream(fetch=fake_fetch)
        _, body = self.c.get("/clients")
        self.assertIn("предложены последние версии · только что", body)
        self.assertIn("версии проверяли меньше 10 минут назад", body)
        self.c.post("/clients/check")
        self.assertFalse(clients.req_file().exists())
        _, body = self.c.get("/clients")
        self.assertIn("Версии проверяли меньше 10 минут назад", body)

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
CHECK = clients.load().raw["check"]


class PackTest(unittest.TestCase):
    def pack(self, plat, links, cache=None):
        return clientviews.build_pack(catalog(), cache or {"checked": None, "versions": {}}, plat, links, [])

    def test_android_set_awg_qr_with_android_tab_then_happ(self):
        p = self.pack("android", [VLESS, AWG_ANDROID, AWG_COMMON, AWG_KEY])
        # ни один клиент не умеет всё: AmneziaWG (первым в раздаче) + Happ для VLESS
        self.assertEqual([s.client["id"] for s in p.sections], ["amneziawg", "happ"])
        s = p.sections[0]
        self.assertEqual((s.proto, s.method), ("amneziawg", "file"), "сообщение на том же телефоне: файл, а не QR")
        self.assertTrue(s.install.startswith("Установите «AmneziaWG»"))
        self.assertIn("play.google.com/store/apps/details?id=org.amnezia.awg", s.install, "проверенные ссылки — раньше")
        self.assertEqual(s.check, CHECK["brave"])
        self.assertNotIn("2ip", s.check)

    def test_per_app_step_names_the_group_list_not_brave(self):
        from zoolib import allowlist
        al = allowlist.Allowlist(Path("x"), ["com.brave.browser", "org.telegram.messenger"], ["brave.exe"],
                                 titles={"com.example.crm": "CRM"})
        args = (catalog(), {"checked": None, "versions": {}}, "android", [VLESS], [], {"android": ["happ"]})
        s = clientviews.build_pack(*args, al=al).sections[0]
        self.assertIn("отметьте Brave и Telegram.", " ".join(s.steps), "без списка группы — общий список")
        self.assertIn("Приложения через VPN в «Happ»: ", " ".join(s.steps))
        s = clientviews.build_pack(*args, al=al, apps=["com.whatsapp", "com.example.crm"]).sections[0]
        steps = " ".join(s.steps)
        self.assertIn("отметьте WhatsApp и CRM.", steps)
        self.assertNotIn("Brave", steps + s.check, "Brave нет в списке группы")
        self.assertNotIn("{apps}", steps)
        many = [f"com.x.app{i}" for i in range(9)]
        steps = " ".join(clientviews.build_pack(*args, al=al, apps=many).sections[0].steps)
        self.assertIn("com.x.app7 и com.x.app8.", steps, "весь список, без «и ещё N»: отмечать больше не по чему")
        self.assertNotIn("ещё", steps)
        p = clientviews.build_pack(*args, al=al, apps=["com.whatsapp", "com.example.crm"])
        self.assertEqual(p.sections[0].check, CHECK["apps"].replace("{app}", "WhatsApp"),
                         "Brave нет в списке — проверка в первом приложении списка, а не «заблокированный сайт»")
        self.assertNotIn("Brave", p.message)
        self.assertIn("Через VPN — только WhatsApp и CRM, остальное напрямую.", p.message)
        self.assertNotEqual(clientviews.pack_sig(clientviews.build_pack(*args, al=al)),
                            clientviews.pack_sig(clientviews.build_pack(*args, al=al, apps=["com.whatsapp"])),
                            "другой список — другой текст: текст группы не подставляется человеку со своим списком")

    def gpack(self, plat, links, prefer, order=None):
        return clientviews.build_pack(catalog(), {"checked": None, "versions": {}}, plat, links, [], prefer, order)

    def test_group_client_and_primary_protocol_win(self):
        hy2 = link("hysteria2", "hysteria2://x@1.2.3.4:443#x")
        links = [VLESS, hy2, AWG_ANDROID]
        # у группы v2rayNG и основной протокол Hysteria2: не рекомендованный Happ и не AWG первым
        s = self.gpack("android", links, {"android": ["v2rayng"]}, ["hysteria2", "vless-reality"]).sections
        self.assertEqual([(x.client["id"], [i.proto for i in x.items]) for x in s],
                         [("v2rayng", ["hysteria2", "vless-reality"])])
        s = self.gpack("android", links, {"android": ["v2rayng"]}, ["vless-reality", "hysteria2"]).sections
        self.assertEqual([i.proto for i in s[0].items], ["vless-reality", "hysteria2"])
        # клиент группы не умеет первый протокол группы — берём те, что умеет
        s = self.gpack("android", links, {"android": ["wgtunnel"]}, ["vless-reality", "amneziawg"]).sections
        self.assertEqual((s[0].client["id"], s[0].proto, s[0].method), ("wgtunnel", "amneziawg", "file"))
        # группа «все включённые»: порядок фиксированный (PRIORITY), клиент — группы
        s = self.gpack("android", links, {"android": ["v2rayng"]}, None).sections
        self.assertEqual([i.proto for i in s[0].items], ["hysteria2", "vless-reality"])
        # у группы без набора приложений раздавать нечего: фолбэка на рекомендованные каталога нет
        self.assertIsNone(self.gpack("android", links, {}, ["hysteria2"]))
        # человек без группы — рекомендованные каталога: AWG и остальное
        self.assertEqual([x.client["id"] for x in self.gpack("android", links, None, ["hysteria2"]).sections],
                         ["amneziawg", "happ"])

    def test_group_prefs_order_is_the_fixed_priority(self):
        from zoolib import groups
        g = groups.Group("g1", "Г", ["ss2022", "vless-reality", "hysteria2-obfs", "amneziawg", "hysteria2"])
        self.assertEqual(g.protocols, ["hysteria2", "amneziawg", "hysteria2-obfs", "vless-reality", "ss2022"],
                         "порядок группы нормализуется при чтении")
        self.assertEqual(clientviews.group_prefs(g)[1], g.protocols)
        self.assertIsNone(clientviews.group_prefs(groups.Group("main", "Основная", ["*"]))[1])
        self.assertEqual(clientviews.group_prefs(None), (None, None), "без группы — рекомендованные каталога")
        self.assertEqual(clientviews.group_prefs(g)[0], {}, "группа без набора — ничего, не «как без группы»")

    def test_salamander_and_plain_hysteria2_are_picked_separately(self):
        plain = link("hysteria2", "hysteria2://x@1.2.3.4:443/?sni=x#x")
        obfs = link("hysteria2", "hysteria2://x@1.2.3.4:8443/?sni=x&obfs=salamander&obfs-password=p#x")
        hop = link("hysteria2", "hysteria2://x@1.2.3.4:443,20000-30000/?sni=x#x")
        links = [plain, hop, obfs]
        self.assertEqual((plain.variant, hop.variant, obfs.variant), ("hysteria2", "hysteria2", "hysteria2-obfs"))
        for order, want in ((["hysteria2"], {"hysteria2": 0}),
                            (["hysteria2-obfs"], {"hysteria2-obfs": 2}),
                            (["hysteria2", "hysteria2-obfs"], {"hysteria2": 0, "hysteria2-obfs": 2})):
            p = self.gpack("windows", links, {"windows": ["v2rayn"]}, order)
            items = [i for s in p.sections for i in s.items]
            self.assertEqual([i.proto for i in items], list(want), order)
            c = catalog()
            cl = c.client("v2rayn")
            for i in items:
                self.assertEqual(clientviews.pick_link(i.proto, cl, "windows", links, i.method), want[i.proto], (order, i.proto))
        # группа «все»: оба варианта, если ссылки есть
        p = self.gpack("windows", links, {"windows": ["v2rayn"]}, None)
        self.assertEqual([i.proto for s in p.sections for i in s.items], ["hysteria2", "hysteria2-obfs"])
        # только обычная ссылка у человека: Salamander раздавать нечего
        p = self.gpack("windows", [plain], {"windows": ["v2rayn"]}, ["hysteria2", "hysteria2-obfs"])
        self.assertEqual([i.proto for s in p.sections for i in s.items], ["hysteria2"])
        self.assertEqual(p.sections[0].tiles, "Hysteria2")
        p = self.gpack("windows", links, {"windows": ["v2rayn"]}, ["hysteria2-obfs"])
        self.assertEqual(p.sections[0].tiles, "Hysteria2 + Salamander")

    def test_synth_links_for_group_pack_and_coverage_use_variant_ids(self):
        from zoolib import groups
        cat = catalog()
        synth = clientviews.synth_links(["hysteria2-obfs"])
        self.assertEqual([ln.variant for ln in synth if ln.proto_id != "allowlist"], ["hysteria2-obfs"])
        p = self.gpack("windows", synth, {"windows": ["v2rayn"]}, ["hysteria2-obfs"])
        self.assertEqual([i.proto for i in p.sections[0].items], ["hysteria2-obfs"])
        done, miss = groups.coverage(cat, "android", ["hysteria2-obfs"], ["v2rayng"])
        self.assertEqual((done, miss), (["hysteria2-obfs"], []))
        done, miss = groups.coverage(cat, "android", ["hysteria2-obfs", "hysteria2"], ["happ"])
        self.assertEqual((done, miss), (["hysteria2"], ["hysteria2-obfs"]), "Happ не заявлен для Salamander")

    def test_self_install_points_to_store_not_github(self):
        hy2 = link("hysteria2", "hysteria2://x@1.2.3.4:443#x")
        cache = {"checked": None, "versions": {}}
        args = (catalog(), cache, "android", [hy2], [], {"android": ["hiddify"]}, ["hysteria2"])
        own = clientviews.build_pack(*args, stores=True).sections[0]
        self.assertIn("play.google.com", own.install, "люди ставят сами — из Google Play (D49)")
        self.assertEqual(own.links[0]["kind"], "play")
        it = clientviews.build_pack(*args).sections[0]
        self.assertIn("github.com", it.install, "ИТ ставит файлом из релиза")

    def test_warn_coverage_is_not_plain_ready(self):
        cat = catalog()
        names = clientviews.proto_names(cat)
        self.assertEqual(clientviews.coverage_label(cat, "android", ["hysteria2"], ["hiddify"], names), ("с оговоркой", "warn"))
        self.assertEqual(clientviews.coverage_label(cat, "android", ["hysteria2"], ["happ"], names), ("все протоколы", "ok"))
        self.assertEqual(clientviews.coverage_label(cat, "android", ["hysteria2", "amneziawg"], ["happ"], names),
                         ("нет AmneziaWG", "warn"))
        items = clientviews.caveat_items(cat, {"android": ["hiddify"], "windows": ["hiddify"]}, ["hysteria2"], names)
        self.assertEqual([t_ for t_, _ in items], ["Hiddify: Hysteria2 без проверки сертификата"], "одна на все устройства")
        self.assertIn("insecure=1", items[0][1], "детали ядра — в полной заметке («подробнее»)")
        self.assertEqual(clientviews.caveat_items(cat, {"android": ["happ", "hiddify"]}, ["hysteria2"], names), [],
                         "есть приложение без оговорки — оговорки нет")
        # AmneziaWG на iPhone — «ok» по коду клиента, но импорт 3.1 не проверен: с оговоркой только там
        self.assertEqual(clientviews.coverage_label(cat, "ios", ["amneziawg"], ["amneziawg"], names), ("с оговоркой", "warn"))
        self.assertEqual(clientviews.coverage_label(cat, "android", ["amneziawg"], ["amneziawg"], names), ("все протоколы", "ok"))
        self.assertEqual([t_ for t_, _ in clientviews.caveat_items(cat, {"ios": ["amneziawg"]}, ["amneziawg"], names)],
                         ["AmneziaWG: импорт формата 3.1 не проверен"])
    def test_group_set_gives_one_section_per_client_with_own_protocols(self):
        hy2 = link("hysteria2", "hysteria2://x@1.2.3.4:443#x")
        p = self.gpack("android", [VLESS, hy2, AWG_ANDROID], {"android": ["happ", "amneziawg"]},
                       ["hysteria2", "vless-reality", "amneziawg"])
        self.assertEqual([(s.client["id"], [i.proto for i in s.items]) for s in p.sections],
                         [("happ", ["hysteria2", "vless-reality"]), ("amneziawg", ["amneziawg"])])
        self.assertEqual(p.sections[0].tiles, "Hysteria2, VLESS Vision")
        msg = p.message
        # одним списком: сначала установка всех приложений, потом импорт каждого; каждый шаг начинается с приложения
        self.assertTrue(msg.startswith("{name}, VPN на Android: что сделать\nЧерез VPN — только приложения из списка, "
                                       "остальное напрямую.\n1) Установите браузер «Brave»"), msg)
        self.assertIn("\n2) Установите «Happ»", msg)
        self.assertIn("\n3) Установите «AmneziaWG»", msg)
        # сообщение открыто на этом же телефоне: ссылка из буфера, файл; QR — только «на другом экране»
        self.assertIn("\n4) Скопируйте ссылки «Hysteria2» и «VLESS Vision» из сообщения. В «Happ» нажмите «+» → "
                      "«Вставить из буфера».", msg)
        self.assertIn("\n5) Открываете сообщение на другом экране — в «Happ» нажмите «+» → «Сканировать QR» и наведите "
                      "камеру на QR «Hysteria2» и «VLESS Vision».", msg)
        self.assertIn("В «Happ»: экран «Inbounds» → режим авторизации «auto»", msg, "служебный вход Happ — под паролем")
        self.assertIn("В «Happ» включите «РФ напрямую»", msg)
        self.assertRegex(msg, r"\n\d\) Сохраните файл «AmneziaWG» из сообщения\. В «AmneziaWG» нажмите «\+» → «Импорт из файла»")
        self.assertRegex(msg, r"\n\d+\) Открываете сообщение на другом экране — в «AmneziaWG» нажмите «\+» → «Сканировать QR»")
        self.assertNotIn("этом же телефоне", msg)
        self.assertLess(msg.index("Установите «AmneziaWG»"), msg.index("В «Happ» нажмите"))
        self.assertLess(msg.index("В «Happ» включите"), msg.index("В «AmneziaWG» нажмите"), "шаги приложения — подряд")
        self.assertEqual(msg.count("Проверьте:"), 1, "проверка одна, одной фразой")
        # два приложения: какое держать включённым (Android держит один VPN)
        self.assertIn("Включённым держите одно приложение — «Happ». Не подключается — выключите его и включите «AmneziaWG»",
                      msg)
        n = len(msg.splitlines()) - 2
        self.assertEqual(msg.splitlines()[-3:], [f"{n - 2}) {catalog().raw['rules']['android']}",
                                                 f"{n - 1}) {CHECK['brave']}", f"{n}) {catalog().raw['report']}"])
        for gone in ("REALITY", "2ip", "vless://"):
            self.assertNotIn(gone, msg)
        # с одним приложением ключи не называются: протоколов в тексте нет
        one = self.gpack("android", [VLESS], {"android": ["happ", "amneziawg"]}, ["vless-reality", "amneziawg"])
        self.assertEqual(len(one.sections), 1, "нет ссылки AWG — второе приложение не нужно")
        self.assertNotIn("Happ (", one.message)
        self.assertNotIn("Happ:", one.message)
        self.assertNotIn("VLESS", one.message)
        self.assertNotIn("держите одно", one.message)
        self.assertIn("Скопируйте ссылку из сообщения.", one.message)
        # бумажная карточка: импорт QR-кодом, без «сообщения»
        paper = "\n".join(p.steps(paper=True))
        self.assertIn("В «Happ» нажмите «+» → «Сканировать QR» и наведите камеру на QR «Hysteria2» и «VLESS Vision».", paper)
        self.assertNotIn("сообщени", paper.replace(catalog().raw["report"], ""))
        self.assertEqual(p.paper_rest, [])

    def test_protocol_covered_by_earlier_client_is_not_repeated(self):
        hy2 = link("hysteria2", "hysteria2://x@1.2.3.4:443#x")
        both = [VLESS, hy2]
        # Happ покрыл всё — v2rayNG после него пуст и в пакет не попадает
        p = self.gpack("android", both, {"android": ["happ", "v2rayng"]}, ["vless-reality", "hysteria2"])
        self.assertEqual([s.client["id"] for s in p.sections], ["happ"])
        # INCY умеет только VLESS, остальное достаётся второму
        p = self.gpack("android", both, {"android": ["incy", "v2rayng"]}, ["vless-reality", "hysteria2"])
        self.assertEqual([(s.client["id"], [i.proto for i in s.items]) for s in p.sections],
                         [("incy", ["vless-reality"]), ("v2rayng", ["hysteria2"])])

    def test_group_without_client_for_platform_gets_no_pack(self):
        self.assertIsNone(self.gpack("windows", [VLESS], {"android": ["happ"]}), "«Не нужна» для Windows")
        self.assertIsNone(self.gpack("android", [VLESS], {"android": []}))
        self.assertIsNone(self.gpack("android", [VLESS], {"android": ["happ"]}, ["amneziawg"]), "Happ не умеет AWG")
        self.assertIsNone(self.gpack("android", [AWG_ANDROID], {"android": ["happ"]}, ["vless-reality"]),
                          "нет ссылки протокола — нечего отправлять")

    def test_group_client_with_protocol_outside_handoff_order(self):
        # TUIC нет в порядке раздачи Windows, но Karing умеет только его: группа «все включённые» всё равно получает пакет
        tuic = link("tuic", "tuic://u:p@1.2.3.4:443?alpn=h3#x")
        s = self.gpack("windows", [tuic], {"windows": ["karing"]}, None).sections[0]
        self.assertEqual((s.client["id"], s.proto, s.method), ("karing", "tuic", "link"))
        # порядок раздачи по-прежнему главнее: VLESS у v2rayN первым, TUIC — после
        both = [tuic, VLESS]
        self.assertEqual([i.proto for i in self.gpack("windows", both, {"windows": ["v2rayn"]}, None).sections[0].items],
                         ["vless-reality"])
        self.assertEqual(self.gpack("windows", both, {"windows": ["karing"]}, None).sections[0].proto, "tuic")

    def test_stale_or_useless_group_client_gives_no_pack_not_a_catalog_fallback(self):
        # клиента убрали из каталога — человеку раздавать нечего: рендерится ровно набор группы
        self.assertIsNone(self.gpack("android", [VLESS], {"android": ["ghost"]}, None))
        # клиент есть, но не для этой платформы
        self.assertIsNone(self.gpack("windows", [VLESS], {"windows": ["happ"]}, None))
        # клиент группы ничего из включённого не умеет: Karing не умеет VLESS
        self.assertIsNone(self.gpack("windows", [VLESS], {"windows": ["karing"]}, None))
        # «не нужна» — тоже пакета нет
        self.assertIsNone(self.gpack("windows", [VLESS], {"android": ["happ"]}, None))
        # человек без группы — рекомендованные каталогом
        self.assertEqual(self.gpack("windows", [VLESS], None, None).sections[0].client["id"], "v2rayn")
    def test_per_app_step_only_where_client_can(self):
        # iPhone: приложений через VPN нет, браузер любой
        s = self.gpack("ios", [VLESS], {"ios": ["incy"]}, ["vless-reality"]).sections[0]
        self.assertNotIn("Приложения через VPN", " ".join(s.steps))
        self.assertEqual(s.check, CHECK["device"])
        # Windows у AmneziaVPN — только исключение приложений: шага «только из списка» нет
        w = self.gpack("windows", [AWG_COMMON, AWG_KEY], {"windows": ["amneziavpn"]}, ["amneziawg"]).sections[0]
        self.assertEqual(w.client["id"], "amneziavpn")
        self.assertNotIn("только приложения из списка", " ".join(w.steps))
        a = self.gpack("android", [AWG_KEY], {"android": ["amneziavpn"]}, ["amneziawg"]).sections[0]
        self.assertIn("только приложения из списка", " ".join(a.steps))

    def test_admin_pack_has_no_install_steps_and_no_stores(self):
        p = clientviews.build_pack(catalog(), {"checked": None, "versions": {}}, "android", [VLESS, AWG_ANDROID], [],
                                   {"android": ["happ", "amneziawg"]}, ["vless-reality", "amneziawg"], False, None, None, True)
        msg = p.message
        self.assertTrue(msg.startswith("{name}, VPN уже установлен. Включите его в «Happ».\nЧерез VPN — "
                                       "только приложения из списка, остальное напрямую.\n1) Скопируйте ссылку"), msg)
        for gone in ("Установите", "releases", "Assets", "play.google.com"):
            self.assertNotIn(gone, msg)
        self.assertEqual(msg.count("\n"), 12, "строка «через VPN», импорт и QR и три настройки Happ, импорт и QR AWG, "
                                              "какое держать включённым, правило Android, проверка и что прислать")
        self.assertTrue(msg.endswith(f"10) {CHECK['brave']}\n11) {catalog().raw['report']}"))
        one = clientviews.build_pack(catalog(), {"checked": None, "versions": {}}, "ios", [VLESS], [],
                                     {"ios": ["incy"]}, ["vless-reality"], False, None, None, True)
        self.assertTrue(one.message.startswith("{name}, VPN уже установлен. Включите его в «INCY»."))

    def test_foreign_apple_id_warning(self):
        p = self.gpack("ios", [VLESS], {"ios": ["happ"]}, ["vless-reality"])
        self.assertEqual(p.sections[0].client["id"], "happ")
        self.assertIn("Apple ID другой страны", p.message)
        self.assertNotIn("Приложения через VPN", p.message, "на iPhone их нет")
        self.assertNotIn("Apple ID", self.gpack("ios", [VLESS], {"ios": ["incy"]}, ["vless-reality"]).message)
        self.assertNotIn("Apple ID", self.gpack("android", [VLESS], {"android": ["happ"]}, ["vless-reality"]).message)

    def test_android_without_awg_uses_happ(self):
        p = self.pack("android", [VLESS])
        s = p.sections[0]
        self.assertEqual((len(p.sections), s.client["id"], s.method), (1, "happ", "link"))
        self.assertIn("Приложения через VPN в «Happ»:", " ".join(s.steps))
        self.assertEqual(s.check, CHECK["brave"])
        self.assertNotIn("не откроется", s.check)

    def test_windows_link_and_rules_file(self):
        s = self.pack("windows", [VLESS, RULES]).sections[0]
        self.assertEqual((s.client["id"], s.method), ("v2rayn", "link"))
        self.assertEqual([i.proto for i in s.extras], ["allowlist"])
        self.assertIn("v2rayn-routing.json", " ".join(s.steps))
        steps = " ".join(s.steps)
        for need in ("«Импорт правил из файла»", "«Устаревшая защита TUN»", "«Перезапустить от имени администратора»",
                     "«Включить TUN»", "«Очистить системный прокси»"):
            self.assertIn(need, steps, "без TUN и очистки прокси правила по программам не работают")
        self.assertEqual(s.via, "apps")
        p = self.pack("windows", [VLESS, RULES])
        self.assertIsNone(s.paper, "ссылку и файл правил с бумаги не перенести")
        self.assertEqual((p.steps(paper=True), p.paper_rest), ([], ["v2rayN"]))
        self.assertNotIn("Открываете сообщение на другом экране", steps, "компьютер QR не сканирует")
        bare = self.pack("windows", [VLESS]).sections[0]
        self.assertEqual(bare.extras, [], "без файла правил — шага нет")
        self.assertEqual(bare.via, "all", "без файла правил через VPN идёт всё")
        self.assertIn("«.zip» с «windows-64»", s.install, "v2rayN — архив, как в USER-GUIDE")

    def test_ios_and_no_match(self):
        p = self.pack("ios", [VLESS])
        self.assertEqual(p.sections[0].client["id"], "incy")
        self.assertEqual(p.sections[0].check, CHECK["device"])
        self.assertIsNone(self.pack("ios", [link("tuic", "tuic://x")]), "для TUIC на iPhone клиента не выбрано")
        self.assertIsNone(self.pack("macos", [VLESS]), "на macOS для VLESS проверенного клиента нет")
        self.assertIsNone(self.pack("android", []))

    def test_amneziavpn_takes_vpn_key_not_conf_qr(self):
        s = self.pack("macos", [AWG_COMMON, AWG_KEY]).sections[0]
        self.assertEqual((s.client["id"], s.method), ("amneziavpn", "link"))
        self.assertIsNone(s.paper, "длинный ключ vpn:// с бумаги не перенести")
        # без vpn:// остаётся файл
        self.assertEqual(self.pack("macos", [AWG_COMMON]).sections[0].method, "file")

    def test_conf_never_offered_as_link_and_vpn_key_not_to_wg_clients(self):
        p = self.pack("android", [AWG_KEY])
        self.assertIsNone(p, "vpn:// клиентам AmneziaWG/WG Tunnel не подходит")

    def test_version_only_for_github_clients(self):
        cache = {"checked": 1.0, "versions": {"amneziawg": {"version": "2.1"}, "incy": {"version": "7"}}}
        self.assertIn("(версия 2.1)", self.pack("android", [AWG_ANDROID], cache).sections[0].install)
        self.assertNotIn("версия", self.pack("ios", [VLESS], cache).sections[0].install)

    def test_message_has_no_secrets(self):
        p = self.pack("android", [VLESS, AWG_ANDROID])
        self.assertNotIn("vless://", p.message)
        self.assertNotIn(".conf", p.message)
        self.assertNotIn("плитк", p.message, "ссылок на плитки в тексте нет")
        self.assertTrue(p.message.startswith("{name}, VPN на Android: что сделать\n"
                                             "Через VPN — только приложения из списка, остальное напрямую.\n"
                                             "1) Установите браузер «Brave»"), p.message)
        self.assertIn("\n2) Установите «AmneziaWG»", p.message)
        self.assertIn("\n4) Сохраните файл «AmneziaWG» из сообщения. В «AmneziaWG» нажмите «+»", p.message,
                      "два приложения — установка, затем шаги с названием")
        one = self.pack("android", [VLESS]).message
        self.assertIn("\n2) Установите «Happ»", one)
        self.assertNotIn("Happ (", one, "названий протоколов перед шагами нет")
        self.assertIn("\n3) Скопируйте ссылку из сообщения. В «Happ» нажмите «+» → «Вставить из буфера».", one)
        self.assertIn("\n4) Открываете сообщение на другом экране — в «Happ» нажмите «+» → «Сканировать QR»", one)
        self.assertTrue(one.endswith(catalog().raw["report"]), "в конце — что прислать администратору")


class PickLinkTest(unittest.TestCase):
    def test_android_gets_android_conf_others_common_one(self):
        awg, links = catalog().client("amneziawg"), [AWG_COMMON, AWG_ANDROID, AWG_KEY]
        self.assertEqual(clientviews.pick_link("amneziawg", awg, "android", links, "qr"), 1)
        self.assertEqual(clientviews.pick_link("amneziawg", awg, "android", links, "file"), 1)
        tun = catalog().client("wgtunnel")
        self.assertEqual(clientviews.pick_link("amneziawg", tun, "ios", links, "file"), 0)
        vpn = catalog().client("amneziavpn")
        self.assertEqual(clientviews.pick_link("amneziawg", vpn, "windows", links, "link"), 2, "vpn:// — только AmneziaVPN")
        self.assertIsNone(clientviews.pick_link("amneziawg", awg, "android", links, "link"))

    def test_first_uri_wins(self):
        a, b = link("hysteria2", "hysteria2://a@h:443#a"), link("hysteria2", "hysteria2://b@h:443?obfs=salamander#b")
        happ = catalog().client("happ")
        self.assertEqual(clientviews.pick_link("hysteria2", happ, "android", [VLESS, a, b], "link"), 1)
        self.assertEqual(clientviews.pick_link("hysteria2", happ, "android", [VLESS, a, b], "qr"), 1)


class SynthLinksTest(unittest.TestCase):
    def test_group_pack_has_no_user_keys(self):
        links = clientviews.synth_links(["vless-reality", "amneziawg"])
        self.assertEqual({ln.proto_id for ln in links}, {"vless-reality", "amneziawg", "allowlist"})
        self.assertTrue(all("@" not in ln.uri for ln in links), "ключей в заглушках нет")
        cat = catalog()
        p = clientviews.build_pack(cat, {"checked": None, "versions": {}}, "android", links, [],
                                   {"android": ["happ", "amneziawg"]}, ["vless-reality", "amneziawg"])
        self.assertEqual([s.client["id"] for s in p.sections], ["happ", "amneziawg"])


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

    def msg(self, body, plat="android", uid=""):
        return html.unescape(re.search(rf'<pre id="msg-{uid}{plat}"[^>]*>(.*?)</pre>', body, re.S).group(1))

    def test_connect_block_is_first_and_per_user(self):
        clients.check_upstream(fetch=fake_fetch)
        resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200, body[-300:])
        main = body[body.index("<main"):]
        self.assertLess(main.index(">Подключить<"), main.index("Все ссылки и QR"))
        self.assertLess(main.index("Все ссылки и QR"), main.index(">Профиль<"))
        # платформа — список; без JS он скрыт, а панели всех платформ видны
        self.assertRegex(body, r'<div class="conn-pick" hidden><label for="pl-">Платформа</label><select id="pl-" data-plat>')
        self.assertRegex(body, r'<option value="android" selected>Android</option><option value="ios">iPhone</option>')
        for plat in ("android", "ios", "windows", "macos", "linux"):
            self.assertIn(f'data-pp="{plat}"', body)
            self.assertIn(f'id="msg-{plat}"', body)
        android = body[body.index('data-pp="android"'):body.index('data-pp="ios"')]
        # приложение с версией и ссылкой, QR и ссылка этого человека
        self.assertIn("<strong>Happ</strong>", android)
        self.assertIn('href="https://github.com/Happ-proxy/happ-android/releases"', android)
        self.assertRegex(android, r'<img class="qr" src="/users/masha/qr/\d+\?p=[0-9a-f]{8}" loading="lazy"')
        self.assertRegex(android, r'<input type="text" id="k-android-happ-0" value="vless://masha@')
        self.assertIn('data-copy="k-android-happ-0">Копировать</button>', android)
        # текст — один на группу, с именем человека; ключей в нём нет
        msg = self.msg(body)
        self.assertTrue(msg.startswith("masha, VPN на Android: что сделать\nЧерез VPN — только "), msg)
        self.assertIn("\n1) Установите браузер «Brave»", msg)
        self.assertNotIn("{name}", body)
        self.assertNotIn("vless://", msg)
        self.assertIn(">Скопировать</button>", body)
        self.assertIn('data-copy="msg-android"', body)
        self.assertIn("Приложения — по протоколам.", body, "человек без группы: рекомендованные каталога")
        self.assertNotIn("Изменить для группы", body)
        self.assertNotIn("<textarea", main.split(">Подключить<")[1].split("Все ссылки и QR")[0], "инструкция — только для чтения")
        self.assertNotIn("можно править", body)
        self.assertRegex(body, r'<label class="chk" data-links hidden><input type="checkbox" data-addlinks="msg-android">')
        self.assertNotIn("Скопировать сообщение", body)
        self.assertNotIn("плитках выше", body)
        self.assertNotIn("Другие платформы", body)
        self.assertIn("не отправляйте через MAX и VK", body)
        self.assertNotIn("style=", body)
        self.assertNotRegex(body, r"\son\w+=")

    def test_group_member_sees_the_group_apps_and_text_read_only(self):
        groups.ensure()
        cat = clients.load()
        main = groups.Groups.load().get("main")
        self.assertTrue(main.clients, "группе без набора приложений подобран набор при первом обращении")
        _, body = self.c.get("/users/masha")
        self.assertIn("Приложения и инструкция — общие для группы.", body)
        self.assertIn('<a href="/groups/main#text" class="small" data-swap>Изменить для группы →</a>', body)
        got = {plat for plat in cat.platforms if f'data-pp="{plat}"' in body}
        self.assertEqual(got, set(main.clients), "ровно платформы набора группы")
        groups.update("main", clients={})
        _, body = self.c.get("/users/masha")
        self.assertIn("Приложения не выбраны", body)
        self.assertIn('href="/groups/main#settings"', body)
        self.assertNotIn('data-pp="', body, "пустой набор — ничего из каталога не раздаётся")

    def test_windows_panel_offers_link_not_qr(self):
        _, body = self.c.get("/users/masha")
        win = body[body.index('data-pp="windows"'):body.index('data-pp="macos"')]
        self.assertIn("<strong>v2rayN</strong>", win)
        self.assertIn('id="k-windows-v2rayn-0" value="vless://masha@', win)
        self.assertNotIn('<img class="qr"', win.split('class="msg"')[0], "v2rayN QR не принимает")

    def test_group_text_with_name_is_what_user_gets(self):
        groups.set_message("main", "android", "Привет, {name}!\nСтавь Happ, дальше по QR.")
        _, body = self.c.get("/users/masha")
        self.assertEqual(self.msg(body), "Привет, masha!\nСтавь Happ, дальше по QR.")
        self.assertTrue(self.msg(body, "windows").startswith("masha, VPN на Windows"), "остальные — по умолчанию")
        self.assertIn('href="/groups/main#text"', body)

    def test_tiles_stay_as_advanced_block(self):
        _, body = self.c.get("/users/masha")
        self.assertRegex(body, r'<details class="card more"><summary>Все ссылки и QR</summary>')
        self.assertNotIn('title="Приложения: ', body, "какое приложение открывает протокол, сказано в «Подключить», а не подсказкой")
        self.assertIn("Быстрый старт", body)

    def test_disabled_catalog_keeps_tiles_open(self):
        with mock.patch.object(clients, "load", side_effect=clients.ClientsError("x")):
            resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(">Подключить<", body)
        self.assertRegex(body, r'<details class="card more" open><summary>Все ссылки и QR</summary>')

    def test_page_stays_light(self):
        _, body = self.c.get("/users/masha")
        self.assertLessEqual(len(body.encode("utf-8")), 32 * 1024, "30 КБ + блок «Если у него не работает» (D58)")


if __name__ == "__main__":
    unittest.main()
