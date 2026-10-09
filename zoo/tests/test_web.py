import argparse
import contextlib
import copy
import gzip
import http.client
import io
import ipaddress
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.helpers import ZooEnv, needs_bash
from zoolib import config, logread, traffic
from zoolib import web as web_mod
from zoolib.web import assets, auth, charts, logs
from zoolib.web import stamp as stamp_mod
from zoolib.web.app import App, Request, _safe_next
from zoolib.web.html import Markup, card, post_button, t, table
from zoolib.web.jobs import Jobs
from zoolib.web.server import make_server
from zoolib.web.userviews import clean_qr_svg

TOKEN = "test-token-0123456789abcdef"
HOST = "127.0.0.1:8999"
SID_COOKIE, LOGIN_COOKIE = auth.cookie_names(HOST)

FAKE_STATUS = {
    "server": {"ip": "10.0.0.1", "label": "test", "hostname": "box"},
    "zoo": {"version": "x", "home": "/opt/vpn-zoo"},
    "protocols": [
        {"id": "vless-reality", "name": "VLESS + REALITY", "port": 443, "layer": "tcp", "engine": "xray",
         "services": {"x-ui": "active"}, "enabled": True, "listening": {"tcp": True}, "ok": True, "users": 2},
        {"id": "hysteria2", "name": "Hysteria2 <b>", "port": 443, "layer": "udp", "engine": "hysteria",
         "services": {"hysteria-server.service": "failed"}, "enabled": True, "listening": {"udp": False},
         "ok": False, "users": 1},
    ],
    "services": {"x-ui.service": {"load": "loaded", "active": "active", "enabled": "enabled", "restarts": "0"},
                 "zoo-web.service": {"load": "loaded", "active": "active", "enabled": "enabled"},
                 "hysteria-server.service": {"load": "loaded", "active": "failed", "enabled": "enabled"}},
    "firewall": {"ufw_active": True},
    "xray": {"state": "running", "version": "26.9.30"},
    "exposed": [{"port": 2096, "proto": "tcp", "process": "x-ui", "public": True}],
    "certs": [{"path": "/etc/hysteria/cert.pem", "not_after": "2036-01-01T00:00:00+00:00", "days_left": 3000}],
    "versions": {"installed": {}, "pinned": {}},
    "host": {"cpu_percent": 3.5, "cpu_count": 2, "load": (0.1, 0.2, 0.3),
             "mem": {"total": 2 << 30, "used": 1 << 30, "available": 1 << 30, "swap_total": 0, "swap_free": 0},
             "disk": {"total": 20 << 30, "used": 5 << 30, "free": 15 << 30}, "net": {}, "uptime": 90000},
    "users": {"total": 2, "enabled": 2, "registry": True},
    "problems": ["hysteria2: сервис hysteria-server.service — failed"],
}

SAMPLE_QR = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Created with qrencode 4.1.1 (https://fukuchi.org/works/qrencode/index.html) -->
<svg width="3.39cm" height="3.39cm" viewBox="0 0 32 32" preserveAspectRatio="none" version="1.1" xmlns="http://www.w3.org/2000/svg">
\t<g id="QRcode">
\t\t<rect x="0" y="0" width="32" height="32" fill="#ffffff"/>
\t\t<path style="stroke:#000000" transform="translate(2,2.5)" d="M0,0h7M8,0h1"/>
\t</g>
</svg>
"""


class Client:
    """Браузер для App без сокетов: хранит cookie, достаёт CSRF из страниц."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}
        self.csrf = ""

    def req(self, method, path, form=None, headers=None, multi=None):
        url = urllib.parse.urlsplit(path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        h = {"host": HOST}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        form = dict(form or {})
        r = Request(method, url.path, query, form, multi or {k: [v] for k, v in form.items()}, h, dict(self.cookies))
        resp = self.app.handle(r)
        for k, v in resp.headers:
            if k == "Set-Cookie":
                name, _, rest = v.partition("=")
                value = rest.split(";", 1)[0]
                if "Max-Age=0" in v:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value
        body = resp.body.decode("utf-8", "replace")
        m = re.search(r'name="csrf" value="([^"]+)"', body)
        if m:
            self.csrf = m.group(1)
        return resp, body

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, form=None, csrf=True, **kw):
        form = dict(form or {})
        if csrf:
            form.setdefault("csrf", self.csrf)
        return self.req("POST", path, form, **kw)

    def login(self):
        self.get("/login")
        resp, _ = self.post("/login", {"token": TOKEN, "lc": self.cookies.get(LOGIN_COOKIE, "")}, csrf=False)
        assert resp.status == 303, resp.status
        self.get("/")
        return resp


def header(resp, name):
    return [v for k, v in resp.headers if k.lower() == name.lower()]


class HtmlTest(unittest.TestCase):
    def test_escaping(self):
        out = t("a", "<script>x</script>", href='/x?a=1&b="2"', class_="btn", data_copy="u-1", hidden=True,
                title=None)
        self.assertEqual(out, '<a href="/x?a=1&amp;b=&quot;2&quot;" class="btn" data-copy="u-1" hidden>'
                              '&lt;script&gt;x&lt;/script&gt;</a>')
        self.assertEqual(t("p", Markup("<b>ok</b>"), ["a", None, 1]), "<p><b>ok</b>a1</p>")
        self.assertEqual(t("input", type="text"), '<input type="text">')

    def test_safe_next(self):
        self.assertEqual(_safe_next("/users/masha"), "/users/masha")
        for bad in ("//evil.com", "https://evil.com", "\\\\evil", None, ""):
            self.assertEqual(_safe_next(bad), "/")
        # браузер выкидывает \t и \n из URL: «/\t/evil.com» стал бы «//evil.com»
        for bad in ("/\t/evil.com", "/\n/evil.com", "/x\r\nSet-Cookie: a=1", "/ /evil.com", "/\x7f"):
            self.assertEqual(_safe_next(bad), "/", repr(bad))

    def test_markup_concat_stays_markup_and_escapes_text(self):
        joined = Markup("<b>a</b>") + Markup("<i>b</i>")
        self.assertIsInstance(joined, Markup)
        self.assertEqual(t("p", joined), "<p><b>a</b><i>b</i></p>")
        self.assertEqual(Markup("<b>a</b>") + "<x>", "<b>a</b>&lt;x&gt;")
        self.assertEqual("<x>" + Markup("<b>a</b>"), "&lt;x&gt;<b>a</b>")


class AuthTest(unittest.TestCase):
    def test_host_allowed(self):
        for ok in ("127.0.0.1:8080", "localhost:1", "[::1]:9", "127.0.0.1", "LOCALHOST:5"):
            self.assertTrue(auth.host_allowed(ok), ok)
        for bad in ("evil.com", "evil.com:8080", "127.0.0.1.evil.com", "", None, "10.0.0.1:80"):
            self.assertFalse(auth.host_allowed(bad), bad)
        self.assertTrue(auth.host_allowed("zoo.lan:80", {"zoo.lan"}))

    def test_login_and_lockout(self):
        now = [1000.0]
        a = auth.Auth(TOKEN, clock=lambda: now[0])
        self.assertIsNone(a.login("wrong"))
        s = a.login(TOKEN)
        self.assertIsNotNone(s)
        self.assertIs(a.session(s.sid), s)
        for _ in range(auth.FAIL_LIMIT):
            a.login("wrong")
        self.assertGreater(a.locked(), 0)
        self.assertIsNone(a.login(TOKEN), "во время блокировки даже верный токен не пускает")
        now[0] += auth.LOCKOUT + 1
        self.assertIsNotNone(a.login(TOKEN))

    def test_session_expiry_and_csrf(self):
        now = [0.0]
        a = auth.Auth(TOKEN, clock=lambda: now[0])
        s = a.login(TOKEN)
        self.assertTrue(a.csrf_ok(s, s.csrf))
        self.assertFalse(a.csrf_ok(s, ""))
        self.assertFalse(a.csrf_ok(s, s.csrf[:-1] + ("y" if s.csrf.endswith("x") else "x")))  # токен может кончаться на x
        now[0] += auth.IDLE_TTL + 1
        self.assertIsNone(a.session(s.sid))

    def test_short_token_rejected(self):
        with self.assertRaises(ValueError):
            auth.Auth("short")

    def test_cookie_flags(self):
        c = auth.cookie("zoo_sid", "abc")
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Strict", c)
        self.assertEqual(auth.parse_cookies("a=1; zoo_sid=abc; b"), {"a": "1", "zoo_sid": "abc"})


class PiecesTest(unittest.TestCase):
    def test_clean_qr_svg(self):
        svg = clean_qr_svg(SAMPLE_QR)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("style=", svg)
        self.assertIn('stroke="#000000"', svg)
        self.assertNotIn("cm\"", svg)
        self.assertNotIn("<?xml", svg)
        self.assertIsNone(clean_qr_svg('<svg><script>alert(1)</script></svg>'))
        self.assertIsNone(clean_qr_svg('<svg onload="x()"><rect/></svg>'))

    def test_sanitize(self):
        cfg = config.Config(values={"PANEL_PASS": "s3cretPass", "SERVER_IP": "1.2.3.4", "HY2_STATS_SECRET": "abcdef123"})
        text = ("link vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443?pbk=x#owner\n"
                "Authorization: Bearer abcdefghijkl\npassword=s3cretPass\nhy secret abcdef123 ok\n"
                "PrivateKey = AAAABBBBCCCC=\n\x1b[32m[+]\x1b[0m done 1.2.3.4")
        out = logs.sanitize(text, cfg)
        for secret in ("11111111-2222", "abcdefghijkl", "s3cretPass", "abcdef123", "AAAABBBB", "\x1b["):
            self.assertNotIn(secret, out)
        self.assertIn("vless://•••", out)
        self.assertIn("1.2.3.4", out)
        self.assertIn("[+] done", out)

    def test_charts(self):
        top, step = charts.nice_max(3 * 1024 ** 2 + 5)
        self.assertGreaterEqual(top, 3 * 1024 ** 2)
        self.assertLessEqual(top / step, 5)
        svg = charts.columns([0, 300, 600], 300, [{"title": "a<b", "values": [0, 10, 20]},
                                                   {"title": "b", "values": [5, 0, 1]}], "test")
        self.assertIn("<svg", svg)
        self.assertNotIn("style=", svg)
        self.assertIn("a&lt;b: 20 Б", svg)
        self.assertIn("<title>", svg)
        self.assertEqual(charts.sparkline([]), "")
        self.assertNotIn("style=", charts.bar(5, 10) + charts.meter(9, 10) + charts.sparkline([1, 0, 3]))

    def test_jobs(self):
        jobs = Jobs()
        j = jobs.start("x", "проба", [sys.executable, "-c", "print('привет')"])
        same = jobs.start("x", "ещё", [sys.executable, "-c", "pass"])
        jobs.wait(j)
        self.assertTrue(j.ok)
        self.assertEqual(j.stdout.strip(), "привет")
        self.assertTrue(same is j or same.id != j.id)
        bad = jobs.start("y", "нет такой", ["/nonexistent-zoo-binary"])
        jobs.wait(bad)
        self.assertEqual(bad.rc, 127)


class WebSetupTest(unittest.TestCase):
    @staticmethod
    def _sock(port, proto="tcp", addr="127.0.0.1"):
        return web_mod.system.Socket(proto=proto, addr=addr, port=port)

    def _setup(self, env, listening=(), active=False, **cfg):
        """web_mod.setup с подменой слушающих сокетов и systemctl; вернуть (config, вызовы systemctl)."""
        if cfg:
            c = config.load()
            env.write_config({**c.values, **cfg})
        calls = []

        def fake_run(argv, timeout=10.0):
            calls.append(argv)
            return 0, "", ""

        states = {web_mod.UNIT: {"active": "active" if active else "inactive"}}
        with mock.patch("zoolib.system.listening_sockets", return_value=list(listening)), \
                mock.patch("zoolib.system.unit_states", return_value=states), \
                mock.patch("zoolib.system.run", side_effect=fake_run):
            web_mod.setup(config.load())
        return config.load(), calls

    def test_default_port_is_7070(self):
        with ZooEnv() as env:
            cfg, _ = self._setup(env)
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "7070")
            self.assertEqual(cfg.get("ZOO_WEB_PORT_AUTO"), "1")
            self.assertGreaterEqual(len(cfg.get("ZOO_WEB_TOKEN")), 16)
            again, _ = self._setup(env)   # повтор ничего не меняет
            self.assertEqual(again.values, cfg.values)

    def test_busy_port_falls_back_with_warning(self):
        with ZooEnv() as env:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
                cfg, _ = self._setup(env, listening=[self._sock(7070), self._sock(7071, addr="0.0.0.0")])
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "7072")
            self.assertEqual(cfg.get("ZOO_WEB_PORT_AUTO"), "1")
            self.assertIn("порт 7070 занят", err.getvalue())
            self.assertIn("ssh -t -L 7072:127.0.0.1:7072 root@10.0.0.1 zoo web --link", err.getvalue())

    def test_udp_listener_and_configured_port_count(self):
        with ZooEnv() as env:
            cfg, _ = self._setup(env, listening=[self._sock(7070, proto="udp")])
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "7070")   # udp админке не мешает
        with ZooEnv() as env:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                cfg, _ = self._setup(env, SS_PORT="7070")
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "7071")

    def test_auto_port_migrates_to_7070(self):
        with ZooEnv() as env:
            out = io.StringIO()
            with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
                cfg, calls = self._setup(env, active=True, ZOO_WEB_PORT="23456", ZOO_WEB_PORT_AUTO="1",
                                         ZOO_WEB_TOKEN="t" * 20)
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "7070")
            self.assertIn("админка теперь на порту 7070 (был 23456)", out.getvalue())
            self.assertEqual(calls, [["systemctl", "restart", web_mod.UNIT]])

    def test_legacy_port_without_marker_migrates(self):
        with ZooEnv() as env:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                cfg, calls = self._setup(env, ZOO_WEB_PORT="31999", ZOO_WEB_TOKEN="t" * 20)
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "7070")
            self.assertEqual(cfg.get("ZOO_WEB_PORT_AUTO"), "1")
            self.assertEqual(calls, [])   # сервис не работает — перезапускать нечего

    def test_migration_waits_while_7070_is_busy(self):
        with ZooEnv() as env:
            cfg, calls = self._setup(env, listening=[self._sock(7070)], active=True,
                                     ZOO_WEB_PORT="23456", ZOO_WEB_PORT_AUTO="1", ZOO_WEB_TOKEN="t" * 20)
            self.assertEqual(cfg.get("ZOO_WEB_PORT"), "23456")
            self.assertEqual(calls, [])

    def test_explicit_port_is_untouched(self):
        cases = (
            {"ZOO_WEB_PORT": "9000"},                                  # вне прежнего диапазона, без метки
            {"ZOO_WEB_PORT": "23456", "ZOO_WEB_PORT_AUTO": "0"},       # закреплён меткой
        )
        for extra in cases:
            with self.subTest(extra=extra), ZooEnv() as env:
                cfg, calls = self._setup(env, active=True, ZOO_WEB_TOKEN="t" * 20, **extra)
                self.assertEqual(cfg.get("ZOO_WEB_PORT"), extra["ZOO_WEB_PORT"])
                self.assertEqual(cfg.get("ZOO_WEB_PORT_AUTO"), extra.get("ZOO_WEB_PORT_AUTO", ""))
                self.assertEqual(calls, [])

    def test_info_command(self):
        with ZooEnv():
            with mock.patch("zoolib.system.listening_sockets", return_value=[]):
                web_mod.setup(config.load())
            cfg = config.load()
            port = cfg.int("ZOO_WEB_PORT")
            self.assertEqual(port, 7070)
            with mock.patch.dict(os.environ, {"SUDO_USER": ""}):
                info = web_mod.access_info(config.Config(values={**cfg.values, "SSH_LOGIN_PORT": "2222"}))
            self.assertEqual(info["command"], "ssh -t -L 7070:127.0.0.1:7070 -p 2222 root@10.0.0.1 zoo web --link")
            with mock.patch.dict(os.environ, {"SUDO_USER": "admin"}):
                info = web_mod.access_info(cfg)
            self.assertEqual(info["command"], "ssh -t -L 7070:127.0.0.1:7070 admin@10.0.0.1 sudo zoo web --link")

    def test_setup_and_info(self):
        with ZooEnv():
            with mock.patch("zoolib.system.listening_sockets", return_value=[]):
                web_mod.setup(config.load())
            cfg = config.load()
            port = cfg.int("ZOO_WEB_PORT")
            self.assertNotIn(port, web_mod.BANNED_PORTS)
            self.assertGreaterEqual(len(cfg.get("ZOO_WEB_TOKEN")), 16)
            web_mod.setup(cfg)  # повтор ничего не меняет
            self.assertEqual(config.load().values, cfg.values)
            with mock.patch.dict(os.environ, {"SUDO_USER": ""}):
                info = web_mod.access_info(config.Config(values={**cfg.values, "SSH_PORTS": "2222,22"}))
            self.assertEqual(info["tunnel"], f"ssh -N -L {port}:127.0.0.1:{port} -p 2222 root@10.0.0.1")
            with mock.patch.dict(os.environ, {"SUDO_USER": ""}):
                info = web_mod.access_info(config.Config(
                    values={**cfg.values, "SSH_PORTS": "22,30366", "SSH_LOGIN_PORT": "30366"}))
            self.assertEqual(info["tunnel"], f"ssh -N -L {port}:127.0.0.1:{port} -p 30366 root@10.0.0.1")
            with mock.patch.dict(os.environ, {"SUDO_USER": "admin"}):
                info = web_mod.access_info(cfg)
            self.assertEqual(info["tunnel"], f"ssh -N -L {port}:127.0.0.1:{port} admin@10.0.0.1")

    def test_refuses_public_bind(self):
        app = App(TOKEN, config.Config)
        for bind in ("0.0.0.0", "::", "10.0.0.1"):
            with self.assertRaises(ValueError):
                make_server(app, bind, 0)


