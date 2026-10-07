"""Страница «Клиенты» (что ставить по протоколам и платформам) и блок «Подключить»: приложения платформы,
QR и ссылки конкретного человека и один текст инструкции на группу. Данные — каталог
zoo/data/clients.json и кэш версий; в сеть страницы не ходят."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import allowlist, clients, groups, manifests, protolib, qr, users
from .html import Markup, card, post_button, t, table
from .views import ago, alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

DESKTOP = {"windows", "macos", "linux"}
MAIN_PLATFORMS = groups.MAIN_DEVICES   # устройства по умолчанию в мастере и на /clients; остальные — чипы выключены
LEGEND = "✓ заявлено поддерживаемым · ! с оговоркой · ✕ не работает · ? не проверено · — не заявлено"
UNVERIFIED = "Шаги и статусы — по коду и документации клиентов, на устройстве не проверялись."
RECOMMEND_BASIS = "по документации и исследованию 04.10.2026, на устройстве не проверено"
SEND_WARN = ("Ссылки и QR — ключи доступа: не отправляйте через MAX и VK, лучше лично или мессенджером "
             "со сквозным шифрованием.")
FOREIGN_STORE = "В российском App Store его нет: нужен Apple ID другой страны, подделки с похожим названием не ставьте."
NAME_TOKEN = "{name}"
MISMATCH = "текст группы не подходит этому человеку — показан свой"
STALE = "набор клиентов изменился — проверьте текст"
FILE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _sorted_links(links: list[dict[str, Any]], store_first: bool = False) -> list[dict[str, Any]]:
    """Проверенные ссылки раньше собранных по id пакета; store_first — магазины раньше всего (люди ставят сами, D49)."""
    return sorted(links, key=lambda ln: (store_first and ln["kind"] not in groups.STORE_KINDS, not ln["checked"]))


def store_first(g: "groups.Group | None") -> bool:
    """Люди ставят сами: ставить из магазина, а не APK со страницы релизов."""
    return g is None or g.install_mode == "self"


def _has_github(client: dict[str, Any], platform: str) -> bool:
    return client.get("repo") is not None and any(ln["kind"] == "github" for ln in client["platforms"].get(platform, []))


def _version(client: dict[str, Any], platform: str, cache: dict[str, Any]) -> str | None:
    return clients.version_of(cache, client["id"]) if _has_github(client, platform) else None


def _link_anchors(links: list[dict[str, Any]], presorted: bool = False) -> list[Markup]:
    out: list[Markup] = []
    for ln in (links if presorted else _sorted_links(links)):
        out.append(t("a", clients.LINK_KINDS[ln["kind"]], href=ln["url"], target="_blank",
                     rel="noopener noreferrer", class_="chip info"))
        if not ln["checked"]:
            out.append(t("span", "не проверена", class_="muted small", title="адрес собран по id пакета, не открывался"))
    return out


# ---------- страница ----------

def proto_names(cat: clients.Catalog) -> dict[str, str]:
    """Названия протоколов: как в плитках сервера, для неизвестных серверу — из каталога клиентов."""
    names = {p: d["title"] for p, d in cat.protocols.items()}
    names.update({m.id: m.short for m in manifests.load_all()[0]})
    return names


def app_names(cat: clients.Catalog, ids: list[str]) -> str:
    return " + ".join((cat.client(i) or {}).get("name", i) for i in ids)


def coverage_label(cat: clients.Catalog, plat: str, protocols: list[str], ids: list[str],
                   names: dict[str, str]) -> tuple[str, str]:
    """«все N» / «готово» (зелёная), «… с оговоркой» или «без X» (жёлтая): что набор приложений даёт по протоколам."""
    done, miss = groups.coverage(cat, plat, protocols, ids)
    if miss:
        return "без " + ", ".join(names.get(p, p) for p in miss), "warn"
    label = f"все {len(done)}" if len(done) > 1 else "готово"
    if groups.caveats(cat, plat, protocols, ids):
        return label + ", с оговоркой", "warn"
    return label, "ok"


def caveat_note(cat: clients.Catalog, plat: str, protocols: list[str], ids: list[str], names: dict[str, str]) -> str:
    """«Hysteria2 в Hiddify: <заметка каталога>» по протоколам, покрытым только с оговоркой (как жёлтое в «Все приложения»)."""
    return "; ".join(f"{names.get(p, p)} в {app}: {note}" if note else f"{names.get(p, p)} в {app}: с оговоркой"
                     for p, app, note in groups.caveats(cat, plat, protocols, ids))


def _version_cell(client: dict[str, Any], cache: dict[str, Any], platform: str | None = None) -> Any:
    plats = [platform] if platform else list(client["platforms"])
    if not any(_has_github(client, p) for p in plats):
        return t("span", "в магазине", class_="muted small", title="версия — на странице магазина, без опроса")
    v = cache["versions"].get(client["id"]) or {}
    if not v.get("version"):
        return t("span", "—", class_="muted", title=v.get("error") or "ещё не проверялась")
    return t("span", v["version"], title=f"релиз от {v['published']}" if v.get("published") else None,
             class_="mono")


def _check_controls(cache: dict[str, Any], csrf: str) -> Markup:
    checked = cache["checked"]
    can, why = clients.check_state()
    status = t("span", f"предложены последние версии · {ago(checked)}" if checked else "версии ещё не проверялись",
               class_="muted small", title="Свежие релизы с GitHub: что вышло, а не что стоит на устройстве")
    if can:
        btn = post_button("/clients/check", "Проверить сейчас", csrf, "btn small",
                          title="Спросить GitHub про свежие версии (не чаще раза в 10 минут)")
    else:
        btn = t("button", "Проверить сейчас", type="button", class_="btn small", disabled=True, title=why)
    return t("div", status, btn, class_="actions")


def _dev_nav(cat: clients.Catalog, dev: str | None) -> Markup:
    """Чипы устройств — ссылки ?dev= (без JS): таблица фильтруется, «Ставить» — про одно устройство."""
    links = [t("a", "Все", href="/clients", class_="active" if not dev else None)]
    links += [t("a", title, href=f"/clients?dev={p}", class_="active" if p == dev else None)
              for p, title in cat.platforms.items()]
    return t("nav", links, class_="seg", aria_label="Устройство")


def _install_rows(cat: clients.Catalog, cache: dict[str, Any], protos: list[str], devs: list[str]) -> Markup:
    """«Ставить»: по устройству — набор приложений (тот же подбор, что в мастере для людей, которые ставят сами), метка
    покрытия, у каждого приложения версия и ссылки."""
    plan = groups.suggest_set(cat, devs, protos, "self")
    names = proto_names(cat)
    rows = []
    for plat in devs:
        ids = plan.get(plat)
        title = cat.platforms[plat]
        if not ids:
            rows.append(t("div", t("strong", title, class_="dev-name"), t("span", "нет приложения под эти протоколы",
                                                                          class_="muted"), class_="dev-row"))
            continue
        label, kind = coverage_label(cat, plat, protos, ids, names)
        apps = []
        for cid in ids:
            c = cat.client(cid)
            if c is None:
                continue
            foreign = t("span", "нет в магазине РФ", class_="chip warn", title=FOREIGN_STORE) if cat.no_ru_store(c, plat) else None
            apps.append(t("div", t("strong", c["name"]), " ", _version_cell(c, cache, plat), " ", foreign,
                          t("div", _link_anchors(c["platforms"][plat]), class_="chips"), class_="app-line"))
        rows.append(t("div", t("div", t("strong", title, class_="dev-name"), t("span", app_names(cat, ids), class_="dev-set"),
                               t("span", label, class_=f"plat-sum {kind}"), class_="dev-head"), apps, class_="dev-row"))
    return t("div", rows, class_="dev-rows")


def _proto_cell(cat: clients.Catalog, c: dict[str, Any], protos: list[str]) -> Markup:
    """Протоколы клиента по каталогу: зелёный — заявлен, жёлтый — с оговоркой, красный — не работает; «стенд» — проверено прогоном."""
    chips = []
    for p in protos:
        st = c["protocols"].get(p)
        if not st:
            continue
        note = st.get("note") or ""
        stand = "проверено на стенде" in note
        kind = {"ok": "chip ok", "warn": "chip warn", "no": "chip bad"}.get(st["s"], "chip")
        chips.append(t("span", cat.protocols[p]["title"], stand and " · стенд" or None, class_=kind,
                       title=f"{clients.STATUS_TEXT[st['s']]}" + (f": {note}" if note else "")))
    return t("div", chips, class_="chips") if chips else t("span", "—", class_="muted")


def _apps_table(cat: clients.Catalog, cache: dict[str, Any], protos: list[str], dev: str | None) -> Markup:
    """«Все приложения»: строка = приложение; устройства, протоколы, версия; ссылки и заметка — в строке под спойлером."""
    rec = {cat.recommended(p, q)["id"] for p in cat.platforms for q in protos if cat.recommended(p, q)}  # type: ignore[index]
    rows = []
    for c in sorted((c for c in cat.clients if not dev or dev in c["platforms"]), key=lambda c: c["id"] not in rec):
        cells = []
        for plat in groups.MAIN_DEVICES:
            if plat not in c["platforms"]:
                cells.append(t("span", "—", class_="muted"))
            elif cat.no_ru_store(c, plat):
                cells.append(t("span", "✓!", class_="chip warn", title=FOREIGN_STORE))
            else:
                cells.append(t("span", "✓", class_="chip ok"))
        more = ", ".join(cat.platforms[p] for p in c["platforms"] if p not in groups.MAIN_DEVICES) or "—"
        links = [t("div", t("span", cat.platforms[p], class_="muted small"), " ", _link_anchors(ln), class_="chips")
                 for p, ln in c["platforms"].items()]
        rows.append([t("span", t("strong", c["name"]),
                       t("details", t("summary", "ссылки"), links, t("p", c["notes"], class_="hint") if c.get("notes") else None,
                         class_="more"), class_="app-cell"),
                     *cells, more, _proto_cell(cat, c, protos), _version_cell(c, cache)])
    head = ["приложение", *[cat.platforms[p] for p in groups.MAIN_DEVICES], "ещё", "протоколы", "версия"]
    return table(head, rows, stack=True, empty="нет приложений")


def clients_page(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return app.render(req, "Клиенты", [page_head("Клиенты"), card("Каталог", alert_list([("bad", str(e))]))],
                          active="/clients")
    cache = clients.load_cache()
    enabled = {m.id for m in manifests.load_all()[0] if m.enabled}
    protos = [p for p in cat.real_protocols() if p in enabled] if enabled else cat.real_protocols()
    dev = req.query.get("dev") if req.query.get("dev") in cat.platforms else None
    devs = [dev] if dev else list(groups.MAIN_DEVICES)

    install = card("Ставить", _install_rows(cat, cache, protos, devs),
                   help="Подбор для людей, которые ставят сами: приложения из магазинов, протоколы сервера. "
                        "В мастере «Новая группа» то же считается под выбранные протоколы.")
    table_card = card("Все приложения", t("p", LEGEND, class_="hint"), _apps_table(cat, cache, protos, dev),
                      help=UNVERIFIED)

    caveats = [("warn", f"{cat.protocols[p]['title']}: {cat.protocols[p]['caveat']}")
               for p in protos if cat.protocols[p].get("caveat")]
    why = [("warn", f"{c['name']} · {cat.protocols[p]['title']}: {st['note']}")
           for c in cat.clients for p, st in c["protocols"].items()
           if p in protos and st["s"] in ("no", "warn") and st.get("note")]
    notes = [alert_list(caveats) if caveats else None,
             t("details", t("summary", f"Почему ✕ и ! ({len(why)})"), alert_list(why), class_="more") if why else None]

    names = {c["id"]: c["name"] for c in cat.clients}
    failed = [names.get(k, k) for k, v in cache["versions"].items() if v.get("error")]
    foot = t("p", "Версии из GitHub: раз в сутки и по кнопке, страница в сеть не ходит"
             + (f" · не удалось: {', '.join(failed)}" if failed else "") + f" · каталог от {cat.raw['updated']}",
             class_="hint")
    intro = t("p", UNVERIFIED, " ", "Зелёное — заявлено, жёлтое — с оговоркой (наведите), «стенд» — проверено прогоном на сервере.",
              class_="hint")
    body = [page_head("Клиенты", "что ставить на устройство", _check_controls(cache, csrf)), _dev_nav(cat, dev), intro,
            install, table_card, *[n for n in notes if n], foot]
    return app.render(req, "Клиенты", body, active="/clients")


def check_now(app: "App", req: "Request") -> "Response":
    from .app import redirect
    ok, why = clients.request_check()
    req.session.flash("ok" if ok else "warn", why[:1].upper() + why[1:])
    return redirect("/clients")


# ---------- пакет раздачи ----------

@dataclass
class Item:
    """Протокол, который отдаёт клиент, и способ передачи ключа по умолчанию."""
    proto: str
    method: str
    tile: str


@dataclass
class Section:
    """Один клиент набора платформы и протоколы, за которые отвечает он (уже покрытые прежним клиентом — не повторяются)."""
    client: dict[str, Any]
    version: str | None
    links: list[dict[str, Any]]
    items: list[Item]
    install: str = ""
    steps: list[str] = field(default_factory=list)
    extras: list[Item] = field(default_factory=list)
    check: str = ""

    @property
    def proto(self) -> str:
        return self.items[0].proto

    @property
    def method(self) -> str:
        return self.items[0].method

    @property
    def tiles(self) -> str:
        return ", ".join(i.tile for i in self.items)


@dataclass
class Pack:
    platform: str
    platform_title: str
    sections: list[Section]
    apps: str = ""   # приложения «через VPN» в тексте шага (список группы или человека) — входят в подпись набора

    @property
    def message(self) -> str:
        """Текст инструкции платформы одним списком: сначала установка всех приложений, потом по порядку
        импорт и настройки каждого (с несколькими приложениями — с их названием), в конце проверка по
        первому протоколу. Начинается с {name}: имя подставляет тот, кто показывает текст человеку."""
        many = len(self.sections) > 1
        steps = [s.install for s in self.sections]
        for s in self.sections:
            for n, x in enumerate(s.steps):
                lead = (f"{s.client['name']} ({s.tiles}): " if n == 0 else f"{s.client['name']}: ") if many else ""
                steps.append(lead + x)
        steps.append(self.sections[0].check)
        return "\n".join([f"{NAME_TOKEN}, VPN на {self.platform_title}: что сделать",
                          *[f"{i}) {x}" for i, x in enumerate(steps, 1)]])


def _usable(proto: str, client_id: str, link: protolib.Link) -> bool:
    """Подходит ли ссылка этому клиенту: .conf — любому AWG-клиенту, vpn:// — только AmneziaVPN."""
    if link.kind == "file":
        return link.uri.endswith(".conf")
    if proto == "amneziawg":
        return link.uri.startswith("vpn://") and client_id == "amneziavpn"
    return not link.uri.startswith("vpn://")


