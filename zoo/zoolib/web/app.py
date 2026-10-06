"""Ядро веб-админки без сокетов: запрос → маршрут → ответ. Сервер (server.py) только
переводит HTTP в Request/Response, поэтому маршруты, вход и CSRF проверяются тестами напрямую."""

from __future__ import annotations

import re
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import __version__
from ..config import Config
from ..config import load as load_config
from . import assets
from .auth import LOGIN_COOKIE, SESSION_COOKIE, Auth, Session, cookie, host_allowed, new_token, same
from .html import Markup, csrf_input, t
from .jobs import Jobs

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = [
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
]
NAV = [("/", "Обзор"), ("/users", "Пользователи"), ("/apps", "Приложения"), ("/traffic", "Трафик"),
       ("/probe", "Проверка"), ("/journal", "Атаки"), ("/logs", "Журнал"), ("/settings", "Настройки")]
# Страницы без форм ввода: обновляются сами (app.js подменяет <main> раз в 10 с)
LIVE_PAGES = {"/", "/traffic", "/journal"}


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    multi: dict[str, list[str]] = field(default_factory=dict)  # все значения полей формы
    headers: dict[str, str] = field(default_factory=dict)  # имена в нижнем регистре
    cookies: dict[str, str] = field(default_factory=dict)
    session: Session | None = None

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    cache: bool = False


def redirect(location: str, status: int = 303) -> Response:
    return Response(status, b"", headers=[("Location", location)])


def text(body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status, body.encode("utf-8"), content_type)


