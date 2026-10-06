"""Страница «Клиенты» (что ставить по протоколам и платформам) и блок «Что отправить» на странице
пользователя. Данные — каталог zoo/data/clients.json и кэш версий; в сеть страницы не ходят."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import clients, groups, manifests, protolib, qr
from .html import Markup, badge, card, t, table
from .views import ago, alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

DESKTOP = {"windows", "macos", "linux"}
MAIN_PLATFORMS = ("android", "ios", "windows")  # остальные — под «Другие платформы»
BADGE_KIND = {"ok": "ok", "warn": "warn", "no": "bad", "unk": "muted"}
LEGEND = "✓ заявлено поддерживаемым · ! с оговоркой · ✕ не работает · ? не проверено · — не заявлено"
UNVERIFIED = "Шаги и статусы — по коду и документации клиентов, на устройстве не проверялись."
SEND_WARN = ("Ссылки и QR — ключи доступа: не отправляйте через MAX и VK, лучше лично или мессенджером "
             "со сквозным шифрованием.")
SEND_WHAT = {"qr": "QR", "link": "ссылку", "file": "файл"}
FOREIGN_STORE = "В российском App Store его нет: нужен Apple ID другой страны, подделки с похожим названием не ставьте."


def _sorted_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Проверенные ссылки раньше собранных по id пакета."""
    return sorted(links, key=lambda ln: not ln["checked"])


def _has_github(client: dict[str, Any], platform: str) -> bool:
    return client.get("repo") is not None and any(ln["kind"] == "github" for ln in client["platforms"].get(platform, []))


def _version(client: dict[str, Any], platform: str, cache: dict[str, Any]) -> str | None:
    return clients.version_of(cache, client["id"]) if _has_github(client, platform) else None


def _link_anchors(links: list[dict[str, Any]]) -> list[Markup]:
    out: list[Markup] = []
    for ln in _sorted_links(links):
        out.append(t("a", clients.LINK_KINDS[ln["kind"]], href=ln["url"], target="_blank",
                     rel="noopener noreferrer", class_="chip info"))
        if not ln["checked"]:
            out.append(t("span", "не проверена", class_="muted small", title="адрес собран по id пакета, не открывался"))
    return out


# ---------- страница ----------

def _status_cell(client: dict[str, Any], proto: str) -> Any:
    st = client["protocols"].get(proto)
    if not st:
        return t("span", "—", class_="muted")
    return t("span", clients.STATUS_MARK[st["s"]], class_=f"badge {BADGE_KIND[st['s']]}",
             title=f"{clients.STATUS_TEXT[st['s']]}" + (f": {st['note']}" if st.get("note") else ""))


def _version_cell(client: dict[str, Any], cache: dict[str, Any], platform: str | None = None) -> Any:
    plats = [platform] if platform else list(client["platforms"])
    if not any(_has_github(client, p) for p in plats):
        return t("span", "в магазине", class_="muted small", title="версия — на странице магазина, без опроса")
    v = cache["versions"].get(client["id"]) or {}
    if not v.get("version"):
        return t("span", "—", class_="muted", title=v.get("error") or "ещё не проверялась")
    return t("span", v["version"], title=f"релиз от {v['published']}" if v.get("published") else None,
             class_="mono")