FAKE_SLOW = {k: v for k, v in FAKE_STATUS.items() if k != "host"}


def patch_status(test: unittest.TestCase) -> None:
    """Статус хоста подменён: медленная часть (юниты, сокеты) и метрики /proc — по отдельности."""
    for target, value in (("zoolib.status.collect_slow", FAKE_SLOW),
                          ("zoolib.system.host_metrics", FAKE_STATUS["host"])):
        m = mock.patch(target, return_value=value)
        m.start()
        test.addCleanup(m.stop)


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        patch_status(self)
        for p in ("zoolib.system.unit_states", "zoolib.web.views.system.unit_states"):
            m = mock.patch(p, side_effect=lambda units: {u: {"load": "loaded", "active": "active"} for u in units})
            m.start()
            self.addCleanup(m.stop)
        self.app = App(TOKEN, config.load)
        self.c = Client(self.app)

    def tearDown(self):
        self.env.__exit__(None, None, None)


class RoutingTest(AppTestBase):
    def test_auth_required(self):
        for path in ("/", "/users", "/traffic", "/probe", "/logs", "/settings", "/users/owner", "/jobs/1"):
            resp, _ = self.c.get(path)
            self.assertEqual(resp.status, 303, path)
            self.assertTrue(header(resp, "Location")[0].startswith("/login?next="), path)
        resp, _ = self.c.get("/handoff?group=office&per=2")
        self.assertEqual(header(resp, "Location"), ["/login?next=%2Fhandoff%3Fgroup%3Doffice%26per%3D2"],
                         "закладка после истёкшей сессии ведёт туда же, с параметрами")
        resp, _ = self.c.post("/users", {"name": "x"})
        self.assertEqual(resp.status, 401)

    def test_login_flow(self):
        resp, body = self.c.get("/login")
        self.assertEqual(resp.status, 200)
        self.assertIn('type="password"', body)
        resp, body = self.c.post("/login", {"token": TOKEN}, csrf=False)
        self.assertEqual(resp.status, 400, "без double-submit cookie вход не принимается")
        self.c.get("/login")
        resp, body = self.c.post("/login", {"token": "nope", "lc": self.c.cookies[LOGIN_COOKIE]}, csrf=False)
        self.assertEqual(resp.status, 401)
        self.assertIn("Неверный токен", body)
        self.c.get("/login?next=/traffic")
        resp, _ = self.c.post("/login", {"token": TOKEN, "lc": self.c.cookies[LOGIN_COOKIE],
                                         "next": "/traffic"}, csrf=False)
        self.assertEqual(resp.status, 303)
        self.assertEqual(header(resp, "Location"), ["/traffic"])
        sid = [v for v in header(resp, "Set-Cookie") if v.startswith(SID_COOKIE + "=")][0]
        self.assertIn("HttpOnly", sid)
        self.assertIn("SameSite=Strict", sid)
        resp, body = self.c.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("Обзор", body)
        # выход: сессия больше не действует
        resp, _ = self.c.post("/logout")
        self.assertEqual(resp.status, 303)
        resp, _ = self.c.get("/")
        self.assertEqual(resp.status, 303)

    def test_csrf_enforced(self):
        self.c.login()
        resp, body = self.c.post("/users", {"name": "masha", "csrf": "bad"})
        self.assertEqual(resp.status, 403)
        self.assertIn("Форма устарела", body)
        resp, _ = self.c.post("/users", {"name": "masha"}, csrf=False)
        self.assertEqual(resp.status, 403)
        resp, _ = self.c.post("/settings/action", {"action": "collect"}, headers={"Origin": "http://evil.com"})
        self.assertEqual(resp.status, 403)

    def test_host_and_misc(self):
        resp, _ = self.c.get("/login", headers={"Host": "evil.example:8999"})
        self.assertEqual(resp.status, 421)
        self.c.login()
        resp, _ = self.c.get("/nope")
        self.assertEqual(resp.status, 404)
        resp, _ = self.c.get("/probe/run")
        self.assertEqual(resp.status, 405)
        resp, body = self.c.get("/static/app.css")
        self.assertEqual(resp.status, 200)
        self.assertIn("prefers-color-scheme: dark", body)
        self.assertIn("immutable", " ".join(header(resp, "Cache-Control")))
        resp, _ = self.c.get("/healthz")
        self.assertEqual(resp.status, 200)

    def test_security_headers(self):
        self.c.login()
        resp, _ = self.c.get("/")
        csp = header(resp, "Content-Security-Policy")[0]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(header(resp, "X-Frame-Options"), ["DENY"])
        self.assertEqual(header(resp, "Cache-Control"), ["no-store"])

    def test_pages_render(self):
        self._seed_traffic()
        self.c.login()
        for path in ("/", "/users", "/users?verify=1", "/traffic", "/traffic?period=1h", "/traffic?period=30d",
                     "/traffic?period=bogus", "/probe", "/logs", "/settings", "/users/owner", "/users/owner?period=24h"):
            resp, body = self.c.get(path)
            self.assertEqual(resp.status, 200, f"{path}: {body[-500:]}")
            self.assertNotIn("style=", body, f"{path}: inline style нарушит CSP")
            self.assertNotIn("<script>", body, path)
            self.assertNotIn("Hysteria2 <b>", body, "имя протокола должно экранироваться")
        _, body = self.c.get("/")
        self.assertIn("hysteria-server.service — failed", body)
        self.assertIn("лишние открытые порты", body)
        _, body = self.c.get("/traffic")
        self.assertIn("<svg", body)
        self.assertIn("masha", body)

    def _seed_traffic(self):
        from zoolib import users
        users.bootstrap()
        now = int(time.time())
        con = traffic.connect()
        with con:
            for i, (o, m) in enumerate(((0, 0), (1000, 300), (5000, 900))):
                samples = [traffic.Sample("xray", "owner", o, o * 3), traffic.Sample("xray", "masha", m, m),
                           traffic.Sample("vless-reality", "", o + m, o * 3 + m), traffic.Sample(traffic.HOST, "", o * 9, o * 9)]
                deltas, new = traffic.compute_deltas(traffic.load_counters(con), samples, now - 900 + i * 300)
                traffic.store(con, deltas, new, now - 900 + i * 300)
        con.close()


@needs_bash
class UsersViewTest(AppTestBase):
    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        from zoolib import users
        users.bootstrap()
        self.c.login()

    def test_user_lifecycle(self):
        resp, body = self.c.get("/users")
        self.assertIn('name="proto" value="amneziawg" checked', body)
        resp, _ = self.c.post("/users", {"name": "masha", "note": "сестра <3"})
        self.assertEqual(resp.status, 303)
        self.assertEqual(header(resp, "Location"), ["/users/masha"])
        self.assertEqual(set(self.env.proto_users("amneziawg")), {"owner", "masha"})
        resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertIn("пользователь создан", body)
        self.assertIn("сестра &lt;3", body)
        self.assertIn("<h1>masha</h1>", body, "страница пользователя сохраняет заголовок")
        self.assertNotIn("<h1>", self.c.get("/users")[1], "раздел меню не дублирует меню заголовком")
        self.assertIn('value="vless://masha@', body)
        self.assertIn("data-copy=", body)
        self.assertNotIn("<svg", body, "QR отдельной картинкой, не в странице")
        self.assertNotIn("style=", body)
        m = re.search(r'href="(/users/masha/file/[\w.-]+)"', body)
        self.assertIsNotNone(m, "ссылка на скачивание .conf")
        # файл отдаётся по имени и без вызова bash-модулей
        from zoolib import protolib
        with mock.patch("zoolib.protolib.call", side_effect=AssertionError("модуль не должен вызываться")):
            resp, data = self.c.get(m.group(1))
        self.assertEqual(resp.status, 200)
        disp = header(resp, "Content-Disposition")[0]
        self.assertIn('attachment; filename="masha-amneziawg.conf"', disp)
        self.assertIn("filename*=UTF-8''masha-amneziawg.conf", disp)
        self.assertIn("[Interface]", data)
        resp, _ = self.c.get("/users/masha/file/99")
        self.assertEqual(resp.status, 404)
        # чужое по имени не отдаём: ключи модулей, не .conf, выход из каталога, чужой каталог
        for bad in ("amneziawg.key", "..", "config.env", "amneziawg-owner.conf"):
            resp, _ = self.c.get(f"/users/masha/file/{bad}")
            self.assertEqual(resp.status, 404, bad)
        resp, _ = self.c.get("/users/masha/file/%2e%2e%2fconfig.env")
        self.assertEqual(resp.status, 404)

        resp, _ = self.c.post("/users/masha/disable", {"back": "/users"})
        self.assertEqual(header(resp, "Location"), ["/users"])
        self.assertEqual(self.env.proto_users("vless-reality")["masha"], "false")
        _, body = self.c.get("/users")
        self.assertIn("отключён", body)
        self.c.post("/users/masha/enable", {"back": "//evil"})
        self.assertEqual(self.env.proto_users("vless-reality")["masha"], "true")

        resp, body = self.c.get("/users/masha/delete")
        self.assertIn("Удалить навсегда", body)
        resp, _ = self.c.post("/users/masha/delete")
        self.assertEqual(header(resp, "Location"), ["/users"])
        self.assertNotIn("masha", self.env.proto_users("amneziawg"))
        _, body = self.c.get("/users")
        self.assertIn("пользователь удалён", body)

    def test_add_errors(self):
        resp, _ = self.c.post("/users", {"name": "Bad Name"})
        _, body = self.c.get(header(resp, "Location")[0])
        self.assertIn("недопустимое имя", body)
        self.env.fail("vless-reality:user_add")
        resp, _ = self.c.post("/users", {"name": "kolya"}, multi={"proto": ["amneziawg", "vless-reality"],
                                                                  "name": ["kolya"], "csrf": [self.c.csrf]})
        _, body = self.c.get("/users")
        self.assertIn("откатены", body)
        self.assertNotIn("kolya", self.env.proto_users("amneziawg"))

    def test_only_selected_protocols(self):
        self.c.post("/users", {"name": "petya"}, multi={"proto": ["amneziawg"], "name": ["petya"],
                                                       "csrf": [self.c.csrf]})
        self.assertIn("petya", self.env.proto_users("amneziawg"))
        self.assertNotIn("petya", self.env.proto_users("vless-reality"))

    def test_nojs_other_group_with_default_group_checkboxes_is_not_custom(self):
        from zoolib import groups, users
        g = groups.create("Семья", ["amneziawg"])
        main = groups.Groups.load().get(groups.MAIN_ID).resolve(users.managed_protocols()[0])
        # без JS галочки остались от «Основной»: набор группы, а не «свой»
        self.c.post("/users", {"name": "petya", "group": g.id},
                    multi={"proto": main, "name": ["petya"], "group": [g.id], "csrf": [self.c.csrf]})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["petya"]["group"], reg["petya"]["protocols"]), (g.id, ["amneziawg"]))
        self.assertNotIn("custom", reg["petya"])
        # осознанно другой набор — по-прежнему «свой»
        self.c.post("/users", {"name": "vasya", "group": g.id},
                    multi={"proto": ["vless-reality"], "name": ["vasya"], "group": [g.id], "csrf": [self.c.csrf]})
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual(reg["vasya"]["protocols"], ["vless-reality"])
        self.assertTrue(reg["vasya"]["custom"])

    def test_owner_not_deletable(self):
        _, body = self.c.get("/users/owner/delete")
        self.assertIn("owner не удаляется", body)
        self.c.post("/users/owner/delete")
        self.assertIn("owner", self.env.proto_users("amneziawg"))


