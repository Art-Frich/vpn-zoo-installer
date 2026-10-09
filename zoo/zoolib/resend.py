"""Кому что переслать после изменений (D57).

Отметка живёт в users.json (User.resend): keys — новые ключи, all — другой набор протоколов или приложений
(сообщение целиком), apps:<платформа> — сменился список «через VPN». Что именно переслать по списку, решает
приложение на устройстве человека (каталог clients.json): список зашит в наш файл (per_app=config — AmneziaWG,
WG Tunnel) — новый файл или QR; правила из нашего файла (rules — v2rayN) — новый файл правил; список отмечают в
самом приложении (ui — Happ, v2rayNG, Hiddify, AmneziaVPN) — пересылать нечего, человек отмечает сам; через VPN идёт
всё устройство или всё, кроме российского (via ≠ apps — iPhone, AmneziaVPN на Windows), — список там не действует.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import clients as clientcat
from . import groups, users

FILE, RULES, MANUAL, NONE = "file", "rules", "manual", "none"
EFFECT_LIST = {FILE: "список из нашего файла", RULES: "список из файла правил",
               MANUAL: "приложения отмечает сам человек", NONE: "список не действует"}
EFFECT_SEND = {FILE: "новый файл или QR", RULES: "новый файл правил", MANUAL: "написать, что отметить",
               NONE: "ничего"}
# что сказать человеку вместе с новым файлом: иначе импорт добавит второе подключение рядом со старым
EFFECT_DROP = {FILE: "старый туннель в «{app}» удалить, новый файл импортировать",
               RULES: "в «{app}» старый набор правил удалить, новый импортировать и сделать активным"}
KIND_TEXT = {"keys": "новые ключи — сообщение целиком (старые подключения удалить)",
             "all": "другой набор — сообщение целиком (старые подключения удалить)"}


def effect(cat: clientcat.Catalog, client: dict[str, Any], platform: str) -> str:
    """Как список «через VPN» действует в приложении на платформе: file, rules, manual или none."""
    if cat.via(client, platform) != "apps":
        return NONE
    return {"config": FILE, "rules": RULES}.get(client.get("per_app", ""), MANUAL)


@dataclass
class Item:
    platform: str
    client: dict[str, Any]
    effect: str


def apps_of(cat: clientcat.Catalog, user: users.User | None, g: groups.Group | None,
            platforms: set[str] | None = None) -> list[Item]:
    """Приложения человека (или группы, user=None) по его устройствам — из набора группы."""
    if g is None:
        return []
    devs = groups.devices_of(user, g) if user is not None else None
    out = []
    for plat, ids in g.clients.items():
        if (devs is not None and plat not in devs) or (platforms is not None and plat not in platforms):
            continue
        out += [Item(plat, c, effect(cat, c, plat)) for cid in ids if (c := cat.client(cid))]
    return out


def list_changes(before: dict[str, dict[str, list[str]]], after: dict[str, dict[str, list[str]]]) -> dict[str, set[str]]:
    """{логин: платформы, где итоговый список «через VPN» изменился}."""
    out: dict[str, set[str]] = {}
    for n, lists in after.items():
        old = before.get(n, {})
        plats = {p for p, ids in lists.items() if {x.lower() for x in ids} != {x.lower() for x in old.get(p, [])}}
        if plats:
            out[n] = plats
    return out


def mark(reg: users.Registry, full: list[str] | tuple[str, ...] = (), lists: dict[str, set[str]] | None = None,
         gs: groups.Groups | None = None) -> list[str]:
    """Отметить в загруженном реестре (сохраняет вызывающий): full — сообщение целиком, lists — {логин: платформы со
    сменившимся списком}. По спискам отмечается только тот, у кого есть что переслать или что отметить: у кого на этих
    устройствах список не действует (iPhone), отметки нет."""
    kinds: dict[str, list[str]] = {n: ["all"] for n in full}
    if lists:
        try:
            cat: clientcat.Catalog | None = clientcat.load()
        except clientcat.ClientsError:
            cat = None
        gs = gs or groups.Groups.load()
        for n, plats in lists.items():
            u = reg.get(n)
            if n in kinds or u is None:
                continue
            g = gs.get(u.group)
            if cat is None or g is None:
                kinds[n] = [f"apps:{p}" for p in sorted(plats)]
                continue
            act = {i.platform for i in apps_of(cat, u, g, plats) if i.effect != NONE}
            if act:
                kinds[n] = [f"apps:{p}" for p in sorted(act)]
    return users.mark_resend(reg, kinds)


def describe(cat: clientcat.Catalog | None, user: users.User, g: groups.Group | None) -> list[str]:
    """Что переслать человеку — строками: «новые ключи — сообщение целиком» или по приложениям
    «Android · «AmneziaWG»: новый файл или QR»."""
    full = next((k for k in users.RESEND_FULL if k in user.resend), None)
    if full:
        return [KIND_TEXT[full]]
    plats = {k.partition(":")[2] for k in user.resend if k.startswith("apps:")}
    if not plats:
        return []
    if cat is None:
        return ["список «через VPN» изменился"]
    out = [f"{cat.platforms.get(i.platform, i.platform)} · «{i.client['name']}»: {EFFECT_SEND[i.effect]}"
           + (f" ({EFFECT_DROP[i.effect].replace('{app}', i.client['name'])})" if i.effect in EFFECT_DROP else "")
           for i in apps_of(cat, user, g, plats) if i.effect != NONE]
    return out or ["список «через VPN» изменился"]


def update_line(cat: clientcat.Catalog | None, user: users.User, g: groups.Group | None) -> str:
    """Первая строка сообщения взамен старого (новые ключи, другой набор): в каких приложениях удалить старые
    подключения. Отметки «сообщение целиком» нет — пусто."""
    full = next((k for k in users.RESEND_FULL if k in user.resend), None)
    if full is None or cat is None:
        return ""
    names = list(dict.fromkeys(f"«{i.client['name']}»" for i in apps_of(cat, user, g)))
    apps = ", ".join(names) if names else "приложении VPN"
    return cat.raw["update"][full].replace("{apps}", apps)


def pending(reg: users.Registry) -> list[users.User]:
    return [u for u in reg.visible() if u.resend]
