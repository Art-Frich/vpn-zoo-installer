"""Страница «Проверка», блоки истории: «Лучшие протоколы», тренды по протоколам, журнал прогонов.
Данные — SQLite истории проб (zoolib.probe.history); пока её нет, блоки подсказывают, как наполнить."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .. import probe as probe_mod
from ..probe import history, rank
from . import charts
from .html import Markup, badge, card, t, table

if TYPE_CHECKING:
    from .app import Request

PERIODS = ("7d", "30d", "90d", "all")
NET_TEXT = {"wifi": "Wi-Fi", "ethernet": "кабель", "cellular": "мобильная"}
CONF_TEXT = {"low": "мало данных", "medium": "средняя", "high": "высокая"}
CONF_KIND = {"low": "warn", "medium": "muted", "high": "ok"}


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


def _num(v: float | None, fmt: str, unit: str = "") -> str:
    return "—" if v is None else format(v, fmt) + unit


def best_card(ranking: list[dict[str, Any]], period: str) -> Markup:
    if not ranking:
        body: list[Any] = [t("p", "Пока нет клиентских проб в истории. Запустите пробник на устройстве, из той сети, "
                                  "которая важна, с метками: ", t("code", "zoo-probe --tag mobile-mts --device pixel7"),
                             "; отчёт ", t("code", "probe/probe-report.json"), " вставьте в форму ниже или "
                             "отправьте командой ", t("code", "scripts/history.sh push"), ".", class_="muted")]
    else:
        body = []
        for c in ranking:
            rows = []
            for i, p in enumerate(c["protocols"], 1):
                place = i if p in c["top"] else "—"
                rows.append([place, t("strong", p["proto"]), f"{p['score']:.0f}",
                             f"{p['success_pct']:.0f}% ({p['ok']}/{p['n']})",
                             _num(p["latency_ms"], ".0f", " мс"), _num(p["down_mbps"], ".1f", " Мбит/с"),
                             badge(CONF_TEXT[p["confidence"]], CONF_KIND[p["confidence"]])])
            sub = ", ".join(x for x in (", ".join(c["isps"]), ", ".join(c["devices"])) if x)
            body += [t("h3", c["context"], t("span", f" · прогонов: {c['reports']}" + (f" · {sub}" if sub else ""),
                                              class_="sub")),
                     table(["место", "протокол", "оценка", "успех", "задержка", "скорость", "данных"], rows,
                           num=[2, 4, 5])]
    return card("Лучшие протоколы",
                t("p", "Из накопленных клиентских проб, отдельно по условиям (метка пробы, без неё — провайдер): "
                       "мобильная сеть и домашний Wi-Fi не смешиваются. Оценка = 100 × успех × (0,6 × скорость к "
                       "лучшей + 0,4 × лучшая задержка к своей) — сравнима только внутри блока.", class_="hint"),
                *body, extra=_period_nav(period))


def trends_card(rows: list[dict[str, Any]]) -> Markup:
    per: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["verdict"] not in probe_mod.verdicts.NOT_TESTED:
            per.setdefault(r["proto"], []).append(r)
    out = []
    for proto in sorted(per):
        rs = per[proto]
        good = [r for r in rs if r["verdict"] in probe_mod.verdicts.WORKING]
        lat = sorted(r["latency_ms"] for r in good if r["latency_ms"])
        speeds = sorted(r["down_mbps"] for r in good if r["down_mbps"])
        spark = charts.sparkline([round(r["down_mbps"] or 0, 1) if r["verdict"] in probe_mod.verdicts.WORKING else 0
                                  for r in rs[-30:]])
        out.append([t("strong", proto), len(rs), f"{100 * len(good) / len(rs):.0f}%",
                    _num(lat[len(lat) // 2] if lat else None, ".0f", " мс"),
                    _num(speeds[len(speeds) // 2] if speeds else None, ".1f", " Мбит/с"), spark,
                    t("span", [_verdict_badge(r["verdict"]) for r in rs[-6:]], class_="small")])
    return card("Тренды по протоколам",
                t("p", "Все клиентские прогоны за период: медианы по удачным, столбики — скорость в каждом прогоне "
                       "(слева старые), справа — последние вердикты.", class_="hint"),
                table(["протокол", "прогонов", "успех", "задержка", "скорость", "динамика скорости", "последние"],
                      out, num=[1, 3, 4], empty="нет данных"))


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
                t("p", f"Записано прогонов: {total}. Прогоны сервера и присланные клиентские отчёты хранятся в ",
                  t("code", str(history.db_path())), "; выгрузка в репозиторий — ", t("code", "zoo history export"),
                  " (README, «История проб»).", class_="hint"),
                table(["когда", "кто", "метка", "провайдер", "страна", "устройство", "сеть", "работает"], rows,
                      empty="прогонов ещё нет"))


def cards(req: "Request") -> list[Markup]:
    period = _period(req)
    con = None
    try:
        con = history.connect(create=False)
        since = history.since_ts(period)
        rows = history.fetch_results(con, since, "remote") if con else []
        ranking = rank.rank(rows)
        return [best_card(ranking, period), trends_card(rows), journal_card(con, since)]
    except sqlite3.Error as e:
        # битая или занятая БД истории не должна ронять всю страницу «Проверка»
        return [card("История проб", t("p", f"База истории не читается ({history.db_path()}): {e}", class_="hint"))]
    finally:
        if con:
            con.close()