def _qrable(link: protolib.Link) -> bool:
    return link.kind == "file" or (not link.uri.startswith("vpn://") and len(link.uri.encode("utf-8")) <= qr.MAX_BYTES)


def pick_method(proto: str, client: dict[str, Any], platform: str, links: list[protolib.Link]) -> str | None:
    """Как человеку передать ключ: на телефоне QR, на компьютере ссылка; клиент должен это уметь."""
    mine = [ln for ln in links if ln.variant == proto and _usable(proto, client["id"], ln)]
    have = {"qr": any(_qrable(ln) for ln in mine), "link": any(ln.kind == "uri" for ln in mine),
            "file": any(ln.kind == "file" for ln in mine)}
    order = ("link", "file", "qr") if platform in DESKTOP else ("qr", "link", "file")
    return next((m for m in order if m in client.get("import", {}) and have[m]), None)


def pick_link(proto: str, client: dict[str, Any], platform: str, links: list[protolib.Link], method: str) -> int | None:
    """Номер ссылки пользователя под способ передачи: ссылка — первая («Обычная»); из файлов Android берёт
    конфиг со списком приложений, остальные — общий."""
    mine = [(i, ln) for i, ln in enumerate(links) if ln.variant == proto and _usable(proto, client["id"], ln)]
    pool = {"link": [x for x in mine if x[1].kind == "uri"], "file": [x for x in mine if x[1].kind == "file"],
            "qr": [x for x in mine if _qrable(x[1])]}[method]

    def rank(x: tuple[int, protolib.Link]) -> int:
        if x[1].kind != "file":
            return 0
        return 0 if Path(x[1].uri).name.endswith("-android.conf") == (platform == "android") else 1

    return min(pool, key=rank)[0] if pool else None


