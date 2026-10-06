"""Карточки протоколов на «Обзоре»: живые метрики (D41) и вкл/выкл (D42).

Админка тут только читает базу замеров и кладёт файлы-заявки (live-req, jobs): управлять юнитами
ей нельзя, замер и install.sh запускают zoo-live-req и zoo-job по zoo-live.path и zoo-job.path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import manifests, protoctl, traffic
from ..output import human_bytes
from ..probe import live, verdicts
from . import logs
from .html import Markup, badge, card, kv, post_button, t

if TYPE_CHECKING:
    from .app import App, Request, Response

DESC = {"enable": "включается…", "disable": "выключается…"}


@dataclass
class Ctx:
    """Всё, что карточкам нужно сверх status: замеры, трафик без zoo-probe, заявки, управляемые протоколы."""
    now: float = field(default_factory=time.time)
    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    split: dict[str, tuple[int, int]] = field(default_factory=dict)
    approx: set[str] = field(default_factory=set)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    busy: dict[str, Any] | None = None
    ctls: dict[str, protoctl.Ctl] = field(default_factory=dict)


def context() -> Ctx:
    ctx = Ctx()
    ctx.live = live.summary(now=ctx.now)
    own = live.own_today(ctx.now)
    ctx.split = traffic.today_split({p: b for p, (b, _) in own.items()}, ctx.now,
                                    {p: b + n * live.LIGHT_BYTES for p, (b, n) in own.items()})
    ctx.approx = {m.id for m in manifests.enabled() if m.users_backend == "xui"}
    ctx.jobs = protoctl.active_by_proto()
    ctx.busy = protoctl.active()
    ctx.ctls = protoctl.controls()
    return ctx


# ---------- формат ----------

def _bytes(n: int) -> str:
    return human_bytes(n).replace(".0 ", " ")


def approx(ctx: Ctx, proto: str) -> str:
    """«≈» у Xray-протоколов: 3x-ui считает клиента одним счётчиком на все inbound, доля замеров делится по весам."""
    return "≈" if proto in ctx.approx else ""


def today_bytes(ctx: Ctx, proto: str) -> int:
    up, down = ctx.split.get(proto, (0, 0))
    return up + down


def metrics(proto: str, ctx: Ctx) -> Markup:
    """«23 мс · ±4 · 38 Мбит/с · ↓1.2 ГБ ↑90 МБ»; точка — свежесть и итог последнего замера."""
    d = ctx.live.get(proto)
    parts: list[str] = []
    dot, title = "muted", "замера ещё не было"
    if d is None:
        parts.append("нет замера")
    else:
        stale = ctx.now - d["ts"] > live.FRESH
        age = live.ago(d["ts"], ctx.now)
        if d["verdict"] in verdicts.NOT_TESTED:
            parts.append("не проверяется")
            title = f"{verdicts.DESCRIPTIONS.get(d['verdict'], d['verdict'])} · {age}"
        elif not d["ok"]:
            parts.append(f"сбой: {d['verdict']}")
            dot, title = "bad", f"{verdicts.DESCRIPTIONS.get(d['verdict'], d['verdict'])} · {age}"
        else:
            parts += live.parts(d, ctx.now)
            if not parts:
                parts.append("нет замера")
            dot = "muted" if stale else ("ok" if d["verdict"] == verdicts.OK else "warn")
            note = live.speed_note(d, ctx.now)
            title = " · ".join(x for x in (f"замер {age}", note,
                                           verdicts.DESCRIPTIONS.get(d["verdict"], "") if d["verdict"] != verdicts.OK else "") if x)
        if stale and dot != "bad":
            dot = "muted"
            title += " · устарел"
    if ctx.split:
        up, down = ctx.split.get(proto, (0, 0))
        parts.append(f"{approx(ctx, proto)}↓{_bytes(down)} ↑{_bytes(up)}")
    # куски не рвутся посередине: перенос — только на « · »
    chunks = [[" · ", t("span", p, class_="nw")] if i else t("span", p, class_="nw") for i, p in enumerate(parts)]
    return t("span", t("span", class_=f"dot {dot}"), t("span", chunks), class_="mline", title=title)


def caption(ctx: Ctx) -> Markup:
    if ctx.live:
        last = max(d["ts"] for d in ctx.live.values())
        stale = ctx.now - last > 3 * 600
        return t("p", f"метрики раз в 10 мин · обновлено {live.ago(last, ctx.now)}",
                 t("span", " · замеры не идут: systemctl status zoo-live.timer", class_="stale") if stale else None,
                 class_="quiet")
    return t("p", "метрики раз в 10 мин · первый замер — через пару минут после установки", class_="quiet")


# ---------- кнопки ----------

def refresh_button(proto: str, ctx: Ctx, csrf: str) -> Markup:
    if live.is_pending(proto):
        return t("span", "меряю…", class_="chip info")
    return post_button(f"/live/{proto}", "↻", csrf, "btn small icon", title="Замерить сейчас", swap=True)


def head_badge(p: dict[str, Any], ctx: Ctx) -> Markup:
    j = ctx.jobs.get(p["id"])
    if j:
        return badge(DESC[j["action"]], "info")
    return badge("работает", "ok") if p["ok"] else badge("сбой", "bad")


def toggle(proto: str, name: str, ctx: Ctx, csrf: str) -> Markup | None:
    """«Выключить» / «Включить» с окном подтверждения; нет — если протоколом нельзя управлять отсюда
    или идёт задача."""
    ctl = ctx.ctls.get(proto)
    if ctl is None or ctx.busy:
        return None
    action = "disable" if ctl.enabled else "enable"
    try:
        protoctl.check(proto, action, ctx.ctls)
    except protoctl.JobError as e:
        return t("span", "последний включённый", class_="muted small", title=str(e)) if action == "disable" else None
    if action == "disable":
        return post_button(f"/protocols/{proto}/disable", "Выключить", csrf, "btn small", swap=True,
                           confirm=f"Выключить {name}? Клиенты по нему перестанут подключаться.")
    return post_button(f"/protocols/{proto}/enable", "Включить", csrf, "btn small", swap=True,
                       confirm=f"Включить {name}? Протокол запустится, это может занять несколько минут.")


def metrics_row(p: dict[str, Any], ctx: Ctx, csrf: str) -> Markup:
    """Строка метрик карточки и ↻ справа (пока идёт вкл/выкл — без кнопки)."""
    return t("div", metrics(p["id"], ctx), None if ctx.jobs.get(p["id"]) else refresh_button(p["id"], ctx, csrf),
             class_="metrics")


def switch_row(p: dict[str, Any], ctx: Ctx, csrf: str) -> Markup | None:
    sw = toggle(p["id"], p["name"].partition(" (")[0], ctx, csrf)
    return t("div", sw, class_="pctl") if sw else None


def off_block(off: list[dict[str, Any]], ctx: Ctx, csrf: str) -> Markup | None:
    """Выключенные протоколы (по манифестам) и ещё не установленные, которые умеет ставить install.sh."""
    rows = [(p["id"], p["name"].partition(" (")[0]) for p in off]
    seen = {pid for pid, _ in rows}
    for pid, ctl in ctx.ctls.items():
        if pid not in seen and not ctl.enabled:
            rows.append((pid, ctl.name))
    items = []
    for pid, name in rows:
        j = ctx.jobs.get(pid)
        sw = badge(DESC[j["action"]], "info") if j else toggle(pid, name, ctx, csrf)
        items.append(t("div", t("span", name, class_="off-name"), sw, class_="off-row"))
    if not items:
        return None
    return t("section", t("h3", "Выключены"), t("div", items, class_="off-list"), class_="off")


# ---------- тревоги и страница задачи ----------

def alerts() -> list[tuple[Any, ...]]:
    """Идущая задача — «info», неудавшаяся последняя за сутки — «bad» со ссылкой на журнал."""
    out: list[tuple[Any, ...]] = []
    cur = protoctl.active()
    if cur:
        out.append(("info", f"{cur['verb'].capitalize()} {cur['name']}… это может занять несколько минут",
                    t("a", "ход", href=f"/pjobs/{cur['id']}")))
        return out
    last = next(iter(protoctl.states()), None)
    if last and last["status"] == "fail" and time.time() - (last["finished"] or 0) < 86400:
        out.append(("bad", f"{last['verb'].capitalize()} {last['name']} не удалось",
                    t("a", "журнал", href=f"/pjobs/{last['id']}")))
    return out


def note_finished(app: "App", ctx: Ctx) -> None:
    """Задача закончилась — статус протоколов в кэше устарел."""
    done = {s["id"] for s in protoctl.states() if s["status"] in ("ok", "fail")}
    if app.seen_jobs is None:
        app.seen_jobs = done
    elif done - app.seen_jobs:
        app.seen_jobs |= done
        app.invalidate("status")


def job_page(app: "App", req: "Request", jid: str) -> "Response":
    j = protoctl.get_state(jid)
    if j is None:
        return app.error(req, 404, "Задача не найдена", "Такой задачи нет или она давно удалена.")
    running = j["status"] in ("queued", "running")
    shown = {"queued": badge("в очереди", "info"), "running": badge("идёт", "info"),
             "ok": badge("готово", "ok"), "fail": badge("ошибка", "bad")}[j["status"]]
    rows: list[tuple[Any, Any]] = [("состояние", shown)]
    if j["started"]:
        rows.append(("начата", time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(j["started"]))))
        rows.append(("длительность", f"{int((j['finished'] or time.time()) - j['started'])} с"))
    parts: list[Any] = [kv(rows)]
    if j["error"]:
        parts.append(t("p", app.safe_msg(j["error"]), class_="muted"))
    text = logs.sanitize(protoctl.log_tail(jid), app.cfg())
    if text.strip() or running:
        parts += [t("h3", "Вывод"), t("pre", text or "…", class_="log")]
    title = f"{j['verb'].capitalize()}: {j['name']}"
    body = [t("div", t("div", t("h1", title), t("div", "обновляется, пока идёт", class_="sub") if running else None),
              t("div", t("a", "← обзор", href="/", class_="btn small"), class_="actions"), class_="page-head"),
            card("Задача", *parts)]
    return app.render(req, title, body, active="/", refresh=3 if running else None)


# ---------- обработчики POST ----------

def proto_toggle(app: "App", req: "Request", proto: str, action: str) -> "Response":
    from .app import redirect
    try:
        jid = protoctl.submit(proto, action)
    except protoctl.JobError as e:
        req.session.flash("bad", str(e))
        return redirect("/")
    j = protoctl.get_state(jid) or {}
    req.session.flash("ok", f"{str(j.get('verb', '')).capitalize()} {j.get('name', proto)}: запущено, карточка обновится сама",
                      [("ход", f"/pjobs/{jid}")])
    app.invalidate("status")
    return redirect("/")


def live_request(app: "App", req: "Request", proto: str) -> "Response":
    from .app import redirect
    if proto not in {m.id for m in manifests.enabled()}:
        req.session.flash("bad", "Такого включённого протокола нет")
        return redirect("/")
    ok, why = live.request(proto)
    req.session.flash("ok" if ok else "warn", why[:1].upper() + why[1:] + (": результат появится на карточке" if ok else ""))
    return redirect("/")
