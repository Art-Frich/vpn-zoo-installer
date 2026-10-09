"""Страница «Приложения» (справочник: какое приложение что умеет) и блок «Подключить»: приложения платформы,
QR и ссылки конкретного человека и одна инструкция на группу. Данные — каталог zoo/data/clients.json и кэш
версий; в сеть страницы не ходят."""

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
LEGEND = "зелёный — заявлено · «с оговоркой» — подробности при наведении · серый — не проверено · — не заявлено"
UNVERIFIED = "Шаги и статусы — по коду и документации приложений, на устройстве не проверялись."
RECOMMEND_BASIS = "по документации и исследованию 04.10.2026, на устройстве не проверено"
SEND_WARN = ("Ссылки и QR — ключи доступа: не отправляйте через MAX и VK, лучше лично или мессенджером "
             "со сквозным шифрованием.")
FOREIGN_STORE = "В российском App Store его нет: нужен Apple ID другой страны, подделки с похожим названием не ставьте."
# то же по меткам foreign_note, для строки «Оговорки»: метка уже сказана, здесь — что с этим делать
FOREIGN_WHY = {"нет в App Store РФ": "нужен Apple ID другой страны, подделки с похожим названием не ставьте",
               "нужен иностранный Apple ID": "в российском App Store приложения нет, подделки с похожим названием не ставьте"}
NAME_TOKEN = "{name}"      # так подстановка имени хранится в группе
NAME_TOKEN_RU = "{имя}"    # так — показывается и вводится в редакторе; принимаются оба
MISMATCH = "инструкция группы не подходит этому человеку — показана его собственная"
NO_KEYS = "Ключей в инструкции нет: QR и ссылки у каждого свои."
STALE = "набор приложений изменился — проверьте инструкцию"
NO_APPS = "Приложения не выбраны"
FILE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def fill_name(text: str, label: str) -> str:
    """Подставить имя человека вместо «{name}» и «{имя}»."""
    return text.replace(NAME_TOKEN, label).replace(NAME_TOKEN_RU, label)


def editor_text(text: str) -> str:
    """Текст для показа и редактора: «{имя}» вместо внутреннего «{name}»."""
    return text.replace(NAME_TOKEN, NAME_TOKEN_RU)


def stored_text(text: str) -> str:
    """Текст для хранения: внутренний «{name}» вместо «{имя}»."""
    return text.replace(NAME_TOKEN_RU, NAME_TOKEN)


def no_apps(g: "groups.Group | None") -> Markup:
    """«Приложения не выбраны → Настроить»: у группы нет набора, раздавать нечего."""
    return t("span", NO_APPS, " ", t("a", "Настроить →", href=f"/groups/{g.id}#settings", data_swap=True) if g else None,
             class_="muted")


def _sorted_links(links: list[dict[str, Any]], store_first: bool = False) -> list[dict[str, Any]]:
    """Проверенные ссылки раньше собранных по id пакета; store_first — магазины раньше всего (люди ставят сами, D49)."""
    return sorted(links, key=lambda ln: (store_first and ln["kind"] not in groups.LINK_STORE_KINDS, not ln["checked"]))


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
            out.append(t("span", "не проверена", class_="muted small"))
    return out


# ---------- страница ----------

def proto_names(cat: clients.Catalog) -> dict[str, str]:
    """Названия протоколов на всех экранах (manifests.proto_title); незнакомым — short манифеста или название каталога."""
    names = {p: manifests.TITLES.get(p, d["title"]) for p, d in cat.protocols.items()}
    names.update({m.id: manifests.TITLES.get(m.id, m.short) for m in manifests.load_all()[0]})
    return names


def supports_list(cat: clients.Catalog, client: dict[str, Any], platform: str) -> bool:
    """Умеет ли приложение пускать в туннель только приложения «через VPN» на платформе (иначе через VPN идёт всё)."""
    return client.get("per_app") in ("config", "rules") or bool(cat.per_app_steps(client, platform))


