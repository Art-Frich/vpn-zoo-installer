"""Страница «Проверка», блоки истории: «Лучшие протоколы», тренды по протоколам, журнал прогонов.
Данные — SQLite истории проб (zoolib.probe.history); пока её нет, блоки подсказывают, как наполнить.

Строки рейтинга и трендов — по всем протоколам сервера (манифесты), а не только по тем, что есть в
истории; имена — короткие, как на карточках «Обзора»."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode

from .. import manifests
from .. import probe as probe_mod
from ..probe import history, rank
from . import charts
from . import table as tbl
from .html import Markup, badge, card, empty, join, kv, post_button, t, table

if TYPE_CHECKING:
    from .app import App, Request, Response

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
    out = [{"id": m.id, "name": manifests.TITLES.get(m.id, m.short), "enabled": m.enabled} for m in good]
    return sorted(out, key=lambda s: not s["enabled"])


def split_rows(servers: list[Server], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(строки включённых протоколов, остальные). Нет манифестов — все считаются включёнными."""
    if not servers:
        return rows, []
    on = {s["id"] for s in servers if s["enabled"]}
    return [r for r in rows if r["proto"] in on], [r for r in rows if r["proto"] not in on]


def _name(pid: str, names: dict[str, Server], note: Any = None) -> Markup:
    s = names.get(pid)
    return t("span", t("strong", s["name"] if s else pid),
             t("span", note, class_="sub") if note else None)


def _cells(p: dict[str, Any]) -> list[str]:
    return [f"{p['success_pct']:.0f}% ({p['ok']}/{p['n']})", rank.spread_text(p, "latency", ".0f", " мс"),
            rank.spread_text(p, "down", ".1f", " Мбит/с")]


def _block_rows(c: dict[str, Any], servers: list[Server], names: dict[str, Server],
                off: dict[str, dict[str, Any]], anywhere: set[str]) -> tuple[list[list[Any]], list[str | None]]:
    """Строки блока: протоколы с данными (с местами — только в ранжируемом блоке), затем включённые без замеров
    в этом блоке (но с замерами в другом: пустая строка), затем выключенные серым. Протоколы, которых нет ни в одном
    блоке, строками не идут: они одной строкой под блоками (best_card)."""
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
        if s["enabled"] and s["id"] not in have and s["id"] in anywhere:
            add(_name(s["id"], names, "нет замеров"), None)
    for pid in [s["id"] for s in servers if not s["enabled"]] + sorted(set(off) - set(names)):
        if pid in off:
            add(_name(pid, names, "выключен на сервере" if pid in names else "нет на сервере"), off[pid], dim=True)
    return rows, cls


# полная команда замера (подготовка — README «Блокирует ли мой провайдер?»): метка сети и устройство — для рейтинга
PROBE_RUN = ('docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe '
             "--tag mobile-mts --device pixel7")


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
              t("p", "Пробник — Docker на компьютере в сети человека (на телефон не ставится; мобильный интернет — "
                     "через раздачу с телефона на ноутбук). Один раз: репозиторий, ", t("code", "probe-export.json"),
                " с сервера и образ — README, раздел «Блокирует ли мой провайдер?». Замер: ", t("code", PROBE_RUN),
                "; отчёт ", t("code", "probe/probe-report.json"), " вставьте в форму ниже или отправьте командой ",
                t("code", "scripts/history.sh push"), "."))
    if not ranking:
        body: list[Any] = [empty("Нет клиентских проб",
                                 join("Нужен компьютер с Docker в сети человека (с телефона — через раздачу на ноутбук): ",
                                      t("code", PROBE_RUN)))]
    else:
        body = []
        anywhere = {p["proto"] for c in ranking for p in c["protocols"]}
        for c in ranking:
            rows, cls = _block_rows(c, servers, names, off_by.get(c["context"], {}), anywhere)
            sub = ", ".join(x for x in (", ".join(c["isps"]), ", ".join(c["devices"])) if x)
            body.append(t("h3", c["context"], t("span", f" · прогонов: {c['reports']}" + (f" · {sub}" if sub else ""),
                                                 class_="sub")))
            if c["ranked"]:
                body.append(table(["место", "протокол", "оценка", "успех", "задержка", "скорость"], rows,
                                  num=[2, 4, 5], stack=True, row_cls=cls))
            else:
                body += [t("p", badge("мало данных", "warn"), f" {rank.plural_runs(c['reports'])}", class_="quiet"),
                         table(["протокол", "успех", "задержка", "скорость"], rows, num=[2, 3], cls="small", stack=True,
                               row_cls=cls)]
    if ranking:
        if any(not c["ranked"] for c in ranking):
            body.append(t("p", "«Мало данных» — ориентир, не рейтинг: мест нет, пока не набралось 3 прогона.",
                          class_="quiet"))
        missing = [s["id"] for s in servers if s["enabled"] and s["id"] not in anywhere]
        if missing:
            body.append(t("p", NO_RUNS.capitalize() + ": " + ", ".join(names[m]["name"] for m in missing) + " — ",
                          t("a", "запустите пробу", href="#" + HOW_ANCHOR), class_="quiet"))
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
            continue   # без замеров — одной строкой в «Лучших протоколах», здесь пустые строки ни к чему
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


