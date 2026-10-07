"""Мастер «Новая группа» (/connect/new) и страницы групп (/groups): веб поверх zoolib.groups.

Мастер — один адрес и пять шагов без перезагрузки (форма data-swap): кто ставит приложения и готовые
варианты → протоколы → приложения (устройство → одна строка) → люди → раздача. Состояние между шагами
лежит в скрытых полях формы (на сервере ничего не хранится), каждый шаг проверяется сервером, запись —
одна, в конце третьего шага формы. Страница группы — те же блоки шагов 1–3 в одной форме."""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import allowlist, clients, groups, manifests, people, protolib, users
from ..fsutil import LockTimeout
from ..probe import history, live, rank, verdicts
from . import allowviews, clientviews, distviews, handoffviews, userviews
from .html import Markup, badge, card, csrf_input, post_button, t, table
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

STEPS = ("Кто ставит", "Протоколы", "Приложения", "Люди", "Раздача")
MODE_TITLES = {"admin": "Приложения ставит ИТ (или я)", "self": "Люди ставят сами по инструкции"}
PRESET_TITLES = {"simple": "Просто", "reliable": "Надёжно"}
SETS_SHOWN = 4   # наборов в «сменить» (не считая «Не нужен»)
LAYER = {"tcp": "TCP", "udp": "UDP", "tcp+udp": "TCP+UDP"}
# Полевой тест 05.10.2026 (находка 7): у VLESS+Vision новые соединения рвутся на части путей,
# Hysteria2 и XHTTP устойчивы: предвыбор — Hysteria2, XHTTP и AmneziaWG
DEFAULT_PROTOS = ("hysteria2", "vless-xhttp", "amneziawg")
# ориентиры из PLAN-builder и каталога клиентов, не замеры: на странице подписаны как ориентир
TIPS = {
    "vless-reality": "TCP: подходит там, где режут UDP.",
    "vless-xhttp": "TCP; ориентир — домашний Wi-Fi.",
    "hysteria2": "UDP; ориентир — мобильная сеть.",
    "amneziawg": "UDP; ориентир — запасной вариант.",
    "tuic": "UDP.",
    "hysteria2-obfs": "UDP; Hysteria2 с обфускацией Salamander.",
    "ss2022": "По умолчанию выключен: в полевом тесте соединения теряли данные.",
}
ERRORS_SHOWN = 6
DONE_ROWS_MAX = 10   # больше людей — на шаге раздачи не строки с QR каждого, а переход к карточкам
CATCH = (groups.GroupError, users.UserError, allowlist.AllowlistError, LockTimeout, protolib.ProtoError)


def _redirect(location: str) -> "Response":
    from .app import redirect
    return redirect(location)


def _err(e: Exception) -> str:
    return f"{e} {e.short()}" if isinstance(e, protolib.ProtoError) else str(e)


# ---------- черновик мастера и формы группы ----------

@dataclass
class Draft:
    name: str = ""
    protocols: list[str] = field(default_factory=list)
    clients: dict[str, list[str]] = field(default_factory=dict)
    clients_for: str = ""   # протоколы, под которые приложения уже выбраны на шаге 3 (изменились — набор пересчитывается)
    install_mode: str = "self"
    devices: list[str] = field(default_factory=lambda: list(groups.MAIN_DEVICES))
    allow_mode: str = "common"
    allow: dict[str, list[str]] = field(default_factory=lambda: {p: [] for p in allowlist.PLATFORMS})
    new_users: str = ""
    existing: list[str] = field(default_factory=list)

    @classmethod
    def from_form(cls, req: "Request") -> "Draft":
        f, m = req.form, req.multi
        sets: dict[str, list[str]] = {}
        gone: set[str] = set()
        for k, vals in m.items():
            if k.startswith("set:"):    # устройство → набор «A+B»; none — устройство не нужно
                if vals and vals[-1] == "none":
                    gone.add(k[4:])
                elif ids := groups.client_ids(vals[-1][:200].split("+")[:groups.CLIENTS_MAX + 1]):
                    sets[k[4:]] = ids
        for k, vals in m.items():       # прежняя форма: галочки client:<платформа> (набор по умолчанию — set:)
            if k.startswith("client:") and k[7:] not in sets and k[7:] not in gone:
                if ids := groups.client_ids([v[:40] for v in vals][:groups.CLIENTS_MAX + 1]):
                    sets[k[7:]] = ids
        if f.get("devs") == "1":
            devices = list(dict.fromkeys(p[:20] for p in m.get("dev", [])))[:10]
        else:
            devices = list(sets) or list(groups.MAIN_DEVICES)
        devices = [p for p in devices if p not in gone]
        return cls(
            name=f.get("name", "")[:200].strip(),
            protocols=list(dict.fromkeys(p[:40] for p in m.get("proto", [])))[:20],
            clients={p: ids for p, ids in sets.items() if p in devices},
            clients_for=f.get("clients_for", "")[:400],
            install_mode=groups.clean_mode(f.get("mode")),
            devices=devices,
            allow_mode="own" if f.get("allow_mode") == "own" else "common",
            allow={p: [v[:128] for v in m.get(p, [])][:allowlist.LIST_MAX + 1] for p in allowlist.PLATFORMS},
            new_users=f.get("users_new", "")[:people.TEXT_MAX],
            existing=[n[:32] for n in m.get("existing", [])][:200])

    @classmethod
    def of_group(cls, g: groups.Group) -> "Draft":
        d = cls(name=g.name, protocols=list(g.protocols), clients={p: list(v) for p, v in g.clients.items()},
                install_mode=g.install_mode, devices=list(g.clients))
        if g.allowlist:
            d.allow_mode, d.allow = "own", {p: list(g.allowlist[p]) for p in allowlist.PLATFORMS}
        return d

    def resolved(self, managed: list[str]) -> list[str]:
        return groups.by_priority(groups.offered(self.protocols, managed))

    def allow_arg(self) -> dict[str, list[str]] | None:
        return dict(self.allow) if self.allow_mode == "own" else None


def _hidden(d: Draft, shown: int, own_allow: bool = True) -> list[Markup]:
    """Состояние шагов, которых сейчас не видно, — скрытыми полями (shown — номер шага формы)."""
    out: list[Markup] = []

    def add(name: str, value: str) -> None:
        out.append(t("input", type="hidden", name=name, value=value))

    add("mode", d.install_mode)
    if shown not in (1, 3):
        add("name", d.name)
    if shown != 1:
        for p in d.protocols:
            add("proto", p)
    if shown != 2:
        if d.clients_for:
            add("clients_for", d.clients_for)
        add("devs", "1")
        for plat in d.devices:
            add("dev", plat)
        for plat, ids in d.clients.items():
            add(f"set:{plat}", "+".join(ids))
    if shown != 3:
        add("users_new", d.new_users)
        for n in d.existing:
            add("existing", n)
        add("allow_mode", d.allow_mode)
        if d.allow_mode == "own":
            for plat in allowlist.PLATFORMS:
                for v in d.allow[plat]:
                    add(plat, v)
    return out


# ---------- факты о протоколах (шаг 1) ----------

def _rank_facts() -> dict[str, dict[str, float]]:
    """По клиентским пробам за 30 дней: {протокол: n, ok, top (в скольких контекстах в топе), score}."""
    out: dict[str, dict[str, float]] = {}
    try:
        ranking = rank.load("30d")
    except (sqlite3.Error, OSError, ValueError, history.HistoryError):
        return out
    for c in ranking:
        for p in c["protocols"]:
            d = out.setdefault(p["proto"], {"n": 0, "ok": 0, "top": 0, "score": 0.0, "ctx": 0})
            d["n"] += p["n"]
            d["ok"] += p["ok"]
            if c["ranked"]:
                d["ctx"] += 1
                d["score"] += p["score"]
                d["top"] += 1 if p in c["top"] else 0
    return out


