"""Вывод: таблицы, JSON, размеры, цвета. Ошибки и предупреждения — в stderr."""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from typing import Any, Callable, Iterable, Sequence


def _color_on(stream=None) -> bool:
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")


_CODES = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36", "dim": "2", "bold": "1"}


def color(text: str, name: str, stream=None) -> str:
    if not _color_on(stream) or name not in _CODES:
        return text
    return f"\033[{_CODES[name]}m{text}\033[0m"


def _say(tag: str, name: str, msg: str) -> None:
    # stdout сначала сбрасываем: иначе в пайпе сообщения обгоняют таблицу
    sys.stdout.flush()
    print(f"{color(tag, name, sys.stderr)} {msg}", file=sys.stderr, flush=True)


def info(msg: str) -> None:
    _say("[i]", "cyan", msg)


def ok(msg: str) -> None:
    _say("[+]", "green", msg)


def warn(msg: str) -> None:
    _say("[!]", "yellow", msg)


def error(msg: str) -> None:
    _say("[x]", "red", msg)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def emit(data: Any, as_json: bool, render: Callable[[Any], None]) -> None:
    """--json → JSON на stdout, иначе человекочитаемый вывод через render(data)."""
    if as_json:
        print_json(data)
    else:
        render(data)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def width(s: str) -> int:
    """Ширина в терминале без ANSI-цветов: широкие символы (CJK, эмодзи) — 2, комбинируемые — 0."""
    w = 0
    for ch in _ANSI_RE.sub("", s):
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(s: str, n: int, right: bool = False) -> str:
    gap = " " * max(0, n - width(s))
    return gap + s if right else s + gap


def table(rows: Iterable[Sequence[Any]], headers: Sequence[str], right: Sequence[int] = ()) -> str:
    """Простая таблица с выравниванием; right — индексы колонок с выравниванием вправо."""
    data = [["" if c is None else str(c) for c in r] for r in rows]
    cols = len(headers)
    widths = [width(h) for h in headers]
    for r in data:
        for i in range(cols):
            if i < len(r):
                widths[i] = max(widths[i], width(r[i]))
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
             "  ".join("-" * widths[i] for i in range(cols))]
    for r in data:
        cells = [_pad(r[i] if i < len(r) else "", widths[i], i in right) for i in range(cols)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def mark(ok_: bool | None) -> str:
    """Значок состояния для таблиц."""
    if ok_ is None:
        return color("?", "dim")
    return color("OK", "green") if ok_ else color("FAIL", "red")
