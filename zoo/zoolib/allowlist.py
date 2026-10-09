"""Приложения через VPN — allowlist (D31, ARCHITECTURE §5).

Через VPN идут только перечисленные приложения, всё остальное (в том числе российские
приложения) — мимо туннеля. Реестр /etc/vpn-setup/allowlist.json (0600):

    {"schema": 1,
     "android": ["com.brave.browser", ...],      пакеты Android → IncludedApplications в
                                                  clients/<имя>/amneziawg-android.conf
     "windows": ["brave.exe", ...],              процессы Windows → правила v2rayN
                                                  clients/<имя>/v2rayn-routing.json
     "users": {"masha": {"android": [...]}},     свой список пользователя (вместо общего)
     "groups": {"main": {"android": [...]}},     списки групп со своим списком (groups.json — источник,
     "members": {"masha": "main"},               здесь зеркало для lib.sh: её читает только allowlist.json);
                                                  порядок: свой список → список группы → общий
     "titles": {"com.example.app": "Название"}}  названия своих приложений (не из каталога), только для админки

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
TITLE_MAX = 40
LIST_MAX = 200  # приложений на платформу: защита от мусорной формы


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


def clean_title(value: Any) -> str:
    """Название своего приложения для админки: без управляющих символов, до TITLE_MAX знаков."""
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", value if isinstance(value, str) else "").strip()[:TITLE_MAX].strip()


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
    titles: dict[str, str] = field(default_factory=dict)  # id в нижнем регистре → название
    groups: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # зеркало groups.json
    members: dict[str, str] = field(default_factory=dict)  # пользователь → id группы со своим списком

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
        raw_titles = data.get("titles")
        titles = {k.lower(): clean_title(v) for k, v in (raw_titles.items() if isinstance(raw_titles, dict) else ())
                  if isinstance(k, str) and isinstance(v, str) and clean_title(v)}
        raw_groups = data.get("groups")
        groups = {str(g): {p: _clean(o[p], p) for p in PLATFORMS if isinstance(o.get(p), list)}
                  for g, o in (raw_groups.items() if isinstance(raw_groups, dict) else ()) if isinstance(o, dict)}
        raw_members = data.get("members")
        members = {str(u): g for u, g in (raw_members.items() if isinstance(raw_members, dict) else ())
                   if isinstance(g, str)}
        return cls(path, _clean(data.get("android"), "android"), _clean(data.get("windows"), "windows"),
                   users, True, titles, groups, members)

    def save(self) -> None:
        data: dict[str, Any] = {"schema": SCHEMA, "android": self.android, "windows": self.windows,
                                "users": self.users}
        if self.titles:
            data["titles"] = self.titles
        if self.groups:
            data["groups"], data["members"] = self.groups, self.members
        atomic_write_json(self.path, data)
        self.exists = True

    def common(self, platform: str) -> list[str]:
        return getattr(self, platform)

    def baseline(self, platform: str, user: str | None = None) -> list[str]:
        """Список без своего: у пользователя в группе со своим списком — список группы, иначе общий."""
        grp = self.groups.get(self.members.get(user or "", ""), {}).get(platform)
        return list(grp if grp is not None else self.common(platform))

    def effective(self, platform: str, user: str | None = None) -> list[str]:
        own = self.users.get(user or "", {}).get(platform)
        return list(own if own is not None else self.baseline(platform, user))

    def own(self, user: str) -> bool:
        return user in self.users

    def from_group(self, user: str) -> bool:
        """Пользователь без своего списка идёт по списку группы (а не общему)."""
        return self.members.get(user, "") in self.groups

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
    affected: list[str] = field(default_factory=list)  # пользователи, у которых изменился итоговый список
    resend: list[str] = field(default_factory=list)    # кому отмечено «переслать» (resend.mark)

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


def _empty_msg(platform: str) -> str:
    return (f"список {PLATFORM_TITLE[platform]} стал бы пустым: пустой список значит "
            + ("«все приложения через VPN»" if platform == "android" else "«ничего через VPN»")
            + ". Сначала добавьте другое приложение")


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
            raise AllowlistError(_empty_msg(p))
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


def _snapshot(al: Allowlist) -> dict[str, dict[str, list[str]]]:
    from . import users
    return {u.name: {p: al.effective(p, u.name) for p in PLATFORMS} for u in users.list_users().visible()}


def _mark(ch: Change, before: dict[str, dict[str, list[str]]], after: dict[str, dict[str, list[str]]]) -> None:
    """Отметить «переслать» тем, у кого список изменился там, где он действует (под блокировкой users)."""
    from . import resend, users
    reg = users.Registry.load()
    if reg.exists and (marked := resend.mark(reg, lists=resend.list_changes(before, after))):
        reg.save()
        ch.resend = marked


def normalize(platform: str, values: list[str]) -> list[str]:
    """Список из формы: только допустимые id, без повторов (без учёта регистра); id каталога — в
    написании каталога. Мусор — ошибка, а не молчаливый пропуск: человек должен увидеть."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        v = (raw or "").strip()
        if not v:
            continue
        if not valid(platform, v):
            hint = "процесс Windows (name.exe)" if platform == "windows" else "пакет Android (com.example.app)"
            raise AllowlistError(f"«{v[:60]}» не похоже на {hint}")
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(next((getattr(a, platform) for a in CATALOG
                             if getattr(a, platform) and getattr(a, platform).lower() == v.lower()), v))
    if len(out) > LIST_MAX:
        raise AllowlistError(f"слишком много приложений в списке {PLATFORM_TITLE[platform]} (больше {LIST_MAX})")
    return out


