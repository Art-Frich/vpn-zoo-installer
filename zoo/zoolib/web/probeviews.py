"""Страница «Проверка», блоки истории: «Лучшие протоколы», тренды по протоколам, журнал прогонов.
Данные — SQLite истории проб (zoolib.probe.history); пока её нет, блоки подсказывают, как наполнить.

Строки рейтинга и трендов — по всем протоколам сервера (манифесты), а не только по тем, что есть в
истории; имена — короткие, как на карточках «Обзора»."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .. import manifests
from .. import probe as probe_mod
from ..probe import history, rank
from . import charts
from .html import Markup, badge, card, empty, t, table

if TYPE_CHECKING:
    from .app import Request

PERIODS = ("7d", "30d", "90d", "all")
NET_TEXT = {"wifi": "Wi-Fi", "ethernet": "кабель", "cellular": "мобильная"}
NO_RUNS = "нет замеров с устройств"
HOW_ANCHOR = "client-probe"
Server = dict[str, Any]


def _period(req: "Request") -> str:
    p = req.query.get("rp", "30d")
    return p if p in PERIODS else "30d"


def _ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def _period_nav(current: str) -> Markup:
    links = [t("a", "всё время" if p == "all" else p, href=f"/probe?rp={p}", class_="active" if p == current else None)
             for p in PERIODS]
    return t("nav", links, class_="seg", aria_label="Период истории")


def _verdict_badge(v: str) -> Markup:
    if v == probe_mod.verdicts.OK:
        return badge("OK", "ok")
    if v == probe_mod.verdicts.SLOW:
        return badge("SLOW", "warn")
    if v in probe_mod.verdicts.NOT_TESTED:
        return badge(v, "muted")
    return badge(v, "bad")


def server_protos() -> list[Server]:
    """Протоколы сервера: {id, name (короткое), full, enabled}; включённые первыми. Манифестов нет — пусто."""
    good, _ = manifests.load_all()
    out = [{"id": m.id, "name": m.short, "full": m.name, "enabled": m.enabled} for m in good]
    return sorted(out, key=lambda s: not s["enabled"])


def split_rows(servers: list[Server], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(строки включённых протоколов, остальные). Нет манифестов — все считаются включёнными."""
    if not servers:
        return rows, []
    on = {s["id"] for s in servers if s["enabled"]}
    return [r for r in rows if r["proto"] in on], [r for r in rows if r["proto"] not in on]


def _name(pid: str, names: dict[str, Server], note: Any = None) -> Markup:
    s = names.get(pid)
    return t("span", t("strong", s["name"] if s else pid, title=s["full"] if s else None),
             t("span", note, class_="sub") if note else None)


def _no_runs_note() -> Markup:
    return t("span", NO_RUNS + " — ", t("a", "запустите пробу", href="#" + HOW_ANCHOR))


def _cells(p: dict[str, Any]) -> list[str]:
    return [f"{p['success_pct']:.0f}% ({p['ok']}/{p['n']})", rank.spread_text(p, "latency", ".0f", " мс"),
            rank.spread_text(p, "down", ".1f", " Мбит/с")]


def _block_rows(c: dict[str, Any], servers: list[Server], names: dict[str, Server],
                off: dict[str, dict[str, Any]]) -> tuple[list[list[Any]], list[str | None]]:
    """Строки блока: протоколы с данными (с местами — только в ранжируемом блоке), затем включённые
    без замеров, затем выключенные серым."""
    ranked = c["ranked"]
    have = {p["proto"] for p in c["protocols"]}
    rows: list[list[Any]] = []
    cls: list[str | None] = []

    def add(name: Markup, p: dict[str, Any] | None, place: Any = "", score: str = "", dim: bool = False) -> None:
        data = _cells(p) if p else ["", "", ""]
        rows.append([place, name, score, *data] if ranked else [name, *data])
        cls.append("dim" if dim else None)

    for p in c["protocols"]:
        note = f" · {rank.plural_runs(p['n'])}" if ranked and p["low_confidence"] else None
        place = c["top"].index(p) + 1 if p in c["top"] else ""
        add(_name(p["proto"], names, note), p, place, "" if p["low_confidence"] else f"{p['score']:.0f}")
    for s in servers:
        if s["enabled"] and s["id"] not in have:
            add(_name(s["id"], names, _no_runs_note()), None)
    for pid in [s["id"] for s in servers if not s["enabled"]] + sorted(set(off) - set(names)):
        if pid in off:
            add(_name(pid, names, "выключен на сервере" if pid in names else "нет на сервере"), off[pid], dim=True)
    return rows, cls


