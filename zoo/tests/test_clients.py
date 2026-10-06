"""Каталог клиентов, версии из GitHub, страница /clients и блок «Подключить»."""

import copy
import json
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

    def test_hiddify_unverified_and_not_recommended(self):
        # исследование 04.10.2026: у Hiddify Hy2/SS/TUIC — ❓; без проверки не рекомендуем и в выбор не берём
        cat = catalog()
        h = cat.client("hiddify")
        for pid in ("ss2022", "hysteria2", "tuic"):
            self.assertEqual(h["protocols"][pid]["s"], "unk", pid)
            self.assertIn("не проверено", h["protocols"][pid]["note"], pid)
        for plat, by_proto in cat.raw["recommended"].items():
            self.assertNotIn("hiddify", by_proto.values(), plat)
        for plat in ("windows", "macos", "linux"):
            self.assertNotIn("tuic", cat.raw["recommended"][plat], "проверенного клиента TUIC на десктопе нет")

    def test_per_app_steps_are_per_platform(self):
        cat = catalog()
        for c in cat.clients:
            self.assertIsNone(cat.per_app_steps(c, "ios"), f"{c['id']}: на iOS приложения через VPN невозможны")
        av = cat.client("amneziavpn")
        self.assertIsNone(cat.per_app_steps(av, "windows"), "на Windows у AmneziaVPN только исключение приложений")
        self.assertIn("только приложения из списка", cat.per_app_steps(av, "android"))
        self.assertIn("Brave", cat.per_app_steps(cat.client("happ"), "android"))

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
        self.assertIn('class="badge muted" title="не проверено: не проверено (04.10.2026)', body)

    def card(self, body, title):
        card = body[body.index(f"<h3>{title}</h3>"):]
        return card[:card.index("</section>")]

    def test_platform_cards_list_every_supported_protocol_and_mark_recommended(self):
        _, body = self.c.get("/clients")
        android = self.card(body, "Android")
        cat = catalog()
        for cid in ("happ", "v2rayng", "incy", "singbox"):
            row = android[android.index(f"<strong>{cat.client(cid)['name']}</strong>"):]
            row = row[:row.index("</tr>")]
            for pid, st in cat.client(cid)["protocols"].items():
                if pid not in cat.real_protocols():
                    continue
                chip = f">{cat.protocols[pid]['title']}</span>"
                if st["s"] in ("ok", "warn"):
                    self.assertIn(chip, row, f"{cid}: {pid}")
                else:
                    self.assertNotIn(chip, row, f"{cid}: {pid}")
        happ = android[android.index("<strong>Happ</strong>"):]
        happ = happ[:happ.index("</tr>")]
        incy = android[android.index("<strong>INCY</strong>"):]
        incy = incy[:incy.index("</tr>")]
        self.assertIn("рекомендуем</span>", happ)
        self.assertRegex(happ, r'class="chip ok" title="рекомендуем для этого протокола">VLESS')
        self.assertNotIn("рекомендуем", incy, "INCY на Android каталог не рекомендует")
        self.assertLess(android.index("<strong>Happ</strong>"), android.index("<strong>INCY</strong>"),
                        "рекомендованные — первыми")
        self.assertIn("<strong>Karing</strong>", android, "не рекомендованный, но умеющий протокол — в списке")
        self.assertNotIn("<strong>Hiddify</strong>", android, "Hiddify на Android ничего не умеет: ✕ и ? в список не идут")

    def test_check_now_button_and_request(self):
        clients.check_upstream(fetch=fake_fetch, now=lambda: time.time() - 7500)
        _, body = self.c.get("/clients")
        self.assertIn("версии проверены 2 ч", body)
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
        self.assertIn("версии проверены только что", body)
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


