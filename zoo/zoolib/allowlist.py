"""Приложения через VPN — allowlist (D31, ARCHITECTURE §5).

Через VPN идут только перечисленные приложения, всё остальное (в том числе российские
приложения) — мимо туннеля. Реестр /etc/vpn-setup/allowlist.json (0600):

    {"schema": 1,
     "android": ["com.brave.browser", ...],      пакеты Android → IncludedApplications в
                                                  clients/<имя>/amneziawg-android.conf
     "windows": ["brave.exe", ...],              процессы Windows → правила v2rayN
                                                  clients/<имя>/v2rayn-routing.json
     "users": {"masha": {"android": [...]}}}     свой список пользователя (вместо общего)

Пока реестра нет, действует пресет scripts/allowlist-default.json (его же читает lib.sh).
Android-вариант .conf собирает модуль AmneziaWG (zoo_allowlist в lib.sh), правила v2rayN —
этот модуль. После каждого изменения apply() пересобирает и то и другое.

Пустой список не допускается: пустой IncludedApplications в AmneziaWG значит «все
приложения через VPN», а v2rayN без правила процессов не пустит в VPN ничего.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths, protolib
from .fsutil import atomic_write_json, read_json

SCHEMA = 1
PLATFORMS = ("android", "windows")
PLATFORM_TITLE = {"android": "Android (AmneziaWG, WG Tunnel)", "windows": "Windows (v2rayN)"}
# те же выражения, что в zoo_allowlist (lib.sh): значение уходит в .conf и JSON как есть
ANDROID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
WINDOWS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,63}\.[eE][xX][eE]")
V2RAYN_FILE = "v2rayn-routing.json"
V2RAYN_PROTO = "allowlist"  # proto_id ссылки на файл v2rayN в zoo links и админке
V2RAYN_LABEL = "v2rayN (Windows): через VPN только выбранные программы"


class AllowlistError(Exception):
    """Неверный ввод или состояние реестра."""


@dataclass(frozen=True)
class App:
    key: str
    title: str
    android: str | None
    windows: str | None
    note: str = ""


# Пакеты Android сверены со страницами Google Play 04.10.2026. Имена процессов Windows:
# brave.exe, Telegram.exe, Discord.exe, Signal.exe — известные имена; ChatGPT.exe и
# claude.exe не сверены на устройстве (Xray сравнивает имена с учётом регистра)
CATALOG: tuple[App, ...] = (
    App("brave", "Brave — браузер для VPN", "com.brave.browser", "brave.exe",
        "из Google Play или GitHub, не из RuStore (там версия 2024 года)"),
    App("telegram", "Telegram", "org.telegram.messenger", "Telegram.exe",
        "версия из Google Play; APK с telegram.org — другой пакет (telegram-web)"),
    App("telegram-web", "Telegram (APK с telegram.org)", "org.telegram.messenger.web", None,
        "в Google Play его нет; пакет сверен по APKMirror"),
    App("telegram-x", "Telegram X", "org.thunderdog.challegram", None),
    App("youtube", "YouTube", "com.google.android.youtube", None,
        "предустановлен почти везде, кроме Huawei без сервисов Google"),
    App("instagram", "Instagram", "com.instagram.android", None),
    App("whatsapp", "WhatsApp", "com.whatsapp", None),
    App("x", "X (Twitter)", "com.twitter.android", None),
    App("discord", "Discord", "com.discord", "Discord.exe"),
    App("signal", "Signal", "org.thoughtcrime.securesms", "Signal.exe"),
    App("chatgpt", "ChatGPT", "com.openai.chatgpt", "ChatGPT.exe", "имя процесса Windows не сверено"),
    App("claude", "Claude", "com.anthropic.claude", "claude.exe", "имя процесса Windows не сверено"),
)
BY_KEY = {a.key: a for a in CATALOG}


def title_of(platform: str, ident: str) -> str:
    """Название приложения из каталога или пусто."""
    for a in CATALOG:
        if getattr(a, platform) and getattr(a, platform).lower() == ident.lower():
            return a.title
    return ""


def valid(platform: str, ident: str) -> bool:
    rx = ANDROID_RE if platform == "android" else WINDOWS_RE
    return bool(rx.fullmatch(ident or ""))


def _clean(values: Any, platform: str) -> list[str]:
    out: list[str] = []
    for v in values if isinstance(values, list) else []:
        if isinstance(v, str) and valid(platform, v) and v not in out:
            out.append(v)
    return out


def defaults() -> dict[str, list[str]]:
    f = paths.allowlist_default_file()
    try:
        data = read_json(f)
    except (OSError, ValueError) as e:
        raise AllowlistError(f"нет пресета {f}: {e}") from None
    return {p: _clean(data.get(p), p) for p in PLATFORMS}


@dataclass
class Allowlist:
    path: Path
    android: list[str] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    users: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    exists: bool = False

    @classmethod
    def load(cls) -> "Allowlist":
        path = paths.allowlist_file()
        # пустой файл — как его отсутствие: так же решает zoo_allowlist в lib.sh ([ -s ])
        if not path.exists() or path.stat().st_size == 0:
            d = defaults()
            return cls(path, d["android"], d["windows"], {}, False)
        try:
            data = read_json(path)
        except (OSError, ValueError) as e:
            raise AllowlistError(f"{path}: {e}") from None
        if not isinstance(data, dict):
            raise AllowlistError(f"{path}: ожидается JSON-объект")
        users: dict[str, dict[str, list[str]]] = {}
        raw_users = data.get("users")
        for name, o in (raw_users.items() if isinstance(raw_users, dict) else ()):
            if isinstance(o, dict):
                # не список — как нет своего списка (общий), так же читает lib.sh
                own = {p: _clean(o[p], p) for p in PLATFORMS if isinstance(o.get(p), list)}
                if own:
                    users[str(name)] = own
        return cls(path, _clean(data.get("android"), "android"), _clean(data.get("windows"), "windows"),
                   users, True)

    def save(self) -> None:
        atomic_write_json(self.path, {"schema": SCHEMA, "android": self.android, "windows": self.windows,
                                      "users": self.users})
        self.exists = True

    def common(self, platform: str) -> list[str]:
        return getattr(self, platform)

    def effective(self, platform: str, user: str | None = None) -> list[str]:
        own = self.users.get(user or "", {}).get(platform)
        return list(own if own is not None else self.common(platform))

    def own(self, user: str) -> bool:
        return user in self.users

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "exists": self.exists, "android": self.android,
                "windows": self.windows, "users": self.users}


# ---------- разбор ввода ----------

def resolve(items: list[str], platform: str | None = None) -> list[tuple[str, str]]:
    """«brave», «com.whatsapp», «Telegram.exe» → [(платформа, id)]. Ключ каталога даёт id
    для всех платформ, где приложение известно (или только для platform)."""
    out: list[tuple[str, str]] = []
    for raw in items:
        item = (raw or "").strip()
        app = BY_KEY.get(item.lower())
        if app:
            found = [(p, getattr(app, p)) for p in PLATFORMS
                     if getattr(app, p) and (platform is None or p == platform)]
            if not found:
                raise AllowlistError(f"у «{item}» нет версии для {platform}")
            out += found
            continue
        guess = "windows" if item.lower().endswith(".exe") else "android"
        if platform and guess != platform:
            raise AllowlistError(f"«{item}» не похоже на {'процесс .exe' if platform == 'windows' else 'пакет Android'}")
        if not valid(guess, item):
            keys = ", ".join(a.key for a in CATALOG)
            raise AllowlistError(f"«{item}»: не ключ каталога ({keys}), не пакет Android (com.example.app) "
                                 "и не процесс Windows (name.exe)")
        out.append((guess, item))
    return out


# ---------- изменения (под блокировкой users._lock в вызывающем коде) ----------

@dataclass
class Change:
    user: str | None
    added: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    unchanged: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""
    applied: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        f = lambda xs: [{"platform": p, "id": i} for p, i in xs]  # noqa: E731
        return {"user": self.user, "added": f(self.added), "removed": f(self.removed),
                "unchanged": f(self.unchanged), "message": self.message, "applied": self.applied}


def check_user(user: str | None) -> None:
    """Свой список — только у обычного пользователя из реестра (у zoo-probe клиента нет)."""
    if user is None:
        return
    from . import users
    reg = users.list_users()
    if reg.exists:
        if reg.require(user).system:
            raise users.UserError(f"{user} — служебный пользователь пробника, своего списка у него нет")
    else:
        users.validate_name(user)


def _edit(al: Allowlist, op: str, items: list[str], user: str | None, platform: str | None) -> Change:
    pairs = resolve(items, platform)
    ch = Change(user)
    lists = {p: al.effective(p, user) for p in PLATFORMS}
    for p, ident in pairs:
        cur = lists[p]
        present = next((x for x in cur if x.lower() == ident.lower()), None)
        if op == "add":
            if present is None:
                cur.append(ident)
                ch.added.append((p, ident))
            else:
                ch.unchanged.append((p, ident))
        elif present is not None:
            cur.remove(present)
            ch.removed.append((p, present))
        else:
            ch.unchanged.append((p, ident))
    for p in PLATFORMS:
        if not lists[p]:
            raise AllowlistError(f"список {PLATFORM_TITLE[p]} стал бы пустым: пустой список значит "
                                 + ("«все приложения через VPN»" if p == "android" else "«ничего через VPN»")
                                 + ". Сначала добавьте другое приложение")
    if ch.added or ch.removed:
        touched = {p for p, _ in ch.added + ch.removed}
        if user is None:
            for p in touched:
                setattr(al, p, lists[p])
        else:
            own = al.users.setdefault(user, {})
            for p in touched:
                own[p] = lists[p]
    return ch


def change(op: str, items: list[str], user: str | None = None, platform: str | None = None,
           apply_now: bool = True) -> Change:
    """zoo allow add|del: изменить общий список или свой список пользователя и пересобрать файлы."""
    if op not in ("add", "del"):
        raise AllowlistError(f"неизвестная операция {op}")
    if not items:
        raise AllowlistError("не указано ни одного приложения")
    from . import users
    check_user(user)
    with users._lock():
        al = Allowlist.load()
        ch = _edit(al, op, items, user, platform)
        if ch.added or ch.removed:
            al.save()
            ch.message = "список изменён"
            if apply_now:
                ch.applied = _apply(None if user is None else [user])
        else:
            ch.message = "без изменений"
        return ch


def reset(user: str | None = None, apply_now: bool = True) -> Change:
    """Общий список — к пресету; пользователь — на общий список (свой список удаляется)."""
    from . import users
    check_user(user)
    with users._lock():
        al = Allowlist.load()
        ch = Change(user)
        if user is None:
            d = defaults()
            al.android, al.windows = d["android"], d["windows"]
            ch.message = "сброшен на пресет по умолчанию"
        elif al.users.pop(user, None) is not None:
            ch.message = "сброшен на общий список"
        else:
            ch.message = "без изменений: своего списка не было"
            return ch
        al.save()
        if apply_now:
            ch.applied = _apply(None if user is None else [user])
        return ch


def ensure_file() -> bool:
    """Создать реестр из пресета (zoo setup, фаза 09). True — создан."""
    al = Allowlist.load()
    if al.exists:
        return False
    al.save()
    return True


# ---------- файлы клиентов ----------

def v2rayn_rules(processes: list[str]) -> list[dict[str, Any]]:
    """Набор правил v2rayN 7.25 («Импорт правил из файла»): список RulesItem. Правила идут
    по порядку, первое совпавшее решает. RU-адреса — напрямую и для разрешённых программ
    (браузер под VPN не покажет российскому сайту адрес сервера), разрешённые программы —
    в VPN, остальное — напрямую. Только domain:/geoip: без geosite-категорий: их набор
    зависит от geo-файлов, выбранных в v2rayN, а неизвестная категория роняет ядро."""
    return [
        {"remarks": "Локальная сеть напрямую", "outboundTag": "direct", "ip": ["geoip:private"]},
        {"remarks": "РФ напрямую: домены", "outboundTag": "direct",
         "domain": ["domain:ru", "domain:su", "domain:xn--p1ai"]},
        {"remarks": "РФ напрямую: IP", "outboundTag": "direct", "ip": ["geoip:ru"]},
        {"remarks": "Через VPN: " + ", ".join(processes), "outboundTag": "proxy", "process": list(processes)},
        {"remarks": "Всё остальное напрямую", "outboundTag": "direct", "port": "0-65535"},
    ]


def user_file(name: str) -> Path:
    return paths.clients_dir() / name / V2RAYN_FILE


def write_user_files(name: str, al: Allowlist | None = None) -> Path | None:
    """Правила v2rayN пользователя. Android-вариант .conf пишет модуль AmneziaWG."""
    al = al or Allowlist.load()
    procs = al.effective("windows", name)
    f = user_file(name)
    if not procs:
        f.unlink(missing_ok=True)
        return None
    for d in (paths.clients_dir(), f.parent):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(f, v2rayn_rules(procs))
    return f


def forget_user(name: str) -> None:
    """Удаление пользователя (под блокировкой users): свой список и файл v2rayN."""
    try:
        al = Allowlist.load()
        if al.users.pop(name, None) is not None:
            al.save()
    except AllowlistError:
        pass
    user_file(name).unlink(missing_ok=True)


def _apply(names: list[str] | None = None, awg: bool = True) -> dict[str, Any]:
    """Пересобрать файлы v2rayN и Android-варианты AmneziaWG. names=None — всех пользователей."""
    from . import users
    al = Allowlist.load()
    reg = users.list_users()
    targets = [u.name for u in reg.users if not u.system] if names is None else names
    written, errors = [], {}
    for n in targets:
        try:
            p = write_user_files(n, al)
            if p:
                written.append(str(p))
        except OSError as e:
            errors[n] = str(e)
    result: dict[str, Any] = {"v2rayn": written, "errors": errors, "amneziawg": "пропущен"}
    if awg:
        managed, skipped = users.managed_protocols(["amneziawg"])
        if "amneziawg" in managed:
            try:
                # модуль сам перечитывает список и пересобирает файлы всех пользователей
                protolib.manifest_refresh("amneziawg")
                result["amneziawg"] = "пересобран"
            except protolib.ProtoError as e:
                result["amneziawg"] = "ошибка: " + e.short()
        else:
            result["amneziawg"] = "пропущен: " + skipped.get("amneziawg", "нет протокола")
    return result


def apply(awg: bool = True) -> dict[str, Any]:
    """zoo allow apply (фаза 99, после обновления): пересобрать файлы всех пользователей."""
    from . import users
    with users._lock():
        return _apply(None, awg)


def links_for(name: str) -> list[protolib.Link]:
    f = user_file(name)
    return [protolib.Link(str(f), V2RAYN_LABEL, V2RAYN_PROTO, "file")] if f.is_file() else []
