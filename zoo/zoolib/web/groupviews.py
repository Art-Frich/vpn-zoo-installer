"""Мастер «Новое подключение» (/connect/new) и страницы групп (/groups): веб поверх zoolib.groups.

Мастер — один адрес и четыре шага без перезагрузки (форма data-swap): протоколы → клиенты →
люди и приложения → раздача. Состояние между шагами лежит в скрытых полях формы (на сервере
ничего не хранится), каждый шаг проверяется сервером, запись — одна, в конце третьего шага.
Страница группы — те же блоки шагов 1–3 в одной форме."""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import allowlist, clients, groups, manifests, protolib, users
from ..fsutil import LockTimeout
from ..probe import history, live, rank, verdicts
from . import allowviews, clientviews, userviews
from .html import Markup, badge, card, csrf_input, post_button, t, table
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

STEPS = ("Протоколы", "Клиенты", "Люди", "Раздача")
LAYER = {"tcp": "TCP", "udp": "UDP", "tcp+udp": "TCP+UDP"}
# Полевой тест 05.10.2026 (находка 7): у VLESS+Vision новые соединения рвутся на части путей,
# Hysteria2 и XHTTP устойчивы — основной Hysteria2, запасные XHTTP и AmneziaWG
DEFAULT_PROTOS = ("hysteria2", "vless-xhttp", "amneziawg")
# ориентиры из PLAN-builder и каталога клиентов, не замеры: на странице подписаны как ориентир
TIPS = {
    "vless-reality": "TCP: подходит там, где режут UDP.",
    "vless-xhttp": "TCP; ориентир — домашний Wi-Fi.",
    "hysteria2": "UDP; ориентир — мобильная сеть.",
    "amneziawg": "UDP; ориентир — запасной вариант.",
    "tuic": "UDP.",
    "ss2022": "По умолчанию выключен: в полевом тесте соединения теряли данные.",
}
TIPS_NOTE = "Подсказки без цифр — ориентир, не замер: смотрите «с сервера» и «у клиентов»."
CATCH = (groups.GroupError, users.UserError, allowlist.AllowlistError, LockTimeout, protolib.ProtoError)
MAIN_PLATFORMS = clientviews.MAIN_PLATFORMS


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
    clients_for: str = ""   # протоколы, под которые клиенты уже выбраны на шаге 2 (изменились — набор пересчитывается)
    allow_mode: str = "common"
    allow: dict[str, list[str]] = field(default_factory=lambda: {p: [] for p in allowlist.PLATFORMS})
    new_users: str = ""
    existing: list[str] = field(default_factory=list)

    @classmethod
    def from_form(cls, req: "Request") -> "Draft":
        f, m = req.form, req.multi
        return cls(
            name=f.get("name", "")[:200].strip(),
            protocols=[p[:40] for p in m.get("proto", [])][:20],
            clients={k[7:]: ids for k, vals in m.items() if k.startswith("client:")
                     if (ids := groups.client_ids([v[:40] for v in vals][:groups.CLIENTS_MAX + 1]))},
            clients_for=f.get("clients_for", "")[:400],
            allow_mode="own" if f.get("allow_mode") == "own" else "common",
            allow={p: [v[:128] for v in m.get(p, [])][:allowlist.LIST_MAX + 1] for p in allowlist.PLATFORMS},
            new_users=f.get("users_new", "")[:4000],
            existing=[n[:32] for n in m.get("existing", [])][:200])

    @classmethod
    def of_group(cls, g: groups.Group) -> "Draft":
        d = cls(name=g.name, protocols=list(g.protocols), clients={p: list(v) for p, v in g.clients.items()})
        if g.allowlist:
            d.allow_mode, d.allow = "own", {p: list(g.allowlist[p]) for p in allowlist.PLATFORMS}
        return d

    def resolved(self, managed: list[str]) -> list[str]:
        return list(managed) if groups.ALL in self.protocols else [p for p in self.protocols if p in managed]

    def allow_arg(self) -> dict[str, list[str]] | None:
        return dict(self.allow) if self.allow_mode == "own" else None


