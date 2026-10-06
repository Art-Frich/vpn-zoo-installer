"""Общий компонент таблиц (BACKLOG п. 12): страница описывает таблицу, а не верстает её.

    Spec + Col      колонки и их свойства (сортировка, поиск, чипы, второй план, приватность)
    parse()         query → State: только значения из белых списков, мусор молча отбрасывается
    sql_page()      keyset-выдача из SQLite (ORDER BY (ключ, id) и «после курсора», без OFFSET)
    memory_page()   то же для списков в памяти (пользователи, версии): фильтр, поиск, сортировка, курсор
    render()        поиск, чипы, «показано N из M», заголовки-ссылки сортировки, «показать ещё»
    download()      CSV/JSON выборки; приватные колонки — только с явным priv=1

Всё состояние — в query-строке (у таблицы может быть префикс: на странице их несколько).
Значения идут в SQL только параметрами; выражения колонок — константы кода, не ввод.
Без JS всё работает обычными ссылками и GET-формой; с JS — подмена <main> (data-swap) и
дописывание строк (data-more, см. assets.py).
"""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.parse import urlencode

from .html import Markup, empty, join, t

if TYPE_CHECKING:
    from .app import Response

ROWS = (15, 50, 100)
QUERY_MAX = 100
TOKENS_MAX = 6
CHIP_MAX = 12          # больше разных значений — это не колонка для чипов
CHIP_LEN = 60
CURSOR_MAX = 120
EXPORT_MAX = 5000
FORMATS = ("csv", "json")
DASH = "—"
SEARCH_HINT = "поиск"


@dataclass(frozen=True)
class Col:
    key: str
    title: str
    cell: Callable[[Any], Any] | None = None    # строка → ячейка (Markup или текст); нет — текст значения
    value: Callable[[Any], Any] | None = None   # строка → простое значение (поиск, сортировка, выгрузка)
    sql: str = ""                               # выражение для SQL-таблицы; сортируемые — без NULL
    num: bool = False                           # число: сортировка и курсор числом, вправо
    left: bool = False                          # число, но влево (время)
    sort: bool = False
    search: bool = False
    chip: bool = False                          # мало разных значений: фильтр-чипы
    secondary: bool = False                     # на телефоне прячется и уходит в подробности
    private: bool = False                       # не выгружается без priv=1
    first_desc: bool = False                    # первый клик по заголовку — по убыванию
    hint: str = ""
    label: Callable[[str], str] | None = None   # подпись значения на чипе
    find: Callable[[Any], str] | None = None    # ещё текст для поиска (заметка рядом с именем)
    export: bool = True                         # False — служебная колонка (полоса, кнопка)
    hidden: bool = False                        # не столбец: только чипы, поиск и выгрузка


@dataclass
class Spec:
    path: str
    cols: list[Col]
    sort: str
    desc: bool = False
    prefix: str = ""
    id_key: str = "id"
    id_sql: str = ""
    href: Callable[[Any], str | None] | None = None   # строка → адрес подробностей (клик по строке)
    detail: Callable[[Any], Any] | None = None        # строка → раскрывающиеся подробности
    row_cls: Callable[[Any], str | None] | None = None   # строка → класс <tr> (приглушить отключённых)
    empty: str = "пусто"
    empty_filtered: str = "Ничего не нашлось"
    placeholder: str = SEARCH_HINT
    keep: list[tuple[str, str]] = field(default_factory=list)   # параметры страницы, что ссылки сохраняют
    paged: bool = True
    export: bool = True
    name: str = "table"

    def col(self, key: str) -> Col | None:
        return next((c for c in self.cols if c.key == key), None)

    @property
    def searchable(self) -> bool:
        return any(c.search for c in self.cols)


@dataclass(frozen=True)
class State:
    q: str
    sort: str
    desc: bool
    n: int
    after: str
    filters: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> tuple[Any, ...]:
        """Нормализованное состояние: ключ кэша страницы (мусор из адреса в него не попадает)."""
        return (self.q, self.sort, self.desc, self.n, self.after, self.filters)

    @property
    def filtered(self) -> bool:
        return bool(self.q or self.filters)


@dataclass
class Page:
    rows: list[Any]
    total: int             # строк по фильтрам и поиску (всего, не на странице)
    before: int            # сколько строк выдачи до этой страницы
    next: str | None       # курсор следующей страницы
    options: dict[str, list[tuple[str, int]]] = field(default_factory=dict)   # чипы: значение, сколько


Options = dict[str, list[tuple[str, int]]]