class App:
    def __init__(self, token: str, cfg_loader: Callable[[], Config] = load_config,
                 extra_hosts: set[str] | None = None) -> None:
        from . import allowviews, journalviews, userviews, views  # маршруты ссылаются на App: импорт здесь
        self.auth = Auth(token)
        self.jobs = Jobs()
        self.cfg_loader = cfg_loader
        self.extra_hosts = extra_hosts or set()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        name = r"(?P<name>[a-z0-9][a-z0-9_-]{0,31})"
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Response], bool]] = []
        for method, pattern, handler, need_auth in [
            ("GET", r"/login", self.login_page, False),
            ("POST", r"/login", self.login_post, False),
            ("POST", r"/logout", self.logout, True),
            ("GET", r"/", views.overview, True),
            ("GET", r"/users", userviews.users_list, True),
            ("POST", r"/users", userviews.user_add, True),
            ("POST", r"/users/sync", userviews.users_sync, True),
            ("GET", rf"/users/{name}", userviews.user_page, True),
            ("POST", rf"/users/{name}/(?P<action>enable|disable)", userviews.user_toggle, True),
            ("GET", rf"/users/{name}/delete", userviews.user_delete_confirm, True),
            ("POST", rf"/users/{name}/delete", userviews.user_delete, True),
            ("GET", rf"/users/{name}/file/(?P<idx>\d+)", userviews.user_file, True),
            ("GET", r"/apps", allowviews.apps_page, True),
            ("POST", r"/apps", allowviews.apps_post, True),
            ("GET", r"/traffic", views.traffic_page, True),
            ("GET", r"/probe", views.probe_page, True),
            ("POST", r"/probe/run", views.probe_run, True),
            ("POST", r"/probe/compare", views.probe_compare, True),
            ("GET", r"/journal", journalviews.journal_page, True),
            ("GET", r"/logs", views.logs_page, True),
            ("GET", r"/settings", views.settings_page, True),
            ("POST", r"/settings/action", views.settings_action, True),
            ("GET", r"/jobs/(?P<job>\d+)", views.job_page, True),
        ]:
            self.routes.append((method, re.compile(pattern + r"/?\Z"), handler, need_auth))

    # ---------- общее ----------

    def cfg(self) -> Config:
        return self.cfg_loader()

    def cached(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        """Дорогие сводки (status.collect) не чаще раза в ttl секунд."""
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def invalidate(self, *keys: str) -> None:
        with self._cache_lock:
            for k in keys or list(self._cache):
                self._cache.pop(k, None)

    # ---------- обработка ----------

    def handle(self, req: Request) -> Response:
        resp = self._handle(req)
        resp.headers += SECURITY_HEADERS
        if not resp.cache:
            resp.headers.append(("Cache-Control", "no-store"))
        return resp

    def _handle(self, req: Request) -> Response:
        if not host_allowed(req.header("host"), self.extra_hosts):
            return text("Неверный заголовок Host: админка доступна только как 127.0.0.1 / localhost "
                        "через ssh-туннель.", 421)
        if req.method in ("GET", "HEAD") and req.path.startswith("/static/"):
            return self.static(req.path)
        if req.path == "/healthz":
            return text("ok")
        req.session = self.auth.session(req.cookies.get(SESSION_COOKIE))
        allowed: list[str] = []
        for method, rx, handler, need_auth in self.routes:
            m = rx.match(req.path)
            if not m:
                continue
            if method != req.method and not (method == "GET" and req.method == "HEAD"):
                allowed.append(method)
                continue
            if need_auth and req.session is None:
                if req.method in ("GET", "HEAD"):
                    return redirect("/login?" + urllib.parse.urlencode({"next": req.path}))
                return self.error(req, 401, "Сессия истекла", "Войдите снова и повторите действие.")
            if req.method == "POST":
                bad = self._post_guard(req, need_auth)
                if bad:
                    return bad
            try:
                return handler(self, req, **m.groupdict())
            except Exception as e:  # страница с ошибкой лучше обрыва соединения
                traceback.print_exc(file=sys.stderr)
                return self.error(req, 500, "Внутренняя ошибка", f"{type(e).__name__}: {e}")
        if allowed:
            return Response(405, b"", headers=[("Allow", ", ".join(sorted(set(allowed))))])
        return self.error(req, 404, "Страница не найдена", "Такой страницы нет.")

    def _post_guard(self, req: Request, need_auth: bool) -> Response | None:
        """POST: Origin (если прислан) — тот же хост; CSRF-токен сессии."""
        origin = req.header("origin")
        if origin and origin != "null":
            host = urllib.parse.urlsplit(origin).netloc
            if host != req.header("host"):
                return self.error(req, 403, "Запрос отклонён", "Форма отправлена с другого сайта.")
        if need_auth and req.session and not Auth.csrf_ok(req.session, req.form.get("csrf")):
            return self.error(req, 403, "Форма устарела",
                              "Токен формы не совпал (страница открыта давно или из другой вкладки). "
                              "Обновите страницу и повторите.")
        return None

    def static(self, path: str) -> Response:
        name = path.split("?", 1)[0]
        if name == "/static/app.css":
            r = Response(200, assets.CSS.encode(), "text/css; charset=utf-8", cache=True)
        elif name == "/static/app.js":
            r = Response(200, assets.JS.encode(), "text/javascript; charset=utf-8", cache=True)
        else:
            return text("нет такого файла", 404)
        r.headers.append(("Cache-Control", "public, max-age=86400, immutable"))
        return r

    # ---------- вход ----------

    def login_page(self, app: "App", req: Request, error: str = "", status: int = 200) -> Response:
        if req.session and not error:
            return redirect("/")
        nonce = new_token(16)
        locked = self.auth.locked()
        msg = error or (f"Слишком много неудачных попыток. Вход закрыт ещё {int(locked) + 1} с." if locked else "")
        form = t("form",
                 t("input", type="hidden", name="lc", value=nonce),
                 t("input", type="hidden", name="next", value=_safe_next(req.query.get("next") or req.form.get("next"))),
                 t("div", t("label", "Токен администратора", for_="token"),
                   t("input", type="password", name="token", id="token", autocomplete="current-password",
                     required=True, autofocus=True), class_="field"),
                 t("button", "Войти", type="submit", class_="btn primary"),
                 method="post", action="/login")
        body = t("div", t("section",
                          t("h1", "vpn-zoo"),
                          t("p", "Админка сервера. Токен — в CREDENTIALS.md или: ", t("code", "sudo zoo web --info"),
                            class_="muted small"),
                          t("ul", t("li", t("span", "!", class_="ico"), msg), class_="alerts") if msg else None,
                          form, class_="card"), class_="login")
        resp = self.render(req, "Вход", body, bare=True, status=status)
        resp.headers.append(("Set-Cookie", cookie(LOGIN_COOKIE, nonce, max_age=1800, path="/login")))
        return resp

    def login_post(self, app: "App", req: Request) -> Response:
        if not same(req.form.get("lc", ""), req.cookies.get(LOGIN_COOKIE, "")):
            return self.login_page(app, req, "Форма входа устарела — попробуйте ещё раз.", 400)
        if self.auth.locked():
            return self.login_page(app, req, "", 429)
        s = self.auth.login(req.form.get("token", ""))
        if s is None:
            time.sleep(0.5)  # перебор вслепую дороже
            return self.login_page(app, req, "Неверный токен.", 401)
        resp = redirect(_safe_next(req.form.get("next")))
        resp.headers.append(("Set-Cookie", cookie(SESSION_COOKIE, s.sid)))
        resp.headers.append(("Set-Cookie", cookie(LOGIN_COOKIE, "", max_age=0, path="/login")))
        return resp

    def logout(self, app: "App", req: Request) -> Response:
        self.auth.logout(req.cookies.get(SESSION_COOKIE))
        resp = redirect("/login")
        resp.headers.append(("Set-Cookie", cookie(SESSION_COOKIE, "", max_age=0)))
        return resp

    # ---------- страницы ----------

    def error(self, req: Request, status: int, title: str, message: str) -> Response:
        body = t("section", t("h1", title), t("p", message, class_="muted"),
                 t("p", t("a", "← на главную", href="/")), class_="card")
        return self.render(req, title, body, bare=req.session is None, status=status)

    def render(self, req: Request, title: str, body: Any, active: str = "", status: int = 200,
               bare: bool = False, refresh: int | None = None) -> Response:
        cfg_label = ""
        if not bare:
            cfg = self.cfg()
            cfg_label = " · ".join(x for x in (cfg.get("LABEL"), cfg.get("SERVER_IP")) if x)
        head = [
            t("meta", charset="utf-8"),
            t("meta", name="viewport", content="width=device-width, initial-scale=1"),
            t("meta", name="color-scheme", content="light dark"),
            t("meta", name="robots", content="noindex, nofollow"),
            t("meta", http_equiv="refresh", content=str(refresh)) if refresh else None,
            t("title", f"{title} — vpn-zoo"),
            t("link", rel="icon", href="data:,"),
            t("link", rel="stylesheet", href=f"/static/app.css?v={assets.CSS_VERSION}"),
            t("script", src=f"/static/app.js?v={assets.JS_VERSION}", defer=True),
        ]
        if bare:
            content = body
        else:
            flashes = req.session.pop_flashes() if req.session else []
            nav = t("nav", [t("a", label, href=href, class_="active" if href == active else None,
                              aria_current="page" if href == active else None) for href, label in NAV],
                    class_="nav", aria_label="Разделы")
            logout = t("form", csrf_input(req.session.csrf if req.session else ""),
                       t("button", "Выйти", type="submit", class_="btn small"), method="post", action="/logout")
            header = t("header", t("div", t("a", "vpn-zoo", t("span", cfg_label) if cfg_label else None,
                                            href="/", class_="brand"), nav, logout, class_="top-inner"),
                       class_="top")
            flash = t("ul", [t("li", t("span", {"ok": "✓", "bad": "✕"}.get(k, "!"), class_="ico"), msg, class_=k)
                             for k, msg in flashes], class_="alerts flash") if flashes else None
            live = active in LIVE_PAGES
            footer = t("footer", f"zoo {__version__} · ",
                       t("span", "live: обновляется каждые 10 с", id="live", data_live="10") if live
                       else t("span", "данные обновляются при открытии страницы"))
            content = [header, t("main", flash, body), footer]
        doc = Markup("<!doctype html>") + t("html", t("head", head), t("body", content), lang="ru")
        return Response(status, doc.encode("utf-8"))


def _safe_next(value: str | None) -> str:
    """Только локальный путь: «/users», не «//evil», не «https://…» и без управляющих символов
    (браузер выкидывает \\t и \\n из URL: «/\\t/evil» стал бы «//evil»; \\r\\n в Location — подмена заголовков)."""
    if (value and value.startswith("/") and not value.startswith("//") and "\\" not in value
            and all(" " < ch != "\x7f" for ch in value)):
        return value
    return "/"
