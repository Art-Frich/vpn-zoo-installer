"""Графики inline SVG: столбцы по времени (со стеком рядов), полоски и спарклайны.

Цвета — классы s1..s8 (CSS-переменные --series-N, свои для светлой и тёмной темы),
подсказки — <title> у прозрачной зоны над столбцом: работают без JS и под строгим CSP
(никаких style="", только атрибуты и классы).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable

from ..output import human_bytes
from .html import Markup, t

MAX_SERIES = 8
GAP = 2  # зазор между сегментами стека и соседними столбцами


def series_class(i: int) -> str:
    return f"s{(i % MAX_SERIES) + 1}"


def nice_max(value: float) -> tuple[float, float]:
    """Верх шкалы и шаг делений (1-2-5 в двоичных единицах: КБ, МБ, ГБ)."""
    if value <= 0:
        return 1024.0, 512.0
    unit = 1024 ** max(0, min(4, int(math.log(value, 1024))))
    v = value / unit
    mag = 10 ** math.floor(math.log10(v)) if v > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag / 2
        top = math.ceil(v / step) * step
        if top / step <= 5:
            return top * unit, step * unit
    return math.ceil(v) * unit, math.ceil(v) * unit / 4


def nice_count(value: float) -> tuple[float, float]:
    """Верх шкалы и шаг делений для штук: четыре деления по 1, 2, 3, 5 × 10^k."""
    k = 1
    while True:
        for step in (1, 2, 3, 5):
            top = 4 * step * k
            if top >= value:
                return float(top), float(step * k)
        k *= 10


def count_label(v: float) -> str:
    v = float(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн".replace(".0 ", " ")
    if v >= 10_000:
        return f"{v / 1000:.0f} тыс"
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def time_label(ts: int, step: int) -> str:
    d = datetime.fromtimestamp(ts)
    if step <= 3600:
        return d.strftime("%H:%M")
    if step < 86400 and d.hour:
        return d.strftime("%d.%m %H:%M")
    return d.strftime("%d.%m")


def _rounded_top(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Путь прямоугольника со скруглённым верхом (данные растут от прямого основания)."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def columns(buckets: list[int], step: int, series: list[dict[str, Any]], label: str,
            width: int = 760, height: int = 240, fmt: Callable[[float], str] = human_bytes,
            nice: Callable[[float], tuple[float, float]] = nice_max, empty_tip: str = "нет трафика") -> Markup:
    """Столбцы по корзинам времени; несколько рядов — стеком. series: [{title, values}].
    fmt и nice — подписи и шкала: по умолчанию байты, для штук — count_label и nice_count."""
    n = len(buckets)
    if not n:
        return Markup("")
    pad_l, pad_r, pad_t, pad_b = 64, 8, 10, 26
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    totals = [sum(s["values"][i] for s in series) for i in range(n)]
    top, tick = nice(max(totals, default=0))
    slot = pw / n
    bw = max(2.0, min(24.0, slot - GAP))
    parts: list[Any] = []
    # сетка и подписи оси Y
    k = 0
    while k * tick <= top + 1e-9:
        y = pad_t + ph - (k * tick) / top * ph
        parts.append(t("line", x1=pad_l, x2=width - pad_r, y1=f"{y:.1f}", y2=f"{y:.1f}", class_="gridline"))
        parts.append(t("text", fmt(k * tick), x=pad_l - 6, y=f"{y + 4:.1f}", class_="axis",
                       text_anchor="end"))
        k += 1
    # подписи оси X: не больше 7; у шагов в несколько часов — даты на полуночах
    every = max(1, math.ceil(n / 7))
    marks = list(range(0, n, every))
    if 3600 < step < 86400:
        marks = [i for i in range(n) if datetime.fromtimestamp(buckets[i]).hour == 0][-7:] or marks
    for i in marks:
        x = pad_l + i * slot + slot / 2
        parts.append(t("text", time_label(buckets[i], step), x=f"{x:.1f}", y=height - 8, class_="axis",
                       text_anchor="middle"))
    for i in range(n):
        x = pad_l + i * slot + (slot - bw) / 2
        base = pad_t + ph
        segs = [(j, s["values"][i]) for j, s in enumerate(series) if s["values"][i] > 0]
        for pos, (j, v) in enumerate(segs):
            h = v / top * ph
            y = base - h
            # над нижним сегментом — зазор цвета фона
            gap = GAP if pos > 0 and h > GAP + 1 else 0
            dh = max(h - gap, 1.0)
            if pos == len(segs) - 1:
                parts.append(t("path", d=_rounded_top(x, y, bw, dh), class_=series_class(j)))
            else:
                parts.append(t("rect", x=f"{x:.1f}", y=f"{y:.1f}", width=f"{bw:.1f}", height=f"{dh:.1f}",
                               class_=series_class(j)))
            base = y
        tip = [time_label(buckets[i], step) + (" (сейчас)" if i == n - 1 else "")]
        tip += [f"{s['title']}: {fmt(s['values'][i])}" for s in series if s["values"][i] > 0]
        if len(series) > 1:
            tip.append(f"всего: {fmt(totals[i])}")
        if totals[i] == 0:
            tip.append(empty_tip)
        parts.append(t("rect", t("title", "\n".join(tip)), x=f"{pad_l + i * slot:.1f}", y=pad_t,
                       width=f"{slot:.1f}", height=ph, class_="hit"))
    parts.append(t("line", x1=pad_l, x2=width - pad_r, y1=pad_t + ph, y2=pad_t + ph, class_="baseline"))
    return t("svg", t("title", label), *parts, viewBox=f"0 0 {width} {height}", class_="chart", role="img",
             aria_label=label, preserveAspectRatio="xMidYMid meet")


def legend(series: list[dict[str, Any]], totals: bool = True,
           fmt: Callable[[float], str] = human_bytes) -> Markup:
    items = []
    for i, s in enumerate(series):
        value = t("span", fmt(sum(s["values"])), class_="muted") if totals else None
        items.append(t("li", t("span", class_=f"swatch {series_class(i)}"), t("span", s["title"]), value))
    return t("ul", items, class_="legend")


def bar(value: float, maximum: float, cls: str = "s1") -> Markup:
    """Горизонтальная полоска доли для ячейки таблицы."""
    pct = 0.0 if maximum <= 0 else max(0.0, min(100.0, value / maximum * 100))
    inner = t("rect", x=0, y=0, width=f"{pct:.1f}", height=8, rx=2, class_=cls) if pct > 0 else None
    return t("svg", t("rect", x=0, y=0, width=100, height=8, rx=2, class_="track"), inner,
             viewBox="0 0 100 8", preserveAspectRatio="none", class_="hbar", aria_hidden="true")


def meter(value: float, maximum: float, warn: float = 0.8, bad: float = 0.92) -> Markup:
    """Заполненность (RAM, диск): цвет по порогам."""
    share = 0.0 if maximum <= 0 else value / maximum
    kind = "bad" if share >= bad else ("warn" if share >= warn else "ok")
    pct = max(0.0, min(100.0, share * 100))
    return t("svg", t("rect", x=0, y=0, width=100, height=6, rx=3, class_="track"),
             t("rect", x=0, y=0, width=f"{pct:.1f}", height=6, rx=3, class_=f"fill-{kind}") if pct else None,
             viewBox="0 0 100 6", preserveAspectRatio="none", class_="meter", aria_hidden="true")


def sparkline(values: list[int], cls: str = "s1", width: int = 120, height: int = 28) -> Markup:
    """Мини-столбцы (24 ч) для карточки протокола."""
    n = len(values)
    mx = max(values, default=0)
    if not n:
        return Markup("")
    slot = width / n
    bw = max(1.0, slot - 1)
    bars = []
    for i, v in enumerate(values):
        h = 0 if mx <= 0 else max(1.0 if v > 0 else 0, v / mx * (height - 2))
        if h:
            bars.append(t("rect", x=f"{i * slot:.1f}", y=f"{height - h:.1f}", width=f"{bw:.1f}",
                          height=f"{h:.1f}", class_=cls))
    return t("svg", t("line", x1=0, x2=width, y1=height - 0.5, y2=height - 0.5, class_="baseline"), bars,
             viewBox=f"0 0 {width} {height}", class_="spark", aria_hidden="true", preserveAspectRatio="none")


