"""Список людей одним текстом (мастер «Новая группа», «Добавить людей списком»).

Строка — «имя» или «имя; заметка» (разделитель — первая «;», табуляция или запятая). Имя может быть
русским: из него получается id пользователя (транслит, нижний регистр, только a-z 0-9 - _), при
совпадении с занятым или с предыдущей строкой добавляется номер («ivan-2»). Исходное имя не теряется:
если id на него не похож, оно уходит в начало заметки.

План строится на сервере из текста и реестра: ему не верят ни скрытые поля, ни предпросмотр."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import users

LINES_MAX = 200
LINE_MAX = 300
TEXT_MAX = 60000
NOTE_MAX = 200
NAME_LEN = 32
SEPARATORS = (";", "\t", ",")

_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def slug(name: str) -> str:
    """Имя → допустимый id (может получиться пустым: в имени не было ни букв, ни цифр)."""
    # транслит до NFKD: иначе «й» и «ё» распались бы на «и» и «е» со знаком
    latin = unicodedata.normalize("NFKD", "".join(_RU.get(ch, ch) for ch in name.lower()))
    s = re.sub(r"[^a-z0-9_-]+", "-", "".join(ch for ch in latin if not unicodedata.combining(ch)))
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return s[:NAME_LEN].strip("-_")


@dataclass
class Row:
    line: int            # номер непустой строки, с 1
    raw: str             # как написано
    display: str         # имя как написано
    note: str            # заметка, как будет сохранена
    name: str = ""       # id пользователя
    converted: bool = False   # id не совпал с написанным (транслит, замена знаков)
    clash: str = ""      # почему к id добавлен номер: «занято», «служебное», «повтор в списке»
    problem: str = ""    # строку создать нельзя


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    problem: str = ""    # общий отказ: слишком много строк

    @property
    def ok(self) -> bool:
        return not self.problem and not any(r.problem for r in self.rows)

    @property
    def error(self) -> str:
        if self.problem:
            return self.problem
        bad = next((r for r in self.rows if r.problem), None)
        return f"строка {bad.line} «{bad.raw[:40]}»: {bad.problem}" if bad else ""

    def pairs(self) -> list[tuple[str, str]]:
        return [(r.name, r.note) for r in self.rows]

    @property
    def renamed(self) -> list[Row]:
        return [r for r in self.rows if r.clash]


def split_line(line: str) -> tuple[str, str]:
    """«имя; заметка» → (имя, заметка): режет по первому из разделителей."""
    cuts = [i for i in (line.find(s) for s in SEPARATORS) if i >= 0]
    i = min(cuts) if cuts else -1
    name, note = (line, "") if i < 0 else (line[:i], line[i + 1:])
    note = re.sub(r"\s*\t\s*", "; ", note)
    clean = lambda s: re.sub(r"\s+", " ", _CTRL.sub(" ", s)).strip().strip('"').strip()
    return clean(name), clean(note)


def _suffixed(base: str, n: int) -> str:
    suf = f"-{n}"
    return base[: NAME_LEN - len(suf)].rstrip("-_") + suf


def build(text: str, taken: set[str] | frozenset[str] = frozenset()) -> Plan:
    """Текст → план. taken — имена, которые уже есть в реестре."""
    plan = Plan()
    lines = [ln[:LINE_MAX] for ln in (text or "").replace("﻿", "").splitlines() if ln.strip()]
    if len(lines) > LINES_MAX:
        plan.problem = f"за раз — не больше {LINES_MAX} человек, в списке {len(lines)}"
        lines = lines[:LINES_MAX]
    used = set(taken) | set(users.SYSTEM_USERS)
    for n, raw in enumerate(lines, 1):
        display, note = split_line(raw)
        row = Row(n, raw.strip(), display, note[:NOTE_MAX])
        base = slug(display)
        if not display:
            row.problem = "нет имени"
        elif not base:
            row.problem = "в имени нет ни букв, ни цифр"
        else:
            row.converted = base != display.lower()
            row.name = base
            if base in used:
                row.clash = ("служебное" if base in users.SYSTEM_USERS else
                             "занято" if base in taken else "повтор в списке")
                k = 2
                while _suffixed(base, k) in used:
                    k += 1
                row.name = _suffixed(base, k)
            used.add(row.name)
            if row.converted:
                row.note = (display + (" · " + note if note else ""))[:NOTE_MAX]
        plan.rows.append(row)
    return plan


def plan_for_registry(text: str) -> Plan:
    """План по текущему реестру пользователей."""
    return build(text, set(users.list_users().names()))