def foreign_note(cat: clients.Catalog, client: dict[str, Any], plat: str, admin: bool = False) -> str:
    """Подпись про магазин: людям, которые ставят сами, — «нет в App Store РФ»; ставит ИТ — на iPhone «нужен иностранный
    Apple ID» (Android и компьютеры у ИТ без магазина); приложение есть в российском магазине — пусто."""
    if not cat.no_ru_store(client, plat):
        return ""
    if admin:
        return "нужен иностранный Apple ID" if plat == "ios" else ""
    return f"нет в {'App Store' if plat == 'ios' else 'магазине'} РФ"


def rules_text(cat: clients.Catalog) -> str:
    """Шаг импорта файла правил v2rayN: тот же текст, что в инструкции (extra каталога)."""
    return next((ex["text"] for c in cat.clients for ex in c.get("extra", []) if ex["proto"] == allowlist.V2RAYN_PROTO), "")


def app_names(cat: clients.Catalog, ids: list[str]) -> str:
    return " + ".join((cat.client(i) or {}).get("name", i) for i in ids)


def coverage_label(cat: clients.Catalog, plat: str, protocols: list[str], ids: list[str],
                   names: dict[str, str]) -> tuple[str, str]:
    """Один чип покрытия: «все протоколы» (ok), «с оговоркой» или «нет Hysteria2» (warn)."""
    _, miss = groups.coverage(cat, plat, protocols, ids)
    if miss:
        return "нет " + ", ".join(names.get(p, p) for p in miss), "warn"
    if groups.caveats(cat, plat, protocols, ids):
        return "с оговоркой", "warn"
    return "все протоколы", "ok"


def caveat_items(cat: clients.Catalog, picked: dict[str, list[str]], protocols: list[str],
                 names: dict[str, str]) -> list[tuple[str, str]]:
    """Оговорки набора приложений по устройствам, каждая один раз: [(строка «Hiddify: Hysteria2 без проверки
    сертификата», полная заметка каталога)]. Одно название у приложения и протокола не повторяется."""
    out: dict[str, str] = {}
    for plat, ids in picked.items():
        for p, app, short, note in groups.caveats(cat, plat, protocols, ids):
            title = names.get(p, p)
            out.setdefault(f"{app}: {short}" if title == app else f"{app}: {title} {short}", note)
    return list(out.items())


def _version_cell(client: dict[str, Any], cache: dict[str, Any], platform: str | None = None) -> Any:
    plats = [platform] if platform else list(client["platforms"])
    if not any(_has_github(client, p) for p in plats):
        return t("span", "в магазине", class_="muted small")
    v = cache["versions"].get(client["id"]) or {}
    if not v.get("version"):
        return t("span", "—", class_="muted", title=v.get("error") or None)
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


def _note_where(cat: clients.Catalog, protos: list[str]) -> dict[str, dict[str, list[str]]]:
    """Заметки каталога у протоколов, которые не «не работает»: {заметка: {приложение: [протоколы]}}."""
    names = proto_names(cat)
    out: dict[str, dict[str, list[str]]] = {}
    for c in cat.clients:
        for p in protos:
            st = c["protocols"].get(p)
            if st and st["s"] != "no" and st.get("note"):
                out.setdefault(st["note"], {}).setdefault(c["name"], []).append(names.get(p, p))
    return out


def _shared_notes(where: dict[str, dict[str, list[str]]]) -> dict[str, str]:
    """Заметки, одинаковые у нескольких приложений или протоколов: {заметка: строка «заметка — Приложение: протоколы»}.
    На чипах их нет (подсказка не повторяется) — строка под таблицей, каждая один раз."""
    return {note: f"{note} — " + "; ".join(f"{app}: {', '.join(ps)}" for app, ps in apps.items())
            for note, apps in where.items() if sum(len(ps) for ps in apps.values()) > 1}


def _broken_line(cat: clients.Catalog, protos: list[str]) -> str:
    """«Не работает: VLESS Vision, AmneziaWG — Hiddify, sing-box · VLESS Vision — Karing»: одна строка на таблицу,
    приложения с одинаковым списком — вместе."""
    names = proto_names(cat)
    by: dict[tuple[str, ...], list[str]] = {}
    for c in cat.clients:
        bad = tuple(names.get(p, p) for p in protos if c["protocols"].get(p, {}).get("s") == "no")
        if bad:
            by.setdefault(bad, []).append(c["name"])
    return "Не работает: " + " · ".join(f"{', '.join(bad)} — {', '.join(apps)}" for bad, apps in by.items()) if by else ""


