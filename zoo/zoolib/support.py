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


def _candidate(cat: clientcat.Catalog | None, plat: str, protos: list[str]) -> tuple[str, str] | None:
    """(протокол, приложение) — каким приложением каталога на устройстве взять один из протоколов: рекомендованное
    каталогом, иначе первое, которое его берёт и принимает ссылки."""
    if cat is None:
        return None
    for p in protos:
        c = cat.recommended(plat, p)
        if c is not None and c.get("import") and cat.status(c, p, plat) in ("ok", "warn"):
            return p, c["name"]
    for p in protos:
        c = next((c for c in cat.clients if plat in c["platforms"] and c.get("import")
                  and cat.status(c, p, plat) == "ok"), None)
        if c is not None:
            return p, c["name"]
    return None


def _other_kind(cat: clientcat.Catalog | None, plat: str, title: str, mine: list[App], need: str, offered: list[str],
                selectable: list[str]) -> str:
    """Как дать устройству протокол другого вида (need — «udp» или «tcp»): добавить группе протокол, который его
    приложение уже берёт; или приложение для протокола, который у группы уже есть; или и то и другое."""
    have = [p for p in offered if groups.TRANSPORT.get(p) == need]
    names = ", ".join(f"«{a.name}»" for a in mine)
    lead = f"{_titles(have)} у группы есть, но {names} {'его' if len(have) == 1 else 'их'} не берёт — " if have else ""
    add = [p for p in groups.by_priority(selectable) if groups.TRANSPORT.get(p) == need and p not in offered]
    for p in add:
        a = next((a for a in mine if p in a.can), None)
        if a is not None:
            return lead + f"добавьте группе {manifests.proto_title(p)}: «{a.name}» его берёт."
    if (got := _candidate(cat, plat, have)) is not None:
        proto = manifests.proto_title(got[0])
        return lead + f"дайте на {title} «{got[1]}»" + ("." if proto == got[1] else f" ({proto}).")
    if (got := _candidate(cat, plat, add)) is not None:
        return lead + f"добавьте группе {manifests.proto_title(got[0])} и на {title} «{got[1]}»."
    return lead + f"другого приложения для {title} с {need.upper()} нет."


def _upper(s: str) -> str:
    return s[:1].upper() + s[1:]


def transport_tips(found: list[App], offered: list[str], selectable: list[str],
                   cat: clientcat.Catalog | None = None) -> list[tuple[str, str, bool]]:
    """По устройствам человека: (вид, текст, нужна ли правка группы). Протоколы — только те, что достались его
    приложениям (как в инструкции). Только UDP — предупреждение: мобильные сети и офисный Wi-Fi режут его чаще;
    только TCP — справка (VLESS обычно проходит), что делать, если режут и его."""
    out: list[tuple[str, str, bool]] = []
    by_plat: dict[str, list[App]] = {}
    for a in found:
        by_plat.setdefault(a.plat or a.platform, []).append(a)
    for plat, mine in by_plat.items():
        title = mine[0].platform
        took = groups.by_priority(p for a in mine for p in _working(a))
        udp = [p for p in took if groups.TRANSPORT.get(p) == "udp"]
        tcp = [p for p in took if groups.TRANSPORT.get(p) == "tcp"]
        if udp and tcp:
            where = next((a.name for a in mine if any(groups.TRANSPORT.get(p) == "tcp" for p in _working(a))), "")
            out.append(("info", f"{title}: не подключается — пусть включит TCP ({_titles(tcp)})"
                        + (f" в «{where}»" if where else "") + f"; где режут TCP — наоборот, UDP ({_titles(udp)}).", False))
        elif udp:
            out.append(("warn", f"{title}: только UDP ({_titles(udp)}) — где режут UDP (мобильный интернет, офисный "
                                "Wi-Fi), не подключится. " + _upper(_other_kind(cat, plat, title, mine, "tcp", offered,
                                                                               selectable)), True))
        elif tcp:
            out.append(("info", f"{title}: только TCP ({_titles(tcp)}) — обычно хватает. Если режут и его: "
                        + _other_kind(cat, plat, title, mine, "udp", offered, selectable), True))
    return out