def best_card(ranking: list[dict[str, Any]], period: str, servers: list[Server] | None = None,
              off_ranking: list[dict[str, Any]] | None = None) -> Markup:
    servers = servers or []
    names = {s["id"]: s for s in servers}
    off_by = {c["context"]: {p["proto"]: p for p in c["protocols"]} for c in off_ranking or []}
    help_ = t("div",
              t("p", "Из накопленных клиентских проб, отдельно по условиям (метка пробы, без неё — провайдер): "
                     "мобильная сеть и домашний Wi-Fi не смешиваются."),
              t("p", "Оценка = 100 × успех × (0,6 × скорость к лучшей + 0,4 × лучшая задержка к своей); "
                     "сравнима только внутри блока. Задержка и скорость — медианы по удачным прогонам, в скобках "
                     "разброс: от 25 % до 75 % замеров (от 3 замеров). Места — от 3 прогонов в блоке и от 3 замеров "
                     "у протокола, по оценке: один замер — шум, а не рейтинг."),
              t("p", "Пробы на устройство: ", t("code", "zoo-probe --tag mobile-mts --device pixel7"),
                "; отчёт ", t("code", "probe/probe-report.json"), " вставьте в форму ниже или отправьте командой ",
                t("code", "scripts/history.sh push"), "."))
    if not ranking:
        body: list[Any] = [empty("Нет клиентских проб", t("code", "zoo-probe --tag mobile-mts --device pixel7"))]
    else:
        body = []
        for c in ranking:
            rows, cls = _block_rows(c, servers, names, off_by.get(c["context"], {}))
            sub = ", ".join(x for x in (", ".join(c["isps"]), ", ".join(c["devices"])) if x)
            body.append(t("h3", c["context"], t("span", f" · прогонов: {c['reports']}" + (f" · {sub}" if sub else ""),
                                                 class_="sub")))
            if c["ranked"]:
                body.append(table(["место", "протокол", "оценка", "успех", "задержка", "скорость"], rows,
                                  num=[2, 4, 5], stack=True, row_cls=cls))
            else:
                body += [t("p", badge("мало данных", "warn"), f" {rank.plural_runs(c['reports'])} — ориентир, не рейтинг",
                           class_="quiet"),
                         table(["протокол", "успех", "задержка", "скорость"], rows, num=[2, 3], cls="small", stack=True,
                               row_cls=cls)]
    return card("Лучшие протоколы", *body, extra=_period_nav(period), help=help_)


def trends_card(rows: list[dict[str, Any]], servers: list[Server] | None = None) -> Markup:
    servers = servers or []
    names = {s["id"]: s for s in servers}
    per: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["verdict"] not in probe_mod.verdicts.NOT_TESTED:
            per.setdefault(r["proto"], []).append(r)
    on = [s["id"] for s in servers if s["enabled"]]
    off = [s["id"] for s in servers if not s["enabled"]]
    # нет манифестов — берём всё из истории; протокол истории, которого нет на сервере, — серым в конце
    order = (on or sorted(per)) + [p for p in off + sorted(set(per) - set(names)) if p in per]
    out: list[list[Any]] = []
    cls: list[str | None] = []
    for proto in order:
        rs = per.get(proto)
        dim = bool(on) and proto not in on
        if not rs:
            out.append([_name(proto, names, _no_runs_note()), "", "", "", "", "", ""])
            cls.append(None)
            continue
        good = [r for r in rs if r["verdict"] in probe_mod.verdicts.WORKING]
        stat = rank.rank_context(rs)[0]
        spark = charts.sparkline([round(r["down_mbps"] or 0, 1) if r["verdict"] in probe_mod.verdicts.WORKING else 0
                                  for r in rs[-30:]])
        note = ("выключен на сервере" if proto in names else "нет на сервере") if dim else None
        out.append([_name(proto, names, note), len(rs), f"{100 * len(good) / len(rs):.0f}%",
                    rank.spread_text(stat, "latency", ".0f", " мс"), rank.spread_text(stat, "down", ".1f", " Мбит/с"),
                    spark, t("span", [_verdict_badge(r["verdict"]) for r in rs[-6:]], class_="small")])
        cls.append("dim" if dim else None)
    return card("Тренды по протоколам",
                table(["протокол", "прогонов", "успех", "задержка", "скорость", "динамика скорости", "последние"],
                      out, num=[1, 3, 4], empty="нет данных", stack=True, row_cls=cls),
                help="Все клиентские прогоны за период: медианы по удачным (в скобках разброс 25–75 %, от 3 "
                     "замеров), столбики — скорость в каждом прогоне (слева старые), справа — последние вердикты.")


def journal_card(con: Any, since: int | None) -> Markup:
    reports = history.list_reports(con, 15, since) if con else []
    rows = []
    for r in reports:
        net = NET_TEXT.get(r["net"], r["net"]) if r["net"] else None
        tested = r["total"] - r["untested"]
        isp = (f"AS{r['asn']} " if r["asn"] else "") + (r["isp"] or "")
        rows.append([_ts(r["ts"]), "сервер" if r["mode"] == "local" else "клиент", r["tag"] or "—",
                     t("span", isp.strip() or "—", class_="small"), r["country"] or "—",
                     r["device"] or "—", net or "—",
                     badge(f"{r['working']}/{tested}", "ok" if tested and r["working"] == tested else "warn")])
    total = history.counts(con)["reports"] if con else 0
    return card("История прогонов",
                table(["когда", "кто", "метка", "провайдер", "страна", "устройство", "сеть", "работает"], rows,
                      empty="прогонов ещё нет", stack=True),
                extra=t("span", str(total), class_="chip", title="записано прогонов"),
                help=t("p", "Прогоны сервера и присланные клиентские отчёты хранятся в ", t("code", str(history.db_path())),
                       "; выгрузка в репозиторий — ", t("code", "zoo history export"), " (README, «История проб»)."))


SECTION_TITLE = "Как работает у пользователей"
SECTION_NOTE = "По вашим замерам с устройств; мобильная сеть и Wi-Fi — отдельно."


def _section_head() -> list[Markup]:
    return [t("h2", SECTION_TITLE), t("p", SECTION_NOTE, class_="quiet")]


def cards(req: "Request") -> list[Markup]:
    period = _period(req)
    con = None
    try:
        con = history.connect(create=False)
        since = history.since_ts(period)
        rows = history.fetch_results(con, since, "remote") if con else []
        servers = server_protos()
        rows_on, rows_off = split_rows(servers, rows)
        return [*_section_head(), best_card(rank.rank(rows_on), period, servers, rank.rank(rows_off)),
                trends_card(rows, servers), journal_card(con, since)]
    except sqlite3.Error as e:
        # битая или занятая БД истории не должна ронять всю страницу «Проверка»
        return [card("История проб", t("p", f"База истории не читается ({history.db_path()}): {e}", class_="hint"))]
    finally:
        if con:
            con.close()
