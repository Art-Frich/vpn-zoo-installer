"""Кому что переслать после изменений (D57, D61).

Отметка живёт в users.json (User.resend, формат — users.RESEND_RE): keys — новые ключи, all — другой набор целиком;
по устройствам — что на нём поменялось (новое устройство вместо потерянного, другое приложение, ещё один ключ, другие
ключи в том же приложении, список «через VPN»). По отметкам строятся строки «Кому переслать» и первая строка сообщения
для каждого устройства: «удалите старые подключения» — только там, где они правда есть, «поставьте заново» — на новом
телефоне, «добавьте только новый ключ» — когда старые работают.

Список «через VPN» (apps:<платформа>): что переслать, решает приложение на устройстве (каталог clients.json): список
зашит в наш файл (per_app=config — AmneziaWG, WG Tunnel) — новый файл или QR; правила из нашего файла (rules — v2rayN)
— новый файл правил; список отмечают в самом приложении (ui — Happ, v2rayNG, Hiddify, AmneziaVPN) — человек отмечает
сам; через VPN идёт всё устройство или всё, кроме российского (via ≠ apps — iPhone, AmneziaVPN на Windows), — список
там не действует.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
DEVICE_WORD = {"android": ("телефон", "телефона"), "ios": ("телефон", "телефона")}
COMPUTER = ("компьютер", "компьютера")


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


View = dict[str, tuple[tuple[str, tuple[str, ...]], ...]]   # groups.member_view: устройство → (приложение, его протоколы)


def view_kinds(old: View, new: View) -> list[str]:
    """Что поменялось у человека на каждом его устройстве (сравнение groups.member_view до и после правки)."""
    out: list[str] = []
    for plat in dict.fromkeys([*new, *old]):
        o, n = dict(old.get(plat, ())), dict(new.get(plat, ()))
        if o == n or not n:
            continue
        if not o:
            out.append(f"new:{plat}")
            continue
        out += [f"drop:{plat}:{cid}" for cid in o if cid not in n]
        out += [f"app:{plat}:{cid}" for cid in n if cid not in o]
        for cid in n:
            if cid not in o or set(n[cid]) == set(o[cid]):
                continue
            if set(n[cid]) > set(o[cid]):
                out += [f"add:{plat}:{p}" for p in n[cid] if p not in o[cid]]
            else:
                out.append(f"upd:{plat}:{cid}")
    return out


def mark(reg: users.Registry, full: list[str] | tuple[str, ...] = (), lists: dict[str, set[str]] | None = None,
         gs: groups.Groups | None = None, kinds: dict[str, list[str]] | None = None) -> list[str]:
    """Отметить в загруженном реестре (сохраняет вызывающий): full — сообщение целиком, kinds — {логин: отметки по
    устройствам (view_kinds)}, lists — {логин: платформы со сменившимся списком}. По спискам отмечается только тот, у
    кого есть что переслать или что отметить: у кого на этих устройствах список не действует (iPhone), отметки нет."""
    out: dict[str, list[str]] = {n: ["all"] for n in full}
    for n, ks in (kinds or {}).items():
        if ks and n not in out:
            out[n] = list(ks)
    if lists:
        try:
            cat: clientcat.Catalog | None = clientcat.load()
        except clientcat.ClientsError:
            cat = None
        gs = gs or groups.Groups.load()
        for n, plats in lists.items():
            u = reg.get(n)
            if u is None:
                continue
            g = gs.get(u.group)
            if cat is None or g is None:
                act = set(plats)
            else:
                act = {i.platform for i in apps_of(cat, u, g, plats) if i.effect != NONE}
            if act:
                out.setdefault(n, []).extend(f"apps:{p}" for p in sorted(act))
    return users.mark_resend(reg, out)


# ---------- что у человека отмечено ----------

@dataclass
class Marks:
    full: str = ""                                             # keys | all
    mods: set[str] = field(default_factory=set)                # keys:<модуль>
    lost: set[str] = field(default_factory=set)
    new: set[str] = field(default_factory=set)
    app: dict[str, list[str]] = field(default_factory=dict)
    drop: dict[str, list[str]] = field(default_factory=dict)
    upd: dict[str, list[str]] = field(default_factory=dict)
    add: dict[str, list[str]] = field(default_factory=dict)
    lists: set[str] = field(default_factory=set)


def marks_of(user: users.User) -> Marks:
    m = Marks()
    for k in user.resend:
        head, _, rest = k.partition(":")
        if not rest:
            m.full = m.full or (k if k in users.RESEND_FULL else "")
            continue
        plat, _, x = rest.partition(":")
        if head == "keys":
            m.mods.add(rest)
        elif head == "lost":
            m.lost.add(rest)
        elif head == "new":
            m.new.add(rest)
        elif head == "apps":
            m.lists.add(rest)
        elif head in ("app", "drop", "upd", "add") and x:
            getattr(m, head).setdefault(plat, []).append(x)
    if "keys" in user.resend:
        m.full = "keys"
    return m


def _is_lost(m: Marks, plat: str, devices: list[str]) -> bool:
    """Это устройство заменили новым: то самое или (новый телефон другой — Android ↔ iPhone) телефон вместо телефона."""
    if plat in m.lost:
        return True
    return plat in groups.PHONES and any(x in groups.PHONES and x not in devices for x in m.lost)


def device_protocols(cat: clientcat.Catalog, u: users.User, g: groups.Group | None, plat: str) -> dict[str, list[str]]:
    """{приложение: протоколы} его устройства — как в инструкции: протокол берёт первое приложение, которое его умеет."""
    from . import support
    protos = support.offered_to(u, g, users.selectable_protocols(), users.variant_modules())
    taken: set[str] = set()
    out: dict[str, list[str]] = {}
    for it in apps_of(cat, u, g, {plat}):
        mine = [p for p in protos if p not in taken and cat.status(it.client, p, plat) in ("ok", "warn")]
        taken.update(mine)
        out[it.client["id"]] = mine
    return out


def modules(protos: list[str] | set[str], have: list[str]) -> set[str]:
    """Модули реестра, где живут ключи этих протоколов (Salamander — в Hysteria2). У Xray один клиент на все его
    протоколы: задет один — меняются все Xray-модули человека."""
    from . import support
    variants = users.variant_modules()
    mods = {variants.get(p, p) for p in protos} & set(have)
    xui = set(support.xui_protocols())
    if mods & xui:
        mods |= xui & set(have)
    return mods


def _apps_with(cat: clientcat.Catalog, u: users.User, g: groups.Group | None, plat: str, mods: set[str]) -> list[str]:
    """Приложения устройства, чьи ключи лежат в этих модулях (mods пусто — все приложения устройства)."""
    out = []
    for cid, protos in device_protocols(cat, u, g, plat).items():
        if not mods or modules(protos, u.protocols) & mods:
            out.append((cat.client(cid) or {}).get("name", cid))
    return out


def _q(names: list[str]) -> str:
    return ", ".join(f"«{n}»" for n in dict.fromkeys(names))


def _name(cat: clientcat.Catalog, cid: str) -> str:
    return (cat.client(cid) or {}).get("name", cid)


def _device_noun(plat: str, case: int = 0) -> str:
    return DEVICE_WORD.get(plat, COMPUTER)[case]


def update_line(cat: clientcat.Catalog | None, user: users.User, g: groups.Group | None, plat: str) -> str:
    """Первая строка сообщения для этого устройства, когда сообщение пересылают взамен старого: в каких ЕГО приложениях
    удалить старые подключения, что ставить заново (новый телефон), что добавить (ещё один ключ). Нечего — пусто."""
    if cat is None or not user.resend:
        return ""
    m = marks_of(user)
    upd = cat.raw["update"]
    devices = groups.devices_of(user, g) or []
    if _is_lost(m, plat, devices):
        return upd["lost"].replace("{device}", _device_noun(plat, 1))
    if m.full == "keys" or (m.mods and _apps_with(cat, user, g, plat, m.mods)):
        names = _apps_with(cat, user, g, plat, set() if m.full == "keys" else m.mods)
        return upd["keys"].replace("{apps}", _q(names) or "приложении VPN")
    if m.full == "all":
        return upd["all"].replace("{apps}", _q(_apps_with(cat, user, g, plat, set())) or "приложении VPN")
    if plat in m.new:
        return ""
    parts: list[str] = []
    drop, app = m.drop.get(plat, []), m.app.get(plat, [])
    if drop and app:
        parts.append(upd["switch"].replace("{old}", _q([_name(cat, c) for c in drop]))
                     .replace("{new}", _q([_name(cat, c) for c in app])))
    elif drop:
        parts.append(upd["drop"].replace("{old}", _q([_name(cat, c) for c in drop])))
    elif app:
        parts.append(upd["app"].replace("{new}", _q([_name(cat, c) for c in app])))
    if cids := m.upd.get(plat):
        parts.append(upd["all"].replace("{apps}", _q([_name(cat, c) for c in cids])))
    if protos := m.add.get(plat):
        from . import manifests
        mine = device_protocols(cat, user, g, plat)
        where = [_name(cat, cid) for cid, ps in mine.items() if any(p in ps for p in protos)]
        parts.append(upd["add"].replace("{apps}", _q(where) or "приложении VPN")
                     .replace("{keys}", _q([manifests.proto_title(p) for p in protos])))
    return " ".join(parts)


def lists_only(user: users.User, plat: str) -> bool:
    """На этом устройстве сменился только список «через VPN»: человеку — короткое сообщение, без шагов установки."""
    if not user.resend:
        return False
    m = marks_of(user)
    return (plat in m.lists and not m.full and not m.mods and not m.lost and plat not in m.new
            and not any(plat in d for d in (m.app, m.drop, m.upd, m.add)))


def device_text(cat: clientcat.Catalog, user: users.User, g: groups.Group | None, plat: str) -> list[str]:
    """Что переслать на это устройство — строками для «Кому переслать» (пусто — ничего)."""
    m = marks_of(user)
    devices = groups.devices_of(user, g) or []
    if _is_lost(m, plat, devices):
        return [f"новый {_device_noun(plat)} — сообщение целиком (поставить приложения)"]
    if m.full == "keys" or (m.mods and _apps_with(cat, user, g, plat, m.mods)):
        return [KIND_TEXT["keys"]]
    if m.full == "all":
        return [KIND_TEXT["all"]]
    if plat in m.new:
        return ["сообщение — раньше для этого устройства его не было"]
    out = []
    drop, app = m.drop.get(plat, []), m.app.get(plat, [])
    if drop and app:
        out.append(f"{_q([_name(cat, c) for c in drop])} → {_q([_name(cat, c) for c in app])}: сообщение "
                   f"(в {_q([_name(cat, c) for c in drop])} выключить и удалить подключение)")
    elif drop:
        out.append(f"{_q([_name(cat, c) for c in drop])} больше не нужно: выключить и удалить подключение")
    elif app:
        out.append(f"добавлено {_q([_name(cat, c) for c in app])}: сообщение (поставить только его)")
    if cids := m.upd.get(plat):
        out.append(f"в {_q([_name(cat, c) for c in cids])} другие ключи — сообщение (старые подключения удалить)")
    if protos := m.add.get(plat):
        from . import manifests
        out.append(f"ещё один ключ {_q([manifests.proto_title(p) for p in protos])} — сообщение (добавить только его, "
                   "старые работают)")
    if plat in m.lists and not out:   # новое сообщение уже со свежим списком: отдельно про список не нужно
        out += [f"«{i.client['name']}» — {EFFECT_SEND[i.effect]}"
                + (f" ({EFFECT_DROP[i.effect].replace('{app}', i.client['name'])})" if i.effect in EFFECT_DROP else "")
                for i in apps_of(cat, user, g, {plat}) if i.effect != NONE]
    return out


def describe(cat: clientcat.Catalog | None, user: users.User, g: groups.Group | None) -> list[str]:
    """Что переслать человеку — строками: одинаковое на всех его устройствах — без названия устройства («новые ключи —
    сообщение целиком (…)»), иначе по устройствам («Android: ещё один ключ … · Windows: …»)."""
    if not user.resend:
        return []
    if cat is None:
        full = next((k for k in users.RESEND_FULL if k in user.resend), None)
        return [KIND_TEXT[full]] if full else ["изменились настройки — сообщение целиком"]
    devices = groups.devices_of(user, g)
    plats = [p for p in cat.platforms if (devices is None or p in devices) and g is not None and g.clients.get(p)]
    by_plat = {p: " · ".join(lines) for p in plats if (lines := device_text(cat, user, g, p))}
    if not by_plat:   # без группы или его устройств у группы нет: отметка есть — человек не должен пропасть из списка
        m = marks_of(user)
        return [KIND_TEXT[m.full] if m.full else "изменились настройки — сообщение целиком"]
    texts = list(dict.fromkeys(by_plat.values()))
    if len(texts) == 1 and len(by_plat) == len(plats):
        return texts
    return [f"{', '.join(cat.platforms[p] for p in by_plat if by_plat[p] == x)}: {x}" for x in texts]


def pending(reg: users.Registry) -> list[users.User]:
    return [u for u in reg.visible() if u.resend]


# ---------- потерял одно устройство ----------

@dataclass
class LostPlan:
    plat: str
    mods: list[str]                                         # модули, где ключи сменятся
    others: dict[str, list[str]] = field(default_factory=dict)   # другие его устройства с теми же ключами → их приложения


def lost_plan(cat: clientcat.Catalog, u: users.User, g: groups.Group | None, plat: str) -> LostPlan:
    """Потерял устройство plat: ключи, которые были на нём (протоколы его приложений), меняются; другие устройства с
    теми же ключами (у Xray ключ один на все VLESS) настроить заново, остальные не трогаются."""
    devs = groups.devices_of(u, g) or []
    used = [p for ps in device_protocols(cat, u, g, plat).values() for p in ps]
    mods = modules(used, u.protocols) if used else set(u.protocols)
    out = LostPlan(plat, sorted(mods))
    for other in devs:
        if other == plat:
            continue
        names = [(cat.client(cid) or {}).get("name", cid) for cid, ps in device_protocols(cat, u, g, other).items()
                 if modules(ps, u.protocols) & mods]
        if names:
            out.others[other] = names
    return out
