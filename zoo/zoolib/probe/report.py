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


def _metrics(r: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    m = r.get("metrics") or {}
    return m.get("latency") or {}, m.get("upload") or {}


def _p50_p90(lat: dict[str, Any]) -> str:
    if lat.get("median_ms") is None:
        return "—"
    return f"{lat['median_ms']:.0f}/{_num(lat.get('p90_ms'), '.0f')}"


def context_line(report: dict[str, Any]) -> str:
    """«метка …, устройство …, провайдер … (AS…), страна, сеть» — условия пробы; пусто, если их нет."""
    c = report.get("context") or {}
    isp = (f"AS{c['asn']} " if c.get("asn") else "") + (c.get("isp") or "")
    net = {"wifi": "Wi-Fi", "ethernet": "кабель", "cellular": "мобильная сеть"}.get(c.get("net") or "", c.get("net"))
    parts = [f"метка {c['tag']}" if c.get("tag") else "", f"устройство {c['device']}" if c.get("device") else "",
             f"провайдер {isp.strip()}" if isp.strip() else "", c.get("country") or "",
             f"сеть: {net}" if net else ""]
    return ", ".join(x for x in parts if x)


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
    if context_line(report):
        print(f"Условия: {context_line(report)}")
    print()
    results = report.get("results", [])
    # расширенные метрики (задержка p50/p90, джиттер, отдача) — колонки только если они есть в отчёте
    has_lat = any(_metrics(r)[0].get("median_ms") is not None for r in results)
    has_up = any(_metrics(r)[1].get("mbps") is not None for r in results)
    rows = []
    for r in results:
        port = f"{r.get('port') or '?'}/{r.get('layer') or '?'}"
        lat, up = _metrics(r)
        row = [r["id"], port, _verdict(r["verdict"]), _num(r.get("l4", {}).get("rtt_ms"), ".0f"),
               _num(r.get("latency_ms"), ".0f")]
        if has_lat:
            row += [_p50_p90(lat), _num(lat.get("jitter_ms"), ".0f")]
        row.append(_num(r.get("speed_mbps"), ".1f"))
        if has_up:
            row.append(_num(up.get("mbps"), ".1f"))
        row.append(r.get("egress_ip") or "—")
        if local:
            row.insert(2, TARGET_TEXT.get(r.get("target"), r.get("target") or ""))
        rows.append(row)
    headers = ["протокол", "порт", "итог", "RTT, мс", "задержка, мс"]
    if has_lat:
        headers += ["ответ p50/p90, мс", "джиттер, мс"]
    headers.append("загрузка, Мбит/с" if has_up else "Мбит/с")
    if has_up:
        headers.append("отдача, Мбит/с")
    headers.append("IP выхода")
    if local:
        headers.insert(2, "куда")
    if rows:
        first_num = 4 if local else 3
        print(output.table(rows, headers, right=tuple(range(first_num, first_num + len(headers) - 4 - (1 if local else 0)))))
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


def _principle(verdict: str) -> tuple[str, str]:
    """Ответ «работает в принципе?» по вердикту самопроверки → (текст, цвет)."""
    if verdict == verdicts.OK:
        return "да", "green"
    if verdict == verdicts.SLOW:
        return "да, медленно", "yellow"
    if verdict in verdicts.NOT_TESTED:
        return "не проверено", "yellow"
    return "НЕТ", "red"


def render_summary(report: dict[str, Any]) -> None:
    """Короткая сводка самопроверки для конца установки: работает ли протокол в принципе."""
    results = report.get("results", [])
    print(f"Самопроверка с самого сервера {report.get('server_ip') or '?'} (креды {report.get('user') or '?'}): "
          "работает ли протокол в принципе, без блокировок провайдера")
    print()
    rows = []
    for r in results:
        text, color = _principle(r["verdict"])
        speed = f"{r['speed_mbps']:.0f} Мбит/с" if r.get("speed_mbps") is not None else ""
        rows.append([r["id"], f"{r.get('port') or '?'}/{r.get('layer') or '?'}", output.color(text, color),
                     _verdict(r["verdict"]), speed])
    if rows:
        print(output.table(rows, ["протокол", "порт", "работает в принципе", "итог", "скорость"]))
    else:
        print("Протоколов для проверки нет.")
    bad = [r for r in results if r["verdict"] not in verdicts.WORKING and r["verdict"] not in verdicts.NOT_TESTED]
    skipped = [r for r in results if r["verdict"] in verdicts.NOT_TESTED]
    good = len(results) - len(bad) - len(skipped)
    print()
    print(f"Работает в принципе: {good} из {len(results)}.")
    for r in bad:
        why = "; ".join(([r["reason"]] if r.get("reason") else []) + (r.get("notes") or []))
        output.warn(output.color(f"{r['id']}: НЕ РАБОТАЕТ на самом сервере ({r['verdict']})", "red")
                    + (f" — {why}" if why else ""))
    for r in skipped:
        why = r.get("reason") or "; ".join(r.get("notes") or [])
        output.info(f"{r['id']}: не проверено ({r['verdict']})" + (f" — {why}" if why else ""))
    if bad:
        output.info("подробности: sudo zoo probe --local --proto " + " --proto ".join(r["id"] for r in bad)
                    + "; журналы: sudo zoo status, journalctl -u x-ui / hysteria-server / awg-quick@awg0")


def render_next_steps(report: dict[str, Any], bundle: str | None, ssh_port: str = "22",
                      repo_url: str = "https://github.com/Art-Frich/vpn-zoo-installer.git") -> None:
    """Что делать дальше: проверка с машины пользователя (блокирует ли его провайдер)."""
    host = report.get("server_ip") or "СЕРВЕР"
    host_s = f"[{host}]" if ":" in host else host
    scp_p = "" if ssh_port in ("", "22") else f"-P {ssh_port} "
    src = bundle or "/etc/vpn-setup/probe-export.json"
    print()
    print("Дальше — проверка с вашей машины: блокирует ли протоколы ваш провайдер.")
    if not bundle:
        print(f"  0. На сервере: sudo zoo export-probe --out {src}")
    print(f"  1. Пакет для пробника: {src} (в нём ключи доступа и итог этой самопроверки).")
    print("  2. На своём компьютере (VPN выключен; нужны Docker, git, scp), один раз:")
    print(f"       git clone {repo_url}")
    print("       cd vpn-zoo-installer")
    print("       mkdir probe")
    print("       docker build -f docker/probe.Dockerfile -t zoo-probe .")
    print("     Перед каждой проверкой:")
    print(f"       scp {scp_p}root@{host_s}:{src} probe/probe-export.json")
    print('       docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe')
    print('     Windows PowerShell: то же, но -v "${PWD}\\probe:/data". Вход root по SSH запрещён —')
    print("     README, «Если вход root по SSH запрещён».")
    print("     Без Docker (Linux с клиентами xray, hysteria, sing-box, amneziawg-go, awg):")
    print("       sudo python3 zoo/zoo probe --remote probe/probe-export.json --md probe/probe-report.md \\")
    print("            --out probe/probe-report.json")
    print("  3. Итог — в конце вывода пробника и в probe/probe-report.md: таблица «сервер ↔ у вас» —")
    print("     «работает у вас», «работает в принципе, блокируется у вас (тип)» или «не работает на самом")
    print("     сервере». probe/probe-report.json можно вставить на странице «Проверка» админки.")
    print("  4. После проверки удалите probe/probe-export.json (в нём ключи). На сервере пакет можно")
    print(f"     удалить: sudo rm {src} (новый создаст sudo zoo probe --local --summary --export {src}).")
    print("  Подробно: README, раздел «Блокирует ли ваш провайдер», и docker/probe/README.md.")


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
    if context_line(report):
        lines.append(f"- условия: {context_line(report)}")
    results = report.get("results", [])
    has_lat = any(_metrics(r)[0].get("median_ms") is not None for r in results)
    has_up = any(_metrics(r)[1].get("mbps") is not None for r in results)
    head = ["Протокол", "Порт", "Итог", "RTT, мс", "Задержка, мс"]
    if has_lat:
        head += ["Ответ p50/p90, мс", "Джиттер, мс"]
    head += ["Загрузка, Мбит/с" if has_up else "Мбит/с"] + (["Отдача, Мбит/с"] if has_up else []) + ["IP выхода", "Причина"]
    lines += ["", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in results:
        why = "; ".join(([r["reason"]] if r.get("reason") else []) + (r.get("notes") or []))
        lat, up = _metrics(r)
        cells = [r["id"], f"{r.get('port')}/{r.get('layer')}", f"**{r['verdict']}**",
                 _num(r.get("l4", {}).get("rtt_ms"), ".0f"), _num(r.get("latency_ms"), ".0f")]
        if has_lat:
            cells += [_p50_p90(lat), _num(lat.get("jitter_ms"), ".0f")]
        cells.append(_num(r.get("speed_mbps"), ".1f"))
        if has_up:
            cells.append(_num(up.get("mbps"), ".1f"))
        cells += [r.get("egress_ip"), why]
        lines.append("| " + " | ".join(_md(x) for x in cells) + " |")
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
