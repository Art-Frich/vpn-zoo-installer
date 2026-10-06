"""Вход по токену, сессии, CSRF и проверка Host для веб-админки.

Токен админки — ZOO_WEB_TOKEN из config.env. После входа браузер получает cookie сессии
(HttpOnly, SameSite=Strict, Max-Age 30 дней). Сессии лежат в файле web-sessions.json (0600):
только sha256(sid) и отпечаток токена, поэтому рестарт админки вход не сбрасывает, а смена
токена (--new-token) гасит все сессии. Каждая форма POST несёт CSRF-токен своей сессии;
форма входа — double-submit cookie. Вход без поиска токена — одноразовая ссылка
`/login?once=n.e.s` (подпись HMAC от токена, живёт 3 минуты, `zoo web --link`).
Host проверяется по списку loopback-имён: защита от DNS rebinding.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..fsutil import atomic_write_json

SESSION_COOKIE = "zoo_sid"
LOGIN_COOKIE = "zoo_login"
IDLE_TTL = 14 * 86400
ABSOLUTE_TTL = 30 * 86400
MAX_SESSIONS = 32
DISK_TOUCH = 600  # last сессии пишется на диск не чаще, чем раз в 10 минут
ONCE_TTL = 180
# после FAIL_LIMIT неудачных входов за FAIL_WINDOW вход закрыт на LOCKOUT секунд
FAIL_LIMIT, FAIL_WINDOW, LOCKOUT = 50, 300, 300

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def host_port(host_header: str | None) -> str:
    """Порт из Host: cookie не различают порты 127.0.0.1, поэтому имя cookie несёт порт."""
    h = (host_header or "").strip().lower()
    m = re.search(r"(?::(\d{1,5}))?$", h.rsplit("]", 1)[-1])
    return (m.group(1) if m else "") or "80"


def cookie_names(host_header: str | None) -> tuple[str, str]:
    """(cookie сессии, cookie формы входа) для порта, на котором открыта админка."""
    port = host_port(host_header)
    return f"{SESSION_COOKIE}_{port}", f"{LOGIN_COOKIE}_{port}"


def host_allowed(host_header: str | None, extra: set[str] | None = None) -> bool:
    """Host: 127.0.0.1:PORT / localhost / [::1] (порт любой: туннель ssh -L может быть на другом)."""
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith("["):
        name = h.split("]", 1)[0] + "]"
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return name in LOOPBACK_HOSTS or name in (extra or set())


def sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def token_fp(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _once_sig(token: str, n: str, e: str) -> str:
    return hmac.new(token.encode(), f"{n}.{e}".encode(), hashlib.sha256).hexdigest()[:32]


def make_once(token: str, now: float | None = None) -> str:
    """Значение для /login?once=: n.e.s — случайный номер, срок (unix) и подпись."""
    n = secrets.token_urlsafe(9)
    e = str(int((time.time() if now is None else now) + ONCE_TTL))
    return f"{n}.{e}.{_once_sig(token, n, e)}"


@dataclass
class Session:
    sid: str
    csrf: str
    created: float
    last: float
    flashes: list[tuple[str, str]] = field(default_factory=list)  # в памяти, на диск не пишутся

    def flash(self, kind: str, text: str) -> None:
        """kind: ok | warn | bad | info"""
        self.flashes.append((kind, text))

    def pop_flashes(self) -> list[tuple[str, str]]:
        out, self.flashes = self.flashes, []
        return out


class Auth:
    def __init__(self, token: str, clock=time.time, store: Path | None = None) -> None:
        if not token or len(token) < 16:
            raise ValueError("токен админки короче 16 символов")
        self.token = token
        self.clock = clock
        self.store = Path(store) if store else None
        self._sessions: dict[str, Session] = {}  # ключ — sha256(sid)
        self._saved: dict[str, float] = {}  # last, который уже лежит на диске
        self._once: dict[str, int] = {}  # использованные номера ссылок → срок
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._lock = threading.Lock()
        self._load()

    # ---------- хранилище ----------

    def _load(self) -> None:
        if self.store is None:
            return
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("fp") != token_fp(self.token):
            return  # токен сменили: старые сессии не действуют
        now = self.clock()
        # использованные номера ссылок: иначе после рестарта та же ссылка входила бы второй раз
        for n, e in (data.get("o") or {}).items():
            if isinstance(e, int) and e >= now:
                self._once[str(n)] = e
        for key, v in (data.get("s") or {}).items():
            try:
                s = Session("", str(v["csrf"]), float(v["created"]), float(v["last"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not self._expired(s, now):
                self._sessions[key] = s
                self._saved[key] = s.last

    def _save(self) -> None:
        if self.store is None:
            return
        data = {"fp": token_fp(self.token),
                "s": {k: {"csrf": s.csrf, "created": s.created, "last": s.last} for k, s in self._sessions.items()},
                "o": self._once}
        try:
            atomic_write_json(self.store, data, 0o600)
        except OSError as e:
            print(f"zoo-web: сессии не записаны ({self.store}): {e}", file=sys.stderr)
            return
        self._saved = {k: s.last for k, s in self._sessions.items()}

    @staticmethod
    def _expired(s: Session, now: float) -> bool:
        return now - s.last > IDLE_TTL or now - s.created > ABSOLUTE_TTL

    # ---------- вход ----------

    def locked(self) -> float:
        """Сколько секунд ещё закрыт вход (0 — открыт)."""
        return max(0.0, self._locked_until - self.clock())

    def _new_session(self, now: float) -> Session:
        for key, s in list(self._sessions.items()):
            if self._expired(s, now):
                del self._sessions[key]
        if len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].last)
            del self._sessions[oldest]
        s = Session(new_token(32), new_token(24), now, now)
        self._sessions[sid_hash(s.sid)] = s
        self._save()
        return s

    def login(self, token: str) -> Session | None:
        now = self.clock()
        with self._lock:
            if now < self._locked_until:
                return None
            if not same(token.strip(), self.token):
                self._fails = [x for x in self._fails if now - x < FAIL_WINDOW] + [now]
                if len(self._fails) >= FAIL_LIMIT:
                    self._locked_until = now + LOCKOUT
                    self._fails = []
                return None
            self._fails = []
            return self._new_session(now)

    def login_once(self, value: str) -> Session | None:
        """Вход по одноразовой ссылке n.e.s: подпись, срок, однократность. Блокировку не учитывает
        (подпись не подобрать, а владельцу, которого закрыли, ссылка и нужна)."""
        parts = (value or "").strip().split(".")
        if len(parts) != 3 or not re.fullmatch(r"\d{1,12}", parts[1]) or not 0 < len(parts[0]) <= 64:
            return None
        n, e, sig = parts
        if not same(sig, _once_sig(self.token, n, e)):
            return None
        now = self.clock()
        if int(e) < now:
            return None
        with self._lock:
            self._once = {k: v for k, v in self._once.items() if v >= now}
            if n in self._once:
                return None
            self._once[n] = int(e)
            return self._new_session(now)

    def logout(self, sid: str | None) -> None:
        with self._lock:
            if sid and self._sessions.pop(sid_hash(sid), None) is not None:
                self._save()

    def session(self, sid: str | None, touch: bool = True) -> Session | None:
        """touch=False — запрос фона (live): простой сессии он не продлевает."""
        if not sid:
            return None
        now = self.clock()
        key = sid_hash(sid)
        with self._lock:
            s = self._sessions.get(key)
            if s is None:
                return None
            if self._expired(s, now):
                del self._sessions[key]
                self._save()
                return None
            s.sid = sid
            if touch:
                s.last = now
                if now - self._saved.get(key, 0.0) >= DISK_TOUCH:
                    self._save()
            return s

    # ---------- CSRF ----------

    @staticmethod
    def csrf_ok(session: Session, submitted: str | None) -> bool:
        return same(submitted or "", session.csrf)


def cookie(name: str, value: str, max_age: int | None = None, path: str = "/") -> str:
    """Set-Cookie: без Secure — админка идёт по http через ssh-туннель на loopback."""
    parts = [f"{name}={value}", f"Path={path}", "HttpOnly", "SameSite=Strict"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def parse_cookies(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k and k not in out:
            out[k] = v
    return out