def clients_page(app: "App", req: "Request") -> "Response":
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return app.render(req, "Клиенты", [page_head("Клиенты"), card("Каталог", alert_list([("bad", str(e))]))],
                          active="/clients")
    cache = clients.load_cache()
    enabled = {m.id for m in manifests.load_all()[0] if m.enabled}
    protos = [p for p in cat.real_protocols() if p in enabled] if enabled else cat.real_protocols()

    cards: list[Markup] = []
    for plat, plat_title in cat.platforms.items():
        by_client: dict[str, list[str]] = {}
        for pid in protos:
            c = cat.recommended(plat, pid)
            if c:
                by_client.setdefault(c["id"], []).append(pid)
        if not by_client:
            continue
        rows = []
        for cid, pids in by_client.items():
            c = cat.client(cid)
            assert c is not None
            rows.append([t("strong", c["name"]),
                         t("div", [t("span", cat.protocols[p]["title"], class_="chip") for p in pids], class_="chips"),
                         _version_cell(c, cache, plat),
                         t("div", _link_anchors(c["platforms"][plat]), class_="chips")])
        cards.append(card(plat_title, table(["клиент", "для протоколов", "версия", "скачать"], rows, stack=True)))

    names = {c["id"]: c["name"] for c in cat.clients}
    head = ["клиент", "платформы", *[cat.protocols[p]["title"] for p in protos], "приложения через VPN", "версия"]
    rows = []
    for c in cat.clients:
        rows.append([
            t("span", t("strong", c["name"]), t("span", c["notes"], class_="sub") if c.get("notes") else None),
            ", ".join(cat.platforms[p] for p in c["platforms"]),
            *[_status_cell(c, p) for p in protos],
            t("span", cat.raw["per_app"][c["per_app"]], class_="small"),
            _version_cell(c, cache)])
    matrix = card("Что умеют клиенты", t("p", LEGEND, class_="hint"), table(head, rows, stack=True),
                  help=UNVERIFIED)

    caveats = [("warn", f"{cat.protocols[p]['title']}: {cat.protocols[p]['caveat']}")
               for p in protos if cat.protocols[p].get("caveat")]
    why = [("warn", f"{c['name']} · {cat.protocols[p]['title']}: {st['note']}")
           for c in cat.clients for p, st in c["protocols"].items()
           if p in protos and st["s"] in ("no", "warn") and st.get("note")]
    notes = [alert_list(caveats) if caveats else None,
             t("details", t("summary", f"Почему ✕ и ! ({len(why)})"), alert_list(why), class_="more") if why else None]

    checked = cache["checked"]
    failed = [names.get(k, k) for k, v in cache["versions"].items() if v.get("error")]
    foot = t("p", "Версии из GitHub: " + (f"проверены {ago(checked)}" if checked else
             "ещё не проверялись (sudo zoo clients --check-upstream)") + " · раз в сутки, страница в сеть не ходит"
             + (f" · не удалось: {', '.join(failed)}" if failed else "") + f" · каталог от {cat.raw['updated']}",
             class_="hint")
    body = [page_head("Клиенты", "что ставить на устройство"), t("p", UNVERIFIED, class_="hint"), *cards, matrix,
            *[n for n in notes if n], foot]
    return app.render(req, "Клиенты", body, active="/clients")


# ---------- пакет раздачи ----------

@dataclass
class Pack:
    platform: str
    platform_title: str
    proto: str
    client: dict[str, Any]
    version: str | None
    links: list[dict[str, Any]]
    method: str
    tile: str
    tab: str | None
    steps: list[str] = field(default_factory=list)
    sends: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        lines = [f"VPN на {self.platform_title}: что сделать"]
        lines += [f"{i}. {s}" for i, s in enumerate(self.steps, 1)]
        return "\n".join(lines)


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
    mine = [ln for ln in links if ln.proto_id == proto and _usable(proto, client["id"], ln)]
    have = {"qr": any(_qrable(ln) for ln in mine), "link": any(ln.kind == "uri" for ln in mine),
            "file": any(ln.kind == "file" for ln in mine)}
    order = ("link", "file", "qr") if platform in DESKTOP else ("qr", "link", "file")
    return next((m for m in order if m in client.get("import", {}) and have[m]), None)


def _tile_title(cat: clients.Catalog, mans: list[Any], proto: str) -> str:
    m = next((x for x in mans if x.id == proto), None)
    return (m.name if m else cat.protocols[proto]["title"]).partition(" (")[0]


def _pick(platform: str, links: list[protolib.Link], proto: str, c: dict[str, Any] | None,
          have: set[str]) -> tuple[str, dict[str, Any], str] | None:
    method = pick_method(proto, c, platform, links) if c and proto in have else None
    return (proto, c, method) if c and method else None


def _choose(cat: clients.Catalog, platform: str, links: list[protolib.Link], have: set[str],
            prefer: dict[str, str] | None, order: list[str] | None) -> tuple[str, dict[str, Any], str] | None:
    """(протокол, клиент, способ передачи). Клиент, выбранный в группе, главнее рекомендованного: берём
    первый протокол группы (порядок группы, первый — основной; у «всех включённых» — порядок раздачи
    каталога, затем остальные включённые), который он умеет и для которого есть что отправить. Группа
    выбрала клиентов, а для платформы — «не нужен» (нет записи): пакета нет. Если выбранный клиент
    устарел (убран из каталога или платформы) или не покрывает ничего включённого — рекомендованный
    клиент (если клиент группы был выбран — только в рамках протоколов группы)."""
    handoff = cat.raw["handoff"].get(platform, [])
    if prefer:
        cid = prefer.get(platform, "")
        if not cid:
            return None
        c = cat.client(cid)
        if c is not None and platform in c["platforms"]:
            for proto in order or [*handoff, *sorted(have - set(handoff))]:
                if c["protocols"].get(proto, {}).get("s") in ("ok", "warn"):
                    got = _pick(platform, links, proto, c, have)
                    if got:
                        return got
    for proto in handoff:
        if not (prefer and order) or proto in order:
            got = _pick(platform, links, proto, cat.recommended(platform, proto), have)
            if got:
                return got
    return None