def _hidden(d: Draft, shown: int, own_allow: bool = True) -> list[Markup]:
    """Состояние шагов, которых сейчас не видно, — скрытыми полями."""
    out: list[Markup] = []

    def add(name: str, value: str) -> None:
        out.append(t("input", type="hidden", name=name, value=value))

    if shown != 1:
        add("name", d.name)
        for p in d.protocols:
            add("proto", p)
    if shown != 2:
        if d.clients_for:
            add("clients_for", d.clients_for)
        for plat, ids in d.clients.items():
            for cid in ids:
                add(f"client:{plat}", cid)
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
        bits = [f"{d['rtt_ms']:.0f} мс" if d["rtt_ms"] is not None else None,
                f"{d['speed']:.0f} Мбит/с" if d["speed"] is not None else None]
        text = "с сервера: " + (" · ".join(b for b in bits if b) or "работает") + (" (устарело)" if stale else "")
        return t("span", text, class_="chip" if stale else "chip ok")

    def rank_chip(self) -> Markup | None:
        r = self.rank
        if not r or not r["n"]:
            return None
        text = f"у клиентов: {int(r['ok'])} из {int(r['n'])}"
        few = r["n"] < rank.LOW_SAMPLES
        return t("span", text + (" · мало данных" if few else ""), class_="chip" if few or r["ok"] < r["n"] else "chip ok",
                 title="клиентские пробы за 30 дней (zoo probe --rank)")


def proto_facts() -> list[Fact]:
    managed, _ = users.managed_protocols()
    mans = {m.id: m for m in manifests.load_all()[0]}
    latest, ranks, now = live.latest(), _rank_facts(), time.time()
    return [Fact(p, mans[p].short if p in mans else p, LAYER.get(mans[p].layer, "") if p in mans else "",
                 latest.get(p), ranks.get(p), now) for p in managed]


def suggest(facts: list[Fact]) -> list[str]:
    """Предвыбор: топ по клиентским пробам (если они есть), иначе дефолты из рабочих протоколов."""
    ranked = sorted((f for f in facts if f.rank and f.rank["top"] and not f.down),
                    key=lambda f: (-f.rank["top"], -f.rank["score"] / max(f.rank["ctx"], 1)))  # type: ignore[index]
    ids = [f.id for f in ranked][:3]
    if ids:
        return ids
    ok = [f.id for f in facts if not f.down]
    return ([p for p in DEFAULT_PROTOS if p in ok] or ok)[:3]


def _protocols_block(facts: list[Fact], selected: list[str]) -> Markup:
    """Карточки-чекбоксы протоколов: выбранные первыми, первый выбранный — основной."""
    by_id = {f.id: f for f in facts}
    order = [p for p in selected if p in by_id] + [f.id for f in facts if f.id not in selected]
    first = next((p for p in order if p in selected), None)
    items = []
    for pid in order:
        f = by_id[pid]
        chips = [t("span", f.layer, class_="chip") if f.layer else None, f.live_chip(), f.rank_chip()]
        tip = TIPS.get(pid)
        extra = _variants(pid)
        if extra:
            tip = (tip + " " if tip else "") + "В той же учётке: " + ", ".join(extra) + "."
        items.append(t("label", t("input", type="checkbox", name="proto", value=pid, checked=pid in selected),
                       t("span", t("span", t("strong", f.title), " ",
                                   badge("основной", "info") if pid == first else None,
                                   class_="opt-title"),
                         t("div", chips, class_="chips"),
                         t("span", tip, class_="hint") if tip else None, class_="opt-body"),
                       class_="opt"))
    if not items:
        return alert_list([("warn", "Нет включённых протоколов с пользователями.")])
    for title, why in _not_selectable(set(by_id)):
        items.append(t("div", t("span", t("span", t("strong", title), class_="opt-title"),
                                t("span", why, class_="hint"), class_="opt-body"), class_="opt off"))
    return t("div", t("div", items, class_="opts"), t("p", TIPS_NOTE, class_="hint"))


def _variants(pid: str) -> list[str]:
    """Включённые варианты с общими учётками (hysteria2-obfs у hysteria2): выбираются вместе с ним."""
    libs = set(protolib.list_libs())
    return [m.short for m in manifests.load_all()[0]
            if m.enabled and m.id != pid and users.shared_module(m, libs) == pid]


