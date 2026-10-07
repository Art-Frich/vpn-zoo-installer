import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ZooEnv
from zoolib import cli, config, journal, storage, traffic
from zoolib.config import Config
from zoolib.probe import history

NOW = 1_800_000_000


def cfg(**kw):
    return Config(values={k: str(v) for k, v in kw.items()})


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def fill_traffic(n5=4000, n1h=500, n1d=100):
    con = traffic.connect()
    rows = [(traffic.RES_5M, NOW - i * 300, "xray", f"u{i % 7}", i, i) for i in range(n5)]
    rows += [(traffic.RES_1H, NOW - i * 3600, "xray", f"u{i % 7}", i, i) for i in range(n1h)]
    rows += [(traffic.RES_1D, NOW - i * 86400, "xray", f"u{i % 7}", i, i) for i in range(n1d)]
    with con:
        con.executemany("INSERT INTO traffic (res, ts, proto, user, up, down) VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.close()


def fill_journal(n=200):
    con = journal.connect()
    with con:
        con.executemany("INSERT INTO hits (res, ts, source, kind, ip, port, n) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(journal.RES_1H, NOW - i * 3600, "ufw", "port-scan", f"203.0.113.{i % 250}", 22, 1)
                         for i in range(n)])
    con.close()


def fill_probe(n=40):
    con = history.connect()
    with con:
        con.executemany("INSERT INTO reports (uid, ts, mode, source, raw) VALUES (?, ?, 'remote', 'test', ?)",
                        [(f"u{i}", NOW - i * 3600, "x" * 3000) for i in range(n)])
    con.close()


def count(con_fn, sql):
    con = con_fn()
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


class SizeTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(storage.parse_size("1G"), 1 << 30)
        self.assertEqual(storage.parse_size(" 500 мб "), 500 << 20)
        self.assertEqual(storage.parse_size("1,5GB"), int(1.5 * (1 << 30)))
        self.assertEqual(storage.parse_size("2048"), 2048)
        for bad in ("", None, "0", "abc", "-1G", "1 ГБайт"):
            self.assertIsNone(storage.parse_size(bad), bad)

    def test_format(self):
        self.assertEqual(storage.format_size(1 << 30), "1G")
        self.assertEqual(storage.format_size(500 << 20), "500M")
        self.assertEqual(storage.format_size(1000), "1000")
        self.assertEqual(storage.parse_size(storage.format_size(750 << 20)), 750 << 20)


class BudgetTest(unittest.TestCase):
    def test_default_is_one_gb_but_not_more_than_5_percent_of_disk(self):
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 50 << 30, "used": 0}):
            self.assertEqual(storage.budget(cfg()), {"limit": 1 << 30, "configured": False, "capped": False,
                                                      "too_small": False})
        with mock.patch.object(storage, "disk", return_value={"total": 10 << 30, "free": 5 << 30, "used": 0}):
            b = storage.budget(cfg())
            self.assertEqual(b["limit"], (10 << 30) // 20)
            self.assertTrue(b["capped"])

    def test_configured_limit_wins(self):
        with mock.patch.object(storage, "disk", return_value={"total": 10 << 30, "free": 5 << 30, "used": 0}):
            b = storage.budget(cfg(ZOO_DATA_LIMIT="3G"))
        self.assertEqual((b["limit"], b["configured"]), (3 << 30, True))

    def test_shares_sum_to_100(self):
        self.assertEqual(sum(storage.SHARES.values()), 100)
        self.assertNotIn("live", storage.SHARES, "live-замеры лежат в базе проб и входят в её долю")

    def test_limit_below_minimum_falls_back_to_default(self):
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 50 << 30, "used": 0}):
            for raw in ("1M", "15M", "1024"):
                b = storage.budget(cfg(ZOO_DATA_LIMIT=raw))
                self.assertEqual((b["limit"], b["configured"], b["too_small"]), (1 << 30, False, True), raw)
            b = storage.budget(cfg(ZOO_DATA_LIMIT="16M"))
            self.assertEqual((b["limit"], b["configured"], b["too_small"]), (16 << 20, True, False))
            self.assertFalse(storage.budget(cfg())["too_small"])

    def test_limit_below_minimum_raises_alert(self):
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 50 << 30, "used": 0}):
            out = storage.alerts(cfg(ZOO_DATA_LIMIT="1M"), NOW)
            self.assertEqual([k for k, _ in out], ["warn"])
            self.assertIn("ZOO_DATA_LIMIT=1M", out[0][1])
            self.assertIn("не применён", out[0][1])
            self.assertEqual(storage.alerts(cfg(ZOO_DATA_LIMIT="64M"), NOW), [])

    def test_enforce_uses_default_when_limit_too_small(self):
        env = ZooEnv().__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        fill_traffic(300, 30, 5)
        res = storage.enforce(cfg(ZOO_DATA_LIMIT="1M"), NOW)
        self.assertEqual((res["limit"], res["trimmed"]), (storage.budget(cfg())["limit"], {}))


