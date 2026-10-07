"""Страницы админки: обзор, трафик, проверка, логи, настройки, задачи.
Пользователи — в userviews.py. Данные — те же функции zoolib, что у CLI.

Правило текста: на странице видна одна строка, пояснение — в «?» (card(help=)) или в title=."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import probe as probe_mod
from .. import journal, manifests, paths, status, storage, system, traffic, upgrade
from ..config import config_set
from ..output import human_bytes, human_duration
from . import charts, logs, probeviews, protoviews
from . import table as tbl
from .html import Markup, badge, card, csrf_input, empty, join, kv, post_button, t, table
from .jobs import Job, outside_sandbox, zoo_argv

if TYPE_CHECKING:
    from .app import App, Request, Response

REBOOT_FLAG = Path("/var/run/reboot-required")
GEO_UNIT = "vpn-zoo-geo-update.service"
COLLECT_ACTION = {"action": "collect"}
NL = chr(10)


def users_line(p: dict[str, Any]) -> Markup:
    """«пользователей: 5 (+1 откл.)»; подсказка — кто с протоколом, кто отключён, у кого его нет и почему."""
    n = p.get("users")
    if n is None:
        return t("div", "пользователей: —", class_="muted small")
    off = p.get("users_off") or 0
    tip = ["с протоколом: " + (", ".join(p.get("user_names") or ()) or "никого")]
    if off:
        tip.append("отключены: " + ", ".join(p.get("off_names") or ()))
    if p.get("lacking"):
        tip.append("без протокола: " + ", ".join(f"{name} ({why})" for name, why in p["lacking"]))
    return t("div", f"пользователей: {n}", t("span", f" (+{off} откл.)", class_="nw") if off else None,
             class_="muted small", title=NL.join(tip))


USERS_TRAFFIC_TITLE = "трафик пользователей сегодня (без служебного пробника)"


# ---------- общие куски ----------

def page_head(title: str, sub: Any = None, actions: Any = None) -> Markup:
    return t("div", t("div", t("h1", title), t("div", sub, class_="sub") if sub else None),
             t("div", actions, class_="actions") if actions else None, class_="page-head")


def period_selector(base: str, current: str, extra: dict[str, str] | None = None) -> Markup:
    links = []
    for p in traffic.PERIODS:
        q = "&".join(f"{k}={v}" for k, v in {**(extra or {}), "period": p}.items())
        links.append(t("a", p, href=f"{base}?{q}", class_="active" if p == current else None))
    return t("nav", links, class_="seg", aria_label="Период")


def get_period(req: "Request", default: str = "24h") -> str:
    try:
        return traffic.resolve_period(req.query.get("period", default))
    except ValueError:
        return default


def ago(ts: float | None) -> str:
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 90:
        return "только что"
    return human_duration(delta) + " назад"


def fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def alert_list(items: list[tuple[Any, ...]]) -> Markup:
    """(вид, текст) или (вид, текст, действия): действия — ссылки справа в строке тревоги."""
    icons = {"bad": "✕", "warn": "!", "ok": "✓", "info": "i"}
    return t("ul", [t("li", t("span", icons.get(it[0], "!"), class_="ico"), t("span", it[1], class_="msg"),
                      t("span", it[2], class_="acts") if len(it) > 2 and it[2] else None, class_=it[0])
                    for it in items], class_="alerts")


def chart_block(ts: dict[str, Any], label: str, empty_text: str = "Нет данных за период",
                wide: bool = False) -> Markup:
    """График в карточке: wide — на всю ширину, иначе — в половину (свой viewBox, чтобы
    подписи осей были одного размера)."""
    if not ts["series"] or not any(sum(s["values"]) for s in ts["series"]):
        return empty(empty_text)
    width, height = (1100, 280) if wide else (560, 260)
    return join(charts.columns(ts["buckets"], ts["step"], ts["series"], label, width, height),
                charts.legend(ts["series"]) if len(ts["series"]) > 1 else None)


def status_data(app: "App") -> dict[str, Any]:
    """Метрики /proc — на каждый запрос (дёшево), остальное (юниты, сокеты, 3x-ui) — раз в минуту."""
    slow = app.cached("status-slow", 60, lambda: status.collect_slow(app.cfg()))
    return {**slow, "host": system.host_metrics(0.3)}


def no_history_hint(csrf: str = "") -> Markup:
    return empty("Нет данных · сбор каждые 5 мин",
                 post_button("/settings/action", "Снять сейчас", csrf, "btn small", COLLECT_ACTION) if csrf else None)


def _tile(label: str, value: str, hint: str = "", extra: Any = None, title: str | None = None,
          label_title: str | None = None) -> Markup:
    """Плитка-сетка: подпись, значение, место под полоску (занято всегда), подсказка — всё в одну строку."""
    return t("div", t("div", label, class_="label", title=label_title or label),
             t("div", value, class_="value", title=value),
             t("div", extra, class_="meter-slot"),
             t("div", hint, class_="hint", title=title or hint or None), class_="tile")


# ---------- обзор ----------

def _unit_name(name: str) -> str:
    return name if "." in name else name + ".service"


def restart_confirm(unit: str) -> str:
    if unit.startswith("x-ui"):
        return "x-ui держит VLESS и TUIC: соединения оборвутся на несколько секунд. Перезапустить?"
    return f"Перезапустить {unit}?"


def _down_units(st: dict[str, Any]) -> dict[str, str]:
    """{имя из текста проблемы: юнит} для включённых и загруженных, но неактивных юнитов."""
    down: dict[str, str] = {}
    loaded = {u for u, s in st["services"].items() if s.get("load") == "loaded"}
    for p in st["protocols"]:
        if p["enabled"]:
            for raw, state in p["services"].items():
                if state != "active" and _unit_name(raw) in loaded:
                    down[raw] = _unit_name(raw)
    for u in (*status.BASE_UNITS, *status.ZOO_UNITS):
        s = st["services"].get(u, {})
        if u in loaded and s.get("active") != "active":
            down[u] = u
    return down


def _unit_actions(unit: str, csrf: str) -> list[Markup]:
    return [t("a", "журнал", href=f"/logs?src=unit:{unit}"),
            post_button("/settings/action", "перезапустить", csrf, "", {"action": "restart", "unit": unit},
                        confirm=restart_confirm(unit))]


def collect_alerts(app: "App", st: dict[str, Any], csrf: str = "") -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    down = _down_units(st)
    seen: set[str] = set()
    for p in st["problems"]:
        unit = next((u for name, u in down.items() if u not in seen
                     and re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", p)), None)
        if unit:
            seen.add(unit)
        out.append(("warn" if p.startswith("сертификат") else "bad", p, _unit_actions(unit, csrf) if unit else None))
    # таймер коллектора, его задержки и ошибки источников уже в problems (status.collect)
    for unit in status.ZOO_UNITS:
        s = st["services"].get(unit, {})
        if unit == "zoo-collector.timer" or unit in seen:
            continue
        if s.get("load") == "loaded" and s.get("enabled") == "enabled" and s.get("active") != "active":
            out.append(("bad", f"{unit} — {s.get('active')}", _unit_actions(unit, csrf)))
    if REBOOT_FLAG.exists():
        pkgs = ""
        try:
            names = Path(str(REBOOT_FLAG) + ".pkgs").read_text(encoding="utf-8").split()
            pkgs = ", ".join(sorted(set(names)))
        except OSError:
            pass
        cfg = app.cfg()
        if cfg.get("AUTO_REBOOT") == "1":
            out.append(("info", t("span", f"Перезагрузится сам в {cfg.get('AUTO_REBOOT_TIME') or '04:00'}",
                                  title=pkgs or None)))
        else:
            out.append(("warn", t("span", "Нужна перезагрузка сервера", title=pkgs or None)))
    if traffic.last_run() is None:
        out.append(("warn", "Трафик ещё не собирался: первое снятие — в течение 5 минут после установки"))
    out += protoviews.alerts()
    out += app.cached("journal-alerts", 60, journal.alerts)
    out += app.cached("storage-alerts", 60, lambda: storage.alerts(app.cfg()))
    return out


def overview(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    pctx = protoviews.context()
    protoviews.note_finished(app, pctx)
    st = status_data(app)
    host = st["host"]
    today_p = traffic.today("protocol")
    today_u = traffic.today("user")
    service_bytes = max(0, sum(traffic.today("user", include_hidden=True).values()) - sum(today_u.values()))
    spark = {s["key"]: s["values"] for s in traffic.timeseries("24h", "protocol", top=50)["series"]}

    alerts = collect_alerts(app, st, csrf)
    mem, disk = host.get("mem") or {}, host.get("disk") or {}
    load = host.get("load")
    swap_used = (mem.get("swap_total") or 0) - (mem.get("swap_free") or 0)
    active_users = sum(1 for v in today_u.values() if v)
    tiles = t("div",
              _tile("CPU", f"{host.get('cpu_percent', '—')} %", f"load {load[0]:.2f}" if load else "",
                    title=f"ядер {host.get('cpu_count')} · load {' '.join(f'{x:.2f}' for x in load)}" if load else None),
              _tile("Память", human_bytes(mem.get("used")), f"из {human_bytes(mem.get('total'))}",
                    charts.meter(mem.get("used") or 0, mem.get("total") or 0),
                    title=f"swap {human_bytes(swap_used)}" if mem.get("swap_total") else None),
              _tile("Диск /", human_bytes(disk.get("used")), f"{human_bytes(disk.get('free'))} своб.",
                    charts.meter(disk.get("used") or 0, disk.get("total") or 0, 0.85, 0.95)),
              _tile("Трафик сегодня", human_bytes(today_p.get(traffic.HOST)), "сервер",
                    title="интерфейс сервера: приём + отдача"),
              _tile("Трафик пользователей", human_bytes(sum(today_u.values())),
                    f"{active_users}/{st['users']['total']} активны"
                    + (f" · служебный {human_bytes(service_bytes)}" if service_bytes else ""),
                    label_title=USERS_TRAFFIC_TITLE,
                    title=f"{USERS_TRAFFIC_TITLE}; служебный трафик пробника zoo-probe: {human_bytes(service_bytes)}"
                    if service_bytes else USERS_TRAFFIC_TITLE),
              _tile("Аптайм", human_duration(host.get("uptime")),
                    f"Xray {st['xray'].get('state') or '—'}" if st["xray"] else ""),
              class_="tiles")

    cards, off = [], []
    for i, p in enumerate(st["protocols"]):
        if not p["enabled"]:
            off.append(p)
            continue
        # короткое имя — в заголовок, полное — в title (на 380 px длинные имена переносились)
        about = f"{p['id']} · {p['engine'] or '—'}"
        problems = [t("span", f"{u}: {s}", class_="chip bad") for u, s in p["services"].items() if s != "active"]
        problems += [t("span", f"{k} не слушает", class_="chip bad") for k, v in p["listening"].items() if not v]
        cards.append(card(
            p.get("short") or p["name"].partition(" (")[0],
            t("div", t("span", f"{p['port']}/{p['layer']}", class_="chip", title=about), problems, class_="chips"),
            protoviews.metrics_row(p, pctx, csrf),
            t("div",
              t("div", t("div", protoviews.approx(pctx, p["id"]) + human_bytes(protoviews.today_bytes(pctx, p["id"])), class_="num-big",
                         title="сегодня, без служебных замеров" + (" (≈: у Xray-протоколов служебный трафик делится по долям замеров)"
                                                                    if protoviews.approx(pctx, p["id"]) else "")),
                users_line(p)),
              charts.sparkline(spark.get(p["id"], []), charts.series_class(i)),
              class_="row"),
            protoviews.switch_row(p, pctx, csrf),
            cls="proto", tip=p["name"], extra=protoviews.head_badge(p, pctx)))
    if cards:
        protos: Any = t("div", cards, class_="grid")
    else:
        protos = empty("Манифестов протоколов нет: фазы 04–06 не выполнены?") if not off else None

    top_users = sorted(today_u.items(), key=lambda kv_: -kv_[1])[:6]
    mx = max((v for _, v in top_users), default=0)
    users_card = card("Пользователи сегодня",
                      table(["имя", "трафик", ""],
                            [[t("a", n, href=f"/users/{n}"), human_bytes(v), charts.bar(v, mx)]
                             for n, v in top_users],
                            num=[1], empty="трафика сегодня ещё не было"),
                      t("p", f"служебный трафик пробника: {human_bytes(service_bytes)}", class_="quiet")
                      if service_bytes else None,
                      extra=t("a", "все →", href="/users", class_="small"),
                      help="Трафик пользователей за сегодня, без служебного пользователя пробника (zoo-probe): "
                           "он учтён только в итогах протоколов.")
    expiring = [c for c in st["certs"] if c["days_left"] is None or c["days_left"] < status.CERT_WARN_DAYS]
    certs = [[c["path"], fmt_time(_iso_ts(c["not_after"])),
              badge("не читается", "bad") if c["days_left"] is None else badge(f"{c['days_left']} дн.", "warn")]
             for c in expiring]
    rows = [("адрес", st["server"]["ip"] or "—"), ("имя", st["server"]["hostname"]),
            ("UFW", {None: "не установлен", True: "включён", False: "ВЫКЛЮЧЕН"}[st["firewall"]["ufw_active"]]),
            ("Xray", f"{st['xray'].get('state', '')} {st['xray'].get('version', '')}".strip() or "—")]
    if st["exposed"]:
        rows.append(("лишние открытые порты", ", ".join(f"{e['port']}/{e['proto']}" for e in st["exposed"])))
    sys_card = card("Сервер", kv(rows), table(["файл", "до", ""], certs) if certs else None)
    start = t("a", "Get started", href="/connect/new", class_="btn primary", data_swap=True,
              title="Новая группа: протоколы, клиенты, люди и что им отправить")
    body = [page_head("Обзор", None, [start, None if alerts else badge("✓ всё в порядке", "ok")]),
            alert_list(alerts) if alerts else None,
            tiles, t("h2", "Протоколы"), protoviews.caption(pctx), protos,
            protoviews.off_block(off, pctx, csrf),
            t("div", users_card, sys_card, class_="cols")]
    return app.render(req, "Обзор", body, active="/")


def _iso_ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


# ---------- трафик ----------

TH_UP = ("↑", "от клиента: загрузка на сервер")
TH_DOWN = ("↓", "к клиенту: скачивание с сервера")
TH_SUM = ("Σ", "всего")


def _traffic_users_spec(period: str, mx: int) -> tbl.Spec:
    def num(key: str, head: tuple[str, str], **kw: Any) -> tbl.Col:
        return tbl.Col(key, head[0], hint=head[1], cell=lambda r: human_bytes(r[key]), value=lambda r: r[key],
                       num=True, sort=True, first_desc=True, **kw)

    cols = [tbl.Col("key", "пользователь", cell=lambda r: t("a", r["key"], href=f"/users/{r['key']}?period={period}"),
                    sort=True, search=True),
            num("up", TH_UP, secondary=True), num("down", TH_DOWN, secondary=True), num("total", TH_SUM),
            tbl.Col("bar", "", cell=lambda r: charts.bar(r["total"], mx), secondary=True, export=False)]
    return tbl.Spec(path="/traffic", cols=cols, sort="total", desc=True, id_key="key", paged=False,
                    keep=[("period", period)], empty="трафика не было", placeholder="пользователь",
                    href=lambda r: f"/users/{r['key']}?period={period}", name=f"traffic-users-{period}")


def _traffic_body(period: str, st: tbl.State | None = None) -> list[Any] | None:
    """Страница без форм и без данных сессии, поэтому её можно держать в кэше. None — данных нет."""
    rep_u = traffic.report(period=period, by="user")
    if rep_u.get("empty"):
        return None
    rep_p = traffic.report(period=period, by="protocol")
    ts_p = traffic.timeseries(period, "protocol", top=8)
    ts_u = traffic.timeseries(period, "user", top=8)
    ts_h = traffic.timeseries(period, "host")
    host_total = sum(sum(s["values"]) for s in ts_h["series"])
    tot = rep_u["total"]
    tiles = t("div",
              _tile("Пользователи", human_bytes(tot["total"]),
                    f"↑ {human_bytes(tot['up'])} · ↓ {human_bytes(tot['down'])}",
                    title=f"↑ {human_bytes(tot['up'])} от клиентов · ↓ {human_bytes(tot['down'])} к клиентам"),
              _tile("Сервер целиком", human_bytes(host_total), "приём + отдача"),
              _tile("Активных", str(sum(1 for r in rep_u["rows"] if r["total"])), traffic.PERIOD_TITLES[period],
                    title="пользователей с трафиком"),
              class_="tiles")
    mx_u = max((r["total"] for r in rep_u["rows"]), default=0)
    mx_p = max((r["total"] for r in rep_p["rows"]), default=0)
    uspec = _traffic_users_spec(period, mx_u)
    ust = st or tbl.parse(uspec, {})
    users_tbl = tbl.render(uspec, ust, tbl.memory_page(uspec, ust, rep_u["rows"]))
    proto_tbl = table(["протокол", TH_UP, TH_DOWN, TH_SUM, ""],
                      [[r["title"], human_bytes(r["up"]), human_bytes(r["down"]), human_bytes(r["total"]),
                        charts.bar(r["total"], mx_p, "s2")] for r in rep_p["rows"]],
                      num=[1, 2, 3], empty="трафика не было", stack=True)
    chips = [t("span", "ⓘ", class_="chip", title="Xray-протоколы (VLESS, XHTTP, SS-2022, TUIC) считаются по "
               "счётчикам inbound 3x-ui. У пользователя их трафик общий — строка «Xray (общий счётчик)»: "
               "3x-ui считает клиента одним счётчиком на все его inbound.")]
    if rep_p["total"]["total"] > tot["total"]:
        chips.append(t("span", "в т.ч. самопроверка", class_="chip", title="В протоколах учтён и трафик служебного "
                       "пользователя пробника (zoo-probe): в списке пользователей его нет."))
    host_series = [{"title": "принято", "values": s["up"]} for s in ts_h["series"]] + \
                  [{"title": "отправлено", "values": s["down"]} for s in ts_h["series"]]
    host_ts = {"buckets": ts_h["buckets"], "step": ts_h["step"], "series": host_series}
    return [tiles,
            t("div", card("По протоколам", chart_block(ts_p, "Трафик по протоколам")),
              card("По пользователям", chart_block(ts_u, "Трафик по пользователям")), class_="cols"),
            t("div", card("Пользователи", users_tbl), card("Протоколы", proto_tbl, extra=t("div", chips, class_="chips")),
              class_="cols"),
            card("Сервер целиком", chart_block(host_ts, "Трафик интерфейса сервера", wide=True),
                 extra=t("span", "×2", class_="chip", title="Всё, что прошло через сетевой интерфейс сервера: "
                         "трафик клиентов учитывается дважды (от клиента и в интернет), плюс обновления и "
                         "служебный трафик."))]


def traffic_page(app: "App", req: "Request") -> "Response":
    period = get_period(req)
    head = page_head("Трафик", None, period_selector("/traffic", period))
    run = traffic.last_run()
    uspec = _traffic_users_spec(period, 0)
    ex = tbl.export_request(uspec, req.query)
    if ex is not None:
        return tbl.memory_export(uspec, req.query, traffic.report(period=period, by="user").get("rows", []))
    st = tbl.parse(uspec, req.query)
    key = ("traffic", period, run["ts"] if run else None, st.key)  # данные меняются только с разбором коллектора
    body = app.cache_get(key, 600)
    if body is None:
        body = _traffic_body(period, st)
        if body is None:
            csrf = req.session.csrf if req.session else ""
            return app.render(req, "Трафик", [head, card("Трафик", no_history_hint(csrf))], active="/traffic")
        app.cache_put(key, body, 600)
    return app.render(req, "Трафик", [head, *body], active="/traffic")


# ---------- проверка ----------
# Пробник — zoolib.probe: `zoo probe --local` сам сохраняет отчёт в selftest_file(),
# сравнение «сервер ↔ клиент» — probe.report.compare.

CATEGORY = {"works": "ok", "blocked": "bad", "server": "bad", "unknown": "muted"}


def _verdict_kind(v: str | None) -> str:
    if not v:
        return "muted"
    if v in probe_mod.verdicts.WORKING:
        return "ok" if v == probe_mod.verdicts.OK else "warn"
    return "muted" if v in probe_mod.verdicts.NOT_TESTED else "bad"


def verdict_badge(v: str | None) -> Markup:
    return badge(v or "—", _verdict_kind(v))


def load_selftest() -> dict[str, Any] | None:
    try:
        data = json.loads(probe_mod.selftest_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("results"), list) else None


def results_table(results: list[dict[str, Any]]) -> Markup:
    rows = []
    for r in results:
        why = "; ".join(([r["reason"]] if r.get("reason") else []) + list(r.get("notes") or []))
        rtt = (r.get("l4") or {}).get("rtt_ms")
        lat = r.get("latency_ms")
        rows.append([t("span", t("strong", r.get("id")),
                       t("span", f"{r.get('port') or '?'}/{r.get('layer') or '?'}", class_="sub")),
                     verdict_badge(r.get("verdict")),
                     t("span", "—" if lat is None else f"{lat:.0f} мс",
                       t("span", f"порт {rtt:.0f} мс", class_="sub") if rtt is not None else None),
                     "—" if r.get("speed_mbps") is None else f"{r['speed_mbps']:.1f} Мбит/с",
                     r.get("egress_ip") or "—", t("span", why, class_="small")])
    return table(["протокол", "итог", ("задержка", "время до первого байта через туннель; ниже — TCP-соединение "
                                        "с портом сервера (RTT), если протокол по TCP"),
                  "скорость", "IP выхода", "причина"], rows,
                 num=[2, 3], empty="протоколов для проверки нет", stack=True)


def verdict_legend(results: list[dict[str, Any]]) -> Markup | None:
    seen = sorted({r.get("verdict") for r in results if r.get("verdict") and r.get("verdict") != "OK"})
    if not seen:
        return None
    return t("ul", [t("li", verdict_badge(v), " ", probe_mod.verdicts.DESCRIPTIONS.get(v, "")) for v in seen],
             class_="legend")


def probe_page(app: "App", req: "Request", compare_rows: list[dict[str, Any]] | None = None,
               report_text: str = "", error: str = "", notice: str = "") -> "Response":
    ex = probeviews.export_runs(app, req)
    if ex is not None:
        return ex
    run = probeviews.run_param(req)
    if run is not None:
        return probeviews.run_page(app, req, run)
    csrf = req.session.csrf if req.session else ""
    running = app.jobs.running("probe")
    last = load_selftest()
    local_body: list[Any] = []
    if running:
        local_body.append(alert_list([("info", t("span", "Самопроверка идёт… ",
                                                  t("a", "ход выполнения", href=f"/jobs/{running.id}")))]))
    if last:
        names = {"public": "публичный IP", "loopback": "loopback"}
        targets = sorted({names.get(r.get("target"), r.get("target")) for r in last["results"] if r.get("target")})
        local_body += [t("p", f"{_gen_time(last.get('generated'))} · {last.get('server_ip') or '?'}"
                              + (f" · {', '.join(targets)}" if targets else ""), class_="muted small"),
                       results_table(last["results"])]
    elif not running:
        local_body.append(empty("Самопроверка ещё не запускалась"))
    run_btn = t("form", csrf_input(csrf),
                t("button", "Запустить", type="submit", class_="btn primary small",
                  title="Сервер поднимает клиента каждого протокола у себя и идёт на свой публичный IP: "
                        "работает ли протокол в принципе (1–3 минуты)."),
                method="post", action="/probe/run", class_="inline", data_swap=True)
    local = card("Самопроверка с сервера", *local_body, extra=run_btn,
                 help=verdict_legend(last["results"]) if last else None)
    cmp_help = t("p", "Отчёт клиентского пробника — файл ", t("code", "probe/probe-report.json"),
                 " после запуска контейнера zoo-probe на машине пользователя (README, «Блокирует ли ваш "
                 "провайдер»). Пакет для пробника: ", t("code", str(paths.probe_export_file())),
                 " (его создаёт самопроверка; в нём ключи — передавайте по scp и удалите после).")
    cmp_body: list[Any] = [
        t("form", csrf_input(csrf),
          t("div", t("label", "JSON-отчёт клиента", for_="report"),
            t("textarea", report_text, name="report", id="report", spellcheck="false",
              placeholder='{"type": "zoo-probe-report", "results": [{"id": "vless-reality", "verdict": "OK"}]}'),
            class_="field"),
          t("div", t("div", t("label", "или файл", for_="report-file"),
                     t("input", type="file", id="report-file", accept=".json,application/json", data_fill="report"),
                     class_="field grow"),
            t("div", t("label", "метка", for_="tag"),
              t("input", type="text", name="tag", id="tag", maxlength="40", placeholder="mobile-mts",
                title="необязательно: сеть, из которой снят отчёт"), class_="field"),
            t("div", t("label", "устройство", for_="device"),
              t("input", type="text", name="device", id="device", maxlength="40", placeholder="pixel7"),
              class_="field"),
            t("button", "Сравнить", type="submit", class_="btn primary",
              title="Сравнить с самопроверкой и записать в историю"), class_="form-row"),
          method="post", action="/probe/compare", class_="stack", data_swap=True)]
    if error:
        cmp_body.insert(0, alert_list([("bad", error)]))
    if notice:
        cmp_body.insert(0, alert_list([("info", notice)]))
    if compare_rows is not None:
        rows = [[t("strong", r["id"]), verdict_badge(r.get("server")), verdict_badge(r.get("client")),
                 badge(r.get("verdict", ""), CATEGORY.get(r.get("category", ""), "muted"))] for r in compare_rows]
        cmp_body += [t("h3", "Сравнение"), table(["протокол", "сервер", "клиент", "вывод"], rows, stack=True)]
    body = [page_head("Проверка"), local, *probeviews.cards(app, req),
            card("Сравнить с клиентом", *cmp_body, help=cmp_help, id_=probeviews.HOW_ANCHOR)]
    return app.render(req, "Проверка", body, active="/probe")


def _gen_time(value: str | None) -> str:
    return fmt_time(_iso_ts(value)) if value else "—"


def probe_run(app: "App", req: "Request") -> "Response":
    from .app import redirect
    job = app.jobs.start("probe", "Самопроверка протоколов (zoo probe --local)",
                         outside_sandbox(zoo_argv("probe", "--local", "--json", "--quiet")), timeout=900)
    return redirect(f"/jobs/{job.id}")


def probe_compare(app: "App", req: "Request") -> "Response":
    raw = req.form.get("report", "").strip()
    if not raw:
        return probe_page(app, req, error="Вставьте JSON-отчёт или выберите файл.")
    try:
        remote = json.loads(raw)
    except json.JSONDecodeError as e:
        return probe_page(app, req, report_text=raw, error=f"Это не JSON: {e}")
    if isinstance(remote, list):
        remote = {"results": remote}
    results = remote.get("results") if isinstance(remote, dict) else None
    if not isinstance(results, list) or not all(isinstance(r, dict) and isinstance(r.get("id"), str)
                                                 and isinstance(r.get("verdict"), str) for r in results):
        return probe_page(app, req, report_text=raw, error="В отчёте нет списка results вида {id, verdict}.")
    try:
        tag = probe_mod.context.clean_label(req.form["tag"], "метка") if req.form.get("tag", "").strip() else None
        device = (probe_mod.context.clean_label(req.form["device"], "устройство")
                  if req.form.get("device", "").strip() else None)
    except ValueError as e:
        return probe_page(app, req, report_text=raw, error=str(e))
    notice = probe_mod.history.record_notice(remote, "upload", tag=tag, device=device)
    local = load_selftest()
    if local is None:
        return probe_page(app, req, report_text=raw, notice=notice,
                          error="Нет серверной самопроверки для сравнения — сначала нажмите «Запустить».")
    return probe_page(app, req, probe_mod.report.compare(local, remote), report_text=raw, notice=notice)


# ---------- логи ----------

def service_units() -> list[str]:
    """Юниты зоопарка по манифестам (без status.collect: он тянет сокеты, сертификаты и API 3x-ui)."""
    units = list(status.BASE_UNITS)
    for m in manifests.load_all()[0]:
        units += [_unit_name(u) for u in m.services]
    units.append("zoo-collector.timer")
    return list(dict.fromkeys(units))


def log_sources(app: "App") -> tuple[list[tuple[str, str, str]], dict[str, dict[str, str]]]:
    """([(группа, ключ, подпись)], состояния юнитов) — только эти источники можно открыть."""
    out = [("Установка", f"file:{f.name}", f.name) for f in logs.log_files()]
    units = ["zoo-web.service", "zoo-collector.service", *service_units(), GEO_UNIT]
    states = system.unit_states(units)
    loaded = {u for u, s in states.items() if s.get("load") == "loaded"}
    out += [("Сервисы", f"unit:{u}", u) for u in dict.fromkeys(units) if u in loaded and u != "zoo-collector.timer"]
    return out, states


# ---------- настройки ----------

def restartable_units(app: "App") -> list[str]:
    units = service_units()
    states = system.unit_states(units)
    return [u for u in units if states.get(u, {}).get("load") == "loaded"]


UPSTREAM_HINT = "обновление — через пины репозитория (bump-pins, sha256), не автоматом"


def _available_cell(c: dict[str, Any]) -> Markup:
    label = c.get("label", "не проверено")
    return badge(label, "warn") if c.get("newer") else t("span", label, class_="muted")


def _version_status(c: dict[str, Any]) -> str:
    return "—" if c["outdated"] is None else ("устарел" if c["outdated"] else "актуален")


def _versions_spec() -> tbl.Spec:
    cols = [tbl.Col("name", "компонент", sort=True),
            tbl.Col("installed", "установлен", secondary=True),
            tbl.Col("pinned", "закреплён", secondary=True),
            tbl.Col("avail", "доступно", cell=lambda r: _available_cell(r["c"]), value=lambda r: r["c"].get("label", ""),
                    secondary=True),
            tbl.Col("status", "", cell=lambda r: badge("—", "muted") if r["c"]["outdated"] is None else
                    (badge("устарел", "warn") if r["c"]["outdated"] else badge("актуален", "ok")), chip=True)]
    return tbl.Spec(path="/settings", cols=cols, sort="name", id_key="name", prefix="v_", paged=False, export=False,
                    empty="нет данных", name="versions")


def _versions_card(app: "App", req: "Request", cfg: Any) -> Markup:
    help_ = join(t("p", "«Доступно» — свежий релиз на GitHub: проверка раз в сутки (", t("code", "zoo upgrade --check-upstream"),
                   "), страница читает только кэш. ", UPSTREAM_HINT.capitalize(), "."),
                 t("p", "Обновление версий — из консоли: ", t("code", "sudo zoo upgrade"), " (план) и ",
                   t("code", "sudo zoo upgrade --apply"), ": фазы перезапускают сервисы, в том числе эту админку."))
    try:
        up = app.cached("upgrade", 60, lambda: upgrade.check(cfg))
        comps, plan = up["components"], up["phases"]
        ver_rows = [{"name": name, "installed": c["installed"], "pinned": c["pinned"], "c": c,
                     "status": _version_status(c)} for name, c in comps.items()]
        vspec = _versions_spec()
        vopts = tbl.options_from_rows(vspec, ver_rows)
        vst = tbl.parse(vspec, req.query, vopts)
        vtbl = tbl.render(vspec, vst, tbl.memory_page(vspec, vst, ver_rows, vopts))
        hint = t("p", UPSTREAM_HINT, class_="hint") if any(c.get("newer") for c in comps.values()) else None
        if comps and not plan and all(c["outdated"] is False for c in comps.values()):
            return card("Версии", badge("✓ всё актуально", "ok"),
                        t("details", t("summary", "компоненты"), vtbl, class_="more"), hint, help=help_)
        return card("Версии", vtbl, t("p", "План: " + " → ".join(plan), class_="hint") if plan else None, hint,
                    help=help_)
    except Exception as e:  # сводка версий не должна ломать страницу
        return card("Версии", alert_list([("warn", f"не удалось сравнить версии: {e}")]), help=help_)


def _date(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y") if ts else "—"


def _data_card(app: "App", cfg: Any, csrf: str) -> Markup:
    help_ = t("p", "Данные zoo (трафик, журнал атак, история проб, логи установки) делят один бюджет ",
              t("code", storage.LIMIT_KEY), ": по умолчанию 1 ГБ, но не больше 5 % диска. Пока он не превышен, ничего "
              "не удаляется; при превышении коллектор режет раздел, сильнее всех вышедший за свою долю, начиная "
              "с самых старых записей. journald (лимит 500 МБ, ставит установщик), geo/prev и geoip в бюджет "
              "не входят.")
    try:
        d = app.cached("storage", 60, lambda: storage.report(cfg))
    except Exception as e:  # сводка объёма не должна ломать страницу
        return card("Данные", alert_list([("warn", f"не удалось посчитать объём: {e}")]), help=help_)
    rows = []
    for s in d["sections"]:
        if not s["available"]:
            continue
        action = post_button("/settings/action", "очистить", csrf, "btn small",
                             {"action": "storage-clear", "section": s["id"]},
                             confirm=f"Очистить раздел «{s['title']}»? Записи удалятся насовсем.") if s["size"] else None
        rows.append([s["title"], human_bytes(s["size"]),
                     join(charts.meter(s["size"], s["share_bytes"]),
                          t("span", f"{s['share']} %", class_="muted small",
                            title=f"доля бюджета: {human_bytes(s['share_bytes'])}")),
                     _date(s["oldest"]), action])
    line = f"занято {human_bytes(d['total'])} из {human_bytes(d['limit'])}"
    if d["disk"]:
        line += f" · свободно на диске {human_bytes(d['disk']['free'])}"
    outside = ", ".join(f"{o['title']} {human_bytes(o['size'])}" for o in d["outside"])
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="action", value="storage-limit"),
             t("label", "Бюджет данных ", t("input", name="limit", value=storage.format_size(d["limit"]),
                                          size="6", maxlength="12", aria_label="Бюджет данных, например 1G")),
             t("button", "Сохранить", type="submit", class_="btn small"),
             method="post", action="/settings/action", class_="inline", data_swap=True)
    more = t("details", t("summary", "бюджет и то, что вне его"), form,
             t("p", f"Вне бюджета: {outside}", class_="hint") if outside else None, class_="more")
    return card("Данные", t("p", line),
                table(["раздел", "занято", "доля бюджета", "хранится с", ""], rows, stack=True), more, help=help_)


# префикс ключа → группа; побеждает самый длинный, секреты и всё неизвестное — «Технические»
CONFIG_MAIN, CONFIG_PROTO, CONFIG_TECH = "Основное", "Протоколы", "Технические"
CONFIG_PREFIXES = {
    "LABEL": CONFIG_MAIN, "SERVER_IP": CONFIG_MAIN, "DOMAIN": CONFIG_MAIN, "SSH_": CONFIG_MAIN,
    "RU_EGRESS": CONFIG_MAIN, "AUTO_REBOOT": CONFIG_MAIN, "SUB_PUBLIC": CONFIG_MAIN, "ZOO_DATA_LIMIT": CONFIG_MAIN,
    "ENABLE_": CONFIG_PROTO, "VLESS_PORT": CONFIG_PROTO, "VLESS_SNI": CONFIG_PROTO, "VLESS_TARGET": CONFIG_PROTO,
    "XHTTP_PORT": CONFIG_PROTO, "XHTTP_SNI": CONFIG_PROTO, "XHTTP_PLACEMENT": CONFIG_PROTO,
    "SS_PORT": CONFIG_PROTO, "TUIC_PORT": CONFIG_PROTO, "TUIC_SNI": CONFIG_PROTO,
    "HY2_PORT": CONFIG_PROTO, "HY2_SNI": CONFIG_PROTO, "HY2_ENGINE": CONFIG_PROTO, "HY2_HOP": CONFIG_PROTO,
    "AWG_PORT": CONFIG_PROTO, "AWG_PROFILE": CONFIG_PROTO, "AWG_ENGINE": CONFIG_PROTO,
    "VLESS_PORT_MIGRATED": CONFIG_TECH, "AWG_ENGINE_": CONFIG_TECH,
}


def config_group(key: str) -> str:
    if logs.SECRET_KEY_RE.search(key):
        return CONFIG_TECH
    best = max((p for p in CONFIG_PREFIXES if key.startswith(p)), key=len, default=None)
    return CONFIG_PREFIXES[best] if best else CONFIG_TECH


def _config_card(cfg: Any) -> Markup:
    groups: dict[str, list[str]] = {CONFIG_MAIN: [], CONFIG_PROTO: [], CONFIG_TECH: []}
    for k in sorted(cfg.values):
        groups[config_group(k)].append(k)

    def cfg_table(keys: list[str]) -> Markup:
        return table(["ключ", "значение"],
                     [[t("code", k), t("span", "••••", class_="muted", title="скрыто") if logs.SECRET_KEY_RE.search(k)
                       else t("span", cfg.get(k), class_="mono")] for k in keys], stack=True)

    parts: list[Any] = [t("p", "Что выбрано при установке. Менять — повторным запуском install.sh.", class_="hint")]
    for name in (CONFIG_MAIN, CONFIG_PROTO):
        if groups[name]:
            parts += [t("h4", name), cfg_table(groups[name])]
    if groups[CONFIG_TECH]:
        parts.append(t("details", t("summary", f"{CONFIG_TECH} · {len(groups[CONFIG_TECH])}"),
                       cfg_table(groups[CONFIG_TECH]), class_="more"))
    return card("config.env", t("details", t("summary", f"Настройки сервера · только чтение · ключей: {len(cfg.values)}"),
                                *parts, class_="more"),
                help=f"{cfg.path} · пароли и ключи скрыты")


def settings_page(app: "App", req: "Request") -> "Response":
    cfg = app.cfg()
    csrf = req.session.csrf if req.session else ""
    from . import access_info
    ssh = access_info(cfg)["ssh"]

    units = service_units()
    states = system.unit_states(units)
    unit_rows = []
    for u in units:
        s = states.get(u) or {}
        if s.get("load") != "loaded":
            continue
        active = s.get("active", "")
        restarts = int(s["restarts"]) if str(s.get("restarts", "")).isdigit() else 0
        unit_rows.append([
            t("code", u, title=f"автозапуск: {s.get('enabled') or '—'}"),
            t("span", badge(active or "?", "ok" if active == "active" else "bad"),
              t("span", f" ↻ {restarts}", class_="muted small", title="рестартов") if restarts > 0 else None),
            post_button("/settings/action", "Перезапустить", csrf, "btn small", {"action": "restart", "unit": u},
                        confirm=restart_confirm(u))])
    services = card("Сервисы", table(["юнит", "состояние", ""], unit_rows, stack=True))

    actions = card("Обслуживание", t("div",
        post_button("/settings/action", "Smoke-проверка", csrf, "btn", {"action": "smoke"}),
        post_button("/settings/action", "Снять трафик", csrf, "btn", COLLECT_ACTION),
        post_button("/settings/action", "Обновить geo-файлы", csrf, "btn", {"action": "geo"}),
        class_="btn-grid"))
    versions = _versions_card(app, req, cfg)
    data = _data_card(app, cfg, csrf)

    jobs = app.jobs.recent(8)
    jobs_card = card("Последние задачи", table(["задача", "начата", "итог"], [
        [t("a", j.title, href=f"/jobs/{j.id}"), fmt_time(j.started), job_badge(j)] for j in jobs],
        empty="задач не было", stack=True)) if jobs else None

    access = None
    if cfg.get("PANEL_PORT"):
        port, path = cfg.get("PANEL_PORT"), cfg.get("PANEL_PATH")
        full = f"http://127.0.0.1:{port}/" + (f"{path}/" if path else "")
        shown = f"http://127.0.0.1:{port}/" + ("•••/" if path else "")
        access = card("Доступ к 3x-ui",
                      t("code", f"ssh -N -L {port}:127.0.0.1:{port} {ssh}", class_="cmd"),
                      t("div", t("code", shown), t("input", type="hidden", id="panel-url", value=full),
                        t("button", "Копировать", type="button", class_="btn small", data_copy="panel-url"),
                        class_="actions"),
                      help=t("p", "Логин и пароль панели — в CREDENTIALS.md. Новый токен админки: ",
                             t("code", "sudo zoo web --new-token"), " (все сессии закроются). Вход по ссылке: ",
                             t("code", "sudo zoo web --link"), "."))

    config = _config_card(cfg)
    body = [page_head("Настройки"), services, t("div", actions, versions, class_="cols"), data, jobs_card,
            t("div", access, config, class_="cols") if access else config]
    return app.render(req, "Настройки", body, active="/settings")


def settings_action(app: "App", req: "Request") -> "Response":
    from .app import redirect
    action = req.form.get("action", "")
    if action == "restart":
        unit = req.form.get("unit", "")
        if unit not in restartable_units(app):
            req.session.flash("bad", f"Юнит {unit} нельзя перезапустить отсюда")
            return redirect("/settings")
        job = app.jobs.start(f"restart:{unit}", f"Перезапуск {unit}", ["systemctl", "restart", unit], timeout=120,
                             on_done=lambda j: app.invalidate("status"))
    elif action == "smoke":
        job = app.jobs.start("smoke", "Smoke-проверка", outside_sandbox(zoo_argv("smoke", "--json")), timeout=900)
    elif action == "collect":
        job = app.jobs.start("collect", "Снятие трафика", zoo_argv("traffic", "--collect", "--json"), timeout=300)
    elif action == "storage-clear":
        sid = req.form.get("section", "")
        sec = storage.section(sid)
        if sec is None or not sec.available:
            req.session.flash("bad", "Неизвестный раздел")
            return redirect("/settings")
        job = app.jobs.start("storage", f"Очистка: {sec.title}", zoo_argv("storage", "--clear", sid, "--json"),
                             timeout=600, on_done=lambda j: app.invalidate("storage"))
    elif action == "storage-limit":
        n = storage.parse_size(req.form.get("limit", ""))
        if n is None or n < storage.MIN_LIMIT:
            req.session.flash("bad", "Не понял размер или он меньше 16M. Пример: 1G, 500M")
            return redirect("/settings")
        config_set(storage.LIMIT_KEY, storage.format_size(n))
        app.invalidate("storage", "storage-alerts")
        req.session.flash("ok", f"Бюджет данных: {human_bytes(n)}")
        return redirect("/settings")
    elif action == "geo":
        if system.unit_states([GEO_UNIT]).get(GEO_UNIT, {}).get("load") != "loaded":
            req.session.flash("bad", f"Нет юнита {GEO_UNIT} (фаза 07 не выполнена?)")
            return redirect("/settings")
        job = app.jobs.start("geo", "Обновление geo-файлов", ["systemctl", "start", GEO_UNIT], timeout=600)
    else:
        req.session.flash("bad", "Неизвестное действие")
        return redirect("/settings")
    return redirect(f"/jobs/{job.id}")


# ---------- задачи ----------

# откуда запущена задача: туда ведёт «назад» и там подсвечен пункт меню
JOB_ORIGIN = {"probe": ("/probe", "← проверка"), "collect": ("/traffic", "← трафик")}


def job_badge(j: Job) -> Markup:
    if j.running:
        return badge("идёт", "info")
    return badge("готово", "ok") if j.ok else badge(f"ошибка ({j.rc})", "bad")


def command_line(argv: list[str]) -> str:
    """«python3 /opt/vpn-zoo/zoo/zoo probe …» → «zoo probe …»."""
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    if len(argv) > 1 and argv[1].replace("\\", "/").endswith("/zoo"):
        argv = ["zoo", *argv[2:]]
    return " ".join(argv)


def job_page(app: "App", req: "Request", job: str) -> "Response":
    j = app.jobs.get(int(job))
    if j is None:
        return app.error(req, 404, "Задача не найдена", "Задачи хранятся в памяти и пропадают после рестарта админки.")
    cfg = app.cfg()
    parts: list[Any] = [kv([("состояние", job_badge(j)), ("начата", fmt_time(j.started)),
                            ("длительность", f"{j.duration:.1f} с"),
                            ("команда", t("code", logs.sanitize(command_line(j.argv), cfg)))])]
    if j.error:
        parts.append(alert_list([("bad", app.safe_msg(j.error))]))
    data = None
    if not j.running and j.stdout.strip().startswith(("{", "[")):
        try:
            data = json.loads(j.stdout)
        except json.JSONDecodeError:
            data = None
    if j.kind == "smoke" and isinstance(data, dict) and "checks" in data:
        rows = [[c["name"], badge("—", "muted") if c["ok"] is None else badge("OK" if c["ok"] else "сбой",
                                                                              "ok" if c["ok"] else "bad"),
                 c["detail"]] for c in data["checks"]]
        parts += [t("h3", "Результат"), table(["проверка", "", "подробности"], rows, stack=True)]
    elif j.kind == "storage" and isinstance(data, dict) and "removed" in data:
        parts.append(kv([("раздел", data.get("section")), ("удалено записей", data["removed"]),
                         ("было → стало", f"{human_bytes(data.get('before'))} → {human_bytes(data.get('size'))}")]))
    elif j.kind == "probe" and isinstance(data, dict) and isinstance(data.get("results"), list):
        parts += [t("h3", "Результат"), results_table(data["results"])]
    out = (j.stdout if data is None else "") + j.stderr
    if out.strip() or j.running:
        parts += [t("h3", "Вывод"), t("pre", logs.sanitize(out, cfg) or "…", class_="log")]
    origin, back = JOB_ORIGIN.get(j.kind, ("/settings", "← настройки"))
    body = [page_head(j.title, "обновляется автоматически, пока идёт" if j.running else None,
                      t("a", back, href=origin, class_="btn small")), card("Задача", *parts)]
    return app.render(req, j.title, body, active=origin, refresh=2 if j.running else None)