@dataclass
class Fact:
    id: str
    title: str
    layer: str
    live: dict[str, Any] | None
    rank: dict[str, float] | None
    now: float

    @property
    def down(self) -> bool:
        d = self.live
        return bool(d and not d["ok"] and d["verdict"] not in verdicts.NOT_TESTED)

    def live_chip(self) -> Markup:
        d = self.live
        if d is None:
            return t("span", "с сервера: нет замера", class_="chip", title="первый замер — через пару минут после включения")
        if d["verdict"] in verdicts.NOT_TESTED:
            return t("span", "с сервера: не проверяется", class_="chip")
        stale = self.now - d["ts"] > live.FRESH
        if not d["ok"]:
            return t("span", f"с сервера: сбой ({d['verdict']})", class_="chip bad",
                     title=verdicts.DESCRIPTIONS.get(d["verdict"], ""))
        text = "с сервера: " + (" · ".join(live.parts(d, self.now, jitter=False)) or "работает") + (" (устарело)" if stale else "")
        return t("span", text, class_="chip" if stale else "chip ok", title=live.speed_note(d, self.now) or None)

    def rank_chip(self) -> Markup | None:
        r = self.rank
        if not r or not r["n"]:
            return None
        text = f"у клиентов: {int(r['ok'])} из {int(r['n'])}"
        few = r["n"] < rank.LOW_SAMPLES
        return t("span", text + (" · мало данных" if few else ""), class_="chip" if few or r["ok"] < r["n"] else "chip ok",
                 title="клиентские пробы за 30 дней (zoo probe --rank)")


def proto_facts() -> list[Fact]:
    """Выбираемые протоколы (модули и их варианты, например Salamander) в порядке PRIORITY."""
    mans = {m.id: m for m in manifests.load_all()[0]}
    latest, ranks, now = live.summary(), _rank_facts(), time.time()

    def fact(pid: str) -> Fact:
        m = mans.get(pid)
        return Fact(pid, m.short if m else pid, LAYER.get(m.layer, "") if m else "", latest.get(pid), ranks.get(pid), now)

    return [fact(p) for p in groups.by_priority(users.selectable_protocols())]


def suggest(facts: list[Fact], only: set[str] | None = None) -> list[str]:
    """Предвыбор: топ по клиентским пробам (если они есть), иначе дефолты из рабочих протоколов.
    only — допустимые протоколы (людям, которые ставят сами, — те, что входят одним QR)."""
    facts = [f for f in facts if only is None or f.id in only]
    ranked = sorted((f for f in facts if f.rank and f.rank["top"] and not f.down),
                    key=lambda f: (-f.rank["top"], -f.rank["score"] / max(f.rank["ctx"], 1)))  # type: ignore[index]
    ids = [f.id for f in ranked][:3]
    if ids:
        return ids
    ok = [f.id for f in facts if not f.down]
    return ([p for p in DEFAULT_PROTOS if p in ok] or ok)[:3]


def _proto_row(f: Fact, checked: bool) -> Markup:
    chips = [t("span", f.layer, class_="chip") if f.layer else None, f.live_chip(), f.rank_chip()]
    tip = TIPS.get(f.id)
    return t("label", t("input", type="checkbox", name="proto", value=f.id, checked=checked),
             t("span", t("span", t("strong", f.title), class_="opt-title"),
               t("div", chips, class_="chips"),
               t("span", tip, class_="hint") if tip else None, class_="opt-body"),
             class_="opt")


def _protocols_block(facts: list[Fact], selected: list[str]) -> Markup:
    """Все выбираемые протоколы сервера строками-чекбоксами (Salamander — отдельная строка); выключенные — серые."""
    items = [_proto_row(f, f.id in selected) for f in facts]
    if not items:
        return alert_list([("warn", "Нет включённых протоколов с пользователями.")])
    for title, why in _not_selectable({f.id for f in facts}):
        items.append(t("div", t("span", t("span", t("strong", title), class_="opt-title"),
                                t("span", why, class_="hint"), class_="opt-body"), class_="opt off"))
    return t("div", items, class_="opts")


def _not_selectable(shown: set[str]) -> list[tuple[str, str]]:
    """Протоколы сервера, которых нет среди выбираемых: выключенные — серой строкой, чтобы были видны все."""
    out = []
    for m in manifests.load_all()[0]:
        if m.id in shown:
            continue
        out.append((m.short, "выключен на сервере — включается на «Обзоре»" if not m.enabled else "без учёток пользователей"))
    return out


# ---------- приложения (шаг 3): устройство → одна строка ----------

def _set_note(cat: clients.Catalog, plat: str, ids: list[str], mode: str) -> str:
    """Подвох набора одной строкой: нет в российском магазине; людям, которые ставят сами, — приложение не из магазина."""
    cs = [c for c in (cat.client(i) for i in ids) if c]
    foreign = [c["name"] for c in cs if cat.no_ru_store(c, plat)]
    if foreign:
        return f"{', '.join(foreign)}: нет в {'App Store' if plat == 'ios' else 'магазине'} РФ"
    if mode == "self" and any(groups.in_store(c, plat) for c in cat.clients if plat in c["platforms"]):
        raw = [c["name"] for c in cs if not groups.in_store(c, plat)]
        if raw:
            return f"{', '.join(raw)}: не из магазина, ставится файлом"
    return ""


def _device_options(cat: clients.Catalog, plat: str, protocols: list[str], current: list[str], plan: list[str],
                    mode: str, used: set[str]) -> list[list[str]]:
    """Наборы для «сменить»: выбранный сейчас, предложенный подбором и лучшие из client_sets (до четырёх из 1–2
    приложений). Выбранный сейчас, даже если приложений больше двух (старая группа), идёт первым."""
    out: list[list[str]] = []
    for ids in (current, plan):
        if ids and sorted(ids) not in [sorted(x) for x in out]:
            out.append(ids)
    for r in groups.client_sets(cat, plat, protocols, mode, prefer=used):
        if len(out) >= SETS_SHOWN:
            break
        if sorted(r["ids"]) not in [sorted(x) for x in out]:
            out.append(r["ids"])
    return out


def _device_row(cat: clients.Catalog, plat: str, title: str, protocols: list[str], chosen: list[str] | None,
                options: list[list[str]], names: dict[str, str], mode: str) -> Markup:
    if not options:
        return t("div", t("strong", title, class_="dev-name"), t("span", "нет приложения под эти протоколы", class_="muted"),
                 class_="dev-row")
    ids = chosen or options[0]
    label, kind = clientviews.coverage_label(cat, plat, protocols, ids, names)
    radios = []
    for opt in options:
        lab, k = clientviews.coverage_label(cat, plat, protocols, opt, names)
        note = _set_note(cat, plat, opt, mode)
        radios.append(t("label", t("input", type="radio", name=f"set:{plat}", value="+".join(opt), checked=opt == ids,
                                   data_auto=True),
                        t("span", t("strong", clientviews.app_names(cat, opt)), " ", t("span", lab, class_=f"chip {k}"),
                          class_="opt-title"),
                        t("span", note, class_="hint") if note else None, class_="opt-row dev-opt"))
    radios.append(t("label", t("input", type="radio", name=f"set:{plat}", value="none", data_auto=True),
                    t("span", f"Не нужен: {title}", class_="opt-title"), class_="opt-row dev-opt"))
    note = _set_note(cat, plat, ids, mode)
    warn = clientviews.caveat_note(cat, plat, protocols, ids, names)
    extra = distviews.ios_note() if plat == "ios" and mode == "admin" else None
    return t("div",
             t("div", t("strong", title, class_="dev-name"), t("span", clientviews.app_names(cat, ids), class_="dev-set"),
               t("span", label, class_=f"plat-sum {kind}"),
               t("details", t("summary", "сменить"), t("div", radios, class_="opts"), class_="more dev-change"),
               class_="dev-head"),
             t("p", "! " + note, class_="hint") if note else None,
             t("p", "! " + warn, class_="hint") if warn else None, extra, class_="dev-row")


