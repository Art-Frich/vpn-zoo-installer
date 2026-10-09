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
from . import groups, manifests, resend, traffic, users

STALE_DAYS = 7   # не подключался дольше — «пропал» (выходные и короткий отпуск не в счёт)


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
    protos: list[tuple[str, str]]   # (id, статус каталога ok/warn/unk) — его протоколы, которые приложение берёт
    via: str               # apps, ru-direct, all
    can: set[str] = field(default_factory=set)   # выбираемые протоколы, которые приложение берёт (ok/warn)


def apps(cat: clientcat.Catalog, u: users.User, g: groups.Group | None, protos: list[str],
         selectable: list[str] | None = None) -> list[App]:
    out = []
    for it in resend.apps_of(cat, u, g):
        took = [(p, s) for p in protos if (s := cat.status(it.client, p, it.platform)) in ("ok", "warn", "unk")]
        can = {p for p in selectable or () if cat.status(it.client, p, it.platform) in ("ok", "warn")}
        out.append(App(cat.platforms.get(it.platform, it.platform), it.client["name"], took,
                       cat.via(it.client, it.platform), can))
    return out


def _titles(ps: list[str]) -> str:
    return ", ".join(manifests.proto_title(p) for p in ps)


def transport_tip(protos: list[str], found: list[App], selectable: list[str]) -> tuple[str, str] | None:
    """(вид, текст) про UDP/TCP: что попробовать, если сеть режет один из них. Приложения известны — считаются только
    протоколы, которые они берут, и подсказывается, где именно."""
    if found:
        took = {p for a in found for p, _ in a.protos}
        protos = [p for p in protos if p in took]
    udp = [p for p in protos if groups.TRANSPORT.get(p) == "udp"]
    tcp = [p for p in protos if groups.TRANSPORT.get(p) == "tcp"]
    if udp and tcp:
        where = list(dict.fromkeys(f"«{a.name}»" for a in found if any(p in tcp for p, _ in a.protos)))
        return "info", (f"Не подключается — пусть попробует TCP: {_titles(tcp)}" + (f" в {', '.join(where)}" if where else "")
                        + f". Где режут TCP — наоборот, UDP: {_titles(udp)}.")
    if udp or tcp:
        have, other = ("UDP", "tcp") if udp else ("TCP", "udp")
        fits = [p for p in groups.by_priority(selectable) if groups.TRANSPORT.get(p) == other
                and (not found or any(p in a.can for a in found))]
        add = (f" Добавьте группе {manifests.proto_title(fits[0])} ({other.upper()})." if fits
               else " Его приложения другого не берут — смените приложение.")
        return "warn", f"У него только {have} ({_titles(udp or tcp)}): где режут {have}, он не подключится." + add
    return None


def app_tips(found: list[App]) -> list[tuple[str, str]]:
    """Приложения, которые с его протоколами не возьмут, не проверены или пускают через VPN всё устройство."""
    out = []
    slow: dict[tuple[str, str], list[str]] = {}   # (приложение, чем заменить) → платформы: одна строка на приложение
    for a in found:
        label = f"«{a.name}» ({a.platform})"
        if not a.protos:
            out.append(("warn", f"{label} не берёт ни один его протокол: смените приложение или протоколы группы."))
        elif all(s == "unk" for _, s in a.protos):
            out.append(("warn", f"{label} с {_titles([p for p, _ in a.protos])} не проверено: не работает — "
                                "дайте другое приложение."))
        if a.via == "all":
            alt = next((b.name for b in found if b.platform == a.platform and b.via != "all" and b.protos), "")
            slow.setdefault((a.name, alt), []).append(a.platform)
    for (name, alt), plats in slow.items():
        out.append(("info", f"Медленно в «{name}» ({', '.join(plats)}): через VPN идёт всё устройство — "
                            + (f"пусть включит «{alt}»." if alt else "дайте приложение, где идёт не всё.")))
    return out


def contact_tip(u: users.User, c: Contacts, now: float | None = None) -> tuple[str, str]:
    """Первый шаг: доходит ли он до сервера."""
    seen = c.of(u.name)
    if not u.enabled:
        return "bad", "Он отключён: не подключится, пока не нажмёте «Включить»."
    if not c.known:
        return "info", "Подключения ещё не собирались: зайдите через 5 минут."
    if seen is None:
        return "warn", "Ни разу не подключался: ключи не добавлены или скопированы не целиком — перешлите сообщение."
    if stale(seen[0], now):
        return "warn", f"Не подключался дольше {STALE_DAYS} дней: выключено приложение или сеть режет протокол."
    return "ok", "До сервера доходит, ключи верные: дело в приложении или в списке «Через VPN»."


def checklist(u: users.User, g: groups.Group | None, c: Contacts, cat: clientcat.Catalog | None,
              selectable: list[str], variants: dict[str, str], now: float | None = None) -> list[tuple[str, str]]:
    """Пункты «Если не работает» по порядку: доходит ли до сервера, UDP/TCP, приложения. [(вид, текст)]."""
    protos = offered_to(u, g, selectable, variants)
    found = apps(cat, u, g, protos, selectable) if cat is not None else []
    tips = [contact_tip(u, c, now)]
    if tt := transport_tip(protos, found, selectable):
        tips.append(tt)
    return tips + app_tips(found)
