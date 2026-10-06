"""«Лучшие протоколы»: рейтинг по накопленной истории проб (`zoo probe --rank`, админка).

Прогоны группируются по контексту (метка --tag; нет метки — провайдер; нет и его — «без метки»).
Внутри контекста у каждого протокола считаются:

    успех      доля прогонов с вердиктом OK или SLOW среди проверенных (SKIPPED и CLIENT_ERROR
               не считаются: проверки не было)
    задержка   медиана по удачным прогонам (медиана TTFB малого запроса, мс)
    скорость   медиана скорости загрузки по удачным прогонам (Мбит/с)

    оценка = 100 × успех × (0,6 × скорость / лучшая скорость в контексте
                            + 0,4 × лучшая задержка в контексте / задержка)

Скорость и задержка берутся относительно лучшего протокола того же контекста: оценки сравнимы
только внутри одного контекста (мобильная сеть и домашний Wi-Fi не смешиваются). Нет данных об
одной из метрик — оценка по другой; нет обеих — множитель 0,5. Протокол, который ни разу не
сработал, получает 0. Меньше 3 прогонов — «мало данных» (низкая уверенность), меньше 10 — средняя.

Блок (контекст), где отчётов меньше 3, не ранжируется: один замер — шум, а не рейтинг (ranked=False,
top пуст). Разброс — квартили p25–p75 по удачным прогонам, считаются от 3 замеров.
"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import Any

from .. import output
from . import history, verdicts

W_SPEED, W_LATENCY = 0.6, 0.4
LOW_SAMPLES, MEDIUM_SAMPLES = 3, 10
NO_LABEL = "без метки"
BY_CHOICES = ("context", "tag", "isp", "device", "net")


def context_label(row: dict[str, Any], by: str = "context") -> str:
    if by == "context":
        return row.get("tag") or row.get("isp") or NO_LABEL
    return row.get(by) or NO_LABEL


def confidence(n: int) -> str:
    return "low" if n < LOW_SAMPLES else ("medium" if n < MEDIUM_SAMPLES else "high")


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _spread(xs: list[float]) -> tuple[float | None, float | None]:
    """(p25, p75) от LOW_SAMPLES замеров; меньше — разброс не показываем."""
    if len(xs) < LOW_SAMPLES:
        return None, None
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return q[0], q[2]


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def rank_context(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """rows — строки одного контекста. → протоколы, лучшие первыми."""
    per: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["verdict"] in verdicts.NOT_TESTED:
            continue
        per.setdefault(r["proto"], []).append(r)
    stats = []
    for proto, rs in per.items():
        good = [r for r in rs if r["verdict"] in verdicts.WORKING]
        lats = [r["latency_ms"] for r in good if r.get("latency_ms")]
        downs = [r["down_mbps"] for r in good if r.get("down_mbps")]
        lat, down = _median(lats), _median(downs)
        (lat_lo, lat_hi), (down_lo, down_hi) = _spread(lats), _spread(downs)
        stats.append({"proto": proto, "n": len(rs), "ok": len(good), "success_pct": round(100 * len(good) / len(rs), 1),
                      "latency_ms": _r(lat, 1), "latency_p25": _r(lat_lo, 1), "latency_p75": _r(lat_hi, 1),
                      "down_mbps": _r(down, 2), "down_p25": _r(down_lo, 2), "down_p75": _r(down_hi, 2),
                      "confidence": confidence(len(rs)), "last": [r["verdict"] for r in rs[-8:]]})
    best_down = max((s["down_mbps"] for s in stats if s["down_mbps"]), default=None)
    best_lat = min((s["latency_ms"] for s in stats if s["latency_ms"]), default=None)
    for s in stats:
        parts = []
        if s["down_mbps"] and best_down:
            parts.append((W_SPEED, s["down_mbps"] / best_down))
        if s["latency_ms"] and best_lat:
            parts.append((W_LATENCY, best_lat / s["latency_ms"]))
        quality = sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else 0.5
        s["score"] = round(100 * (s["ok"] / s["n"]) * quality, 1) if s["ok"] else 0.0
        s["low_confidence"] = s["confidence"] == "low"
    return sorted(stats, key=lambda s: (-s["score"], -s["n"], s["proto"]))


def rank(rows: list[dict[str, Any]], by: str = "context", top: int = 3) -> list[dict[str, Any]]:
    """Рейтинг по контекстам. Контекст с большим числом прогонов — выше."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(context_label(r, by), []).append(r)
    out = []
    for name, rs in groups.items():
        protos = rank_context(rs)
        if not protos:
            continue
        reports = len({r["report_id"] for r in rs})
        ranked = reports >= LOW_SAMPLES
        out.append({"context": name, "reports": reports, "ranked": ranked,
                    "devices": sorted({r["device"] for r in rs if r.get("device")}),
                    "isps": sorted({r["isp"] for r in rs if r.get("isp")}),
                    "top": [p for p in protos if p["ok"] and p["n"] >= LOW_SAMPLES][:top] if ranked else [],
                    "protocols": protos})
    return sorted(out, key=lambda c: (-c["reports"], c["context"]))