def _tile_title(cat: clients.Catalog, mans: list[Any], proto: str) -> str:
    m = next((x for x in mans if x.id == proto), None)
    return (m.name if m else cat.protocols[proto]["title"]).partition(" (")[0]


def _assign(platform: str, links: list[protolib.Link], have: set[str], cs: list[dict[str, Any]],
            protos: list[str]) -> list[tuple[dict[str, Any], list[tuple[str, str]]]]:
    """Клиенты набора по порядку → [(клиент, [(протокол, способ передачи)])]. Протокол отдаёт первый клиент
    набора, который его умеет и которому есть что отправить; следующим он не повторяется, клиент без
    протоколов в пакет не попадает."""
    taken: set[str] = set()
    out = []
    for c in cs:
        mine = []
        for proto in protos:
            if proto in taken or proto not in have or c["protocols"].get(proto, {}).get("s") not in ("ok", "warn"):
                continue
            method = pick_method(proto, c, platform, links)
            if method:
                mine.append((proto, method))
                taken.add(proto)
        if mine:
            out.append((c, mine))
    return out


def _plan(cat: clients.Catalog, platform: str, links: list[protolib.Link], have: set[str],
          prefer: dict[str, list[str]] | None, order: list[str] | None) -> list[tuple[dict[str, Any], list[tuple[str, str]]]]:
    """Набор клиентов платформы и их протоколы. Набор группы главнее рекомендованных: протоколы идут по PRIORITY
    (выбранные группой; у «всех включённых» — все, что есть у человека).
    Группа выбрала клиентов, а для платформы — «не нужна» (нет записи): пакета нет. Если набор устарел
    (клиент убран из каталога или платформы) или не покрывает ничего включённого — рекомендованные
    клиенты по протоколам (если клиенты группы были выбраны — только в рамках протоколов группы)."""
    handoff = cat.raw["handoff"].get(platform, [])
    if prefer:
        ids = prefer.get(platform) or []
        if not ids:
            return []
        cs = [c for c in (cat.client(i) for i in ids) if c is not None and platform in c["platforms"]]
        got = _assign(platform, links, have, cs, order if order is not None else groups.by_priority(have))
        if got:
            return got
    protos = [p for p in handoff if not (prefer and order is not None) or p in order]
    if prefer and order is not None:
        protos = [p for p in order if p in protos]   # порядок группы — PRIORITY, не каталога
    rec: list[dict[str, Any]] = []
    for proto in protos:
        c = cat.recommended(platform, proto)
        if c is not None and c not in rec:
            rec.append(c)
    return _assign(platform, links, have, rec, protos)