# ---------- разбор адреса ----------

def parse(spec: Spec, query: dict[str, str], options: Options | None = None) -> State:
    """Query → State. sort — из колонок с sort, фильтр — из значений, что есть в options (чипы), n — из ROWS."""
    p = spec.prefix

    def g(name: str) -> str:
        v = query.get(p + name, "")
        return v if isinstance(v, str) else ""

    q = " ".join(g("q").split())[:QUERY_MAX] if spec.searchable else ""
    raw = g("sort")
    key = raw[1:] if raw.startswith("-") else raw
    col = spec.col(key)
    if col is not None and col.sort:
        sort, desc = key, raw.startswith("-")
    else:
        sort, desc = spec.sort, spec.desc
    try:
        n = int(g("n"))
    except ValueError:
        n = ROWS[0]
    filters = []
    for c in spec.cols:
        if not c.chip:
            continue
        v = g("f_" + c.key)[:CHIP_LEN]
        if v and options is not None and any(v == o for o, _ in options.get(c.key, ())):
            filters.append((c.key, v))
    return State(q, sort, desc, n if n in ROWS else ROWS[0], g("after")[:CURSOR_MAX], tuple(filters))


def export_request(spec: Spec, query: dict[str, str]) -> tuple[str, bool] | None:
    """(формат, с приватными полями) или None, если выгрузку не просили."""
    if not spec.export:
        return None
    fmt = query.get(spec.prefix + "export", "")
    return (fmt, query.get(spec.prefix + "priv") == "1") if fmt in FORMATS else None


def href(spec: Spec, st: State, **over: Any) -> str:
    """Ссылка на страницу с тем же состоянием, кроме over (q, sort, desc, n, after, filters; export и priv — как есть).
    Значения по умолчанию в адрес не попадают."""
    export, priv = over.pop("export", None), over.pop("priv", False)
    st = replace(st, **over)
    p = spec.prefix
    args = list(spec.keep)
    if st.q:
        args.append((p + "q", st.q))
    if (st.sort, st.desc) != (spec.sort, spec.desc):
        args.append((p + "sort", ("-" if st.desc else "") + st.sort))
    if st.n != ROWS[0]:
        args.append((p + "n", str(st.n)))
    args += [(p + "f_" + k, v) for k, v in st.filters]
    if st.after:
        args.append((p + "after", st.after))
    if export:
        args.append((p + "export", export))
        if priv:
            args.append((p + "priv", "1"))
    return spec.path + ("?" + urlencode(args) if args else "")


def with_filter(st: State, key: str, value: str | None) -> State:
    rest = tuple((k, v) for k, v in st.filters if k != key)
    return replace(st, filters=rest + (((key, value),) if value else ()), after="")


# ---------- значения ----------

def value_of(col: Col, row: Any) -> Any:
    return col.value(row) if col.value else row.get(col.key)


def text_of(col: Col, row: Any) -> str:
    v = value_of(col, row)
    return "" if v is None else str(v)


def _fold(v: Any) -> Any:
    return v.casefold() if isinstance(v, str) else v


def _like(text: str) -> list[str]:
    return text.casefold().split()[:TOKENS_MAX]


# ---------- SQL: keyset ----------

def _cursor(col: Col, text: str) -> tuple[Any, str] | None:
    """«значение|id»; мусор или значение не того типа — как «с начала»."""
    val, _, rid = text.rpartition("|")
    if not rid or not rid.lstrip("-").isdigit():
        return None
    if col.num:
        try:
            num = float(val)
        except ValueError:
            return None
        return (num, rid) if math.isfinite(num) else None
    return val, rid


def _cursor_text(value: Any, rid: Any) -> str:
    return f"{value}|{rid}"


def sql_options(con: sqlite3.Connection, spec: Spec, from_: str, where: str = "1", args: Iterable[Any] = ()) -> Options:
    """Значения чип-колонок (в пределах базового условия) с числом строк; колонка с более чем CHIP_MAX
    значений чипов не получает. Пустые значения не в счёт."""
    out: Options = {}
    for c in spec.cols:
        if not (c.chip and c.sql):
            continue
        rows = con.execute(f"SELECT {c.sql} AS v, COUNT(*) AS n FROM {from_} WHERE {where} GROUP BY v "
                           f"HAVING v IS NOT NULL AND v != '' ORDER BY n DESC, v LIMIT {CHIP_MAX + 1}",
                           list(args)).fetchall()
        if 2 <= len(rows) <= CHIP_MAX:
            out[c.key] = [(str(r[0]), int(r[1])) for r in rows]
    return out


