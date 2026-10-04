"""Отчёт пробы: таблица в терминал, Markdown, сравнение «сервер ↔ клиент»."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import output
from . import verdicts

VERDICT_COLOR = {"OK": "green", "SLOW": "yellow", "SKIPPED": "dim", "CLIENT_ERROR": "yellow"}
CATEGORY_TEXT = {"works": "работает", "blocked": "блокируется у вас", "server": "сломан на сервере",
                 "unknown": "нет данных"}
TARGET_TEXT = {"public": "публичный IP", "loopback": "loopback"}


def summary(results: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(r["verdict"] for r in results))


def _num(v: Any, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def _verdict(v: str) -> str:
    return output.color(v, VERDICT_COLOR.get(v, "red"))


def compare(local: dict[str, Any] | None, remote: dict[str, Any]) -> list[dict[str, Any]]:
    """Вердикт по протоколам: локальный прогон (сервер) против удалённого (клиент).
    local может быть отчётом `zoo probe --local` или словарём {id: вердикт}."""
    if local is None:
        lv: dict[str, str] = {}
    elif "results" in local:
        lv = {r["id"]: r["verdict"] for r in local["results"]}
    else:
        lv = dict(local)
    rv = {r["id"]: r["verdict"] for r in remote.get("results", [])}
    rows = []
    for pid in sorted(set(lv) | set(rv)):
        cat, text = verdicts.compare_one(lv.get(pid), rv.get(pid))
        rows.append({"id": pid, "server": lv.get(pid), "client": rv.get(pid), "category": cat, "verdict": text})
    return rows


# ---------- терминал ----------

def render(report: dict[str, Any]) -> None:
    local = report.get("mode") == "local"
    head = "Самопроверка с сервера" if local else "Проба с этой машины"
    print(f"{head}: сервер {report.get('server_ip') or '?'}, пользователь {report.get('user') or '?'}"
          + (f", ваш IP {report['direct_ip']}" if report.get("direct_ip") else ""))
    print()
    rows = []
    for r in report.get("results", []):
        port = f"{r.get('port') or '?'}/{r.get('layer') or '?'}"
        row = [r["id"], port, _verdict(r["verdict"]), _num(r.get("l4", {}).get("rtt_ms"), ".0f"),
               _num(r.get("latency_ms"), ".0f"), _num(r.get("speed_mbps"), ".1f"), r.get("egress_ip") or "—"]
        if local:
            row.insert(2, TARGET_TEXT.get(r.get("target"), r.get("target") or ""))
        rows.append(row)
    headers = ["протокол", "порт", "итог", "RTT, мс", "задержка, мс", "Мбит/с", "IP выхода"]
    if local:
        headers.insert(2, "куда")
    if rows:
        print(output.table(rows, headers, right=(4, 5, 6) if local else (3, 4, 5)))
    else:
        print("Протоколов для проверки нет.")
    details = [r for r in report.get("results", []) if r.get("reason") or r.get("notes")]
    if details:
        print()
        for r in details:
            parts = [r["reason"]] if r.get("reason") else []
            parts += r.get("notes") or []
            print(f"  {r['id']}: " + "; ".join(parts))
    seen = sorted({r["verdict"] for r in report.get("results", []) if r["verdict"] != "OK"})
    if seen:
        print()
        for v in seen:
            print(f"  {v} — {verdicts.DESCRIPTIONS.get(v, '')}")
    if report.get("compare"):
        print()
        render_compare(report["compare"])


def render_compare(rows: list[dict[str, Any]]) -> None:
    print("Сравнение с самопроверкой сервера:")
    table = [[r["id"], r["server"] or "—", r["client"] or "—", r["verdict"]] for r in rows]
    print(output.table(table, ["протокол", "сервер", "у вас", "вывод"]))


# ---------- Markdown ----------

def _md(s: Any) -> str:
    return str(s if s is not None else "—").replace("|", "\\|").replace("\n", " ")


def markdown(report: dict[str, Any]) -> str:
    local = report.get("mode") == "local"
    lines = [f"# Проба VPN-зоопарка — {'сервер' if local else 'клиент'}", "",
             f"- время: {report.get('generated')}",
             f"- сервер: {report.get('server_ip') or '?'}" + (f" ({report['label']})" if report.get("label") else ""),
             f"- пользователь: {report.get('user') or '?'}"]
    if report.get("direct_ip"):
        lines.append(f"- ваш IP (без VPN): {report['direct_ip']}")
    lines += ["", "| Протокол | Порт | Итог | RTT, мс | Задержка, мс | Мбит/с | IP выхода | Причина |",
              "|---|---|---|---|---|---|---|---|"]
    for r in report.get("results", []):
        why = "; ".join(([r["reason"]] if r.get("reason") else []) + (r.get("notes") or []))
        lines.append("| " + " | ".join(_md(x) for x in (
            r["id"], f"{r.get('port')}/{r.get('layer')}", f"**{r['verdict']}**", _num(r.get("l4", {}).get("rtt_ms"), ".0f"),
            _num(r.get("latency_ms"), ".0f"), _num(r.get("speed_mbps"), ".1f"), r.get("egress_ip"), why)) + " |")
    if report.get("compare"):
        lines += ["", "## Сравнение с самопроверкой сервера", "",
                  "| Протокол | Сервер | У вас | Вывод |", "|---|---|---|---|"]
        for c in report["compare"]:
            lines.append(f"| {_md(c['id'])} | {_md(c['server'])} | {_md(c['client'])} | {_md(c['verdict'])} |")
    seen = sorted({r["verdict"] for r in report.get("results", [])})
    if seen:
        lines += ["", "## Обозначения", ""]
        lines += [f"- **{v}** — {verdicts.DESCRIPTIONS.get(v, '')}" for v in seen]
    return "\n".join(lines) + "\n"