class StorageBase(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.logs = self.env.root / "logs"
        self.logs.mkdir()
        p = mock.patch.dict(os.environ, {"LOG_DIR": self.logs.as_posix()})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(storage, "MIN_LIMIT", 1)     # тестовые бюджеты — килобайты
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(storage, "JOURNALD_DIRS", (str(self.env.root / "no-journal"),))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def total(self):
        return sum(s.size() for s in storage.sections())


class ReportTest(StorageBase):
    def test_empty_install(self):
        d = storage.report(cfg())
        self.assertEqual([s["id"] for s in d["sections"]], ["traffic", "journal", "probe", "logs", "dist"])
        self.assertEqual(d["total"], 0)
        self.assertFalse(d["over"])
        self.assertTrue(all(s["oldest"] is None for s in d["sections"]))

    def test_sizes_oldest_and_outside(self):
        fill_traffic(100, 10, 5)
        fill_journal(20)
        fill_probe(3)
        (self.logs / "install-1.log").write_text("x" * 500, encoding="utf-8")
        prev = self.env.root / "state" / "geo" / "prev"
        prev.mkdir(parents=True)
        (prev / "geoip.dat").write_bytes(b"0" * 4096)
        d = storage.report(cfg(ZOO_DATA_LIMIT="10M"))
        by = {s["id"]: s for s in d["sections"]}
        self.assertGreater(by["traffic"]["size"], 0)
        self.assertEqual(by["traffic"]["oldest"], NOW - 4 * 86400)
        self.assertEqual(by["journal"]["oldest"], NOW - 19 * 3600)
        self.assertEqual(by["probe"]["oldest"], NOW - 2 * 3600)
        self.assertEqual(by["logs"]["size"], 500)
        self.assertEqual(by["traffic"]["share_bytes"], (10 << 20) * 20 // 100)
        self.assertEqual(by["dist"]["share_bytes"], (10 << 20) * 30 // 100)
        self.assertEqual(d["total"], sum(s["size"] for s in d["sections"]))
        self.assertEqual([o["id"] for o in d["outside"]], ["geo-prev"])
        self.assertEqual(d["outside"][0]["size"], 4096)


class EnforceTest(StorageBase):
    def test_under_budget_touches_nothing(self):
        fill_traffic(200, 20, 5)
        before = self.total()
        with mock.patch.object(storage.SqliteSection, "compact") as compact, \
                mock.patch.object(storage.SqliteSection, "delete_oldest") as delete:
            res = storage.enforce(cfg(ZOO_DATA_LIMIT="1G"), NOW)
        compact.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(res["trimmed"], {})
        self.assertEqual(self.total(), before)

    def test_cuts_the_section_most_over_its_share_and_vacuums(self):
        fill_traffic()
        fill_journal(60)
        fill_probe(2)
        sizes = {s.id: s.size() for s in storage.sections()}
        self.assertGreater(sizes["traffic"], 100_000)
        limit = (sizes["traffic"] + sizes["journal"] + sizes["probe"]) * 7 // 10
        journal_rows = count(journal.connect, "SELECT COUNT(*) FROM hits")
        res = storage.enforce(cfg(ZOO_DATA_LIMIT=limit), NOW)
        self.assertIn("traffic", res["trimmed"])
        self.assertNotIn("journal", res["trimmed"])
        self.assertLessEqual(self.total(), limit)
        self.assertEqual(count(journal.connect, "SELECT COUNT(*) FROM hits"), journal_rows)
        self.assertLess(traffic.db_path().stat().st_size, sizes["traffic"], "VACUUM вернул место")

    def test_finest_resolution_goes_first(self):
        fill_traffic()
        size = storage.section("traffic").size()
        storage.enforce(cfg(ZOO_DATA_LIMIT=size * 9 // 10), NOW)
        r5 = count(traffic.connect, f"SELECT COUNT(*) FROM traffic WHERE res = {traffic.RES_5M}")
        r1d = count(traffic.connect, f"SELECT COUNT(*) FROM traffic WHERE res = {traffic.RES_1D}")
        self.assertLess(r5, 4000)
        self.assertEqual(r1d, 100, "суточные трогаются последними")
        # режутся самые старые 5-минутки, свежие остаются
        newest = count(traffic.connect, f"SELECT MAX(ts) FROM traffic WHERE res = {traffic.RES_5M}")
        self.assertEqual(newest, NOW)

    def test_probe_trim_drops_live_rows_before_reports(self):
        fill_probe(30)
        con = history.connect()
        with con:
            con.executemany("INSERT INTO live (ts, proto, ok, rtt_ms, jitter_ms, mbps, verdict, bytes) "
                            "VALUES (?, 'tuic', 1, 20, 2, NULL, 'OK', 0)", [(NOW - i * 600,) for i in range(2000)])
        con.close()
        sec = storage.section("probe")
        self.assertEqual(sec.rows(), 2030)
        full = sec.size()
        self.assertGreater(storage.trim(sec, full * 8 // 10), 0)
        self.assertEqual(count(history.connect, "SELECT COUNT(*) FROM reports"), 30, "отчёты целы, пока есть live")
        left = count(history.connect, "SELECT COUNT(*) FROM live")
        self.assertLess(left, 2000)
        self.assertEqual(count(history.connect, "SELECT MAX(ts) FROM live"), NOW, "режутся самые старые")
        storage.trim(sec, 0)
        self.assertEqual((count(history.connect, "SELECT COUNT(*) FROM reports"),
                          count(history.connect, "SELECT COUNT(*) FROM live")), (0, 0))

    def test_probe_trim_works_without_live_table(self):
        fill_probe(30)
        con = history.connect()
        with con:
            con.execute("DROP TABLE live")
        con.close()
        sec = storage.section("probe")
        self.assertEqual(sec.rows(), 30)
        self.assertGreater(storage.trim(sec, sec.size() // 2), 0)
        self.assertLess(count(history.connect, "SELECT COUNT(*) FROM reports"), 30)

    def test_cut_is_oldest_first_for_probe_history(self):
        fill_probe(60)
        size = storage.section("probe").size()
        storage.enforce(cfg(ZOO_DATA_LIMIT=size * 6 // 10), NOW)   # probe — 30 % бюджета: далеко за долей
        con = history.connect()
        left = [r[0] for r in con.execute("SELECT ts FROM reports ORDER BY ts")]
        con.close()
        self.assertTrue(left)
        self.assertLess(len(left), 60)
        self.assertEqual(max(left), NOW, "свежее осталось")

    def test_hysteresis_stops_at_90_percent(self):
        fill_traffic()
        total = self.total()
        limit = total * 98 // 100
        res = storage.enforce(cfg(ZOO_DATA_LIMIT=limit), NOW)
        self.assertLessEqual(res["total"], int(limit * storage.HYSTERESIS))
        again = storage.enforce(cfg(ZOO_DATA_LIMIT=limit), NOW)
        self.assertEqual(again["trimmed"], {}, "второй запуск сразу после — в бюджете, ничего не режет")

    def test_trims_are_recorded_for_alert(self):
        fill_traffic()
        limit = self.total() // 2
        storage.enforce(cfg(ZOO_DATA_LIMIT=limit), NOW)
        self.assertEqual(len(storage.report(cfg())["trims"]["traffic"]), 1)

    def test_logs_keep_newest(self):
        for i in range(4):
            f = self.logs / f"install-{i}.log"
            f.write_text("x" * 1000, encoding="utf-8")
            os.utime(f, (NOW - (10 - i) * 3600, NOW - (10 - i) * 3600))
        res = storage.enforce(cfg(ZOO_DATA_LIMIT=1500), NOW)
        self.assertEqual(res["trimmed"], {"logs": 3})
        self.assertEqual([f.name for f in self.logs.iterdir()], ["install-3.log"])


class ClearTest(StorageBase):
    def test_clear_traffic_keeps_counters_table(self):
        fill_traffic(300, 30, 5)
        con = traffic.connect()
        with con:
            con.execute("INSERT INTO counters VALUES ('xray', 'u0', 1, 1, '', 1, NULL)")
        con.close()
        res = storage.clear("traffic")
        self.assertEqual(res["removed"], 335)
        self.assertEqual(count(traffic.connect, "SELECT COUNT(*) FROM traffic"), 0)
        self.assertEqual(count(traffic.connect, "SELECT COUNT(*) FROM counters"), 1)
        self.assertLess(res["size"], res["before"])

    def test_clear_probe_and_logs(self):
        fill_probe(5)
        (self.logs / "install-a.log").write_text("a", encoding="utf-8")
        os.utime(self.logs / "install-a.log", (1, 1))
        (self.logs / "install-b.log").write_text("b", encoding="utf-8")
        storage.clear("probe")
        storage.clear("logs")
        self.assertEqual(count(history.connect, "SELECT COUNT(*) FROM reports"), 0)
        self.assertEqual(sorted(f.name for f in self.logs.iterdir()), ["install-b.log"])

    def test_unknown_and_unavailable(self):
        with self.assertRaises(ValueError):
            storage.clear("nope")
        with self.assertRaises(ValueError):
            storage.clear("live")


class AlertsTest(StorageBase):
    def test_low_disk(self):
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 8 << 30, "used": 0}):
            out = storage.alerts(cfg(), NOW)
        self.assertEqual([k for k, _ in out], ["warn"])
        self.assertIn("Мало места", out[0][1])
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 3 << 30, "used": 0}):
            self.assertEqual(storage.alerts(cfg(), NOW)[0][0], "bad")
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 30 << 30, "used": 0}):
            self.assertEqual(storage.alerts(cfg(), NOW), [])

    def test_section_cut_too_often(self):
        with mock.patch.object(storage, "disk", return_value={"total": 100 << 30, "free": 50 << 30, "used": 0}):
            storage._note_trim("traffic", NOW - 3600)
            self.assertEqual(storage.alerts(cfg(), NOW), [])
            storage._note_trim("traffic", NOW - 60)
            out = storage.alerts(cfg(), NOW)
        self.assertEqual(len(out), 1)
        self.assertIn("Трафик", out[0][1])
        self.assertIn("чаще раза в сутки", out[0][1])


class CliTest(StorageBase):
    def test_report_json_and_text(self):
        fill_traffic(50, 5, 2)
        code, out, _ = run_cli("storage", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sections"][0]["id"], "traffic")
        code, out, _ = run_cli("storage")
        self.assertEqual(code, 0)
        self.assertIn("Журнал атак", out)
        self.assertIn("занято", out)

    def test_limit_written_to_config(self):
        code, _, _ = run_cli("storage", "--limit", "512M")
        self.assertEqual(code, 0)
        self.assertEqual(config.load().get("ZOO_DATA_LIMIT"), "512M")
        with mock.patch.object(storage, "MIN_LIMIT", 16 << 20):
            code, _, err = run_cli("storage", "--limit", "1K")
        self.assertEqual(code, 2)
        self.assertEqual(config.load().get("ZOO_DATA_LIMIT"), "512M")
        self.assertEqual(run_cli("storage", "--limit", "мусор")[0], 2)

    def test_enforce_and_clear(self):
        fill_traffic(300, 30, 5)
        self.env.write_config({"ZOO_DATA_LIMIT": "1G"})
        code, out, _ = run_cli("storage", "--enforce")
        self.assertEqual((code, out.strip()), (0, "в бюджете"))
        code, out, _ = run_cli("storage", "--clear", "traffic", "--json")
        self.assertEqual(json.loads(out)["removed"], 335)
        self.assertEqual(run_cli("storage", "--clear", "nope")[0], 2)

    def test_storage_in_collector_unit(self):
        unit = (Path(__file__).resolve().parent.parent / "systemd" / "zoo-collector.service").read_text(encoding="utf-8")
        self.assertIn("ExecStartPost=-/usr/local/bin/zoo storage --enforce", unit)
        self.assertIn("/var/log/vpn-zoo", unit)


if __name__ == "__main__":
    unittest.main()