def sql_page(con: sqlite3.Connection, spec: Spec, st: State, from_: str, where: str = "1", args: Iterable[Any] = (),
             extra_select: Iterable[str] = (), options: Options | None = None) -> Page:
    """Страница выдачи: условия и порядок — из белых списков spec, значения — параметрами. Строки — dict по
    ключам колонок плюс «id». st.n — размер страницы (для выгрузки — EXPORT_MAX)."""
    args = list(args)
    sort = spec.col(st.sort)
    assert sort is not None and sort.sql and spec.id_sql
    con.create_function("zfold", 1, _fold, deterministic=True)  # lower() в SQLite знает только ASCII
    conds, cargs = [where], list(args)
    for k, v in st.filters:
        c = spec.col(k)
        assert c is not None and c.sql
        conds.append(f"{c.sql} = ?")
        cargs.append(v)
    for tok in _like(st.q):
        parts = [f"instr(zfold(CAST({c.sql} AS TEXT)), ?) > 0" for c in spec.cols if c.search and c.sql]
        conds.append("(" + " OR ".join(parts) + ")")
        cargs += [tok] * len(parts)
    base = " AND ".join(f"({c})" for c in conds)
    total = con.execute(f"SELECT COUNT(*) FROM {from_} WHERE {base}", cargs).fetchone()[0]

    e, i = sort.sql, spec.id_sql
    order = "DESC" if st.desc else "ASC"
    fwd, back = ("<", ">") if st.desc else (">", "<")
    cur = _cursor(sort, st.after) if st.after else None
    cond, before, qargs = base, 0, list(cargs)
    if cur:
        pos = [cur[0], cur[0], int(cur[1])]
        cond = f"({base}) AND ({e} {fwd} ? OR ({e} = ? AND {i} {fwd} ?))"
        qargs += pos
        # курсор — последняя показанная строка: до неё и она сама — «не после курсора»
        before = con.execute(f"SELECT COUNT(*) FROM {from_} WHERE ({base}) AND "
                             f"({e} {back} ? OR ({e} = ? AND {i} {back}= ?))", [*cargs, *pos]).fetchone()[0]
    cols = [f"{c.sql} AS {c.key}" for c in spec.cols if c.sql]
    sel = ", ".join([f"{i} AS id", f"{e} AS _k", *cols, *extra_select])
    cur_ = con.execute(f"SELECT {sel} FROM {from_} WHERE {cond} ORDER BY {e} {order}, {i} {order} LIMIT {st.n + 1}", qargs)
    names = [d[0] for d in cur_.description]
    rows = [dict(zip(names, r)) for r in cur_.fetchall()]
    nxt = None
    if len(rows) > st.n:
        rows = rows[:st.n]
        nxt = _cursor_text(rows[-1]["_k"], rows[-1]["id"])
    return Page(rows, int(total), int(before), nxt, options or {})


# ---------- список в памяти ----------

def options_from_rows(spec: Spec, rows: list[Any]) -> Options:
    out: Options = {}
    for c in spec.cols:
        if not c.chip:
            continue
        seen: dict[str, int] = {}
        for r in rows:
            v = text_of(c, r)
            if v:
                seen[v] = seen.get(v, 0) + 1
        if 2 <= len(seen) <= CHIP_MAX:
            out[c.key] = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    return out


def _sort_key(col: Col, row: Any) -> tuple[int, Any]:
    v = value_of(col, row)
    if v is None or v == "":
        return (0, 0 if col.num else "")
    if col.num:
        try:
            return (1, float(v))
        except (TypeError, ValueError):
            return (0, 0)
    return (1, str(v).casefold())


def memory_page(spec: Spec, st: State, rows: list[Any], options: Options | None = None) -> Page:
    """Фильтр, поиск, сортировка и курсор (id последней показанной строки) для списка в памяти."""
    sort = spec.col(st.sort)
    assert sort is not None
    keep = rows
    for k, v in st.filters:
        c = spec.col(k)
        keep = [r for r in keep if c is not None and text_of(c, r) == v]
    cols = [c for c in spec.cols if c.search]
    for tok in _like(st.q):
        keep = [r for r in keep if any(tok in (text_of(c, r) + " " + (c.find(r) if c.find else "")).casefold()
                                       for c in cols)]
    ordered = sorted(keep, key=lambda r: str(r.get(spec.id_key, "")))
    ordered = sorted(ordered, key=lambda r: _sort_key(sort, r), reverse=st.desc)
    start = 0
    if st.after:
        ids = [str(r.get(spec.id_key, "")) for r in ordered]
        start = ids.index(st.after) + 1 if st.after in ids else 0
    page = ordered[start:start + st.n] if spec.paged else ordered
    more = spec.paged and start + st.n < len(ordered)
    nxt = str(page[-1].get(spec.id_key, "")) if more and page else None
    return Page(page, len(ordered), start, nxt, options or {})


