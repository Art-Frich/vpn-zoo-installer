"""Страница «Логи»: просмотр с прокруткой назад по всему логу, поиск (по одному логу и по всем), очистка.

Читает `zoolib/logread.py` (куски по токенам, лимиты), чистит `zoolib/logctl.py`. Всё, что уходит в
браузер или в выгрузку — страница, найденные строки, JSON подгрузки, файл — проходит `logs.sanitize`.
Всё состояние страницы — в query-строке; без JS всё работает обычными ссылками и формами.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from .. import logctl, logread, system
from ..output import human_bytes
from . import logs, views
from .html import Markup, card, csrf_input, empty, join, t, table

if TYPE_CHECKING:
    from .app import App, Request, Response

LINES = (100, 300, 1000)
DEFAULT_LINES = 300
PERIODS = {"all": ("за всё время", None), "1h": ("за час", 3600), "24h": ("за сутки", 86400),
           "7d": ("за 7 дней", 7 * 86400)}
SRC_RE = re.compile(r"^(file|unit):[A-Za-z0-9@:._-]{1,120}\Z")
TOK_RE = re.compile(r"(?:\d{1,15}|" + logread.CURSOR_RE.pattern[1:-2] + r")\Z")
DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d")
STOP_NOTE = {"time": "поиск остановлен по времени", "entries": "просмотрено максимум строк",
             "bytes": "просмотрено максимум данных"}
UNIT_UNLOADED = "Нет юнита zoo-logs.path: обновите установку (install.sh --phase 09)."


# ---------- состояние страницы ----------

def _state(req: "Request") -> dict[str, Any]:
    q = req.query

    def g(name: str, limit: int = 200) -> str:
        v = q.get(name, "")
        return v[:limit] if isinstance(v, str) else ""

    try:
        lines = max(50, min(logread.CHUNK_MAX, int(g("lines", 6))))
    except ValueError:
        lines = DEFAULT_LINES
    try:
        lim = int(g("lim", 5))
    except ValueError:
        lim = logread.HITS[0]
    tok = {k: (g(k) if TOK_RE.match(g(k)) else "") for k in ("before", "after", "at")}
    per = g("per", 4)
    return {"src": g("src", 130), "lines": lines, "q": " ".join(g("q", 400).split())[:logread.QUERY_MAX],
            "rx": g("rx", 1) == "1", "cs": g("cs", 1) == "1", "scope": "all" if g("in", 4) == "all" else "one",
            "per": per if per in PERIODS else "all", "since": g("since", 20).strip(), "until": g("until", 20).strip(),
            "lim": lim if lim in logread.HITS else logread.HITS[0], **tok}


def _url(st: dict[str, Any], path: str = "/logs", **over: Any) -> str:
    d = {**st, **over}
    pairs: list[tuple[str, str]] = []
    if d["src"]:
        pairs.append(("src", d["src"]))
    if d["lines"] != DEFAULT_LINES:
        pairs.append(("lines", str(d["lines"])))
    if d["q"]:
        pairs.append(("q", d["q"]))
        pairs += [(k, "1") for k in ("rx", "cs") if d[k]]
        if d["scope"] == "all":
            pairs.append(("in", "all"))
        if d["per"] != "all":
            pairs.append(("per", d["per"]))
        pairs += [(k, d[k]) for k in ("since", "until") if d[k]]
        if d["lim"] != logread.HITS[0]:
            pairs.append(("lim", str(d["lim"])))
    pairs += [(k, d[k]) for k in ("at", "before", "after") if d.get(k)]
    return path + ("?" + urlencode(pairs) if pairs else "")


def _parse_dt(text: str, end: bool = False) -> float | None:
    for fmt in DT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.timestamp() + (86399 if end and fmt == "%Y-%m-%d" else 0)
    raise logread.LogError(f"не понял дату «{text[:20]}»: нужна запись вида 2026-10-07 14:30")


def _period(st: dict[str, Any], now: float) -> tuple[float | None, float | None]:
    since = until = None
    secs = PERIODS[st["per"]][1]
    if secs:
        since = now - secs
    if st["since"]:
        since = _parse_dt(st["since"])
    if st["until"]:
        until = _parse_dt(st["until"], True)
    if since is not None and until is not None and since > until:
        raise logread.LogError("начало периода позже конца")
    return since, until


def _split(key: str) -> tuple[str, str]:
    kind, _, name = key.partition(":")
    return kind, name


# ---------- поиск ----------

def _highlight(text: str, spans: list[tuple[int, int]]) -> Markup:
    out: list[Any] = []
    pos = 0
    for a, b in sorted(spans):
        if a < pos:
            continue
        out += [text[pos:a], t("mark", text[a:b])]
        pos = b
    return join(out, text[pos:])


def run_search(app: "App", st: dict[str, Any], sources: list[tuple[str, str, str]]) -> logread.Result:
    """Поиск по состоянию страницы; LogError — запрос не принят (текст для человека)."""
    q = logread.compile_query(st["q"], st["rx"], not st["cs"])
    since, until = _period(st, time.time())
    keys = {k for _, k, _ in sources}
    if st["scope"] == "all":
        files = logread.log_files()
        units = [name for _, k, name in sources if k.startswith("unit:")]
    elif st["src"] in keys:
        kind, name = _split(st["src"])
        files = [logread.file_path(name)] if kind == "file" else []
        units = [name] if kind == "unit" else []
    else:
        raise logread.LogError("Выберите лог слева или ищите во всех логах.")
    return logread.search(q, logs.cleaner(app.cfg()), files=files, units=units, since=since, until=until,
                          limit=st["lim"])


def known_source(key: str) -> bool:
    """Источник из списка страницы, но без опроса systemd (для подгрузки и выгрузки): лог установки или сервис зоопарка."""
    kind, name = _split(key)
    if key == logread.JOURNAL_SRC:
        return True
    if not SRC_RE.match(key):
        return False
    if kind == "file":
        return any(f.name == name for f in logread.log_files())
    units = {"zoo-web.service", "zoo-collector.service", *views.service_units(), views.GEO_UNIT}
    return name in units


def _label(key: str) -> str:
    return "журнал" if key == logread.JOURNAL_SRC else _split(key)[1]


def _hit_time(h: logread.Hit) -> Any:
    if not h.ts:
        return "—"
    s = datetime.fromtimestamp(h.ts).strftime("%d.%m %H:%M:%S")
    return t("span", s, title="время файла: у строк установки своего времени нет") if h.file_time else s


def _results_card(st: dict[str, Any], res: logread.Result) -> Markup:
    rows = [[_label(h.src), _hit_time(h), t("span", _highlight(h.text, h.spans), class_="hitcell"),
             t("a", "открыть", href=_url(st, src=h.src, at=h.tok, before="", after="", q="") + "#hit",
               class_="btn small", data_swap=True)] for h in res.hits]
    notes: list[Any] = []
    if res.stopped in STOP_NOTE:
        notes.append(t("p", f"{STOP_NOTE[res.stopped].capitalize()} (просмотрено {res.scanned}): "
                            "сузьте период или уточните запрос.", class_="hint err"))
    if res.more:
        nxt = next((n for n in logread.HITS if n > st["lim"]), None)
        notes.append(t("p", f"Показаны первые {len(res.hits)}. ",
                       t("a", f"показать до {nxt}", href=_url(st, lim=nxt), data_swap=True) if nxt else
                       "Сузьте период или уточните запрос.", class_="hint"))
    notes += [t("p", e, class_="hint err") for e in res.errors]
    head = f"Найдено: {len(res.hits)}{'+' if res.more else ''}"
    body = table(["лог", "время", "строка", ""], rows, empty="Ничего не нашлось", stack=True) if rows else \
        empty("Ничего не нашлось", f"просмотрено строк: {res.scanned}")
    return card(head, body, *notes, id_="results", help="Найденное показано с маскировкой секретов. «Открыть» — тот же лог "
                "на этом месте, прокручивается вверх и вниз.", extra=t("a", "скачать", href=_url(st, "/logs/export"),
                                                                       class_="btn small", title="найденное в файл"))


def _search_form(st: dict[str, Any]) -> Markup:
    hidden = [t("input", type="hidden", name=k, value=st[k]) for k in ("src",) if st[k]]
    hidden += [t("input", type="hidden", name="lines", value=st["lines"])] if st["lines"] != DEFAULT_LINES else []
    pers = [t("option", lbl, value=k, selected=st["per"] == k) for k, (lbl, _) in PERIODS.items()]
    scopes = [t("option", "в этом логе", value="one", selected=st["scope"] == "one"),
              t("option", "во всех логах", value="all", selected=st["scope"] == "all")]
    exact = t("details", t("summary", "точный период"),
              t("input", type="text", name="since", value=st["since"], placeholder="с: 2026-10-07 14:30", size="20",
                maxlength=20, aria_label="Начало периода"),
              t("input", type="text", name="until", value=st["until"], placeholder="по: 2026-10-08", size="20",
                maxlength=20, aria_label="Конец периода"),
              open=bool(st["since"] or st["until"]))
    reset = t("a", "сбросить", href=_url(st, q="", since="", until="", at="", before="", after=""),
              class_="btn small", data_swap=True) if st["q"] else None
    return t("form", hidden,
             t("input", type="search", name="q", value=st["q"], placeholder="поиск по логам", maxlength=logread.QUERY_MAX,
               autocomplete="off", aria_label="Поиск по логам"),
             t("select", scopes, name="in", aria_label="Где искать"),
             t("select", pers, name="per", aria_label="Период"),
             t("label", t("input", type="checkbox", name="rx", value="1", checked=st["rx"]), "регулярка",
               title="Выражение Python re; вложенные повторы и больше двух неограниченных («.*», «+») отклоняются"),
             t("label", t("input", type="checkbox", name="cs", value="1", checked=st["cs"]), "регистр",
               title="Учитывать регистр букв"),
             t("button", "Найти", type="submit", class_="btn small primary"), reset, exact,
             method="get", action="/logs", class_="search", data_get=True)


# ---------- просмотр ----------

def _view_card(app: "App", st: dict[str, Any], kind: str, name: str, chunk: logread.Chunk) -> Markup:
    tail = not (st["before"] or st["after"] or st["at"])
    clean = logs.sanitize("\n".join(line.text for line in chunk.lines), app.cfg())
    text: Any = clean
    if st["at"]:  # строку, на которую пришли из поиска, подсвечиваем и прокручиваем к ней
        parts = clean.split("\n")
        idx = next((i for i, line in enumerate(chunk.lines) if line.tok == st["at"]), -1)
        if idx >= 0 and len(parts) == len(chunk.lines):
            text = join("\n".join(parts[:idx]) + ("\n" if idx else ""), t("mark", parts[idx], id="hit", class_="hitline"),
                        ("\n" + "\n".join(parts[idx + 1:])) if idx + 1 < len(parts) else "")
    pre = t("pre", text, class_="log", id="logtext", data_src=st["src"], data_lines=st["lines"],
            data_older=chunk.older, data_tip=chunk.tip or None, data_tail=True if tail else None)
    bar: list[Any] = []
    if chunk.older:
        base = _url(st, before="", after="", at="") + ("&" if "?" in _url(st, before="", after="", at="") else "?") + "before="
        bar.append(t("p", t("a", "↑ показать раньше", href=base + quote(chunk.older, safe=""), class_="btn small",
                            data_older=True, data_base=base), class_="logbar"))
    below: list[Any] = []
    if not tail:
        links = []
        if chunk.newer:
            links.append(t("a", "↓ показать позже", href=_url(st, after=chunk.newer, before="", at=""),
                           class_="btn small", data_swap=True))
        links.append(t("a", "к концу лога", href=_url(st, before="", after="", at=""), class_="btn small",
                       data_swap=True))
        below.append(t("p", links, class_="logbar"))
    export = t("a", "скачать", href=_url(st, "/logs/export", q="", before="", after="", at=""), class_="btn small",
               title="конец лога в файл (до 20 000 строк), секреты скрыты")
    return card(name, bar, pre, below, extra=export,
                help="Весь доступный лог: ↑ или прокрутка вверх подгружает предыдущие строки. Ключи, пароли и ссылки скрыты.")


# ---------- очистка ----------

def _rules() -> list[Any]:
    out = []
    for action, (_flag, values, _lbl) in logctl.ACTIONS.items():
        for v in values:
            out.append(t("option", logctl.label(action, v), value=f"{action}:{v}", selected=(action, v) == ("vacuum-time", "30d")))
    return out


def _fmt_dt(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "—"


def _vacuum_state() -> Any:
    pend = logctl.pending()
    if pend:
        return t("p", "Очистка журнала ждёт выполнения: " + logctl.label(str(pend[0]["action"]), str(pend[0]["value"])) +
                 ". Обычно это секунды.", class_="hint")
    last = next(iter(logctl.states(1)), None)
    if not last:
        return None
    what = logctl.label(str(last.get("action")), str(last.get("value")))
    if last.get("status") == "ok":
        return t("p", f"Последняя очистка ({_fmt_dt(last.get('finished'))}, {what}): освобождено "
                      f"{human_bytes(int(last.get('freed') or 0))}.", class_="hint")
    return t("p", f"Последняя очистка не удалась: {last.get('error') or 'ошибка'}", class_="hint err")


def _clean_card(req: "Request", st: dict[str, Any]) -> Markup:
    csrf = csrf_input(req.session.csrf if req.session else "")
    files = logctl.install_logs()
    jsize = logctl.journald_size()
    keep = files[-1].name if files else ""
    items = []
    for f in reversed(files[-30:]):
        try:
            stt = f.stat()
        except OSError:
            continue
        lbl = f"{f.name} · {human_bytes(stt.st_size)} · {_fmt_dt(stt.st_mtime)}"
        items.append(t("li", t("label", t("input", type="checkbox", name="names", value=f.name, disabled=f.name == keep),
                               lbl, title="самый свежий лог не удаляется: возможно, идёт установка" if f.name == keep else None)))
    back = t("input", type="hidden", name="src", value=st["src"])
    days = [t("option", f"старше {n} дн.", value=n, selected=n == 30) for n in logctl.OLDER_DAYS]
    sel = t("form", csrf, back, t("input", type="hidden", name="mode", value="selected"), t("ul", items),
            t("button", "Удалить выбранные", type="submit", class_="btn small danger"),
            method="post", action="/logs/clean", data_confirm="Удалить выбранные логи установки насовсем?",
            data_swap=True) if items else None
    old = t("form", csrf, back, t("input", type="hidden", name="mode", value="older"), t("select", days, name="days",
                                                                                          aria_label="Срок"),
            t("button", "Удалить старые", type="submit", class_="btn small danger"),
            method="post", action="/logs/clean", data_confirm="Удалить логи установки старше выбранного срока? "
            "Самый свежий останется.", data_swap=True) if len(files) > 1 else None
    vac = t("form", csrf, back, t("select", _rules(), name="rule", aria_label="Правило очистки журнала"),
            t("button", "Очистить журнал", type="submit", class_="btn small danger"),
            method="post", action="/logs/vacuum", data_swap=True,
            data_confirm="Очистить журнал systemd по выбранному правилу? Удалённые записи не вернуть. "
            "Выполнится в фоне, обычно за секунды.")
    return card("Очистка",
                t("div", t("p", f"Логи установки: {len(files)} шт., {human_bytes(sum(_size(f) for f in files))}",
                           class_="hint"), sel, old,
                  t("p", f"Журнал systemd занимает {human_bytes(jsize)}", class_="hint"), vac, _vacuum_state(),
                  class_="logclean"),
                help="Логи установки админка удаляет сама (самый свежий остаётся). Журнал systemd чистит отдельная "
                     "служба по заявке: песочница админки в него не пишет. Разрешены только готовые правила из списка.")


def _size(f: Any) -> int:
    try:
        return f.stat().st_size
    except OSError:
        return 0


# ---------- маршруты ----------

def logs_page(app: "App", req: "Request") -> "Response":
    sources, states = views.log_sources(app)
    keys = {k for _, k, _ in sources}
    st = _state(req)
    if not st["src"]:
        failed = [k for g, k, label in sources if g == "Сервисы" and states.get(label, {}).get("active") == "failed"]
        st["src"] = failed[0] if failed else sources[0][1] if sources else ""
    nav: list[Any] = []
    group = None
    for g, k, label in sources:
        if g != group:
            nav.append(t("li", g, class_="group"))
            group = g
        nav.append(t("li", t("a", label, href=_url(st, src=k, before="", after="", at=""),
                             class_="active" if k == st["src"] else None, title=label, data_swap=True)))
    problems: list[tuple[str, Any]] = []
    results = None
    if st["q"]:
        try:
            res = run_search(app, st, sources)
            results = t("div", _results_card(st, res), data_expanded=True)  # live не перезапускает тяжёлый поиск
        except logread.LogError as e:
            problems.append(("bad", str(e)))
    content: Any
    title = ""
    if st["src"] in keys or st["src"] == logread.JOURNAL_SRC:
        kind, name = _split(st["src"])
        title = name = _label(st["src"])
        try:
            chunk = logread.view(kind, _split(st["src"])[1], before=st["before"], after=st["after"], at=st["at"],
                                 n=st["lines"])
            content = _view_card(app, st, kind, name, chunk)
        except logread.LogError as e:
            problems.append(("bad", str(e)))
            content = card(name, empty("Не прочитать"))
    else:
        content = card("Лог", empty("Выберите лог слева" if sources else "Логов нет"))
    sizes = t("nav", [t("a", str(n), href=_url(st, lines=n, before="", after="", at=""),
                        class_="active" if n == st["lines"] else None) for n in LINES], class_="seg", aria_label="Строк")
    search = card("Поиск", _search_form(st), help="Подстрока или регулярное выражение, по этому логу или по всем "
                  "(логи установки и сервисы). Секреты в результатах скрыты.")
    body = [views.page_head("Логи", None, sizes),
            t("div", card("Источники", t("ul", nav, class_="list")),
              t("div", views.alert_list(problems) if problems else None, search, results, content, _clean_card(req, st),
                class_="col-stack"), class_="side")]
    return app.render(req, "Логи", body, active="/logs")


def logs_chunk(app: "App", req: "Request") -> "Response":
    """JSON для прокрутки: {text, older, tip, newer}. Только источники из списка страницы."""
    from .app import Response
    st = _state(req)
    if not known_source(st["src"]) or not (st["before"] or st["after"]):
        return Response(400, json.dumps({"error": "нет такого источника"}).encode(), "application/json")
    kind, name = _split(st["src"])
    try:
        ch = logread.view(kind, name, before=st["before"], after=st["after"], n=st["lines"])
    except logread.LogError as e:
        return Response(200, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
    body = {"text": logs.sanitize("\n".join(line.text for line in ch.lines), app.cfg()), "older": ch.older,
            "newer": ch.newer, "tip": ch.tip, "n": len(ch.lines)}
    return Response(200, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def _attachment(name: str, body: str) -> "Response":
    from .app import Response
    return Response(200, body.encode("utf-8"), "text/plain; charset=utf-8",
                    headers=[("Content-Disposition", f'attachment; filename="{name}"')])


def logs_export(app: "App", req: "Request") -> "Response":
    """Выгрузка: найденное (если задан q) или конец выбранного лога. Секреты скрыты и здесь."""
    from .app import text
    st = _state(req)
    cfg = app.cfg()
    if st["q"]:
        try:
            res = run_search(app, st, views.log_sources(app)[0])
        except logread.LogError as e:
            return text(str(e), 400)
        lines = [f"{h.src}\t{logread.fmt_ts(h.ts)}\t{h.text}" for h in res.hits]
        return _attachment("logs-search.txt", logs.sanitize("\n".join(lines), cfg) + "\n")
    if not known_source(st["src"]):
        return text("нет такого источника", 404)
    kind, name = _split(st["src"])
    try:
        lines = logread.export(kind, name)
    except logread.LogError as e:
        return text(str(e), 400)
    fname = "journal" if kind == "journal" else re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return _attachment(fname if fname.endswith(".log") else fname + ".log",
                       logs.sanitize("\n".join(line.text for line in lines), cfg) + "\n")


def _back(req: "Request") -> str:
    src = req.form.get("src", "")
    return "/logs?src=" + quote(src, safe=":") if SRC_RE.match(src) else "/logs"


def logs_clean(app: "App", req: "Request") -> "Response":
    from .app import redirect
    mode = req.form.get("mode", "")
    try:
        if mode == "selected":
            names = req.multi.get("names", [])[:100]
            if not names:
                req.session.flash("warn", "Ничего не выбрано")
                return redirect(_back(req))
            res = logctl.clean_files(names=names)
        elif mode == "older":
            res = logctl.clean_files(older_days=int(req.form.get("days", "")))
        else:
            req.session.flash("bad", "Неизвестное действие")
            return redirect(_back(req))
    except (logctl.CleanError, ValueError):
        req.session.flash("bad", "Такого срока нет")
        return redirect(_back(req))
    app.invalidate("storage", "storage-alerts")
    req.session.flash("ok" if res["deleted"] else "info",
                      f"Удалено логов установки: {len(res['deleted'])}, освобождено {human_bytes(res['freed'])}"
                      + (f"; оставлено как свежее: {len(res['skipped'])}" if res["skipped"] else ""))
    for e in res["errors"][:3]:
        req.session.flash("warn", e)
    return redirect(_back(req))


def logs_vacuum(app: "App", req: "Request") -> "Response":
    from .app import redirect
    action, _, value = req.form.get("rule", "").partition(":")
    try:
        logctl.check(action, value)
    except logctl.CleanError as e:
        req.session.flash("bad", str(e))
        return redirect(_back(req))
    if system.unit_states(["zoo-logs.path"]).get("zoo-logs.path", {}).get("load") != "loaded":
        req.session.flash("bad", UNIT_UNLOADED)
        return redirect(_back(req))
    try:
        logctl.submit(action, value)
    except logctl.CleanError as e:
        req.session.flash("warn", str(e))
        return redirect(_back(req))
    app.invalidate("storage")
    req.session.flash("info", f"Заявка принята: {logctl.label(action, value)}. Выполняется в фоне, итог появится в «Очистке».")
    return redirect(_back(req))
