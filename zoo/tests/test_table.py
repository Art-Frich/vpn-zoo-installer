"""Общий компонент таблиц (web/table.py, BACKLOG п. 12): разбор адреса по белым спискам, keyset-выдача из SQLite
и из памяти, разметка, выгрузка с приватными полями; страницы: история прогонов, пользователи, трафик, версии."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import time
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import needs_bash
from tests.test_history import EGRESS_IP, report, result
from tests.test_web import AppTestBase, header, visible_words
from zoolib.probe import history
from zoolib.web import assets, probeviews
from zoolib.web import table as tbl
from zoolib.web.html import Markup, t

EVIL = "%22%3E%3Cscript%3Ealert(1)%3C%2Fscript%3E"
NOW = int(time.time())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def fill(con: sqlite3.Connection, n: int = 40, same_ts: bool = False) -> None:
    """n прогонов: метки и устройства чередуются; same_ts — одно время у всех (проверка ничьих при курсоре)."""
    for i in range(n):
        ts = iso(NOW - 3600 if same_ts else NOW - i * 3600)
        rep = report([result("hysteria2", lat=40 + i), result("vless-reality", "FREEZE_16K" if i % 3 == 0 else "OK",
                                                              down=None if i % 3 == 0 else 20)],
                     ts=ts, tag=["mobile-mts", "home-wifi", "cafe"][i % 3], device=["pixel7", "iphone"][i % 2],
                     isp="MTS PJSC" if i % 2 else "Ростелеком", label=f"vpn{i}")
        history.record(rep, "upload", con=con)


def spec() -> tbl.Spec:
    return probeviews._runs_spec("30d", lambda r: f"/probe?run={r['id']}")


def page(con, query="", **kw):
    sp = spec()
    q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
    opts = tbl.sql_options(con, sp, "reports r")
    st = tbl.parse(sp, q, opts)
    st = replace(st, **kw) if kw else st
    return st, tbl.sql_page(con, sp, st, "reports r", "1", [], probeviews.RUN_EXTRA, opts)


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.sp = spec()
        self.opts = {"tag": [("cafe", 3), ("home-wifi", 2)]}

    def test_whitelists(self):
        st = tbl.parse(self.sp, {"h_sort": "-working", "h_n": "50", "h_q": "  mts   pixel ", "h_f_tag": "cafe"}, self.opts)
        self.assertEqual((st.sort, st.desc, st.n, st.q, st.filters), ("working", True, 50, "mts pixel", (("tag", "cafe"),)))
        for bad in ("ts; DROP TABLE reports", "r.id", "-", "--ts", "id", "COALESCE(r.tag,'')", "tag desc", ""):
            st = tbl.parse(self.sp, {"h_sort": bad})
            self.assertEqual((st.sort, st.desc), ("ts", True), bad)
        for bad in ("99999", "-5", "abc", "1e3", "15; --", ""):
            self.assertEqual(tbl.parse(self.sp, {"h_n": bad}).n, tbl.ROWS[0], bad)

    def test_filter_value_must_be_offered(self):
        self.assertEqual(tbl.parse(self.sp, {"h_f_tag": "x' OR '1'='1"}, self.opts).filters, ())
        self.assertEqual(tbl.parse(self.sp, {"h_f_tag": "cafe"}).filters, (), "без списка значений фильтра нет")
        self.assertEqual(tbl.parse(self.sp, {"h_f_isp": "MTS"}, {"isp": [("MTS", 1)]}).filters, (), "не чип-колонка")

    def test_limits_and_non_searchable(self):
        self.assertEqual(len(tbl.parse(self.sp, {"h_q": "а" * 5000}).q), tbl.QUERY_MAX)
        self.assertEqual(len(tbl.parse(self.sp, {"h_after": "9" * 5000}).after), tbl.CURSOR_MAX)
        plain = tbl.Spec("/x", [tbl.Col("a", "a", sort=True)], sort="a")
        self.assertEqual(tbl.parse(plain, {"q": "найти"}).q, "", "без поисковых колонок поле поиска не работает")

    def test_href_omits_defaults_and_keeps_page_params(self):
        sp = replace(self.sp, keep=[("rp", "7d")])
        st = tbl.parse(sp, {})
        self.assertEqual(tbl.href(sp, st), "/probe?rp=7d")
        self.assertEqual(tbl.href(self.sp, st), "/probe")
        st2 = tbl.parse(sp, {"h_q": "a b", "h_sort": "tag", "h_n": "50", "h_f_tag": "cafe", "h_after": "5|3"}, self.opts)
        url = tbl.href(sp, st2)
        back = tbl.parse(sp, {k: v[-1] for k, v in urllib.parse.parse_qs(url.split("?", 1)[1]).items()}, self.opts)
        self.assertEqual(back, st2, "ссылка восстанавливает то же состояние")
        self.assertNotIn("after", tbl.href(sp, st2, after=""))
        self.assertIn("h_export=csv", tbl.href(sp, st, export="csv"))
        self.assertIn("h_priv=1", tbl.href(sp, st, export="csv", priv=True))

    def test_export_request(self):
        self.assertEqual(tbl.export_request(self.sp, {"h_export": "csv"}), ("csv", False))
        self.assertEqual(tbl.export_request(self.sp, {"h_export": "json", "h_priv": "1"}), ("json", True))
        for bad in ("xlsx", "CSV", "csv; rm", ""):
            self.assertIsNone(tbl.export_request(self.sp, {"h_export": bad}), bad)
        self.assertIsNone(tbl.export_request(replace(self.sp, export=False), {"h_export": "csv"}))


class SqlPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = history.connect(Path(self.tmp.name) / "h.sqlite")
        self.addCleanup(self.con.close)

    def walk(self, query="", **kw):
        """Все страницы по курсору → (id по порядку, число страниц, последняя страница)."""
        sp = spec()
        opts = tbl.sql_options(self.con, sp, "reports r")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
        st = tbl.parse(sp, q, opts)
        ids, pages, seen_before = [], 0, []
        while True:
            pg = tbl.sql_page(self.con, sp, st, "reports r", "1", [], probeviews.RUN_EXTRA, opts)
            pages += 1
            seen_before.append((pg.before, len(ids)))
            ids += [r["id"] for r in pg.rows]
            if not pg.next:
                return ids, pages, pg
            st = replace(st, after=pg.next)
            self.assertLess(pages, 200)

    def test_keyset_walks_every_row_once_in_every_sort(self):
        fill(self.con, 40)
        everything = [r[0] for r in self.con.execute("SELECT id FROM reports")]
        for sort in ("ts", "-ts", "tag", "-tag", "mode", "isp", "-isp", "country", "device", "net", "working", "-working"):
            ids, pages, last = self.walk("h_sort=" + sort)
            self.assertEqual(sorted(ids), sorted(everything), sort)
            self.assertEqual(len(ids), len(set(ids)), f"{sort}: без повторов")
            self.assertEqual(pages, 3, sort)
            self.assertEqual(last.total, 40)
        # порядок совпадает с полной сортировкой в SQL
        ids, _, _ = self.walk("h_sort=-working")
        full = [r[0] for r in self.con.execute(
            f"SELECT r.id FROM reports r ORDER BY {probeviews.WORKING_SQL} DESC, r.id DESC")]
        self.assertEqual(ids, full)

    def test_ties_on_sort_key_do_not_skip_or_repeat(self):
        fill(self.con, 40, same_ts=True)  # у всех одно время: порядок решает только id
        ids, pages, _ = self.walk("h_n=15")
        self.assertEqual(sorted(ids), list(range(1, 41)))
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(pages, 3)

    def test_shown_counter_follows_cursor(self):
        fill(self.con, 40)
        sp = spec()
        st = tbl.parse(sp, {})
        p1 = tbl.sql_page(self.con, sp, st, "reports r", "1", [], probeviews.RUN_EXTRA)
        self.assertEqual((p1.before, len(p1.rows), p1.total), (0, 15, 40))
        p2 = tbl.sql_page(self.con, sp, replace(st, after=p1.next), "reports r", "1", [], probeviews.RUN_EXTRA)
        self.assertEqual((p2.before, len(p2.rows)), (15, 15))
        p3 = tbl.sql_page(self.con, sp, replace(st, after=p2.next), "reports r", "1", [], probeviews.RUN_EXTRA)
        self.assertEqual((p3.before, len(p3.rows), p3.next), (30, 10, None))
        html = tbl.render(sp, replace(st, after=p2.next), p3)
        self.assertIn("показано 40 из 40", html)
        self.assertNotIn("показать ещё", html)

    def test_filters_and_search(self):
        fill(self.con, 40)
        _, pg = page(self.con, "h_f_tag=cafe")
        self.assertEqual(pg.total, 13)
        self.assertTrue(all(r["tag"] == "cafe" for r in pg.rows))
        _, pg = page(self.con, "h_q=ростелеком")
        self.assertEqual(pg.total, 20, "поиск без учёта регистра и по-русски")
        _, pg = page(self.con, "h_q=iphone+mts")
        self.assertEqual(pg.total, 20)
        _, pg = page(self.con, "h_q=iphone+ростелеком")
        self.assertEqual(pg.total, 0)
        _, pg = page(self.con, "h_q=%D0%A0%D0%BE%D1%81&h_f_tag=cafe")
        self.assertEqual(pg.total, 7)
        self.assertEqual(page(self.con, "h_q=AS8359")[1].total, 0, "поиск идёт по колонкам, не по AS-подписи")

    def test_injection_in_every_part(self):
        fill(self.con, 20)
        tables = lambda: {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}  # noqa: E731
        before = tables()
        n = self.con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        for q in ("h_q=%27%3B+DROP+TABLE+reports%3B+--", "h_q=%25%27+OR+1%3D1+--", "h_sort=ts%3BDROP+TABLE+reports",
                  "h_f_tag=%27+OR+%271%27%3D%271", "h_after=5%7C1%3BDROP", "h_after=%27%7C%27",
                  "h_after=nan%7C1", "h_after=inf%7C1", "h_after=9e999%7C3", "h_after=%7C", "h_after=-1%7C-1",
                  "h_n=1%3BDROP", "h_q=" + "%25" * 500, "h_q=%00%00", "h_f_tag=" + "a" * 5000):
            st, pg = page(self.con, q)
            self.assertIsInstance(pg.rows, list, q)
        self.assertEqual(tables(), before)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM reports").fetchone()[0], n)
        # поиск строкой — подстрока, а не шаблон: «%» и «_» ничего не открывают
        self.assertEqual(page(self.con, "h_q=%25")[1].total, 0)
        self.assertEqual(page(self.con, "h_q=_____")[1].total, 0)

    def test_bad_cursor_means_from_start(self):
        fill(self.con, 20)
        _, first = page(self.con, "")
        for bad in ("garbage", "1|x", "x|1", "|", "1.5|2|3"):
            _, pg = page(self.con, "h_after=" + urllib.parse.quote(bad))
            self.assertEqual([r["id"] for r in pg.rows], [r["id"] for r in first.rows], bad)

    def test_chip_options_skip_single_value_and_overflow(self):
        fill(self.con, 40)
        sp = spec()
        opts = tbl.sql_options(self.con, sp, "reports r")
        self.assertEqual({k: [v for v, _ in vs] for k, vs in opts.items() if k != "tag"},
                         {"device": ["iphone", "pixel7"]})
        self.assertEqual(sorted(v for v, _ in opts["tag"]), ["cafe", "home-wifi", "mobile-mts"])
        self.assertNotIn("country", opts, "одно значение — не фильтр")
        for i in range(20):
            self.con.execute("UPDATE reports SET tag = ? WHERE id = ?", (f"tag{i}", i + 1))
        self.assertNotIn("tag", tbl.sql_options(self.con, sp, "reports r"), "слишком много значений — не чипы")

    def test_empty_database(self):
        sp = spec()
        st = tbl.parse(sp, {})
        pg = tbl.sql_page(self.con, sp, st, "reports r", "1", [], probeviews.RUN_EXTRA)
        html = tbl.render(sp, st, pg)
        self.assertIn("прогонов ещё нет", html)
        self.assertNotIn("<table", html)
        st = tbl.parse(sp, {"h_q": "нет такого"})
        self.assertIn("Ничего не нашлось", tbl.render(sp, st, tbl.sql_page(self.con, sp, st, "reports r", "1", [], ())))


class MemoryPageTest(unittest.TestCase):
    def setUp(self):
        self.rows = [{"name": n, "note": note, "day": d, "access": a} for n, note, d, a in (
            ("anna", "бухгалтерия", 30, "включён"), ("boris", "", 10, "отключён"), ("clara", "подруга Анны", 20, "включён"),
            ("dmitry", "", None, "включён"))]
        self.sp = tbl.Spec("/u", [
            tbl.Col("name", "имя", sort=True, search=True, find=lambda r: r["note"]),
            tbl.Col("day", "24 ч", num=True, sort=True, first_desc=True),
            tbl.Col("access", "доступ", chip=True, hidden=True)], sort="name", id_key="name", paged=False)

    def names(self, query="", **kw):
        opts = tbl.options_from_rows(self.sp, self.rows)
        st = tbl.parse(self.sp, {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}, opts)
        return [r["name"] for r in tbl.memory_page(replace(self.sp, **kw), st, self.rows, opts).rows]

    def test_sort_search_filter(self):
        self.assertEqual(self.names(), ["anna", "boris", "clara", "dmitry"])
        self.assertEqual(self.names("sort=-day"), ["anna", "clara", "boris", "dmitry"], "пустое число — в конец")
        self.assertEqual(self.names("sort=day"), ["dmitry", "boris", "clara", "anna"])
        self.assertEqual(self.names("q=анны"), ["clara"], "поиск идёт и по заметке")
        self.assertEqual(self.names("q=A"), ["anna", "clara"])
        self.assertEqual(self.names("f_access=" + urllib.parse.quote("отключён")), ["boris"])
        self.assertEqual(self.names("f_access=nope"), ["anna", "boris", "clara", "dmitry"])
        self.assertEqual(self.names("sort=name%3Bdrop"), ["anna", "boris", "clara", "dmitry"])

    def test_paged_keyset_by_id(self):
        rows = [{"name": f"u{i:02d}", "day": i % 5} for i in range(40)]
        sp = tbl.Spec("/u", [tbl.Col("name", "имя", sort=True), tbl.Col("day", "д", num=True, sort=True)],
                      sort="day", desc=True, id_key="name")
        st = tbl.parse(sp, {})
        seen, pages = [], 0
        while True:
            pg = tbl.memory_page(sp, st, rows)
            seen += [r["name"] for r in pg.rows]
            pages += 1
            self.assertEqual(pg.before, len(seen) - len(pg.rows))
            if not pg.next:
                break
            st = replace(st, after=pg.next)
        self.assertEqual((len(seen), len(set(seen)), pages), (40, 40, 3))
        self.assertEqual(tbl.memory_page(sp, replace(st, after="nope"), rows).before, 0, "чужой курсор — с начала")


class RenderTest(unittest.TestCase):
    def sample(self, **kw):
        sp = tbl.Spec("/x", [tbl.Col("a", "имя", sort=True, search=True, chip=True),
                             tbl.Col("b", "число", num=True, sort=True, secondary=True),
                             tbl.Col("c", "тайна", private=True, secondary=True)],
                      sort="a", id_key="a", **kw)
        rows = [{"a": "x", "b": 3, "c": "1.2.3.4"}, {"a": "y", "b": 5, "c": "5.6.7.8"}]
        opts = tbl.options_from_rows(sp, rows)
        return sp, rows, opts

    def test_header_links_toggle_direction_and_mark_current(self):
        sp, rows, opts = self.sample()
        st = tbl.parse(sp, {}, opts)
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows, opts))
        self.assertIn('aria-sort="ascending"', html)
        self.assertIn('href="/x?sort=-a"', html, "второй клик по текущему — в обратную сторону")
        self.assertIn('href="/x?sort=-b"', html, "числа сначала по убыванию")
        self.assertRegex(html, r'<th class="num sec"[^>]*><a href="/x\?sort=-b" data-swap')

    def test_row_details_for_secondary_columns_and_no_inline_js_or_css(self):
        sp, rows, opts = self.sample()
        st = tbl.parse(sp, {}, opts)
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows, opts))
        self.assertEqual(html.count('class="det" hidden'), 2)
        self.assertIn('class="exp"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('<td class="num sec">3</td>', html)
        self.assertIn('<dl class="kv sec-kv"><dt>число</dt><dd>3</dd>', html)
        self.assertNotRegex(html, r"\sstyle=|\son[a-z]+=|<script|javascript:")
        spd = replace(sp, href=lambda r: f"/x/{r['a']}")
        html = tbl.render(spd, st, tbl.memory_page(spd, st, rows, opts))
        self.assertIn('data-href="/x/x"', html)
        self.assertNotIn("det", html.replace("detail", ""), "у строк со ссылкой раскрытия нет")

    def test_chips_pressed_and_toggle_off(self):
        sp, rows, opts = self.sample()
        st = tbl.parse(sp, {"f_a": "x"}, opts)
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows, opts))
        self.assertIn('class="chip info" data-swap aria-pressed="true"', html)
        self.assertIn('href="/x"', html, "повторный клик по включённому чипу снимает фильтр")
        self.assertIn("сбросить", html)
        self.assertIn("показано 1 из 1", html, "с фильтром число видно и без пагинации")

    def test_count_hidden_for_plain_unfiltered_unpaged(self):
        sp, rows, opts = self.sample(paged=False, export=False)
        st = tbl.parse(sp, {}, opts)
        self.assertNotIn("показано", tbl.render(sp, st, tbl.memory_page(sp, st, rows, opts)))

    def test_hostile_values_are_escaped(self):
        sp = tbl.Spec("/x", [tbl.Col("a", "имя", sort=True, search=True)], sort="a", id_key="a")
        rows = [{"a": "<script>alert(1)</script>"}]
        st = tbl.parse(sp, {"q": '"><img src=x onerror=1>'})
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows))
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_select_column_header_checkbox_and_form_bound_row_checkboxes(self):
        sp, rows, opts = self.sample(select="names", select_form="bulk")
        st = tbl.parse(sp, {}, opts)
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows, opts))
        self.assertIn('<th class="pick" scope="col"><input type="checkbox" data-pick-all aria-label="Выбрать всех"></th>', html)
        self.assertNotIn('data-pick-all name=', html, "общая галочка в форму не уходит")
        for r in ("x", "y"):
            self.assertIn(f'<td class="pick"><input type="checkbox" name="names" value="{r}" form="bulk" data-pick '
                          f'aria-label="Выбрать {r}"></td>', html)
        # раскрывающаяся строка занимает и колонку галочек
        self.assertEqual(html.count('<td colspan="4">'), 2)
        # клик по галочке не открывает строку: app.js пропускает input, кнопка раскрытия остаётся в первой колонке данных
        self.assertRegex(html, r'<td class="pick">.*?</td><td><button type="button" class="exp"')

    def test_no_select_column_by_default(self):
        sp, rows, opts = self.sample()
        st = tbl.parse(sp, {}, opts)
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows, opts))
        self.assertNotIn("data-pick", html)
        self.assertNotIn('class="pick"', html)

    def test_select_hostile_ids_are_escaped_and_export_has_no_checkbox_column(self):
        sp = tbl.Spec("/x", [tbl.Col("a", "имя", sort=True)], sort="a", id_key="a", select="names", select_form="bulk")
        evil = '"><script>alert(1)</script>'
        rows = [{"a": evil}]
        st = tbl.parse(sp, {})
        html = tbl.render(sp, st, tbl.memory_page(sp, st, rows))
        self.assertNotIn("<script>", html)
        self.assertIn("&quot;&gt;&lt;script&gt;", html)
        self.assertNotRegex(html, r"\son[a-z]+=|\sstyle=")
        body, _, _ = tbl.download(sp.cols, rows, "csv")
        self.assertNotIn(b"pick", body)

    def test_select_js_contract(self):
        self.assertIn("input[data-pick]:checked", assets.JS)
        self.assertIn("a, button, input, select, textarea, label, summary, form", assets.JS,
                      "клик по галочке строки не уводит на страницу")
        self.assertIn(".bulkbar", assets.CSS)
        self.assertIn("position: sticky", assets.CSS)


class ExportTest(unittest.TestCase):
    cols = [tbl.Col("name", "имя"), tbl.Col("ip", "адрес", private=True), tbl.Col("bar", "", export=False),
            tbl.Col("n", "число", num=True), tbl.Col("x", "икс", value=lambda r: Markup("<b>1</b>"))]
    rows = [{"name": "=HYPERLINK(\"http://evil\")", "ip": "1.2.3.4", "bar": "...", "n": 5},
            {"name": "+1", "ip": "5.6.7.8", "bar": "", "n": None}, {"name": "ok, \"q\"", "ip": "", "bar": "", "n": 0.5}]

    def test_private_columns_need_explicit_flag(self):
        for fmt in tbl.FORMATS:
            body, ctype, name = tbl.download(self.cols, self.rows, fmt)
            self.assertNotIn(b"1.2.3.4", body, fmt)
            self.assertNotIn("адрес".encode(), body, fmt)
            self.assertNotIn(b"...", body, "служебные колонки не выгружаются")
            body, _, _ = tbl.download(self.cols, self.rows, fmt, private=True)
            self.assertIn(b"1.2.3.4", body, fmt)

    def test_csv_shape_and_formula_guard(self):
        body, ctype, name = tbl.download(self.cols, self.rows, "csv")
        self.assertTrue(ctype.startswith("text/csv"))
        self.assertEqual(name, "table.csv")
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"), "BOM: Excel читает кириллицу")
        got = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(got[0], ["имя", "число", "икс"])
        self.assertEqual(got[1][0], "'=HYPERLINK(\"http://evil\")")
        self.assertEqual(got[2][0], "'+1")
        self.assertEqual(got[3], ['ok, "q"', "0.5", "<b>1</b>"])
        self.assertEqual(got[2][1], "")

    def test_json_shape(self):
        body, ctype, name = tbl.download(self.cols, self.rows, "json", name="x")
        self.assertEqual(name, "x.json")
        data = json.loads(body)
        self.assertEqual(list(data[0]), ["name", "n", "x"])
        self.assertEqual(data[1]["n"], None)


# ---------- страницы ----------

def put(n=40, same_ts=False):
    con = history.connect()
    fill(con, n, same_ts)
    con.close()


class ProbeHistoryPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.c.login()
        put(40)

    def test_first_page_count_chips_and_row_links(self):
        _, body = self.c.get("/probe")
        self.assertIn("показано 15 из 40", body)
        self.assertEqual(body.count("data-more>"), 1)
        self.assertEqual(len(re.findall(r'<tr data-href="/probe\?run=\d+&amp;back=', body)), 15)
        self.assertRegex(body, r'<a href="/probe\?h_f_tag=cafe" class="chip" data-swap')
        self.assertRegex(body, r'<a href="/probe\?h_sort=tag" data-swap class="sortlink">метка</a>')
        self.assertIn('<input type="search" name="h_q"', body)
        self.assertIn("выгрузка:", body)

    def test_more_walks_all_pages_and_counter_grows(self):
        _, body = self.c.get("/probe")
        seen = set(re.findall(r'data-href="/probe\?run=(\d+)', body))
        counts = ["показано 15 из 40"]
        while True:
            m = re.search(r'href="([^"]*)" class="btn small" data-more', body)
            if not m:
                break
            href = m.group(1).replace("&amp;", "&")
            self.assertIn("h_after=", href)
            self.assertNotIn("offset", href.lower())
            _, body = self.c.get(href, headers={"X-Zoo-Live": "1"})
            new = set(re.findall(r'data-href="/probe\?run=(\d+)', body))
            self.assertFalse(seen & new, "строки не повторяются")
            seen |= new
            counts.append(re.search(r"показано \d+ из \d+", body).group(0))
        self.assertEqual(len(seen), 40)
        self.assertEqual(counts, ["показано 15 из 40", "показано 30 из 40", "показано 40 из 40"])

    def test_filter_search_sort_in_url(self):
        _, body = self.c.get("/probe?h_f_tag=cafe")
        self.assertIn("показано 13 из 13", body)
        self.assertRegex(body, r'class="chip info" data-swap aria-pressed="true"[^>]*>cafe</a>')
        _, body = self.c.get("/probe?h_q=" + urllib.parse.quote("ростелеком"))
        self.assertIn("из 20", body)
        self.assertIn('value="ростелеком"', body)
        _, body = self.c.get("/probe?h_q=zzzz")
        self.assertIn("Ничего не нашлось", body)
        self.assertNotIn("показать ещё", body)
        _, asc = self.c.get("/probe?h_sort=tag&h_n=100")
        tags = re.findall(r'<tr data-href[^>]*><td><a [^>]*>[^<]*</a></td><td class="sec">[^<]*</td><td>([^<]*)</td>', asc)
        self.assertEqual(tags, sorted(tags))
        self.assertEqual(len(tags), 40)
        _, body = self.c.get("/probe?rp=7d&h_sort=-working")
        self.assertIn("rp=7d", body, "период страницы сохраняется в ссылках таблицы")

    def test_injection_and_html_in_every_param(self):
        for q in (f"h_q={EVIL}", f"h_sort={EVIL}", f"h_f_tag={EVIL}", f"h_after={EVIL}", f"h_n={EVIL}", f"rp={EVIL}",
                  f"run={EVIL}", f"h_export={EVIL}", "h_q=%27+OR+1%3D1+--", "h_after=1%7C%27%3BDROP", "h_n=99999",
                  "h_sort=ts%3BDROP+TABLE+reports", "h_f_tag=%27+OR+%271%27%3D%271", "back=%2F%2Fevil.example",
                  f"run=1&back={EVIL}", "h_q=" + "x" * 3000):
            resp, body = self.c.get("/probe?" + q)
            self.assertIn(resp.status, (200, 404), q)
            self.assertNotIn("<script>alert", body, q)
        _, body = self.c.get("/probe?h_q=" + EVIL)
        self.assertIn("&lt;script&gt;", body)
        con = history.connect(create=False)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 40)
        con.close()

    def test_export_csv_and_json_follow_filters_and_hide_nothing_private(self):
        resp, body = self.c.get("/probe?h_f_tag=cafe&h_export=csv")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.content_type.startswith("text/csv"))
        self.assertIn('attachment; filename="probe-history.csv"', header(resp, "Content-Disposition")[0])
        rows = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
        self.assertEqual(rows[0], ["когда", "кто", "метка", "провайдер", "страна", "устройство", "сеть", "работает"])
        self.assertEqual(len(rows), 14, "вся выборка по фильтру, а не одна страница")
        self.assertTrue(all(r[2] == "cafe" for r in rows[1:]))
        resp, body = self.c.get("/probe?h_export=json&h_n=15")
        data = json.loads(body)
        self.assertEqual(len(data), 40)
        self.assertEqual(set(data[0]), {"ts", "mode", "tag", "isp", "country", "device", "net", "working"})
        self.assertNotIn(EGRESS_IP, body)
        resp, _ = self.c.get("/probe?h_export=xml")
        self.assertTrue(resp.content_type.startswith("text/html"), "неизвестный формат — обычная страница")

    def test_run_page_has_every_protocol_and_private_ip(self):
        _, body = self.c.get("/probe")
        href = re.search(r'data-href="([^"]+)"', body).group(1).replace("&amp;", "&")
        resp, run = self.c.get(href)
        self.assertEqual(resp.status, 200)
        for word in ("hysteria2", "vless-reality", "вердикт", "↓ Мбит/с", "джиттер", "потери", "IP выхода", "pixel7",
                     "mobile-mts", EGRESS_IP, "p90"):
            self.assertIn(word, run)
        self.assertIn("← к истории", run)
        self.assertIn('data-confirm="Удалить прогон', run)
        self.assertRegex(run, r'<form method="post" action="/probe/history/\d+/delete"[^>]*data-swap')
        self.assertLessEqual(visible_words(run), 140)

    def test_run_export_hides_ip_unless_asked(self):
        _, ok = self.c.get("/probe?run=2&export=json")
        self.assertNotIn(EGRESS_IP, ok)
        self.assertNotIn("egress_ip", ok)
        _, priv = self.c.get("/probe?run=2&export=json&priv=1")
        data = json.loads(priv)
        self.assertEqual({r["egress_ip"] for r in data}, {EGRESS_IP})
        self.assertEqual({r["proto"] for r in data}, {"hysteria2", "vless-reality"})
        _, csv_ = self.c.get("/probe?run=2&export=csv")
        self.assertNotIn(EGRESS_IP, csv_)
        _, csv_ = self.c.get("/probe?run=2&export=csv&priv=1")
        self.assertIn(EGRESS_IP, csv_)

    def test_run_not_found_and_junk(self):
        resp, body = self.c.get("/probe?run=9999")
        self.assertEqual(resp.status, 404)
        self.assertIn("Нет такого прогона", body)
        resp, body = self.c.get("/probe?run=abc")
        self.assertEqual(resp.status, 200)
        self.assertIn("История прогонов", body)

    def test_back_link_is_local_only(self):
        for bad in ("//evil.example/x", "https://evil.example", "/probe/../users", "/users", "/probe?<x>",
                    "/probe?a=" + "b" * 500, "javascript:alert(1)"):
            _, body = self.c.get("/probe?run=1&back=" + urllib.parse.quote(bad, safe=""))
            self.assertIn('href="/probe" class="btn small" data-swap>← к истории', body, bad)
        good = "/probe?h_f_tag=cafe&h_sort=-working"
        _, body = self.c.get("/probe?run=1&back=" + urllib.parse.quote(good, safe=""))
        self.assertIn(f'href="{good.replace("&", "&amp;")}" class="btn small" data-swap>← к истории', body)
        _, hist = self.c.get("/probe?h_f_tag=cafe&h_sort=-working")
        self.assertIn("back=%2Fprobe%3Fh_sort%3D-working%26h_f_tag%3Dcafe", hist, "строка помнит состояние таблицы")

    def test_delete_run_needs_csrf_and_removes_only_that_run(self):
        resp, _ = self.c.post("/probe/history/3/delete", csrf=False)
        self.assertEqual(resp.status, 403)
        resp, _ = self.c.get("/probe/history/3/delete")
        self.assertEqual(resp.status, 405)
        con = history.connect(create=False)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM results WHERE report_id = 3").fetchone()[0], 2)
        con.close()
        _, run = self.c.get("/probe?run=3")
        resp, _ = self.c.post("/probe/history/3/delete", {"back": "/probe?h_f_tag=cafe"})
        self.assertEqual((resp.status, header(resp, "Location")), (303, ["/probe?h_f_tag=cafe"]))
        _, body = self.c.get("/probe")
        self.assertIn("Прогон удалён", body)
        self.assertIn("показано 15 из 39", body)
        self.assertEqual(self.c.get("/probe?run=3")[0].status, 404)
        con = history.connect(create=False)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 39)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM results WHERE report_id = 3").fetchone()[0], 0, "каскадом")
        con.close()
        resp, _ = self.c.post("/probe/history/3/delete", {"back": "//evil.example"})
        self.assertEqual(header(resp, "Location"), ["/probe"], "повтор и чужой адрес возврата безопасны")
        _, body = self.c.get("/probe")
        self.assertIn("уже нет", body)

    def test_run_stays_in_history_when_other_run_deleted(self):
        self.c.post("/probe/history/5/delete", {})
        self.assertEqual(self.c.get("/probe?run=6")[0].status, 200)

    def test_page_stays_short_and_has_no_inline_code(self):
        _, body = self.c.get("/probe")
        self.assertNotRegex(body, r"\sstyle=|\son[a-z]+=")
        self.assertNotIn("<script>", body)


class PagesTest(AppTestBase):
    def test_probe_page_without_history_file(self):
        self.c.login()
        resp, body = self.c.get("/probe")
        self.assertEqual(resp.status, 200)
        self.assertIn("прогонов ещё нет", body)
        self.assertEqual(self.c.get("/probe?h_export=csv")[0].status, 200)

    def test_js_and_css_contract(self):
        js, css = assets.JS, assets.CSS
        for needle in ("table.dt tr[data-href]", "data-count", "tr.det:not([hidden])", "button.exp", "getElementById(box.id)"):
            self.assertIn(needle, js)
        for needle in ("table.dt .sec { display: none; }", ".sec-kv", 'aria-sort="descending"', "button.exp"):
            self.assertIn(needle, css)
        self.assertRegex(css, r"@media \(max-width: 600px\) \{[^@]*table\.dt \.sec \{ display: none; \}")

    @needs_bash
    def test_users_and_traffic_and_versions(self):
        from zoolib import users
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)
        users.bootstrap()
        self.c.login()
        for name in ("masha", "petya", "anna"):
            self.c.post("/users", {"name": name, "note": "бухгалтерия" if name == "masha" else ""})
        self.c.post("/users/petya/disable", {"back": "/users"})
        _, body = self.c.get("/users")
        self.assertRegex(body, r'<tr data-href="/users/masha">')
        self.assertRegex(body, r'<tr data-href="/users/petya" class="off">')
        self.assertIn('class="search"', body)
        self.assertIn(">включён</a>", body)
        names = lambda b: re.findall(r'<tr data-href="/users/([a-z]+)"', b)  # noqa: E731
        self.assertEqual(names(body), ["anna", "masha", "owner", "petya"])
        self.assertEqual(names(self.c.get("/users?q=" + urllib.parse.quote("БУХГАЛТЕРИЯ"))[1]), ["masha"])
        self.assertEqual(names(self.c.get("/users?q=a")[1]), ["anna", "masha", "petya"])
        self.assertEqual(names(self.c.get("/users?sort=-name")[1]), ["petya", "owner", "masha", "anna"])
        self.assertEqual(names(self.c.get("/users?f_access=" + urllib.parse.quote("отключён"))[1]), ["petya"])
        self.assertEqual(names(self.c.get("/users?sort=-seen")[1]).__len__(), 4)
        for q in (f"q={EVIL}", f"sort={EVIL}", f"f_access={EVIL}", f"after={EVIL}", "n=-1", "sort=name%3Bdrop"):
            resp, b = self.c.get("/users?" + q)
            self.assertEqual(resp.status, 200, q)
            self.assertNotIn("<script>alert", b, q)
        resp, csv_body = self.c.get("/users?export=csv&q=a")
        self.assertEqual(resp.status, 200)
        rows = list(csv.reader(io.StringIO(csv_body.lstrip("﻿"))))
        self.assertEqual(rows[0], ["пользователь", "доступ", "группа", "протоколы", "24 ч", "30 дней", "активность"])
        self.assertEqual([r[0] for r in rows[1:]], ["anna", "masha", "petya"])
        self.assertEqual(self.c.get("/users?export=json&q=zzz")[1].strip(), "[]")
        # трафик по пользователям: порядок и ссылки
        rep = {"rows": [{"key": "masha", "up": 5, "down": 10, "total": 15}, {"key": "anna", "up": 1, "down": 2, "total": 3},
                        {"key": "owner", "up": 100, "down": 200, "total": 300}], "total": {"up": 106, "down": 212, "total": 318}}
        with mock.patch("zoolib.web.views.traffic.report", side_effect=lambda **kw: rep if kw.get("by") == "user" else
                        {"rows": [], "total": {"up": 0, "down": 0, "total": 0}}):
            body = self.c.get("/traffic")[1]
            self.assertEqual(re.findall(r'<tr data-href="/users/([a-z]+)\?period=24h"', body), ["owner", "masha", "anna"])
            body = self.c.get("/traffic?sort=key")[1]
            self.assertEqual(re.findall(r'<tr data-href="/users/([a-z]+)\?period=24h"', body), ["anna", "masha", "owner"])
            self.assertIn("period=24h", self.c.get("/traffic?sort=key&period=7d")[1])
            self.assertEqual(re.findall(r'<tr data-href="/users/([a-z]+)\?period=7d"',
                                        self.c.get("/traffic?period=7d&q=ma")[1]), ["masha"])
            resp, body = self.c.get("/traffic?export=json&sort=key")
            self.assertEqual([r["key"] for r in json.loads(body)], ["anna", "masha", "owner"])
            for q in (f"q={EVIL}", f"sort={EVIL}", "n=zzz"):
                self.assertEqual(self.c.get("/traffic?" + q)[0].status, 200, q)
        # версии: те же заголовки-ссылки; на телефоне второстепенное раскрывается в строке
        self.env.add_manifest("vless-reality")
        body = self.c.get("/settings")[1]
        self.assertIn('class="dt"', body)
        self.assertRegex(body, r'<a href="/settings\?v_sort=-name" data-swap class="sortlink">компонент</a>')
        self.assertIn('class="det" hidden', body)
        for q in (f"v_sort={EVIL}", f"v_f_status={EVIL}", "v_sort=name%3Bdrop"):
            self.assertEqual(self.c.get("/settings?" + q)[0].status, 200, q)

    def test_traffic_cache_is_keyed_by_table_state(self):
        self.c.login()
        spec = __import__("zoolib.web.views", fromlist=["x"])._traffic_users_spec("24h", 0)
        a, b = tbl.parse(spec, {}), tbl.parse(spec, {"sort": "key"})
        self.assertNotEqual(a.key, b.key)
        self.assertEqual(tbl.parse(spec, {"sort": "key;drop"}).key, a.key, "мусор даёт тот же ключ кэша")


if __name__ == "__main__":
    unittest.main()