class PackTest(unittest.TestCase):
    def pack(self, plat, links, cache=None):
        return clientviews.build_pack(catalog(), cache or {"checked": None, "versions": {}}, plat, links, [])

    def test_android_set_awg_qr_with_android_tab_then_happ(self):
        p = self.pack("android", [VLESS, AWG_ANDROID, AWG_COMMON, AWG_KEY])
        # ни один клиент не умеет всё: AmneziaWG (первым в раздаче) + Happ для VLESS
        self.assertEqual([s.client["id"] for s in p.sections], ["amneziawg", "happ"])
        s = p.sections[0]
        self.assertEqual((s.proto, s.method), ("amneziawg", "qr"))
        self.assertTrue(s.install.startswith("Установите «AmneziaWG»"))
        self.assertIn("github.com/amnezia-vpn/amneziawg-android", s.install, "проверенные ссылки — раньше")
        self.assertIn("Brave", s.check)
        self.assertIn("2ip.ru", s.check, "у AWG echo-правила нет: адрес сервера виден")

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
        self.assertEqual((s[0].client["id"], s[0].proto, s[0].method), ("wgtunnel", "amneziawg", "qr"))
        # группа «все включённые»: порядок раздачи каталога, но клиент — группы
        s = self.gpack("android", links, {"android": ["v2rayng"]}, None).sections
        self.assertEqual([i.proto for i in s[0].items], ["vless-reality", "hysteria2"])
        # без клиентов группы — рекомендованные каталога: AWG и остальное
        self.assertEqual([x.client["id"] for x in self.gpack("android", links, {}, ["hysteria2"]).sections],
                         ["amneziawg", "happ"])

    def test_group_set_gives_one_section_per_client_with_own_protocols(self):
        hy2 = link("hysteria2", "hysteria2://x@1.2.3.4:443#x")
        p = self.gpack("android", [VLESS, hy2, AWG_ANDROID], {"android": ["happ", "amneziawg"]},
                       ["hysteria2", "vless-reality", "amneziawg"])
        self.assertEqual([(s.client["id"], [i.proto for i in s.items]) for s in p.sections],
                         [("happ", ["hysteria2", "vless-reality"]), ("amneziawg", ["amneziawg"])])
        self.assertEqual(p.sections[0].tiles, "Hysteria2, VLESS + REALITY")
        msg = p.message
        # одним списком: сначала установка всех приложений, потом импорт каждого
        self.assertTrue(msg.startswith("{name}, VPN на Android: что сделать\n1) Установите «Happ»"))
        self.assertIn("\n2) Установите «AmneziaWG»", msg)
        self.assertIn("\n3) Happ (Hysteria2, VLESS + REALITY): ", msg)
        self.assertIn("AmneziaWG (AmneziaWG): ", msg)
        self.assertLess(msg.index("Установите «AmneziaWG»"), msg.index("Happ (Hysteria2"))
        self.assertEqual(msg.count("Включите VPN"), 1, "проверка одна, по основному протоколу")
        self.assertNotIn("vless://", msg)
        # с одним приложением названий перед шагами нет
        one = self.gpack("android", [VLESS], {"android": ["happ", "amneziawg"]}, ["vless-reality", "amneziawg"])
        self.assertEqual(len(one.sections), 1, "нет ссылки AWG — второе приложение не нужно")
        self.assertNotIn("Happ (", one.message)
        self.assertNotIn("Happ:", one.message)

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

    def test_stale_or_useless_group_client_falls_back_to_recommended(self):
        # клиента убрали из каталога
        s = self.gpack("android", [VLESS], {"android": ["ghost"]}, None).sections[0]
        self.assertEqual((s.client["id"], s.proto), ("happ", "vless-reality"))
        # клиент есть, но не для этой платформы
        self.assertEqual(self.gpack("windows", [VLESS], {"windows": ["happ"]}, None).sections[0].client["id"], "v2rayn")
        # клиент группы ничего из включённого не умеет: Karing не умеет VLESS
        self.assertEqual(self.gpack("windows", [VLESS], {"windows": ["karing"]}, None).sections[0].client["id"], "v2rayn")
        # а «не нужна» по-прежнему означает «пакета нет»
        self.assertIsNone(self.gpack("windows", [VLESS], {"android": ["happ"]}, None))

    def test_per_app_step_only_where_client_can(self):
        # iPhone: приложений через VPN нет, браузер любой
        s = self.gpack("ios", [VLESS], {"ios": ["incy"]}, ["vless-reality"]).sections[0]
        self.assertNotIn("Приложения через VPN", " ".join(s.steps))
        self.assertIn("любом браузере", s.check)
        # Windows у AmneziaVPN — только исключение приложений: шага «только из списка» нет
        w = self.gpack("windows", [AWG_COMMON, AWG_KEY], {"windows": ["amneziavpn"]}, ["amneziawg"]).sections[0]
        self.assertEqual(w.client["id"], "amneziavpn")
        self.assertNotIn("только приложения из списка", " ".join(w.steps))
        self.assertIn("любом браузере", w.check)
        a = self.gpack("android", [AWG_KEY], {"android": ["amneziavpn"]}, ["amneziawg"]).sections[0]
        self.assertIn("только приложения из списка", " ".join(a.steps))
        self.assertIn("Brave", a.check)

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
        self.assertEqual((len(p.sections), s.client["id"], s.method), (1, "happ", "qr"))
        self.assertIn("Приложения через VPN:", " ".join(s.steps))
        self.assertIn("не откроется", s.check, "echo-сервисы на сервере блокируются для Xray-протоколов")

    def test_windows_link_and_rules_file(self):
        s = self.pack("windows", [VLESS, RULES]).sections[0]
        self.assertEqual((s.client["id"], s.method), ("v2rayn", "link"))
        self.assertEqual([i.proto for i in s.extras], ["allowlist"])
        self.assertIn("v2rayn-routing.json", " ".join(s.steps))
        self.assertEqual(self.pack("windows", [VLESS]).sections[0].extras, [], "без файла правил — шага нет")

    def test_ios_and_no_match(self):
        p = self.pack("ios", [VLESS])
        self.assertEqual(p.sections[0].client["id"], "incy")
        self.assertIn("любом браузере", p.sections[0].check)
        self.assertIsNone(self.pack("ios", [link("tuic", "tuic://x")]), "для TUIC на iPhone клиента не выбрано")
        self.assertIsNone(self.pack("macos", [VLESS]), "на macOS для VLESS проверенного клиента нет")
        self.assertIsNone(self.pack("android", []))

    def test_amneziavpn_takes_vpn_key_not_conf_qr(self):
        s = self.pack("macos", [AWG_COMMON, AWG_KEY]).sections[0]
        self.assertEqual((s.client["id"], s.method), ("amneziavpn", "link"))
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
        self.assertTrue(p.message.startswith("{name}, VPN на Android: что сделать\n1) Установите «AmneziaWG»"))
        self.assertIn("\n3) AmneziaWG (AmneziaWG): ", p.message, "два приложения — установка, затем шаги с названием")
        one = self.pack("android", [VLESS]).message
        self.assertTrue(one.startswith("{name}, VPN на Android: что сделать\n1) Установите «Happ»"))
        self.assertNotIn("Happ (", one, "одно приложение — названий перед шагами нет")


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
        return re.search(rf'<textarea id="msg-{uid}{plat}"[^>]*>(.*?)</textarea>', body, re.S).group(1)

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
        self.assertRegex(android, r'<img class="qr" src="/users/masha/qr/\d+" loading="lazy"')
        self.assertRegex(android, r'<input type="text" id="k-android-happ-0" value="vless://masha@')
        self.assertIn('data-copy="k-android-happ-0">Копировать</button>', android)
        # текст — один на группу, с именем человека; ключей в нём нет
        msg = self.msg(body)
        self.assertTrue(msg.startswith("masha, VPN на Android: что сделать\n1) Установите «"), msg)
        self.assertNotIn("{name}", body)
        self.assertNotIn("vless://", msg)
        self.assertIn("Скопировать текст", body)
        self.assertIn('data-copy="msg-android"', body)
        self.assertRegex(body, r'<label class="chk" data-links hidden><input type="checkbox" data-addlinks="msg-android">')
        self.assertNotIn("Скопировать сообщение", body)
        self.assertNotIn("плитках выше", body)
        self.assertNotIn("Другие платформы", body)
        self.assertIn("не отправляйте через MAX и VK", body)
        self.assertNotIn("style=", body)
        self.assertNotRegex(body, r"\son\w+=")

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
        self.assertIn('href="/groups/main#texts"', body)

    def test_tiles_stay_as_advanced_block(self):
        _, body = self.c.get("/users/masha")
        self.assertRegex(body, r'<details class="card more"><summary>Все ссылки и QR</summary>')
        self.assertIn("Клиенты: ", body, "плитки по-прежнему со списком клиентов из каталога")
        self.assertIn("Быстрый старт", body)

    def test_disabled_catalog_keeps_tiles_open(self):
        with mock.patch.object(clients, "load", side_effect=clients.ClientsError("x")):
            resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(">Подключить<", body)
        self.assertRegex(body, r'<details class="card more" open><summary>Все ссылки и QR</summary>')

    def test_page_stays_light(self):
        _, body = self.c.get("/users/masha")
        self.assertLessEqual(len(body.encode("utf-8")), 30 * 1024)


if __name__ == "__main__":
    unittest.main()