def build_pack(cat: clients.Catalog, cache: dict[str, Any], platform: str, links: list[protolib.Link],
               mans: list[Any], prefer: dict[str, str] | None = None, order: list[str] | None = None) -> Pack | None:
    """prefer — клиенты группы по платформам, order — протоколы группы по порядку (первый — основной);
    без них — рекомендованный клиент и порядок раздачи из каталога."""
    have = {ln.proto_id for ln in links}
    chosen = _choose(cat, platform, links, have, prefer, order)
    if chosen is None:
        return None
    proto, c, method = chosen
    tabs = cat.raw.get("tabs", {}).get(proto)
    pack = Pack(platform, cat.platforms[platform], proto, c, _version(c, platform, cache),
                _sorted_links(c["platforms"][platform]), method, _tile_title(cat, mans, proto),
                (tabs.get(platform) or tabs.get("*")) if tabs else None)
    ver = f" (версия {pack.version})" if pack.version else ""
    foreign = f" {FOREIGN_STORE}" if cat.no_ru_store(c, platform) else ""
    pack.steps.append(f"Скачайте «{c['name']}»{ver}: {pack.links[0]['url']}{foreign}")
    pack.steps.append(c["import"][method])
    pack.sends.append(f"{SEND_WHAT[method]} — плитка «{pack.tile}»" + (f", вкладка «{pack.tab}»" if pack.tab else ""))
    for ex in c.get("extra", []):
        if ex["platform"] == platform and ex["proto"] in have:
            pack.steps.append(ex["text"])
            pack.sends.append(f"файл — плитка «{_tile_title(cat, mans, ex['proto'])}»")
    app_step = cat.per_app_steps(c, platform)
    if app_step:
        pack.steps.append("Приложения через VPN: " + app_step)
    # Brave — «приложение под VPN»: только там, где клиент умеет пускать в туннель выбранные приложения
    brave = (c.get("per_app") in ("config", "rules") and platform != "ios") or bool(app_step)
    browser = "Brave" if brave else "любом браузере"
    check = cat.raw["check"].get(proto) or cat.raw["check"]["*"]
    pack.steps.append(check.replace("{browser}", browser))
    return pack


def group_prefs(g: groups.Group | None) -> tuple[dict[str, str], list[str] | None]:
    """Клиенты и порядок протоколов группы для «Что отправить» (у «всех включённых» порядок — из каталога)."""
    if g is None:
        return {}, None
    if g.all_protocols:
        return dict(g.clients), None
    return dict(g.clients), list(g.protocols)


def handoff_card(links: list[protolib.Link], prefer: dict[str, str] | None = None, uid: str = "",
                 heading: str = "Что отправить", order: list[str] | None = None) -> Markup | None:
    """«Что отправить»: по платформе — клиент (ссылка, версия), что прислать, одно сообщение.
    prefer и order — клиенты и протоколы группы (см. build_pack); uid — приставка id полей, если на странице
    несколько пакетов."""
    if not links:
        return None
    try:
        cat = clients.load()
    except clients.ClientsError:
        return None
    cache = clients.load_cache()
    mans = manifests.load_all()[0]
    blocks: dict[bool, list[Markup]] = {True: [], False: []}
    unverified = False
    for plat in cat.platforms:
        pack = build_pack(cat, cache, plat, links, mans, prefer, order)
        if pack is None:
            continue
        unverified = unverified or not pack.client["verified"]["device"]
        mid = f"msg-{uid}{plat}"
        title = [pack.platform_title, " · ", t("strong", pack.client["name"]),
                 t("span", f" {pack.version}", class_="muted") if pack.version else None]
        blocks[plat in MAIN_PLATFORMS].append(t(
            "div", t("h3", title, class_="sub-h"),
            t("ul", t("li", "Скачать: ", _link_anchors(pack.links)),
              t("li", "Отправить: ", "; ".join(pack.sends)), class_="steps"),
            t("textarea", pack.message, id=mid, hidden=True, readonly=True),
            t("div", t("button", "Скопировать сообщение", type="button", class_="btn", data_copy=mid,
                       title="Инструкция без ключей: QR, ссылку или файл отправьте отдельно"), class_="actions"),
            class_="pack"))
    if not blocks[True] and not blocks[False]:
        return None
    other = t("details", t("summary", "Другие платформы"), blocks[False], class_="more") if blocks[False] else None
    return card(heading, blocks[True], other, t("p", SEND_WARN, class_="hint"),
                t("p", UNVERIFIED, class_="hint") if unverified else None,
                help="Сообщение — только инструкция, без ключей. Сами QR, ссылки и файлы — в плитках выше.")
