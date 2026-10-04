"""Страницы админки: обзор, трафик, проверка, журнал, настройки, задачи.
Пользователи — в userviews.py. Данные — те же функции zoolib, что у CLI."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import probe as probe_mod
from .. import journal, paths, status, system, traffic, upgrade
from ..output import human_bytes, human_duration
from . import charts, logs, probeviews
from .html import Markup, badge, card, csrf_input, join, kv, post_button, t, table
from .jobs import Job, outside_sandbox, zoo_argv

if TYPE_CHECKING:
    from .app import App, Request, Response

REBOOT_FLAG = Path("/var/run/reboot-required")
GEO_UNIT = "vpn-zoo-geo-update.service"


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


def alert_list(items: list[tuple[str, Any]]) -> Markup:
    icons = {"bad": "✕", "warn": "!", "ok": "✓", "info": "i"}
    return t("ul", [t("li", t("span", icons.get(k, "!"), class_="ico"), t("span", msg), class_=k)
                    for k, msg in items], class_="alerts")


def chart_block(ts: dict[str, Any], label: str, empty: str = "Нет данных за период", wide: bool = False) -> Markup:
    """График в карточке: wide — на всю ширину, иначе — в половину (свой viewBox, чтобы
    подписи осей были одного размера)."""
    if not ts["series"] or not any(sum(s["values"]) for s in ts["series"]):
        return t("p", empty, class_="muted")
    width, height = (1100, 280) if wide else (560, 260)
    return join(charts.columns(ts["buckets"], ts["step"], ts["series"], label, width, height),
                charts.legend(ts["series"]) if len(ts["series"]) > 1 else None)


def status_data(app: "App") -> dict[str, Any]:
    return app.cached("status", 5, lambda: status.collect(app.cfg(), cpu_interval=0.3))


def no_history_hint() -> Markup:
    return t("p", "Истории трафика пока нет: коллектор снимает счётчики раз в 5 минут "
                  "(zoo-collector.timer). Снять сейчас — в ", t("a", "настройках", href="/settings"), ".",
             class_="muted")


# ---------- обзор ----------

def collect_alerts(st: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for p in st["problems"]:
        out.append(("warn" if p.startswith("сертификат") else "bad", p))
    # таймер коллектора, его задержки и ошибки источников уже в problems (status.collect)
    for unit in status.ZOO_UNITS:
        s = st["services"].get(unit, {})
        if unit == "zoo-collector.timer":
            continue
        if s.get("load") == "loaded" and s.get("enabled") == "enabled" and s.get("active") != "active":
            out.append(("bad", f"{unit} — {s.get('active')}"))
    if REBOOT_FLAG.exists():
        pkgs = ""
        try:
            names = Path(str(REBOOT_FLAG) + ".pkgs").read_text(encoding="utf-8").split()
            pkgs = f" (обновлены: {', '.join(sorted(set(names))[:6])})" if names else ""
        except OSError:
            pass
        out.append(("warn", f"Нужна перезагрузка сервера{pkgs}"))
    if traffic.last_run() is None:
        out.append(("warn", "Трафик ещё не собирался: zoo-collector.timer снимает счётчики раз в 5 минут, "
                            "первое снятие — в течение 5 минут после установки"))
    out += journal.alerts()
    return out


def overview(app: "App", req: "Request") -> "Response":
    st = status_data(app)
    host = st["host"]
    today_p = traffic.today("protocol")
    today_u = traffic.today("user")
    spark = {s["key"]: s["values"] for s in traffic.timeseries("24h", "protocol", top=50)["series"]}

    alerts = collect_alerts(st) or [("ok", "Проблем не найдено")]
    mem, disk = host.get("mem") or {}, host.get("disk") or {}
    load = host.get("load")
    swap_used = (mem.get("swap_total") or 0) - (mem.get("swap_free") or 0)
    tiles = t("div",
              _tile("CPU", f"{host.get('cpu_percent', '—')} %",
                    f"ядер {host.get('cpu_count')} · load {' '.join(f'{x:.2f}' for x in load) if load else '—'}"),
              _tile("Память", f"{human_bytes(mem.get('used'))}", f"из {human_bytes(mem.get('total'))}"
                    + (f" · swap {human_bytes(swap_used)}" if mem.get("swap_total") else ""),
                    charts.meter(mem.get("used") or 0, mem.get("total") or 0)),
              _tile("Диск /", f"{human_bytes(disk.get('used'))}", f"свободно {human_bytes(disk.get('free'))}",
                    charts.meter(disk.get("used") or 0, disk.get("total") or 0, 0.85, 0.95)),
              _tile("Трафик сервера сегодня", human_bytes(today_p.get(traffic.HOST)), "приём + отдача"),
              _tile("Пользователи сегодня", human_bytes(sum(today_u.values())),
                    f"активных {sum(1 for v in today_u.values() if v)} из {st['users']['total']}"),
              _tile("Аптайм", human_duration(host.get("uptime")),
                    f"Xray {st['xray'].get('state') or '—'}" if st["xray"] else "API 3x-ui не опрашивался"),
              class_="tiles")

    cards = []
    for i, p in enumerate(st["protocols"]):
        if not p["enabled"]:
            state = badge("выключен", "muted")
        else:
            state = badge("работает", "ok") if p["ok"] else badge("сбой", "bad")
        svc = [t("span", f"{u}: {s}", class_="chip") for u, s in p["services"].items()]
        listen = [t("span", f"{p['port']}/{k}" + ("" if v else " — не слушает"), class_="chip")
                  for k, v in p["listening"].items()]
        cards.append(card(
            p["name"],
            t("div", f"{p['id']} · {p['engine'] or '—'}", class_="meta"),
            t("div", svc, listen, class_="chips"),
            t("div",
              t("div", t("div", "сегодня", class_="muted small"),
                t("div", human_bytes(today_p.get(p["id"], 0)), class_="num-big"),
                t("div", f"пользователей: {'—' if p['users'] is None else p['users']}", class_="muted small")),
              charts.sparkline(spark.get(p["id"], []), charts.series_class(i)),
              class_="row"),
            cls="proto", extra=state))
    protos = t("div", cards, class_="grid") if cards else t("p", "Манифестов протоколов нет: фазы 04–06 не "
                                                                  "выполнены?", class_="muted")

    top_users = sorted(today_u.items(), key=lambda kv_: -kv_[1])[:6]
    mx = max((v for _, v in top_users), default=0)
    users_card = card("Пользователи сегодня",
                      table(["имя", "трафик", ""],
                            [[t("a", n, href=f"/users/{n}"), human_bytes(v), charts.bar(v, mx)]
                             for n, v in top_users],
                            num=[1], empty="трафика сегодня ещё не было"),
                      extra=t("a", "все →", href="/users", class_="small"))
    certs = [[c["path"], fmt_time(_iso_ts(c["not_after"])),
              badge("не читается", "bad") if c["days_left"] is None else
              badge(f"{c['days_left']} дн.", "ok" if c["days_left"] >= status.CERT_WARN_DAYS else "warn")]
             for c in st["certs"]]
    sys_card = card("Сервер",
                    kv([("адрес", st["server"]["ip"] or "—"), ("имя", st["server"]["hostname"]),
                        ("UFW", {None: "не установлен", True: "включён", False: "ВЫКЛЮЧЕН"}[st["firewall"]["ufw_active"]]),
                        ("Xray", f"{st['xray'].get('state', '')} {st['xray'].get('version', '')}".strip() or "—"),
                        ("лишние открытые порты", ", ".join(f"{e['port']}/{e['proto']}" for e in st["exposed"]) or "нет")]),
                    t("h3", "Сертификаты", class_="sub-h") if certs else None,
                    table(["файл", "до", ""], certs) if certs else None)
    body = [page_head("Обзор", f"обновлено {datetime.now().strftime('%H:%M:%S')}",
                      t("a", "Обновить", href="/", class_="btn small")),
            alert_list(alerts), t("h2", "Хост"), tiles, t("h2", "Протоколы"), protos,
            t("h2", "Сводка"), t("div", users_card, sys_card, class_="cols")]
    return app.render(req, "Обзор", body, active="/")


def _tile(label: str, value: str, hint: str = "", extra: Any = None) -> Markup:
    return t("div", t("div", label, class_="label"), t("div", value, class_="value"), extra,
             t("div", hint, class_="hint") if hint else None, class_="tile")


def _iso_ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


# ---------- трафик ----------

def traffic_page(app: "App", req: "Request") -> "Response":
    period = get_period(req)
    rep_u = traffic.report(period=period, by="user")
    head = page_head("Трафик", f"{traffic.PERIOD_TITLES[period]}, с {fmt_time(rep_u['since'])}",
                     period_selector("/traffic", period))
    if rep_u.get("empty"):
        return app.render(req, "Трафик", [head, card("Нет данных", no_history_hint())], active="/traffic")
    rep_p = traffic.report(period=period, by="protocol")
    ts_p = traffic.timeseries(period, "protocol", top=8)
    ts_u = traffic.timeseries(period, "user", top=8)
    ts_h = traffic.timeseries(period, "host")
    host_total = sum(sum(s["values"]) for s in ts_h["series"])
    tot = rep_u["total"]
    tiles = t("div",
              _tile("Пользователи, всего", human_bytes(tot["total"]),
                    f"↑ {human_bytes(tot['up'])} от клиентов · ↓ {human_bytes(tot['down'])} к клиентам"),
              _tile("Сервер целиком", human_bytes(host_total), "приём + отдача интерфейса"),
              _tile("Активных пользователей", str(sum(1 for r in rep_u["rows"] if r["total"])),
                    traffic.PERIOD_TITLES[period]),
              class_="tiles")
    mx_u = max((r["total"] for r in rep_u["rows"]), default=0)
    mx_p = max((r["total"] for r in rep_p["rows"]), default=0)
    users_tbl = table(["пользователь", "↑ от клиента", "↓ к клиенту", "всего", ""],
                      [[t("a", r["key"], href=f"/users/{r['key']}?period={period}"), human_bytes(r["up"]),
                        human_bytes(r["down"]), human_bytes(r["total"]), charts.bar(r["total"], mx_u)]
                       for r in rep_u["rows"]], num=[1, 2, 3], empty="трафика не было")
    proto_tbl = table(["протокол", "↑ от клиентов", "↓ к клиентам", "всего", ""],
                      [[r["title"], human_bytes(r["up"]), human_bytes(r["down"]), human_bytes(r["total"]),
                        charts.bar(r["total"], mx_p, "s2")] for r in rep_p["rows"]],
                      num=[1, 2, 3], empty="трафика не было")
    host_series = [{"title": "принято", "values": s["up"]} for s in ts_h["series"]] + \
                  [{"title": "отправлено", "values": s["down"]} for s in ts_h["series"]]
    host_ts = {"buckets": ts_h["buckets"], "step": ts_h["step"], "series": host_series}
    body = [head, tiles,
            t("div", card("По протоколам", chart_block(ts_p, "Трафик по протоколам")),
              card("По пользователям", chart_block(ts_u, "Трафик по пользователям")), class_="cols"),
            t("div", card("Пользователи", users_tbl), card("Протоколы", proto_tbl), class_="cols"),
            card("Сервер целиком", chart_block(host_ts, "Трафик интерфейса сервера", wide=True),
                 t("p", "Всё, что прошло через сетевой интерфейс сервера: трафик клиентов учитывается дважды "
                        "(от клиента и в интернет), плюс обновления и служебный трафик.", class_="hint")),
            t("p", "Xray-протоколы (VLESS, XHTTP, SS-2022, TUIC) в разбивке по протоколам считаются по счётчикам "
                   "inbound 3x-ui. У пользователя их трафик общий — строка «Xray (общий счётчик)»: 3x-ui считает "
                   "клиента одним счётчиком на все его inbound.", class_="hint")]
    return app.render(req, "Трафик", body, active="/traffic")


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
        rows.append([t("span", t("strong", r.get("id")),
                       t("span", f"{r.get('port') or '?'}/{r.get('layer') or '?'}", class_="sub")),
                     verdict_badge(r.get("verdict")),
                     "—" if rtt is None else f"{rtt:.0f} мс",
                     "—" if r.get("latency_ms") is None else f"{r['latency_ms']:.0f} мс",
                     "—" if r.get("speed_mbps") is None else f"{r['speed_mbps']:.1f} Мбит/с",
                     r.get("egress_ip") or "—", t("span", why, class_="small")])
    return table(["протокол", "итог", "RTT", "задержка", "скорость", "IP выхода", "причина"], rows,
                 num=[2, 3, 4], empty="протоколов для проверки нет")


def verdict_legend(results: list[dict[str, Any]]) -> Markup | None:
    seen = sorted({r.get("verdict") for r in results if r.get("verdict") and r.get("verdict") != "OK"})
    if not seen:
        return None
    return t("ul", [t("li", verdict_badge(v), " ", probe_mod.verdicts.DESCRIPTIONS.get(v, "")) for v in seen],
             class_="legend")


def probe_page(app: "App", req: "Request", compare_rows: list[dict[str, Any]] | None = None,
               report_text: str = "", error: str = "", notice: str = "") -> "Response":
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
        local_body += [t("p", f"Последний прогон: {_gen_time(last.get('generated'))}, сервер "
                              f"{last.get('server_ip') or '?'}"
                              + (f", адрес проверки: {', '.join(targets)}" if targets else ""), class_="muted small"),
                       results_table(last["results"]), verdict_legend(last["results"])]
    elif not running:
        local_body.append(t("p", "Самопроверка ещё не запускалась.", class_="muted"))
    local = card("Самопроверка с сервера",
                 t("p", "Сервер поднимает клиента каждого протокола у себя и идёт на свой публичный IP: "
                        "работает ли протокол в принципе (1–3 минуты).", class_="hint"),
                 *local_body, extra=post_button("/probe/run", "Запустить", csrf, "btn primary small"))
    cmp_body: list[Any] = [
        t("p", "Отчёт клиентского пробника — файл ", t("code", "probe/probe-report.json"),
          " после запуска контейнера zoo-probe на машине пользователя (README, «Блокирует ли ваш провайдер»). "
          "Пакет для пробника: ", t("code", str(paths.probe_export_file())),
          " (его создаёт самопроверка; в нём ключи — передавайте по scp и удалите после).",
          class_="hint"),
        t("form", csrf_input(csrf),
          t("div", t("label", "JSON-отчёт клиента", for_="report"),
            t("textarea", report_text, name="report", id="report", spellcheck="false",
              placeholder='{"type": "zoo-probe-report", "results": [{"id": "vless-reality", "verdict": "OK"}]}'),
            class_="field"),
          t("div", t("div", t("label", "или файл", for_="report-file"),
                     t("input", type="file", id="report-file", accept=".json,application/json", data_fill="report"),
                     class_="field grow"),
            t("div", t("label", "метка (необязательно)", for_="tag"),
              t("input", type="text", name="tag", id="tag", maxlength="40", placeholder="mobile-mts"), class_="field"),
            t("div", t("label", "устройство", for_="device"),
              t("input", type="text", name="device", id="device", maxlength="40", placeholder="pixel7"),
              class_="field"),
            t("button", "Сравнить и записать в историю", type="submit", class_="btn primary"), class_="form-row"),
          method="post", action="/probe/compare", class_="stack")]
    if error:
        cmp_body.insert(0, alert_list([("bad", error)]))
    if notice:
        cmp_body.insert(0, alert_list([("info", notice)]))
    if compare_rows is not None:
        rows = [[t("strong", r["id"]), verdict_badge(r.get("server")), verdict_badge(r.get("client")),
                 badge(r.get("verdict", ""), CATEGORY.get(r.get("category", ""), "muted"))] for r in compare_rows]
        cmp_body += [t("h3", "Сравнение"), table(["протокол", "сервер", "клиент", "вывод"], rows)]
    body = [page_head("Проверка", "работает ли протокол в принципе и блокируется ли он у пользователя"),
            local, *probeviews.cards(req), card("Сравнить с клиентом", *cmp_body)]
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


# ---------- журнал ----------

def log_sources(app: "App") -> list[tuple[str, str, str]]:
    """[(группа, ключ, подпись)] — только эти источники можно открыть."""
    out = [("Установка", f"file:{f.name}", f.name) for f in logs.log_files()]
    st = status_data(app)
    units = ["zoo-web.service", "zoo-collector.service"]
    for p in st["protocols"]:
        units += [u if "." in u else u + ".service" for u in p["services"]]
    units += ["x-ui.service", GEO_UNIT]
    loaded = {u for u, s in system.unit_states(units).items() if s.get("load") == "loaded"}
    out += [("Сервисы", f"unit:{u}", u) for u in dict.fromkeys(units) if u in loaded]
    return out


def logs_page(app: "App", req: "Request") -> "Response":
    sources = log_sources(app)
    keys = {k for _, k, _ in sources}
    src = req.query.get("src") or (sources[0][1] if sources else "")
    try:
        lines = max(50, min(2000, int(req.query.get("lines", "300"))))
    except ValueError:
        lines = 300
    nav: list[Any] = []
    group = None
    for g, k, label in sources:
        if g != group:
            nav.append(t("li", g, class_="group"))
            group = g
        nav.append(t("li", t("a", label, href=f"/logs?src={k}&lines={lines}", class_="active" if k == src else None,
                             title=label)))
    if src in keys:
        kind, _, name = src.partition(":")
        raw = logs.tail_file(logs.log_dir() / name, lines) if kind == "file" else logs.journal(name, lines)
        content = t("pre", logs.sanitize(raw, app.cfg()) or "пусто", class_="log")
        title = name
    else:
        content, title = t("p", "Выберите журнал слева." if sources else "Журналов нет.", class_="muted"), ""
    sizes = t("nav", [t("a", str(n), href=f"/logs?src={src}&lines={n}", class_="active" if n == lines else None)
                      for n in (100, 300, 1000)], class_="seg", aria_label="Строк")
    body = [page_head("Журнал", "последние строки; ключи, пароли и ссылки скрыты", sizes),
            t("div", card("Источники", t("ul", nav, class_="list")),
              card(title or "Журнал", content), class_="side")]
    return app.render(req, "Журнал", body, active="/logs")


# ---------- настройки ----------

SHOW_KEYS_FIRST = ("LABEL", "SERVER_IP", "DOMAIN", "SSH_PORTS", "RU_EGRESS", "AWG_ENGINE", "HY2_ENGINE",
                   "AUTO_REBOOT", "AUTO_REBOOT_TIME", "PANEL_PORT", "ZOO_WEB_PORT", "ZOO_HOME")


def restartable_units(app: "App") -> list[str]:
    st = status_data(app)
    units = list(status.BASE_UNITS)
    for p in st["protocols"]:
        units += [u if "." in u else u + ".service" for u in p["services"]]
    units.append("zoo-collector.timer")
    loaded = {u for u, s in st["services"].items() if s.get("load") == "loaded"}
    return [u for u in dict.fromkeys(units) if u in loaded]


def settings_page(app: "App", req: "Request") -> "Response":
    cfg = app.cfg()
    csrf = req.session.csrf if req.session else ""
    st = status_data(app)
    from . import access_info
    info = access_info(cfg)
    ssh = info["ssh"]
    panel = ""
    if cfg.get("PANEL_PORT"):
        panel = (f"ssh -N -L {cfg.get('PANEL_PORT')}:127.0.0.1:{cfg.get('PANEL_PORT')} {ssh}\n"
                 f"http://127.0.0.1:{cfg.get('PANEL_PORT')}/{cfg.get('PANEL_PATH')}/")
    access = card("Доступ",
                  t("p", "Админка zoo:", class_="small muted"), t("code", f"{info['tunnel']}\n{info['url']}", class_="cmd"),
                  t("p", "Панель 3x-ui (логин и пароль — в CREDENTIALS.md):", class_="small muted") if panel else None,
                  t("code", panel, class_="cmd") if panel else None,
                  t("p", "Новый токен админки: ", t("code", "sudo zoo web --new-token"), " (все сессии закроются).",
                    class_="hint"))

    units = restartable_units(app)
    unit_rows = []
    for u in units:
        s = st["services"].get(u) or system.unit_states([u]).get(u, {})
        active = s.get("active", "")
        unit_rows.append([t("code", u), badge(active or "?", "ok" if active == "active" else "bad"),
                          s.get("enabled", ""), s.get("restarts", ""),
                          post_button("/settings/action", "Перезапустить", csrf, "btn small",
                                      {"action": "restart", "unit": u})])
    services = card("Сервисы", table(["юнит", "состояние", "автозапуск", "рестартов", ""], unit_rows))

    actions = card("Обслуживание", t("div",
        post_button("/settings/action", "Smoke-проверка", csrf, "btn", {"action": "smoke"}),
        post_button("/settings/action", "Снять трафик сейчас", csrf, "btn", {"action": "collect"}),
        post_button("/settings/action", "Обновить geo-файлы", csrf, "btn", {"action": "geo"}),
        t("a", "Сверить пользователей", href="/users?verify=1", class_="btn"),
        class_="actions"),
        t("p", "Обновление версий — из консоли: ", t("code", "sudo zoo upgrade"), " (план) и ",
          t("code", "sudo zoo upgrade --apply"), ": фазы перезапускают сервисы, в том числе эту админку.",
          class_="hint"))

    try:
        up = app.cached("upgrade", 60, lambda: upgrade.check(cfg))
        ver_rows = [[name, c["installed"] or "—", c["pinned"] or "—",
                     badge("—", "muted") if c["outdated"] is None else
                     (badge("устарел", "warn") if c["outdated"] else badge("актуален", "ok"))]
                    for name, c in up["components"].items()]
        plan = up["phases"]
        versions = card("Версии", table(["компонент", "установлен", "закреплён", ""], ver_rows),
                        t("p", ("План обновления: " + " → ".join(plan)) if plan else "Всё совпадает с versions.env.",
                          class_="hint"))
    except Exception as e:  # сводка версий не должна ломать страницу
        versions = card("Версии", alert_list([("warn", f"не удалось сравнить версии: {e}")]))

    secret_keys = {k for k in cfg.values if logs.SECRET_KEY_RE.search(k)}
    ordered = [k for k in SHOW_KEYS_FIRST if k in cfg.values] + sorted(k for k in cfg.values if k not in SHOW_KEYS_FIRST)
    cfg_rows = [[t("code", k), badge("скрыто", "muted") if k in secret_keys else t("span", cfg.get(k), class_="mono")]
                for k in ordered]
    config = card("config.env", t("p", f"{cfg.path} · только просмотр; правка — config_set или install.sh",
                                       class_="hint"), table(["ключ", "значение"], cfg_rows))
    jobs = app.jobs.recent(8)
    jobs_card = card("Последние задачи", table(["задача", "начата", "итог"], [
        [t("a", j.title, href=f"/jobs/{j.id}"), fmt_time(j.started), job_badge(j)] for j in jobs],
        empty="задач не было")) if jobs else None
    body = [page_head("Настройки", "просмотр конфигурации и обслуживание"),
            t("div", access, actions, class_="cols"), services, jobs_card,
            t("div", versions, config, class_="cols")]
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
        parts.append(alert_list([("bad", j.error)]))
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
        parts += [t("h3", "Результат"), table(["проверка", "", "подробности"], rows)]
    elif j.kind == "probe" and isinstance(data, dict) and isinstance(data.get("results"), list):
        parts += [t("h3", "Результат"), results_table(data["results"]), verdict_legend(data["results"]),
                  t("p", t("a", "К странице проверки →", href="/probe"))]
    out = (j.stdout if data is None else "") + j.stderr
    if out.strip() or j.running:
        parts += [t("h3", "Вывод"), t("pre", logs.sanitize(out, cfg) or "…", class_="log")]
    body = [page_head(j.title, "обновляется автоматически, пока идёт" if j.running else None,
                      t("a", "← настройки", href="/settings", class_="btn small")), card("Задача", *parts)]
    return app.render(req, j.title, body, active="/settings", refresh=2 if j.running else None)
