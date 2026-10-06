"""Ядро веб-админки без сокетов: запрос → маршрут → ответ. Сервер (server.py) только
переводит HTTP в Request/Response, поэтому маршруты, вход и CSRF проверяются тестами напрямую."""

from __future__ import annotations

import hashlib
import re
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import __version__, paths, qr
from ..config import Config
from ..config import load as load_config
from . import assets, logs, stamp
from .auth import ABSOLUTE_TTL, Auth, Session, cookie, cookie_names, host_allowed, new_token, same
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
NAV = [("/", "Обзор"), ("/users", "Пользователи"), ("/groups", "Группы"), ("/apps", "Приложения"), ("/clients", "Клиенты"), ("/traffic", "Трафик"),
       ("/probe", "Проверка"), ("/journal", "Атаки"), ("/logs", "Логи"), ("/settings", "Настройки")]
MSG_MAX = 300  # ошибки на странице короткие: длинный вывод модуля — в журнал, не в браузер
LOGIN_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
ONCE_RE = re.compile(r"[A-Za-z0-9._-]{1,160}")


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
    stamp: str = ""  # отпечаток данных страницы на начало запроса (live, app.js)

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    @property
    def live(self) -> bool:
        """Фоновое обновление страницы (app.js): не тратит сообщения и не продлевает сессию."""
        return bool(self.header("x-zoo-live")) or self.path == "/api/stamp"

    def cookie_names(self) -> tuple[str, str]:
        """(сессия, форма входа): имена несут порт, cookie общие для всех портов 127.0.0.1."""
        return cookie_names(self.header("host"))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    cache: bool = False
    gz: bytes | None = None  # то же тело, сжатое заранее (статика)


def redirect(location: str, status: int = 303) -> Response:
    return Response(status, b"", headers=[("Location", location)])


def text(body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status, body.encode("utf-8"), content_type)


def clip(msg: str, limit: int = MSG_MAX) -> str:
    return msg if len(msg) <= limit else msg[:limit - 1].rstrip() + "…"