@needs_bash
class BulkUsersTest(AppTestBase):
    """Таблица пользователей: галочки, панель действий, /users/bulk."""

    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        from zoolib import users
        users.bootstrap()
        self.c.login()
        for n in ("masha", "kolya", "petya"):
            self.c.post("/users", {"name": n})

    def bulk(self, action, names, csrf=None, **extra):
        self.c.get("/users")
        token = self.c.csrf if csrf is None else csrf
        multi = {"action": [action], "names": list(names), "csrf": [token], **{k: [v] for k, v in extra.items()}}
        return self.c.req("POST", "/users/bulk", {k: v[-1] for k, v in multi.items() if v}, multi=multi)

    def enabled(self, name, pid="vless-reality"):
        return self.env.proto_users(pid).get(name)

    def flashes(self):
        return self.c.get("/users")[1]

    def test_selection_ui(self):
        _, body = self.c.get("/users")
        self.assertIn('<input type="checkbox" name="names" value="masha" form="bulk" data-pick aria-label="Выбрать masha">', body)
        self.assertIn('<th class="pick" scope="col"><input type="checkbox" data-pick-all aria-label="Выбрать всех">', body)
        bar = body[body.index('<form method="post" action="/users/bulk"'):]
        bar = bar[:bar.index("</form>")]
        for text in (">Удалить<", ">Отключить<", ">Включить<", "В группу ▾", 'value="move:main"', 'name="csrf"'):
            self.assertIn(text, bar)
        self.assertIn('id="bulk"', bar)
        self.assertIn("data-bulk-bar", bar)
        self.assertNotIn("Новое подключение", body, "кнопки мастера на странице пользователей больше нет")
        self.assertNotIn("/connect/new", body)
        self.assertNotRegex(body, r"\sstyle=|\son[a-z]+=")

    def test_disable_and_enable_selected(self):
        resp, _ = self.bulk("disable", ["masha", "kolya"])
        self.assertEqual(header(resp, "Location"), ["/users"])
        self.assertEqual((self.enabled("masha"), self.enabled("kolya"), self.enabled("petya")), ("false", "false", "true"))
        self.assertEqual(self.enabled("masha", "amneziawg"), "false")
        body = self.flashes()
        self.assertIn("Отключено: 2 из 2 — masha, kolya", body)
        self.assertEqual(body.count('class="ok"'), 1, "одно итоговое сообщение, не по сообщению на человека")
        self.bulk("enable", ["masha", "kolya"])
        self.assertEqual((self.enabled("masha"), self.enabled("kolya")), ("true", "true"))
        self.assertIn("Включено: 2 из 2", self.flashes())

    def test_delete_goes_through_confirm_page_listing_names(self):
        resp, body = self.bulk("delete", ["masha", "kolya"])
        self.assertEqual(resp.status, 200)
        self.assertIn("Удалить пользователей: 2?", body)
        for n in ("masha", "kolya"):
            self.assertIn(f'<span class="chip">{n}</span>', body)
            self.assertIn(f'<input type="hidden" name="names" value="{n}">', body)
        self.assertIn('name="confirm" value="1"', body)
        self.assertIn("Удалить навсегда", body)
        self.assertNotIn("petya</span>", body)
        self.assertEqual((self.enabled("masha"), self.enabled("kolya")), ("true", "true"), "до подтверждения ничего не удалено")
        resp, _ = self.bulk("delete", ["masha", "kolya"], confirm="1")
        self.assertEqual(header(resp, "Location"), ["/users"])
        for pid in ("vless-reality", "amneziawg"):
            self.assertEqual(set(self.env.proto_users(pid)), {"owner", "petya"})
        self.assertIn("Удалено: 2 из 2 — masha, kolya", self.flashes())
        self.assertEqual(set(self.env.users_json()["users"][i]["name"] for i in range(2)), {"owner", "petya"})

    def test_owner_and_system_are_refused_for_delete_and_disable(self):
        from zoolib import users
        users.ensure_probe_user()
        resp, body = self.bulk("delete", ["owner", "masha", "zoo-probe"])
        self.assertEqual(resp.status, 200)
        self.assertIn("Удалить пользователей: 1?", body)
        self.assertIn("owner: owner не удаляется", body)
        self.assertIn("zoo-probe: служебный", body)
        self.assertNotIn('name="names" value="owner"', body)
        self.assertNotIn('name="names" value="zoo-probe"', body)
        # подтверждение с теми же именами: отказы повторяются на сервере, скрытым полям он не верит
        resp, _ = self.bulk("delete", ["owner", "masha", "zoo-probe"], confirm="1")
        self.assertEqual(resp.status, 303)
        body = self.flashes()
        self.assertIn("owner: owner не удаляется", body)
        self.assertIn("zoo-probe: служебный", body)
        self.assertIn("Удалено: 1 из 3 — masha", body)
        names = {u["name"] for u in self.env.users_json()["users"]}
        self.assertEqual(names, {"owner", "kolya", "petya", "zoo-probe"})
        resp, _ = self.bulk("delete", ["owner"], confirm="1")
        self.assertEqual(resp.status, 303, "один owner: страницы подтверждения нет, только отказ")
        self.assertIn("owner не удаляется", self.flashes())
        self.assertIn("owner", self.env.proto_users("amneziawg"))
        self.bulk("disable", ["owner", "zoo-probe", "kolya"])
        body = self.flashes()
        self.assertIn("owner: owner не отключается", body)
        self.assertEqual((self.enabled("owner"), self.enabled("kolya")), ("true", "false"))
        self.assertEqual(self.enabled("zoo-probe"), "true")

    def test_errors_are_reported_per_user(self):
        self.env.fail("amneziawg:user_enable")
        self.bulk("disable", ["masha", "kolya"])
        body = self.flashes()
        self.assertIn("masha: состояние не изменено", body)
        self.assertIn("kolya: состояние не изменено", body)
        self.assertEqual((self.enabled("masha"), self.enabled("kolya")), ("true", "true"))
        # смешанный итог: один отказ owner, остальные прошли
        os.environ.pop("FAKE_FAIL")
        self.bulk("disable", ["owner", "masha"])
        body = self.flashes()
        self.assertIn("Отключено: 1 из 2 — masha", body)
        self.assertIn('class="warn"', body)

    def test_move_to_group(self):
        from zoolib import groups
        g = groups.create("Телефон", ["amneziawg"])
        self.bulk(f"move:{g.id}", ["masha", "kolya"])
        reg = {u["name"]: u for u in self.env.users_json()["users"]}
        self.assertEqual((reg["masha"]["group"], reg["masha"]["protocols"]), (g.id, ["amneziawg"]))
        self.assertEqual(reg["kolya"]["group"], g.id)
        self.assertEqual(reg["petya"]["group"], "main")
        self.assertIn("В группе «Телефон»: 2 из 2 — masha, kolya", self.flashes())
        self.bulk("move:nope", ["petya"])
        self.assertIn("нет", self.flashes())
        self.assertEqual(self.env.users_json()["users"][3]["group"], "main")
        # владельца переводить можно, служебного — нет
        self.bulk("move:main", ["owner", "zoo-probe"])
        self.assertIn("zoo-probe: нет в реестре", self.flashes())

    def test_hostile_input(self):
        evil = '<script>alert(1)</script>'
        for names in ([evil], ["../etc/passwd"], ["a" * 500], ["masha\nkolya"], [""]):
            for action in ("disable", "delete"):
                resp, body = self.bulk(action, names, confirm="1")
                self.assertNotIn("<script>alert", body)
                self.assertEqual(resp.status, 303)
        body = self.flashes()
        self.assertNotIn("<script>alert", body)
        self.assertIn("некорректное имя", body)
        for action in ("", "evil", "move:", "delete:x", "enable:main", "move:a b"):
            resp, _ = self.bulk(action, ["masha"])
            self.assertEqual(resp.status, 303, action)
            self.assertIn("Неизвестное действие", self.flashes()) if action != "move:a b" else None
        resp, _ = self.bulk("disable", [])
        self.assertIn("Никого не выбрано", self.flashes())
        self.assertEqual({self.enabled(n) for n in ("masha", "kolya", "petya")}, {"true"})
        self.assertEqual(len(self.env.users_json()["users"]), 4)
        # очень длинный список режется, а не раздувает работу
        resp, _ = self.bulk("disable", [f"u{i}" for i in range(2000)])
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.enabled("masha"), "true")

    def test_csrf_origin_and_login(self):
        resp, _ = self.bulk("disable", ["masha"], csrf="wrong")
        self.assertEqual(resp.status, 403)
        resp, _ = self.bulk("delete", ["masha"], csrf="", confirm="1")
        self.assertEqual(resp.status, 403)
        self.c.get("/users")
        resp, _ = self.c.post("/users/bulk", {"action": "disable", "names": "masha"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, _ = Client(self.app).post("/users/bulk", {"action": "disable", "names": "masha"}, csrf=False)
        self.assertEqual(resp.status, 401)
        resp, _ = self.c.get("/users/bulk")
        self.assertEqual(resp.status, 404, "это не страница пользователя «bulk»")
        self.assertEqual({self.enabled(n) for n in ("masha", "kolya", "petya")}, {"true"})
        self.assertEqual(len(self.env.users_json()["users"]), 4)

    def test_one_lock_for_the_whole_batch(self):
        from zoolib import users
        self.c.get("/users")
        self.c.get("/users")
        real = users._lock
        multi = {"action": ["disable"], "names": ["masha", "kolya", "petya"], "csrf": [self.c.csrf]}
        with mock.patch.object(users, "_lock", side_effect=real) as lock:
            self.c.req("POST", "/users/bulk", {"action": "disable", "csrf": self.c.csrf}, multi=multi)
        self.assertEqual(lock.call_count, 1)
        self.assertEqual({self.enabled(n) for n in ("masha", "kolya", "petya")}, {"false"})

    def test_live_pauses_while_rows_are_selected(self):
        from zoolib.web import assets
        self.assertIn("input[data-pick]:checked", assets.JS)
        self.assertIn("data-pick-all", assets.JS)
        self.assertIn("bulkSync", assets.JS)


@needs_bash
class ProtoChipTest(AppTestBase):
    """Чип протоколов: сравнение с ожидаемым набором пользователя, а не со всем сервером."""

    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        from zoolib import groups, users
        users.bootstrap()
        self.g = groups.create("Телефон", ["vless-reality", "amneziawg"])
        users.add_user("masha", group=self.g.id)
        self.env.add_protocol("tuic", users=())   # включили позже: у группы Телефон его нет
        self.c.login()

    def test_group_without_new_protocol_is_not_warned(self):
        _, body = self.c.get("/users")
        self.assertIn('<span class="chip">2 из 2</span>', body)
        self.assertIn('<span class="chip warn" title="2 из 3">нет: TUIC</span>', body, "owner в «Основной»: ждёт все три")

    def test_all_protocols_group_and_no_group_expect_everything(self):
        from zoolib import users
        reg = users.Registry.load()
        reg.get("masha").group = ""
        reg.save()
        _, body = self.c.get("/users")
        self.assertEqual(body.count("нет: TUIC"), 2)
        reg.get("masha").group = "main"
        reg.save()
        _, body = self.c.get("/users")
        self.assertEqual(body.count("нет: TUIC"), 2)

    def test_custom_set_is_compared_with_all_enabled(self):
        from zoolib import users
        reg = users.Registry.load()
        u = reg.get("masha")
        u.custom, u.protocols = True, ["amneziawg"]
        reg.save()
        _, body = self.c.get("/users")
        m = re.search(r'<span class="chip warn" title="([^"]*)">нет: TUIC, VLESS Vision</span>', body)
        self.assertIsNotNone(m)
        self.assertIn("1 из 3", m.group(1))
        self.assertIn("свой набор", m.group(1))

    def test_expected_set_unit(self):
        from zoolib import groups, users
        from zoolib.web import userviews
        managed = ["a", "b", "c"]
        gs = groups.Groups(None, [groups.Group("x", "X", ["b", "gone"]), groups.Group("all", "Все", ["*"])])
        mk = lambda **kw: users.User("u", protocols=kw.pop("protocols", ["b"]), **kw)
        self.assertEqual(userviews._expected_protocols(mk(group="x"), managed, gs), ["b"], "только включённые из группы")
        self.assertEqual(userviews._expected_protocols(mk(group="all"), managed, gs), managed)
        self.assertEqual(userviews._expected_protocols(mk(group=""), managed, gs), managed)
        self.assertEqual(userviews._expected_protocols(mk(group="nope"), managed, gs), managed)
        self.assertEqual(userviews._expected_protocols(mk(group="x", custom=True), managed, gs), managed)
        self.assertEqual(str(userviews._proto_chip(mk(group="x"), managed, gs)), '<span class="chip">1 из 1</span>')
        self.assertEqual(str(userviews._proto_chip(mk(group="all", protocols=[]), managed, gs)),
                         '<span class="chip warn" title="0 из 3; нет в: a, b, c">нет: a, b…</span>')


@needs_bash
class OverviewStartTest(AppTestBase):
    def test_get_started_button(self):
        self.c.login()
        _, body = self.c.get("/")
        self.assertIn('<a href="/connect/new" class="btn primary" data-swap title="Группа: протоколы, приложения, люди и что им отправить">Подключить людей</a>', body)
        self.assertEqual(body.count("Подключить людей"), 1)
        self.assertNotIn("Get started", body)


class ProbeViewTest(AppTestBase):
    def test_compare(self):
        self.c.login()
        resp, body = self.c.post("/probe/compare", {"report": "not json"})
        self.assertIn("Это не JSON", body)
        resp, body = self.c.post("/probe/compare", {"report": '{"results": [{"id": "a", "verdict": "OK"}]}'})
        self.assertIn("Нет серверной самопроверки", body)
        from zoolib import probe
        from zoolib.fsutil import atomic_write_json
        atomic_write_json(probe.selftest_file(), {"generated": "2026-10-04T00:00:00+00:00", "server_ip": "10.0.0.1",
                                                 "results": [{"id": "vless-reality", "verdict": "OK", "port": 443},
                                                             {"id": "hysteria2", "verdict": "SERVER_DOWN"}]})
        report = ('{"results": [{"id": "vless-reality", "verdict": "FREEZE_16K"},'
                  ' {"id": "hysteria2", "verdict": "UDP_BLOCKED"}]}')
        resp, body = self.c.post("/probe/compare", {"report": report})
        self.assertEqual(resp.status, 200)
        self.assertIn("блокируется у вас", body)
        self.assertIn("не работает на самом сервере", body)
        _, body = self.c.get("/probe")
        self.assertIn("SERVER_DOWN", body)


class SocketBase(unittest.TestCase):
    """Настоящий сокет: сервер на 127.0.0.1:0, запросы http.client."""

    def setUp(self):
        self.env = ZooEnv().__enter__()
        patch_status(self)
        self.httpd = make_server(App(TOKEN, config.load), "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.__exit__(None, None, None)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data


class SocketServerTest(SocketBase):
    def test_over_socket(self):
        r, _ = self.request("GET", "/")
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Server"), "zoo-web")
        r, body = self.request("GET", "/login")
        lc = r.getheader("Set-Cookie").split(";")[0]
        nonce = lc.split("=", 1)[1]
        form = urllib.parse.urlencode({"token": TOKEN, "lc": nonce})
        r, _ = self.request("POST", "/login", form, {"Content-Type": "application/x-www-form-urlencoded",
                                                     "Cookie": lc})
        self.assertEqual(r.status, 303)
        sid = [c for c in r.headers.get_all("Set-Cookie") if c.startswith("zoo_sid_")][0].split(";")[0]
        r, body = self.request("GET", "/", headers={"Cookie": sid})
        self.assertEqual(r.status, 200)
        self.assertIn("Обзор".encode(), body)
        r, _ = self.request("POST", "/users", "{}", {"Content-Type": "application/json", "Cookie": sid})
        self.assertEqual(r.status, 415)
        r, _ = self.request("POST", "/users", "x", {"Content-Type": "application/x-www-form-urlencoded",
                                                   "Content-Length": str(10 ** 8), "Cookie": sid})
        self.assertEqual(r.status, 413)
        r, _ = self.request("POST", "/login", "", {"Content-Length": "-1"})
        self.assertEqual(r.status, 400)


# ---------- вёрстка: общие куски ----------

class VisibleWords(HTMLParser):
    """Слова, которые человек видит сразу: без шапки, подвала, картинок SVG и свёрнутого (<details>, окна)."""
    SKIP = {"head", "nav", "footer", "script", "style", "select", "textarea", "svg", "dialog"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.words: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("input", "img", "br", "meta", "link", "hr"):
            self.stack.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        tags = [x for x, _ in self.stack]
        if any(x in self.SKIP for x in tags) or ("details" in tags and "summary" not in tags):
            return
        if any("hidden" in a for _, a in self.stack):
            return
        self.words += data.split()


def visible_words(html: str) -> int:
    p = VisibleWords()
    p.feed(html)
    return len(p.words)


class HtmlPartsTest(unittest.TestCase):
    def test_table_stack_and_headers(self):
        out = table([("↑", "от клиента"), "имя"], [["1", "a"], ["2", "b"]], num=[0], stack=True)
        self.assertIn('data-label="↑"', out)
        self.assertIn('data-label="имя"', out)
        self.assertIn('title="от клиента"', out)
        self.assertIn('class="stack"', out)
        self.assertNotIn("data-label", table(["a"], [["1"]]))

    def test_table_empty_is_state_not_table(self):
        out = table(["a", "b"], [], empty="пусто тут")
        self.assertIn("empty-state", out)
        self.assertNotIn("<table", out)

    def test_card_help_and_confirm(self):
        out = card("Заголовок", "тело", help="пояснение")
        self.assertIn('<details class="help"><summary aria-label="Пояснение">?</summary>', out)
        self.assertNotIn("<details", card("Без", "тела"))
        self.assertIn('data-confirm="Точно?"', post_button("/x", "Да", "csrf", confirm="Точно?"))
        self.assertNotIn("data-confirm", post_button("/x", "Да", "csrf"))
        self.assertIn(" data-swap", post_button("/x", "Да", "csrf"))
        self.assertNotIn("data-swap", post_button("/x", "Да", "csrf", swap=False))

    def test_css_fixes(self):
        css = assets.CSS
        self.assertIn("[hidden] { display: none !important; }", css)
        self.assertNotIn(".ptile.s1", css)
        self.assertNotRegex(css, r"\n\.s1 \{")  # цвета рядов — только у графиков и маркеров
        self.assertIn(".chart .s1", css)
        self.assertIn(".swatch.s1", css)
        self.assertRegex(css, r"@media \(max-width: 600px\) \{[^@]*\.nav \{[^}]*display: grid")
        self.assertRegex(css, r"table\.stack td::before")
        for rule in (".tab.active", ".btn.danger-solid"):
            line = next(x for x in css.splitlines() if x.startswith(rule))
            self.assertNotIn("#fff", line)

    def test_js_contract(self):
        js = assets.JS
        for needle in ("dialog[open]", "focusin", "/api/stamp", "X-Zoo-Live", "X-Zoo-Stamp", "a.origin",
                       "form[data-autosubmit]", "img[data-src]", "сессия истекла", "нет связи", "details[open]",
                       "removeItem('zoo-live')"):
            self.assertIn(needle, js)
        for gone in ("Пауза", "setItem('zoo-live'", "live-btn"):
            self.assertNotIn(gone, js + assets.CSS)
        self.assertEqual(gzip.decompress(assets.JS_GZ).decode(), js)
        self.assertEqual(gzip.decompress(assets.CSS_GZ).decode(), assets.CSS)

    def test_js_contract_navigation(self):
        js = assets.JS
        # 1: ответ POST (сравнение, шаги мастера, ошибки проверки) держится, пока не будет GET-перехода
        self.assertRegex(js, r"held = post && !x\.moved")
        self.assertRegex(js, r"function idle\(\) \{\s*if \(document\.hidden \|\| held \|\| document\.body\.classList\.contains\('busy'\)\) return true;")
        # 2: запоздалые ответы отбрасываются
        for needle in ("seq !== navSeq", "href !== location.href", "document.contains(box)", "var post = !!(init"):
            self.assertIn(needle, js)
        # 3: история
        for needle in ("history.pushState(null, '', x.url)", "addEventListener('popstate'", "{ pop: true }"):
            self.assertIn(needle, js)
        self.assertRegex(js, r"(?s)'popstate'.*?go\(location\.href, undefined, \{ pop: true \}\)")
        # 4: одиночный select сравнивается по selectedIndex
        self.assertIn("i.selectedIndex !== (def < 0 ? 0 : def)", js)
        # 5: двойная отправка
        for needle in ("if (f.dataset.sending) return;", "delete opts.form.dataset.sending", "btn.disabled = true",
                       "opts.btn.disabled = false"):
            self.assertIn(needle, js)

    def test_sanitize_hides_qr_blocks(self):
        qr_text = "\n".join(["████ ▄▄ ████", "█  █ ▀▀ █  █", "████ ▄  ████", "     ▀▄     "])
        out = logs.sanitize("до\n" + qr_text + "\nпосле")
        self.assertIn("[QR скрыт]", out)
        self.assertNotIn("█", out)
        self.assertIn("до", out)
        self.assertIn("после", out)
        # две строки — ещё не QR
        self.assertIn("█", logs.sanitize("█ █\n▄▄ █\nтекст"))
        # длинная строка блоков без конца строки: регулярка линейная (была квадратичной — зависала)
        t0 = time.monotonic()
        logs.sanitize("█" * 200_000 + "x")
        self.assertLess(time.monotonic() - t0, 2)


# ---------- вход и сессии ----------

class AuthStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zoo-auth-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Path(self.dir) / "web-sessions.json"

    def test_session_survives_new_auth(self):
        a = auth.Auth(TOKEN, store=self.store)
        s = a.login(TOKEN)
        b = auth.Auth(TOKEN, store=self.store)  # рестарт админки
        got = b.session(s.sid)
        self.assertIsNotNone(got)
        self.assertEqual(got.csrf, s.csrf)

    def test_other_token_has_no_sessions(self):
        s = auth.Auth(TOKEN, store=self.store).login(TOKEN)
        self.assertIsNone(auth.Auth("another-token-0123456789", store=self.store).session(s.sid))

    def test_file_is_private_and_has_no_plain_sid(self):
        s = auth.Auth(TOKEN, store=self.store).login(TOKEN)
        text = self.store.read_text(encoding="utf-8")
        self.assertNotIn(s.sid, text)
        self.assertIn(auth.sid_hash(s.sid), text)
        data = json.loads(text)
        self.assertEqual(data["fp"], auth.token_fp(TOKEN))
        self.assertNotIn(TOKEN, text)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

    def test_flash_is_not_stored(self):
        a = auth.Auth(TOKEN, store=self.store)
        s = a.login(TOKEN)
        s.flash("bad", "секретное сообщение")
        a.login(TOKEN)  # любая запись на диск
        self.assertNotIn("секретное", self.store.read_text(encoding="utf-8"))
        self.assertEqual(auth.Auth(TOKEN, store=self.store).session(s.sid).flashes, [])

    def test_live_request_does_not_extend_idle(self):
        now = [1000.0]
        a = auth.Auth(TOKEN, clock=lambda: now[0], store=self.store)
        s = a.login(TOKEN)
        now[0] += 100
        a.session(s.sid, touch=False)
        self.assertEqual(s.last, 1000.0)
        a.session(s.sid)
        self.assertEqual(s.last, 1100.0)
        # одна открытая вкладка с live не держит сессию вечно
        now[0] += auth.IDLE_TTL - 50
        self.assertIsNotNone(a.session(s.sid, touch=False))
        now[0] += 100
        self.assertIsNone(a.session(s.sid, touch=False))

    def test_ttl_and_throttled_disk_writes(self):
        self.assertEqual((auth.IDLE_TTL, auth.ABSOLUTE_TTL), (14 * 86400, 30 * 86400))
        now = [1000.0]
        a = auth.Auth(TOKEN, clock=lambda: now[0], store=self.store)
        s = a.login(TOKEN)
        now[0] += 60
        a.session(s.sid)
        self.assertEqual(json.loads(self.store.read_text())["s"][auth.sid_hash(s.sid)]["last"], 1000.0)
        now[0] += auth.DISK_TOUCH
        a.session(s.sid)
        self.assertEqual(json.loads(self.store.read_text())["s"][auth.sid_hash(s.sid)]["last"], now[0])
        # абсолютный срок
        for _ in range(35):
            now[0] += 86400
            a.session(s.sid)
        self.assertIsNone(a.session(s.sid))

    def test_logout_removes_from_file(self):
        a = auth.Auth(TOKEN, store=self.store)
        s = a.login(TOKEN)
        a.logout(s.sid)
        self.assertIsNone(auth.Auth(TOKEN, store=self.store).session(s.sid))
        self.assertNotIn(auth.sid_hash(s.sid), self.store.read_text(encoding="utf-8"))

    def test_broken_store_is_ignored(self):
        self.store.write_text("{не json", encoding="utf-8")
        a = auth.Auth(TOKEN, store=self.store)
        self.assertIsNotNone(a.login(TOKEN))

    def test_new_token_removes_sessions_file(self):
        with ZooEnv() as env:
            with mock.patch("zoolib.system.listening_sockets", return_value=[]):
                web_mod.setup(config.load())
            store = web_mod.sessions_file()
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(new_token=True, info=False, link=False, json=True, bind="127.0.0.1", port=None)
            with mock.patch("zoolib.system.unit_states", return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(web_mod.cmd_web(args, config.load()), 0)
            self.assertFalse(store.exists())


class LoginFormTest(AppTestBase):
    def test_form_has_username_hint_and_label(self):
        _, body = self.c.get("/login")
        self.assertIn('name="username"', body)
        self.assertIn('autocomplete="username"', body)
        self.assertIn('value="zoo test"', body)  # метка из config.env
        self.assertIn(">Токен</label>", body)
        # та же команда, что в README, с настоящими портом и адресом: --link даёт ссылку, а не токен
        self.assertIn(web_mod.access_info(config.load())["command"], body)
        self.assertIn("ssh -t -L 7070:127.0.0.1:7070 root@", body)
        self.assertNotIn("sudo zoo web --link", body)
        self.assertNotIn("data-autosubmit", body)

    def test_lockout_message_and_threshold(self):
        self.assertEqual(auth.FAIL_LIMIT, 50)
        for _ in range(auth.FAIL_LIMIT):
            self.app.auth.login("nope")
        resp, body = self.c.get("/login")
        self.assertRegex(body, r"Подождите \d+ с")
        self.c.get("/login")
        resp, body = self.c.post("/login", {"token": TOKEN, "lc": self.c.cookies[LOGIN_COOKIE]}, csrf=False)
        self.assertEqual(resp.status, 429)

    def test_cookie_names_carry_port(self):
        self.assertEqual(auth.cookie_names("127.0.0.1:8999"), ("zoo_sid_8999", "zoo_login_8999"))
        self.assertEqual(auth.cookie_names("[::1]:24305"), ("zoo_sid_24305", "zoo_login_24305"))
        self.assertEqual(auth.cookie_names("localhost"), ("zoo_sid_80", "zoo_login_80"))
        self.c.login()
        self.assertIn("zoo_sid_8999", self.c.cookies)
        resp, _ = self.c.post("/logout")
        self.assertIn("Max-Age=0", [v for v in header(resp, "Set-Cookie") if v.startswith("zoo_sid_8999")][0])

    def test_session_cookie_max_age(self):
        self.c.get("/login")
        resp, _ = self.c.post("/login", {"token": TOKEN, "lc": self.c.cookies[LOGIN_COOKIE]}, csrf=False)
        sid = [v for v in header(resp, "Set-Cookie") if v.startswith(SID_COOKIE + "=")][0]
        self.assertIn(f"Max-Age={auth.ABSOLUTE_TTL}", sid)

    def test_second_login_tab_does_not_break_first(self):
        self.c.get("/login")
        first = self.c.cookies[LOGIN_COOKIE]
        _, body2 = self.c.get("/login")  # вторая вкладка
        self.assertEqual(self.c.cookies[LOGIN_COOKIE], first)
        self.assertIn(f'name="lc" value="{first}"', body2)
        resp, _ = self.c.post("/login", {"token": TOKEN, "lc": first}, csrf=False)
        self.assertEqual(resp.status, 303)


class OnceLinkTest(AppTestBase):
    def open_form(self, value):
        resp, body = self.c.get("/login?" + urllib.parse.urlencode({"once": value}))
        return resp, body

    def submit(self, value, **kw):
        self.c.get("/login")
        form = {"lc": self.c.cookies[LOGIN_COOKIE], "once": value, "next": "/", **kw}
        return self.c.post("/login", form, csrf=False)

    def test_link_logs_in_once(self):
        value = auth.make_once(TOKEN)
        resp, body = self.open_form(value)
        self.assertEqual(resp.status, 200)
        self.assertIn("data-autosubmit", body)
        self.assertIn(f'name="once" value="{value}"', body)
        resp, _ = self.submit(value)
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.c.get("/")[0].status, 200)
        # повторное использование — отказ
        other = Client(self.app)
        other.get("/login")
        resp, body = other.post("/login", {"lc": other.cookies[LOGIN_COOKIE], "once": value}, csrf=False)
        self.assertEqual(resp.status, 401)
        self.assertIn("Ссылка устарела", body)

    def test_get_does_not_consume_link(self):
        value = auth.make_once(TOKEN)
        for _ in range(3):
            self.open_form(value)
        self.assertEqual(self.submit(value)[0].status, 303)

    def test_expired_and_forged(self):
        old = auth.make_once(TOKEN, now=time.time() - auth.ONCE_TTL - 5)
        self.assertEqual(self.submit(old)[0].status, 401)
        n, e, sig = auth.make_once(TOKEN).split(".")
        forged = f"{n}.{e}.{'0' * len(sig)}"
        self.assertEqual(self.submit(forged)[0].status, 401)
        later = f"{n}.{int(e) + 3600}.{sig}"  # срок продлили, подпись осталась старой
        self.assertEqual(self.submit(later)[0].status, 401)
        self.assertEqual(self.submit("мусор")[0].status, 401)
        self.assertEqual(self.submit(auth.make_once("a-different-token-0123456789"))[0].status, 401)
        self.assertEqual(self.c.get("/")[0].status, 303, "ни одна попытка не пустила")

    def test_link_works_during_lockout(self):
        for _ in range(auth.FAIL_LIMIT):
            self.app.auth.login("nope")
        self.assertGreater(self.app.auth.locked(), 0)
        self.assertEqual(self.submit(auth.make_once(TOKEN))[0].status, 303)

    def test_failed_link_does_not_autosubmit_again(self):
        """Иначе страница с ошибкой отправляла бы форму сама по кругу (в блокировке — без паузы)."""
        old = auth.make_once(TOKEN, now=time.time() - auth.ONCE_TTL - 5)
        resp, body = self.submit(old)
        self.assertEqual(resp.status, 401)
        self.assertNotIn("data-autosubmit", body)
        self.assertNotIn('name="once"', body)
        for _ in range(auth.FAIL_LIMIT):
            self.app.auth.login("nope")
        resp, body = self.submit(old)
        self.assertEqual(resp.status, 429)
        self.assertNotIn("data-autosubmit", body)
        self.assertIn("Подождите", body)

    def test_link_not_reusable_after_restart(self):
        value = auth.make_once(TOKEN)
        self.assertEqual(self.submit(value)[0].status, 303)
        self.app = App(TOKEN, config.load)  # рестарт админки: тот же файл сессий
        self.c = Client(self.app)
        self.assertEqual(self.submit(value)[0].status, 401)

    def test_token_typed_by_hand_still_works_with_link_form(self):
        resp, _ = self.submit("мусор", token=TOKEN)
        self.assertEqual(resp.status, 303)

    def test_info_prints_link(self):
        with ZooEnv():
            with mock.patch("zoolib.system.listening_sockets", return_value=[]):
                web_mod.setup(config.load())
            cfg = config.load()
            info = web_mod.access_info(cfg)
            m = re.fullmatch(rf"http://127\.0\.0\.1:{info['port']}/login\?once=([\w-]+)\.(\d+)\.([0-9a-f]{{32}})",
                             info["link"])
            self.assertIsNotNone(m, info["link"])
            self.assertAlmostEqual(int(m.group(2)), time.time() + auth.ONCE_TTL, delta=5)
            a = auth.Auth(cfg.get("ZOO_WEB_TOKEN"))
            self.assertIsNotNone(a.login_once(info["link"].split("once=")[1]))


# ---------- сервер: keep-alive и gzip ----------

class TransportTest(SocketBase):
    def conn(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)
        return c

    def test_keep_alive_two_requests_one_connection(self):
        c = self.conn()
        c.request("GET", "/healthz")
        r1 = c.getresponse()
        self.assertEqual((r1.status, r1.read(), r1.version), (200, b"ok", 11))
        sock = c.sock
        c.request("GET", "/")  # 303 с пустым телом не зависает
        r2 = c.getresponse()
        r2.read()
        self.assertEqual(r2.status, 303)
        c.request("GET", "/healthz")
        r3 = c.getresponse()
        self.assertEqual((r3.status, r3.read()), (200, b"ok"))
        self.assertIs(c.sock, sock, "соединение не переоткрывалось")

    def test_gzip_matches_plain(self):
        c = self.conn()
        c.request("GET", "/static/app.css")
        plain = c.getresponse()
        raw = plain.read()
        self.assertIsNone(plain.getheader("Content-Encoding"))
        self.assertEqual(plain.getheader("Vary"), "Accept-Encoding")
        c.request("GET", "/static/app.css", headers={"Accept-Encoding": "gzip, deflate"})
        z = c.getresponse()
        body = z.read()
        self.assertEqual(z.getheader("Content-Encoding"), "gzip")
        self.assertLess(len(body), len(raw) // 2)
        self.assertEqual(gzip.decompress(body), raw)
        c.request("GET", "/static/app.css", headers={"Accept-Encoding": "gzip;q=0"})
        r = c.getresponse()
        r.read()
        self.assertIsNone(r.getheader("Content-Encoding"))

    def test_gzip_dynamic_page_and_small_bodies(self):
        c = self.conn()
        c.request("GET", "/login", headers={"Accept-Encoding": "gzip"})
        r = c.getresponse()
        html = gzip.decompress(r.read())
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        self.assertIn("Токен".encode(), html)
        c.request("GET", "/healthz", headers={"Accept-Encoding": "gzip"})
        r = c.getresponse()
        r.read()
        self.assertIsNone(r.getheader("Content-Encoding"), "короткое тело не сжимается")

    def test_bad_content_length_closes_connection(self):
        for length, status in (("-1", 400), ("abc", 400), (str(10 ** 8), 413)):
            c = self.conn()
            c.request("POST", "/login", "", {"Content-Length": length})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, status)
            self.assertEqual(r.getheader("Connection"), "close", length)


# ---------- страницы: подключение, список, тексты ----------

SIX = ("vless-reality", "vless-xhttp", "hysteria2", "amneziawg", "tuic", "ss2022")


@needs_bash
class ConnectPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        for pid in SIX:
            self.env.add_protocol(pid)
        from zoolib import users
        users.bootstrap()
        users.add_user("masha", note="сестра")
        self.c.login()

    def test_connect_first_light_and_without_qr_inside(self):
        resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        main = body[body.index("<main"):]
        self.assertLess(main.index("Подключить"), main.index("Все ссылки и QR"))
        self.assertLess(main.index("Все ссылки и QR"), main.index("Профиль"))
        self.assertLess(main.index("Подключить"), main.index(">Трафик<"))
        self.assertLessEqual(len(body.encode("utf-8")), 40 * 1024,
                             "страница с 6 протоколами и 5 платформами должна быть лёгкой (30 КБ + «Если у человека не "
                             "работает» + Mac и Linux с шагами Brave, D59)")
        self.assertNotIn("<svg", body.split("Профиль")[0], "QR — отдельные картинки по требованию")
        self.assertIn('data-src="/users/masha/qr/', body)
        self.assertNotIn("нужен Xray-клиент", body)
        self.assertNotIn("Для:", body)
        self.assertIn("Быстрый старт", body)
        self.assertIn("Скопировать всё", body)
        self.assertNotIn("class=\"ptile s", body)
        self.assertRegex(body, r'class="ptile acc[1-8]"')

    def test_profile_is_short(self):
        _, body = self.c.get("/users/masha")
        profile = body[body.index(">Профиль<"):]
        self.assertNotIn(">протоколы<", profile)
        self.assertIn(">создан<", profile)
        self.users_disable()
        _, body = self.c.get("/users/masha")
        self.assertIn('class="badge muted" title="креды сохранены, доступ закрыт">отключён', body)
        self.assertNotIn("Пользователь отключён: ссылки сохранены", body)

    def test_stamp_is_skipped_where_response_is_not_a_page(self):
        self.c.post("/users", {"name": "masha"})
        _, body = self.c.get("/users/masha")
        qr = re.search(r'data-src="(/users/masha/qr/\d+\?p=[0-9a-f]{8})"', body).group(1)
        file = re.search(r'href="(/users/masha/file/[\w.-]+)"', body).group(1)
        real = stamp_mod.compute
        with mock.patch("zoolib.web.stamp.compute", side_effect=real) as comp:
            self.c.get(qr)
            self.c.get(file)
            self.c.post("/live/vless-reality")
            self.assertEqual(comp.call_count, 0)
            resp, _ = self.c.get("/users/masha")
            self.assertTrue(header(resp, "X-Zoo-Stamp"))
            self.assertEqual(comp.call_count, 1)

    def users_disable(self):
        self.c.post("/users/masha/disable")

    def test_copy_all_has_every_uri(self):
        _, body = self.c.get("/users/masha")
        self.assertNotIn('id="copy-all"', body, "ссылки не дублируются скрытым полем")
        self.assertIn('data-copy-all=".pdlg [data-copy]"', body)
        card_part, windows = body.split('<details class="card more"')
        field = r'<input type="text"[^>]* value="(vless://[^"]+)"'
        in_card, in_windows = set(re.findall(field, card_part)), re.findall(field, windows)
        self.assertEqual(len(in_windows), len(set(in_windows)))
        self.assertFalse(in_card & set(in_windows), "ссылка из «Подключить» в окнах — только кнопкой, без второго поля")
        self.assertEqual(len(in_card | set(in_windows)), len(SIX), "все ссылки есть на странице")

    def test_qr_route_needs_session_and_has_no_style(self):
        _, body = self.c.get("/users/masha")
        url = re.search(r'data-src="(/users/masha/qr/\d+\?p=[0-9a-f]{8})"', body).group(1)
        anon = Client(self.app)
        resp, _ = anon.get(url)
        self.assertEqual(resp.status, 303)
        self.assertTrue(header(resp, "Location")[0].startswith("/login"))
        with mock.patch("zoolib.qr.svg", return_value=SAMPLE_QR) as svg:
            resp, data = self.c.get(url)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/svg+xml")
        self.assertEqual(header(resp, "Cache-Control"), ["no-store"])
        self.assertTrue(data.startswith("<svg"))
        self.assertNotIn("style=", data)
        self.assertIn('stroke="#000000"', data)
        self.assertIn("vless://masha@", svg.call_args[0][0])
        for bad in ("/users/masha/qr/999", "/users/nobody/qr/0"):
            self.assertEqual(self.c.get(bad)[0].status, 404, bad)

    def test_qr_url_carries_link_tag_and_shifted_list_is_404(self):
        from zoolib.web import userviews
        _, body = self.c.get("/users/masha")
        url = re.search(r'data-src="(/users/masha/qr/\d+\?p=[0-9a-f]{8})"', body).group(1)
        base, tag = url.split("?p=")
        idx = int(base.rsplit("/", 1)[1])
        links, errs = userviews._cached_links(self.app, "masha")
        self.assertEqual(links[idx].tag, tag)
        self.assertEqual(len({ln.tag for ln in links}), len(links), "метки разных ссылок различаются")
        with mock.patch("zoolib.qr.svg", return_value=SAMPLE_QR):
            self.assertEqual(self.c.get(url)[0].status, 200)
            self.assertEqual(self.c.get(base)[0].status, 404, "без метки не отдаём")
            self.assertEqual(self.c.get(base + "?p=00000000")[0].status, 404)
            self.assertEqual(self.c.get(base + "?p=" + tag.upper())[0].status, 404)
            # между показом страницы и запросом картинки список ссылок поменялся: тот же номер — уже другая ссылка
            with mock.patch.object(userviews, "_cached_links", return_value=(links[::-1], errs)):
                self.assertEqual(self.c.get(url)[0].status, 404, "чужой QR не отдаётся")
                new = links[::-1]
                self.assertEqual(self.c.get(f"{base.rsplit('/', 1)[0]}/{len(new) - 1 - idx}?p={tag}")[0].status, 200)

    def test_qr_cache_cleared_with_links(self):
        from zoolib import qr
        qr._svg_cache[("x", 6)] = "<svg/>"
        self.app.cache_put("links:masha", ("s", ([], {})), 3600)
        self.c.post("/users/masha/disable")
        self.assertNotIn(("x", 6), qr._svg_cache)
        self.assertIsNone(self.app.cache_get("links:masha", 3600))

    def test_links_cached_and_expired_entries_dropped(self):
        self.c.get("/users/masha")
        calls = len([x for x in self.env.calls() if " links " in x])
        self.c.get("/users/masha")
        self.assertEqual(len([x for x in self.env.calls() if " links " in x]), calls, "второй раз — из кэша")
        self.app.cache_put("old", 1, 0.0)
        self.app.cache_put("new", 2, 100)
        self.assertNotIn("old", self.app._cache)

    def test_user_links_keep_order(self):
        from zoolib import users
        links, errors = users.user_links("masha")
        self.assertEqual(errors, {})
        registry = users.list_users().get("masha").protocols
        self.assertEqual([l.proto_id for l in links if l.proto_id != "allowlist"],
                         [p for p in registry for _ in range(2 if p == "amneziawg" else 1)])

    def test_list_has_chips_no_open_button(self):
        resp, body = self.c.get("/users")
        self.assertIn("6 из 6", body)
        self.assertNotIn("Открыть", body)
        self.assertNotIn(">статус<", body)
        self.assertIn("Проверить учётки", body)
        self.assertNotIn("Синхронизировать", body)
        self.assertIn("＋ Добавить пользователя", body)
        self.c.post("/users/masha/disable", {"back": "/users"})
        _, body = self.c.get("/users")
        self.assertIn('class="off"', body)
        self.assertIn(">откл.<", body)
        self.assertIn("data-confirm", body)  # отключение — с подтверждением

    def test_list_shows_missing_protocols_and_sync_only_on_mismatch(self):
        from zoolib import users
        # протокол включили после создания пользователей: в реестре его у них нет
        self.env.add_protocol("trojan-x", users=())
        resp, body = self.c.get("/users")
        from zoolib import manifests
        self.assertIn("нет: " + manifests.proto_title("trojan-x"), body)
        _, body = self.c.get("/users?verify=1")
        self.assertIn("Расхождений нет", body)
        self.assertNotIn("Синхронизировать", body)
        # а в протоколе пользователь пропал — «Синхронизировать» появляется
        (self.env.fake / "tuic.users").write_text("owner true\n", encoding="utf-8")
        _, body = self.c.get("/users?verify=1")
        self.assertIn("masha есть в реестре, но нет в протоколе", body)
        self.assertIn("Синхронизировать", body)

    def test_add_user_name_lowercased(self):
        resp, _ = self.c.post("/users", {"name": "  Petya "})
        self.assertEqual(header(resp, "Location"), ["/users/petya"])

    def test_job_page_origin(self):
        j = self.app.jobs.start("probe", "Самопроверка", [sys.executable, "-c", "print('{}')"])
        self.app.jobs.wait(j)
        _, body = self.c.get(f"/jobs/{j.id}")
        self.assertRegex(body, r'<a href="/probe" class="active" aria-current="page">')
        self.assertIn("← проверка", body)
        c = self.app.jobs.start("collect", "Снятие", [sys.executable, "-c", "pass"])
        self.app.jobs.wait(c)
        _, body = self.c.get(f"/jobs/{c.id}")
        self.assertRegex(body, r'<a href="/traffic" class="active" aria-current="page">')
        r = self.app.jobs.start("restart:x", "Перезапуск", [sys.executable, "-c", "pass"])
        self.app.jobs.wait(r)
        _, body = self.c.get(f"/jobs/{r.id}")
        self.assertRegex(body, r'<a href="/settings" class="active" aria-current="page">')


class TilesTest(unittest.TestCase):
    def test_awg_dialog_opens_on_android_file(self):
        from zoolib import protolib
        from zoolib.web.userviews import connect_tiles
        links = [protolib.Link("vpn://abc", "ключ", "amneziawg", "uri"),
                 protolib.Link("/etc/clients/masha/amneziawg.conf", "", "amneziawg", "file"),
                 protolib.Link("/etc/clients/masha/amneziawg-android.conf", "", "amneziawg", "file")]
        out = str(connect_tiles(links, [], "masha"))
        tabs = re.findall(r'<button type="button" data-tab="dlg-0-v(\d)"[^>]*>([^<]*)</button>', out)
        self.assertEqual([t_[1] for t_ in tabs], ["Файл Android", "Файл Windows, iPhone", "Ключ AmneziaVPN"])
        first = re.search(r'<div class="variant" id="dlg-0-v0">(.*?)</div>', out, re.S).group(1)
        self.assertIn("/qr/2", first, "первым — android.conf (третья ссылка)")
        self.assertIn('class="tab active">Файл Android', out)
        self.assertNotIn(' s0"', out)

    def test_variant_names_and_titles(self):
        from zoolib import protolib
        from zoolib.web.userviews import connect_tiles
        links = [protolib.Link("hysteria2://a@h:443?sni=x", "", "hysteria2"),
                 protolib.Link("hysteria2://a@h:443?obfs=salamander&obfs-password=p", "", "hysteria2"),
                 protolib.Link("hysteria2://a@h:20000,20100?sni=x", "", "hysteria2")]
        out = str(connect_tiles(links, [], "masha"))
        # Salamander — свой протокол группы: своя плитка, обычная Hysteria2 и hop остаются вкладками первой
        self.assertIn(">Ссылка</button>", out)
        self.assertIn(">Ссылка 2</button>", out)
        self.assertNotIn(">Ссылка 3</button>", out)
        for gone in ("Обычная", "Запасная", "Основные", "Запасные"):
            self.assertNotIn(gone, out, "слова из других экранов: «Ссылка», без «Основные/Запасные»")
        self.assertIn('title="Port hopping: порт меняется"', out)
        self.assertEqual(out.count('class="ptile '), 2)
        self.assertIn('ptile-name">Hysteria2 + Salamander<', out)
        self.assertNotIn("Для:", out)
        self.assertNotIn("нужен Xray-клиент", out)

    def test_tiles_are_one_list_by_priority_and_v2rayn_rules_file_is_not_a_protocol(self):
        from zoolib import clients, protolib
        from zoolib.web import clientviews
        from zoolib.web.userviews import connect_tiles
        links = [protolib.Link("vless://a@h:443?type=tcp", "", "vless-reality"),
                 protolib.Link("/etc/clients/masha/v2rayn-routing.json", "", "allowlist", "file"),
                 protolib.Link("hysteria2://a@h:443?sni=x", "", "hysteria2"),
                 protolib.Link("vless://a@h:443?type=xhttp", "", "vless-xhttp")]
        out = str(connect_tiles(links, [], "masha"))
        names = re.findall(r'class="ptile-name">([^<]*)<', out)
        self.assertEqual(names, ["Hysteria2", "VLESS XHTTP", "VLESS Vision", "Правила v2rayN"],
                         "протоколы по PRIORITY, файл правил — последним")
        protocols, files = out.split('<h3 class="sub-h">Файлы</h3>')
        self.assertIn("VLESS Vision", protocols)
        self.assertNotIn("Правила v2rayN", protocols, "файл правил не в списке протоколов")
        self.assertIn("Правила v2rayN", files)
        # импорт файла — тем же текстом, что в инструкции
        text = clientviews.rules_text(clients.load())
        self.assertIn("«Добавить набор правил» → «Импорт правил из файла»", text)
        self.assertIn(text, out)
        self.assertNotIn("Маршрутизация → Импорт из файла", out)

    def test_show_filter_keeps_only_selected_variants(self):
        from zoolib import protolib
        from zoolib.web.userviews import connect_tiles, quick_start
        links = [protolib.Link("hysteria2://a@h:443?sni=x", "", "hysteria2"),
                 protolib.Link("hysteria2://a@h:443?obfs=salamander&obfs-password=p", "", "hysteria2"),
                 protolib.Link("hysteria2://a@h:20000,20100?sni=x", "", "hysteria2")]
        only_obfs = lambda ln: ln.variant != "hysteria2"
        out = str(connect_tiles(links, [], "masha", only_obfs))
        self.assertEqual(out.count('class="ptile '), 1)
        self.assertIn("obfs=salamander", out)
        self.assertNotIn("20000", out)
        self.assertIn("/qr/1?", out, "номера QR — по полному списку ссылок")
        only_plain = lambda ln: ln.variant == "hysteria2"
        out = str(connect_tiles(links, [], "masha", only_plain))
        self.assertNotIn("salamander", out)
        self.assertIn("20000", out)
        copy_all = str(quick_start(links, "masha", only_plain))
        self.assertNotIn("salamander", copy_all)
        self.assertIsNone(connect_tiles(links, [], "masha", lambda ln: False))


# ---------- объём текста, подтверждения, ошибки ----------

HEALTHY = copy.deepcopy(FAKE_SLOW)
HEALTHY["protocols"][1].update(services={"hysteria-server.service": "active"}, listening={"udp": True}, ok=True)
HEALTHY["services"] = {**FAKE_SLOW["services"],
                       "hysteria-server.service": {"load": "loaded", "active": "active", "enabled": "enabled"}}
HEALTHY.update(problems=[], exposed=[])


class QuietPagesTest(AppTestBase):
    def test_healthy_overview_is_quiet(self):
        with mock.patch("zoolib.status.collect_slow", return_value=HEALTHY):
            self.c.login()
            _, body = self.c.get("/")
        self.assertNotIn("не слушает", body)
        self.assertNotIn('class="chip bad"', body)
        self.assertNotIn(">active<", body)
        self.assertNotIn("лишние открытые порты", body)
        self.assertNotIn("Сертификаты", body)
        self.assertNotIn(">Хост<", body)
        self.assertNotIn(">Сводка<", body)
        self.assertNotIn("Обновить", body)
        self.assertIn("443/tcp", body)

    def test_overview_alert_actions_for_failed_unit(self):
        self.c.login()
        _, body = self.c.get("/")
        self.assertIn('href="/logs?src=unit:hysteria-server.service"', body)
        self.assertIn('name="unit" value="hysteria-server.service"', body)
        self.assertIn("data-confirm=\"Перезапустить hysteria-server.service?\"", body)
        self.assertIn("443/udp", body)
        self.assertIn("не слушает", body, "при сбое чипы показываются")

    def test_all_good_badge(self):
        self._seed_run()
        with mock.patch("zoolib.status.collect_slow", return_value=HEALTHY):
            self.c.login()
            _, body = self.c.get("/")
        self.assertIn("✓ сервер работает", body)

    def test_overview_service_traffic_and_short_names(self):
        from zoolib import users
        users.bootstrap()
        now = int(time.time())
        con = traffic.connect()
        with con:
            deltas = [traffic.Delta("xray", "owner", 1000, 2000), traffic.Delta("xray", users.PROBE_USER, 5_000_000, 0)]
            traffic.store(con, deltas, {}, now)
        con.close()
        status = copy.deepcopy(FAKE_SLOW)
        status["protocols"][0].update(short="VLESS Vision", name="VLESS + REALITY + Vision")
        with mock.patch("zoolib.status.collect_slow", return_value=status):
            self.c.login()
            _, body = self.c.get("/")
        self.assertIn("Трафик пользователей", body)
        self.assertIn("Без служебного трафика пробника", body)
        self.assertIn("служебный 4.8 МБ", body)
        self.assertIn("<h3>VLESS Vision</h3>", body)   # одно название, без подсказки с длинным именем
        self.assertIn("<h3>Hysteria2</h3>", body)

    def test_overview_no_service_line_without_probe_traffic(self):
        self._seed_run()
        with mock.patch("zoolib.status.collect_slow", return_value=FAKE_SLOW):
            self.c.login()
            _, body = self.c.get("/")
        self.assertNotIn("служебный", body)

    def _seed_run(self):
        from zoolib import users
        users.bootstrap()
        now = int(time.time())
        con = traffic.connect()
        with con:
            samples = [traffic.Sample("xray", "owner", 1, 1)]
            deltas, new = traffic.compute_deltas(traffic.load_counters(con), samples, now)
            traffic.store(con, deltas, new, now)
            con.execute("INSERT OR REPLACE INTO runs VALUES (?, 1, 1, 0, 0, 0.1, '{}')", (now,))
        con.close()

    def test_auto_reboot_message(self):
        with mock.patch("zoolib.web.views.REBOOT_FLAG") as flag:
            flag.exists.return_value = True
            self.env.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "AUTO_REBOOT": "1",
                                   "AUTO_REBOOT_TIME": "04:30"})
            self.c.login()
            _, body = self.c.get("/")
        self.assertIn("Перезагрузится сам в 04:30", body)

    def test_settings_is_short(self):
        self.env.add_manifest("vless-reality")
        self.c.login()
        _, body = self.c.get("/settings")
        self.assertLessEqual(visible_words(body), 120)
        self.assertLess(body.index("Сервисы"), body.index("Обслуживание"))
        self.assertLess(body.index("Обслуживание"), body.index("Версии"))
        self.assertLess(body.index("Версии"), body.index("config.env"))
        self.assertNotIn("Сверить пользователей", body)
        self.assertIn("btn-grid", body)
        self.assertIn('class="cols maint"', body, "узкая колонка обслуживания и широкая таблица версий")
        from zoolib.web import assets
        self.assertRegex(assets.CSS, r"\.btn-grid \{[^}]*grid-template-columns: minmax\(0, 1fr\);",
                         "кнопки обслуживания — в один столбец")
        self.assertRegex(assets.CSS, r"\.cols\.maint \{ grid-template-columns: 230px minmax\(0, 1fr\)")

    def test_settings_masks_panel_path_and_copies_full_url(self):
        self.env.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "PANEL_PORT": "24680",
                               "PANEL_PATH": "supersecretpath", "ENABLE_TUIC": "1", "SSH_PORTS": "22",
                               "HY2_STATS_SECRET": "abcdef123456"})
        self.c.login()
        _, body = self.c.get("/settings")
        visible = re.sub(r"<input[^>]*>", "", body)
        self.assertIn("http://127.0.0.1:24680/•••/", visible)
        self.assertNotIn("supersecretpath", visible)
        self.assertIn('value="http://127.0.0.1:24680/supersecretpath/"', body)
        self.assertIn('data-copy="panel-url"', body)
        self.assertNotIn("abcdef123456", body)
        cfg_html = body[body.index("Настройки сервера"):]
        self.assertIn("только чтение · ключей: 7", cfg_html)
        self.assertLess(cfg_html.index("Основное"), cfg_html.index("Протоколы"))
        self.assertLess(cfg_html.index("Протоколы"), cfg_html.index("Технические"))
        self.assertLess(cfg_html.index("Протоколы"), cfg_html.index("ENABLE_TUIC"))
        self.assertLess(cfg_html.index("ENABLE_TUIC"), cfg_html.index("Технические"))
        self.assertLess(cfg_html.index("Технические"), cfg_html.index("HY2_STATS_SECRET"), "секреты — в технических")
        self.assertIn("••••", cfg_html)

    def test_restart_asks_confirmation(self):
        self.env.add_manifest("vless-reality")
        self.c.login()
        _, body = self.c.get("/settings")
        self.assertIn("оборвутся", body)  # x-ui: про VLESS и TUIC
        self.assertIn('data-confirm="Перезапустить fail2ban.service?"', body)

    def test_logout_and_reset_confirm(self):
        self.c.login()
        _, body = self.c.get("/apps")
        self.assertIn('action="/logout"', body)
        self.assertIn('data-confirm="Выйти из админки?"', body)
        self.assertRegex(body, r'data-confirm="[^"]+"[^>]*>(<input[^>]*>)*<input type="hidden" name="action" value="reset"')

    def test_error_page_hides_secrets_and_is_short(self):
        self.env.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "PANEL_PASS": "s3cretPanelPass"})
        self.c.login()
        with mock.patch("zoolib.web.views.status_data", side_effect=RuntimeError("упало: s3cretPanelPass " + "x" * 600)):
            with contextlib.redirect_stderr(io.StringIO()):
                resp, body = self.c.get("/")
        self.assertEqual(resp.status, 500)
        self.assertNotIn("s3cretPanelPass", body)
        self.assertLess(len(body), 4000)
        text = re.search(r'<p class="muted">(.*?)</p>', body).group(1)
        self.assertLessEqual(len(text), 300)

    def test_flash_errors_are_sanitized(self):
        self.env.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "PANEL_PASS": "s3cretPanelPass"})
        self.c.login()
        self.app.auth.session(self.c.cookies[SID_COOKIE]).flash("bad", "ошибка s3cretPanelPass " + "y" * 500)
        _, body = self.c.get("/users")
        self.assertNotIn("s3cretPanelPass", body)
        self.assertIn("•••", body)

    def test_traffic_empty_state_has_collect_button(self):
        self.c.login()
        _, body = self.c.get("/traffic")
        self.assertIn("Нет данных · сбор каждые 5 мин", body)
        self.assertIn("Снять сейчас", body)
        self.assertIn('name="action" value="collect"', body)

    def test_probe_page_is_short(self):
        from zoolib import probe
        from zoolib.fsutil import atomic_write_json
        atomic_write_json(probe.selftest_file(), {"generated": "2026-10-04T00:00:00+00:00", "server_ip": "10.0.0.1",
                                                 "results": [{"id": "vless-reality", "verdict": "OK", "port": 443},
                                                             {"id": "hysteria2", "verdict": "SERVER_DOWN"}]})
        self.c.login()
        _, body = self.c.get("/probe")
        self.assertLessEqual(visible_words(body), 150)
        self.assertIn("Сравнить</button>", body)
        self.assertNotIn("необязательно", body.replace('title="необязательно', ""))

    def test_traffic_page_cached_until_next_collect(self):
        self._seed_run()
        self._seed_more()
        self.c.login()
        resp, first = self.c.get("/traffic")
        self.assertEqual(resp.status, 200)
        with mock.patch.object(traffic, "report", wraps=traffic.report) as spy:
            _, again = self.c.get("/traffic")
        self.assertEqual(spy.call_count, 0, "второй раз — из кэша")
        self.assertEqual(first, again)

    def _seed_more(self):
        now = int(time.time())
        con = traffic.connect()
        with con:
            samples = [traffic.Sample("xray", "owner", 100, 100), traffic.Sample("vless-reality", "", 100, 100),
                       traffic.Sample(traffic.HOST, "", 100, 100)]
            deltas, new = traffic.compute_deltas(traffic.load_counters(con), samples, now + 1)
            traffic.store(con, deltas, new, now + 1)
        con.close()