# GitHub: страница последнего релиза (не список всех) и какой файл из «Assets» брать — человек ставит сам
GITHUB_FILE = {"android": "файл «.apk» (universal или arm64-v8a)",
               "windows": "файл для Windows с «x64» — установщик «.exe» (Setup), если его нет — «.zip»",
               "macos": "файл «.dmg»", "linux": "файл «.AppImage» или «.deb» с «x64» / «amd64»"}


def _install_target(ln: dict[str, Any], platform: str) -> str:
    url = ln["url"]
    if ln.get("kind") != "github":
        return url
    if re.fullmatch(r"https://github\.com/[^/]+/[^/]+/releases/?", url):
        url = url.rstrip("/") + "/latest"
    hint = GITHUB_FILE.get(platform)
    return f"{url} — в «Assets» скачайте {hint}" if hint else url


BRAVE_IDS = {"com.brave.browser", "brave.exe"}
VIA_LIST = "приложении из списка «через VPN»"
APPS_SHOWN = 6


def via_vpn_names(al: allowlist.Allowlist | None, platform: str, ids: list[str]) -> list[str]:
    """Названия приложений списка «через VPN» для текста: своё название, из каталога (до « — »), иначе идентификатор."""
    out = []
    for i in ids:
        name = (al.titles.get(i.lower(), "") if al else "") or allowlist.title_of(platform, i) or i
        out.append(name.split(" — ")[0])
    return list(dict.fromkeys(out))


