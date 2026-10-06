"""Чтение и поиск по логам для админки: файлы (смещения в байтах) и journald (курсоры).

Файл читается кусками с любого места, journald — `journalctl --cursor/--after-cursor` с `-n`:
ни то ни другое не грузит журнал целиком. Каждый вызов ограничен числом строк, байтов и временем;
journalctl при превышении убивается. Маскировку секретов делает вызывающий (`clean`): поиск сверяет
шаблон с уже очищенной строкой, поэтому по найденному нельзя угадать скрытое значение.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import re._parser as _sre
except ImportError:  # Python 3.10
    import sre_parse as _sre  # type: ignore[no-redef]

CHUNK = 300                 # строк на страницу по умолчанию
CHUNK_MAX = 2000
LINE_MAX = 4000             # длиннее — обрезается при чтении (память и ширина страницы)
BLOCK = 64 * 1024
READ_MAX = 8 * 1024 * 1024  # назад по файлу за один вызов — не больше
EXPORT_LINES = 20000
EXPORT_BYTES = 8 * 1024 * 1024
JOURNAL_SECONDS = 20.0

QUERY_MAX = 120
SEARCH_SECONDS = 8.0
SCAN_ENTRIES = 300_000      # строк (записей) за один поиск
SCAN_BYTES = 64 * 1024 * 1024
HITS = (200, 500, 1000)
SNIPPET = 600
SPANS_MAX = 30
RX_LINE = 1000              # строк длиннее regex видит только начало
REPEAT_CAP = 50             # «неограниченным» считается повтор с верхней границей больше этой

CURSOR_RE = re.compile(r"^s=[0-9a-f]+;i=[0-9a-f]+;b=[0-9a-f]+;m=[0-9a-f]+;t=([0-9a-f]+);x=[0-9a-f]+\Z")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.log\Z")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@:._-]{0,100}\Z")
STAMP_RE = re.compile(r"install-(\d{8})-(\d{6})\.log\Z")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class LogError(Exception):
    """Лог не прочитать или запрос не принят; текст — по-русски, для человека."""


@dataclass
class Line:
    text: str
    tok: str                  # смещение строки в файле или курсор journald
    ts: float | None = None


@dataclass
class Chunk:
    lines: list[Line]
    older: str | None = None  # токен «раньше этой страницы»; None — раньше ничего нет
    newer: str | None = None  # токен «позже» (ссылка под логом); None — это конец
    tip: str = ""             # откуда дочитывать новое (after=): позиция после последней показанной строки


def log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR", "/var/log/vpn-zoo"))


def log_files(limit: int = 60) -> list[Path]:
    d = log_dir()
    try:
        files = [f for f in d.iterdir() if FILE_RE.match(f.name) and f.is_file() and not f.is_symlink()]
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []


def file_path(name: str) -> Path:
    if not FILE_RE.match(name):
        raise LogError("недопустимое имя файла")
    return log_dir() / name


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").rstrip("\r\n")
    return text if len(text) <= LINE_MAX else text[:LINE_MAX] + "…"


def file_start(path: Path) -> float:
    """Когда файл начат: из имени install-ГГГГММДД-ЧЧММСС.log, иначе время изменения."""
    m = STAMP_RE.search(path.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------- файлы ----------

def lines_before(path: Path, end: int | None, n: int) -> Chunk:
    """До n строк, что заканчиваются перед смещением end (None — конец файла). end — начало строки."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            end = size if end is None else max(0, min(end, size))
            pos, buf, nl = end, b"", 0
            while pos > 0 and nl <= n and end - pos < READ_MAX:
                step = min(BLOCK, pos)
                pos -= step
                f.seek(pos)
                block = f.read(step)
                nl += block.count(b"\n")
                buf = block + buf
    except OSError as e:
        raise LogError(f"не прочитать {path.name}: {e.strerror or e}") from None
    base = pos
    if pos > 0:  # первая строка могла обрезаться: отбрасываем её до перевода строки
        cut = buf.find(b"\n") + 1
        base, buf = pos + cut, buf[cut:]
    parts = buf.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    out, off = [], base
    for p in parts:
        out.append(Line(_decode(p), str(off)))
        off += len(p) + 1
    out = out[-n:]
    older = out[0].tok if out and int(out[0].tok) > 0 else None
    return Chunk(out, older, str(end) if end < size else None, str(end))