def _not_selectable(shown: set[str]) -> list[tuple[str, str]]:
    """Протоколы сервера, которых нет среди выбираемых: выключенные — серой строкой, чтобы были видны все."""
    libs = set(protolib.list_libs())
    out = []
    for m in manifests.load_all()[0]:
        if m.id in shown or (m.enabled and users.shared_module(m, libs)):
            continue
        out.append((m.short, "выключен на сервере — включается на «Обзоре»" if not m.enabled else "без учёток пользователей"))
    return out


# ---------- клиенты (шаг 2) ----------

def _proto_names(cat: clients.Catalog) -> dict[str, str]:
    """Названия протоколов: как в плитках сервера, для неизвестных серверу — из каталога клиентов."""
    names = {p: d["title"] for p, d in cat.protocols.items()}
    names.update({m.id: m.short for m in manifests.load_all()[0]})
    return names


def set_summary(cat: clients.Catalog, plat: str, protocols: list[str], ids: list[str],
                names: dict[str, str]) -> tuple[str, str]:
    """Строка над платформой: чем набор покрывает протоколы группы. Второе — вид: ok, warn, muted."""
    if not ids:
        return "Платформа не нужна: ничего не отмечено", "muted"
    done, miss = groups.coverage(cat, plat, protocols, ids)
    label = " + ".join((cat.client(i) or {}).get("name", i) for i in ids)
    text = f"Набор: {label} — покрывает {len(done)} из {len(done) + len(miss)}"
    if miss:
        return text + ": для " + ", ".join(names.get(p, p) for p in miss) + " нет клиента", "warn"
    return text, "ok"


def _client_option(plat: str, o: dict[str, Any], checked: bool, suggested: bool, cache: dict[str, Any],
                   cat: clients.Catalog, names: dict[str, str]) -> Markup:
    c = o["client"]
    cid = c["id"]
    caveats = [f"{names.get(p, p)}: {c['protocols'][p]['note']}" for p in o["covers"] if c["protocols"][p].get("note")]
    chips = [t("span", names.get(p, p), class_="chip warn" if c["protocols"][p]["s"] == "warn" else "chip ok",
               title=c["protocols"][p].get("note") or "умеет этот протокол") for p in o["covers"]]
    chips.append(t("span", "приложения: " + cat.raw["per_app"][c["per_app"]], class_="chip"))
    if o["no_ru_store"]:
        chips.append(t("span", "нет в App Store РФ", class_="chip warn", title=clientviews.FOREIGN_STORE))
    if not c["verified"]["device"]:
        chips.append(t("span", "на устройстве не проверено", class_="chip", title=clientviews.UNVERIFIED))
    ver = clientviews._version_cell(c, cache, plat)
    return t("label", t("input", type="checkbox", name=f"client:{plat}", value=cid, checked=checked,
                        data_covers=" ".join(o["covers"]), data_name=c["name"]),
             t("span", t("span", t("strong", c["name"]), " ", ver, " ",
                         badge("рекомендуем", "ok") if suggested else None, class_="opt-title"),
               t("div", chips, class_="chips"),
               t("div", clientviews._link_anchors(c["platforms"][plat]), class_="chips"),
               t("span", "; ".join(caveats), class_="hint") if caveats else None,
               t("details", t("summary", "подробнее"), t("p", c["notes"], class_="hint"), class_="more")
               if c.get("notes") else None, class_="opt-body"), class_="opt")


def _clients_block(d: Draft, managed: list[str]) -> Markup:
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return alert_list([("bad", str(e))])
    cache = clients.load_cache()
    names = _proto_names(cat)
    protocols = d.resolved(managed)
    real = [p for p in protocols if p in cat.protocols and not cat.protocols[p].get("pseudo")]
    main_, other = [], []
    for plat, title in cat.platforms.items():
        opts = {o["client"]["id"]: o for o in groups.client_options(cat, plat, protocols)}
        if not opts:
            body: Any = t("p", "Нет клиента под выбранные протоколы.", class_="muted small")
            sum_attrs: dict[str, Any] = {}
        else:
            chosen = [i for i in d.clients.get(plat, []) if i in opts]
            sugg = groups.suggest_clients(cat, plat, protocols)
            first = [*sugg, *[i for i in chosen if i not in sugg]]
            rest = [i for i in opts if i not in first]
            text, kind = set_summary(cat, plat, protocols, chosen, names)

            def cards(ids: list[str]) -> list[Markup]:
                return [_client_option(plat, opts[i], i in chosen, i in sugg, cache, cat, names) for i in ids]

            body = t("div", t("p", text, class_="plat-sum " + kind, data_sumtext=True),
                     t("div", cards(first), class_="opts"),
                     t("details", t("summary", f"Другие клиенты ({len(rest)})"),
                       t("div", cards(rest), class_="opts"), class_="more") if rest else None)
            sum_attrs = {"data_sum": True, "data_protos": " ".join(real),
                         "data_names": "|".join(names.get(p, p) for p in real)}
        (main_ if plat in MAIN_PLATFORMS else other).append(
            t("fieldset", t("legend", title), body, class_="plat", **sum_attrs))
    return t("div", main_,
             t("details", t("summary", "Другие платформы"), other, class_="more") if other else None,
             t("p", clientviews.UNVERIFIED, class_="hint"))