def join_names(names: list[str]) -> str:
    if len(names) > APPS_SHOWN:
        names = names[:APPS_SHOWN - 1] + [f"ещё {len(names) - APPS_SHOWN + 1} из списка"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1]


def build_pack(cat: clients.Catalog, cache: dict[str, Any], platform: str, links: list[protolib.Link],
               mans: list[Any], prefer: dict[str, list[str]] | None = None, order: list[str] | None = None,
               stores: bool = False, al: allowlist.Allowlist | None = None, apps: list[str] | None = None) -> Pack | None:
    """prefer — наборы клиентов группы по платформам, order — протоколы группы (по PRIORITY);
    без них — рекомендованные клиенты и порядок раздачи из каталога. stores — ссылки магазинов первыми.
    apps — список «через VPN» этой платформы (группы или человека; None — общий из al): его названия идут
    в шаг выбора приложений, а проверка «в Brave» — только если Brave в нём есть."""
    have = {ln.variant for ln in links}
    plan = _plan(cat, platform, links, have, prefer, order)
    if not plan:
        return None
    if apps is None and al is not None and platform in allowlist.PLATFORMS:
        apps = al.common(platform)
    names = join_names(via_vpn_names(al, platform, apps)) if apps else ""
    sections = []
    for c, mine in plan:
        items = [Item(proto, method, _tile_title(cat, mans, proto)) for proto, method in mine]
        sec = Section(c, _version(c, platform, cache), _sorted_links(c["platforms"][platform], stores), items)
        ver = f" (версия {sec.version})" if sec.version else ""
        foreign = f" {FOREIGN_STORE}" if cat.no_ru_store(c, platform) else ""
        sec.install = f"Установите «{c['name']}»{ver}: {_install_target(sec.links[0], platform)}{foreign}"
        sec.steps += [c["import"][m] for m in dict.fromkeys(i.method for i in items)]
        for ex in c.get("extra", []):
            if ex["platform"] == platform and ex["proto"] in have:
                sec.steps.append(ex["text"])
                sec.extras.append(Item(ex["proto"], "file", _tile_title(cat, mans, ex["proto"])))
        app_step = cat.per_app_steps(c, platform)
        if app_step:
            sec.steps.append("Приложения через VPN: " + app_step.replace("{apps}", names or "нужные приложения"))
        # Brave — «приложение под VPN»: только там, где клиент умеет пускать в туннель выбранные приложения
        brave = (c.get("per_app") in ("config", "rules") and platform != "ios") or bool(app_step)
        browser = "Brave" if brave else "любом браузере"
        if brave and apps is not None and not any(i.lower() in BRAVE_IDS for i in apps):
            browser = VIA_LIST   # Brave нет в списке: в нём сайт пойдёт мимо VPN
        sec.check = (cat.raw["check"].get(sec.proto) or cat.raw["check"]["*"]).replace("{browser}", browser)
        sections.append(sec)
    return Pack(platform, cat.platforms[platform], sections, names)