def _clients_block(d: Draft, managed: list[str]) -> Markup:
    """Шаг «Приложения»: чипы устройств; по строке на устройство (что поставить, «все N» / «без X», «сменить»);
    «Итого» — каждое приложение один раз со своими устройствами. Любая смена радиокнопки или чипа пересобирает
    блок: кнопка «Обновить», с JS она нажимается сама."""
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return alert_list([("bad", str(e))])
    names = clientviews.proto_names(cat)
    protocols = d.resolved(managed)
    devices = [p for p in d.devices if p in cat.platforms]
    plan = groups.suggest_set(cat, devices, protocols, d.install_mode)
    chips = t("div", [t("label", t("input", type="checkbox", name="dev", value=p, checked=p in devices, data_auto=True),
                        t("span", title), class_="chip-check") for p, title in cat.platforms.items()],
              class_="dev-chips")
    rows: list[Markup] = []
    picked: dict[str, list[str]] = {}
    for plat in cat.platforms:
        if plat not in devices:
            continue
        chosen = [i for i in d.clients.get(plat, []) if cat.client(i)]
        used = {a for p, ids in d.clients.items() if p != plat for a in ids}
        opts = _device_options(cat, plat, protocols, chosen, plan.get(plat, []), d.install_mode, used)
        rows.append(_device_row(cat, plat, cat.platforms[plat], protocols, chosen or None, opts, names, d.install_mode))
        if opts:
            picked[plat] = chosen or opts[0]
    head, _ = groups.apps_summary(cat, picked)
    where: dict[str, list[str]] = {}
    for plat, ids in picked.items():
        for cid in ids:
            where.setdefault(cid, []).append(cat.platforms[plat])
    total = (t("p", t("strong", f"Итого {len(where)}: "),
               " · ".join(f"{(cat.client(c) or {}).get('name', c)} — {', '.join(v)}" for c, v in where.items()),
               class_="dev-total") if where else t("p", "Устройства не выбраны.", class_="muted"))
    unverified = any(not (cat.client(c) or {}).get("verified", {}).get("device", True) for c in where)
    return t("div", t("span", "Устройства", class_="label"), chips,
             t("p", head, class_="plat-sum ok") if head else None,
             t("div", rows, class_="dev-rows"), total,
             t("p", clientviews.UNVERIFIED, class_="hint") if unverified else None,
             t("button", "Обновить", type="submit", name="go", value="refresh", class_="btn small", data_refresh=True))


