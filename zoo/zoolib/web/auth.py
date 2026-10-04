"""Вход по токену, сессии, CSRF и проверка Host для веб-админки.

Токен админки — ZOO_WEB_TOKEN из config.env. После входа браузер получает cookie сессии
(HttpOnly, SameSite=Strict); сессии живут в памяти процесса: рестарт zoo-web = новый вход.
Каждая форма POST несёт CSRF-токен своей сессии; форма входа — double-submit cookie.
Host проверяется по списку loopback-имён: защита от DNS rebinding.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "zoo_sid"
LOGIN_COOKIE = "zoo_login"
IDLE_TTL = 8 * 3600
ABSOLUTE_TTL = 24 * 3600
MAX_SESSIONS = 32
# после FAIL_LIMIT неудачных входов за FAIL_WINDOW вход закрыт на LOCKOUT секунд
FAIL_LIMIT, FAIL_WINDOW, LOCKOUT = 10, 300, 300

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


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


@dataclass
class Session:
    sid: str
    csrf: str
    created: float
    last: float
    flashes: list[tuple[str, str]] = field(default_factory=list)

    def flash(self, kind: str, text: str) -> None:
        """kind: ok | warn | bad | info"""
        self.flashes.append((kind, text))

    def pop_flashes(self) -> list[tuple[str, str]]:
        out, self.flashes = self.flashes, []
        return out


class Auth:
    def __init__(self, token: str, clock=time.time) -> None:
        if not token or len(token) < 16:
            raise ValueError("токен админки короче 16 символов")
        self.token = token
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._lock = threading.Lock()

    # ---------- вход ----------

    def locked(self) -> float:
        """Сколько секунд ещё закрыт вход (0 — открыт)."""
        return max(0.0, self._locked_until - self.clock())

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
            self._gc(now)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.last)
                self._sessions.pop(oldest.sid, None)
            s = Session(new_token(32), new_token(24), now, now)
            self._sessions[s.sid] = s
            return s

    def logout(self, sid: str | None) -> None:
        with self._lock:
            if sid:
                self._sessions.pop(sid, None)

    def session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        now = self.clock()
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if now - s.last > IDLE_TTL or now - s.created > ABSOLUTE_TTL:
                self._sessions.pop(sid, None)
                return None
            s.last = now
            return s

    def _gc(self, now: float) -> None:
        for sid, s in list(self._sessions.items()):
            if now - s.last > IDLE_TTL or now - s.created > ABSOLUTE_TTL:
                del self._sessions[sid]

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