def group_prefs(g: groups.Group | None) -> tuple[dict[str, list[str]], list[str] | None]:
    """Наборы клиентов и протоколы группы по PRIORITY для «Подключить» (у «всех включённых» — None: что есть у человека)."""
    if g is None:
        return {}, None
    prefer = {p: list(ids) for p, ids in g.clients.items()}
    return prefer, (None if g.all_protocols else groups.by_priority(g.protocols))


def synth_links(protos: list[str]) -> list[protolib.Link]:
    """Ссылки-заглушки по протоколам группы: пакет группы строится до того, как у кого-то есть ключи, и без
    них (ни в тексте, ни в способах передачи ничего личного)."""
    out = [protolib.Link(f"{p}://", "", p, "uri") for p in protos]   # hysteria2-obfs:// — вариант (Link.variant) без «obfs=salamander»
    if "amneziawg" in protos:
        out += [protolib.Link("vpn://", "", "amneziawg", "uri"), protolib.Link("amneziawg.conf", "", "amneziawg", "file")]
    return out + [protolib.Link(allowlist.V2RAYN_FILE, "", allowlist.V2RAYN_PROTO, "file")]


def pack_sig(pack: "Pack | None") -> str:
    """Подпись набора: какие клиенты и за какие протоколы отвечают. Версии не входят: текст от них не «устаревает»."""
    if pack is None:
        return "-"
    raw = ";".join(f"{s.client['id']}:{','.join(i.proto for i in s.items)}" for s in pack.sections) + "|" + pack.apps
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