class LiveTest(AppTestBase):
    def test_live_does_not_eat_flash(self):
        self.c.login()
        self.c.post("/users", {"name": "Bad Name!"})
        _, live = self.c.get("/traffic", headers={"X-Zoo-Live": "1"})
        self.assertNotIn("недопустимое имя", live)
        _, page = self.c.get("/users")
        self.assertIn("недопустимое имя", page, "обычный запрос получает сообщение")
        _, again = self.c.get("/users")
        self.assertNotIn("недопустимое имя", again, "и тратит его")

    def test_period_switch_also_live(self):
        # переключатель периода шлёт X-Zoo-Live (app.js), поэтому тоже не забирает сообщения
        self.assertRegex(assets.JS, r"headers: \{ 'X-Zoo-Live': '1' \}")

    def test_partial_navigation_contract(self):
        # ссылки и формы с data-swap — без перезагрузки: прокрутка и фокус остаются, без дёрганий вёрстки
        for needle in ("a[data-swap]", "nav.seg a", "form[data-draft]", "hasAttribute('data-swap')",
                       "window.scrollTo(0, y)", "new URLSearchParams(fd)", "getAttribute('action')"):
            self.assertIn(needle, assets.JS)
        self.assertIn("scrollbar-gutter: stable", assets.CSS)
        self.assertNotRegex(assets.JS, r"\.style\.|setAttribute\('style'|on(click|change|submit)=")

    def test_flash_links_and_escaping(self):
        self.c.login()
        s = self.app.auth.session(self.c.cookies[SID_COOKIE], touch=False)
        s.flash("ok", "Сохранено. Нужны:", [("a<b", "/users/a"), ("c", "/users/c")])
        s.flash("bad", "ошибка")
        _, body = self.c.get("/users")
        self.assertIn('<span class="msg">Сохранено. Нужны: <a href="/users/a">a&lt;b</a>, '
                      '<a href="/users/c">c</a> →</span>', body)
        self.assertIn('<span class="msg">ошибка</span>', body)

    def test_second_live_request_gets_304(self):
        self.c.login()
        with mock.patch("zoolib.web.stamp._now", return_value=1_000_000.0):  # окно «минуты» не меняется
            self._second_live_request_gets_304()

    def _second_live_request_gets_304(self):
        r1, b1 = self.c.get("/probe", headers={"X-Zoo-Live": "1"})
        etag = header(r1, "ETag")[0]
        self.assertRegex(etag, r'^"[0-9a-f]{20}"$')
        r2, b2 = self.c.get("/probe", headers={"X-Zoo-Live": "1", "If-None-Match": etag})
        self.assertEqual((r2.status, b2), (304, ""))
        self.assertEqual(header(r2, "ETag"), [etag])
        r3, b3 = self.c.get("/probe", headers={"X-Zoo-Live": "1", "If-None-Match": '"другой"'})
        self.assertEqual(r3.status, 200)
        self.assertEqual(b3, b1)

    def test_live_request_does_not_extend_session(self):
        self.c.login()
        s = self.app.auth.session(self.c.cookies[SID_COOKIE], touch=False)
        before = s.last
        time.sleep(0.02)
        self.c.get("/probe", headers={"X-Zoo-Live": "1"})
        self.assertEqual(s.last, before)
        self.c.get("/probe")
        self.assertGreater(s.last, before)

    def test_login_page_is_not_live_and_footer_is_short(self):
        self.c.login()
        for path in ("/", "/settings", "/users", "/apps", "/probe", "/logs", "/traffic", "/journal"):
            _, body = self.c.get(path)
            self.assertRegex(body, r'<footer>zoo [^<]*<span id="live" data-live="10" data-stamp="[0-9a-f]{12}" hidden>',
                             path)
            self.assertNotIn("Пауза", body)
        self.assertNotIn("данные обновляются при открытии", body)
        self.assertIn(">Логи<", body)
        self.assertNotIn(">Журнал<", body)
        self.c.cookies.clear()
        _, body = self.c.get("/login")
        self.assertNotIn('id="live"', body)