class App:
    def __init__(self, token: str, cfg_loader: Callable[[], Config] = load_config,
                 extra_hosts: set[str] | None = None) -> None:
        from . import allowviews, clientviews, groupviews, journalviews, probeviews, protoviews, userviews, views  # маршруты ссылаются на App: импорт здесь
        self.auth = Auth(token, store=paths.state_dir() / "web-sessions.json")
        self.jobs = Jobs()
        self.cfg_loader = cfg_loader
        self.extra_hosts = extra_hosts or set()
        self._cache: dict[Any, tuple[float, Any, float]] = {}  # ключ → (когда, значение, ttl)
        self._cache_lock = threading.Lock()
        self.seen_jobs: set[str] | None = None  # завершённые задачи вкл/выкл, о которых кэш статуса уже знает
        name = r"(?P<name>[a-z0-9][a-z0-9_-]{0,31})"
        gid = r"(?P<gid>[a-z0-9][a-z0-9_-]{0,31})"
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
            ("GET", rf"/users/{name}/file/(?P<fname>[A-Za-z0-9._-]{{1,64}})", userviews.user_file, True),
            ("GET", rf"/users/{name}/qr/(?P<idx>\d+)", userviews.user_qr, True),
            ("GET", r"/connect/new", groupviews.connect_page, True),
            ("POST", r"/connect/new", groupviews.connect_post, True),
            ("GET", r"/connect/done", groupviews.connect_done, True),
            ("GET", r"/groups", groupviews.groups_list, True),
            ("GET", rf"/groups/{gid}", groupviews.group_get, True),
            ("POST", rf"/groups/{gid}", groupviews.group_save, True),
            ("POST", rf"/groups/{gid}/members", groupviews.group_members, True),
            ("POST", rf"/groups/{gid}/move", groupviews.group_move, True),
            ("POST", rf"/groups/{gid}/delete", groupviews.group_delete, True),
            ("GET", r"/apps", allowviews.apps_page, True),
            ("POST", r"/apps", allowviews.apps_post, True),
            ("GET", r"/clients", clientviews.clients_page, True),
            ("GET", r"/traffic", views.traffic_page, True),
            ("GET", r"/probe", views.probe_page, True),
            ("POST", r"/probe/run", views.probe_run, True),
            ("POST", r"/probe/compare", views.probe_compare, True),
            ("POST", r"/probe/history/(?P<rid>\d{1,9})/delete", probeviews.run_delete, True),
            ("GET", r"/journal", journalviews.journal_page, True),
            ("GET", r"/logs", views.logs_page, True),
            ("GET", r"/settings", views.settings_page, True),
            ("POST", r"/settings/action", views.settings_action, True),
            ("GET", r"/jobs/(?P<job>\d+)", views.job_page, True),
            ("GET", r"/api/stamp", self.stamp_api, False),
            ("POST", r"/live/(?P<proto>[a-z0-9][a-z0-9-]{0,39})", protoviews.live_request, True),
            ("POST", r"/protocols/(?P<proto>[a-z0-9][a-z0-9-]{0,39})/(?P<action>enable|disable)",
             protoviews.proto_toggle, True),
            ("GET", r"/pjobs/(?P<jid>[0-9a-f]{32})", protoviews.job_page, True),
        ]:
            self.routes.append((method, re.compile(pattern + r"/?\Z"), handler, need_auth))

    # ---------- общее ----------

    def cfg(self) -> Config:
        return self.cfg_loader()

    def cached(self, key: Any, ttl: float, fn: Callable[[], Any]) -> Any:
        """Дорогие сводки (status.collect) не чаще раза в ttl секунд."""
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        self.cache_put(key, value, ttl)
        return value

    def cache_get(self, key: Any, ttl: float) -> Any:
        with self._cache_lock:
            hit = self._cache.get(key)
            return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    def cache_put(self, key: Any, value: Any, ttl: float) -> None:
        """Запись в кэш; заодно уходят все просроченные (иначе ключи со временем копятся)."""
        now = time.monotonic()
        with self._cache_lock:
            for k in [k for k, (ts, _, life) in self._cache.items() if now - ts >= life]:
                del self._cache[k]
            self._cache[key] = (now, value, ttl)

    def invalidate(self, *keys: str) -> None:
        """Без аргументов — весь кэш. «status» — обе половины сводки: быстрая и медленная."""
        with self._cache_lock:
            if not keys:
                self._cache.clear()
            for k in keys:
                for key in (("status", "status-slow") if k == "status" else (k,)):
                    self._cache.pop(key, None)

    def invalidate_links(self, name: str | None = None) -> None:
        """Ссылки и QR пользователя (или всех): после удаления, отключения, смены списка приложений."""
        with self._cache_lock:
            for k in [k for k in self._cache if isinstance(k, str) and k.startswith("links:")
                      and (name is None or k == f"links:{name}")]:
                del self._cache[k]
        qr.clear_cache()

    # ---------- обработка ----------

    def handle(self, req: Request) -> Response:
        resp = self._handle(req)
        if (resp.status == 200 and req.method in ("GET", "HEAD") and resp.body
                and resp.content_type.startswith("text/html")):
            etag = '"' + hashlib.sha1(resp.body).hexdigest()[:20] + '"'
            if req.header("if-none-match") == etag:
                resp = Response(304, b"", resp.content_type)
            resp.headers.append(("ETag", etag))
        if req.stamp and resp.content_type.startswith("text/html"):
            resp.headers.append(("X-Zoo-Stamp", req.stamp))  # app.js берёт отсюда исходную точку для live
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
        req.session = self.auth.session(req.cookies.get(req.cookie_names()[0]), touch=not req.live)
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
            if need_auth:
                req.stamp = stamp.compute(self, req.path)  # до чтения данных: изменение во время рендера не теряется
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
            r = Response(200, assets.CSS.encode(), "text/css; charset=utf-8", cache=True, gz=assets.CSS_GZ)
        elif name == "/static/app.js":
            r = Response(200, assets.JS.encode(), "text/javascript; charset=utf-8", cache=True, gz=assets.JS_GZ)
        else:
            return text("нет такого файла", 404)
        r.headers.append(("Cache-Control", "public, max-age=86400, immutable"))
        return r

    def stamp_api(self, app: "App", req: Request) -> Response:
        """Отпечаток данных страницы для live: несколько os.stat, без SQL и рендера; сессию не продлевает."""
        if req.session is None:
            return text("Сессия истекла", 401)
        return text(stamp.compute(self, req.query.get("page", "/")[:200]))

    # ---------- вход ----------

    def login_page(self, app: "App", req: Request, error: str = "", status: int = 200) -> Response:
        if req.session and not error:
            return redirect(_safe_next(req.query.get("next")))
        login_cookie = req.cookie_names()[1]
        have = req.cookies.get(login_cookie, "")
        nonce = have if LOGIN_NONCE_RE.fullmatch(have) else new_token(16)  # вторая вкладка не ломает первую
        once = req.query.get("once") or req.form.get("once") or ""
        once = once if ONCE_RE.fullmatch(once) else ""
        # автоотправка — только на чистой странице: после ошибки форма отправлялась бы по кругу
        autosubmit = bool(once) and not error and status == 200
        locked = self.auth.locked()
        msg = error or (f"Подождите {int(locked) + 1} с." if locked and not once else "")
        try:
            cfg = self.cfg()
            who = "zoo " + (cfg.get("LABEL") or cfg.get("SERVER_IP") or "server")
        except Exception:  # страница входа открывается и при битом config.env
            who = "zoo"
        form = t("form",
                 t("input", type="hidden", name="lc", value=nonce),
                 t("input", type="hidden", name="next", value=_safe_next(req.query.get("next") or req.form.get("next"))),
                 t("input", type="hidden", name="once", value=once) if once else None,
                 # скрытое имя: менеджер паролей сохраняет токен как пару «zoo <метка> / токен»
                 t("input", type="text", name="username", value=who, autocomplete="username", hidden=True,
                   readonly=True),
                 t("div", t("label", "Токен", for_="token"),
                   t("input", type="password", name="token", id="token", autocomplete="current-password",
                     required=not once, autofocus=not once), class_="field"),
                 t("button", "Войти", type="submit", class_="btn primary"),
                 method="post", action="/login", data_autosubmit=True if autosubmit else None)
        body = t("div", t("section",
                          t("h1", "vpn-zoo"),
                          t("ul", t("li", t("span", "!", class_="ico"), msg), class_="alerts") if msg else None,
                          t("p", "Входим по ссылке…", class_="muted small") if autosubmit else None,
                          form,
                          t("p", "Токен: ", t("code", "sudo zoo web --link"), class_="muted small"),
                          class_="card"), class_="login")
        resp = self.render(req, "Вход", body, bare=True, status=status)
        resp.headers.append(("Set-Cookie", cookie(login_cookie, nonce, max_age=1800, path="/login")))
        return resp

    def login_post(self, app: "App", req: Request) -> Response:
        sid_cookie, login_cookie = req.cookie_names()
        if not same(req.form.get("lc", ""), req.cookies.get(login_cookie, "")):
            return self.login_page(app, req, "Форма входа устарела — попробуйте ещё раз.", 400)
        s = None
        once = req.form.get("once", "")
        if once:
            s = self.auth.login_once(once)  # ссылка работает и во время блокировки
        if s is None:
            req.form.pop("once", None)  # ссылка не подошла: в форме её больше нет, ждём токен
            req.query.pop("once", None)
            if self.auth.locked():
                return self.login_page(app, req, "", 429)
            token = req.form.get("token", "")
            if once and not token:
                time.sleep(0.5)
                return self.login_page(app, req, "Ссылка устарела. Новая: sudo zoo web --link", 401)
            s = self.auth.login(token)
            if s is None:
                time.sleep(0.5)  # перебор вслепую дороже
                return self.login_page(app, req, "Неверный токен.", 401)
        resp = redirect(_safe_next(req.form.get("next")))
        resp.headers.append(("Set-Cookie", cookie(sid_cookie, s.sid, max_age=ABSOLUTE_TTL)))
        return resp

    def logout(self, app: "App", req: Request) -> Response:
        sid_cookie = req.cookie_names()[0]
        self.auth.logout(req.cookies.get(sid_cookie))
        resp = redirect("/login")
        resp.headers.append(("Set-Cookie", cookie(sid_cookie, "", max_age=0)))
        return resp

    # ---------- страницы ----------

    def safe_msg(self, msg: Any) -> str:
        """Сообщение об ошибке для браузера: без секретов конфигурации, не длиннее MSG_MAX."""
        try:
            cfg = self.cfg()
        except Exception:
            cfg = None
        return clip(logs.sanitize(str(msg), cfg))

    def error(self, req: Request, status: int, title: str, message: str) -> Response:
        body = t("section", t("h1", title), t("p", self.safe_msg(message), class_="muted"),
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
            # фоновое обновление сообщений не забирает: их увидит человек, а не app.js
            flashes = [] if req.live or not req.session else req.session.pop_flashes()
            nav = t("nav", [t("a", label, href=href, class_="active" if href == active else None,
                              aria_current="page" if href == active else None) for href, label in NAV],
                    class_="nav", aria_label="Разделы")
            logout = t("form", csrf_input(req.session.csrf if req.session else ""),
                       t("button", "Выйти", type="submit", class_="btn small"), method="post", action="/logout",
                       data_confirm="Выйти из админки?")
            header = t("header", t("div", t("a", "vpn-zoo", t("span", cfg_label) if cfg_label else None,
                                            href="/", class_="brand"), nav, logout, class_="top-inner"),
                       class_="top")
            flash = t("ul", [t("li", t("span", {"ok": "✓", "bad": "✕"}.get(k, "!"), class_="ico"),
                               t("span", self.safe_msg(msg) if k in ("bad", "warn") else msg,
                                 [" ", [[", " if i else None, t("a", label, href=href)]
                                        for i, (label, href) in enumerate(links)], " →"] if links else None,
                                 class_="msg"), class_=k)
                             for k, msg, links in flashes], class_="alerts flash") if flashes else None
            # без JS live нет: подпись показывает app.js
            footer = t("footer", f"zoo {__version__}",
                       t("span", id="live", data_live="10", data_stamp=req.stamp or None, hidden=True))
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