def _proto_cell(cat: clients.Catalog, c: dict[str, Any], protos: list[str], shared: dict[str, str]) -> Markup:
    """Протоколы приложения по каталогу: зелёный — заявлен, жёлтый — с оговоркой (наведите), «стенд» — проверено
    прогоном. Не работающих здесь нет: они одной строкой под таблицей. Подсказка — только своя заметка: статус
    говорит цвет (легенда), а заметка, общая для нескольких приложений, — строка под таблицей."""
    chips = []
    names = proto_names(cat)
    for p in protos:
        st = c["protocols"].get(p)
        if not st or st["s"] == "no":
            continue
        note = st.get("note") or ""
        stand = "проверено на стенде" in note
        kind = {"ok": "chip ok", "warn": "chip warn"}.get(st["s"], "chip")
        chips.append(t("span", names.get(p, p), " · стенд" if stand else None, class_=kind,
                       title=note if note and note not in shared else None))
    return t("div", chips, class_="chips") if chips else t("span", "—", class_="muted")

def _apps_table(cat: clients.Catalog, cache: dict[str, Any], protos: list[str], dev: str | None,
                shared: dict[str, str]) -> Markup:
    """«Все приложения»: строка = приложение; устройства, протоколы, версия; ссылки и заметка — в строке под спойлером."""
    rec = {cat.recommended(p, q)["id"] for p in cat.platforms for q in protos if cat.recommended(p, q)}  # type: ignore[index]
    rows = []
    for c in sorted((c for c in cat.clients if not dev or dev in c["platforms"]), key=lambda c: c["id"] not in rec):
        cells = []
        for plat in groups.MAIN_DEVICES:
            if plat not in c["platforms"]:
                cells.append(t("span", "—", class_="muted"))
            elif cat.no_ru_store(c, plat):
                cells.append(t("span", f"нет в {'App Store' if plat == 'ios' else 'магазине'} РФ", class_="chip warn"))
            else:
                cells.append(t("span", "✓", class_="chip ok"))
        more = ", ".join(cat.platforms[p] for p in c["platforms"] if p not in groups.MAIN_DEVICES) or "—"
        links = [t("div", t("span", cat.platforms[p], class_="muted small"), " ", _link_anchors(ln), class_="chips")
                 for p, ln in c["platforms"].items()]
        rows.append([t("span", t("strong", c["name"]),
                       t("details", t("summary", "ссылки"), links, t("p", c["notes"], class_="hint") if c.get("notes") else None,
                         class_="more"), class_="app-cell"),
                     *cells, more, _proto_cell(cat, c, protos, shared), _version_cell(c, cache)])
    head = ["приложение", *[cat.platforms[p] for p in groups.MAIN_DEVICES], "ещё", "протоколы", "версия"]
    return table(head, rows, stack=True, empty="нет приложений")