class StampTest(AppTestBase):
    """/api/stamp: дешёвый отпечаток данных страницы для live (только os.stat)."""

    def stamp(self, page):
        resp, body = self.c.get("/api/stamp?page=" + urllib.parse.quote(page))
        self.assertEqual(resp.status, 200, body)
        self.assertTrue(resp.content_type.startswith("text/plain"))
        self.assertRegex(body, r"^[0-9a-f]{12}$")
        return body

    def at(self, ts):
        return mock.patch("zoolib.web.stamp._now", return_value=ts)

    def test_requires_login_and_answers_401(self):
        resp, _ = self.c.get("/api/stamp?page=/")
        self.assertEqual(resp.status, 401)
        self.c.login()
        self.stamp("/")

    def test_does_not_extend_session_even_without_live_header(self):
        self.c.login()
        s = self.app.auth.session(self.c.cookies[SID_COOKIE], touch=False)
        before = s.last
        time.sleep(0.02)
        self.stamp("/users")
        self.assertEqual(s.last, before)

    def test_stable_until_data_changes(self):
        self.c.login()
        with self.at(1_000_000.0):
            a = self.stamp("/traffic")
            self.assertEqual(self.stamp("/traffic"), a)
            self.assertEqual(self.stamp("/traffic?period=7d"), a, "query не в счёт")
            traffic.connect().close()
            b = self.stamp("/traffic")
            self.assertNotEqual(b, a)
            apps = self.stamp("/apps")
            os.utime(traffic.db_path(), ns=(1, 1))
            self.assertNotEqual(self.stamp("/traffic"), b)
            self.assertEqual(self.stamp("/apps"), apps, "базе трафика нет дела до /apps")

    def test_wal_counts_but_empty_wal_does_not(self):
        self.c.login()
        with self.at(1_000_000.0):
            traffic.connect().close()
            wal = traffic.db_path().with_name(traffic.DB_NAME + "-wal")
            base = self.stamp("/traffic")
            wal.write_bytes(b"")
            self.assertEqual(self.stamp("/traffic"), base)
            wal.write_bytes(b"x" * 32)
            self.assertNotEqual(self.stamp("/traffic"), base)

    def test_users_file_and_clients_dir(self):
        self.c.login()
        with self.at(1_000_000.0):
            a = self.stamp("/users")
            Path(os.environ["ZOO_USERS_FILE"]).write_text("{}", encoding="utf-8")
            b = self.stamp("/users")
            self.assertNotEqual(a, b)
            (Path(os.environ["ZOO_CLIENTS_DIR"]) / "masha").mkdir(parents=True)
            c = self.stamp("/users")
            self.assertNotEqual(c, b)
            self.assertEqual(self.stamp("/users/"), c, "хвостовой слэш — та же страница")
            self.assertEqual(self.stamp("/users/masha"), self.stamp("/users/masha"))

    def test_time_windows(self):
        self.c.login()
        with self.at(1_000_000.0):
            root, users, traffic_, apps = (self.stamp(p) for p in ("/", "/users", "/traffic", "/apps"))
        with self.at(1_000_012.0):  # +12 с: «/» обновляется (метрики), остальные нет
            self.assertNotEqual(self.stamp("/"), root)
            self.assertEqual(self.stamp("/users"), users)
        with self.at(1_000_070.0):  # +70 с: «N мин назад» устарело
            self.assertNotEqual(self.stamp("/users"), users)
            self.assertEqual(self.stamp("/traffic"), traffic_)
            self.assertEqual(self.stamp("/apps"), apps)

    def test_pjobs_stamp_reads_no_files(self):
        self.c.login()
        with mock.patch("zoolib.web.stamp._stat", side_effect=AssertionError("файлы не нужны")):
            self.assertRegex(self.stamp("/pjobs/" + "a" * 32), r"^[0-9a-f]{12}$")

    def test_page_param_is_only_a_key(self):
        self.c.login()
        for page in ("../../etc/passwd", "/" + "a" * 5000, "", "/jobs/999999", "/users/%00", "//x"):
            self.stamp(page)

    def test_job_page_stamp_follows_job(self):
        self.c.login()
        job = self.app.jobs.start("t", "t", [sys.executable, "-c", "import time; time.sleep(0.3); print('x')"])
        before = self.stamp(f"/jobs/{job.id}")
        self.app.jobs.wait(job)
        self.assertNotEqual(self.stamp(f"/jobs/{job.id}"), before)

    def test_page_carries_its_stamp(self):
        self.c.login()
        with self.at(1_000_000.0):
            resp, body = self.c.get("/traffic")
            got = header(resp, "X-Zoo-Stamp")
            self.assertEqual(got, [self.stamp("/traffic")])
            self.assertIn(f'data-stamp="{got[0]}"', body)
            resp, _ = self.c.get("/static/app.js")
            self.assertEqual(header(resp, "X-Zoo-Stamp"), [])

    def test_no_sql_on_stamp(self):
        self.c.login()
        with mock.patch("sqlite3.connect", side_effect=AssertionError("SQL в /api/stamp")):
            for page in ("/", "/users", "/probe", "/journal", "/logs", "/settings", "/traffic", "/apps"):
                self.stamp(page)