def load(period: str | None = "30d", tag: str | None = None, with_local: bool = False, by: str = "context",
         con: Any = None) -> list[dict[str, Any]]:
    """Рейтинг из БД истории; нет БД — пусто."""
    own = con is None
    con = con or history.connect(create=False)
    if con is None:
        return []
    try:
        rows = history.fetch_results(con, history.since_ts(period), None if with_local else "remote", tag)
    finally:
        if own:
            con.close()
    return rank(rows, by)


# ---------- вывод ----------

FORMULA = ("оценка = 100 × успех × (0,6 × скорость/лучшая + 0,4 × лучшая задержка/задержка); "
           "лучшие — внутри контекста, поэтому оценки сравнимы только в его пределах; "
           "задержка и скорость — медианы по удачным прогонам, в скобках разброс p25–p75 (от 3 замеров); "
           "места — от 3 прогонов в контексте и от 3 замеров протокола")


def plural_runs(n: int) -> str:
    """«1 замер», «2 замера», «5 замеров»."""
    word = "замер" if n % 10 == 1 and n % 100 != 11 else (
        "замера" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "замеров")
    return f"{n} {word}"


def spread_text(p: dict[str, Any], metric: str, fmt: str, unit: str) -> str:
    """metric: latency | down. «95 мс (80–130)»; нет данных — «—»; без разброса (мало замеров) — медиана."""
    v = p.get("latency_ms" if metric == "latency" else "down_mbps")
    if v is None:
        return "—"
    lo, hi = p.get(f"{metric}_p25"), p.get(f"{metric}_p75")
    text = format(v, fmt) + unit
    if lo is None or hi is None or format(lo, fmt) == format(hi, fmt):
        return text
    return f"{text} ({format(lo, fmt)}–{format(hi, fmt)})"



def render(ranking: list[dict[str, Any]], period: str | None) -> None:
    if not ranking:
        print("В истории нет клиентских проб за период. Запустите пробник с метками "
              "(zoo-probe --tag mobile-mts --device pixel7) и добавьте отчёт: zoo history add probe-report.json. "
              "Прогоны самого сервера: --with-local.")
        return
    print(f"Лучшие протоколы по истории проб ({'за ' + period if period and period != 'all' else 'за всё время'}):")
    for c in ranking:
        extra = [x for x in (", ".join(c["isps"]), ", ".join(c["devices"])) if x]
        print()
        print(f"== {c['context']} — прогонов: {c['reports']}" + (f" ({'; '.join(extra)})" if extra else "") + " ==")
        rows = []
        if not c["ranked"]:
            print(f"{plural_runs(c['reports'])} — ориентир, не рейтинг (мало данных)")
            for p in c["protocols"]:
                rows.append([p["proto"], f"{p['success_pct']:.0f}% ({p['ok']}/{p['n']})",
                             spread_text(p, "latency", ".0f", ""), spread_text(p, "down", ".1f", "")])
            print(output.table(rows, ["протокол", "успех", "задержка, мс", "скорость, Мбит/с"], right=(2, 3)))
            continue
        for p in c["protocols"]:
            place = c["top"].index(p) + 1 if p in c["top"] else ""
            rows.append([place, p["proto"], f"{p['score']:.0f}", f"{p['success_pct']:.0f}% ({p['ok']}/{p['n']})",
                         spread_text(p, "latency", ".0f", ""), spread_text(p, "down", ".1f", ""),
                         "мало замеров" if p["low_confidence"] else ""])
        print(output.table(rows, ["место", "протокол", "оценка", "успех", "задержка, мс", "скорость, Мбит/с", ""],
                           right=(2, 4, 5)))
    print()
    print(FORMULA)


def cmd_rank(args: argparse.Namespace) -> int:
    try:
        ranking = load(args.period, args.tag, args.with_local, args.by)
    except history.HistoryError as e:
        output.error(str(e))
        return 2
    if args.json:
        output.print_json({"period": args.period, "formula": FORMULA, "rank": ranking, "now": int(time.time())})
    else:
        render(ranking, args.period)
    return 0