def lines_from(path: Path, start: int, n: int) -> Chunk:
    """До n строк начиная со смещения start (начало строки)."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            start = max(0, min(start, size))
            f.seek(start)
            off, out = start, []
            while len(out) < n:
                raw = f.readline(BLOCK)
                if not raw:
                    break
                out.append(Line(_decode(raw), str(off)))
                off += len(raw)
    except OSError as e:
        raise LogError(f"не прочитать {path.name}: {e.strerror or e}") from None
    return Chunk(out, str(start) if start > 0 else None, str(off) if off < size else None, str(off))


def file_around(path: Path, at: int, n: int) -> Chunk:
    """Окно вокруг строки со смещением at: примерно n/2 строк до и после."""
    half = max(1, n // 2)
    before = lines_before(path, at, half)
    after = lines_from(path, at, half + 1)
    return Chunk(before.lines + after.lines, before.older if before.lines else (str(at) if at > 0 else None),
                 after.newer, after.tip)


def file_tail(path: Path, n: int) -> Chunk:
    return lines_before(path, None, n)


# ---------- journald ----------

def _journalctl_argv(args: list[str], fields: str) -> list[str]:
    return ["journalctl", "--no-pager", "-q", "-o", "json", f"--output-fields={fields}", *args]


def _popen(argv: list[str]) -> Any:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def journal_entries(args: list[str], fields: str = "MESSAGE,SYSLOG_IDENTIFIER", *,
                    seconds: float = JOURNAL_SECONDS, max_entries: int | None = None,
                    max_bytes: int | None = None, state: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Записи journalctl по одной. Лишнее по числу, байтам или времени обрывается (процесс убивается);
    причина — в state["stopped"]. Не нашлось journalctl или он упал — LogError."""
    st = state if state is not None else {}
    try:
        proc = _popen(_journalctl_argv(args, fields))
    except FileNotFoundError:
        raise LogError("journalctl недоступен") from None
    except OSError as e:
        raise LogError(f"journalctl не запустился: {e}") from None
    timer = threading.Timer(seconds, lambda: (st.setdefault("stopped", "time"), proc.kill()))
    timer.daemon = True
    timer.start()
    seen = size = 0
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            size += len(raw)
            seen += 1
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e
            if max_entries is not None and seen >= max_entries:
                st.setdefault("stopped", "entries")
                break
            if max_bytes is not None and size >= max_bytes:
                st.setdefault("stopped", "bytes")
                break
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        err = b""
        try:
            if proc.stderr is not None:
                err = proc.stderr.read(2000) or b""
        except (OSError, ValueError):
            pass
        proc.wait()
        st["rc"] = proc.returncode
        st["err"] = err.decode("utf-8", "replace").strip()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    if "stopped" not in st and proc.returncode not in (0, None) and st["err"]:
        raise LogError(st["err"][:300])


def _field(e: dict[str, Any], key: str) -> str:
    v = e.get(key)
    if isinstance(v, list):  # бинарное значение приходит списком байтов
        try:
            return bytes(v).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return "" if v is None else str(v)


def entry_ts(e: dict[str, Any]) -> float | None:
    try:
        return int(_field(e, "__REALTIME_TIMESTAMP")) / 1_000_000
    except ValueError:
        return None


def entry_body(e: dict[str, Any]) -> str:
    """«программа: сообщение» без времени (время — отдельная колонка)."""
    msg = " ".join(_field(e, "MESSAGE").split("\n")) if "\n" in _field(e, "MESSAGE") else _field(e, "MESSAGE")
    ident = _field(e, "SYSLOG_IDENTIFIER")
    body = f"{ident}: {msg}" if ident else msg
    return body if len(body) <= LINE_MAX else body[:LINE_MAX] + "…"


def fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def entry_line(e: dict[str, Any]) -> Line:
    ts = entry_ts(e)
    return Line((fmt_ts(ts) + " " if ts else "") + entry_body(e), _field(e, "__CURSOR"), ts)


def cursor_ts(cursor: str) -> float | None:
    m = CURSOR_RE.match(cursor)
    return int(m.group(1), 16) / 1_000_000 if m else None


def check_cursor(cursor: str) -> str:
    if not CURSOR_RE.match(cursor):
        raise LogError("недопустимая позиция в журнале")
    return cursor


def check_unit(unit: str) -> str:
    if not UNIT_RE.match(unit):
        raise LogError("недопустимое имя сервиса")
    return unit