def normalize_clients(d: Draft, managed: list[str], fill: bool = True) -> None:
    """Выбор приложений под текущие протоколы и устройства: остаются подходящие. fill (мастер): пока приложения не
    выбраны под эти же протоколы (первый показ шага или протоколы сменили) — предлагается подбор (suggest_set под
    режим «кто ставит»); после — отмеченное сохраняется. Устройство без выбора (только что добавленный чип)
    получает подбор; устройство, под которое приложений нет, остаётся без набора."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return
    protocols = d.resolved(managed)
    stale = fill and d.clients_for != ",".join(protocols)
    devices = [p for p in d.devices if p in cat.platforms]
    plan = groups.suggest_set(cat, devices, protocols, d.install_mode)
    fixed: dict[str, list[str]] = {}
    for plat in devices:
        opts = groups.client_options(cat, plat, protocols)
        if not opts:
            continue
        ids = {o["client"]["id"] for o in opts}
        got = [] if stale else [i for i in d.clients.get(plat, []) if i in ids]
        got = got or plan.get(plat, [])
        if got:
            fixed[plat] = got
    d.devices = devices
    d.clients = fixed
    if fill:
        d.clients_for = ",".join(protocols)


# ---------- люди и приложения (шаг 3) ----------

def _apps_block(d: Draft) -> Markup:
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return alert_list([("bad", str(e))])
    own = d.allow_mode == "own"
    base = {p: (d.allow[p] if own and d.allow[p] else al.common(p)) for p in allowlist.PLATFORMS}
    on = {p: {i.lower() for i in base[p]} for p in allowlist.PLATFORMS}
    rows = [allowviews._row_cells(r, on, on) for r in allowviews._rows(al, None, base, al.titles)]
    common = ", ".join(f"{p}: {len(al.common(p))}" for p in allowlist.PLATFORMS)
    return t("div",
             t("label", t("input", type="radio", name="allow_mode", value="common", checked=not own),
               t("span", "Общий список ", t("span", f"({common})", class_="muted small"), class_="opt-title"),
               class_="opt-row"),
             t("label", t("input", type="radio", name="allow_mode", value="own", checked=own),
               t("span", "Свой список группы", class_="opt-title"), class_="opt-row"),
             t("details", t("summary", "Приложения своего списка"),
               table(["приложение", "Android", "Windows"], rows, num=[1, 2], cls="apps"),
               t("p", "Нужен хотя бы один пункт на Android и на Windows. Свой список пользователя "
                      "потом можно задать отдельно («Приложения»).", class_="hint"),
               open=own or None, class_="more"))


def _mode_radios(mode: str) -> Markup:
    return t("div", [t("label", t("input", type="radio", name="mode", value=m, checked=m == mode, data_auto=True),
                      t("span", title, class_="opt-title"), class_="opt-row") for m, title in MODE_TITLES.items()],
             class_="opts")


def _picker(label: str, name: str, items: list[tuple[str, str, bool]]) -> Markup:
    """Список с поиском и галочками: (значение, пояснение справа, отмечен). Без JS — просто прокручиваемый
    список галочек; с JS — строка поиска и счётчик отмеченных (app.js, [data-picker])."""
    rows = [t("label", t("input", type="checkbox", name=name, value=v, checked=on), t("span", v),
              t("span", sub, class_="muted small") if sub else None, class_="pick-item",
              data_find=f"{v} {sub}".lower()) for v, sub, on in items]
    return t("div", t("span", label, class_="label"),
             t("div", t("input", type="search", data_filter=True, placeholder="найти", autocomplete="off",
                        aria_label="Найти в списке", class_="pick-find"),
               t("span", data_picked=True, class_="muted small"), class_="pick-head"),
             t("div", rows, class_="pick-list"), class_="field picker", data_picker=True)


def _existing_block(d: Draft, group_id: str | None = None) -> Markup | None:
    gs = groups.Groups.load()
    items = [(u.name, g.name if (g := gs.get(u.group)) else "", u.name in d.existing)
             for u in users.list_users().visible() if not (group_id and u.group == group_id)]
    return _picker("Уже есть (перейдут в группу):", "existing", items) if items else None


def _users_block(d: Draft, group_id: str | None = None) -> Markup:
    return t("div",
             t("div", t("label", "Новые люди", for_="users_new"),
               t("textarea", d.new_users, name="users_new", id="users_new", rows="8", maxlength=str(people.TEXT_MAX),
                 placeholder="Иван Петров; бухгалтерия\nМария Сидорова\nОльга; склад", autocomplete="off",
                 spellcheck="false"),
               t("div", f"По строке на человека: «Имя» или «Имя; заметка», до {people.LINES_MAX}. "
                        "Имена станут латинскими; перед созданием покажем, что получится.", class_="hint"),
               class_="field"),
             _existing_block(d, group_id), class_="stack")


def _preview_rows(plan: people.Plan) -> Markup:
    rows, cls = [], []
    for r in plan.rows:
        chips = [t("span", "латиницей", class_="chip", title="имя переведено в латиницу") if r.converted else None,
                 t("span", f"{r.clash}: добавлен номер", class_="chip warn",
                   title="такое имя уже есть: добавлен номер") if r.clash else None,
                 t("span", r.problem, class_="chip bad") if r.problem else None]
        rows.append([str(r.line), r.raw[:80], t("strong", r.name) if r.name else "—", r.note or "—", chips])
        cls.append("row-bad" if r.problem else ("row-warn" if r.clash else None))
    return table(["№", "строка", "имя", "заметка", ""], rows, num=[0], cls="preview", row_cls=cls)


def _preview_summary(plan: people.Plan, existing: int = 0) -> str:
    text = f"Будет создано: {len(plan.rows)}"
    if existing:
        text += f", переведено из имеющихся: {existing}"
    if plan.renamed:
        text += f". Совпали имена, добавлен номер: {len(plan.renamed)}"
    return text + "."


def _preview(app: "App", req: "Request", d: Draft, plan: people.Plan) -> "Response":
    """Шаг 3 → создание: что получится из списка; создаёт только кнопка под таблицей (confirm=1)."""
    csrf = req.session.csrf if req.session else ""
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="step", value="3"),
             t("input", type="hidden", name="confirm", value="1"), _hidden(d, 0),
             t("div", t("button", f"Создать группу и {len(plan.rows)} чел.", type="submit", name="go", value="create",
                        class_="btn primary"),
               t("button", "← Изменить список", type="submit", name="go", value="edit", class_="btn", formnovalidate=True),
               class_="wiz-nav"),
             method="post", action="/connect/new", class_="stack", data_swap=True)
    body = card("4. Люди: проверьте список", t("p", _preview_summary(plan, len(d.existing)), class_="hint"),
                _preview_rows(plan), form)
    return app.render(req, "Новая группа", t("div", [page_head("Новая группа"), _stepper(4), body], class_="wizard",
                                              data_expanded=True), active="/groups")


# ---------- мастер ----------

def _stepper(pos: int) -> Markup:
    """pos — номер текущего шага в STEPS с единицы."""
    return t("ol", [t("li", t("span", str(i), class_="n"), name, class_="cur" if i == pos else ("done" if i < pos else None),
                      aria_current="step" if i == pos else None) for i, name in enumerate(STEPS, 1)],
             class_="stepper")


def _mode_nav(mode: str) -> Markup:
    return t("nav", [t("a", MODE_TITLES[m], href=f"/connect/new?mode={m}", class_="active" if m == mode else None)
                     for m in groups.INSTALL_MODES], class_="seg", aria_label="Кто ставит приложения")


def _preset_row(pr: dict[str, Any], cat: clients.Catalog, names: dict[str, str], facts: dict[str, Fact]) -> Markup:
    """Готовый вариант: протоколы, сколько приложений на устройство и какие, цифры «с сервера», кнопка выбора."""
    apps = [cid for ids in pr["plan"].values() for cid in ids]
    apps = list(dict.fromkeys(apps))
    per = pr["per_device"]
    count = "1 приложение на устройство" if per == 1 else f"до {per} приложений на устройство"
    foreign = [cat.platforms[p] for p, ids in pr["plan"].items() if any(cat.no_ru_store(cat.client(i) or {}, p) for i in ids)]
    lines = [t("div", t("strong", names.get(p, p)), " ", facts[p].live_chip() if p in facts else None, class_="chips")
             for p in pr["protocols"]]
    warns = dict.fromkeys(f"{names.get(p, p)} в {app}" for plat, ids in pr["plan"].items()
                          for p, app, _ in groups.caveats(cat, plat, pr["protocols"], ids))
    notes = [t("span", f"{count}: {clientviews.app_names(cat, apps)}", class_="hint"),
             t("span", f"! нет в магазине РФ: {', '.join(foreign)}", class_="hint") if foreign else None,
             t("span", "! не на всех устройствах все протоколы", class_="hint") if not pr["complete"] else None,
             t("span", f"! с оговоркой: {', '.join(warns)} (подробности — на шаге «Приложения»)",
               class_="hint") if warns else None]
    return t("div", t("span", t("span", t("strong", PRESET_TITLES[pr["id"]]), class_="opt-title"), lines, notes,
                      class_="opt-body"),
             t("button", "Выбрать", type="submit", name="go", value=pr["id"], class_="btn primary"), class_="opt preset")


def _start_block(d: Draft, facts: list[Fact], managed: list[str]) -> Markup:
    """Шаг 0: кто ставит приложения и готовые варианты («Просто», «Надёжно», «Свой набор»)."""
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return alert_list([("bad", str(e))])
    names = clientviews.proto_names(cat)
    by_id = {f.id: f for f in facts}
    rows = [_preset_row(pr, cat, names, by_id) for pr in groups.presets(cat, managed, d.install_mode)]
    rows.append(t("div", t("span", t("span", t("strong", "Свой набор"), class_="opt-title"),
                           t("span", "Протоколы и приложения выбираю сам.", class_="hint"), class_="opt-body"),
                  t("button", "Выбрать", type="submit", name="go", value="custom", class_="btn", formnovalidate=True),
                  class_="opt preset"))
    hint = ("Сервер скачает APK и установщики, раздать их можно из «Скачать дистрибутивы»." if d.install_mode == "admin"
            else "Только приложения из магазинов; протоколы, которые входят одним QR без ручных правок.")
    return t("div", _mode_nav(d.install_mode), t("p", hint, class_="hint"), t("div", rows, class_="opts"))


def _wizard(app: "App", req: "Request", step: int, d: Draft, errors: list[str] | None = None,
            status: int = 200) -> "Response":
    csrf = req.session.csrf if req.session else ""
    facts = proto_facts()
    managed = [f.id for f in facts]
    gs = groups.Groups.load()
    nav_next = True
    if step == 0:
        body, title, hint = [_start_block(d, facts, managed)], "Кто ставит приложения?", None
        nav_next = False
    elif step == 1:
        if not d.name:
            d.name = gs.next_name()
        if not d.protocols:
            d.protocols = suggest(facts, _easy_only(d, managed))
        body = [t("div", t("label", "Название группы", for_="name"),
                  t("input", type="text", name="name", id="name", value=d.name, required=True, maxlength=str(groups.NAME_MAX),
                    autocomplete="off"), class_="field"),
                _protocols_block(facts, d.protocols)]
        title, hint = "Протоколы", None
    elif step == 2:
        normalize_clients(d, managed)
        body = [t("input", type="hidden", name="clients_for", value=d.clients_for),
                t("p", MODE_TITLES[d.install_mode], class_="hint"), _clients_block(d, managed)]
        title, hint = "Приложения", "Что поставить на каждое устройство."
    else:
        if not d.name:
            d.name = gs.next_name()
        body = [t("div", t("label", "Название группы", for_="name"),
                  t("input", type="text", name="name", id="name", value=d.name, required=True, maxlength=str(groups.NAME_MAX),
                    autocomplete="off"), class_="field"),
                _users_block(d), t("h3", "Приложения через VPN", class_="sub-h"), _apps_block(d)]
        title, hint = "Люди", "Кто подключается и какие приложения идут через VPN."
    last = step == 3
    nav = t("div",
            t("button", "Создать группу" if last else "Далее →", type="submit", name="go",
              value="create" if last else "next", class_="btn primary") if nav_next else None,
            t("button", "← Назад", type="submit", name="go", value="back", class_="btn", formnovalidate=True)
            if step > 0 else None, class_="wiz-nav")
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="step", value=str(step)),
             _hidden(d, step), body, nav, method="post", action="/connect/new", class_="stack", data_swap=True)
    parts: list[Any] = [page_head("Новая группа"), _stepper(step + 1)]
    if errors:
        parts.append(alert_list([("bad", e) for e in errors]))
    parts.append(card(f"{step + 1}. {title}", t("p", hint, class_="hint") if hint else None, form))
    # data-expanded: живое обновление не заменяет страницу, пока идёт мастер (иначе шаг сбросился бы на первый)
    return app.render(req, "Новая группа", t("div", parts, class_="wizard", data_expanded=True),
                      active="/groups", status=status)


def _easy_only(d: Draft, managed: list[str]) -> set[str] | None:
    """Протоколы по умолчанию: людям, которые ставят сами, — только те, что входят одним QR из магазинного приложения."""
    if d.install_mode != "self":
        return None
    try:
        easy = groups.easy_protocols(clients.load())
    except clients.ClientsError:
        return None
    return {p for p in managed if p in easy} or None


def connect_page(app: "App", req: "Request") -> "Response":
    if not users.managed_protocols()[0]:
        return app.render(req, "Новая группа", [page_head("Новая группа"), card(
            "Нет протоколов", alert_list([("warn", "Нет включённых протоколов, куда можно добавить пользователя.")]))],
            active="/groups")
    try:
        groups.ensure()
    except CATCH:
        pass
    return _wizard(app, req, 0, Draft(install_mode=groups.clean_mode(req.query.get("mode"))))


def _check(d: Draft, step: int) -> list[str]:
    """Первая ошибка шага (и предыдущих): сервер не верит скрытым полям."""
    try:
        gs = groups.Groups.load()
        if step >= 1:
            groups.clean_name(d.name, gs)
            groups.clean_protocols(d.protocols)
        if step >= 2:
            groups.clean_clients(d.clients, d.protocols)
        if step >= 3:
            groups.clean_allow(d.allow_arg())
            groups.check_members(groups.parse_new_users(d.new_users), d.existing)
            if not groups.parse_new_users(d.new_users) and not d.existing:
                raise groups.GroupError("Добавьте хотя бы одного пользователя")
    except CATCH as e:
        return [_err(e)]
    return []


def _start(app: "App", req: "Request", d: Draft, go: str) -> "Response":
    """Шаг 0: выбран готовый вариант («Просто», «Надёжно») — протоколы заданы, дальше сразу приложения; «Свой
    набор» — шаг протоколов с предвыбором под режим."""
    facts = proto_facts()
    managed = [f.id for f in facts]
    d.devices, d.clients, d.clients_for = list(groups.MAIN_DEVICES), {}, ""
    d.name = d.name or groups.Groups.load().next_name()
    if go == "custom":
        d.protocols = suggest(facts, _easy_only(d, managed))
        return _wizard(app, req, 1, d)
    try:
        pr = next(p for p in groups.presets(clients.load(), managed, d.install_mode) if p["id"] == go)
    except (StopIteration, clients.ClientsError):
        return _wizard(app, req, 0, d, ["Такого варианта нет: выберите ещё раз"], 422)
    d.protocols = list(pr["protocols"])
    return _wizard(app, req, 2, d)


def connect_post(app: "App", req: "Request") -> "Response":
    try:
        groups.ensure()
    except CATCH:
        pass
    d = Draft.from_form(req)
    try:
        step = min(max(int(req.form.get("step", "1")), 0), 3)
    except ValueError:
        step = 1
    go = req.form.get("go", "next")
    if go == "back":
        return _wizard(app, req, max(step - 1, 0), d)
    if go == "edit":
        return _wizard(app, req, 3, d)
    if step == 0:
        return _start(app, req, d, go)
    if go == "refresh" and step == 2:
        return _wizard(app, req, 2, d)
    errors = _check(d, step)
    if errors:
        return _wizard(app, req, step, d, errors, 422)
    if go != "create" or step < 3:
        return _wizard(app, req, min(step + 1, 3), d)
    plan = people.plan_for_registry(d.new_users)
    if plan.rows and not req.form.get("confirm"):
        return _preview(app, req, d, plan)
    try:
        rep = groups.connect(d.name, d.protocols, d.clients, d.allow_arg(), plan.pairs(), d.existing, d.install_mode)
    except CATCH as e:
        return _wizard(app, req, 3, d, [_err(e)], 422)
    app.invalidate("status")
    app.invalidate_links()
    if rep.removed:
        # никого не добавили, пустая группа убрана: форма остаётся на шаге 3, повтор с тем же названием возможен
        return _wizard(app, req, 3, d, rep.errors or ["Никого не удалось добавить"], 422)
    flash_report(req, rep, _renamed(plan))
    if rep.crashed:
        return _redirect(f"/groups/{rep.group.id}")
    who = ",".join(rep.created + rep.moved)
    return _redirect("/connect/done?" + urllib.parse.urlencode({"group": rep.group.id, "u": who}))


def connect_done(app: "App", req: "Request") -> "Response":
    gs = groups.Groups.load()
    g = gs.get(req.query.get("group"))
    if g is None:
        return app.error(req, 404, "Нет группы", "Такой группы нет.")
    ureg = users.list_users()
    wanted = [n for n in req.query.get("u", "").split(",") if n][:groups.NEW_USERS_MAX * 2]
    members = [u for u in ureg.visible() if u.group == g.id and u.name in wanted]
    ctx = clientviews.Ctx.load()
    dist = distviews.card_for(gs, g.id, "/connect/done?" + urllib.parse.urlencode({"group": g.id, "u": req.query.get("u", "")}),
                              req.session.csrf if req.session else "") if g.install_mode == "admin" else None
    rows: list[Any] = []
    if len(members) > DONE_ROWS_MAX:
        return _done_many(app, req, g, members, ctx, dist)
    for u in members:
        links, _ = userviews._cached_links(app, u.name)
        panel = clientviews.connect_panel(links, u.name, ctx, g, uid=f"{u.name}-") if ctx else None
        rows.append(t("details", t("summary", t("strong", u.name), t("span", f" {u.note}", class_="muted small")
                                   if u.note else None),
                      panel or t("p", "Ссылок пока нет.", class_="muted"),
                      t("p", t("a", "Страница пользователя →", href=f"/users/{u.name}"), class_="small"),
                      name="conn-user", open=len(members) == 1 or None, class_="urow"))
    summary = card(f"Группа «{g.name}»", _summary(g),
                   extra=t("a", "Настроить", href=f"/groups/{g.id}", class_="btn small", data_swap=True))
    body = [page_head("Новая группа", "готово: раздайте пакеты"), _stepper(len(STEPS)), summary, dist,
            _texts_card(g, ctx) if ctx else None,
            card("Кому что отправить", t("div", rows, class_="urows"), clientviews.hints(ctx) if ctx else None,
                 help="Откройте человека: его QR и ссылки, приложения и текст с его именем.")
            if rows else alert_list([("warn", "Никого не добавили — раздавать нечего.")])]
    return app.render(req, "Новая группа", body, active="/groups")


def _done_many(app: "App", req: "Request", g: groups.Group, members: list[users.User],
               ctx: clientviews.Ctx | None, dist: Markup | None = None) -> "Response":
    """Шаг раздачи для команды: строка с QR на каждого не нужна — все карточки одной страницей и архивом."""
    st = handoffviews.connection([u.name for u in members])
    link = "/handoff?" + urllib.parse.urlencode({"group": g.id})
    go = card(f"Раздать {len(members)} человек",
              t("p", "Карточка на каждого: приложение, его QR и ссылка, шаги. Печать, ZIP с папкой на человека и CSV для рассылки.",
                class_="hint"),
              t("div", t("a", "Карточки для раздачи", href=link, class_="btn primary", data_swap=True),
                t("span", st.counter, class_="chip"), class_="actions"),
              t("p", clientviews.SEND_WARN, class_="hint"))
    summary = card(f"Группа «{g.name}»", _summary(g),
                   extra=t("a", "Настроить", href=f"/groups/{g.id}", class_="btn small", data_swap=True))
    body = [page_head("Новая группа", "готово: раздайте пакеты"), _stepper(len(STEPS)), summary, dist, go,
            _texts_card(g, ctx) if ctx else None]
    return app.render(req, "Новая группа", body, active="/groups")


def _texts_card(g: groups.Group, ctx: clientviews.Ctx) -> Markup | None:
    """Один текст инструкции на платформу — общий для группы (на шаге раздачи — только показ)."""
    items = []
    for plat, title in ctx.cat.platforms.items():
        text = ctx.text(g, plat)
        if text:
            items.append(t("details", t("summary", title), t("pre", text.replace(clientviews.NAME_TOKEN, "имя"),
                                                           class_="msg-pre"), class_="more"))
    if not items:
        return None
    return card("Текст для группы", t("div", items),
                extra=t("a", "Изменить", href=f"/groups/{g.id}#texts", class_="btn small", data_swap=True),
                help="Один текст на платформу для всех участников; в каждом сообщении вместо «имя» — имя человека.")


def _summary(g: groups.Group) -> Markup:
    selectable = users.selectable_protocols()
    titles = {m.id: m.short for m in manifests.load_all()[0]}
    try:
        cat = clients.load()
        names = {p: " + ".join((cat.client(c) or {}).get("name", c) for c in ids) for p, ids in g.clients.items()}
        plat = cat.platforms
    except clients.ClientsError:
        names, plat = {p: " + ".join(ids) for p, ids in g.clients.items()}, {}
    chips = [t("span", titles.get(p, p), class_="chip") for p in g.offered(selectable)]
    cl = [t("span", f"{plat.get(p, p)}: {n}", class_="chip info") for p, n in names.items()]
    return t("div", t("div", chips, class_="chips"), t("div", cl, class_="chips") if cl else None,
             t("p", "приложения через VPN: " + ("свой список группы" if g.allowlist else "общий список")
               + " · ставит: " + ("ИТ" if g.install_mode == "admin" else "люди сами"), class_="hint"))


def _list(names: list[str], limit: int = 8) -> str:
    """«a, b, c»; длинный список — число, начало и «и ещё N»."""
    if len(names) <= limit:
        return ", ".join(names)
    return f"{len(names)}: " + ", ".join(names[:5]) + f" и ещё {len(names) - 5}"


def _renamed(plan: people.Plan) -> list[str]:
    return [f"{r.display} → {r.name}" for r in plan.renamed]


def flash_report(req: "Request", rep: groups.GroupReport, renamed: list[str] | None = None) -> None:
    """Один итог на всё действие; ошибки по людям — по одной, но не больше ERRORS_SHOWN."""
    s = req.session
    if s is None:
        return
    parts = [f"«{rep.group.name}»: {rep.message}"]
    if rep.created:
        parts.append("создано: " + _list(rep.created))
    if rep.moved:
        parts.append("переведено: " + _list(rep.moved))
    if rep.needs_qr and not rep.created and len(rep.needs_qr) <= 8:
        s.flash("ok" if rep.ok else "warn", ". ".join(parts) + ". Новые QR/файлы нужны:",
                [(n, f"/users/{n}") for n in rep.needs_qr])
    else:
        s.flash("ok" if rep.ok else "warn", ". ".join(parts))
    if renamed:
        s.flash("warn", "Совпали имена, добавлен номер: " + _list(renamed, 5))
    if rep.skipped:
        s.flash("warn", "Свой набор протоколов, группа его не тронула: " + _list(rep.skipped)
                + ". «Как у группы» в списке участников вернёт.")
    for e in rep.errors[:ERRORS_SHOWN]:
        s.flash("bad", e)
    if len(rep.errors) > ERRORS_SHOWN:
        s.flash("bad", f"…и ещё ошибок: {len(rep.errors) - ERRORS_SHOWN}")


# ---------- страницы групп ----------

def groups_list(app: "App", req: "Request") -> "Response":
    try:
        gs = groups.ensure()
    except CATCH as e:
        return app.render(req, "Группы", [page_head("Группы"), card("Группы", alert_list([("bad", _err(e))]))],
                          active="/groups")
    ureg = users.list_users()
    selectable = users.selectable_protocols()
    titles = {m.id: m.short for m in manifests.load_all()[0]}
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None
    rows = []
    for g in gs.groups:
        mem = groups.members_of(gs, ureg, g.id)
        protos = t("div", [t("span", titles.get(p, p), class_="chip") for p in g.offered(selectable)], class_="chips")
        cl = t("div", [t("span", f"{(cat.platforms.get(p, p) if cat else p)}: "
                                 + " + ".join((cat.client(c) or {}).get('name', c) if cat else c for c in ids),
                         class_="chip") for p, ids in g.clients.items()], class_="chips") if g.clients else t("span", "—", class_="muted")
        people = [[t("a", u.name, href=f"/users/{u.name}"), ", "] for u in mem[:8]]
        if len(mem) > 8:
            people.append(f"и ещё {len(mem) - 8}")
        elif people:
            people[-1] = people[-1][0]
        rows.append([t("a", t("strong", g.name), href=f"/groups/{g.id}", data_swap=True), protos, cl,
                     "свой список" if g.allowlist else "общий", "ИТ" if g.install_mode == "admin" else "сами",
                     people or t("span", "пусто", class_="muted"),
                     _delete_link(g, "btn small danger")])
    head = t("a", "Новая группа", href="/connect/new", class_="btn primary", data_swap=True,
             title="Протоколы, клиенты, люди и что им отправить")
    parts: list[Any] = [page_head("Группы", f"{len(gs.groups)}", head),
                        card("Список", table(["группа", "протоколы", "клиенты", "приложения", ("ставит", "кто ставит приложения: ИТ или люди сами"),
                                              "участники", ""], rows,
                                             stack=True, empty="групп нет"),
                             help="Группа задаёт протоколы, клиентов и приложения через VPN сразу всем участникам.")]
    lone = [u.name for u in ureg.visible() if not u.group]
    if lone:
        parts.append(alert_list([("warn", "Без группы: " + ", ".join(lone))]))
    return app.render(req, "Группы", parts, active="/groups")


def _delete_link(g: groups.Group, cls: str) -> Markup:
    """«Удалить» — страница подтверждения; «Основная» не удаляется (кнопка серая, причина в подсказке).
    Из списка групп ссылка помечена from=list: «Отмена» вернёт в список, а не в группу."""
    small = "small" in cls
    if g.id == groups.MAIN_ID:
        return t("span", "Удалить" if small else "Удалить группу", class_=cls, aria_disabled="true",
                 title=groups.MAIN_KEEP)
    return t("a", "Удалить" if small else "Удалить группу",
             href=f"/groups/{g.id}/delete" + ("?from=list" if small else ""), class_=cls,
             data_swap=True)


def _members_card(g: groups.Group, gs: groups.Groups, ureg: users.Registry, al: allowlist.Allowlist,
                  csrf: str) -> Markup:
    """Участники: число и имена чипами; действия над отмеченными — одним списком с поиском."""
    others = [x for x in gs.groups if x.id != g.id]
    mem = groups.members_of(gs, ureg, g.id)
    if not mem:
        return card("Участники", t("p", "В группе никого нет.", class_="muted"))
    st = handoffviews.connection([u.name for u in mem])
    chips = []
    for u in mem:
        notes = (["свой набор протоколов: группа его не меняет"] if u.custom else []) + (
            ["свой список приложений"] if al.own(u.name) else []) + (
            ["уже подключился"] if st.connected(u.name) else [])
        chips.append(t("a", u.name, href=f"/users/{u.name}",
                       class_="chip warn" if u.custom else ("chip ok" if st.connected(u.name) else "chip"),
                       title="; ".join(notes) or None))
    acts = [t("select", [t("option", x.name, value=x.id) for x in others], name="to", aria_label="В группу")
            if others else None,
            t("button", "Перевести", type="submit", name="act", value="move", class_="btn small") if others else None,
            t("button", "Убрать из группы", type="submit", name="act", value="remove", class_="btn small",
              title=f"Перевести в группу «{groups.MAIN_NAME}»") if g.id != groups.MAIN_ID else None,
            t("button", "Как у группы", type="submit", name="act", value="reset", class_="btn small",
              title="Вернуть протоколы и приложения группы") if any(u.custom for u in mem) else None]
    form = t("form", csrf_input(csrf), _picker("Отметьте участников:", "user", [(u.name, "", False) for u in mem]),
             t("div", acts, class_="actions"), method="post", action=f"/groups/{g.id}/move", class_="stack",
             data_swap=True)
    cards_url = "/handoff?" + urllib.parse.urlencode({"group": g.id})
    left = len(mem) - len(st.on)
    go = t("div", t("a", "Карточки для раздачи", href=cards_url, class_="btn small primary", data_swap=True),
           t("a", f"Ещё не подключились: {left}", href=cards_url + "&only=pending", class_="btn small", data_swap=True)
           if st.known and 0 < left < len(mem) else None, class_="actions")
    return card("Участники", t("div", chips, class_="chips"),
                t("p", "Зелёные уже подключились (трафик за 30 дней).", class_="hint") if st.on else None,
                go,
                t("details", t("summary", "Действия с участниками"), form, class_="more"),
                extra=t("span", f"{st.counter}" if st.known else str(len(mem)), class_="chip" + (" ok" if st.on else ""),
                        title=None if not st.known else "Подключился — за 30 дней был трафик"))


def _messages_card(g: groups.Group, ctx: clientviews.Ctx, csrf: str, open_plat: str) -> Markup | None:
    """Текст инструкции по платформам: один на группу, участникам подставляется имя. Свой текст — до «вернуть»."""
    items = []
    for plat, title in ctx.cat.platforms.items():
        default = ctx.default_text(g, plat)
        if default is None:
            continue
        mine = g.messages.get(plat)
        body = mine or default
        stale = bool(mine and g.msg_sigs.get(plat) and g.msg_sigs[plat] != ctx.group_sig(g, plat))
        form = t("form", csrf_input(csrf), t("input", type="hidden", name="platform", value=plat),
                 alert_list([("warn", clientviews.STALE)]) if stale else None,
                 t("textarea", body, name="text", rows=str(min(16, len(body.splitlines()) + 2)),
                   maxlength=str(groups.MESSAGE_MAX), spellcheck="false", class_="msg-edit",
                   aria_label=f"Текст для {title}"),
                 t("div", t("button", "Сохранить", type="submit", class_="btn primary small"),
                   t("button", "Вернуть по умолчанию", type="submit", name="reset", value="1", class_="btn small")
                   if mine else None, class_="actions"),
                 method="post", action=f"/groups/{g.id}/message", class_="stack", data_swap=True)
        items.append(t("details", t("summary", title, " ", badge("свой текст", "info") if mine else None,
                                     *([" ", badge("проверьте", "warn")] if stale else [])), form,
                       open=plat == open_plat or None, class_="more"))
    if not items:
        return None
    return card("Текст для участников", t("p", "Один текст на платформу для всех. «{name}» заменится именем человека; "
                                              "ключей в тексте нет.", class_="hint"),
                t("div", items), id_="texts")


def group_page(app: "App", req: "Request", gid: str, d: Draft | None = None, errors: list[str] | None = None,
               status: int = 200, add_draft: Draft | None = None) -> "Response":
    try:
        gs = groups.ensure()
    except CATCH as e:
        return app.error(req, 500, "Группы не читаются", _err(e))
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    csrf = req.session.csrf if req.session else ""
    d = d or Draft.of_group(g)
    facts = proto_facts()
    managed = [f.id for f in facts]
    selected = d.resolved(managed)
    ureg = users.list_users()
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return app.error(req, 500, "Список приложений не читается", str(e))
    normalize_clients(d, managed, fill=False)
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="action", value="save"),
             t("input", type="hidden", name="devs", value="1"),
             t("button", "Сохранить", type="submit", hidden=True, tabindex="-1"),   # Enter в названии сохраняет, а не «Обновить»
             t("div", t("label", "Название", for_="name"),
               t("input", type="text", name="name", id="name", value=d.name, required=True,
                 maxlength=str(groups.NAME_MAX), autocomplete="off"), class_="field"),
             t("h3", "Протоколы", class_="sub-h"), _protocols_block(facts, selected),
             t("h3", "Приложения", class_="sub-h"), _mode_radios(d.install_mode),
             _clients_block(Draft(protocols=selected, clients=d.clients, devices=d.devices, install_mode=d.install_mode),
                            managed),
             t("h3", "Приложения через VPN", class_="sub-h"), _apps_block(d),
             t("div", t("button", "Сохранить", type="submit", class_="btn primary"), class_="actions"),
             method="post", action=f"/groups/{g.id}", class_="stack", data_swap=True)
    add = t("details", t("summary", "＋ Добавить людей списком"),
            t("form", csrf_input(csrf), _users_block(add_draft or Draft(new_users="", existing=[]), g.id),
              t("button", "Проверить список", type="submit", class_="btn primary"),
              method="post", action=f"/groups/{g.id}/members", class_="stack", data_swap=True),
            class_="card more", open=bool(add_draft) or None)
    others = [x for x in gs.groups if x.id != g.id]
    mem_users = groups.members_of(gs, ureg, g.id)
    names = [u.name for u in mem_users]
    own = [u.name for u in mem_users if u.custom]
    who = ", ".join(names[:8]) + (f" и ещё {len(names) - 8}" if len(names) > 8 else "")
    merge = t("details", t("summary", "Объединить с…"),
              t("form", csrf_input(csrf),
                t("p", "Участники этой группы перейдут в выбранную и получат её протоколы и приложения, эта группа "
                       "будет удалена, настройки выбранной не изменятся."
                       + (f" Свой набор протоколов сохранится: {', '.join(own)}." if own else ""), class_="hint"),
                t("select", [t("option", x.name, value=x.id) for x in others], name="to", aria_label="Объединить с группой"),
                t("button", "Объединить", type="submit", class_="btn small"),
                method="post", action=f"/groups/{g.id}/merge", class_="actions", data_swap=True,
                data_confirm=f"Объединить: участники «{g.name}» ({who or 'никого нет'}) перейдут в выбранную "
                             f"группу и получат её протоколы и приложения, «{g.name}» будет удалена."
                             + (f" Свой набор протоколов сохранится: {', '.join(own)}." if own else "")
                             + " Отменить нельзя."),
              class_="more") if others and g.id != groups.MAIN_ID else None
    delete = _delete_link(g, "btn danger")
    parts: list[Any] = [page_head(g.name, "группа", t("a", "← Группы", href="/groups", class_="btn small", data_swap=True), top=False)]
    if errors:
        parts.append(alert_list([("bad", e) for e in errors]))
    ctx = clientviews.Ctx.load()
    parts += [_members_card(g, gs, ureg, al, csrf),
              add, distviews.card_for(gs, g.id, f"/groups/{g.id}", csrf) if g.install_mode == "admin" else None,
              _messages_card(g, ctx, csrf, req.query.get("m", "")) if ctx else None,
              card("Настройки группы", form,
                        help="Сохранение применяется ко всем участникам один раз; в сообщении — кому нужен новый QR."),
              t("div", delete, merge, class_="actions")]
    return app.render(req, g.name, parts, active="/groups", status=status)


def group_get(app: "App", req: "Request", gid: str) -> "Response":
    return group_page(app, req, gid)


def _done(app: "App", req: "Request", rep: groups.GroupReport, back: str) -> "Response":
    app.invalidate("status")
    app.invalidate_links()
    flash_report(req, rep)
    return _redirect(back)


def group_save(app: "App", req: "Request", gid: str) -> "Response":
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    d = Draft.from_form(req)
    if req.form.get("go") == "refresh":
        return group_page(app, req, gid, d)
    try:
        selectable = users.selectable_protocols()
        # «Основная» следует за включением протоколов, пока в форме отмечено всё включённое
        protos = [groups.ALL] if g.all_protocols and set(selectable) <= set(d.protocols) else d.protocols
        rep = groups.update(g.id, name=d.name, protocols=protos, clients=d.clients, allow=d.allow_arg(),
                            install_mode=d.install_mode)
    except CATCH as e:
        return group_page(app, req, gid, d, [_err(e)], 422)
    return _done(app, req, rep, f"/groups/{g.id}")


def group_message(app: "App", req: "Request", gid: str) -> "Response":
    plat = req.form.get("platform", "")[:20]
    back = f"/groups/{gid}?" + urllib.parse.urlencode({"m": plat}) + "#texts"
    try:
        g = groups.Groups.load().require(gid)
        text: str | None = None if req.form.get("reset") else groups.clean_message(req.form.get("text", ""))
        ctx = clientviews.Ctx.load()
        if text and ctx is not None and text == ctx.default_text(g, plat):
            text = None  # совпал с умолчанием — не замораживаем версии приложений в файле
        groups.set_message(g.id, plat, text, ctx.group_sig(g, plat) if ctx is not None else None)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(back)
    req.session.flash("ok", "Текст сохранён" if text else "Текст по умолчанию")
    return _redirect(back)


def group_members(app: "App", req: "Request", gid: str) -> "Response":
    """«Добавить людей списком»: без confirm — предпросмотр (что получится из текста), с confirm — создание."""
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    text = req.form.get("users_new", "")[:people.TEXT_MAX]
    existing = [n[:32] for n in req.multi.get("existing", [])][:200]
    try:
        plan = people.plan_for_registry(text)
        if not plan.ok:   # список не теряется: форма открыта с тем же текстом
            return group_page(app, req, gid, errors=[plan.error], status=422,
                              add_draft=Draft(new_users=text, existing=existing))
        if req.form.get("go") == "edit":
            return group_page(app, req, gid, add_draft=Draft(new_users=text, existing=existing))
        if plan.rows and not req.form.get("confirm"):
            return _members_preview(app, req, g, plan, text, existing)
        rep = groups.add_members(g.id, plan.pairs(), existing)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{g.id}")
    rep.message = "участники добавлены"
    app.invalidate("status")
    app.invalidate_links()
    flash_report(req, rep, _renamed(plan))
    return _redirect(f"/groups/{g.id}")


def _members_preview(app: "App", req: "Request", g: groups.Group, plan: people.Plan, text: str,
                     existing: list[str]) -> "Response":
    csrf = req.session.csrf if req.session else ""
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="users_new", value=text),
             t("input", type="hidden", name="confirm", value="1"),
             [t("input", type="hidden", name="existing", value=n) for n in existing],
             t("div", t("button", f"Создать: {len(plan.rows)}", type="submit", class_="btn primary"),
               t("button", "← Изменить список", type="submit", name="go", value="edit", class_="btn", formnovalidate=True),
               class_="actions"),
             method="post", action=f"/groups/{g.id}/members", class_="stack", data_swap=True)
    body = card(f"Проверьте список: группа «{g.name}»", t("p", _preview_summary(plan, len(existing)), class_="hint"),
                _preview_rows(plan), form)
    return app.render(req, g.name, t("div", [page_head(g.name, "добавление людей"), body], data_expanded=True),
                      active="/groups")


def _names(req: "Request", key: str) -> list[str]:
    return list(dict.fromkeys(n[:32] for n in req.multi.get(key, [])[:200]))


def group_move(app: "App", req: "Request", gid: str) -> "Response":
    act = req.form.get("act", "move")
    to = {"remove": groups.MAIN_ID, "reset": gid}.get(act, req.form.get("to", "")[:40])
    names = _names(req, "user")
    if act == "remove" and gid == groups.MAIN_ID:
        req.session.flash("bad", f"Из «{groups.MAIN_NAME}» убирать некуда: переведите в другую группу")
        return _redirect(f"/groups/{gid}")
    if not names:
        req.session.flash("warn", "Никого не выбрано")
        return _redirect(f"/groups/{gid}")
    try:
        gs = groups.Groups.load()
        here = {u.name for u in groups.members_of(gs, users.list_users(), gid)}
        if any(n not in here for n in names):
            raise groups.GroupError("отмеченные не из этой группы: обновите страницу")
        rep = groups.move_many(names, to)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    rep.message = "готово"
    return _done(app, req, rep, f"/groups/{gid}")


def group_delete_confirm(app: "App", req: "Request", gid: str) -> "Response":
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    csrf = req.session.csrf if req.session else ""
    back = t("a", "Отмена", href="/groups" if req.query.get("from") == "list" else f"/groups/{g.id}",
             class_="btn", data_swap=True)
    if g.id == groups.MAIN_ID:
        body = card(f"«{g.name}» не удаляется", t("p", groups.MAIN_KEEP + "."), back)
        return app.render(req, "Удаление группы", [page_head("Удаление группы"), body], active="/groups")
    members = groups.members_of(gs, users.list_users(), g.id)
    mem = [u.name for u in members]
    own = [u.name for u in members if u.custom]
    if mem:
        gone = [n for n in mem if n != users.OWNER]
        kept_own = [n for n in own if n not in gone]
        choice: Any = t("div",
                        t("label", t("input", type="radio", name="members", value="move", checked=True),
                          t("span", f"Перевести в группу «{groups.MAIN_NAME}»",
                            t("span", f" ({len(mem)}): " + ", ".join(mem), class_="muted small"),
                            t("span", "Свой набор протоколов сохранится: " + ", ".join(own), class_="hint")
                            if own else None, class_="opt-title"),
                          class_="opt-row"),
                        t("label", t("input", type="radio", name="members", value="delete"),
                          t("span", "Удалить вместе с группой",
                            t("span", " (" + (", ".join(gone) or "никого") + ")", class_="muted small"),
                            t("span", f"{users.OWNER} не удаляется — он перейдёт в «{groups.MAIN_NAME}»"
                              + (" (свой набор протоколов сохранится)" if kept_own else ""), class_="hint")
                            if users.OWNER in mem else None,
                            t("span", "Креды удалённых будут стёрты из протоколов, ссылки и QR перестанут работать.",
                              class_="hint") if gone else None, class_="opt-title"),
                          class_="opt-row"))
    else:
        choice = t("input", type="hidden", name="members", value="move")
    shown = [t("input", type="hidden", name="shown", value="1")] + [
        t("input", type="hidden", name="expected", value=n) for n in mem]
    form = t("form", csrf_input(csrf), shown, choice,
             t("div", t("button", "Удалить группу", type="submit", class_="btn danger-solid"), back, class_="actions"),
             method="post", action=f"/groups/{g.id}/delete", class_="stack", data_swap=True)
    body = card(f"Удалить группу «{g.name}»?",
                t("p", f"В группе участников: {len(mem)}. Что с ними сделать?" if mem else
                  "В группе никого нет. Группа будет удалена."), form, cls="danger-zone")
    return app.render(req, "Удаление группы", [page_head("Удаление группы"), body], active="/groups")


def group_delete(app: "App", req: "Request", gid: str) -> "Response":
    mode = req.form.get("members", "move")
    s = req.session
    try:
        if mode not in ("move", "delete"):
            raise groups.GroupError("выберите, что сделать с участниками")
        expected = list(dict.fromkeys(req.multi.get("expected", []))) if req.form.get("shown") == "1" else None
        rep = groups.delete_group(gid, mode, expected)
    except groups.MembersChanged as e:
        s.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}/delete")
    except CATCH as e:
        s.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    app.invalidate("status")
    app.invalidate_links()
    parts = [rep.message]
    if rep.deleted:
        parts.append("удалены: " + ", ".join(rep.deleted))
    if rep.moved:
        parts.append(f"переведены в «{rep.group.name}»: " + ", ".join(rep.moved))
    s.flash("ok" if rep.removed and not rep.errors else "warn", ". ".join(parts))
    if rep.skipped:
        s.flash("warn", "Свой набор протоколов, группа его не тронула: " + ", ".join(rep.skipped))
    for e in rep.errors:
        s.flash("bad", e)
    return _redirect("/groups" if rep.removed else f"/groups/{gid}")


def group_merge(app: "App", req: "Request", gid: str) -> "Response":
    to = req.form.get("to", "")[:40]
    try:
        rep = groups.merge_groups(gid, to)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    return _done(app, req, rep, f"/groups/{rep.group.id}")
