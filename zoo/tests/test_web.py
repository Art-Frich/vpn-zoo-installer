import http.client
import os
import re
import sys
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from tests.helpers import ZooEnv, needs_bash
from zoolib import config, traffic
from zoolib import web as web_mod
from zoolib.web import auth, charts, logs
from zoolib.web.app import App, Request, _safe_next
from zoolib.web.html import Markup, t
from zoolib.web.jobs import Jobs
from zoolib.web.server import make_server
from zoolib.web.userviews import clean_qr_svg

TOKEN = "test-token-0123456789abcdef"
HOST = "127.0.0.1:8999"

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
                 "zoo-web.service": {"load": "loaded", "active": "active", "enabled": "enabled"}},
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
        resp, _ = self.post("/login", {"token": TOKEN, "lc": self.cookies.get(auth.LOGIN_COOKIE, "")}, csrf=False)
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
        self.assertFalse(a.csrf_ok(s, s.csrf[:-1] + "x"))
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
    def test_setup_and_info(self):
        with ZooEnv():
            with mock.patch("zoolib.system.listening_sockets", return_value=[]):
                web_mod.setup(config.load())
            cfg = config.load()
            port = cfg.int("ZOO_WEB_PORT")
            self.assertTrue(20000 <= port <= 65535)
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


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        patcher = mock.patch("zoolib.status.collect", return_value=FAKE_STATUS)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        resp, _ = self.c.post("/users", {"name": "x"})
        self.assertEqual(resp.status, 401)

    def test_login_flow(self):
        resp, body = self.c.get("/login")
        self.assertEqual(resp.status, 200)
        self.assertIn('type="password"', body)
        resp, body = self.c.post("/login", {"token": TOKEN}, csrf=False)
        self.assertEqual(resp.status, 400, "без double-submit cookie вход не принимается")
        self.c.get("/login")
        resp, body = self.c.post("/login", {"token": "nope", "lc": self.c.cookies[auth.LOGIN_COOKIE]}, csrf=False)
        self.assertEqual(resp.status, 401)
        self.assertIn("Неверный токен", body)
        self.c.get("/login?next=/traffic")
        resp, _ = self.c.post("/login", {"token": TOKEN, "lc": self.c.cookies[auth.LOGIN_COOKIE],
                                         "next": "/traffic"}, csrf=False)
        self.assertEqual(resp.status, 303)
        self.assertEqual(header(resp, "Location"), ["/traffic"])
        sid = [v for v in header(resp, "Set-Cookie") if v.startswith("zoo_sid=")][0]
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
        with mock.patch("zoolib.qr.svg", return_value=SAMPLE_QR):
            resp, body = self.c.get("/users/masha")
        self.assertEqual(resp.status, 200)
        self.assertIn("пользователь создан", body)
        self.assertIn("сестра &lt;3", body)
        self.assertIn('value="vless://masha@', body)
        self.assertIn("data-copy=", body)
        self.assertIn('stroke="#000000"', body)
        self.assertNotIn("style=", body)
        m = re.search(r'href="(/users/masha/file/\d+)"', body)
        self.assertIsNotNone(m, "ссылка на скачивание .conf")
        resp, data = self.c.get(m.group(1))
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", header(resp, "Content-Disposition")[0])
        self.assertIn("[Interface]", data)
        resp, _ = self.c.get("/users/masha/file/99")
        self.assertEqual(resp.status, 404)
        # модуль вернул путь вне clients/masha/ или не .conf — не отдаём
        from zoolib import protolib
        for bad in (self.env.etc / "config.env", self.env.etc / "clients" / "masha" / "amneziawg.key",
                    self.env.etc / "clients" / "owner" / "amneziawg.conf"):
            fake = [protolib.Link(str(bad), "", "amneziawg", "file")]
            with mock.patch("zoolib.users.user_links", return_value=(fake, {})):
                resp, _ = self.c.get("/users/masha/file/0")
            self.assertEqual(resp.status, 404, bad)

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

    def test_owner_not_deletable(self):
        _, body = self.c.get("/users/owner/delete")
        self.assertIn("owner не удаляется", body)
        self.c.post("/users/owner/delete")
        self.assertIn("owner", self.env.proto_users("amneziawg"))


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


class SocketServerTest(unittest.TestCase):
    """Настоящий сокет: сервер на 127.0.0.1:0, запросы http.client."""

    def setUp(self):
        self.env = ZooEnv().__enter__()
        m = mock.patch("zoolib.status.collect", return_value=FAKE_STATUS)
        m.start()
        self.addCleanup(m.stop)
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
        sid = [c for c in r.headers.get_all("Set-Cookie") if c.startswith("zoo_sid=")][0].split(";")[0]
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


if __name__ == "__main__":
    unittest.main()