def _jlines(args: list[str], n: int, *, drop: str | None = None, newest_first: bool,
            upto: float | None = None) -> tuple[list[Line], bool]:
    """Прочитать до n записей (+1, чтобы знать, есть ли ещё). → (строки в хронологии, есть ли ещё)."""
    out: list[Line] = []
    for e in journal_entries(args, max_entries=n + 3):
        line = entry_line(e)
        if drop and line.tok == drop:
            continue
        if upto is not None and line.ts is not None and line.ts > upto + 1e-3:
            continue  # journalctl взял запись «после» опорной: лишнюю отбрасываем
        out.append(line)
        if len(out) > n:
            break
    more = len(out) > n
    out = out[:n]
    return (out[::-1] if newest_first else out), more


def journal_tail(unit: str, n: int) -> Chunk:
    lines, more = _jlines(["-u", check_unit(unit), "-r", "-n", str(n + 3)], n, newest_first=True)
    return Chunk(lines, lines[0].tok if lines and more else None, None, lines[-1].tok if lines else "")


def journal_before(unit: str, cursor: str, n: int, include: bool = False) -> Chunk:
    """Записи раньше курсора (include — вместе с ним)."""
    check_cursor(cursor)
    lines, more = _jlines(["-u", check_unit(unit), "-r", "-n", str(n + 3), "--cursor", cursor], n,
                          drop=None if include else cursor, newest_first=True, upto=cursor_ts(cursor))
    tip = lines[-1].tok if lines else cursor
    return Chunk(lines, lines[0].tok if lines and more else None, tip, tip)


def journal_after(unit: str, cursor: str, n: int) -> Chunk:
    check_cursor(cursor)
    lines, more = _jlines(["-u", check_unit(unit), "--after-cursor", cursor, "-n", str(n + 3)], n, newest_first=False)
    tip = lines[-1].tok if lines else cursor
    return Chunk(lines, None, tip if more else None, tip)