class SwapFormsTest(AppTestBase):
    """Формы и ссылки внутри main уходят без перезагрузки (data-swap), а без JS остаются обычными."""

    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        from zoolib import users
        users.bootstrap()

    def test_user_forms_and_links(self):
        self.c.login()
        self.c.post("/users", {"name": "masha"})
        _, body = self.c.get("/users")
        forms = re.findall(r"<form [^>]*>", body)
        for f in forms:
            if 'action="/logout"' not in f and " data-get" not in f:
                self.assertIn(" data-swap", f, f)
                self.assertIn('method="post"', f)
        self.assertTrue(any('action="/users"' in f for f in forms))
        self.assertTrue(any("/disable" in f and "data-confirm" in f for f in forms), "подтверждение осталось")
        self.assertRegex(body, r'<a href="/users\?verify=1"[^>]*data-swap')
        _, page = self.c.get("/users/masha")
        self.assertRegex(page, r'<form method="post" action="/users/masha/disable"[^>]*data-swap')
        _, confirm = self.c.get("/users/masha/delete")
        self.assertRegex(confirm, r'<form method="post" action="/users/masha/delete"[^>]*data-swap')

    def test_settings_probe_logs_journal(self):
        self.c.login()
        _, body = self.c.get("/settings")
        forms = [f for f in re.findall(r"<form [^>]*>", body) if 'action="/logout"' not in f]
        self.assertTrue(forms)
        for f in forms:
            self.assertIn(" data-swap", f, f)
        _, body = self.c.get("/probe")
        for action in ("/probe/run", "/probe/compare"):
            self.assertRegex(body, rf'<form method="post" action="{action}"[^>]*data-swap')
        _, body = self.c.get("/journal")
        self.assertNotIn("all=1", body, "переключателя своих адресов в вебе нет")

    def test_post_without_js_still_redirects_with_flash(self):
        self.c.login()
        resp, _ = self.c.post("/users", {"name": "Bad Name!"})
        self.assertEqual((resp.status, header(resp, "Location")), (303, ["/users"]))
        _, page = self.c.get("/users")
        self.assertIn("недопустимое имя", page)