@dataclass
class Ctx:
    """Всё, что нужно блокам «Подключить» и текстам групп; грузится один раз на страницу."""
    cat: clients.Catalog
    cache: dict[str, Any]
    mans: list[Any]
    packs: dict[tuple[str, str], "Pack | None"] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Ctx | None":
        try:
            cat = clients.load()
        except clients.ClientsError:
            return None
        return cls(cat, clients.load_cache(), manifests.load_all()[0])

    @cached_property
    def al(self) -> allowlist.Allowlist | None:
        try:
            return allowlist.Allowlist.load()
        except allowlist.AllowlistError:
            return None

    def apps_for(self, plat: str, g: groups.Group | None = None, user: str | None = None) -> list[str] | None:
        """Список «через VPN» платформы: человека (свой → группы → общий), иначе группы, иначе общий."""
        if self.al is None or plat not in allowlist.PLATFORMS:
            return None
        if user:
            return self.al.effective(plat, user)
        if g is not None and g.allowlist:
            return list(g.allowlist.get(plat, []))
        return self.al.common(plat)

    @cached_property
    def selectable(self) -> list[str]:
        return users.selectable_protocols()

    def group_pack(self, g: groups.Group, plat: str) -> "Pack | None":
        key = (g.id, plat)
        if key not in self.packs:
            prefer, order = group_prefs(g)
            self.packs[key] = build_pack(self.cat, self.cache, plat, synth_links(g.offered(self.selectable)), self.mans,
                                         prefer, order, store_first(g), self.al, self.apps_for(plat, g))
        return self.packs[key]

    def group_sig(self, g: groups.Group, plat: str) -> str:
        return pack_sig(self.group_pack(g, plat))

    def default_text(self, g: groups.Group, plat: str) -> str | None:
        """Текст по умолчанию: набор клиентов группы по всем её протоколам. None — для платформы пакета нет."""
        pack = self.group_pack(g, plat)
        return pack.message if pack else None

    def text(self, g: groups.Group, plat: str) -> str | None:
        """Текст группы для платформы: свой или по умолчанию."""
        return g.messages.get(plat) or self.default_text(g, plat)


# ---------- «Подключить»: приложения, QR и ссылки человека, текст группы ----------

@dataclass
class Key:
    """Что передать человеку по одному протоколу его приложением: QR, ссылка, файл."""
    title: str
    qr: int | None = None
    qr_tag: str = ""
    uri: str | None = None
    file: str | None = None


def _keys(sec: Section, platform: str, links: list[protolib.Link]) -> list[Key]:
    c = sec.client
    imp = c.get("import", {})
    out = []
    for it in sec.items:
        k = Key(it.tile)
        if "qr" in imp:
            k.qr = pick_link(it.proto, c, platform, links, "qr")
            k.qr_tag = links[k.qr].tag if k.qr is not None else ""
        if "link" in imp and (i := pick_link(it.proto, c, platform, links, "link")) is not None:
            k.uri = links[i].uri
        if "file" in imp and (i := pick_link(it.proto, c, platform, links, "file")) is not None:
            k.file = Path(links[i].uri).name
        out.append(k)
    for ex in sec.extras:
        f = next((Path(ln.uri).name for ln in links if ln.variant == ex.proto and ln.kind == "file"), None)
        out.append(Key(ex.tile, file=f))
    return [k for k in out if k.qr is not None or k.uri or k.file]


def _key_html(k: Key, name: str, kid: str) -> Markup:
    parts: list[Any] = [t("div", k.title, class_="key-name")]
    if k.qr is not None:
        parts.append(t("img", class_="qr", src=f"/users/{name}/qr/{k.qr}?p={k.qr_tag}", loading="lazy", width=160, height=160,
                       alt=f"QR: {k.title}"))
    if k.uri:
        parts.append(t("div", t("input", type="text", id=kid, value=k.uri, readonly=True, data_select=True,
                                aria_label=f"Ссылка: {k.title}"),
                       t("button", "Копировать", type="button", class_="btn small", data_copy=kid), class_="link-uri"))
    if k.file and FILE_NAME_RE.fullmatch(k.file):
        parts.append(t("a", "Скачать файл", href=f"/users/{name}/file/{k.file}", class_="btn small"))
    return t("div", parts, class_="key")