# ---------- разметка ----------

def link(text: Any, to: str) -> Markup:
    """Ссылка-ячейка: переход без перезагрузки; сама строка тоже кликабельна (JS), а без JS работает ссылка."""
    return t("a", text, href=to, data_swap=True)


def _chip(label: str, to: str, on: bool, title: str | None = None) -> Markup:
    return t("a", label, href=to, class_="chip info" if on else "chip", data_swap=True,
             aria_pressed="true" if on else None, title=title)


def _toolbar(spec: Spec, st: State, options: Options) -> Markup | None:
    parts: list[Any] = []
    if spec.searchable:
        hidden = [t("input", type="hidden", name=k, value=v) for k, v in spec.keep]
        hidden += [t("input", type="hidden", name=spec.prefix + "f_" + k, value=v) for k, v in st.filters]
        if (st.sort, st.desc) != (spec.sort, spec.desc):
            hidden.append(t("input", type="hidden", name=spec.prefix + "sort", value=("-" if st.desc else "") + st.sort))
        if st.n != ROWS[0]:
            hidden.append(t("input", type="hidden", name=spec.prefix + "n", value=st.n))
        reset = (t("a", "сбросить", href=href(spec, st, q="", filters=(), after=""), class_="btn small", data_swap=True)
                 if st.filtered else None)
        parts.append(t("form", t("input", type="search", name=spec.prefix + "q", value=st.q, placeholder=spec.placeholder,
                                 maxlength=QUERY_MAX, autocomplete="off", aria_label="Поиск по таблице"),
                       hidden, t("button", "Найти", type="submit", class_="btn small"), reset,
                       method="get", action=spec.path, class_="search", data_get=True))
    elif st.filters:
        parts.append(t("p", t("a", "сбросить", href=href(spec, st, filters=(), after=""), class_="btn small",
                              data_swap=True), class_="more"))
    for c in spec.cols:
        opts = options.get(c.key)
        if not opts:
            continue
        on = dict(st.filters).get(c.key)
        chips = [_chip(c.label(v) if c.label else v, href(spec, with_filter(st, c.key, None if v == on else v)),
                       v == on, f"{c.title}: {n}") for v, n in opts]
        parts.append(t("div", t("span", c.title, class_="hint") if c.title else None, chips, class_="chips filters"))
    return join(parts) if parts else None


def _th(spec: Spec, st: State, c: Col) -> Markup:
    cls = " ".join(x for x in ("num" if c.num and not c.left else "", "sec" if c.secondary else "") if x) or None
    if not c.sort:
        return t("th", c.title, class_=cls, scope="col", title=c.hint or None)
    here = st.sort == c.key
    desc_next = (not st.desc) if here else (c.num and not c.left) or c.first_desc
    return t("th", t("a", c.title, href=href(spec, st, sort=c.key, desc=desc_next, after=""), data_swap=True,
                     class_="sortlink"),
             class_=cls, scope="col", title=c.hint or None,
             aria_sort=("descending" if st.desc else "ascending") if here else None)


def _inner(c: Col, row: Any) -> Any:
    if c.cell:
        return c.cell(row)
    return text_of(c, row) or DASH


def _td_class(c: Col) -> str | None:
    return " ".join(x for x in ("num" if c.num and not c.left else "", "sec" if c.secondary else "") if x) or None


def _row(spec: Spec, row: Any) -> list[Markup]:
    to = spec.href(row) if spec.href else None
    shown = [c for c in spec.cols if not c.hidden]
    sec = [c for c in shown if c.secondary]
    det = not to and bool(spec.detail or sec)
    cells = []
    for n, c in enumerate(shown):
        # кнопка раскрытия — в первой ячейке: доступна с клавиатуры, жестов не требует
        exp = (t("button", type="button", class_="exp", aria_expanded="false", aria_label="Подробности",
                 title="Подробности")
               if det and n == 0 else None)
        cells.append(t("td", exp, _inner(c, row), class_=_td_class(c)))
    tr = t("tr", cells, data_href=to, data_row=True if det else None, class_=spec.row_cls(row) if spec.row_cls else None)
    if not det:
        return [tr]
    kv = t("dl", [[t("dt", c.title), t("dd", _inner(c, row))] for c in sec], class_="kv sec-kv") if sec else None
    body = t("td", kv, spec.detail(row) if spec.detail else None, colspan=len(shown))
    return [tr, t("tr", body, class_="det", hidden=True)]