def journal_around(unit: str, cursor: str, n: int) -> Chunk:
    half = max(1, n // 2)
    before = journal_before(unit, cursor, half + 1, include=True)
    after = journal_after(unit, cursor, half)
    return Chunk(before.lines + after.lines, before.older, after.newer, after.tip)


# ---------- страница: единая точка входа ----------

def view(kind: str, name: str, *, before: str = "", after: str = "", at: str = "", n: int = CHUNK) -> Chunk:
    """Страница лога. Без токенов — конец (живой хвост); before — раньше токена; after — позже; at — вокруг строки."""
    n = max(20, min(CHUNK_MAX, n))
    if kind == "file":
        path = file_path(name)
        if before:
            return lines_before(path, _off(before), n)
        if after:
            return lines_from(path, _off(after), n)
        if at:
            return file_around(path, _off(at), n)
        return file_tail(path, n)
    if kind == "unit":
        if before:
            return journal_before(name, before, n)
        if after:
            return journal_after(name, after, n)
        if at:
            return journal_around(name, at, n)
        return journal_tail(name, n)
    raise LogError("неизвестный источник")


def _off(tok: str) -> int:
    if not tok.isdigit() or len(tok) > 15:
        raise LogError("недопустимая позиция в файле")
    return int(tok)


def export(kind: str, name: str) -> list[Line]:
    """Конец лога для выгрузки: не больше EXPORT_LINES строк и EXPORT_BYTES байт."""
    if kind == "file":
        lines = lines_before(file_path(name), None, EXPORT_LINES).lines
    else:
        lines = []
        for e in journal_entries(["-u", check_unit(name), "-r", "-n", str(EXPORT_LINES)], max_entries=EXPORT_LINES,
                                 max_bytes=EXPORT_BYTES):
            lines.append(entry_line(e))
        lines.reverse()
    total, keep = 0, []
    for line in reversed(lines):
        total += len(line.text) + 1
        if total > EXPORT_BYTES:
            break
        keep.append(line)
    return keep[::-1]


# ---------- поиск ----------

def _walk(p: Any, unbounded: bool = False, wide: list[int] | None = None) -> None:
    """Отказ шаблонам, на которых re уходит в экспоненту или в степень: вложенные повторы, обратные ссылки,
    развилки внутри повтора, больше двух «широких» повторов (.* и т. п.). re не прерывается по времени,
    а зависший поиск остановил бы всю админку, поэтому проверка до запуска, а строки обрезаны до RX_LINE."""
    wide = wide if wide is not None else [0]
    for op, av in p:
        name = str(op)
        if name in ("GROUPREF", "GROUPREF_EXISTS"):
            raise LogError("обратные ссылки в шаблоне не поддерживаются")
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            _lo, hi, sub = av
            big = hi > REPEAT_CAP
            if big and unbounded:
                raise LogError("вложенные повторы (например, (a+)+) не поддерживаются")
            if big and any(str(o) == "ANY" or (str(o) == "IN" and any(str(x) in ("NEGATE", "CATEGORY") for x, _ in a))
                           for o, a in sub):
                wide[0] += 1
                if wide[0] > 2:
                    raise LogError("слишком много «.*» в выражении — уточните")
            _walk(sub, unbounded or big, wide)
        elif name == "SUBPATTERN":
            _walk(av[-1], unbounded, wide)
        elif name == "BRANCH":
            alts = av[1]
            if unbounded:
                first = [alt[0] for alt in alts if len(alt)]
                distinct = len({a for _, a in first}) == len(first)
                if len(first) != len(alts) or not distinct or any(str(o) != "LITERAL" for o, _ in first):
                    raise LogError("«или» внутри повтора не поддерживается — вынесите его или используйте [ab]")
            for alt in alts:
                _walk(alt, unbounded, wide)
        elif name in ("ASSERT", "ASSERT_NOT"):
            _walk(av[-1], unbounded, wide)
        elif name == "ATOMIC_GROUP":
            _walk(av, unbounded, wide)


@dataclass(frozen=True)
class Query:
    rx: re.Pattern[str]
    regex: bool
    limit: int | None         # строки длиннее обрезаются при сверке (только для regex)


def compile_query(text: str, regex: bool = False, ci: bool = True) -> Query:
    """Шаблон поиска. Простой текст — подстрока; regex — выражение Python с ограничениями. Ошибка — LogError."""
    text = text.strip()
    if not text:
        raise LogError("пустой запрос")
    if len(text) > QUERY_MAX:
        raise LogError(f"запрос длиннее {QUERY_MAX} символов")
    flags = re.IGNORECASE if ci else 0
    if not regex:
        return Query(re.compile(re.escape(text), flags), False, None)
    try:
        _walk(_sre.parse(text, flags))
        rx = re.compile(text, flags)
    except re.error as e:
        raise LogError(f"ошибка в выражении: {e}") from None
    except (RecursionError, OverflowError, ValueError):
        raise LogError("выражение слишком сложное") from None
    if rx.search(""):
        raise LogError("выражение совпадает с пустой строкой — уточните")
    return Query(rx, True, RX_LINE)


@dataclass
class Hit:
    src: str                  # file:<имя> | unit:<имя>
    ts: float | None
    tok: str
    text: str                 # очищенная строка (возможно, фрагмент)
    spans: list[tuple[int, int]] = field(default_factory=list)
    file_time: bool = False   # время — файла, а не строки


@dataclass
class Result:
    hits: list[Hit] = field(default_factory=list)
    scanned: int = 0
    stopped: str = ""         # time | entries | bytes | hits — поиск оборван по лимиту
    more: bool = False        # есть ещё совпадения сверх лимита
    errors: list[str] = field(default_factory=list)


class Budget:
    def __init__(self, hits: int, seconds: float | None = None, entries: int | None = None,
                 size: int | None = None) -> None:
        self.hits = hits
        self.entries = SCAN_ENTRIES if entries is None else entries
        self.size = SCAN_BYTES if size is None else size
        self.deadline = time.monotonic() + (SEARCH_SECONDS if seconds is None else seconds)
        self.scanned = 0
        self.stopped = ""

    def spend(self, nbytes: int) -> bool:
        """Учесть строку; False — лимит исчерпан (причина в stopped)."""
        self.scanned += 1
        self.size -= nbytes
        if self.scanned >= self.entries:
            self.stopped = self.stopped or "entries"
        elif self.size <= 0:
            self.stopped = self.stopped or "bytes"
        elif self.scanned % 512 == 0 and time.monotonic() > self.deadline:
            self.stopped = self.stopped or "time"
        return not self.stopped

    @property
    def left(self) -> float:
        return max(0.5, self.deadline - time.monotonic())


def _make_hit(src: str, ts: float | None, tok: str, raw: str, q: Query, clean: Callable[[str], str],
              prefilter: bool, file_time: bool = False) -> Hit | None:
    probe = _ANSI_RE.sub("", raw) if "" in raw else raw
    if prefilter and not q.rx.search(probe[:q.limit]):
        return None
    text = clean(raw)
    spans = []
    for m in q.rx.finditer(text[:q.limit]):
        if m.end() > m.start():
            spans.append((m.start(), m.end()))
            if len(spans) >= SPANS_MAX:
                break
    if not spans:
        return None
    text, spans = _snippet(text, spans)
    return Hit(src, ts, tok, text, spans, file_time)


def _snippet(text: str, spans: list[tuple[int, int]]) -> tuple[str, list[tuple[int, int]]]:
    if len(text) <= SNIPPET:
        return text, spans
    start = max(0, spans[0][0] - SNIPPET // 3)
    end = min(len(text), start + SNIPPET)
    pre = "…" if start else ""
    cut = [(a - start + len(pre), min(b, end) - start + len(pre)) for a, b in spans if a < end and b > start]
    return pre + text[start:end] + ("…" if end < len(text) else ""), cut


def _mask_safe(q: Query) -> bool:
    """Предварительный отбор по сырой строке годится, пока шаблон не ищет саму маску."""
    return "•" not in q.rx.pattern and "QR" not in q.rx.pattern


def search_file(path: Path, q: Query, clean: Callable[[str], str], budget: Budget, out: Result,
                since: float | None, until: float | None) -> None:
    started = file_start(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    if (since is not None and mtime < since) or (until is not None and started > until):
        return
    prefilter = _mask_safe(q)
    found: deque[Hit] = deque(maxlen=budget.hits)   # у файла нужны самые свежие совпадения
    total = 0
    try:
        with open(path, "rb") as f:
            off = 0
            while True:
                raw = f.readline(BLOCK)
                if not raw:
                    break
                here, off = off, off + len(raw)
                if not budget.spend(len(raw)):
                    break
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                hit = _make_hit(f"file:{path.name}", mtime, str(here), text[:LINE_MAX], q, clean, prefilter, True)
                if hit:
                    found.append(hit)
                    total += 1
    except OSError as e:
        out.errors.append(f"{path.name}: {e.strerror or e}")
    out.more = out.more or total > budget.hits
    out.hits += list(found)[::-1]


def search_journal(units: list[str], q: Query, clean: Callable[[str], str], budget: Budget, out: Result,
                   since: float | None, until: float | None) -> None:
    if not units:
        return
    args = ["-r"]
    if since is not None:
        args += ["--since", f"@{int(since)}"]
    if until is not None:
        args += ["--until", f"@{int(until) + 1}"]
    for u in units:
        args += ["-u", check_unit(u)]
    known = set(units)
    prefilter = _mask_safe(q)
    state: dict[str, Any] = {}
    n = 0
    try:
        for e in journal_entries(args, "MESSAGE,SYSLOG_IDENTIFIER,_SYSTEMD_UNIT,UNIT", seconds=budget.left,
                                 state=state):
            body = entry_body(e)
            if not budget.spend(len(body)):
                break
            ts = entry_ts(e)
            if ts is not None and ((since is not None and ts < since) or (until is not None and ts > until + 1)):
                continue
            unit = next((u for u in (_field(e, "UNIT"), _field(e, "_SYSTEMD_UNIT")) if u in known), "")
            hit = _make_hit(f"unit:{unit or units[0]}", ts, _field(e, "__CURSOR"), body, q, clean, prefilter)
            if hit:
                out.hits.append(hit)
                n += 1
                if n > budget.hits:
                    out.more = True
                    break
    except LogError as e:
        out.errors.append(str(e))
    if state.get("stopped") == "time":
        budget.stopped = budget.stopped or "time"


def search(q: Query, clean: Callable[[str], str], *, files: list[Path], units: list[str],
           since: float | None = None, until: float | None = None, limit: int = HITS[0]) -> Result:
    """Поиск по файлам и журналу: самые свежие совпадения первыми, не больше limit."""
    out = Result()
    budget = Budget(limit)
    for path in files:
        if budget.stopped:
            break
        search_file(path, q, clean, budget, out, since, until)
    if not budget.stopped:
        search_journal(units, q, clean, budget, out, since, until)
    out.scanned = budget.scanned
    out.stopped = budget.stopped
    out.hits.sort(key=lambda h: (h.ts or 0, int(h.tok) if h.tok.isdigit() else 0), reverse=True)
    if len(out.hits) > limit:
        out.hits = out.hits[:limit]
        out.more = True
    if out.more and not out.stopped:
        out.stopped = "hits"
    return out