def app_tips(found: list[App], cat: clientcat.Catalog | None = None,
             offered: list[str] | tuple[str, ...] = ()) -> list[tuple[str, str, bool]]:
    """Приложения, которые с его протоколами не возьмут, не проверены или пускают через VPN всё устройство; замена —
    названа: другое его приложение на том же устройстве, иначе приложение каталога, которое берёт протоколы группы."""
    out: list[tuple[str, str, bool]] = []
    slow: dict[tuple[str, str, bool], list[str]] = {}   # (приложение, чем заменить, правка группы) → устройства
    for a in found:
        label = f"«{a.name}» ({a.platform})"
        if not a.protos:
            out.append(("warn", f"{label} не берёт ни один его протокол: смените приложение или протоколы группы.", True))
        elif not _working(a):
            out.append(("warn", f"{label} с {_titles([p for p, _ in a.protos])} не проверено: не работает — "
                                "дайте другое приложение.", True))
        if a.via == "all":
            alt = next((b.name for b in found if b.platform == a.platform and b.via != "all" and _working(b)), "")
            fix = False
            if not alt and cat is not None:
                c = next((c for c in cat.clients if a.plat in c["platforms"] and c.get("import")
                          and cat.via(c, a.plat) not in ("all", "")
                          and any(cat.status(c, p, a.plat) == "ok" for p in offered)), None)
                alt, fix = (c["name"], True) if c else ("", False)
            slow.setdefault((a.name, alt, fix), []).append(a.platform)
    for (name, alt, fix), plats in slow.items():
        where = ", ".join(plats)
        tail = (f"смените {where} в группе на «{alt}»." if fix else f"пусть включит «{alt}»." if alt
                else "приложения, где идёт не всё, для этого устройства нет.")
        out.append(("info", f"Медленно в «{name}» ({where}): через VPN идёт всё устройство — " + tail, fix))
    return out


def server_tip(mine: list[str], broken: set[str], variants: dict[str, str]) -> tuple[str, str] | None:
    """Его протоколы, которые сейчас не работают на самом сервере (сервис упал, порт не слушает)."""
    down = [p for p in mine if variants.get(p, p) in broken or p in broken]
    if not down:
        return None
    rest = [p for p in mine if p not in down]
    if not rest:
        return "bad", (f"На сервере не работает {_titles(down)} — другого у человека нет: дело не в нём. "
                       "Почините на «Обзоре» (перезапуск, журнал).")
    return "warn", (f"На сервере не работает {_titles(down)}: пусть включит {_titles(rest)}; "
                    "сам протокол почините на «Обзоре».")


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
              broken: set[str] | None = None) -> list[tuple[str, str, bool]]:
    """Пункты «Если не работает» по порядку: работают ли его протоколы на сервере, доходит ли он, UDP/TCP по
    устройствам, приложения. [(вид, текст, нужна ли правка группы)]."""
    protos = offered_to(u, g, selectable, variants)
    found = apps(cat, u, g, protos, selectable) if cat is not None else []
    tips: list[tuple[str, str, bool]] = []
    if st := server_tip(protos, broken or set(), variants):
        tips.append((*st, False))
    kind, text = contact_tip(u, c, now)
    if tips and kind == "ok":
        kind, text = "info", text.split(" — ")[0] + "."
    tips.append((kind, text, False))
    offered = g.offered(selectable) if g is not None else list(selectable)
    tips += transport_tips(found, offered, selectable, cat)
    return tips + app_tips(found, cat, offered)


def stranded(protocols: list[dict[str, Any]], reg: users.Registry, gs: groups.Groups) -> dict[str, tuple[str, int]]:
    """{название группы: (id, сколько людей без VPN)}: у кого все его протоколы сейчас не работают на сервере (сводка
    status: enabled и не ok). Для плашки на «Обзоре»: кого задевает сбой."""
    broken = {p["id"] for p in protocols if p.get("enabled") and not p.get("ok")}
    if not broken:
        return {}
    selectable = users.selectable_protocols()
    variants = users.variant_modules()
    out: dict[str, tuple[str, int]] = {}
    for u in reg.visible():
        if not u.enabled or u.name == users.OWNER:
            continue
        g = gs.get(u.group)
        mine = offered_to(u, g, selectable, variants)
        if mine and all(variants.get(p, p) in broken or p in broken for p in mine):
            key = g.name if g else "без группы"
            gid, n = out.get(key, (g.id if g else "", 0))
            out[key] = (gid, n + 1)
    return out