def _app_html(sec: Section, keys: list[Key], plat: str, name: str, cat: clients.Catalog, uid: str) -> Markup:
    head = t("div", t("strong", sec.client["name"]),
             t("span", sec.version, class_="mono muted") if sec.version else None,
             t("span", "нет в App Store РФ", class_="chip warn", title=FOREIGN_STORE) if cat.no_ru_store(sec.client, plat)
             else None,
             t("div", _link_anchors(sec.links, presorted=True), class_="chips"), class_="app-head")
    return t("div", head, t("div", [_key_html(k, name, f"k-{uid}{plat}-{sec.client['id']}-{n}")
                                    for n, k in enumerate(keys)], class_="keys"), class_="app")


def connect_panel(links: list[protolib.Link], name: str, ctx: Ctx, g: groups.Group | None,
                  uid: str = "") -> Markup | None:
    """Платформа (список) → приложения набора с версией и ссылкой, QR/ссылка/файл этого человека, текст
    группы с его именем. Без JS видны все платформы подряд; с JS список оставляет одну. uid — приставка id
    полей, если на странице несколько блоков."""
    if not links:
        return None
    prefer, order = group_prefs(g)
    panels: list[Markup] = []
    plats: list[tuple[str, str]] = []
    for plat, title in ctx.cat.platforms.items():
        pack = build_pack(ctx.cat, ctx.cache, plat, links, ctx.mans, prefer, order, store_first(g), ctx.al,
                          ctx.apps_for(plat, g, name))
        if pack is None:
            continue
        group_text = ctx.text(g, plat) if g else None
        own = bool(g and group_text and pack_sig(pack) != ctx.group_sig(g, plat))
        # у человека свои протоколы или другие приложения, чем в наборе группы: текст группы про другое
        text = ((None if own else group_text) or pack.message).replace(NAME_TOKEN, name)
        mid = f"msg-{uid}{plat}"
        keys = [_keys(s, plat, links) for s in pack.sections]
        apps = [_app_html(s, k, plat, name, ctx.cat, uid) for s, k in zip(pack.sections, keys)]
        msg = t("div",
                alert_list([("warn", MISMATCH)]) if own else None,
                t("label", "Текст сообщения — можно править", for_=mid),
                t("textarea", text, id=mid, rows=str(min(16, text.count("\n") + 3)), spellcheck="false"),
                t("div",
                  t("button", "Скопировать текст", type="button", class_="btn primary", data_copy=mid,
                    title="Копируется текст из поля выше, без ключей"),
                  t("label", t("input", type="checkbox", data_addlinks=mid), " добавить ссылки в текст",
                    class_="chk", data_links=True, hidden=True) if any(k.uri for ks in keys for k in ks) else None,
                  t("a", "текст для всей группы", href=f"/groups/{g.id}#texts", class_="small") if g else None,
                  class_="actions"),
                t("p", "Ключей в тексте нет: QR и ссылки выше отправьте отдельно.", class_="hint"), class_="msg")
        panels.append(t("section", t("h4", title, class_="plat-title"), apps, msg, class_="conn-plat", data_pp=plat))
        plats.append((plat, title))
    if not panels:
        return None
    pick = (t("div", t("label", "Платформа", for_=f"pl-{uid}"),
              t("select", [t("option", title, value=p, selected=n == 0) for n, (p, title) in enumerate(plats)],
                id=f"pl-{uid}", data_plat=True), class_="conn-pick", hidden=True) if len(panels) > 1 else None)
    return t("div", pick, panels, class_="conn")


def hints(ctx: Ctx) -> list[Markup]:
    """Строки под блоками «Подключить»: один раз на страницу, не в каждом блоке."""
    out = [t("p", SEND_WARN, class_="hint")]
    if any(not c["verified"]["device"] for c in ctx.cat.clients):
        out.append(t("p", UNVERIFIED, class_="hint"))
    return out


def connect_card(links: list[protolib.Link], name: str, ctx: Ctx | None, g: groups.Group | None,
                 uid: str = "") -> Markup | None:
    panel = connect_panel(links, name, ctx, g, uid) if ctx else None
    if panel is None:
        return None
    return card("Подключить", panel, hints(ctx),
                help="Приложения и текст — как у группы (менять на её странице). QR и ссылки — этого человека: "
                     "показывайте только ему.")