def _count(spec: Spec, st: State, page: Page) -> Markup | None:
    if not (spec.paged or st.filtered):
        return None
    shown = page.before + len(page.rows)
    return t("span", f"показано {shown} из {page.total}", data_count=True)


def _exports(spec: Spec, st: State, private: bool) -> Markup | None:
    if not spec.export:
        return None
    links = [t("a", f.upper(), href=href(spec, st, after="", export=f), title="Выгрузить выбранное (всё по фильтрам)")
             for f in FORMATS]
    if private:
        links.append(t("a", "CSV с адресами", href=href(spec, st, after="", export="csv", priv=True),
                       title="Вместе с приватными полями (IP): не отправляйте выгрузку наружу"))
    return t("span", "выгрузка: ", links, class_="exports")


def render(spec: Spec, st: State, page: Page) -> Markup:
    """Панель поиска и чипов, число строк, таблица, «показать ещё». Вместо пустой таблицы — пустое состояние."""
    toolbar = _toolbar(spec, st, page.options)
    box_id = "t-" + (spec.prefix.strip("_") or "main")
    if not page.rows:
        text = spec.empty_filtered if st.filtered else spec.empty
        return join(toolbar, t("div", empty(text), id=box_id, data_more_box=True))
    head = t("tr", [_th(spec, st, c) for c in spec.cols if not c.hidden])
    body = [tr for r in page.rows for tr in _row(spec, r)]
    count, exports = _count(spec, st, page), _exports(spec, st, any(c.private for c in spec.cols))
    more = (t("p", t("a", "показать ещё", href=href(spec, st, after=page.next), class_="btn small", data_more=True),
              class_="more") if page.next else None)
    start = (t("p", t("a", "← с начала", href=href(spec, st, after=""), data_swap=True), class_="more")
             if st.after else None)
    meta = t("p", count, " " if count and exports else None, exports, class_="hint count") if count or exports else None
    return join(toolbar, t("div", meta, start,
                           t("div", t("table", t("thead", head), t("tbody", body), class_="dt"), class_="table-wrap"),
                           more, id=box_id, data_more_box=True))


# ---------- выгрузка ----------

def _plain(v: Any) -> Any:
    if isinstance(v, Markup):
        return str(v)
    return v


def _csv_safe(v: Any) -> Any:
    """Ячейка, что Excel примет за формулу, получает ведущий апостроф."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def export_rows(cols: list[Col], rows: Iterable[Any], private: bool = False) -> tuple[list[Col], list[dict[str, Any]]]:
    keep = [c for c in cols if c.export and (private or not c.private)]
    return keep, [{c.key: _plain(value_of(c, r)) for c in keep} for r in rows]


def download(cols: list[Col], rows: Iterable[Any], fmt: str, private: bool = False,
             name: str = "table") -> tuple[bytes, str, str]:
    """→ (тело, content-type, имя файла). Приватные колонки без private не попадают ни в CSV, ни в JSON."""
    keep, data = export_rows(cols, rows, private)
    if fmt == "json":
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        return body, "application/json; charset=utf-8", f"{name}.json"
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([_csv_safe(c.title) for c in keep])
    for d in data:
        w.writerow([_csv_safe("" if d[c.key] is None else d[c.key]) for c in keep])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8", f"{name}.csv"


def memory_export(spec: Spec, query: dict[str, str], rows: list[Any]) -> "Response | None":
    """Выгрузка списка в памяти по текущим фильтрам; None — выгрузку не просили."""
    ex = export_request(spec, query)
    if ex is None:
        return None
    st = replace(parse(spec, query, options_from_rows(spec, rows)), after="", n=EXPORT_MAX)
    page = memory_page(replace(spec, paged=False), st, rows)
    return export_response(spec.cols, page.rows, ex[0], ex[1], spec.name)


def export_response(cols: list[Col], rows: Iterable[Any], fmt: str, private: bool = False,
                    name: str = "table") -> "Response":
    from .app import Response
    body, ctype, fname = download(cols, rows, fmt, private, name)
    return Response(200, body, ctype, headers=[("Content-Disposition", f'attachment; filename="{fname}"')])
