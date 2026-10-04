"""Метрики пробы: задержка (медиана, p90, джиттер), скорость. Чистая математика, без сети.

В отчёте результата протокола эти числа лежат в `metrics` (схема 1 дополняется, старые поля
`latency_ms` и `speed_mbps` остаются: на них держится вердикт):

    metrics = {
      "latency":  {"sent", "ok", "loss_pct", "min_ms", "median_ms", "p90_ms", "max_ms", "jitter_ms",
                   "connect_median_ms", "per_target": {хост: {те же поля}}},
      "download": {"ok", "bytes", "seconds", "mbps", "ttfb_ms"},
      "upload":   {"ok", "bytes", "seconds", "mbps", "error"},
    }

Задержка — время от отправки запроса до первого байта ответа (TTFB) на малый запрос через
туннель: один круг «клиент → туннель → интернет → назад», без установки соединения и TLS.
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable
from urllib.parse import urlsplit


def percentile(values: Iterable[float], p: float) -> float | None:
    """Перцентиль p (0..100) с линейной интерполяцией между соседними значениями."""
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def jitter(samples: list[float]) -> float | None:
    """Джиттер как в RFC 3550: среднее |разность соседних замеров| в порядке получения."""
    if len(samples) < 2:
        return None
    return sum(abs(b - a) for a, b in zip(samples, samples[1:])) / (len(samples) - 1)


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def latency_stats(samples: list[float], sent: int, connect: list[float] | None = None) -> dict[str, Any]:
    """samples — удавшиеся замеры, мс (в порядке получения); sent — сколько запросов отправлено."""
    ok = len(samples)
    out: dict[str, Any] = {
        "sent": sent, "ok": ok,
        "loss_pct": round((sent - ok) * 100.0 / sent, 1) if sent else None,
        "min_ms": _r(min(samples)) if samples else None,
        "median_ms": _r(statistics.median(samples)) if samples else None,
        "p90_ms": _r(percentile(samples, 90)),
        "max_ms": _r(max(samples)) if samples else None,
        "jitter_ms": _r(jitter(samples)),
    }
    if connect is not None:
        out["connect_median_ms"] = _r(statistics.median(connect)) if connect else None
    return out


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


def latency_from_jobs(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Результаты fetch() малых запросов (в порядке отправки) → блок `latency` с разбивкой по целям.
    Замер без ttfb_ms (запрос не удался) — потеря."""
    all_s: list[float] = []
    all_c: list[float] = []
    per: dict[str, tuple[list[float], list[float], int]] = {}
    for j in jobs:
        host = host_of(j.get("url") or "")
        s, c, n = per.setdefault(host, ([], [], 0))
        per[host] = (s, c, n + 1)
        if j.get("ok") and j.get("ttfb_ms") is not None:
            s.append(float(j["ttfb_ms"]))
            all_s.append(float(j["ttfb_ms"]))
            if j.get("connect_ms") is not None:
                c.append(float(j["connect_ms"]))
                all_c.append(float(j["connect_ms"]))
    out = latency_stats(all_s, len(jobs), all_c)
    out["per_target"] = {h: latency_stats(s, n, c) for h, (s, c, n) in per.items()}
    return out


def mbps(nbytes: int | float | None, seconds: float | None) -> float | None:
    if not nbytes or seconds is None or seconds < 0:
        return None
    return round(nbytes * 8 / 1e6 / max(seconds, 1e-3), 2)


def download_block(job: dict[str, Any]) -> dict[str, Any]:
    """Результат большого запроса → блок `download`."""
    secs = job.get("body_seconds") or job.get("seconds")
    ok = bool(job.get("ok"))
    return {"ok": ok, "bytes": int(job.get("bytes") or 0), "seconds": secs,
            "mbps": mbps(job.get("bytes"), secs) if ok else None, "ttfb_ms": job.get("ttfb_ms")}


def upload_block(job: dict[str, Any]) -> dict[str, Any]:
    """Результат POST-замера → блок `upload`."""
    ok = bool(job.get("ok"))
    secs = job.get("upload_seconds")
    return {"ok": ok, "bytes": int(job.get("sent_bytes") or 0), "seconds": secs,
            "mbps": mbps(job.get("sent_bytes"), secs) if ok else None, "error": job.get("error") or ""}


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> float | None:
    """Число из отчёта или None: присланный отчёт может быть чужим или битым, а строка в БД
    потом ломала бы рейтинг и страницу «Проверка»."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def result_numbers(r: dict[str, Any]) -> dict[str, float | None]:
    """Числа результата для истории и ранжирования; новые поля metrics, иначе старые latency_ms/speed_mbps."""
    m = _dict(r.get("metrics"))
    lat, down, up = _dict(m.get("latency")), _dict(m.get("download")), _dict(m.get("upload"))
    med, dmb = _num(lat.get("median_ms")), _num(down.get("mbps"))
    return {
        "latency_ms": med if med is not None else _num(r.get("latency_ms")),
        "p90_ms": _num(lat.get("p90_ms")), "jitter_ms": _num(lat.get("jitter_ms")),
        "loss_pct": _num(lat.get("loss_pct")), "rtt_ms": _num(_dict(r.get("l4")).get("rtt_ms")),
        "down_mbps": dmb if dmb is not None else _num(r.get("speed_mbps")),
        "up_mbps": _num(up.get("mbps")),
    }