class DataBlockTest(AppTestBase):
    """Блок «Данные» на странице настроек (BACKLOG п. 13) и версии «Доступно» (п. 3)."""

    def setUp(self):
        super().setUp()
        from tests import test_storage as ts
        self.ts = ts
        self.logs = self.env.root / "logs"
        self.logs.mkdir()
        p = mock.patch.dict("os.environ", {"LOG_DIR": self.logs.as_posix()})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("zoolib.storage.JOURNALD_DIRS", (str(self.env.root / "no-journal"),))
        p.start()
        self.addCleanup(p.stop)
        self.c.login()

    def test_block_summary_and_sections(self):
        self.ts.fill_traffic(300, 30, 5)
        self.env.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "ZOO_DATA_LIMIT": "1G"})
        _, body = self.c.get("/settings")
        block = body[body.index("<h3>Данные</h3>"):]
        block = block[:block.index("</section>")]
        self.assertRegex(block, r"занято [\d.]+ (Б|КБ|МБ) из 1\.0 ГБ · свободно на диске")
        for title in ("Трафик", "Журнал атак", "История проб", "Логи установки"):
            self.assertIn(title, block)
        self.assertNotIn("Live-замеры", block)
        self.assertIn("хранится с", block)
        self.assertIn(datetime.fromtimestamp(self.ts.NOW - 4 * 86400).strftime("%d.%m.%Y"), block)
        self.assertEqual(block.count("storage-clear"), 1, "кнопка только у непустого раздела")
        self.assertIn('data-confirm="Очистить раздел «Трафик»?', block)
        self.assertIn('class="meter"', block)
        self.assertIn('value="1G"', block)

    def test_outside_budget_listed(self):
        prev = self.env.root / "state" / "geo" / "prev"
        prev.mkdir(parents=True)
        (prev / "geosite.dat").write_bytes(b"0" * 2048)
        _, body = self.c.get("/settings")
        self.assertIn("Вне бюджета: geo/prev (откат geo-файлов) 2.0 КБ", body)

    def test_clear_runs_as_job_after_post(self):
        self.ts.fill_traffic(300, 30, 5)
        resp, _ = self.c.post("/settings/action", {"action": "storage-clear", "section": "traffic"})
        self.assertEqual(resp.status, 303)
        job = self.app.jobs.recent(1)[0]
        self.app.jobs.wait(job, 60)
        self.assertEqual(job.rc, 0, job.stderr)
        self.assertEqual(self.ts.count(traffic.connect, "SELECT COUNT(*) FROM traffic"), 0)
        _, body = self.c.get(f"/jobs/{job.id}")
        self.assertIn("удалено записей", body)

    def test_clear_rejects_unknown_and_placeholder(self):
        for sid in ("nope", "live", ""):
            resp, _ = self.c.post("/settings/action", {"action": "storage-clear", "section": sid})
            self.assertEqual((resp.status, header(resp, "Location")), (303, ["/settings"]))
        self.assertEqual(self.app.jobs.recent(1), [])

    def test_clear_needs_csrf_and_post(self):
        resp, _ = self.c.post("/settings/action", {"action": "storage-clear", "section": "traffic"}, csrf=False)
        self.assertEqual(resp.status, 403)
        resp, _ = self.c.get("/settings/action?action=storage-clear&section=traffic")
        self.assertEqual(resp.status, 405)

    def test_limit_saved_to_config(self):
        resp, _ = self.c.post("/settings/action", {"action": "storage-limit", "limit": "2 ГБ"})
        self.assertEqual(resp.status, 303)
        self.assertEqual(config.load().get("ZOO_DATA_LIMIT"), "2G")
        _, body = self.c.get("/settings")
        self.assertIn("из 2.0 ГБ", body)
        self.assertIn('value="2G"', body)
        for bad in ("мусор", "1K", "0"):
            self.c.post("/settings/action", {"action": "storage-limit", "limit": bad})
            self.assertEqual(config.load().get("ZOO_DATA_LIMIT"), "2G", bad)

    def test_low_disk_alert_on_overview(self):
        du = {"total": 100 << 30, "free": 5 << 30, "used": 95 << 30}
        self.app.invalidate("storage-alerts")
        with mock.patch("zoolib.storage.disk", return_value=du):
            _, body = self.c.get("/")
        self.assertIn("Мало места на диске", body)
        with mock.patch("zoolib.storage.disk", return_value={**du, "free": 60 << 30}):
            self.app.invalidate("storage-alerts")
            _, body = self.c.get("/")
        self.assertNotIn("Мало места на диске", body)

    def upstream(self, **items):
        (self.env.root / "state" / "upstream.json").write_text(json.dumps(
            {"ts": 1, "items": {k: {"tag": v} for k, v in items.items()}}), encoding="utf-8")
        self.app.invalidate("upgrade")

    def test_versions_column_available(self):
        pins = self.env.root / "opt" / "scripts"
        pins.mkdir(parents=True)
        (pins / "versions.env").write_text("XUI_VERSION=v3.9.0\nHY2_VERSION=v2.12.3\n", encoding="utf-8")
        _, body = self.c.get("/settings")
        self.assertIn("доступно", body)
        self.assertIn("не проверено", body)
        self.upstream(**{"x-ui": "v99.0.0", "hysteria": "v2.12.3"})
        _, body = self.c.get("/settings")
        self.assertIn("v99.0.0 ↑", body)
        self.assertIn("обновление — через пины репозитория (bump-pins, sha256), не автоматом", body)

    def test_settings_page_makes_no_network_calls(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("сеть")):
            resp, _ = self.c.get("/settings")
        self.assertEqual(resp.status, 200)

    def test_kernel_engine_hides_amneziawg_go(self):
        self.env.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "AWG_ENGINE_ACTIVE": "kernel"})
        self.upstream(**{"amneziawg-go": "v99.0.0"})
        _, body = self.c.get("/settings")
        self.assertIn("не используется (ядро)", body)
        self.assertNotIn("v99.0.0", body)

    def test_config_groups(self):
        from zoolib.web import views
        for key, group in (("LABEL", "Основное"), ("SSH_PORTS", "Основное"), ("RU_EGRESS", "Основное"),
                           ("ENABLE_TUIC", "Протоколы"), ("HY2_PORT", "Протоколы"), ("AWG_ENGINE", "Протоколы"),
                           ("AWG_ENGINE_ACTIVE", "Технические"), ("AWG_H1", "Технические"),
                           ("AWG_JC", "Технические"), ("VLESS_PRIV", "Технические"),
                           ("HY2_PASSWORD", "Технические"), ("SOME_NEW_KEY", "Технические"),
                           ("ZOO_DATA_LIMIT", "Основное")):
            self.assertEqual(views.config_group(key), group, key)