def clients_page(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return app.render(req, "Приложения", [page_head("Приложения"), card("Каталог", alert_list([("bad", str(e))]))],
                          active="/clients")
    cache = clients.load_cache()
    enabled = {m.id for m in manifests.load_all()[0] if m.enabled}
    protos = [p for p in cat.real_protocols() if p in enabled] if enabled else cat.real_protocols()
    dev = req.query.get("dev") if req.query.get("dev") in cat.platforms else None
    names = proto_names(cat)

    shared = _shared_notes(_note_where(cat, protos))
    broken = _broken_line(cat, protos)
    foreign = any(cat.no_ru_store(c, plat) for c in cat.clients for plat in c["platforms"] if plat == "ios")
    table_card = card("Все приложения", t("p", LEGEND, class_="hint"), _apps_table(cat, cache, protos, dev, shared),
                      t("p", broken, class_="hint") if broken else None,
                      t("p", f"Нет в App Store РФ: {FOREIGN_WHY['нет в App Store РФ']}.", class_="hint") if foreign else None,
                      [t("p", line, class_="hint") for line in shared.values()],
                      help=UNVERIFIED)

    caveats = [("warn", f"{names.get(p, p)}: {cat.protocols[p]['caveat']}")
               for p in protos if cat.protocols[p].get("caveat")]
    why = [("warn", f"{c['name']} · {names.get(p, p)}: {st['note']}")
           for c in cat.clients for p, st in c["protocols"].items()
           if p in protos and st["s"] in ("no", "warn") and st.get("note")]
    notes = [alert_list(caveats) if caveats else None,
             t("details", t("summary", f"Почему «с оговоркой» и «не работает» ({len(why)})"), alert_list(why), class_="more") if why else None]

    ids = {c["id"]: c["name"] for c in cat.clients}
    failed = [ids.get(k, k) for k, v in cache["versions"].items() if v.get("error")]
    foot = t("p", "Версии из GitHub: раз в сутки и по кнопке, страница в сеть не ходит"
             + (f" · не удалось: {', '.join(failed)}" if failed else "") + f" · каталог от {cat.raw['updated']}",
             class_="hint")
    intro = t("p", t("a", "Что ставить группе — в «Подключить людей» и на странице группы →", href="/connect/new", data_swap=True),
              class_="hint")
    body = [page_head("Приложения", "справочник: что умеет каждое", _check_controls(cache, csrf)), _dev_nav(cat, dev), intro,
            table_card, *[n for n in notes if n], foot]
    return app.render(req, "Приложения", body, active="/clients")


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
    foreign: bool = False   # на этой платформе приложения нет в российском магазине (iPhone: нужен иностранный Apple ID)
    via: str = ""           # что идёт через VPN: apps, ru-direct, all (справочник via каталога); пусто — не описано
    paper: list[str] | None = None   # шаги для бумажной карточки (импорт QR-кодом); None — с бумаги ключ не перенести

    @property
    def proto(self) -> str:
        return self.items[0].proto

    @property
    def method(self) -> str:
        return self.items[0].method

    @property
    def tiles(self) -> str:
        return ", ".join(i.tile for i in self.items)


VIA_PREFIX = "Через VPN — "


@dataclass
class Pack:
    platform: str
    platform_title: str
    sections: list[Section]
    apps: str = ""   # приложения «через VPN» в тексте шага (список группы или человека) — входят в подпись набора
    admin: bool = False   # приложения ставит ИТ: людям шагов установки нет
    via: str = ""    # строка «Через VPN — …» (что идёт через VPN у главного приложения); пусто — не описано
    before: list[str] = field(default_factory=list)   # общие шаги до установки (браузер Brave)
    after: list[str] = field(default_factory=list)    # общие шаги перед проверкой (настройки Android)
    report: str = ""  # что прислать администратору, если не работает
    one_on: str = ""  # шаблон «держите включённым одно приложение» ({first}, {rest}) — когда приложений два и больше

    def steps(self, paper: bool = False) -> list[str]:
        """Шаги без заголовка: Brave и установка (если люди ставят сами), импорт и настройка каждого приложения, какое
        держать включённым (их два), проверка, что прислать. paper — для бумажной карточки: только приложения, ключ
        которых переносится QR-кодом (остальные — в сообщении, paper_rest); таких нет — пусто."""
        secs = [s for s in self.sections if s.paper is not None] if paper else self.sections
        if not secs:
            return []
        out = [] if self.admin else [*self.before, *(s.install for s in secs)]
        for s in secs:
            out += (s.paper or []) if paper else s.steps
        if len(secs) > 1 and self.one_on:
            names = [f"«{s.client['name']}»" for s in secs]
            out.append(self.one_on.replace("{first}", names[0]).replace("{rest}", " или ".join(names[1:])))
        out += [*self.after, secs[0].check]
        if self.report:
            out.append(self.report)
        return out

    @property
    def paper_rest(self) -> list[str]:
        """Приложения, ключ которых с бумаги не перенести (длинная ссылка, файл): им — сообщение."""
        return [s.client["name"] for s in self.sections if s.paper is None]

    @property
    def message(self) -> str:
        """Инструкция платформы одним списком (steps). Первая строка начинается с {name}: имя подставляет тот, кто
        показывает текст человеку; вторая — что идёт через VPN. Протоколов в тексте нет; только если приложений на
        устройстве два, ключи названы, как подписаны у человека («ссылку «VLESS XHTTP»»): какой ключ в какое."""
        steps = self.steps()
        if self.admin:
            names = f"«{self.sections[0].client['name']}»"   # второе приложение — запасное (шаг one_on)
            abroad = " и ".join(f"«{s.client['name']}»" for s in self.sections if s.foreign)
            head = (f"{NAME_TOKEN}. Установите {abroad} с иностранного Apple ID. Включите VPN в {names}." if abroad
                    else f"{NAME_TOKEN}, VPN уже установлен. Включите его в {names}.")
        else:
            head = f"{NAME_TOKEN}, VPN на {self.platform_title}: что сделать"
        return "\n".join([head, *([self.via] if self.via else []), *[f"{i}) {x}" for i, x in enumerate(steps, 1)]])


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
    """Как человеку передать ключ: ссылкой или файлом из сообщения (оно открыто на том же устройстве — QR своим
    же телефоном не отсканировать); QR — только если иначе приложение ключ не берёт. Клиент должен это уметь."""
    mine = [ln for ln in links if ln.variant == proto and _usable(proto, client["id"], ln)]
    have = {"qr": any(_qrable(ln) for ln in mine), "link": any(ln.kind == "uri" for ln in mine),
            "file": any(ln.kind == "file" for ln in mine)}
    return next((m for m in ("link", "file", "qr") if m in client.get("import", {}) and have[m]), None)


def can_qr(proto: str, client: dict[str, Any], platform: str, links: list[protolib.Link]) -> bool:
    """Ключ можно отдать QR-кодом (другой экран, бумага): приложение его сканирует и ключ в QR помещается."""
    return "qr" in client.get("import", {}) and pick_link(proto, client, platform, links, "qr") is not None


def pick_link(proto: str, client: dict[str, Any], platform: str, links: list[protolib.Link], method: str) -> int | None:
    """Номер ссылки пользователя под способ передачи: ссылка — первая; из файлов Android берёт
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
    return manifests.TITLES.get(proto) or (m.short if m else cat.protocols[proto]["title"])


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
    """Набор приложений платформы и их протоколы. Набор группы — единственный источник: рендерится ровно он, протоколы
    идут по PRIORITY (выбранные группой; у «всех включённых» — все, что есть у человека). Нет записи для платформы
    (устройство «не нужно») или у группы нет набора — пакета нет. Без группы (prefer=None) — рекомендованные каталогом
    приложения по протоколам раздачи."""
    if prefer is not None:
        cs = [c for c in (cat.client(i) for i in prefer.get(platform) or []) if c is not None and platform in c["platforms"]]
        return _assign(platform, links, have, cs, order if order is not None else groups.by_priority(have))
    protos = list(cat.raw["handoff"].get(platform, []))
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


def _install_target(ln: dict[str, Any], platform: str, client: dict[str, Any] | None = None) -> str:
    url = ln["url"]
    if ln.get("kind") != "github":
        return url
    if re.fullmatch(r"https://github\.com/[^/]+/[^/]+/releases/?", url):
        url = url.rstrip("/") + "/latest"
    hint = ((client or {}).get("asset") or {}).get(platform) or GITHUB_FILE.get(platform)
    return f"{url} — в «Assets» скачайте {hint}" if hint else url


def has_brave(apps: list[str] | None) -> bool:
    """Brave в списке «через VPN» (Android или Windows)."""
    b = allowlist.BY_KEY["brave"]
    ids = {x.lower() for x in apps or []}
    return any((getattr(b, p) or "").lower() in ids for p in allowlist.PLATFORMS)


def via_line(cat: clients.Catalog, mode: str, names: str) -> str:
    """«Через VPN — только Brave, Telegram и Claude, остальное напрямую»: что идёт через VPN у приложения."""
    if not mode:
        return ""
    if mode == "apps":
        return f"{VIA_PREFIX}только {names or 'приложения из списка'}, остальное напрямую."
    return f"{VIA_PREFIX}{cat.raw['via'][mode]}."


def via_vpn_names(al: allowlist.Allowlist | None, platform: str, ids: list[str]) -> list[str]:
    """Названия приложений списка «через VPN» для текста: своё название, из каталога (до « — »), иначе идентификатор."""
    out = []
    for i in ids:
        name = (al.titles.get(i.lower(), "") if al else "") or allowlist.title_of(platform, i) or i
        out.append(name.split(" — ")[0])
    return list(dict.fromkeys(out))


def join_names(names: list[str]) -> str:
    """Весь список, без «и ещё N»: человеку отмечать приложения не по чему, кроме этого текста."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1]


def build_pack(cat: clients.Catalog, cache: dict[str, Any], platform: str, links: list[protolib.Link],
               mans: list[Any], prefer: dict[str, list[str]] | None = None, order: list[str] | None = None,
               stores: bool = False, al: allowlist.Allowlist | None = None, apps: list[str] | None = None,
               admin: bool = False) -> Pack | None:
    """prefer — наборы приложений группы по платформам (у группы без набора — {}: раздавать нечего), order —
    протоколы группы (по PRIORITY); prefer=None (человек без группы) — рекомендованные каталогом приложения и
    порядок раздачи. stores — ссылки магазинов первыми. admin — приложения ставит ИТ: шагов установки нет.
    apps — список «через VPN» этой платформы (группы или человека; None — общий из al): его названия идут
    в шаг выбора приложений."""
    have = {ln.variant for ln in links}
    plan = _plan(cat, platform, links, have, prefer, order)
    if not plan:
        return None
    if apps is None and al is not None and platform in allowlist.PLATFORMS:
        apps = al.common(platform)
    titles = via_vpn_names(al, platform, apps) if apps else []
    names = join_names(titles) if titles else ""
    brave = has_brave(apps) if apps is not None else True   # списка нет — общий пресет, Brave в нём есть
    sections = []
    said = False   # «один способ из двух» — один раз на устройство
    for c, mine in plan:
        items = [Item(proto, method, _tile_title(cat, mans, proto)) for proto, method in mine]
        sec = Section(c, _version(c, platform, cache), _sorted_links(c["platforms"][platform], stores), items)
        ver = f" (версия {sec.version})" if sec.version else ""
        sec.foreign = cat.no_ru_store(c, platform)
        foreign = f" {FOREIGN_STORE}" if sec.foreign else ""
        sec.install = f"Установите «{c['name']}»{ver}: {_install_target(sec.links[0], platform, c)}{foreign}"
        imports: dict[str, list[str]] = {}
        for i in items:
            imports.setdefault(i.method, []).append(i.tile)
        sec.extras = [Item(ex["proto"], "file", _tile_title(cat, mans, ex["proto"])) for ex in c.get("extra", [])
                      if ex["platform"] == platform and ex["proto"] in have]
        tiles = [i.tile for i in items]
        qr_ok = all(can_qr(i.proto, c, platform, links) for i in items)
        # QR с другого экрана — телефонам, у которых главный способ не QR
        alt = tiles if qr_ok and platform not in DESKTOP and "qr" not in imports else None
        named = len(plan) > 1   # два приложения на устройстве: ключи называются, как подписаны у человека
        sec.steps = cat.steps(c, platform, list(imports.items()), names, lambda p: p in have, alt, named, not said)
        said = said or alt is not None
        if qr_ok and not sec.extras:
            sec.paper = cat.steps(c, platform, [("qr", tiles)], names, lambda p: p in have, named=named)
        sec.via = cat.via(c, platform)
        if sec.via == "apps" and c.get("per_app") == "rules" and not sec.extras:
            sec.via = "all"   # список — файлом правил, а его у человека нет: через VPN идёт всё
        sec.check = cat.check(sec.via, brave, titles[0] if titles else "")
        sections.append(sec)
    modes = [s.via for s in sections]
    lists = "apps" in modes
    pack = Pack(platform, cat.platforms[platform], sections, names, admin, via_line(cat, modes[0], names),
                report=cat.raw["report"], one_on=cat.raw["one_on"])
    if ((brave and lists) or "brave" in modes) and platform in cat.raw.get("brave", {}):   # brave — VPN только в нём
        pack.before.append(cat.raw["brave"][platform])
    if lists and platform in cat.raw.get("rules", {}):
        pack.after.append(cat.raw["rules"][platform])
    return pack


def group_prefs(g: groups.Group | None) -> tuple[dict[str, list[str]] | None, list[str] | None]:
    """Наборы приложений и протоколы группы по PRIORITY для «Подключить» (у «всех включённых» — None: что есть у
    человека). Человек без группы — (None, None): рекомендованные каталогом. У группы без набора — ({}, …): раздавать нечего."""
    if g is None:
        return None, None
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
                                         prefer, order, store_first(g), self.al, self.apps_for(plat, g),
                                         g.install_mode == "admin")
        return self.packs[key]

    def group_sig(self, g: groups.Group, plat: str) -> str:
        return pack_sig(self.group_pack(g, plat))

    def default_text(self, g: groups.Group, plat: str) -> str | None:
        """Инструкция по умолчанию: набор приложений группы по всем её протоколам. None — для платформы пакета нет."""
        pack = self.group_pack(g, plat)
        return pack.message if pack else None

    def text(self, g: groups.Group, plat: str) -> str | None:
        """Инструкция группы для платформы: своя или по умолчанию."""
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


