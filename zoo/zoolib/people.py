"""Список людей одним текстом (мастер «Подключить людей», «Добавить людей списком»).

Строка — «имя», «имя; заметка» или «имя; заметка; android, windows» (разделитель — первая «;», табуляция или
запятая; любое поле после имени из одних названий устройств — устройства человека, без них — как у группы). Столбцы
из Excel: строка заголовка («Имя | Отдел | Телефон») пропускается, «Фамилия | Имя» — склеиваются в имя, пустая клетка
устройства — подсказка «как у группы». Имя может быть
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
    "macos": "macos", "mac": "macos", "macbook": "macos", "мак": "macos", "макбук": "macos", "imac": "macos",
    "linux": "linux", "линукс": "linux", "ubuntu": "linux", "убунту": "linux",
}
FIELD_WORDS = 2   # в поле устройств слово-другое («айфон», «ноутбук асус»); длиннее — это заметка
# слова про устройство, по которым не понять, какое оно: предпросмотр спрашивает, а не молчит
AMBIGUOUS = {"телефон": "Android или iPhone?", "смартфон": "Android или iPhone?", "мобильный": "Android или iPhone?",
             "сотовый": "Android или iPhone?", "phone": "Android или iPhone?", "smartphone": "Android или iPhone?",
             "планшет": "Android или iPad?", "tablet": "Android или iPad?",
             # в офисе бывают и MacBook: «ноутбук» молча в Windows не превращаем
             "ноутбук": "Windows или Mac?", "ноут": "Windows или Mac?", "laptop": "Windows или Mac?",
             "notebook": "Windows или Mac?", "компьютер": "Windows или Mac?", "комп": "Windows или Mac?",
             "computer": "Windows или Mac?"}
# заголовок таблицы из Excel: первая клетка — «Имя», «ФИО», «Сотрудник»…; такую строку не заводим человеком
HEADER_NAMES = ("имя", "фио", "ф.и.о", "ф. и. о", "фамилия", "сотрудник", "сотрудники", "name", "full name", "employee")
# заголовки столбцов про устройства: пустая клетка в них — «устройство не указано»
DEVICE_HEADS = {"устройство", "устройства", "телефон", "компьютер", "ноутбук", "смартфон", "мобильный", "планшет", "пк",
                "ос", "платформа", "device", "devices", "phone", "computer", "laptop", "os", "platform"}
NAME_PARTS = {"фамилия", "имя", "отчество", "surname", "name", "first name", "last name"}
# названия устройств, которые бывают и фамилией или словом имени: в конце имени — только вопрос, имя не трогаем
NAMEY = {"мак", "mac", "apple", "хонор", "honor", "pixel", "poco", "realme", "tecno", "infinix", "win"}
DEVICES_HELP = "android, iphone, windows, mac, linux"

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
    header: str = ""     # пропущенная строка заголовка из Excel («Имя | Отдел | Телефон»)

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


def _device_like(piece: str) -> bool:
    """Поле про устройства: только названия устройств и слова вроде «телефон», по которым не понять, какое."""
    words = _words(piece)
    return bool(words) and all(w in DEVICE_WORDS or w in AMBIGUOUS for w in words)


def unknown_text(words: list[str]) -> str:
    """Чип предпросмотра: «не понял: телефон — Android или iPhone?», «не понял: кабинет 5 — устройство? android, …»."""
    parts = [f"{w} — {AMBIGUOUS[w]}" if w in AMBIGUOUS else w for w in words]
    tail = "" if all(w in AMBIGUOUS for w in words) else f" — устройство? {DEVICES_HELP}"
    return "не понял: " + ", ".join(parts) + tail


def _field_words(piece: str) -> tuple[list[str], list[str]]:
    """Поле из одних названий устройств («Android», «iPhone + MacBook», «ноутбук»): (устройства, слова-вопросы)."""
    words = _words(piece)
    return [DEVICE_WORDS[w] for w in words if w in DEVICE_WORDS], [w for w in words if w not in DEVICE_WORDS]


def split_devices(note: str, dev_cols: set[int] | frozenset[int] = frozenset()) -> tuple[str, list[str], list[str], bool]:
    """Заметка → (заметка, устройства, непонятые слова, есть ли пустое поле). Поля через «;» (столбцы из Excel): любое
    поле из одних названий устройств или слов вроде «телефон» — устройства, в каком бы столбце оно ни стояло («ИТ;
    Android; Windows»). Последнее поле, кроме того, разбирается хвостом через запятые («склад, android»); короткие
    незнакомые слова в нём («Оля; склад; хз») — вопрос, если устройств в других полях нет. dev_cols — номера полей
    заметки (с 0), которые заголовок таблицы назвал устройствами: незнакомое слово в них — тоже вопрос."""
    if ";" not in note:
        rest, found, unknown = _tail_devices(note, False)
        return rest, found, unknown, False
    fields = note.split(";")
    keep: list[str] = []
    found: list[str] = []
    unknown: list[str] = []
    empty = False
    last = len(fields) - 1
    for k, f in enumerate(fields):
        if not f.strip():
            empty = empty or k > 0 or len(fields) > 1
            continue
        if _device_like(f):
            devs, ask = _field_words(f)
            found += devs
            unknown += ask
            continue
        if k == last:
            rest, devs, ask = _tail_devices(f, not found)
            found += devs
            unknown += ask
            if rest:
                keep.append(rest.strip())
            continue
        if k in dev_cols and len(_words(f)) <= FIELD_WORDS:
            unknown += _words(f)
            continue
        keep.append(f.strip())
    return "; ".join(keep), list(dict.fromkeys(found)), list(dict.fromkeys(unknown)), empty and not found


def _tail_devices(note: str, ask: bool) -> tuple[str, list[str], list[str]]:
    """Одно поле: устройства — хвост через запятые, с первого названия устройства или слова вроде «телефон»; короткие
    слова там, которых мы не знаем («айфон, планшет»), возвращаются вопросом. ask — поле без единого знакомого слова,
    но короткое («хз»), тоже вопрос (третье поле строки, а устройств в других полях нет)."""
    parts = re.split(r"(,)", note)
    fields = parts[0::2]
    k = len(fields)
    while k > 0 and len(_words(fields[k - 1])) <= FIELD_WORDS and fields[k - 1].strip():
        k -= 1
    while k < len(fields) and not _device_like(fields[k]):   # поле устройств начинается со слова про устройство
        k += 1
    if k >= len(fields):
        short = _words(note)
        return note.strip(), [], (short if ask and short and len(short) <= FIELD_WORDS else [])
    found: list[str] = []
    unknown: list[str] = []
    for f in fields[k:]:
        devs, words = _field_words(f)
        found += devs
        unknown += words
    rest = "".join(parts[:max(2 * k - 1, 0)]).strip(" ,")
    return rest, list(dict.fromkeys(found)), list(dict.fromkeys(unknown))


def name_devices(display: str) -> tuple[str, list[str], str]:
    """«Анна Смирнова android» → («Анна Смирнова», ['android'], ""): названия устройств в конце имени (через пробел, без
    «;») — устройства. Слово, которое бывает и в имени («Иван Мак»), не трогается — возвращается вопросом."""
    words = display.split()
    found: list[str] = []
    while len(words) > 1:
        w = words[-1].casefold().strip(".,!")
        if w in NAMEY:
            return " ".join(words), found, words[-1] if not found else ""
        if w not in DEVICE_WORDS:
            break
        found.insert(0, DEVICE_WORDS[w])
        words.pop()
    return " ".join(words), list(dict.fromkeys(found)), ""


def _comma_cut(raw: str, note: str) -> str:
    """Строку разрезала запятая, а следом — одно слово с большой буквы: похоже на «Петров, Иван» из Excel (и с полями
    дальше: «Петров, Иван; продажи; android»). Возвращает это слово или пусто."""
    cuts = [i for i in (raw.find(s) for s in SEPARATORS) if i >= 0]
    first = re.split(r"[;\t,]", note, maxsplit=1)[0].strip()
    ok = bool(cuts) and raw[min(cuts)] == "," and bool(re.fullmatch(r"[A-ZА-ЯЁ][a-zа-яё-]+", first))
    return first if ok else ""


def _cells(line: str) -> list[str]:
    """Клетки строки: по табуляции (Excel), иначе по «;»."""
    sep = "\t" if "\t" in line else ";"
    return [c.strip().strip('"').strip() for c in line.split(sep)]


def _header(line: str) -> tuple[int, frozenset[int]] | None:
    """Строка заголовка из Excel («Имя | Отдел | Телефон | Компьютер»): (сколько первых столбцов — части имени, номера
    полей заметки — столбцов про устройства). Не заголовок — None."""
    cells = [c.casefold() for c in _cells(line)]
    first = cells[0].strip(" .:") if cells else ""
    if not first or not any(first == h or first.startswith(h + " ") for h in HEADER_NAMES):
        return None
    parts = 1
    while parts < len(cells) and cells[parts].strip(" .:") in NAME_PARTS and parts < 3:
        parts += 1
    devs = frozenset(i - parts for i, c in enumerate(cells) if i >= parts
                     and (c.strip(" .:") in DEVICE_HEADS or (_words(c) and all(w in DEVICE_HEADS or w in DEVICE_WORDS
                                                                              or w in AMBIGUOUS for w in _words(c)))))
    return parts, devs


def _join_name(line: str, parts: int) -> str:
    """«Петров⇥Иван⇥бух» при заголовке «Фамилия | Имя» → «Петров Иван⇥бух»."""
    if parts < 2 or "\t" not in line:
        return line
    cells = line.split("\t")
    return "\t".join([" ".join(c.strip() for c in cells[:parts] if c.strip()), *cells[parts:]])


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
    head = _header(lines[0]) if lines else None
    parts, dev_cols = head or (1, frozenset())
    for n, raw in enumerate(lines, 1):
        if n == 1 and head is not None:
            plan.header = raw.strip()
            continue
        display, note = split_line(_join_name(raw, parts))
        comma = _comma_cut(raw, note) if parts == 1 else ""
        note, devices, unknown, empty = split_devices(note, dev_cols)
        display, tail, ask = (display, [], "") if devices else name_devices(display)
        row = Row(n, raw.strip(), display, note[:NOTE_MAX], devices=devices or tail, unknown=unknown)
        hints = []
        if tail:
            hints.append(f"«{device_titles(tail)}» из конца имени — устройство, не имя")
        elif ask:
            hints.append(f"«{ask}» в конце имени — устройство? Тогда через «;»: «Имя; ; {ask}»")
        if comma:
            hints.append(f"«{comma}» — заметка; если это часть имени, уберите запятую")
        if not row.devices and not unknown and (dev_cols or (empty and "\t" in raw)):
            hints.append("устройство не указано — будут устройства группы")
        row.hint = "; ".join(hints)
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
