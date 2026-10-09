"""Список людей одним текстом (мастер «Подключить людей», «Добавить людей списком»).

Строка — «имя», «имя; заметка» или «имя; заметка; android, windows» (разделитель — первая «;», табуляция или
запятая; последнее поле из одних названий устройств — устройства человека, без него — как у группы). Имя может быть
русским: из него получается логин пользователя (транслит, нижний регистр, только a-z 0-9 - _), при
совпадении с занятым или с предыдущей строкой добавляется номер («ivan-2»). Исходное имя не теряется:
оно хранится у человека отдельно (users.User.display), и к нему обращаются в инструкциях и карточках.

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
# устройства человека в списке: id платформы каталога (clients.json) → название; слова, которыми их пишут
DEVICES = {"android": "Android", "ios": "iPhone", "windows": "Windows", "macos": "macOS", "linux": "Linux"}
DEVICE_WORDS = {
    "android": "android", "андроид": "android", "андройд": "android", "samsung": "android", "самсунг": "android",
    "xiaomi": "android", "сяоми": "android", "ксиоми": "android", "redmi": "android", "редми": "android",
    "poco": "android", "honor": "android", "хонор": "android", "huawei": "android", "хуавей": "android",
    "realme": "android", "pixel": "android", "oneplus": "android", "tecno": "android", "infinix": "android",
    "iphone": "ios", "ios": "ios", "айфон": "ios", "ipad": "ios", "айпад": "ios", "apple": "ios",
    "windows": "windows", "win": "windows", "виндовс": "windows", "винда": "windows", "пк": "windows", "pc": "windows",
    "ноутбук": "windows", "ноут": "windows", "компьютер": "windows", "комп": "windows", "laptop": "windows",
    "macos": "macos", "mac": "macos", "macbook": "macos", "мак": "macos", "макбук": "macos", "imac": "macos",
    "linux": "linux", "линукс": "linux", "ubuntu": "linux", "убунту": "linux",
}
FIELD_WORDS = 2   # в поле устройств слово-другое («айфон», «ноутбук асус»); длиннее — это заметка

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
    note: str            # заметка (без имени)
    name: str = ""       # id пользователя
    converted: bool = False   # id не совпал с написанным (транслит, замена знаков)
    clash: str = ""      # почему к id добавлен номер: «занято», «служебное», «повтор в списке»
    problem: str = ""    # строку создать нельзя
    devices: list[str] = field(default_factory=list)   # устройства из списка (id платформ); пусто — как у группы
    unknown: list[str] = field(default_factory=list)   # слова в поле устройств, которых мы не знаем («планшет»)
    hint: str = ""       # похоже на ошибку разбора, но строку создать можно («Петров, Иван»: запятая отрезала имя)


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

    def triples(self) -> list[tuple[str, str, str]]:
        """(логин, заметка, имя как написано): имя сохраняется у человека отдельно от заметки."""
        return [(r.name, r.note, r.display) for r in self.rows]

    def entries(self) -> list[tuple[str, str, str, tuple[str, ...]]]:
        """(логин, заметка, имя, устройства) — для создания людей."""
        return [(r.name, r.note, r.display, tuple(r.devices)) for r in self.rows]

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


def _words(piece: str) -> list[str]:
    return [w for w in re.split(r"[\s+/()]+", piece.strip().casefold().strip(".!")) if w]


def _device_ids(piece: str) -> list[str] | None:
    """«android, iPhone» → ['android', 'ios']; есть слово не про устройство — None."""
    words = _words(piece)
    if not words or any(w not in DEVICE_WORDS for w in words):
        return None
    return [DEVICE_WORDS[w] for w in words]


def split_devices(note: str) -> tuple[str, list[str], list[str]]:
    """Заметка → (заметка, устройства, непонятые слова). Устройства — последнее поле после «;» (без «;» — хвост через
    запятые), с первого названия устройства; короткие слова после него, которых мы не знаем («айфон, планшет»), не
    уходят молча в заметку, а возвращаются: предпросмотр спросит о них."""
    head, sep, tail = note.rpartition(";")   # есть «;» — устройства только в последнем поле, иначе — в хвосте через запятые
    if not sep:
        head, tail = "", note
    parts = re.split(r"(,)", tail)
    fields = parts[0::2]
    k = len(fields)
    while k > 0 and len(_words(fields[k - 1])) <= FIELD_WORDS and fields[k - 1].strip():
        k -= 1
    while k < len(fields) and _device_ids(fields[k]) is None:   # поле устройств начинается с узнанного устройства
        k += 1
    if k >= len(fields):
        return note, [], []
    found: list[str] = []
    unknown: list[str] = []
    for f in fields[k:]:
        for w in _words(f):
            if w in DEVICE_WORDS:
                found.append(DEVICE_WORDS[w])
            else:
                unknown.append(w)
    rest = "".join(parts[:max(2 * k - 1, 0)]).strip(" ,")
    note = "; ".join(x for x in (head.strip(" ;,"), rest) if x)
    return note, list(dict.fromkeys(found)), list(dict.fromkeys(unknown))


def _comma_cut(raw: str, note: str) -> bool:
    """Строку разрезала запятая, а заметка — одно слово с большой буквы: похоже на «Петров, Иван» из Excel."""
    cuts = [i for i in (raw.find(s) for s in SEPARATORS) if i >= 0]
    return bool(cuts) and raw[min(cuts)] == "," and bool(re.fullmatch(r"[A-ZА-ЯЁ][a-zа-яё-]+", note))


def device_titles(ids: list[str] | tuple[str, ...]) -> str:
    return ", ".join(DEVICES.get(i, i) for i in ids)


def _suffixed(base: str, n: int) -> str:
    suf = f"-{n}"
    return base[: NAME_LEN - len(suf)].rstrip("-_") + suf


def login_for(display: str, taken: set[str] | frozenset[str] = frozenset()) -> tuple[str, str]:
    """Логин из имени (как в списке людей): (логин, почему добавлен номер — «занято», «служебное» или пусто).
    В имени нет ни букв, ни цифр — логин пустой."""
    base = slug(display)
    if not base:
        return "", ""
    used = set(taken) | set(users.SYSTEM_USERS)
    if base not in used:
        return base, ""
    k = 2
    while _suffixed(base, k) in used:
        k += 1
    return _suffixed(base, k), "служебное" if base in users.SYSTEM_USERS else "занято"


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
        note, devices, unknown = split_devices(note)
        row = Row(n, raw.strip(), display, note[:NOTE_MAX], devices=devices, unknown=unknown)
        if note and not devices and _comma_cut(raw, note):
            row.hint = f"«{note}» — заметка; если это часть имени, уберите запятую"
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
        plan.rows.append(row)
    return plan


def plan_for_registry(text: str) -> Plan:
    """План по текущему реестру пользователей."""
    return build(text, set(users.list_users().names()))