def _key_id(uid: str, plat: str, sec: Section, n: int) -> str:
    return f"k-{uid}{plat}-{sec.client['id']}-{n}"


def _app_html(sec: Section, keys: list[Key], plat: str, name: str, cat: clients.Catalog, uid: str,
              admin: bool = False) -> Markup:
    """Приложение набора: название, версия, магазины и ключи человека. Приложения ставит ИТ — магазинов нет, а на
    iPhone — «нужен иностранный Apple ID»."""
    note = foreign_note(cat, sec.client, plat, admin)
    head = t("div", t("strong", sec.client["name"]),
             t("span", sec.version, class_="mono muted") if sec.version else None,
             t("span", note, class_="chip warn") if note else None,
             None if admin else t("div", _link_anchors(sec.links, presorted=True), class_="chips"), class_="app-head")
    return t("div", head, t("div", [_key_html(k, name, _key_id(uid, plat, sec, n))
                                    for n, k in enumerate(keys)], class_="keys"), class_="app")


def text_block(text: str, mid: str, g: groups.Group | None, extra: Any = None, links: bool = False,
               primary: bool = True) -> Markup:
    """Инструкция только для чтения: <pre>, «Скопировать» и «Изменить для группы →» (править её можно только на
    странице группы). links — предложить дописать ссылки человека в копируемый текст (нужен JS). primary — главная
    ли это кнопка экрана (на «Раздаче» главная одна — «Карточки»)."""
    return t("div",
             t("span", "Инструкция", class_="label"),
             t("pre", text, id=mid, class_="msg-pre"),
             t("div",
               t("button", "Скопировать", type="button", class_="btn primary" if primary else "btn", data_copy=mid,
                 title="Копируется текст выше, без ключей"),
               t("label", t("input", type="checkbox", data_addlinks=mid), " добавить ссылки в текст",
                 class_="chk", data_links=True, hidden=True) if links else None,
               t("a", "Изменить для группы →", href=f"/groups/{g.id}#text", class_="small", data_swap=True) if g else None,
               class_="actions"),
             extra, class_="msg")