# ---------- история прогонов: таблица, карточка прогона, выгрузка, удаление ----------

DEFAULT_PERIOD = "30d"
HISTORY_ANCHOR = "history"
BACK_RE = re.compile(r"/probe(?:\?[A-Za-z0-9_%&=.+~:|-]{0,400})?")
RUN_RE = re.compile(r"\d{1,9}")
WORKING_SQL = "(SELECT COUNT(*) FROM results x WHERE x.report_id = r.id AND x.verdict IN ('OK','SLOW'))"
RUN_EXTRA = ("r.asn AS asn", "(SELECT COUNT(*) FROM results x WHERE x.report_id = r.id) AS total",
             "(SELECT COUNT(*) FROM results x WHERE x.report_id = r.id "
             "AND x.verdict IN ('SKIPPED','CLIENT_ERROR')) AS untested")


def _who(v: str) -> str:
    return "сервер" if v == "local" else "клиент"


def _iso(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


def _isp_text(r: dict[str, Any]) -> str:
    return ((f"AS{r['asn']} " if r.get("asn") else "") + (r.get("isp") or "")).strip()


def _works(r: dict[str, Any]) -> Markup:
    tested = r["total"] - r["untested"]
    return badge(f"{r['working']}/{tested}", "ok" if tested and r["working"] == tested else "warn")


def _runs_cols(run_url: Callable[[dict[str, Any]], str]) -> list[tbl.Col]:
    return [
        tbl.Col("ts", "когда", cell=lambda r: tbl.link(_ts(r["ts"]), run_url(r)), value=lambda r: _iso(r["ts"]),
                sql="r.ts", num=True, left=True, sort=True),
        tbl.Col("mode", "кто", cell=lambda r: _who(r["mode"]), value=lambda r: _who(r["mode"]), sql="r.mode",
                sort=True, chip=True, secondary=True, label=_who),
        tbl.Col("tag", "метка", sql="COALESCE(r.tag, '')", sort=True, search=True, chip=True),
        tbl.Col("isp", "провайдер", cell=lambda r: t("span", _isp_text(r) or "—", class_="small"), value=_isp_text,
                sql="COALESCE(r.isp, '')", sort=True, search=True, secondary=True),
        tbl.Col("country", "страна", sql="COALESCE(r.country, '')", sort=True, search=True, chip=True, secondary=True),
        tbl.Col("device", "устройство", sql="COALESCE(r.device, '')", sort=True, search=True, chip=True,
                secondary=True),
        tbl.Col("net", "сеть", cell=lambda r: NET_TEXT.get(r["net"], r["net"]) if r["net"] else "—",
                value=lambda r: NET_TEXT.get(r["net"], r["net"]), sql="COALESCE(r.net, '')", sort=True, chip=True,
                secondary=True, label=lambda v: NET_TEXT.get(v, v)),
        tbl.Col("working", "работает", cell=_works, value=lambda r: f"{r['working']}/{r['total'] - r['untested']}",
                sql=WORKING_SQL, num=True, sort=True, first_desc=True, hint="протоколов с OK или SLOW из проверенных"),
    ]


def _runs_spec(period: str, run_url: Callable[[dict[str, Any]], str] | None = None) -> tbl.Spec:
    spec = tbl.Spec(path="/probe", cols=_runs_cols(run_url or (lambda r: "")), sort="ts", desc=True, prefix="h_",
                    id_sql="r.id", empty="прогонов ещё нет", placeholder="метка, провайдер, страна…",
                    keep=[] if period == DEFAULT_PERIOD else [("rp", period)], name="probe-history")
    spec.href = run_url
    return spec


def _runs_where(since: int | None) -> tuple[str, list[Any]]:
    return ("r.ts >= ?", [since]) if since is not None else ("1", [])


def _db_stamp() -> tuple[Any, ...]:
    out: list[Any] = [str(history.db_path())]
    for p in (history.db_path(), history.db_path().with_name(history.DB_NAME + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _options(app: "App", con: Any, spec: tbl.Spec, period: str, where: str, args: list[Any]) -> tbl.Options:
    """Значения чипов — полный проход по таблице отчётов (строки толстые: в них сырой JSON), поэтому кэш по состоянию файла."""
    if not con:
        return {}
    key = ("runs-options", period, _db_stamp())
    got = app.cache_get(key, 600)
    if got is None:
        got = tbl.sql_options(con, spec, "reports r", where, args)
        app.cache_put(key, got, 600)
    return got


def runs_card(app: "App", req: "Request", con: Any, since: int | None, period: str) -> Markup:
    where, args = _runs_where(since)
    opts = _options(app, con, _runs_spec(period), period, where, args)
    st = tbl.parse(_runs_spec(period), req.query, opts)
    here = tbl.href(_runs_spec(period), st)  # к этому состоянию таблицы возвращает «← к истории»

    def run_url(r: dict[str, Any]) -> str:
        return "/probe?" + urlencode([("run", r["id"]), ("back", here)])

    spec = _runs_spec(period, run_url)
    page = tbl.sql_page(con, spec, st, "reports r", where, args, RUN_EXTRA, opts) if con else tbl.Page([], 0, 0, None)
    return card("История прогонов", tbl.render(spec, st, page), id_=HISTORY_ANCHOR,
                help=t("p", "Прогоны сервера и присланные клиентские отчёты хранятся в ", t("code", str(history.db_path())),
                       "; выгрузка в репозиторий — ", t("code", "zoo history export"), " (справочник, «История проб и рейтинг»). "
                       "Нажмите на строку: все протоколы прогона, там же удаление. Выгрузка — всё по текущим "
                       f"фильтрам (до {tbl.EXPORT_MAX} строк)."))


def export_runs(app: "App", req: "Request") -> "Response | None":
    """CSV/JSON истории прогонов по текущим фильтрам; None — выгрузку не просили."""
    if req.method != "GET":
        return None
    period = _period(req)
    spec = _runs_spec(period)
    ex = tbl.export_request(spec, req.query)
    if ex is None:
        return None
    con = None
    try:
        con = history.connect(create=False)
        where, args = _runs_where(history.since_ts(period))
        opts = _options(app, con, spec, period, where, args)
        st = replace(tbl.parse(spec, req.query, opts), after="", n=tbl.EXPORT_MAX)
        rows = tbl.sql_page(con, spec, st, "reports r", where, args, RUN_EXTRA, opts).rows if con else []
    finally:
        if con:
            con.close()
    return tbl.export_response(spec.cols, rows, ex[0], ex[1], "probe-history")


def run_param(req: "Request") -> int | None:
    v = req.query.get("run", "")
    return int(v) if req.method == "GET" and RUN_RE.fullmatch(v) else None


def safe_back(value: str | None) -> str:
    """Возврат к таблице: только адрес /probe с обычными символами запроса."""
    return value if value and BACK_RE.fullmatch(value) else "/probe"


def _n(v: float | None, fmt: str, unit: str = "") -> str:
    return "—" if v is None else format(v, fmt) + unit


RESULT_COLS = [
    tbl.Col("ts", "когда", value=lambda r: _iso(r["ts"])),
    tbl.Col("proto", "протокол"),
    tbl.Col("verdict", "вердикт"),
    tbl.Col("latency_ms", "задержка p50, мс", num=True),
    tbl.Col("p90_ms", "задержка p90, мс", num=True),
    tbl.Col("jitter_ms", "джиттер, мс", num=True),
    tbl.Col("loss_pct", "потери, %", num=True),
    tbl.Col("down_mbps", "скорость вниз, Мбит/с", num=True),
    tbl.Col("up_mbps", "скорость вверх, Мбит/с", num=True),
    tbl.Col("egress_ip", "IP выхода", private=True),
]


def run_page(app: "App", req: "Request", rid: int) -> "Response":
    from .views import fmt_time, page_head
    con = None
    try:
        con = history.connect(create=False)
        d = history.report_detail(con, rid) if con else None
    except sqlite3.Error as e:
        return app.error(req, 500, "История не читается", str(e))
    finally:
        if con:
            con.close()
    back = safe_back(req.query.get("back"))
    if d is None:
        return app.error(req, 404, "Нет такого прогона", "Его удалили или он старше срока хранения.")
    ex = req.query.get("export", "")
    if ex in tbl.FORMATS:
        rows = [dict(r, ts=d["ts"]) for r in d["results"]]
        return tbl.export_response(RESULT_COLS, rows, ex, req.query.get("priv") == "1", f"probe-run-{rid}")
    names = {s["id"]: s for s in server_protos()}
    body = []
    for r in d["results"]:
        s = names.get(r["proto"])
        body.append([t("strong", s["name"] if s else r["proto"]),
                     _verdict_badge(r["verdict"]), _n(r["latency_ms"], ".0f", " мс"), _n(r["p90_ms"], ".0f", " мс"),
                     _n(r["jitter_ms"], ".1f", " мс"), _n(r["loss_pct"], ".0f", " %"), _n(r["down_mbps"], ".1f"),
                     _n(r["up_mbps"], ".1f"), t("code", r["egress_ip"]) if r["egress_ip"] else "—"])
    results = table(["протокол", "вердикт", ("p50", "медиана времени до первого байта"), ("p90", "90-й перцентиль"),
                     ("джиттер", "RFC 3550"), "потери", ("↓ Мбит/с", "скорость загрузки"),
                     ("↑ Мбит/с", "скорость отдачи"), ("IP выхода", "с какого адреса туннель вышел в интернет")],
                    body, num=[2, 3, 4, 5, 6, 7], stack=True, empty="в прогоне нет протоколов")
    info = [("когда", fmt_time(d["ts"])), ("кто", _who(d["mode"]))]
    info += [(title, d[k]) for k, title in (("tag", "метка"), ("device", "устройство"), ("country", "страна")) if d[k]]
    if d["isp"] or d["asn"]:
        info.append(("провайдер", _isp_text(d)))
    if d["net"]:
        info.append(("сеть", NET_TEXT.get(d["net"], d["net"])))
    csrf = req.session.csrf if req.session else ""
    here = f"/probe?run={rid}"
    actions = t("div",
                t("a", "← к истории", href=back, class_="btn small", data_swap=True),
                t("a", "JSON", href=f"{here}&export=json", class_="btn small", title="Без IP выхода"),
                t("a", "CSV", href=f"{here}&export=csv", class_="btn small", title="Без IP выхода"),
                t("a", "JSON с IP", href=f"{here}&export=json&priv=1", class_="btn small",
                  title="Вместе с IP выхода: не отправляйте выгрузку наружу"),
                post_button(f"/probe/history/{rid}/delete", "Удалить прогон", csrf, "btn small danger", {"back": back},
                            confirm=f"Удалить прогон от {fmt_time(d['ts'])} из истории? Это насовсем."),
                class_="actions")
    return app.render(req, "Прогон", [page_head("Прогон", fmt_time(d["ts"]), actions), card("Условия", kv(info)),
                                      card("Протоколы", results)], active="/probe")


def run_delete(app: "App", req: "Request", rid: str) -> "Response":
    from .app import redirect
    con = None
    try:
        con = history.connect(create=False)
        gone = history.delete_report(con, int(rid)) if con else False
    except sqlite3.Error as e:
        req.session.flash("bad", f"Не удалось удалить: {e}")
        return redirect(f"/probe?run={rid}")
    finally:
        if con:
            con.close()
    req.session.flash("ok" if gone else "warn", "Прогон удалён из истории." if gone else "Такого прогона уже нет.")
    return redirect(safe_back(req.form.get("back")))


SECTION_TITLE = "Как работает у пользователей"
SECTION_NOTE = "По вашим замерам с устройств; мобильная сеть и Wi-Fi — отдельно."


def _section_head() -> list[Markup]:
    return [t("h2", SECTION_TITLE), t("p", SECTION_NOTE, class_="quiet")]


def cards(app: "App", req: "Request") -> list[Markup]:
    period = _period(req)
    con = None
    try:
        con = history.connect(create=False)
        since = history.since_ts(period)
        rows = history.fetch_results(con, since, "remote") if con else []
        servers = server_protos()
        rows_on, rows_off = split_rows(servers, rows)
        return [*_section_head(), best_card(rank.rank(rows_on), period, servers, rank.rank(rows_off)),
                trends_card(rows, servers), runs_card(app, req, con, since, period)]
    except sqlite3.Error as e:
        # битая или занятая БД истории не должна ронять всю страницу «Проверка»
        return [card("История проб", t("p", f"База истории не читается ({history.db_path()}): {e}", class_="hint"))]
    finally:
        if con:
            con.close()
