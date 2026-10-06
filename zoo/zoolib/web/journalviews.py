"""Страница «Атаки»: кто нас щупал (zoolib.journal). Только чтение базы, ничего не меняет."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .. import journal, traffic
from ..output import human_duration
from . import charts
from .html import Markup, badge, card, empty, join, kv, t, table
from .views import _tile, alert_list, ago, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

SCOPE_BADGE = {"own": ("свой", "info"), "local": ("локальный", "muted")}
KIND_BADGE = {"port-scan": "warn", "ssh-auth": "bad", "ssh-scan": "warn", "ssh-limit": "warn", "ssh-ban": "ok",
              "hy2-auth": "bad", "reality-probe": "bad", "panel-login": "muted", "web-login": "muted"}


SORT_TITLES = {"n": "по числу", "last": "по давности"}
SEARCH_HINT = "IP или начало, cc:NL, :22, ssh"


def _state(req: "Request") -> dict[str, Any]:
    """Всё состояние страницы — в query-строке: период, свои, поиск, фильтры, сортировка, число строк, курсор, адрес."""
    g = req.query.get
    try:
        period = journal.resolve_period(g("period", "24h"))
    except ValueError:
        period = "24h"
    svc, kind, sort = g("svc", ""), g("kind", ""), g("sort", "n")
    try:
        rows = int(g("n", ""))
    except ValueError:
        rows = journal.ROWS[0]
    return {"period": period, "all": g("all") == "1", "q": g("q", "").strip()[:journal.QUERY_MAX],
            "svc": svc if svc in journal.SERVICES else "", "kind": kind if kind in journal.KINDS else "",
            "sort": sort if sort in journal.SORTS else "n", "n": rows if rows in journal.ROWS else journal.ROWS[0],
            "after": g("after", "")[:90], "ip": journal.norm_ip(g("ip", "")) or ""}


def _url(st: dict[str, Any], **over: Any) -> str:
    """Ссылка на страницу с тем же состоянием, кроме over; значения по умолчанию в адрес не попадают."""
    st = {**st, **over}
    default = {"all": False, "q": "", "svc": "", "kind": "", "sort": "n", "n": journal.ROWS[0], "after": "", "ip": ""}
    args = [(k, "1" if k == "all" else st[k]) for k in ("ip", "q", "svc", "kind", "sort", "n", "after", "all")
            if st[k] != default[k]]
    return "/journal?" + urlencode([("period", st["period"]), *args])


def _selector(st: dict[str, Any]) -> Markup:
    return t("nav", [t("a", p, href=_url(st, period=p, after=""), class_="active" if p == st["period"] else None)
                     for p in journal.PERIODS], class_="seg", aria_label="Период")


def _kind_badge(kind: str, n: int | None = None) -> Markup:
    """Вид события: нажатие открывает справку (окно kh-<вид>, см. _kind_dialogs)."""
    title = journal.KINDS[kind][1] + (f" {n}" if n is not None else "")
    return t("button", title, type="button", class_=f"badge {KIND_BADGE.get(kind, 'muted')}",
             data_dialog=f"kh-{kind}", title="Что это значит")


def _kind_dialogs() -> Markup:
    """Справка по видам событий: по окну на вид, четыре строки из journal.KIND_HELP."""
    out = []
    for kind, lines in journal.KIND_HELP.items():
        out.append(t("dialog",
                     t("div", t("h3", journal.KINDS[kind][1]),
                       t("button", "✕", type="button", class_="btn small", data_close=True, aria_label="Закрыть"),
                       class_="dlg-head"),
                     [t("p", t("strong", label + ". "), text) for label, text in zip(journal.HELP_LABELS, lines)],
                     id=f"kh-{kind}", class_="pdlg"))
    return join(out)


def _timeline(data: dict[str, Any]) -> Markup:
    tl = data["timeline"]
    if not tl["series"]:
        return empty("За период попыток не было")
    return join(charts.columns(tl["buckets"], tl["step"], tl["series"], "Попытки по времени", 1100, 260,
                               fmt=charts.count_label, nice=charts.nice_count, empty_tip="попыток не было"),
                charts.legend(tl["series"], fmt=charts.count_label))


def _group_cards(data: dict[str, Any]) -> Markup:
    """Карточки групп с попытками; группы без попыток — одной строкой «…— попыток не было»,
    а слепой детектор (REALITY) — отдельной строкой «не отслеживается», а не нулём."""
    cards = []
    for g in data["groups"]:
        if not g["n"]:
            continue
        chips = [t("span", f"{k['title']}: {k['n']}", class_="chip") for k in g["kinds"]]
        cards.append(card(g["title"],
                          t("div", t("div", charts.count_label(g["n"]), class_="num-big", title="попыток"),
                            t("div", f"адресов: {g['ips']}", class_="muted small")),
                          t("div", chips, class_="chips") if chips else None, cls="proto"))
    tracked = data.get("reality_tracked", False)
    quiet = ["REALITY, Hysteria2" if g["key"] == "proxy" and tracked else journal.GROUP_QUIET[g["key"]]
             for g in data["groups"] if not g["n"]]
    return join(t("div", cards, class_="grid") if cards else None,
                t("p", ", ".join(quiet) + " — попыток не было", class_="quiet") if quiet else None,
                t("p", "REALITY — " + journal.REALITY_UNTRACKED, class_="quiet") if not tracked else None)


def _ports_cell(ports: list[int]) -> Markup | str:
    """Не больше двух портов, остальное — «+N»; полный список в title."""
    if not ports:
        return "—"
    shown = ", ".join(map(str, ports[:2])) + (f" +{len(ports) - 2}" if len(ports) > 2 else "")
    return t("span", shown, title=", ".join(map(str, ports)))


def _ports_table(data: dict[str, Any]) -> Markup:
    mx = max((p["n"] for p in data["top_ports"]), default=0)
    return table(["порт", "попыток", "", "адресов"],
                 [[t("code", p["port"]), charts.count_label(p["n"]), charts.bar(p["n"], mx, "s2"), p["ips"]]
                  for p in data["top_ports"]], num=[1, 3], empty="нет данных", stack=True)


def _countries_table(data: dict[str, Any]) -> Markup:
    mx = max((c["n"] for c in data["countries"]), default=0)
    return table(["страна", "попыток", "", "адресов"],
                 [[("не определена" if c["cc"] == "?" else c["cc"]), charts.count_label(c["n"]),
                   charts.bar(c["n"], mx, "s3"), c["ips"]] for c in data["countries"]],
                 num=[1, 3], empty="нет данных", stack=True)


def _verdict(data: dict[str, Any]) -> tuple[str, str]:
    """Одна строка вывода: зондирования прокси и панелей нет — обычный фон, иначе — с числами."""
    by = {g["key"]: g for g in data["groups"]}
    proxy, login = by.get("proxy", {"n": 0, "ips": 0}), by.get("login", {"n": 0, "ips": 0})
    if not proxy["n"] and not login["n"]:
        return "ok", "Обычный фон: сканеры и перебор SSH"
    parts = []
    if proxy["n"]:
        parts.append(f"{'REALITY/Hy2' if data.get('reality_tracked') else 'Hysteria2'} — {proxy['n']} "
                     f"(адресов: {proxy['ips']})")
    if login["n"]:
        parts.append(f"входы в панели — {login['n']} (адресов: {login['ips']})")
    return "warn", "Щупают прокси и панели: " + "; ".join(parts)


def _help(data: dict[str, Any]) -> Markup:
    """«?» у таблицы источников: виды событий (нажать — справка), чего мы не видим, свои адреса."""
    return join(
        t("p", t("strong", "Что значат виды событий"), " — нажмите на вид"),
        t("ul", [t("li", _kind_badge(k), " ", lines[0]) for k, lines in journal.KIND_HELP.items()]),
        t("p", t("strong", "Чего мы не видим")),
        t("ul", [t("li", t("strong", b["what"] + ": "), b["why"]) for b in data["blind"]]),
        t("p", "ufw пишет блокировки не чаще 3 в минуту на весь сервер, поэтому число попыток по портам — "
               "выборка, а не итог. Журнал не хранит трафик и назначения пользователей VPN."),
        t("p", t("strong", "Свои адреса"), " — с них заходили по ключу SSH, их попытки не считаются атакой. "
                 "Добавить вручную: ", t("code", str(journal.ignore_file())), " (адрес или сеть в строке)."),
        t("ul", [t("li", t("code", o["ip"]), " · ", ago(o["ts"])) for o in data["own_ips"]])
        if data["own_ips"] else None)


# ---------- источники: поиск, фильтры, «показать ещё» ----------

def _chip(label: str, href: str, on: bool) -> Markup:
    return t("a", label, href=href, class_="chip info" if on else "chip", data_swap=True,
             aria_pressed="true" if on else None)


def _toolbar(st: dict[str, Any]) -> Markup:
    """Поле поиска (GET-форма: без JS — обычный переход) и чипы сервисов и видов."""
    hidden = [t("input", type="hidden", name=k, value=v) for k, v in (
        ("period", st["period"]), ("all", "1" if st["all"] else ""), ("svc", st["svc"]), ("kind", st["kind"]),
        ("sort", st["sort"] if st["sort"] != "n" else ""), ("n", st["n"] if st["n"] != journal.ROWS[0] else ""))
        if v]
    reset = (t("a", "сбросить", href=_url(st, q="", svc="", kind="", after=""), class_="btn small", data_swap=True)
             if st["q"] or st["svc"] or st["kind"] else None)
    form = t("form", t("input", type="search", name="q", id="jq", value=st["q"], placeholder=SEARCH_HINT,
                       maxlength=journal.QUERY_MAX, autocomplete="off", aria_label="Поиск по журналу"),
             hidden, t("button", "Найти", type="submit", class_="btn small"), reset,
             method="get", action="/journal", class_="search", data_get=True)
    svc_chips = [_chip(journal.SERVICE_TITLES[sv], _url(st, svc="" if st["svc"] == sv else sv, kind="", after=""),
                       st["svc"] == sv) for sv in journal.SERVICES]
    kinds = [k for k, v in journal.KINDS.items() if v[0] == st["svc"]] if st["svc"] else         ([st["kind"]] if st["kind"] else [])  # виды — после выбора сервиса: иначе чипов больше, чем текста на странице
    kind_chips = [_chip(journal.KINDS[k][1], _url(st, kind="" if st["kind"] == k else k, after=""), st["kind"] == k)
                  for k in kinds]
    return join(form, t("div", svc_chips, class_="chips filters"), t("div", kind_chips, class_="chips filters"))


def _controls(st: dict[str, Any]) -> Markup:
    rows = t("nav", [t("a", str(n), href=_url(st, n=n, after=""), class_="active" if n == st["n"] else None)
                     for n in journal.ROWS], class_="seg", aria_label="Строк на странице")
    order = t("nav", [t("a", title, href=_url(st, sort=k, after=""), class_="active" if k == st["sort"] else None)
                      for k, title in SORT_TITLES.items()], class_="seg", aria_label="Сортировка")
    return t("div", t("span", "строк", class_="hint"), rows, order, class_="controls")


def _more(st: dict[str, Any], nxt: str | None) -> Markup | None:
    """«Показать ещё»: с JS строки дописываются в таблицу, без JS — переход на следующую страницу выдачи."""
    return t("p", t("a", "показать ещё", href=_url(st, after=nxt), class_="btn small", data_more=True),
             class_="more") if nxt else None


def _ip_cell(ip: str, st: dict[str, Any]) -> Markup:
    return t("a", t("code", ip), href=_url(st, ip=ip, after=""), data_swap=True, title="Что делал этот адрес")


def _ips_table(rows: list[dict[str, Any]], st: dict[str, Any]) -> Markup:
    mx = max((i["n"] for i in rows), default=0)
    body = []
    for i in rows:
        sc = SCOPE_BADGE.get(i["scope"])
        kinds = [_kind_badge(k, n) for k, n in sorted(i["kinds"].items(), key=lambda kv: -kv[1])]
        if i.get("bans"):
            kinds.append(badge(f"бан ×{i['bans']}", "ok"))
        body.append([_ip_cell(i["ip"], st), i["cc"] or "—", badge(*sc) if sc else "", charts.count_label(i["n"]),
                     charts.bar(i["n"], mx), _ports_cell(i["ports"]), t("span", kinds, class_="chips"),
                     ago(i["last"])])
    filtered = bool(st["q"] or st["svc"] or st["kind"])
    return table(["адрес", "страна", "", "попыток", "", "порты", "что делал", "последний раз"], body, num=[3],
                 empty="Ничего не нашлось" if filtered else "Внешних попыток за период не было", stack=True)


def _sources(st: dict[str, Any], res: dict[str, Any], data: dict[str, Any]) -> Markup:
    """Карточка «Источники»: поиск, чипы, страница адресов из keyset-выдачи."""
    bad = res["query"].bad
    notice = t("p", "Не понял: " + ", ".join(bad) + f". Примеры: {SEARCH_HINT}.", class_="hint") if bad else None
    start = t("p", t("a", "← с начала", href=_url(st, after=""), data_swap=True), class_="more") if st["after"] else None
    return card("Источники", _toolbar(st), notice, _controls(st), start,
                t("div", _ips_table(res["rows"], st), _more(st, res["next"]), data_more_box=True),
                help=_help(data))


# ---------- карточка адреса ----------

def _ip_page(app: "App", req: "Request", st: dict[str, Any]) -> "Response":
    back = t("a", "← все адреса", href=_url(st, ip="", after=""), class_="btn small", data_swap=True)
    head = page_head("Атаки", None, join(_selector(st), back))
    d = journal.ip_card(st["ip"], st["period"], st["after"], limit=st["n"] if st["n"] != journal.ROWS[0] else journal.ROWS[1])
    if d is None:
        return app.render(req, "Атаки", [head, card(st["ip"], empty("Адреса нет в журнале (или он старше срока хранения)"))],
                          active="/journal")
    sc = SCOPE_BADGE.get(d["scope"])
    rows = [("Страна", d["cc"] or "не определена"), ("Первый раз", _fmt_ts(d["first"])),
            ("Последний раз", join(_fmt_ts(d["last"]), " · ", ago(d["last"]))),
            ("Всего в журнале", charts.count_label(d["total"])),
            ("За период", charts.count_label(d["n"]) + (f", банов fail2ban: {d['bans']}" if d["bans"] else ""))]
    if sc:
        rows.append(("Метка", badge(*sc)))
    by_service = [t("span", f"{journal.SERVICE_TITLES.get(sv, sv)}: {n}", class_="chip") for sv, n in d["by_service"]]
    by_kind = [_kind_badge(k, n) for k, n in d["by_kind"]]
    by_port = [t("span", f"{p}: {n}", class_="chip") for p, n in d["by_port"]]
    top = card(d["ip"], kv(rows), t("h4", "По сервисам"), t("div", by_service or "—", class_="chips"),
               t("h4", "По видам"), t("div", by_kind or "—", class_="chips"),
               t("h4", "По портам"), t("div", by_port or "—", class_="chips"))
    feed_rows = [[_fmt_bucket(e["ts"], d["res"]), _kind_badge(e["kind"]), e["port"] or "—", e["n"]] for e in d["feed"]]
    feed = card("События", t("div", table(["когда", "что", "порт", "раз"], feed_rows, num=[3],
                                           empty="За период событий нет", stack=True),
                             _more(st, d["next"]), data_more_box=True),
                help=t("p", "Сводка по часам (за последние 8 суток) или по суткам, а не строки журнала."))
    return app.render(req, "Атаки", [head, top, feed, _kind_dialogs()], active="/journal")


def _fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "—"


def _fmt_bucket(ts: int, res: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M" if res < 86400 else "%d.%m")


# ---------- страница ----------

def _build(data: dict[str, Any], app: "App", show_all: bool) -> tuple[list[Any], list[Any]]:
    """Тело страницы без форм и данных сессии — его можно держать в кэше: что выше таблицы источников и что ниже."""
    problems: list[tuple[str, Any]] = [_verdict(data)]
    problems += [("warn", h) for h in app.cached("journal-health", 60, journal.health)]
    run = data["last_run"]
    if run is None:
        problems.append(("warn", "Коллектор журнала ещё не отрабатывал."))
    else:
        problems += [("warn", f"Источник {src}: {err}") for src, err in run["errors"].items()]
        if run["age"] > traffic.STALE_AFTER:
            problems.append(("warn", f"Последний разбор журнала {human_duration(run['age'])} назад — "
                                     "проверьте zoo-collector.timer."))
    problems += app.cached("journal-alerts", 60, journal.alerts)
    t_ = data["totals"]
    top = data["top_ips"][0] if data["top_ips"] else None
    hid = data["hidden"]
    hidden_txt = []
    if not show_all and hid["local"]:
        hidden_txt.append(f"локальных {hid['local']}")
    if not show_all and hid["own"]:
        hidden_txt.append(f"со своих адресов {hid['own']}")
    tiles = t("div",
              _tile("Попыток", charts.count_label(t_["events"]),
                    title="скрыто: " + ", ".join(hidden_txt) if hidden_txt else "извне на сервер"),
              _tile("Адресов", str(t_["ips"]), title="разных источников"),
              _tile("Банов fail2ban", str(t_["bans"]), title="по SSH"),
              _tile("Чаще всего", top["ip"] if top else "—",
                    f"{top['n']}" + (f" · {top['cc']}" if top["cc"] else "") if top else "тихо"),
              class_="tiles")
    above = [alert_list(problems), tiles, _group_cards(data), card("По времени", _timeline(data))]
    below = [t("div", card("Порты", _ports_table(data)), card("Страны", _countries_table(data)), class_="cols")]
    return above, below


def journal_page(app: "App", req: "Request") -> "Response":
    st = _state(req)
    if st["ip"]:
        return _ip_page(app, req, st)
    toggle = t("a", "скрыть локальные" if st["all"] else "показать локальные и свои",
               href=_url(st, all=not st["all"], after=""), class_="btn small", data_swap=True)
    head = page_head("Атаки", None, join(_selector(st), toggle))
    stamp = journal.last_run_ts()  # данные меняются только с разбором коллектора
    key = ("journal", st["period"], st["all"], stamp)
    body = app.cache_get(key, 600)
    if body is None:
        data = journal.report(st["period"], include_local=st["all"])
        if data.get("empty"):
            nodata = empty("Нет данных · сбор каждые 5 мин", t("code", "sudo zoo journal --collect"))
            return app.render(req, "Атаки", [head, card("Атаки", nodata)], active="/journal")
        body = (*_build(data, app, st["all"]), data)
        app.cache_put(key, body, 600)
    above, below, data = body
    skey = ("journal-src", tuple(sorted(st.items())), stamp)
    src = app.cache_get(skey, 120)
    if src is None:
        res = journal.search(st["period"], st["q"], st["svc"], st["kind"], st["sort"], st["after"], st["n"], st["all"])
        src = _sources(st, res, data)
        app.cache_put(skey, src, 120)
    return app.render(req, "Атаки", [head, *above, src, *below, _kind_dialogs()], active="/journal")