def connect_panel(links: list[protolib.Link], name: str, ctx: Ctx, g: groups.Group | None,
                  uid: str = "", label: str | None = None, primary: bool = True,
                  shown: dict[str, str] | None = None, devices: list[str] | None = None) -> Markup | None:
    """Платформа (список) → приложения набора с версией и ссылкой, QR/ссылка/файл этого человека, инструкция
    группы с его именем (label; нет — логин) только для чтения. Без JS видны все платформы подряд; с JS список
    оставляет одну. uid — приставка id полей, если на странице несколько блоков. shown — сюда кладутся {ссылка: id поля
    на странице}: «Все ссылки и QR» копируют из этих полей, а не показывают те же ссылки второй раз. devices —
    устройства человека (groups.devices_of); None — все, для которых есть приложения."""
    if not links:
        return None
    label = label or name
    prefer, order = group_prefs(g)
    admin = bool(g and g.install_mode == "admin")
    panels: list[Markup] = []
    plats: list[tuple[str, str]] = []
    mismatch: list[str] = []
    for plat, title in ctx.cat.platforms.items():
        if devices is not None and plat not in devices:
            continue
        pack = build_pack(ctx.cat, ctx.cache, plat, links, ctx.mans, prefer, order, store_first(g), ctx.al,
                          ctx.apps_for(plat, g, name), admin)
        if pack is None:
            continue
        group_text = ctx.text(g, plat) if g else None
        own = bool(g and group_text and pack_sig(pack) != ctx.group_sig(g, plat))
        # у человека свои протоколы или другие приложения, чем в наборе группы: инструкция группы про другое
        text = fill_name((None if own else group_text) or pack.message, label)
        keys = [_keys(s, plat, links) for s in pack.sections]
        apps = [_app_html(s, k, plat, name, ctx.cat, uid, admin) for s, k in zip(pack.sections, keys)]
        if own:
            gpack = ctx.group_pack(g, plat)
            lack = [it.tile for s in (gpack.sections if gpack else ()) for it in s.items
                    if it.proto not in {i.proto for sec in pack.sections for i in sec.items}]
            mismatch.append(title + (f": нет {', '.join(dict.fromkeys(lack))}" if lack else ""))
        if shown is not None:
            for s, ks in zip(pack.sections, keys):
                for n, k in enumerate(ks):
                    if k.uri:
                        shown.setdefault(k.uri, _key_id(uid, plat, s, n))
        msg = text_block(text, f"msg-{uid}{plat}", g, links=any(k.uri for ks in keys for k in ks), primary=primary)
        panels.append(t("section", t("h4", title, class_="plat-title"), apps, msg, class_="conn-plat", data_pp=plat))
        plats.append((plat, title))
    if not panels:
        return None
    pick = (t("div", t("label", "Платформа", for_=f"pl-{uid}"),
              t("select", [t("option", title, value=p, selected=n == 0) for n, (p, title) in enumerate(plats)],
                id=f"pl-{uid}", data_plat=True), class_="conn-pick", hidden=True) if len(panels) > 1 else None)
    # одно предупреждение на человека, а не по строке на платформу
    warn = alert_list([("warn", MISMATCH + f" ({'; '.join(mismatch)})")]) if mismatch else None
    return t("div", warn, pick, panels, class_="conn")


