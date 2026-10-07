"""Логи: чтение назад по файлу и журналу, поиск (один источник и все), регулярки, маскировка секретов,
очистка (белый список, CSRF, заявка для root-исполнителя)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ZooEnv
from tests.test_web import AppTestBase, header
from zoolib import logctl, logread
from zoolib.web import logs

BASE = 1_791_000_000
SECRET = "tok-abcdefghijkl"


def cursor(i: int, ts: float) -> str:
    return f"s=aa;i={i:x};b=bb;m=1;t={int(ts * 1_000_000):x};x=cc"


def entry(i: int, unit: str = "a.service", msg: str | None = None, ts: float | None = None) -> dict:
    ts = BASE + i if ts is None else ts
    return {"MESSAGE": msg if msg is not None else f"запись {i:04d}", "SYSLOG_IDENTIFIER": unit.split(".")[0],
            "_SYSTEMD_UNIT": unit, "__REALTIME_TIMESTAMP": str(int(ts * 1_000_000)), "__CURSOR": cursor(i, ts)}


class FakeJournal:
    """Подставка journalctl: записи в памяти, понимает -u, -r, -n, --cursor, --after-cursor, --since, --until.
    Семантика курсора — как в journalctl: --cursor включает запись, --after-cursor — нет."""

    def __init__(self, entries: list[dict], fail: tuple[int, str] | None = None) -> None:
        self.entries = sorted(entries, key=lambda e: int(e["__REALTIME_TIMESTAMP"]))
        self.fail = fail
        self.calls: list[list[str]] = []
        self.killed = 0

    def popen(self, argv: list[str]):
        self.calls.append(argv)
        args = argv[1:]
        units, reverse, n, cur, after, since, until = [], False, None, None, None, None, None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-u":
                units.append(args[i + 1])
                i += 1
            elif a == "-r":
                reverse = True
            elif a == "-n":
                n = int(args[i + 1])
                i += 1
            elif a == "--cursor":
                cur = args[i + 1]
                i += 1
            elif a == "--after-cursor":
                after = args[i + 1]
                i += 1
            elif a == "--since":
                since = int(args[i + 1][1:]) * 1_000_000
                i += 1
            elif a == "--until":
                until = int(args[i + 1][1:]) * 1_000_000
                i += 1
            elif a == "-o":
                i += 1
            i += 1
        match = ("UNIT", "_SYSTEMD_UNIT", "OBJECT_SYSTEMD_UNIT", "COREDUMP_UNIT", "_SYSTEMD_SLICE")
        rows = [e for e in self.entries if not units or any(e.get(k) in units for k in match)]
        ts = lambda e: int(e["__REALTIME_TIMESTAMP"])  # noqa: E731
        if since is not None:
            rows = [e for e in rows if ts(e) >= since]
        if until is not None:
            rows = [e for e in rows if ts(e) <= until]
        pivot = cur or after
        if pivot:
            if not any(e["__CURSOR"] == pivot for e in self.entries):
                return self._proc([], 1, b"Failed to seek to cursor: No data available\n")
            pts = next(ts(e) for e in self.entries if e["__CURSOR"] == pivot)
            if reverse:
                rows = [e for e in rows if ts(e) <= pts]
            else:
                rows = [e for e in rows if ts(e) > pts or (cur and ts(e) == pts)]
        if reverse:
            rows = rows[::-1]
        if n is not None:
            rows = rows[:n]
        if self.fail:
            return self._proc([], *self.fail[:1], self.fail[1].encode())
        return self._proc(rows, 0, b"")

    def _proc(self, rows, rc, err):
        fake = self
        out = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in rows).encode()

        class P:
            stdout = io.BytesIO(out)
            stderr = io.BytesIO(err)
            returncode = rc

            def poll(self):
                return rc

            def kill(self):
                fake.killed += 1

            def wait(self):
                return rc
        return P()


def write_lines(path: Path, lines: list[str], newline: str = "\n", final: bool = True) -> None:
    data = newline.join(lines) + (newline if final else "")
    path.write_bytes(data.encode("utf-8"))


class FileReadTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="zoo-logs-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def pages_back(self, path: Path, n: int) -> list[list[str]]:
        pages, end = [], None
        for _ in range(10_000):
            ch = logread.lines_before(path, end, n)
            pages.append([line.text for line in ch.lines])
            if ch.older is None:
                break
            end = int(ch.older)
        return pages[::-1]

    def test_whole_file_in_pages_without_gaps(self):
        lines = [f"строка {i:05d} " + "x" * (i % 53) for i in range(5000)]   # больше одного блока чтения
        p = self.dir / "install-1.log"
        write_lines(p, lines)
        for n in (100, 300, 777):
            pages = self.pages_back(p, n)
            self.assertEqual([x for page in pages for x in page], lines, n)
            self.assertTrue(all(len(page) <= n for page in pages))

    def test_no_trailing_newline_and_crlf_and_empty(self):
        p = self.dir / "a.log"
        write_lines(p, ["один", "два", "три"], final=False)
        ch = logread.lines_before(p, None, 10)
        self.assertEqual([line.text for line in ch.lines], ["один", "два", "три"])
        self.assertIsNone(ch.older)
        write_lines(p, ["один", "два"], newline="\r\n")
        self.assertEqual([line.text for line in logread.lines_before(p, None, 10).lines], ["один", "два"])
        p.write_bytes(b"")
        ch = logread.lines_before(p, None, 10)
        self.assertEqual((ch.lines, ch.older, ch.newer), ([], None, None))

    def test_forward_and_window(self):
        lines = [f"л{i:03d}" for i in range(500)]
        p = self.dir / "w.log"
        write_lines(p, lines)
        tail = logread.lines_before(p, None, 50)
        at = int(tail.lines[10].tok)
        win = logread.file_around(p, at, 20)
        texts = [line.text for line in win.lines]
        self.assertEqual(texts, lines[450 + 10 - 10:450 + 10 + 11])
        self.assertIn(tail.lines[10].text, texts)
        fwd = logread.lines_from(p, int(win.newer), 1000)
        self.assertEqual([line.text for line in fwd.lines], lines[len(lines) - len(fwd.lines):])
        self.assertIsNone(fwd.newer)

    def test_offsets_inside_a_line_snap_to_its_start(self):
        lines = ["первая", f"token={SECRET}x", "█▀▀▀▀▀█ ▄▄ █▀▀▀▀▀█", "последняя"]
        p = self.dir / "s.log"
        write_lines(p, lines)
        raw = p.read_bytes()
        starts = [0]
        for part in raw.split(b"\n")[:-1]:
            starts.append(starts[-1] + len(part) + 1)
        mid = starts[1] + 10
        ch = logread.lines_from(p, mid, 10)
        self.assertEqual([x.text for x in ch.lines], lines[1:])
        self.assertEqual(ch.lines[0].tok, str(starts[1]))
        self.assertEqual(ch.older, str(starts[1]))
        ch = logread.lines_before(p, mid, 10)
        self.assertEqual([x.text for x in ch.lines], lines[:1])
        self.assertEqual(ch.newer, str(starts[1]))
        ch = logread.lines_before(p, starts[2] + 3, 10)
        self.assertEqual([x.text for x in ch.lines], lines[:2])
        win = logread.file_around(p, starts[2] + 4, 4)
        self.assertIn(lines[2], [x.text for x in win.lines])
        self.assertEqual([x.text for x in win.lines][:3], lines[:3])
        for off in (0, starts[1]):
            self.assertEqual(logread.lines_from(p, off, 10).lines[0].tok, str(off))
        for off in (len(raw), len(raw) + 99):
            self.assertEqual(logread.lines_from(p, off, 10).lines, [])
        big = self.dir / "t.log"
        big.write_bytes(b"x" * 200_000 + b"\n" + b"tail\n")
        self.assertEqual(logread.lines_from(big, 150_000, 5).lines[0].tok, "0")

    def test_long_line_is_cut_and_binary_is_safe(self):
        p = self.dir / "b.log"
        p.write_bytes(b"x" * 70000 + b"\n" + b"\xff\xfe bad\n")
        got = [line.text for line in logread.lines_before(p, None, 5).lines]
        self.assertLessEqual(len(got[0]), logread.LINE_MAX + 1)
        self.assertIn("bad", got[-1])

    def test_unreadable_and_bad_names(self):
        with self.assertRaises(logread.LogError):
            logread.lines_before(self.dir / "нет.log", None, 5)
        for bad in ("../x.log", "a/b.log", ".hidden.log", "x.txt", "x.log\n", "x.log.bak"):
            with self.assertRaises(logread.LogError, msg=bad):
                logread.file_path(bad)


class JournalReadTest(unittest.TestCase):
    def setUp(self):
        self.fj = FakeJournal([entry(i, "a.service" if i % 3 else "b.service") for i in range(1, 301)])
        p = mock.patch("zoolib.logread._popen", side_effect=self.fj.popen)
        p.start()
        self.addCleanup(p.stop)
        self.a = [e for e in self.fj.entries if e["_SYSTEMD_UNIT"] == "a.service"]

    def test_tail_then_back_to_the_beginning(self):
        ch = logread.journal_tail("a.service", 50)
        seen = [line.tok for line in ch.lines]
        self.assertEqual(len(seen), 50)
        self.assertEqual(seen[-1], self.a[-1]["__CURSOR"])
        self.assertEqual(ch.tip, seen[-1])
        pages = 1
        while ch.older:
            ch = logread.journal_before("a.service", ch.older, 50)
            self.assertEqual(ch.newer, ch.tip)
            seen = [line.tok for line in ch.lines] + seen
            pages += 1
            self.assertLess(pages, 20)
        self.assertEqual(seen, [e["__CURSOR"] for e in self.a], "весь журнал юнита без пропусков и повторов")

    def test_after_picks_up_new_entries(self):
        tip = logread.journal_tail("a.service", 5).tip
        self.assertEqual(logread.journal_after("a.service", tip, 10).lines, [])
        self.fj.entries.append(entry(400, "a.service"))
        self.fj.entries.append(entry(401, "b.service"))
        ch = logread.journal_after("a.service", tip, 10)
        self.assertEqual([line.text for line in ch.lines], ["2026-10-01 05:46:40 a: запись 0400".replace(
            "2026-10-01 05:46:40", logread.fmt_ts(BASE + 400))])
        self.assertIsNone(ch.newer)

    def test_around_has_both_sides_and_the_pivot(self):
        pivot = self.a[100]
        ch = logread.journal_around("a.service", pivot["__CURSOR"], 20)
        toks = [line.tok for line in ch.lines]
        self.assertIn(pivot["__CURSOR"], toks)
        self.assertEqual(toks, [e["__CURSOR"] for e in self.a[90:111]][:len(toks)])
        self.assertTrue(ch.older and ch.newer)

    def test_cursor_and_unit_are_validated(self):
        for bad in ("--vacuum-time=1d", "s=zz", "x" * 40, ""):
            with self.assertRaises(logread.LogError, msg=bad):
                logread.journal_before("a.service", bad, 10)
        for bad in ("-a", "a b", "", "../x"):
            with self.assertRaises(logread.LogError, msg=bad):
                logread.journal_tail(bad, 10)
        self.assertEqual(self.fj.calls, [])

    def test_vanished_cursor_is_an_error(self):
        gone = cursor(99999, BASE + 5)
        with self.assertRaises(logread.LogError):
            logread.journal_before("a.service", gone, 10)

    def test_missing_journalctl(self):
        with mock.patch("zoolib.logread._popen", side_effect=FileNotFoundError):
            with self.assertRaises(logread.LogError) as cm:
                logread.journal_tail("a.service", 5)
        self.assertIn("journalctl", str(cm.exception))

    def test_binary_message_and_missing_fields(self):
        e = entry(1, "a.service")
        e["MESSAGE"] = list("привет".encode())
        del e["SYSLOG_IDENTIFIER"]
        self.assertTrue(logread.entry_line(e).text.endswith("привет"))
        self.assertEqual(logread.entry_body({"__CURSOR": "x"}), "")


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="zoo-logs-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"LOG_DIR": self.dir.as_posix()})
        env.start()
        self.addCleanup(env.stop)
        self.f1 = self.dir / "install-20261001-100000.log"
        self.f2 = self.dir / "install-20261002-100000.log"
        write_lines(self.f1, ["старт установки", "\x1b[0;36m[i]\x1b[0m Hysteria ОК", "ошибка: нет места"])
        write_lines(self.f2, ["старт", "ERROR timeout 10", f"token={SECRET}x", "Authorization: Bearer abcdefghij123456"])
        os.utime(self.f1, (BASE, BASE))
        os.utime(self.f2, (BASE + 86400, BASE + 86400))
        self.fj = FakeJournal([entry(1, "a.service", "соединение отклонено"), entry(2, "b.service", "Error: timeout"),
                               entry(3, "a.service", "всё хорошо"), entry(4, "a.service", f"пароль password={SECRET}zz")])
        p = mock.patch("zoolib.logread._popen", side_effect=self.fj.popen)
        p.start()
        self.addCleanup(p.stop)
        self.clean = logs.cleaner(None)

    def find(self, text, files=None, units=None, **kw):
        q = logread.compile_query(text)
        return logread.search(q, self.clean, files=[self.f1, self.f2] if files is None else files,
                              units=["a.service", "b.service"] if units is None else units, **kw)

    def srcs(self, res):
        return sorted({h.src for h in res.hits})

    def test_single_source_only(self):
        res = self.find("timeout", files=[self.f2], units=[])
        self.assertEqual(self.srcs(res), ["file:install-20261002-100000.log"])
        res = self.find("timeout", files=[], units=["b.service"])
        self.assertEqual(self.srcs(res), ["unit:b.service"])
        self.assertEqual(self.find("timeout", files=[], units=["a.service"]).hits, [])

    def test_all_sources_and_order(self):
        res = self.find("timeout")
        self.assertEqual(self.srcs(res), ["file:install-20261002-100000.log", "unit:b.service"])
        self.assertTrue(all(h.spans for h in res.hits))
        ts = [h.ts for h in res.hits]
        self.assertEqual(ts, sorted(ts, reverse=True), "свежие первыми")
        self.assertEqual(len(self.find("всё хорошо").hits), 1)

    def test_search_ignores_case(self):
        self.assertEqual(len(self.find("error", files=[self.f2], units=[]).hits), 1)
        self.assertEqual(len(self.find("ERROR", files=[self.f2], units=[]).hits), 1)
        self.assertEqual(len(self.find("ПРИВЕТ", files=[self.f1], units=[]).hits), 0)
        self.assertEqual(len(self.find("ОШИБКА", files=[self.f1], units=[]).hits), 1, "кириллица без учёта регистра")

    def test_substring_is_literal(self):
        self.assertEqual(self.find("timeout 1.").hits, [])
        self.assertEqual(len(self.find("timeout 1.", files=[self.f2], units=[]).hits), 0, "точка — просто точка")
        self.assertEqual(len(self.find(r"timeout \d+", files=[self.f2], units=[]).hits), 0)
        self.assertEqual(self.find("a+b").hits, [])
        self.assertEqual(self.find("error|ошибка").hits, [])

    def test_query_limits(self):
        for bad, why in (("x" * 121, "длиннее"), ("   ", "пустой")):
            with self.assertRaises(logread.LogError, msg=bad) as cm:
                logread.compile_query(bad)
            self.assertIn(why, str(cm.exception), bad)
        for ok in ("[", "(a+)+b", "a*", "x.*y.*z.*w", "x" * 120):
            logread.compile_query(ok)

    def test_qr_rows_are_masked_in_single_line_hits(self):
        qr = ["█▀▀▀▀▀█ ▄▄ █▀▀▀▀▀█", "█ ███ █ ▀▀ █ ███ █", "▀▀▀▀▀▀▀ ▀▄ ▀▀▀▀▀▀▀"]
        write_lines(self.f1, ["до", *qr, "после QR"])
        for query in ("█", "█▀", "▄"):
            self.assertEqual(self.find(query, files=[self.f1], units=[]).hits, [], query)
        res = self.find("QR", files=[self.f1], units=[])
        self.assertTrue(res.hits)
        self.assertNotRegex(" ".join(h.text for h in res.hits), "[█▀▄]")
        self.assertEqual(self.clean("█▀▀▀▀▀█ ▄▄ █"), "[QR скрыт]")
        self.assertEqual(self.clean("█ █"), "█ █", "короткое — не QR")
        self.assertEqual(self.clean("текст █▀▀▀▀▀█ ▄▄ █"), "текст █▀▀▀▀▀█ ▄▄ █", "строка не только из блоков")

    def test_journal_hit_unit_comes_from_any_unit_field_and_unknown_is_not_guessed(self):
        def raw(i, msg, **fields):
            e = entry(i, "init.scope", msg)
            e.update(fields)
            return e
        self.fj.entries += [raw(10, "crash one", OBJECT_SYSTEMD_UNIT="b.service"),
                            raw(11, "crash two", COREDUMP_UNIT="b.service"),
                            raw(12, "crash three", UNIT="a.service"),
                            raw(13, "crash four", _SYSTEMD_SLICE="a.service")]
        self.fj.entries.sort(key=lambda e: int(e["__REALTIME_TIMESTAMP"]))
        res = self.find("crash", files=[], units=["a.service", "b.service"])
        src = {h.text.split(": ", 1)[1]: h.src for h in res.hits}
        self.assertEqual(src, {"crash one": "unit:b.service", "crash two": "unit:b.service",
                               "crash three": "unit:a.service", "crash four": logread.JOURNAL_SRC})
        fields = self.fj.calls[-1][self.fj.calls[-1].index("-o") + 2]
        for f in ("OBJECT_SYSTEMD_UNIT", "COREDUMP_UNIT", "UNIT", "_SYSTEMD_UNIT"):
            self.assertIn(f, fields)

    def test_secrets_are_masked_in_hits_and_cannot_be_probed(self):
        res = self.find("token")
        text = " ".join(h.text for h in res.hits)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("abcdefghij123456", text)
        cfg = mock.Mock(values={"ZOO_WEB_TOKEN": SECRET})
        self.clean = logs.cleaner(cfg)
        res = self.find("token")
        self.assertTrue(any("•••" in h.text for h in res.hits))
        self.assertNotIn(SECRET, " ".join(h.text for h in res.hits))
        self.assertEqual(self.find(SECRET).hits, [], "скрытое значение найти нельзя")
        self.assertEqual(self.find("tok-abc", files=[self.f2], units=[]).hits, [])
        self.assertEqual(self.find("Bearer abcdef").hits, [], "и значение шаблона маскировки тоже")
        self.assertTrue(self.find("Authorization").hits)
        self.assertTrue(self.find("•••").hits, "саму маску искать можно")

    def test_ansi_does_not_split_a_match(self):
        self.assertEqual(len(self.find("[i] Hysteria", files=[self.f1], units=[]).hits), 1)

    def test_period_filters_files_and_journal(self):
        s1, s2 = logread.file_start(self.f1), logread.file_start(self.f2)
        os.utime(self.f1, (s1 + 60, s1 + 60))
        os.utime(self.f2, (s2 + 60, s2 + 60))
        self.assertEqual(self.srcs(self.find("старт", since=s2)), ["file:install-20261002-100000.log"])
        self.assertEqual(self.srcs(self.find("старт", until=s1 + 3600)), ["file:install-20261001-100000.log"])
        self.assertEqual(self.srcs(self.find("старт", since=s1, until=s2 + 3600)),
                         ["file:install-20261001-100000.log", "file:install-20261002-100000.log"])
        self.assertEqual(self.find("старт", since=s2 + 10 * 86400).hits, [])
        res = self.find("timeout", files=[], since=BASE + 3, until=BASE + 4)
        self.assertEqual(res.hits, [])
        res = self.find("timeout", files=[], since=BASE + 2, until=BASE + 2)
        self.assertEqual(len(res.hits), 1)
        self.assertIn(["--since", f"@{BASE + 2}"], [self.fj.calls[-1][i:i + 2] for i in range(len(self.fj.calls[-1]))])

    def test_hit_limit_and_scan_caps(self):
        write_lines(self.f1, [f"совпадение {i}" for i in range(500)])
        res = self.find("совпадение", files=[self.f1], units=[], limit=200)
        self.assertEqual((len(res.hits), res.more, res.stopped), (200, True, "hits"))
        self.assertEqual(res.hits[0].text, "совпадение 499", "самые свежие строки файла первыми")
        with mock.patch.object(logread, "SCAN_ENTRIES", 50):
            res = self.find("совпадение", files=[self.f1], units=[])
        self.assertEqual(res.stopped, "entries")
        self.assertLessEqual(res.scanned, 50)
        with mock.patch.object(logread, "SCAN_BYTES", 100):
            self.assertEqual(self.find("совпадение", files=[self.f1], units=[]).stopped, "bytes")

    def test_journalctl_is_stopped_not_read_to_the_end(self):
        self.fj.entries = [entry(i, "a.service", "строка поиска") for i in range(1, 2001)]
        res = self.find("поиска", files=[], units=["a.service"], limit=200)
        self.assertEqual(len(res.hits), 200)
        self.assertTrue(res.more)

    def test_journal_errors_are_reported_not_raised(self):
        self.fj.fail = (1, "Failed to open journal")
        res = self.find("x", files=[], units=["a.service"])
        self.assertEqual(res.hits, [])
        self.assertTrue(any("Failed" in e for e in res.errors))

    def test_snippet_of_long_line(self):
        write_lines(self.f1, ["a" * 3000 + "НАЙДИ" + "b" * 3000])
        h = self.find("найди", files=[self.f1], units=[]).hits
        self.assertEqual(len(h), 1)
        self.assertLessEqual(len(h[0].text), logread.SNIPPET + 2)
        a, b = h[0].spans[0]
        self.assertEqual(h[0].text[a:b], "НАЙДИ")


class WebLogsTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.logdir = self.env.root / "logs"
        self.logdir.mkdir()
        p = mock.patch.dict(os.environ, {"LOG_DIR": self.logdir.as_posix()})
        p.start()
        self.addCleanup(p.stop)
        self.env.write_config({"SERVER_IP": "10.0.0.1", "ZOO_WEB_TOKEN": SECRET})
        self.f1 = self.logdir / "install-20261001-100000.log"
        self.f2 = self.logdir / "install-20261002-100000.log"
        self.f3 = self.logdir / "install-20261003-100000.log"
        write_lines(self.f1, ["старая установка", "timeout здесь"])
        write_lines(self.f2, [f"строка {i:04d}" for i in range(1000)])
        write_lines(self.f3, ["новая установка", f"token={SECRET}x", "vless://uuid@1.2.3.4:443?x=y#n"])
        for i, f in enumerate((self.f1, self.f2, self.f3)):
            os.utime(f, (BASE + i * 100000, BASE + i * 100000))
        self.fj = FakeJournal([entry(i, "zoo-web.service", f"web {i:03d}" + (f" token {SECRET}" if i == 7 else ""))
                               for i in range(1, 400)])
        p = mock.patch("zoolib.logread._popen", side_effect=self.fj.popen)
        p.start()
        self.addCleanup(p.stop)
        self.jdir = self.env.root / "journal"
        (self.jdir / "m").mkdir(parents=True)
        (self.jdir / "m" / "system.journal").write_bytes(b"j" * 10_000)
        p = mock.patch.object(logctl, "JOURNALD_DIRS", (str(self.jdir),))
        p.start()
        self.addCleanup(p.stop)
        self.c.login()

    def body(self, path):
        resp, body = self.c.get(path)
        self.assertEqual(resp.status, 200, path)
        return body

    def test_page_has_search_pagination_and_cleaning_menus(self):
        body = self.body("/logs?src=file:install-20261002-100000.log")
        self.assertIn('id="logtext"', body)
        self.assertIn("строка 0999", body)
        self.assertNotIn("строка 0600", body)
        self.assertIn("↑ показать раньше", body)
        self.assertIn('name="q"', body)
        self.assertIn("в этом логе", body)
        self.assertIn("во всех логах", body)
        self.assertNotIn("<h3>Очистка</h3>", body, "отдельной карточки очистки больше нет")
        self.assertEqual(body.count('class="menu"'), 5, "«Установка», три файла, «Сервисы»; у юнитов меню нет")
        self.assertEqual(len(re.findall(r'action="/logs/vacuum"', body)), 3)
        self.assertIn("Журнал systemd общий для всех сервисов", body)
        self.assertIn("9.8 КБ", body)
        self.assertEqual(len(re.findall(r'data-confirm="', body)), 11,
                         "выход, 2 «старше N дн.», 2 «Удалить» (не у свежего), 3 «последние строки», 3 очистки журнала")
        self.assertRegex(body, r'<span class="menu-off" title="самый свежий лог не удаляется[^"]*"><button[^>]*disabled>')
        self.assertIn(" data-tail", body)

    def test_menu_only_for_install_logs(self):
        write_lines(self.logdir / "other.log", ["x"])
        body = self.body("/logs")
        self.assertIn("other.log", body)
        self.assertNotIn("Действия с other.log", body)
        self.assertIn("Действия с install-20261001-100000.log", body)

    def test_csp_friendly_markup(self):
        for path in ("/logs?src=file:install-20261002-100000.log", "/logs?q=timeout&in=all",
                     "/logs?src=file:install-20261002-100000.log&at=" + str(len("строка 0000\n".encode()) * 3)):
            body = self.body(path)
            self.assertNotRegex(body, r"<script(?![^>]*\bsrc=)", path)
            self.assertNotRegex(body, r"\sstyle=|\son[a-z]+=", path)

    def test_every_chunk_walks_back_through_the_whole_file(self):
        body = self.body("/logs?src=file:install-20261002-100000.log&lines=100")
        got = re.search(r'<pre[^>]*id="logtext"[^>]*>(.*?)</pre>', body, re.S).group(1).split("\n")
        older = re.search(r'data-older="([^"]+)"', body).group(1)
        pieces = [got]
        for _ in range(30):
            resp, raw = self.c.get(f"/logs/chunk?src=file:install-20261002-100000.log&before={older}&lines=100")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type.split(";")[0], "application/json")
            d = json.loads(raw)
            pieces.insert(0, d["text"].split("\n"))
            if not d["older"]:
                break
            older = d["older"]
        flat = [x for p in pieces for x in p]
        self.assertEqual(flat, [f"строка {i:04d}" for i in range(1000)])

    def test_journal_unit_pagination_via_chunk(self):
        body = self.body("/logs?src=unit:zoo-web.service&lines=100")
        self.assertIn("web 399", body)
        older = re.search(r'data-older="([^"]+)"', body).group(1)
        seen = 100
        for _ in range(10):
            d = json.loads(self.c.get(f"/logs/chunk?src=unit:zoo-web.service&before={older}&lines=100")[1])
            seen += d["n"]
            if not d["older"]:
                break
            older = d["older"]
        self.assertEqual(seen, 399)

    def test_chunk_rejects_bad_input(self):
        for q in ("src=unit:nope.service&before=s=aa;i=1;b=1;m=1;t=1;x=1", "src=file:install-20261002-100000.log",
                  "src=file:../etc/passwd&before=0", "src=unit:zoo-web.service"):
            self.assertEqual(self.c.get("/logs/chunk?" + q)[0].status, 400, q)
        resp, raw = self.c.get("/logs/chunk?src=unit:zoo-web.service&before=--vacuum-time=1d")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.c.get("/logs/chunk?src=file:install-20261002-100000.log&before=abc")[0].status, 400)

    def test_search_one_and_all_with_highlight_and_open_link(self):
        body = self.body("/logs?src=file:install-20261001-100000.log&q=timeout")
        self.assertIn("Найдено: 1", body)
        self.assertIn("<mark>timeout</mark>", body)
        self.assertRegex(body, r'href="/logs\?src=file%3Ainstall-20261001-100000.log&amp;at=\d+#hit"')
        body = self.body("/logs?src=file:install-20261001-100000.log&q=web+007&in=all")
        self.assertIn("Найдено: 1", body)
        self.assertIn("<mark>web 007</mark>", body)
        self.assertRegex(body, r"at=s%3Daa%3Bi%3D7%3B")
        body = self.body("/logs?src=file:install-20261001-100000.log&q=web+007")
        self.assertIn("Ничего не нашлось", body)

    def test_search_open_scrolls_to_hit_line(self):
        body = self.body("/logs?src=file:install-20261001-100000.log&q=timeout")
        at = re.search(r"at=(\d+)#hit", body).group(1)
        body = self.body(f"/logs?src=file:install-20261001-100000.log&at={at}")
        self.assertIn('<mark id="hit" class="hitline">timeout здесь</mark>', body)

    def test_search_is_plain_substring_ignoring_case(self):
        body = self.body("/logs?src=file:install-20261001-100000.log&q=TIMEOUT")
        self.assertIn("Найдено: 1", body)
        body = self.body("/logs?src=file:install-20261001-100000.log&q=t[a-z]%2Bout&rx=1&cs=1")
        self.assertIn("Ничего не нашлось", body)
        self.assertNotIn('name="rx"', body)
        self.assertNotIn('name="cs"', body)
        self.assertNotIn("rx=1", self.body("/logs?src=file:install-20261001-100000.log&q=a&rx=1&cs=1"))

    def test_search_period_and_errors(self):
        body = self.body("/logs?q=timeout&in=all&per=1h")
        self.assertIn("Ничего не нашлось", body)
        body = self.body("/logs?q=timeout&in=all&since=2000-01-01&until=2999-01-01")
        self.assertIn("Найдено: 1", body)
        body = self.body("/logs?q=timeout&in=all&since=вчера")
        self.assertIn("не понял дату", body)
        body = self.body("/logs?q=timeout&in=all&since=2026-10-05&until=2026-10-01")
        self.assertIn("начало периода позже конца", body)

    def test_exact_period_uses_datetime_local_with_seconds(self):
        body = self.body("/logs?q=timeout&in=all&since=2026-10-05T14:30&until=2026-10-07+08:15:20")
        self.assertEqual(len(re.findall(r'<input type="datetime-local" step="1" name="(?:since|until)"', body)), 2)
        self.assertIn('name="since" value="2026-10-05T14:30:00"', body)
        self.assertIn('name="until" value="2026-10-07T08:15:20"', body)
        self.assertIn("<label>с<input", body)
        self.assertIn("<label>по<input", body)
        self.assertNotIn("placeholder=\"с:", body)
        body = self.body("/logs?q=timeout&in=all&until=2026-10-07")
        self.assertIn('name="until" value="2026-10-07T23:59:59"', body)
        self.assertNotIn('name="since" value=', body)
        for ok in ("2026-10-05T14:30", "2026-10-05T14:30:15", "2026-10-05 14:30"):
            self.assertNotIn("не понял дату", self.body("/logs?q=timeout&in=all&since=" + ok.replace(" ", "+")), ok)
        self.assertIn("не понял дату", self.body("/logs?q=timeout&in=all&since=2026-10-05T25:99"))

    def test_lines_selector_is_in_log_card_head(self):
        body = self.body("/logs?src=file:install-20261001-100000.log&lines=100")
        head = re.search(r'<div class="card-head"><h3[^>]*>install-20261001-100000.log</h3>(.*?)</div></div>', body, re.S).group(1)
        self.assertIn('class="seg small"', head)
        self.assertIn("скачать", head)
        self.assertEqual(re.findall(r'>(\d+)</a>', head), ['100', '300', '1000'])
        self.assertRegex(head, r'<a href="[^"]*" class="active">100</a>')
        self.assertIn("lines=1000", head)
        self.assertNotIn('aria-label="Строк"', body.split('class="card-head"')[0])

    def test_top_pages_do_not_repeat_nav_as_h1(self):
        for path in ("/", "/users", "/groups", "/apps", "/clients", "/traffic", "/probe", "/journal", "/logs", "/settings"):
            self.assertNotIn("<h1>", self.body(path), path)

    def test_masking_on_every_output(self):
        for path in ("/logs?src=file:install-20261003-100000.log", "/logs?q=token&in=all", "/logs?q=vless&in=all",
                     "/logs?src=unit:zoo-web.service&q=web&lines=500", "/logs/export?src=file:install-20261003-100000.log",
                     "/logs/export?q=token&in=all", "/logs/export?src=unit:zoo-web.service"):
            resp, body = self.c.get(path)
            self.assertEqual(resp.status, 200, path)
            self.assertNotIn(SECRET, body, path)
            self.assertNotIn("1.2.3.4:443", body, path)
        resp, body = self.c.get("/logs/chunk?src=file:install-20261003-100000.log&before=0")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(SECRET, body)
        d = json.loads(self.c.get("/logs/chunk?src=unit:zoo-web.service&after=" + cursor(1, BASE + 1) + "&lines=50")[1])
        self.assertNotIn(SECRET, d["text"])
        self.assertIn("•••", self.body("/logs?src=unit:zoo-web.service&lines=500&at=" + cursor(7, BASE + 7)))

    def test_qr_rows_and_mid_line_offsets_do_not_leak_on_any_output(self):
        qr = ["█▀▀▀▀▀█ ▄▄ █▀▀▀▀▀█", "█ ███ █ ▀▀ █ ███ █", "▀▀▀▀▀▀▀ ▀▄ ▀▀▀▀▀▀▀"]
        lines = ["начало", *qr, f"token={SECRET}x", "конец"]
        write_lines(self.f1, lines)
        src = "file:install-20261001-100000.log"
        raw = self.f1.read_bytes()
        qr_mid = raw.index("█ ███".encode()) + 5
        tok_mid = raw.index(SECRET.encode()) + 4
        for path in (f"/logs/chunk?src={src}&after={qr_mid}", f"/logs/chunk?src={src}&before={qr_mid + 20}",
                     f"/logs/chunk?src={src}&after={tok_mid}", f"/logs/chunk?src={src}&before={tok_mid + 3}",
                     f"/logs?src={src}&at={qr_mid}", f"/logs?src={src}&q=QR", f"/logs/export?src={src}",
                     f"/logs/export?src={src}&q=QR"):
            resp, body = self.c.get(path)
            self.assertEqual(resp.status, 200, path)
            for bad in ("█", "▀", "▄", SECRET[4:], "abcdefghijkl"):
                self.assertNotIn(bad, body, (path, bad))
        d = json.loads(self.c.get(f"/logs/chunk?src={src}&after={qr_mid}")[1])
        self.assertEqual(d["text"].split("\n")[0], "[QR скрыт]", "строка берётся целиком, с её начала")
        d = json.loads(self.c.get(f"/logs/chunk?src={src}&after={tok_mid}")[1])
        self.assertTrue(d["text"].startswith("token=•••"), d["text"])

    def test_hit_without_known_unit_opens_the_whole_journal(self):
        e = entry(500, "init.scope", "странный crash")
        e["_SYSTEMD_SLICE"] = "zoo-web.service"
        self.fj.entries.append(e)
        body = self.body("/logs?q=crash&in=all")
        self.assertIn("Найдено: 1", body)
        self.assertRegex(body, r"<td[^>]*>журнал</td>|>журнал<")
        href = re.search(r'href="(/logs\?src=journal%3Aall&amp;at=[^"]+)#hit"', body).group(1).replace("&amp;", "&")
        self.fj.calls.clear()
        page = self.body(href)
        self.assertIn("странный crash", page)
        self.assertIn('<mark id="hit" class="hitline">', page)
        self.assertTrue(self.fj.calls)
        self.assertTrue(all("-u" not in call for call in self.fj.calls), "без отбора по юниту")
        d = json.loads(self.c.get("/logs/chunk?src=journal:all&before=" + e["__CURSOR"])[1])
        self.assertNotIn("error", d)
        self.assertEqual(self.c.get("/logs/export?src=journal:all")[0].status, 200)
        self.assertEqual(self.c.get("/logs?src=journal:nope")[0].status, 200)
        self.assertEqual(self.c.get("/logs/chunk?src=journal:nope&before=" + e["__CURSOR"])[0].status, 400)

    def test_export_is_attachment(self):
        resp, body = self.c.get("/logs/export?src=file:install-20261003-100000.log")
        self.assertIn("attachment", header(resp, "Content-Disposition")[0])
        self.assertIn("новая установка", body)
        self.assertEqual(self.c.get("/logs/export?src=file:nope.log")[0].status, 404)
        resp, body = self.c.get("/logs/export?q=timeout&in=all")
        self.assertIn("timeout", body)
        self.assertEqual(self.c.get("/logs/export?q=timeout")[0].status, 400)

    def test_live_tail_view_has_tip_for_appending(self):
        body = self.body("/logs?src=unit:zoo-web.service")
        tip = re.search(r'data-tip="([^"]+)"', body).group(1)
        self.assertEqual(tip, cursor(399, BASE + 399))
        self.fj.entries.append(entry(500, "zoo-web.service", "свежая"))
        d = json.loads(self.c.get(f"/logs/chunk?src=unit:zoo-web.service&after={tip}")[1])
        self.assertIn("свежая", d["text"])
        self.assertEqual(d["tip"], cursor(500, BASE + 500))

    # ---------- очистка ----------

    def test_clean_requires_csrf(self):
        for path, form in (("/logs/clean", {"mode": "older", "days": "1"}),
                           ("/logs/vacuum", {"rule": "vacuum-time:7d"})):
            resp, _ = self.c.post(path, form, csrf=False)
            self.assertEqual(resp.status, 403, path)
            resp, _ = self.c.post(path, {**form, "csrf": "wrong"}, csrf=False)
            self.assertEqual(resp.status, 403, path)
        self.assertEqual(len(list(self.logdir.glob("install-*.log"))), 3)
        self.assertFalse(logctl.req_dir().exists() and list(logctl.req_dir().glob("*.json")))
        anon = type(self.c)(self.app)
        self.assertEqual(anon.post("/logs/clean", {"mode": "older", "days": "1"}, csrf=False)[0].status, 401)

    def test_delete_selected_keeps_newest_and_only_install_logs(self):
        other = self.logdir / "other.log"
        other.write_text("x", encoding="utf-8")
        keep2 = self.logdir / "install-x.log.bak"
        keep2.write_text("x", encoding="utf-8")
        self.c.get("/logs")
        resp, _ = self.c.post("/logs/clean", {"mode": "selected"}, multi={"names": [
            "install-20261001-100000.log", "install-20261003-100000.log", "other.log", "../other.log",
            "install-x.log.bak", "install-../../etc.log", "nope.log"]})
        self.assertEqual(resp.status, 303)
        left = sorted(f.name for f in self.logdir.iterdir())
        self.assertEqual(left, ["install-20261002-100000.log", "install-20261003-100000.log", "install-x.log.bak",
                                "other.log"], "удалён только выбранный install-лог; самый свежий и чужие остались")
        body = self.body("/logs")
        self.assertIn("Удалено логов установки: 1", body)

    def test_delete_older_than_days(self):
        """Возраст — по времени в имени install-YYYYMMDD-HHMMSS.log, а не по mtime."""
        now = time.time()
        for f, age in ((self.f3, 0), (self.f2, 10), (self.f1, 40)):
            f.rename(self.logdir / time.strftime("install-%Y%m%d-%H%M%S.log", time.localtime(now - age * 86400)))
        names = sorted(f.name for f in self.logdir.iterdir())
        self.c.get("/logs")
        self.c.post("/logs/clean", {"mode": "older", "days": "30"})
        self.assertEqual(sorted(f.name for f in self.logdir.iterdir()), names[1:])
        self.c.post("/logs/clean", {"mode": "older", "days": "7"})
        self.assertEqual(sorted(f.name for f in self.logdir.iterdir()), names[2:])
        newest = self.logdir / names[2]
        newest.rename(self.logdir / time.strftime("install-%Y%m%d-%H%M%S.log", time.localtime(now - 100 * 86400)))
        self.c.post("/logs/clean", {"mode": "older", "days": "1"})
        self.assertEqual(len(list(self.logdir.glob("install-*.log"))), 1,
                         "единственный (самый свежий) лог не удаляется никогда")

    def test_bad_clean_requests_change_nothing(self):
        self.c.get("/logs")
        for form in ({"mode": "older", "days": "0"}, {"mode": "older", "days": "abc"}, {"mode": "older", "days": "-1"},
                     {"mode": "rm -rf"}, {"mode": "selected"}, {}):
            resp, _ = self.c.post("/logs/clean", form)
            self.assertEqual(resp.status, 303, form)
        self.assertEqual(len(list(self.logdir.glob("install-*.log"))), 3)

    def test_tail_keeps_last_lines_in_place(self):
        write_lines(self.f2, [f"строка {i:04d}" for i in range(1500)])
        inode = self.f2.stat().st_ino
        self.c.get("/logs")
        resp, _ = self.c.post("/logs/clean", {"mode": "tail", "src": "file:install-20261002-100000.log"},
                              multi={"names": ["install-20261002-100000.log"]})
        self.assertEqual(resp.status, 303)
        lines = self.f2.read_bytes().decode("utf-8").split("\n")
        self.assertEqual((len(lines), lines[0], lines[-2], lines[-1]), (1001, "строка 0500", "строка 1499", ""))
        self.assertEqual(self.f2.stat().st_ino, inode)
        self.assertIn("оставлено 1000 строк, освобождено", self.body("/logs"))
        self.c.post("/logs/clean", {"mode": "tail"}, multi={"names": ["install-20261001-100000.log"]})
        self.assertIn("не больше 1000 строк", self.body("/logs"))

    def test_tail_rejects_foreign_names(self):
        other = self.logdir / "other.log"
        other.write_text("a\nb\n", encoding="utf-8")
        self.c.get("/logs")
        for name in ("other.log", "../other.log", "install-x.log", ""):
            self.c.post("/logs/clean", {"mode": "tail"}, multi={"names": [name]})
        self.assertEqual(other.read_text(encoding="utf-8"), "a\nb\n")

    def test_deleting_the_open_log_goes_back_to_list(self):
        self.c.get("/logs")
        resp, _ = self.c.post("/logs/clean", {"mode": "selected", "src": "file:install-20261001-100000.log"},
                              multi={"names": ["install-20261001-100000.log"]})
        self.assertEqual((resp.status, header(resp, "Location")), (303, ["/logs"]))
        self.assertFalse(self.f1.exists())

    def test_finished_vacuum_result_is_shown_with_freed_space(self):
        logctl.state_dir().mkdir(parents=True)
        rid = "a" * 32
        (logctl.state_dir() / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "action": "vacuum-time", "value": "7d", "status": "ok", "freed": 3 * 1024 * 1024,
             "finished": int(time.time())}), encoding="utf-8")
        body = self.body("/logs")
        self.assertIn("Журнал очищен", body)
        self.assertIn("освобождено 3.0 МБ", body)

    def test_symlink_is_not_deleted(self):
        link = self.logdir / "install-20261004-100000.log"
        try:
            link.symlink_to(self.f1)
        except (OSError, NotImplementedError):
            self.skipTest("нет символьных ссылок")
        self.assertNotIn(link, logctl.install_logs())

    def test_vacuum_request_is_whitelisted(self):
        self.c.get("/logs")
        for rule in ("vacuum-time:1y", "vacuum-time:7d; reboot", "vacuum-size:1G --rotate", "rotate:1", "7d", "",
                     "vacuum-time:", "vacuum-size:5T", "vacuum-time:-1d"):
            self.c.post("/logs/vacuum", {"rule": rule})
        self.assertEqual(list(logctl.req_dir().glob("*.json")) if logctl.req_dir().exists() else [], [])
        resp, _ = self.c.post("/logs/vacuum", {"rule": "vacuum-time:7d"})
        self.assertEqual(resp.status, 303)
        files = list(logctl.req_dir().glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual((data["action"], data["value"], data["id"]), ("vacuum-time", "7d", files[0].stem))
        self.assertIn("Заявка принята", self.body("/logs"))
        self.assertIn("Очистка журнала выполняется", self.body("/logs"))
        self.c.post("/logs/vacuum", {"rule": "vacuum-size:100M"})
        self.assertEqual(len(list(logctl.req_dir().glob("*.json"))), 1, "пока первая не выполнена, вторая не принимается")

    def test_vacuum_needs_installed_unit(self):
        self.c.get("/logs")
        with mock.patch("zoolib.system.unit_states", return_value={"zoo-logs.path": {"load": "not-found"}}):
            self.c.post("/logs/vacuum", {"rule": "vacuum-time:7d"})
        self.assertEqual(list(logctl.req_dir().glob("*.json")) if logctl.req_dir().exists() else [], [])
        self.assertIn("zoo-logs.path", self.body("/logs"))

    def test_nav_and_unit_files(self):
        root = Path(__file__).resolve().parents[1] / "systemd"
        path = (root / "zoo-logs.path").read_text(encoding="utf-8")
        svc = (root / "zoo-logs.service").read_text(encoding="utf-8")
        self.assertIn("PathExistsGlob=/var/lib/vpn-zoo/logs-req/*.json", path)
        self.assertIn("Unit=zoo-logs.service", path)
        self.assertIn("ExecStart=/usr/local/bin/zoo logs run", svc)
        for need in ("ProtectSystem=strict", "NoNewPrivileges=yes", "PrivateDevices=yes"):
            self.assertIn(need, svc)
        rw = next(ln for ln in svc.splitlines() if ln.startswith("ReadWritePaths=")).split("=", 1)[1].split()
        self.assertEqual(sorted(p.lstrip("-") for p in rw),
                         ["/run/log/journal", "/var/lib/vpn-zoo", "/var/log/journal", "/var/log/vpn-zoo"])
        self.assertIn("zoo-logs.path", (root / "enable.list").read_text(encoding="utf-8").split())
        web = (root / "zoo-web.service").read_text(encoding="utf-8")
        self.assertIn("ProtectSystem=strict", web)
        self.assertNotIn("/var/log/journal", web.split("ReadWritePaths=")[1].split("\n")[0])


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.jdir = self.env.root / "journal"
        (self.jdir / "m").mkdir(parents=True)
        self.jfile = self.jdir / "m" / "system@1.journal"
        self.jfile.write_bytes(b"j" * 5000)
        p = mock.patch.object(logctl, "JOURNALD_DIRS", (str(self.jdir),))
        p.start()
        self.addCleanup(p.stop)
        self.calls: list[list[str]] = []

        def run(argv, timeout=10.0):
            self.calls.append(argv)
            if argv[1].startswith("--vacuum"):
                self.jfile.write_bytes(b"j" * 1200)
            return 0, "", ""
        p = mock.patch("zoolib.logctl.system.run", side_effect=run)
        p.start()
        self.addCleanup(p.stop)

    def test_runs_whitelisted_action_and_reports_freed_space(self):
        rid = logctl.submit("vacuum-time", "7d")
        self.assertEqual(logctl.run_queue(), 0)
        self.assertEqual(self.calls, [["journalctl", "--rotate"], ["journalctl", "--vacuum-time=7d"]])
        st = logctl.states(1)[0]
        self.assertEqual((st["id"], st["status"], st["freed"], st["before"], st["after"]), (rid, "ok", 3800, 5000, 1200))
        self.assertEqual(logctl.pending(), [])

    def test_request_is_removed_even_when_state_write_fails(self):
        logctl.submit("vacuum-time", "7d")
        with mock.patch.object(logctl, "atomic_write_json", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(logctl.run_queue(), 1)
        self.assertEqual(logctl.pending(), [], "иначе zoo-logs.path перезапускает заявку до trigger-limit-hit")
        self.assertEqual(len([c for c in self.calls if c[1].startswith("--vacuum")]), 1)

    def test_size_rule(self):
        logctl.submit("vacuum-size", "200M")
        logctl.run_queue()
        self.assertIn(["journalctl", "--vacuum-size=200M"], self.calls)

    def test_tampered_requests_are_rejected_without_running_anything(self):
        d = logctl.req_dir()
        d.mkdir(parents=True)
        bad = [("a" * 32, {"id": "a" * 32, "action": "vacuum-time", "value": "7d; reboot"}),
               ("b" * 32, {"id": "b" * 32, "action": "rm", "value": "7d"}),
               ("c" * 32, {"id": "other", "action": "vacuum-time", "value": "7d"}),
               ("d" * 32, {"id": "d" * 32, "action": "vacuum-size", "value": "--rotate"})]
        for rid, data in bad:
            (d / f"{rid}.json").write_text(json.dumps(data), encoding="utf-8")
        (d / "notjson.json").write_text("{", encoding="utf-8")
        (d / ("e" * 32 + ".json")).write_text("[1]", encoding="utf-8")
        logctl.run_queue()
        self.assertEqual(self.calls, [])
        self.assertEqual({s["status"] for s in logctl.states(10)}, {"fail"})
        self.assertEqual(logctl.pending(), [])

    def symlink(self, link, target):
        try:
            Path(link).symlink_to(target, target_is_directory=Path(target).is_dir())
        except (OSError, NotImplementedError):
            self.skipTest("нет символьных ссылок")

    def test_symlinked_request_dirs_are_refused_by_the_root_runner(self):
        victim = self.env.root / "victim"
        victim.mkdir()
        rid = "a" * 32
        req = json.dumps({"id": rid, "action": "vacuum-time", "value": "7d"})
        (victim / f"{rid}.json").write_text(req, encoding="utf-8")
        self.symlink(logctl.req_dir(), victim)             # logs-req -> чужой каталог
        self.assertEqual(logctl.run_queue(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(sorted(p.name for p in victim.iterdir()), [f"{rid}.json"], "в чужой каталог ничего не писали")
        logctl.req_dir().unlink()
        logctl.req_dir().mkdir()
        (logctl.req_dir() / f"{rid}.json").write_text(req, encoding="utf-8")
        self.symlink(logctl.state_dir(), victim)           # logs-req/state -> чужой каталог
        self.assertEqual(logctl.run_queue(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(sorted(p.name for p in victim.iterdir()), [f"{rid}.json"])
        logctl.state_dir().unlink()
        self.assertEqual(logctl.run_queue(), 0, "обычные каталоги — работает как прежде")
        self.assertEqual(len(self.calls), 2)

    def test_symlinked_state_root_is_refused(self):
        victim = self.env.root / "victim"
        victim.mkdir()
        real = self.env.root / "state"
        moved = self.env.root / "state-real"
        real.rename(moved)
        self.symlink(real, victim)
        self.assertEqual(logctl.run_queue(), 1)
        self.assertEqual(list(victim.iterdir()), [], "каталог данных — ссылка: ни замка, ни заявок")
        self.assertEqual(self.calls, [])

    def test_request_that_is_a_symlink_is_not_read(self):
        target = self.env.root / "elsewhere.json"
        rid = "a" * 32
        target.write_text(json.dumps({"id": rid, "action": "vacuum-time", "value": "7d"}), encoding="utf-8")
        logctl.req_dir().mkdir(parents=True)
        self.symlink(logctl.req_dir() / f"{rid}.json", target)
        self.assertIsNone(logctl._read(logctl.req_dir() / f"{rid}.json"))
        self.assertEqual(logctl.pending(), [])
        self.assertEqual(logctl.run_queue(), 0)
        self.assertEqual(self.calls, [], "по заявке-ссылке journalctl не запускается")
        self.assertEqual(logctl.states(1)[0]["status"], "fail")
        self.assertTrue(target.exists(), "цель ссылки цела, сама ссылка убрана")
        self.assertFalse((logctl.req_dir() / f"{rid}.json").is_symlink())

    def test_unsafe_directory_check_without_real_symlinks(self):
        # то же без ссылок ФС (на Windows их может не быть): lstat подсказывает «ссылка»
        from zoolib import fsutil
        real = os.lstat(self.env.root)
        fake = os.stat_result((stat.S_IFLNK | 0o777,) + tuple(real)[1:])
        with mock.patch("zoolib.fsutil.os.lstat", return_value=fake):
            with self.assertRaises(fsutil.UnsafePath):
                fsutil.check_real_dirs(self.env.root)
            self.assertEqual(logctl.run_queue(), 1)
        self.assertEqual(self.calls, [])
        fsutil.check_real_dirs(self.env.root, self.env.root / "нет такого")

    def test_failure_is_reported(self):
        with mock.patch("zoolib.logctl.system.run", return_value=(1, "", "Failed to vacuum: Permission denied")):
            logctl.submit("vacuum-time", "7d")
            logctl.run_queue()
        st = logctl.states(1)[0]
        self.assertEqual(st["status"], "fail")
        self.assertIn("Permission denied", st["error"])

    def test_second_request_waits_but_stale_one_does_not_block(self):
        rid = logctl.submit("vacuum-time", "7d")
        with self.assertRaises(logctl.CleanError):
            logctl.submit("vacuum-size", "1G")
        path = logctl.req_dir() / (rid + ".json")
        path.write_text(json.dumps({"id": rid, "action": "vacuum-time", "value": "7d",
                                    "created": int(time.time() - 3600)}), encoding="utf-8")
        self.assertTrue(logctl.submit("vacuum-size", "1G"), "заявка, которую никто не забрал, не держит очередь")

    def test_clean_files_unit(self):
        d = self.env.root / "logs"
        d.mkdir()
        with mock.patch.dict(os.environ, {"LOG_DIR": d.as_posix()}):
            for i in range(5):
                f = d / f"install-{i}.log"
                f.write_bytes(b"x" * 100)
                os.utime(f, (BASE + i, BASE + i))
            res = logctl.clean_files(names=[f"install-{i}.log" for i in range(5)], keep=2)
            self.assertEqual((sorted(res["deleted"]), res["freed"], sorted(res["skipped"])),
                             (["install-0.log", "install-1.log", "install-2.log"], 300,
                              ["install-3.log", "install-4.log"]))
            with self.assertRaises(logctl.CleanError):
                logctl.clean_files(older_days=13)

    def test_trim_file_unit(self):
        d = self.env.root / "logs"
        d.mkdir()
        with mock.patch.dict(os.environ, {"LOG_DIR": d.as_posix()}):
            f = d / "install-1.log"
            full = "".join(f"line {i}\n" for i in range(5000))
            f.write_bytes(full.encode())
            res = logctl.trim_file("install-1.log", 100)
            self.assertEqual((res["trimmed"], res["kept"]), (True, 100))
            got = f.read_text(encoding="utf-8").splitlines()
            self.assertEqual((len(got), got[0], got[-1]), (100, "line 4900", "line 4999"))
            self.assertEqual(res["freed"], len(full) - f.stat().st_size)
            self.assertFalse(logctl.trim_file("install-1.log", 100)["trimmed"], "уже короче — не трогаем")
            f.write_bytes(b"a\nb\nc")
            self.assertEqual(logctl.trim_file("install-1.log", 2)["trimmed"], True)
            self.assertEqual(f.read_bytes(), b"b\nc")
            with self.assertRaises(logctl.CleanError):
                logctl.trim_file("../x.log")

    def test_cli(self):
        from zoolib import cli
        logctl.submit("vacuum-time", "7d")
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["logs", "run"]), 0)
            self.assertEqual(cli.main(["logs", "list", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())[0]["status"], "ok")
            self.assertEqual(cli.main(["logs", "vacuum"]), 2)
            self.assertEqual(cli.main(["logs", "vacuum", "--size", "100M"]), 0)
            self.assertEqual(cli.main(["logs", "clean"]), 2)


class InstallLogOrderTest(unittest.TestCase):
    """Новизна и возраст логов установки — по времени в имени; обрезка не должна ни менять порядок, ни резать лог,
    в который прямо сейчас пишет install.sh."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="zoo-logord-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.dict(os.environ, {"LOG_DIR": self.dir.as_posix(), "ZOO_SETUP_LOCK": (self.dir / "setup.lock").as_posix()})
        p.start()
        self.addCleanup(p.stop)
        self.old = self.dir / "install-20261001-100000.log"
        self.new = self.dir / "install-20261003-100000.log"
        write_lines(self.old, [f"строка {i:04d}" for i in range(1500)])
        write_lines(self.new, [f"новая {i:04d}" for i in range(1500)])
        os.utime(self.old, (BASE, BASE))
        os.utime(self.new, (BASE + 100, BASE + 100))

    def names(self):
        return sorted(f.name for f in self.dir.iterdir() if f.name.startswith("install-"))

    def test_trim_does_not_make_old_log_the_newest(self):
        """Старый лог обрезали (mtime стал «сейчас») — самым свежим остаётся тот, что новее по имени."""
        self.assertTrue(logctl.trim_file(self.old.name)["trimmed"])
        os.utime(self.old, (BASE + 999_999, BASE + 999_999))   # как если бы обрезка сдвинула mtime
        self.assertEqual([f.name for f in logctl.install_logs()], [self.old.name, self.new.name])
        res = logctl.clean_files(names=[self.old.name, self.new.name])
        self.assertEqual((res["deleted"], res["skipped"]), ([self.old.name], [self.new.name]))
        self.assertEqual(self.names(), [self.new.name])

    def test_older_than_uses_name_not_mtime(self):
        now = time.mktime(time.strptime("20261010-100000", "%Y%m%d-%H%M%S"))
        os.utime(self.old, (now, now))   # mtime «свежий», имя — девять дней назад
        res = logctl.clean_files(older_days=7, now=now)
        self.assertEqual(res["deleted"], [self.old.name])
        write_lines(self.old, ["x"])
        os.utime(self.old, (0, 0))   # mtime древний, имя — сегодня
        res = logctl.clean_files(older_days=1, now=time.mktime(time.strptime("20261001-120000", "%Y%m%d-%H%M%S")))
        self.assertEqual(res["deleted"], [])

    def test_unparseable_name_falls_back_to_mtime(self):
        odd = self.dir / "install-manual.log"
        write_lines(odd, ["x"])
        later = logctl.log_stamp(self.new) + 500
        os.utime(odd, (later, later))
        self.assertEqual([f.name for f in logctl.install_logs()][-1], odd.name)
        bad_date = self.dir / "install-20269999-999999.log"
        write_lines(bad_date, ["x"])
        os.utime(bad_date, (BASE + 600, BASE + 600))
        self.assertEqual(logctl.log_stamp(bad_date), BASE + 600)

    def test_trim_restores_times(self):
        before = self.old.stat()
        self.assertTrue(logctl.trim_file(self.old.name)["trimmed"])
        after = self.old.stat()
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_size < before.st_size, True)

    def test_trim_refuses_newest_while_install_holds_lock(self):
        size = self.new.stat().st_size
        with mock.patch.object(logctl, "install_running", return_value=True):
            with self.assertRaises(logctl.CleanError) as cm:
                logctl.trim_file(self.new.name)
            self.assertIn("идёт установка", str(cm.exception))
            self.assertTrue(logctl.trim_file(self.old.name)["trimmed"], "не самый свежий обрезать можно")
            self.assertEqual(logctl.clean_files(names=[self.new.name])["skipped"], [self.new.name],
                             "удалить самый свежий нельзя и без блокировки")
        self.assertEqual(self.new.stat().st_size, size)
        with mock.patch.object(logctl, "install_running", return_value=False):
            self.assertTrue(logctl.trim_file(self.new.name)["trimmed"])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and logctl.fcntl, "нужен flock (Linux)")
    def test_install_running_probes_real_flock(self):
        lock = self.dir / "setup.lock"
        self.assertFalse(logctl.install_running(), "файла блокировки нет")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertFalse(logctl.install_running(), "файл есть, но никто не держит")
            logctl.fcntl.flock(fd, logctl.fcntl.LOCK_EX | logctl.fcntl.LOCK_NB)
            self.assertTrue(logctl.install_running())
            with self.assertRaises(logctl.CleanError):
                logctl.trim_file(self.new.name)
        finally:
            os.close(fd)
        self.assertFalse(logctl.install_running(), "держатель вышел")
        self.assertFalse(logctl.install_running(), "проба сама блокировку не оставляет")

    def test_bytes_appended_during_trim_are_kept(self):
        real = logctl._tail_of
        appended = "дописано установкой\n".encode()

        def racing(fh, size, lines):
            out = real(fh, size, lines)
            with open(self.old, "ab") as w:
                w.write(appended)
            return out

        with mock.patch.object(logctl, "_tail_of", racing):
            res = logctl.trim_file(self.old.name)
        self.assertTrue(res["trimmed"])
        data = self.old.read_bytes()
        self.assertTrue(data.endswith("строка 1499\n".encode() + appended), data[-80:])
        self.assertEqual(data.count(b"\n"), logctl.TAIL_LINES + 1)

    def test_web_menu_disables_trim_of_newest_while_running(self):
        from tests.test_web import AppTestBase

        class Page(AppTestBase):
            def runTest(self):
                pass

        page = Page()
        page.setUp()
        try:
            page.env.write_config({"SERVER_IP": "10.0.0.1", "ZOO_WEB_TOKEN": SECRET})
            page.c.login()
            with mock.patch.object(logctl, "install_running", return_value=True):
                resp, body = page.c.get("/logs")
            self.assertEqual(resp.status, 200)
            self.assertEqual(body.count("идёт установка и пишет в этот лог"), 1)
            with mock.patch.object(logctl, "install_running", return_value=False):
                resp, body = page.c.get("/logs")
            self.assertNotIn("идёт установка и пишет в этот лог", body)
        finally:
            page.doCleanups()


if __name__ == "__main__":
    unittest.main()