class ServerSpeedTest(unittest.TestCase):
    def test_scope_of_checks_own_first_and_caches(self):
        from zoolib import journal
        journal._addr.cache_clear()
        with mock.patch("zoolib.journal.ipaddress.ip_address", wraps=ipaddress.ip_address) as parse:
            self.assertEqual(journal.scope_of("8.8.8.8", {"8.8.8.8"}, []), "own")
            self.assertEqual(parse.call_count, 0, "свой адрес — без разбора")
            for _ in range(3):
                self.assertEqual(journal.scope_of("8.8.4.4", set(), []), "public")
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(journal.scope_of("10.0.0.5", set(), []), "local")
        self.assertEqual(journal.scope_of("не-адрес", set(), []), "local")
        self.assertEqual(journal.scope_of("9.9.9.9", set(), [ipaddress.ip_network("9.9.9.0/24")]), "own")

    def test_status_split_and_cert_cache(self):
        from zoolib import status, system
        with ZooEnv():
            cfg = config.load()
            with mock.patch.object(system, "listening_sockets", return_value=[]), \
                    mock.patch.object(system, "ufw_active", return_value=True), \
                    mock.patch.object(system, "unit_states", return_value={}), \
                    mock.patch.object(system, "host_metrics") as host:
                slow = status.collect_slow(cfg, with_xui=False)
                self.assertNotIn("host", slow)
                host.assert_not_called()
                full = status.collect(cfg, cpu_interval=0, with_xui=False)
                self.assertIn("host", full)
        with tempfile.TemporaryDirectory() as d:
            crt = Path(d) / "c.pem"
            crt.write_text("x", encoding="utf-8")
            system._cert_cache.clear()
            with mock.patch.object(system, "cert_expiry", return_value=None) as exp:
                system.cert_info(crt)
                system.cert_info(crt)
                self.assertEqual(exp.call_count, 1)
                os.utime(crt, (1, 1))
                system.cert_info(crt)
                self.assertEqual(exp.call_count, 2, "изменился файл — читаем заново")

    def test_cpu_percent_does_not_sleep_when_called_often(self):
        from zoolib import system
        with mock.patch.object(system, "_cpu_last", (time.monotonic(), 10.0, 100.0, 12.5)), \
                mock.patch.object(system.time, "sleep") as sleep:
            self.assertEqual(system.cpu_percent(0.3), 12.5)
            sleep.assert_not_called()

    def test_logs_and_settings_do_not_collect_status(self):
        with ZooEnv() as env:
            env.add_manifest("vless-reality")
            m = mock.patch("zoolib.system.unit_states",
                           side_effect=lambda units: {u: {"load": "loaded", "active": "active"} for u in units})
            m.start()
            self.addCleanup(m.stop)
            app = App(TOKEN, config.load)
            c = Client(app)
            c.login()
            with mock.patch("zoolib.status.collect_slow", side_effect=AssertionError("статус не нужен")), \
                    mock.patch("zoolib.status.collect", side_effect=AssertionError("статус не нужен")):
                for path in ("/logs", "/settings"):
                    self.assertEqual(c.get(path)[0].status, 200, path)

    def test_logs_open_failed_unit_by_default(self):
        with ZooEnv() as env:
            env.add_manifest("vless-reality")

            def states(units):
                return {u: {"load": "loaded", "active": "failed" if u == "fail2ban.service" else "active"}
                        for u in units}
            with mock.patch("zoolib.system.unit_states", side_effect=states), \
                    mock.patch("zoolib.logread.view", return_value=logread.Chunk([logread.Line("fail2ban упал", "s=1")])):
                app = App(TOKEN, config.load)
                c = Client(app)
                c.login()
                _, body = c.get("/logs")
        self.assertIn("fail2ban упал", body)
        self.assertRegex(body, r'class="active" data-swap>fail2ban.service</a>')


if __name__ == "__main__":
    unittest.main()