def hints(ctx: Ctx) -> list[Markup]:
    """Строки под блоками «Подключить»: один раз на страницу, не в каждом блоке."""
    out = [t("p", SEND_WARN, class_="hint")]
    if any(not c["verified"]["device"] for c in ctx.cat.clients):
        out.append(t("p", UNVERIFIED, class_="hint"))
    return out


def no_devices(cat: clients.Catalog, devices: list[str], g: "groups.Group | None") -> Markup:
    """У человека устройства, для которых у группы нет приложений."""
    return t("span", "Для " + ", ".join(cat.platforms.get(d, d) for d in devices) + " у группы нет приложений. ",
             t("a", "Настроить →", href=f"/groups/{g.id}#settings", data_swap=True) if g else None, class_="muted")


def connect_card(links: list[protolib.Link], name: str, ctx: Ctx | None, g: groups.Group | None,
                 uid: str = "", label: str | None = None, shown: dict[str, str] | None = None,
                 devices: list[str] | None = None) -> Markup | None:
    if ctx is None:
        return None
    if g is not None and not g.clients:
        return card("Подключить", t("p", no_apps(g)))
    panel = connect_panel(links, name, ctx, g, uid, label, shown=shown, devices=devices)
    if panel is None:
        return card("Подключить", t("p", no_devices(ctx.cat, devices, g))) if devices and links else None
    return card("Подключить", t("p", "Приложения и инструкция — общие для группы." if g else "Приложения — по протоколам.",
                                " " + NO_KEYS, class_="hint"),
                panel, hints(ctx))