def normalize_clients(d: Draft, managed: list[str], fill: bool = True) -> None:
    """Выбор клиентов под текущие протоколы: остаются подходящие. fill (мастер): пока клиенты не выбраны под эти
    же протоколы (первый показ шага или протоколы сменили) — предлагается набор, покрывающий протоколы;
    после — отмеченное сохраняется как есть, платформа без отметок — «не нужна»."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return
    protocols = d.resolved(managed)
    stale = fill and d.clients_for != ",".join(protocols)
    fixed: dict[str, list[str]] = {}
    for plat in cat.platforms:
        opts = groups.client_options(cat, plat, protocols)
        if not opts:
            continue
        ids = {o["client"]["id"] for o in opts}
        got = groups.suggest_clients(cat, plat, protocols) if stale else [i for i in d.clients.get(plat, []) if i in ids]
        if got:
            fixed[plat] = got
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


def _existing_block(d: Draft, group_id: str | None = None) -> Markup | None:
    gs = groups.Groups.load()
    boxes = []
    for u in users.list_users().visible():
        if group_id and u.group == group_id:
            continue
        g = gs.get(u.group)
        boxes.append(t("label", t("input", type="checkbox", name="existing", value=u.name, checked=u.name in d.existing),
                       u.name, t("span", f" {g.name}" if g else "", class_="muted small")))
    return t("div", t("span", "Уже есть (перейдут в группу):", class_="label"), t("div", boxes, class_="checks"),
             class_="field") if boxes else None


def _users_block(d: Draft, group_id: str | None = None) -> Markup:
    return t("div",
             t("div", t("label", "Новые пользователи", for_="users_new"),
               t("textarea", d.new_users, name="users_new", id="users_new", rows="4", maxlength="4000",
                 placeholder="masha сестра\npetya", autocomplete="off", autocapitalize="none", spellcheck="false"),
               t("div", "По одному в строке: имя латиницей, после него — заметка.", class_="hint"), class_="field"),
             _existing_block(d, group_id), class_="stack")


# ---------- мастер ----------

def _stepper(step: int) -> Markup:
    return t("ol", [t("li", t("span", str(i), class_="n"), name, class_="cur" if i == step else ("done" if i < step else None),
                      aria_current="step" if i == step else None) for i, name in enumerate(STEPS, 1)],
             class_="stepper")


def _wizard(app: "App", req: "Request", step: int, d: Draft, errors: list[str] | None = None,
            status: int = 200) -> "Response":
    csrf = req.session.csrf if req.session else ""
    facts = proto_facts()
    managed = [f.id for f in facts]
    gs = groups.Groups.load()
    if step == 1:
        if not d.name:
            d.name = gs.next_name()
        if not d.protocols:
            d.protocols = suggest(facts)
        body = [t("div", t("label", "Название группы", for_="name"),
                  t("input", type="text", name="name", id="name", value=d.name, required=True, maxlength=str(groups.NAME_MAX),
                    autocomplete="off"), class_="field"),
                _protocols_block(facts, d.protocols)]
        title, hint = "Протоколы", "Первый отмеченный — основной, остальные — запасные (обычно 2–3)."
    elif step == 2:
        normalize_clients(d, managed)
        body = [t("input", type="hidden", name="clients_for", value=d.clients_for), _clients_block(d, managed)]
        title = "Клиенты"
        hint = ("Ни одно приложение не умеет все протоколы, поэтому отмечен набор, который вместе их покрывает. "
                "Платформа, где ничего не отмечено, не нужна.")
    else:
        body, title = [_users_block(d), t("h3", "Приложения через VPN", class_="sub-h"), _apps_block(d)], "Люди"
        hint = "Кто подключается и какие приложения идут через VPN."
    last = step == 3
    nav = t("div",
            t("button", "Создать подключение" if last else "Далее →", type="submit", name="go",
              value="create" if last else "next", class_="btn primary"),
            t("button", "← Назад", type="submit", name="go", value="back", class_="btn", formnovalidate=True)
            if step > 1 else None, class_="wiz-nav")
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="step", value=str(step)),
             _hidden(d, step), body, nav, method="post", action="/connect/new", class_="stack", data_swap=True)
    parts: list[Any] = [page_head("Новое подключение", "от «хочу VPN для мамы» до «мама подключена»"), _stepper(step)]
    if errors:
        parts.append(alert_list([("bad", e) for e in errors]))
    parts.append(card(f"{step}. {title}", t("p", hint, class_="hint"), form))
    # data-expanded: живое обновление не заменяет страницу, пока идёт мастер (иначе шаг сбросился бы на первый)
    return app.render(req, "Новое подключение", t("div", parts, class_="wizard", data_expanded=True),
                      active="/groups", status=status)


def connect_page(app: "App", req: "Request") -> "Response":
    if not users.managed_protocols()[0]:
        return app.render(req, "Новое подключение", [page_head("Новое подключение"), card(
            "Нет протоколов", alert_list([("warn", "Нет включённых протоколов, куда можно добавить пользователя.")]))],
            active="/groups")
    try:
        groups.ensure()
    except CATCH:
        pass
    return _wizard(app, req, 1, Draft())


def _check(d: Draft, step: int) -> list[str]:
    """Первая ошибка шага (и предыдущих): сервер не верит скрытым полям."""
    try:
        gs = groups.Groups.load()
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


def connect_post(app: "App", req: "Request") -> "Response":
    try:
        groups.ensure()
    except CATCH:
        pass
    d = Draft.from_form(req)
    try:
        step = min(max(int(req.form.get("step", "1")), 1), 3)
    except ValueError:
        step = 1
    go = req.form.get("go", "next")
    if go == "back":
        return _wizard(app, req, max(step - 1, 1), d)
    errors = _check(d, step)
    if errors:
        return _wizard(app, req, step, d, errors, 422)
    if go != "create" or step < 3:
        return _wizard(app, req, min(step + 1, 3), d)
    try:
        rep = groups.connect(d.name, d.protocols, d.clients, d.allow_arg(), groups.parse_new_users(d.new_users),
                             d.existing)
    except CATCH as e:
        return _wizard(app, req, 3, d, [_err(e)], 422)
    app.invalidate("status")
    app.invalidate_links()
    if rep.removed:
        # никого не добавили, пустая группа убрана: форма остаётся на шаге 3, повтор с тем же названием возможен
        return _wizard(app, req, 3, d, rep.errors or ["Никого не удалось добавить"], 422)
    flash_report(req, rep)
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
    prefer, order = clientviews.group_prefs(g)
    blocks: list[Any] = []
    for u in members:
        links, _ = userviews._cached_links(app, u.name)
        blocks.append(t("div", t("h3", t("a", u.name, href=f"/users/{u.name}"), " ", t("span", u.note, class_="muted small")
                                  if u.note else None, class_="sub-h"),
                        clientviews.handoff_card(links, prefer, uid=f"{u.name}-", heading=f"{u.name}: что отправить",
                                                 order=order)
                        or t("p", "Ссылок пока нет.", class_="muted"),
                        t("p", t("a", "Ссылки и QR →", href=f"/users/{u.name}"), class_="small")))
    summary = card(f"Группа «{g.name}»", _summary(g),
                   extra=t("a", "Настроить", href=f"/groups/{g.id}", class_="btn small", data_swap=True))
    body = [page_head("Новое подключение", "готово: раздайте пакеты"), _stepper(4), summary,
            *(blocks or [alert_list([("warn", "Никого не добавили — раздавать нечего.")])]),
            t("p", clientviews.SEND_WARN, class_="hint")]
    return app.render(req, "Новое подключение", body, active="/groups")


def _summary(g: groups.Group) -> Markup:
    managed, _ = users.managed_protocols()
    titles = {m.id: m.short for m in manifests.load_all()[0]}
    try:
        cat = clients.load()
        names = {p: " + ".join((cat.client(c) or {}).get("name", c) for c in ids) for p, ids in g.clients.items()}
        plat = cat.platforms
    except clients.ClientsError:
        names, plat = {p: " + ".join(ids) for p, ids in g.clients.items()}, {}
    chips = [t("span", titles.get(p, p), class_="chip") for p in g.resolve(managed)]
    cl = [t("span", f"{plat.get(p, p)}: {n}", class_="chip info") for p, n in names.items()]
    return t("div", t("div", chips, class_="chips"), t("div", cl, class_="chips") if cl else None,
             t("p", "приложения: " + ("свой список группы" if g.allowlist else "общий список"), class_="hint"))


def flash_report(req: "Request", rep: groups.GroupReport) -> None:
    s = req.session
    if s is None:
        return
    parts = [f"«{rep.group.name}»: {rep.message}"]
    if rep.created:
        parts.append("создано: " + ", ".join(rep.created))
    if rep.moved:
        parts.append("переведено: " + ", ".join(rep.moved))
    if rep.needs_qr and not rep.created:
        s.flash("ok" if rep.ok else "warn", ". ".join(parts) + ". Новые QR/файлы нужны:",
                [(n, f"/users/{n}") for n in rep.needs_qr])
    else:
        s.flash("ok" if rep.ok else "warn", ". ".join(parts))
    if rep.skipped:
        s.flash("warn", "Свой набор протоколов, группа его не тронула: " + ", ".join(rep.skipped)
                + ". «Как у группы» в списке участников вернёт.")
    for e in rep.errors:
        s.flash("bad", e)


# ---------- страницы групп ----------

def groups_list(app: "App", req: "Request") -> "Response":
    try:
        gs = groups.ensure()
    except CATCH as e:
        return app.render(req, "Группы", [page_head("Группы"), card("Группы", alert_list([("bad", _err(e))]))],
                          active="/groups")
    ureg = users.list_users()
    managed, _ = users.managed_protocols()
    titles = {m.id: m.short for m in manifests.load_all()[0]}
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None
    rows = []
    for g in gs.groups:
        mem = groups.members_of(gs, ureg, g.id)
        protos = t("div", [t("span", titles.get(p, p), class_="chip") for p in g.resolve(managed)], class_="chips")
        cl = t("div", [t("span", f"{(cat.platforms.get(p, p) if cat else p)}: "
                                 + " + ".join((cat.client(c) or {}).get('name', c) if cat else c for c in ids),
                         class_="chip") for p, ids in g.clients.items()], class_="chips") if g.clients else t("span", "—", class_="muted")
        people = [[t("a", u.name, href=f"/users/{u.name}"), ", "] for u in mem[:8]]
        if len(mem) > 8:
            people.append(f"и ещё {len(mem) - 8}")
        elif people:
            people[-1] = people[-1][0]
        rows.append([t("a", t("strong", g.name), href=f"/groups/{g.id}", data_swap=True), protos, cl,
                     "свой список" if g.allowlist else "общий", people or t("span", "пусто", class_="muted")])
    head = t("a", "Новое подключение", href="/connect/new", class_="btn primary", data_swap=True)
    parts: list[Any] = [page_head("Группы", f"{len(gs.groups)}", head),
                        card("Список", table(["группа", "протоколы", "клиенты", "приложения", "участники"], rows,
                                             stack=True, empty="групп нет"),
                             help="Группа задаёт протоколы, клиентов и приложения через VPN сразу всем участникам.")]
    lone = [u.name for u in ureg.visible() if not u.group]
    if lone:
        parts.append(alert_list([("warn", "Без группы: " + ", ".join(lone))]))
    return app.render(req, "Группы", parts, active="/groups")


def _member_rows(g: groups.Group, gs: groups.Groups, ureg: users.Registry, al: allowlist.Allowlist,
                 csrf: str) -> list[list[Any]]:
    others = [x for x in gs.groups if x.id != g.id]
    rows = []
    for u in groups.members_of(gs, ureg, g.id):
        proto = (t("span", "свой набор", class_="chip warn", title="протоколы заданы вручную, группа их не меняет")
                 if u.custom else t("span", "как у группы", class_="chip"))
        apps = (t("a", "свой список", href=f"/apps?user={u.name}", class_="chip info") if al.own(u.name)
                else t("span", "как у группы" if g.allowlist else "общий", class_="chip"))
        move = t("form", csrf_input(csrf), t("input", type="hidden", name="user", value=u.name),
                 t("select", [t("option", x.name, value=x.id) for x in others], name="to", aria_label="В группу"),
                 t("button", "Перевести", type="submit", class_="btn small"),
                 method="post", action=f"/groups/{g.id}/move", class_="inline", data_swap=True) if others else None
        reset = post_button(f"/groups/{g.id}/move", "Как у группы", csrf, "btn small", {"user": u.name, "to": g.id},
                            title="Вернуть протоколы группы") if u.custom else None
        rows.append([t("a", t("strong", u.name), href=f"/users/{u.name}"), proto, apps,
                     t("div", move, reset, class_="actions")])
    return rows


def group_page(app: "App", req: "Request", gid: str, d: Draft | None = None, errors: list[str] | None = None,
               status: int = 200) -> "Response":
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
             t("div", t("label", "Название", for_="name"),
               t("input", type="text", name="name", id="name", value=d.name, required=True,
                 maxlength=str(groups.NAME_MAX), autocomplete="off"), class_="field"),
             t("h3", "Протоколы", class_="sub-h"), _protocols_block(facts, selected),
             t("h3", "Клиенты", class_="sub-h"), _clients_block(Draft(protocols=selected, clients=d.clients), managed),
             t("h3", "Приложения через VPN", class_="sub-h"), _apps_block(d),
             t("div", t("button", "Сохранить", type="submit", class_="btn primary"), class_="actions"),
             method="post", action=f"/groups/{g.id}", class_="stack", data_swap=True)
    members = _member_rows(g, gs, ureg, al, csrf)
    add = t("details", t("summary", "＋ Добавить участников"),
            t("form", csrf_input(csrf), _users_block(Draft(new_users="", existing=[]), g.id),
              t("button", "Добавить", type="submit", class_="btn primary"),
              method="post", action=f"/groups/{g.id}/members", class_="stack", data_swap=True), class_="card more")
    if members:
        delete: Any = t("p", "Удалить можно только пустую группу.", class_="hint")
    else:
        delete = post_button(f"/groups/{g.id}/delete", "Удалить группу", csrf, "btn danger",
                             confirm=f"Удалить группу «{g.name}»?")
    parts: list[Any] = [page_head(g.name, "группа", t("a", "← Группы", href="/groups", class_="btn small", data_swap=True))]
    if errors:
        parts.append(alert_list([("bad", e) for e in errors]))
    parts += [card("Участники", table(["пользователь", "протоколы", "приложения", ""], members, stack=True,
                                      empty="в группе никого нет")),
              add, card("Настройки группы", form,
                        help="Сохранение применяется ко всем участникам один раз; в сообщении — кому нужен новый QR."),
              t("div", delete, class_="actions")]
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
    try:
        managed, _ = users.managed_protocols()
        # «Основная» следует за включением протоколов, пока в форме отмечено всё включённое
        protos = [groups.ALL] if g.all_protocols and set(managed) <= set(d.protocols) else d.protocols
        rep = groups.update(g.id, name=d.name, protocols=protos, clients=d.clients, allow=d.allow_arg())
    except CATCH as e:
        return group_page(app, req, gid, d, [_err(e)], 422)
    return _done(app, req, rep, f"/groups/{g.id}")


def group_members(app: "App", req: "Request", gid: str) -> "Response":
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    try:
        rep = groups.add_members(g.id, groups.parse_new_users(req.form.get("users_new", "")[:4000]),
                                 [n[:32] for n in req.multi.get("existing", [])][:200])
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{g.id}")
    rep.message = "участники добавлены"
    return _done(app, req, rep, f"/groups/{g.id}")


def group_move(app: "App", req: "Request", gid: str) -> "Response":
    name, to = req.form.get("user", "")[:32], req.form.get("to", "")[:40]
    try:
        rep = groups.move_many([name], to)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    rep.message = "готово"
    return _done(app, req, rep, f"/groups/{gid}")


def group_delete(app: "App", req: "Request", gid: str) -> "Response":
    try:
        g = groups.remove(gid)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    req.session.flash("ok", f"Группа «{g.name}» удалена")
    return _redirect("/groups")