def set_lists(lists: dict[str, list[str]], user: str | None = None, titles: dict[str, str] | None = None,
              apply_now: bool = True) -> Change:
    """Админка: список целиком (обе платформы) одним действием — одна запись и одна пересборка
    файлов. Порядок id сохраняется, новые — в конец. Свой список пользователя хранит только те
    платформы, где он отличается от списка группы (или общего); совпал — снова без своего. titles: id → название
    для приложений не из каталога."""
    from . import users
    check_user(user)
    new = {p: normalize(p, lists.get(p) or []) for p in PLATFORMS}
    for p in PLATFORMS:
        if not new[p]:
            raise AllowlistError(_empty_msg(p))
    with users._lock():
        al = Allowlist.load()
        before = _snapshot(al)
        ch = Change(user)
        for p in PLATFORMS:
            cur = al.effective(p, user)
            have, want = {x.lower() for x in cur}, {x.lower() for x in new[p]}
            if have == want:
                continue
            ch.added += [(p, x) for x in new[p] if x.lower() not in have]
            ch.removed += [(p, x) for x in cur if x.lower() not in want]
            merged = [x for x in cur if x.lower() in want] + [x for x in new[p] if x.lower() not in have]
            if user is None:
                setattr(al, p, merged)
            elif want == {x.lower() for x in al.baseline(p, user)}:
                al.users.get(user, {}).pop(p, None)
            else:
                al.users.setdefault(user, {})[p] = merged
        if user in al.users and not al.users[user]:
            del al.users[user]
        old_titles = dict(al.titles)
        catalog = {getattr(a, p).lower() for a in CATALOG for p in PLATFORMS if getattr(a, p)}
        for ident, title in (titles or {}).items():
            key, title = ident.lower(), clean_title(title)
            if title and key not in catalog and any(key == x.lower() for p in PLATFORMS for x in new[p]):
                al.titles[key] = title
        used = {x.lower() for p in PLATFORMS for x in [*al.common(p), *(i for o in [*al.users.values(), *al.groups.values()]
                                                                        for i in o.get(p, []))]}
        al.titles = {k: v for k, v in al.titles.items() if k in used}
        if not (ch.added or ch.removed):
            if al.titles != old_titles:
                al.save()
            ch.message = "без изменений"
            return ch
        al.save()
        after = _snapshot(al)
        ch.affected = [n for n in after if after[n] != before.get(n)]
        _mark(ch, before, after)
        ch.message = "список изменён"
        if apply_now:
            ch.applied = _apply(ch.affected)
        return ch


def remember_titles(titles: dict[str, str]) -> None:
    """Названия своих приложений (не из каталога), которые уже есть в каком-то списке: для списка группы, который
    сохраняет groups.update, а не set_lists."""
    from . import users
    with users._lock():
        al = Allowlist.load()
        catalog = {getattr(a, p).lower() for a in CATALOG for p in PLATFORMS if getattr(a, p)}
        used = {x.lower() for p in PLATFORMS for x in [*al.common(p), *(i for o in [*al.users.values(), *al.groups.values()]
                                                                        for i in o.get(p, []))]}
        new = {k.lower(): v for k, t in titles.items() if (v := clean_title(t)) and k.lower() in used
               and k.lower() not in catalog}
        if any(al.titles.get(k) != v for k, v in new.items()):
            al.titles.update(new)
            al.save()


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
        before = _snapshot(al)
        ch = _edit(al, op, items, user, platform)
        if ch.added or ch.removed:
            al.save()
            _mark(ch, before, _snapshot(al))
            ch.message = "список изменён"
            if apply_now:
                ch.applied = _apply(None if user is None else [user])
        else:
            ch.message = "без изменений"
        return ch


def reset(user: str | None = None, apply_now: bool = True) -> Change:
    """Общий список — к пресету; пользователь — на список группы или общий (свой список удаляется)."""
    from . import users
    check_user(user)
    with users._lock():
        al = Allowlist.load()
        ch = Change(user)
        before = _snapshot(al)
        if user is None:
            d = defaults()
            al.android, al.windows = d["android"], d["windows"]
            ch.message = "сброшен на список по умолчанию"
        elif al.users.pop(user, None) is not None:
            ch.message = "сброшен на список группы" if al.from_group(user) else "сброшен на общий список"
        else:
            ch.message = "без изменений: своего списка не было"
            return ch
        al.save()
        after = _snapshot(al)
        ch.affected = [n for n in after if after[n] != before.get(n)]
        _mark(ch, before, after)
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
        gone = [al.users.pop(name, None) is not None, al.members.pop(name, None) is not None]
        if any(gone):
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
