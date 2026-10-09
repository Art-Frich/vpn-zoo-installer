"""«У человека не работает или медленно» (D58): когда он последний раз подключался и что проверить.

Отметка подключения — из коллектора (traffic.last_contact): Xray — lastOnline клиента 3x-ui или приращение его
трафика (какой из Xray-протоколов, не видно: счётчик один на все), Hysteria2 — «в сети» (/online trafficStats) или
трафик за 5 минут, AmneziaWG — последнее рукопожатие. Подсказки — из того, что у человека есть: протоколы его группы
(транспорт UDP/TCP), приложения на его устройствах (каталог clients.json: статус протокола в приложении, что идёт
через VPN).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import clients as clientcat
from . import groups, manifests, output, resend, traffic, users

STALE_DAYS = 7   # не подключался дольше — «пропал» (выходные и короткий отпуск не в счёт)
FRESH = 3600     # подключался не позже — «сейчас доходит»; коллектор снимает раз в 5 минут


@dataclass
class Contacts:
    known: bool = False                                        # коллектор уже снимал счётчики
    last: dict[str, tuple[int, str]] = field(default_factory=dict)

    def of(self, name: str) -> tuple[int, str] | None:
        return self.last.get(name)


def contacts() -> Contacts:
    last = traffic.last_contact()
    return Contacts(bool(last) or traffic.last_run() is not None, last)


class Fix(str):
    """Правка группы, которую советует подсказка: сама строка — устройство (счётчик людей с ним), add — протоколы
    добавить группе, switch — новый набор приложений устройства, give — приложение добавить устройству. По ним
    страница человека считает, кому придёт что переслать, тем же сравнением, что «Кому переслать»."""
    add: tuple[str, ...] = ()
    switch: tuple[str, ...] = ()
    give: str = ""

    def __new__(cls, plat: str, add: Any = (), switch: Any = (), give: str = "") -> "Fix":
        obj = super().__new__(cls, plat)
        obj.add, obj.switch, obj.give = tuple(add), tuple(switch), give
        return obj


def _either(titles: list[str]) -> str:
    return titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + " или " + titles[-1]


def xui_protocols() -> list[str]:
    return groups.by_priority(m.id for m in manifests.load_all()[0] if m.users_backend == "xui")


def via_title(proto: str, user: users.User | None = None, xui: list[str] | None = None) -> str:
    """Через что подключался: название протокола; у Xray — его Xray-протоколы («VLESS XHTTP или VLESS Vision»).
    xui — xui_protocols(), если звать для многих людей."""
    if proto != traffic.XRAY:
        return manifests.proto_title(proto)
    mine = [p for p in (xui_protocols() if xui is None else xui) if user is None or p in user.protocols]
    return _either([manifests.proto_title(p) for p in mine]) if mine else "Xray"


def stale(ts: int, now: float | None = None) -> bool:
    return (time.time() if now is None else now) - ts > STALE_DAYS * 86400


def silent(reg: users.Registry, c: Contacts, now: float | None = None) -> tuple[list[users.User], list[users.User]]:
    """(ни разу не подключались, пропали дольше STALE_DAYS) среди включённых людей; owner — ключи самого админа — не в счёт."""
    never, gone = [], []
    if not c.known:
        return never, gone
    for u in reg.visible():
        if not u.enabled or u.name == users.OWNER:
            continue
        seen = c.of(u.name)
        if seen is None:
            never.append(u)
        elif stale(seen[0], now):
            gone.append(u)
    return never, gone


def offered_to(u: users.User, g: groups.Group | None, selectable: list[str], variants: dict[str, str]) -> list[str]:
    """Протоколы, которые человек видит у себя (как ссылки на его странице): выбранные группой, где у него есть учётка."""
    have = set(u.protocols)
    sees_all = g is None or g.all_protocols or u.custom or u.name == users.OWNER
    base = selectable if sees_all else g.offered(selectable)
    return groups.by_priority(p for p in base if variants.get(p, p) in have)


@dataclass
class App:
    platform: str          # «Android»
    name: str              # «Happ»
    protos: list[tuple[str, str]]   # (id, статус ok/warn) — его протоколы, которые достались этому приложению; только unk — не проверено
    via: str               # apps, ru-direct, all, brave
    can: set[str] = field(default_factory=set)   # выбираемые протоколы, которые приложение берёт (ok/warn)
    plat: str = ""         # id платформы («android»)
    cid: str = ""          # id приложения в каталоге


def apps(cat: clientcat.Catalog, u: users.User, g: groups.Group | None, protos: list[str],
         selectable: list[str] | None = None) -> list[App]:
    """Приложения человека по его устройствам. Протокол достаётся первому приложению устройства, которое его берёт
    (ok/warn), — как в его инструкции (clientviews._assign): «не проверено» в инструкцию не попадает, на него не
    надеяться."""
    out = []
    taken: dict[str, set[str]] = {}
    for it in resend.apps_of(cat, u, g):
        got = taken.setdefault(it.platform, set())
        mine = [(p, s) for p in protos if p not in got and (s := cat.status(it.client, p, it.platform)) in ("ok", "warn")]
        got.update(p for p, _ in mine)
        if not mine:
            mine = [(p, "unk") for p in protos if p not in got and cat.status(it.client, p, it.platform) == "unk"]
        can = {p for p in selectable or () if cat.status(it.client, p, it.platform) in ("ok", "warn")}
        out.append(App(cat.platforms.get(it.platform, it.platform), it.client["name"], mine,
                       cat.via(it.client, it.platform), can, it.platform, it.client["id"]))
    return out


def _titles(ps: list[str]) -> str:
    return ", ".join(manifests.proto_title(p) for p in ps)


def _working(a: App) -> list[str]:
    return [p for p, s in a.protos if s in ("ok", "warn")]


def _candidate(cat: clientcat.Catalog | None, plat: str, protos: list[str]) -> tuple[str, str, str] | None:
    """(протокол, приложение, его id) — каким приложением каталога на устройстве взять один из протоколов:
    рекомендованное каталогом, иначе первое, которое его берёт и принимает ссылки."""
    if cat is None:
        return None
    for p in protos:
        c = cat.recommended(plat, p)
        if c is not None and c.get("import") and cat.status(c, p, plat) in ("ok", "warn"):
            return p, c["name"], c["id"]
    for p in protos:
        c = next((c for c in cat.clients if plat in c["platforms"] and c.get("import")
                  and cat.status(c, p, plat) == "ok"), None)
        if c is not None:
            return p, c["name"], c["id"]
    return None


def _dead(broken: set[str] | frozenset[str], variants: dict[str, str] | None = None) -> Any:
    """Проверка «протокол сейчас не работает на сервере» (вариант — по своему модулю: Salamander — по Hysteria2)."""
    variants = variants or {}
    return lambda p: p in broken or variants.get(p, p) in broken


def _other_kind(cat: clientcat.Catalog | None, plat: str, title: str, mine: list[App], need: str, offered: list[str],
                selectable: list[str], dead: Any = lambda p: False) -> tuple[str, Fix | str]:
    """Как дать устройству протокол другого вида (need — «udp» или «tcp»): добавить группе протокол, который его
    приложение уже берёт; или приложение для протокола, который у группы уже есть; или и то и другое. Не работающие
    сейчас на сервере протоколы не советуются. (текст, правка группы — Fix; советовать нечего — пусто)."""
    have = [p for p in offered if groups.TRANSPORT.get(p) == need and not dead(p)]
    names = ", ".join(f"«{a.name}»" for a in mine)
    lead = f"{_titles(have)} у группы есть, но {names} {'его' if len(have) == 1 else 'их'} не берёт — " if have else ""
    add = [p for p in groups.by_priority(selectable) if groups.TRANSPORT.get(p) == need and p not in offered
           and not dead(p)]
    for p in add:
        a = next((a for a in mine if p in a.can), None)
        if a is not None:
            return lead + f"добавьте группе {manifests.proto_title(p)}: «{a.name}» его берёт.", Fix(plat, add=[p])
    if (got := _candidate(cat, plat, have)) is not None:
        proto = manifests.proto_title(got[0])
        return (lead + f"дайте на {title} «{got[1]}»" + ("." if proto == got[1] else f" ({proto})."),
                Fix(plat, give=got[2]))
    if (got := _candidate(cat, plat, add)) is not None:
        return (lead + f"добавьте группе {manifests.proto_title(got[0])} и на {title} «{got[1]}».",
                Fix(plat, add=[got[0]], give=got[2]))
    return lead + f"другого приложения для {title} с {need.upper()} нет.", ""


def _upper(s: str) -> str:
    return s[:1].upper() + s[1:]


def transport_tips(found: list[App], offered: list[str], selectable: list[str],
                   cat: clientcat.Catalog | None = None, dead: Any = lambda p: False) -> list[tuple[str, str, str]]:
    """По устройствам человека: (вид, текст, устройство, если нужна правка группы — иначе пусто). Протоколы — только
    те, что достались его приложениям (как в инструкции). Только UDP — предупреждение: мобильные сети и офисный Wi-Fi
    режут его чаще; только TCP — справка (VLESS обычно проходит), что делать, если режут и его."""
    out: list[tuple[str, str, str]] = []
    by_plat: dict[str, list[App]] = {}
    for a in found:
        by_plat.setdefault(a.plat or a.platform, []).append(a)
    for plat, mine in by_plat.items():
        title = mine[0].platform
        # упавший на сервере протокол запасным не предлагается: о нём — строка «На сервере не работает»
        took = groups.by_priority(p for a in mine for p in _working(a) if not dead(p))
        udp = [p for p in took if groups.TRANSPORT.get(p) == "udp"]
        tcp = [p for p in took if groups.TRANSPORT.get(p) == "tcp"]
        if udp and tcp:
            where = next((a.name for a in mine if any(groups.TRANSPORT.get(p) == "tcp" for p in _working(a))), "")
            out.append(("info", f"{title}: не подключается — пусть включит TCP ({_titles(tcp)})"
                        + (f" в «{where}»" if where else "") + f"; где режут TCP — наоборот, UDP ({_titles(udp)}).", ""))
        elif udp:
            text, fix = _other_kind(cat, plat, title, mine, "tcp", offered, selectable, dead)
            out.append(("warn", f"{title}: только UDP ({_titles(udp)}) — где режут UDP (мобильный интернет, офисный "
                                "Wi-Fi), не подключится. " + _upper(text), fix))
        elif tcp:
            text, fix = _other_kind(cat, plat, title, mine, "udp", offered, selectable, dead)
            out.append(("info", f"{title}: только TCP ({_titles(tcp)}) — обычно хватает. Если режут и его: " + text, fix))
    return out


def _switch(cat: clientcat.Catalog, plat: str, title: str, offered: list[str] | tuple[str, ...],
            selectable: list[str] | tuple[str, ...], dead: Any) -> tuple[str, Fix | str]:
    """«смените Windows в группе на «v2rayN» (ему достанется VLESS XHTTP)»: приложение каталога, где через VPN идёт не всё,
    и какой работающий протокол ему достанется; достанется только UDP — сразу и какой TCP добавить группе (одна правка,
    одна пересылка, а не вторая тревога «только UDP»); рабочего у группы нет — какой протокол добавить. Нет — пусто."""
    cands = [c for c in cat.clients if plat in c["platforms"] and c.get("import") and cat.via(c, plat) not in ("all", "")]
    for c in cands:
        gets = [p for p in offered if cat.status(c, p, plat) == "ok" and not dead(p)]
        if not gets:
            continue
        if not any(groups.TRANSPORT.get(p) == "tcp" for p in gets):
            tcp = [p for p in groups.by_priority(selectable) if p not in offered and groups.TRANSPORT.get(p) == "tcp"
                   and cat.status(c, p, plat) == "ok" and not dead(p)]
            if tcp:
                return (f"смените {title} в группе на «{c['name']}» и добавьте группе {_titles(tcp[:1])} — одна правка и "
                        f"одна пересылка (с одним {_titles(gets[:1])} у него было бы только UDP).",
                        Fix(plat, add=tcp[:1], switch=[c["id"]]))
        return f"смените {title} в группе на «{c['name']}» (ему достанется {_titles(gets[:1])}).", Fix(plat, switch=[c["id"]])
    for c in cands:
        add = [p for p in groups.by_priority(selectable) if p not in offered and cat.status(c, p, plat) == "ok"
               and not dead(p)]
        if add:
            gets = [p for p in offered if cat.status(c, p, plat) in ("ok", "warn")]
            why = (f" (сейчас ему достанется только {_titles(gets)} — {'он не работает' if len(gets) == 1 else 'они не работают'})"
                   if gets and all(dead(p) for p in gets) else "")
            return (f"смените {title} в группе на «{c['name']}» и добавьте группе {_titles(add[:1])}{why}.",
                    Fix(plat, add=add[:1], switch=[c["id"]]))
    return "", ""


def app_tips(found: list[App], cat: clientcat.Catalog | None = None, offered: list[str] | tuple[str, ...] = (),
             selectable: list[str] | tuple[str, ...] = (), dead: Any = lambda p: False) -> list[tuple[str, str, str]]:
    """Приложения, которые с его протоколами не возьмут, не проверены или пускают через VPN всё устройство; замена —
    названа: другое его приложение на том же устройстве, иначе приложение каталога и какой рабочий протокол ему
    достанется (_switch). Третье поле — устройство, если нужна правка группы."""
    out: list[tuple[str, str, str]] = []
    slow: dict[tuple[str, str], tuple[list[str], Fix | str]] = {}   # (приложение, чем заменить) → устройства, правка
    for a in found:
        label = f"«{a.name}» ({a.platform})"
        if not a.protos:
            out.append(("warn", f"{label} не берёт ни один его протокол: смените приложение или протоколы группы.", a.plat))
        elif not _working(a):
            out.append(("warn", f"{label} с {_titles([p for p, _ in a.protos])} не проверено: не работает — "
                                "дайте другое приложение.", a.plat))
        if a.via == "all":
            alt = next((f"пусть включит «{b.name}»." for b in found if b.platform == a.platform and b.via != "all"
                        and any(not dead(p) for p in _working(b))), "")
            fix: Fix | str = ""
            if not alt and cat is not None:
                alt, fix = _switch(cat, a.plat, a.platform, offered, selectable or offered, dead)
            slow.setdefault((a.name, alt), ([], fix))[0].append(a.platform)
        elif a.via == "apps" and cat is not None and _rules_app(cat, a):
            out.append(("info", f"«{a.name}» ({a.platform}): медленно только приложения из его списка «Через VPN» — по "
                                f"подсказке «Медленно» ниже; медленно всё подряд — проверьте, что в «{a.name}» активен наш "
                                "набор правил (шаг импорта файла правил), иначе через VPN может идти весь компьютер; "
                                "правила активны — дело не в VPN.", ""))
    for (name, alt), (plats, fix) in slow.items():
        where = ", ".join(plats)
        out.append(("info", f"Медленно в «{name}» ({where}): через VPN идёт всё устройство — "
                    + (alt or "приложения, где идёт не всё, для этого устройства нет."), fix))
    return out


def _rules_app(cat: clientcat.Catalog, a: App) -> bool:
    """Список «через VPN» в приложении — из нашего файла правил (v2rayN на Windows): если правило не активно, через VPN
    идёт всё."""
    c = cat.client(a.cid) if a.cid else next((c for c in cat.clients if c["name"] == a.name), None)
    return bool(c and c.get("per_app") == "rules" and any(ex["platform"] == a.plat for ex in c.get("extra", [])))


def speed_tip(found: list[App], offered: list[str] | tuple[str, ...], selectable: list[str] | tuple[str, ...],
              dead: Any = lambda p: False) -> str:
    """Что делать с цифрами speedtest: с VPN намного медленнее — запасной ключ или второе приложение его устройства,
    иначе протокол, который его приложение берёт, а группе не дан; медленно у всех — сервер."""
    alts: list[str] = []
    by_plat: dict[str, list[App]] = {}
    for a in found:
        by_plat.setdefault(a.platform, []).append(a)
    for title, mine in by_plat.items():
        live = [(a, [p for p in _working(a) if not dead(p)]) for a in mine]
        live = [(a, ps) for a, ps in live if ps]
        if len(live) > 1:
            alts.append(f"{title}: пусть включит «{live[1][0].name}»")
        elif live and len(live[0][1]) > 1:
            alts.append(f"{title}: пусть включит запасной ключ {_titles(live[0][1][1:2])}")
    if not alts:
        for a in found:
            add = [p for p in groups.by_priority(selectable) if p not in offered and p in a.can and not dead(p)]
            if add:
                alts.append(f"добавьте группе {_titles(add[:1])} («{a.name}» его берёт)")
                break
    first = "; ".join(alts) or "другого протокола, который берут его приложения, на сервере нет"
    return (f"Медленно: с VPN намного медленнее, чем без, — {first}. Медленно у всех — «Обзор»: нагрузка процессора; "
            "«Проверка» → «Самопроверка с сервера»: сколько тянет туннель на самом сервере.")


def server_tip(found: list[App], mine: list[str], broken: set[str],
               variants: dict[str, str]) -> tuple[str, str] | None:
    """Протоколы, которые его приложения правда берут (как в инструкции) и которые сейчас не работают на самом сервере
    (сервис упал, порт не слушает). По устройствам: на каком есть другой рабочий протокол, на каком — нет."""
    dead = _dead(broken, variants)
    by_plat: dict[str, list[tuple[str, str]]] = {}   # устройство → (протокол, приложение)
    for a in found:
        by_plat.setdefault(a.platform, []).extend((p, a.name) for p in _working(a))
    if not found:
        by_plat = {"": [(p, "") for p in mine]}
    down = groups.by_priority(p for ps in by_plat.values() for p, _ in ps if dead(p))
    if not down:
        return None
    alive = {plat: [(p, app) for p, app in ps if not dead(p)] for plat, ps in by_plat.items()}
    if not any(alive.values()):
        return "bad", (f"На сервере не работает {_titles(down)} — другого у человека нет: дело не в нём. "
                       "Почините на «Обзоре» (перезапуск, журнал).")
    parts = []
    for plat, ps in alive.items():
        if not any(dead(p) for p, _ in by_plat[plat]):
            continue
        where = f"{plat}: " if plat else ""
        if not ps:
            parts.append(f"{where}другого у него нет")
            continue
        names = list(dict.fromkeys(f"{manifests.proto_title(p)}" + (f" в «{app}»" if app and app != manifests.proto_title(p) else "")
                                   for p, app in ps))
        parts.append(f"{where}пусть включит {_either(names)}")
    return "warn", f"На сервере не работает {_titles(down)}: " + "; ".join(parts) + ". Сам протокол почините на «Обзоре»."


def contact_tip(u: users.User, c: Contacts, now: float | None = None) -> tuple[str, str]:
    """Доходит ли он до сервера сейчас: «да» — только если подключался не позже часа назад (FRESH)."""
    seen = c.of(u.name)
    now = time.time() if now is None else now
    if not u.enabled:
        return "bad", "Отключён: не подключится, пока не нажмёте «Включить»."
    if not c.known:
        return "info", "Подключения ещё не собирались: зайдите через 5 минут."
    if seen is None:
        return "warn", "Ни разу не подключался: ключи не добавлены или скопированы не целиком — перешлите сообщение."
    if stale(seen[0], now):
        return "warn", f"Не подключался дольше {STALE_DAYS} дней: выключено приложение или сеть режет протокол."
    if now - seen[0] > FRESH:
        return "warn", (f"Последний раз — {output.human_duration(now - seen[0])} назад: сейчас до сервера не доходит. "
                        "Выключено приложение, сеть режет протокол или ключ старый.")
    return "ok", "Подключался в последний час — до сервера доходит, ключи верные: дело в приложении или в списке «Через VPN»."


def checklist(u: users.User, g: groups.Group | None, c: Contacts, cat: clientcat.Catalog | None,
              selectable: list[str], variants: dict[str, str], now: float | None = None,
              broken: set[str] | None = None) -> list[tuple[str, str, str]]:
    """Пункты «Если не работает» по порядку: работают ли его протоколы на сервере, доходит ли он, UDP/TCP по
    устройствам, приложения, что делать с «медленно». [(вид, текст, устройство, если нужна правка группы — иначе пусто)].
    Протоколы — те, что берут его приложения (как в инструкции), а не все протоколы группы."""
    protos = offered_to(u, g, selectable, variants)
    found = apps(cat, u, g, protos, selectable) if cat is not None else []
    dead = _dead(broken or set(), variants)
    tips: list[tuple[str, str, str]] = []
    if st := server_tip(found, protos, broken or set(), variants):
        tips.append((*st, ""))
    kind, text = contact_tip(u, c, now)
    if tips and kind == "ok":
        kind, text = "info", text.split(" — ")[0] + "."
    tips.append((kind, text, ""))
    offered = g.offered(selectable) if g is not None else list(selectable)
    tips += transport_tips(found, offered, selectable, cat, dead)
    tips += app_tips(found, cat, offered, selectable, dead)
    if found:
        tips.append(("info", speed_tip(found, offered, selectable, dead), ""))
    return tips


def usable(cat: clientcat.Catalog | None, u: users.User, g: groups.Group | None, mine: list[str]) -> list[str]:
    """Протоколы, которые берут его приложения (как в «Все ссылки и QR»); без каталога или группы — все его."""
    if cat is None or g is None:
        return mine
    found = apps(cat, u, g, mine)
    took = {p for a in found for p in _working(a)}
    return [p for p in mine if p in took] if found else mine


@dataclass
class Fallback:
    group: str          # название группы
    gid: str
    device: str         # «Android»
    app: str            # запасное приложение, которое надо включить
    people: list[users.User] = field(default_factory=list)


def fallbacks(protocols: list[dict[str, Any]], reg: users.Registry, gs: groups.Groups) -> list[Fallback]:
    """Кому при сбое протокола писать «включите запасное»: у основного (первого) приложения устройства все протоколы
    сейчас не работают на сервере, а у запасного на том же устройстве рабочий есть (AmneziaWG упал — Android с
    «Надёжно» живёт на Happ, но сам человек об этом не узнает). По группе и устройству."""
    broken = {p["id"] for p in protocols if p.get("enabled") and not p.get("ok")}
    if not broken:
        return []
    selectable = users.selectable_protocols()
    variants = users.variant_modules()
    dead = _dead(broken, variants)
    try:
        cat = clientcat.load()
    except clientcat.ClientsError:
        return []
    out: dict[tuple[str, str, str], Fallback] = {}
    for u in reg.visible():
        g = gs.get(u.group)
        if not u.enabled or u.name == users.OWNER or g is None:
            continue
        by_plat: dict[str, list[App]] = {}
        for a in apps(cat, u, g, offered_to(u, g, selectable, variants)):
            by_plat.setdefault(a.plat, []).append(a)
        for mine in by_plat.values():
            main = mine[0]
            if not _working(main) or not all(dead(p) for p in _working(main)):
                continue
            alt = next((b for b in mine[1:] if any(not dead(p) for p in _working(b))), None)
            if alt is not None:
                key = (g.id, main.platform, alt.name)
                out.setdefault(key, Fallback(g.name, g.id, main.platform, alt.name)).people.append(u)
    return list(out.values())


def stranded(protocols: list[dict[str, Any]], reg: users.Registry, gs: groups.Groups) -> dict[str, tuple[str, int]]:
    """{название группы: (id, сколько людей без VPN)}: у кого все протоколы, которые берут его приложения, сейчас не
    работают на сервере (сводка status: enabled и не ok). Для плашки на «Обзоре»: кого задевает сбой."""
    broken = {p["id"] for p in protocols if p.get("enabled") and not p.get("ok")}
    if not broken:
        return {}
    selectable = users.selectable_protocols()
    variants = users.variant_modules()
    try:
        cat: clientcat.Catalog | None = clientcat.load()
    except clientcat.ClientsError:
        cat = None
    out: dict[str, tuple[str, int]] = {}
    for u in reg.visible():
        if not u.enabled or u.name == users.OWNER:
            continue
        g = gs.get(u.group)
        mine = usable(cat, u, g, offered_to(u, g, selectable, variants))
        if mine and all(variants.get(p, p) in broken or p in broken for p in mine):
            key = g.name if g else "без группы"
            gid, n = out.get(key, (g.id if g else "", 0))
            out[key] = (gid, n + 1)
    return out
