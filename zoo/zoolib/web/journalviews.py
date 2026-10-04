"""Страница «Атаки»: кто нас щупал (zoolib.journal). Только чтение базы, ничего не меняет."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import journal, traffic
from ..output import human_duration
from . import charts
from .html import Markup, badge, card, join, t, table
from .views import _tile, alert_list, ago, fmt_time, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

SCOPE_BADGE = {"own": ("свой", "info"), "local": ("локальный", "muted")}
KIND_BADGE = {"port-scan": "warn", "ssh-auth": "bad", "ssh-scan": "warn", "ssh-limit": "warn", "ssh-ban": "ok",
              "hy2-auth": "bad", "reality-probe": "bad", "panel-login": "muted", "web-login": "muted"}


def _period(req: "Request") -> str:
    try:
        return journal.resolve_period(req.query.get("period", "24h"))
    except ValueError:
        return "24h"


def _selector(period: str, show_all: bool) -> Markup:
    extra = "&all=1" if show_all else ""
    return t("nav", [t("a", p, href=f"/journal?period={p}{extra}", class_="active" if p == period else None)
                     for p in journal.PERIODS], class_="seg", aria_label="Период")


def _timeline(data: dict[str, Any]) -> Markup:
    tl = data["timeline"]
    if not tl["series"]:
        return t("p", "За период попыток не было.", class_="muted")
    return join(charts.columns(tl["buckets"], tl["step"], tl["series"], "Попытки по времени", 1100, 260,
                               fmt=charts.count_label, nice=charts.nice_count, empty_tip="попыток не было"),
                charts.legend(tl["series"], fmt=charts.count_label))


def _group_cards(data: dict[str, Any]) -> Markup:
    cards = []
    for g in data["groups"]:
        chips = [t("span", f"{k['title']}: {k['n']}", class_="chip") for k in g["kinds"]]
        cards.append(card(g["title"],
                          t("div", t("div", "попыток", class_="muted small"),
                            t("div", charts.count_label(g["n"]), class_="num-big"),
                            t("div", f"адресов: {g['ips']}", class_="muted small")),
                          t("div", chips, class_="chips") if chips else None, cls="proto"))
    return t("div", cards, class_="grid")


def _ips_table(data: dict[str, Any]) -> Markup:
    rows = []
    mx = max((i["n"] for i in data["top_ips"]), default=0)
    for i in data["top_ips"]:
        sc = SCOPE_BADGE.get(i["scope"])
        kinds = [badge(journal.KINDS[k][1] + f" {n}", KIND_BADGE.get(k, "muted")) for k, n in
                 sorted(i["kinds"].items(), key=lambda kv: -kv[1])]
        if i.get("bans"):
            kinds.append(badge(f"бан ×{i['bans']}", "ok"))
        rows.append([t("code", i["ip"]), i["cc"] or "—", badge(*sc) if sc else "", charts.count_label(i["n"]),
                     charts.bar(i["n"], mx), ", ".join(map(str, i["ports"])) or "—", t("span", kinds, class_="chips"),
                     ago(i["last"])])
    return table(["адрес", "страна", "", "попыток", "", "порты", "что делал", "последний раз"], rows,
                 num=[3], empty="Внешних попыток за период не было")


def _ports_table(data: dict[str, Any]) -> Markup:
    mx = max((p["n"] for p in data["top_ports"]), default=0)
    return table(["порт", "попыток", "", "адресов"],
                 [[t("code", p["port"]), charts.count_label(p["n"]), charts.bar(p["n"], mx, "s2"), p["ips"]]
                  for p in data["top_ports"]], num=[1, 3], empty="нет данных")


def _countries_table(data: dict[str, Any]) -> Markup:
    mx = max((c["n"] for c in data["countries"]), default=0)
    return table(["страна", "попыток", "", "адресов"],
                 [[("не определена" if c["cc"] == "?" else c["cc"]), charts.count_label(c["n"]),
                   charts.bar(c["n"], mx, "s3"), c["ips"]] for c in data["countries"]],
                 num=[1, 3], empty="нет данных")


def journal_page(app: "App", req: "Request") -> "Response":
    period = _period(req)
    show_all = req.query.get("all") == "1"
    data = journal.report(period, include_local=show_all)
    sub = f"{traffic.PERIOD_TITLES[period]}, с {fmt_time(data['since'])}"
    toggle = t("a", "скрыть локальные" if show_all else "показать локальные и свои",
               href=f"/journal?period={period}" + ("" if show_all else "&all=1"), class_="btn small")
    head = page_head("Атаки", sub, join(_selector(period, show_all), toggle))
    if data.get("empty"):
        body = [head, card("Нет данных", t("p", "Журнал атак ещё не собирался: его разбирает коллектор вместе с "
                                                 "трафиком раз в 5 минут (zoo-collector.timer). Вручную: ",
                                           t("code", "sudo zoo journal --collect"), ".", class_="muted"))]
        return app.render(req, "Атаки", body, active="/journal")
    problems: list[tuple[str, Any]] = [("warn", h) for h in app.cached("journal-health", 60, journal.health)]
    run = data["last_run"]
    if run is None:
        problems.append(("warn", "Коллектор журнала ещё не отрабатывал."))
    else:
        problems += [("warn", f"Источник {src}: {err}") for src, err in run["errors"].items()]
        if run["age"] > traffic.STALE_AFTER:
            problems.append(("warn", f"Последний разбор журнала {human_duration(run['age'])} назад — "
                                     "проверьте zoo-collector.timer."))
    problems += journal.alerts()
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
                    "скрыто: " + ", ".join(hidden_txt) if hidden_txt else "извне на сервер"),
              _tile("Адресов", str(t_["ips"]), "разных источников"),
              _tile("Банов fail2ban", str(t_["bans"]), "по SSH"),
              _tile("Чаще всего", top["ip"] if top else "—",
                    f"{top['n']} попыток" + (f", {top['cc']}" if top and top["cc"] else "") if top else "тихо"),
              class_="tiles")
    blind = card("Чего мы не видим",
                 t("ul", [t("li", t("strong", b["what"] + ": "), b["why"]) for b in data["blind"]]),
                 t("p", "ufw пишет блокировки не чаще 3 в минуту на весь сервер, поэтому число попыток по портам — "
                        "выборка, а не итог; адреса при этом видны. Журнал не хранит трафик и назначения "
                        "пользователей VPN: только адрес источника, порт, вид попытки и счётчик.", class_="hint"))
    own = card("Свои адреса",
               t("p", "С этих адресов заходили по ключу SSH, их попытки не считаются атакой. Свои адреса можно "
                      "добавить вручную: ", t("code", str(journal.ignore_file())), " (адрес или сеть в строке).",
                 class_="hint"),
               table(["адрес", "последний вход"], [[t("code", o["ip"]), ago(o["ts"])] for o in data["own_ips"]],
                     empty="входов по ключу не видно"))
    body = [head, alert_list(problems) if problems else None, tiles,
            t("h2", "Что делали"), _group_cards(data),
            card("По времени", _timeline(data)),
            t("h2", "Кто"), card("Источники", _ips_table(data)),
            t("div", card("Порты", _ports_table(data)), card("Страны", _countries_table(data)), class_="cols"),
            t("div", blind, own, class_="cols")]
    return app.render(req, "Атаки", body, active="/journal")
