"""Живые метрики карточек (probe/live.py, D41) и вкл/выкл протокола с карточки (protoctl.py, D42):
расчёт, база, заявки, исполнитель с фейковым install.sh, страницы админки."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import BASH, REPO, ZooEnv, needs_bash
from tests.test_cli import run_cli
from tests.test_probe import ENTRIES, HY, VLESS
from tests.test_web import HEALTHY, TOKEN, AppTestBase, Client, patch_status
from zoolib import config, manifests, paths, protoctl, traffic, users
from zoolib import probe as probe_pkg
from zoolib.probe import clients, engine, export, history, live, metrics, verdicts
from zoolib.web.app import App


class SeqClient:
    """Клиент без процессов: TTFB малых запросов — по списку, большой запрос — по сценарию; журнал url."""

    def __init__(self, ttfb: list[float], large_ok: bool = True) -> None:
        self.ttfb = list(ttfb)
        self.large_ok = large_ok
        self.urls: list[str] = []

    def start(self, timeout):
        pass

    def handshake(self, timeout):
        return ("unknown", "", None)

    def run_jobs(self, jobs):
        out = []
        for j in jobs:
            url = j["url"]
            self.urls.append(url)
            if "generate_204" in url:
                ms = self.ttfb.pop(0) if self.ttfb else 20.0
                out.append({"ok": True, "url": url, "seconds": ms / 1000, "ttfb_ms": ms, "connect_ms": 5.0})
            elif "__down" in url or "10Mb" in url:
                out.append({"ok": self.large_ok, "url": url, "bytes": 1_000_000, "body_seconds": 0.25,
                            "seconds": 0.3})
            else:
                out.append({"ok": True, "url": url, "body": "ip=198.51.100.1\n"})
        return out

    def log_tail(self, n=6):
        return ""

    def stop(self):
        pass


def patched_engine(clients_by_id: dict[str, SeqClient]):
    """Подмена клиентов пробника и сетевых проверок: замер идёт без сети."""
    def make(probe, workdir, mode, stall):
        return clients_by_id[probe["_id"]]

    stack = mock.patch.multiple(engine, tcp_check=lambda h, p, t, tries=3: ("ok", 5.0),
                                udp_listening=lambda p: "ok")
    return mock.patch.object(clients, "make_client", make), stack


def entry(pid: str, probe: dict) -> dict:
    return {"id": pid, "name": pid, "layer": "tcp" if probe["kind"] == "xray" else "udp", "port": 443,
            "probe": dict(probe, _id=pid)}


class MathTest(unittest.TestCase):
    def test_jitter_is_mean_abs_difference(self):
        self.assertEqual(metrics.jitter([10, 20, 15, 25, 20]), 7.5)
        self.assertIsNone(metrics.jitter([10]))

    def test_row_of_takes_median_and_jitter(self):
        r = {"id": "tuic", "verdict": verdicts.OK, "speed_mbps": 38.2, "large": {"bytes": 1_000_000},
             "metrics": {"latency": {"median_ms": 23.0, "jitter_ms": 4.0}}}
        row = live.row_of(r, 1000)
        self.assertEqual((row["rtt_ms"], row["jitter_ms"], row["mbps"], row["bytes"], row["ok"]),
                         (23.0, 4.0, 38.2, 1_000_000, 1))
        bad = live.row_of({"id": "x", "verdict": verdicts.HANDSHAKE_FAIL}, 5)
        self.assertEqual((bad["ok"], bad["rtt_ms"], bad["mbps"], bad["bytes"]), (0, None, None, 0))

    def test_light_settings_do_not_download(self):
        light, heavy = live.settings(False), live.settings(True)
        self.assertEqual(light.large_bytes, 0)
        self.assertEqual(light.ip_urls, ())
        self.assertEqual(heavy.large_bytes, live.SPEED_BYTES)
        with mock.patch.dict(os.environ, {"ZOO_LIVE_SPEED_BYTES": "524288"}):
            self.assertEqual(live.settings(True).large_bytes, 524288)
        for bad in ("abc", "1", "999999999"):
            with mock.patch.dict(os.environ, {"ZOO_LIVE_SPEED_BYTES": bad}):
                self.assertIn(live.speed_bytes(), (live.SPEED_BYTES, 100_000, 5_000_000), bad)
        self.assertEqual(light.latency_samples, 5)
        self.assertEqual(len(light.latency_urls), 1)


class SummaryTest(unittest.TestCase):
    ROW = {"ts": 1000, "rtt_ms": 8.4, "jitter_ms": 1.2, "speed": 51.4, "speed_ts": 1000}

    def test_parts_are_shared_format(self):
        self.assertEqual(live.parts(self.ROW, 1000 + 180), ["8 мс", "±1", "51 Мбит/с", "3 мин назад"])
        self.assertEqual(live.parts(self.ROW, 1030, jitter=False), ["8 мс", "51 Мбит/с", "только что"])
        self.assertEqual(live.parts(dict(self.ROW, speed=4.56), 1030)[2], "4.6 Мбит/с")
        self.assertEqual(live.parts(dict(self.ROW, rtt_ms=None, jitter_ms=None, speed=None), 1030), [])

    def test_speed_note_only_when_speed_is_older(self):
        self.assertEqual(live.speed_note(self.ROW, 2000), "")
        self.assertEqual(live.speed_note(dict(self.ROW, ts=1000 + 2400), 1000 + 2400), "скорость 40 мин назад")
        self.assertEqual(live.speed_note(dict(self.ROW, speed=None), 2000), "")

    def test_summary_one_source_for_all_and_one_protocol(self):
        with ZooEnv():
            now = time.time()
            for pid, mbps in (("a", None), ("b", None)):
                con = history.connect()
                live.store(con, [{"ts": int(now - 3000), "proto": pid, "ok": 1, "rtt_ms": 9.0, "jitter_ms": 1.0,
                                  "mbps": 40.0, "verdict": "OK", "bytes": 0},
                                 {"ts": int(now - 60), "proto": pid, "ok": 1, "rtt_ms": 8.0, "jitter_ms": 1.0,
                                  "mbps": mbps, "verdict": "OK", "bytes": 0}])
                con.close()
            allp = live.summary(now=now)
            self.assertEqual(set(allp), {"a", "b"})
            self.assertEqual(live.summary("a", now), allp["a"])
            self.assertEqual((allp["a"]["rtt_ms"], allp["a"]["speed"]), (8.0, 40.0), "задержка — последняя, скорость — последняя загрузка")
            self.assertEqual(allp["a"]["speed_ts"], int(now - 3000))
            self.assertIsNone(live.summary("ghost", now))


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        p = mock.patch.object(probe_pkg, "server_addresses", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def run_measure(self, fake: dict[str, SeqClient], entries, now):
        mk, net = patched_engine(fake)
        with mk, net:
            return live.measure(entries, now=now)

    def test_light_check_has_no_download_and_computes_jitter(self):
        c = SeqClient([50, 50, 10, 20, 15, 25, 20])  # два запроса вердикта и пять на задержку
        now = 10_000_000
        with mock.patch.object(live, "speed_due", return_value=False):
            rows = self.run_measure({"vless-reality": c}, [entry("vless-reality", VLESS)], now)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["rtt_ms"], r["jitter_ms"], r["ok"], r["verdict"]), (20.0, 7.5, 1, verdicts.OK))
        self.assertIsNone(r["mbps"])
        self.assertEqual(r["bytes"], 0)
        self.assertFalse([u for u in c.urls if "__down" in u or "10Mb" in u], "лёгкий замер не качает")
        self.assertEqual(len(c.urls), 7, "2 малых запроса вердикта + 5 на задержку")

    def test_speed_once_per_55_minutes(self):
        t0 = 10_000_000
        c = SeqClient([])
        first = self.run_measure({"vless-reality": c}, [entry("vless-reality", VLESS)], t0)[0]
        self.assertEqual(first["mbps"], 32.0)
        self.assertEqual(first["bytes"], 1_000_000)
        self.assertTrue([u for u in c.urls if "__down" in u])
        c2 = SeqClient([])
        second = self.run_measure({"vless-reality": c2}, [entry("vless-reality", VLESS)], t0 + 600)[0]
        self.assertIsNone(second["mbps"])
        self.assertFalse([u for u in c2.urls if "__down" in u], "через 10 минут скорость не меряется")
        c3 = SeqClient([])
        third = self.run_measure({"vless-reality": c3}, [entry("vless-reality", VLESS)], t0 + 55 * 60)[0]
        self.assertEqual(third["mbps"], 32.0)

    def test_prune_keeps_seven_days_and_rows_are_not_exported(self):
        now = int(time.time())
        con = history.connect()
        live.store(con, [
            {"ts": now - 8 * 86400, "proto": "old", "ok": 1, "rtt_ms": 1, "jitter_ms": 1, "mbps": None,
             "verdict": "LIVEONLY", "bytes": 0},
            {"ts": now - 6 * 86400, "proto": "fresh", "ok": 1, "rtt_ms": 1, "jitter_ms": 1, "mbps": None,
             "verdict": "LIVEONLY", "bytes": 0}])
        self.assertEqual(live.prune(con, now), 1)
        self.assertEqual([r[0] for r in con.execute("SELECT proto FROM live")], ["fresh"])
        rep = {"schema": 1, "type": "zoo-probe-report", "mode": "local", "generated": "2026-10-06T10:00:00+00:00",
               "label": "t", "results": [{"id": "tuic", "verdict": "OK"}]}
        history.record(rep, "local", con=con)
        out = self.env.root / "hist"
        export.write_dir(out, history.raw_reports(con))
        text = "".join(f.read_text(encoding="utf-8") for f in out.rglob("*") if f.is_file())
        self.assertIn("tuic", text)
        self.assertNotIn("LIVEONLY", text)
        self.assertNotIn("fresh", text)
        con.close()

    def test_failed_protocol_is_recorded_not_ok(self):
        c = SeqClient([])
        c.run_jobs = lambda jobs: [{"ok": False, "url": j["url"], "error_kind": "reset", "error": "RST"} for j in jobs]
        rows = self.run_measure({"vless-reality": c}, [entry("vless-reality", VLESS)], 5_000_000)
        self.assertEqual((rows[0]["ok"], rows[0]["verdict"], rows[0]["rtt_ms"]), (0, verdicts.HANDSHAKE_FAIL, None))
        self.assertEqual(live.latest()["vless-reality"]["verdict"], verdicts.HANDSHAKE_FAIL)


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

    def test_root_runner_refuses_symlinked_db_lock_and_dirs(self):
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        victim = self.env.root / "victim.db"
        victim.write_bytes(b"")
        for link in (history.db_path(), Path(f"{history.db_path()}-wal")):
            try:
                link.symlink_to(victim)
            except (OSError, NotImplementedError):
                self.skipTest("нет символьных ссылок")
            with mock.patch.object(live, "measure", side_effect=AssertionError("замер по ссылке")):
                code, _, err = run_cli("live", "run")
            self.assertEqual(code, 1, err)
            self.assertIn("не обычный файл", err)
            self.assertEqual(victim.read_bytes(), b"", "чужую базу не трогали")
            link.unlink()
        lock_victim = self.env.root / "lock-victim"
        live.lock_file().symlink_to(lock_victim)
        with self.assertRaises(OSError):
            with live.exclusive(timeout=0):
                pass
        self.assertFalse(lock_victim.exists(), "блокировка по ссылке не создаёт файл root")

    def test_request_rate_limit_and_stale(self):
        t0 = 1_000_000.0
        self.assertEqual(live.request("tuic", t0), (True, "замер заказан"))
        f = live.req_dir() / "tuic"
        self.assertTrue(f.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(f.stat().st_mode) & 0o077, 0)
        ok, why = live.request("tuic", t0 + 1)
        self.assertFalse(ok)
        self.assertIn("уже заказан", why)
        # замер состоялся, заявку забрал исполнитель: в первую минуту — отказ
        live.clear_request("tuic")
        con = history.connect()
        live.store(con, [{"ts": int(t0), "proto": "tuic", "ok": 1, "rtt_ms": 9.0, "jitter_ms": 1.0, "mbps": None,
                          "verdict": "OK", "bytes": 0}])
        con.close()
        ok, why = live.request("tuic", t0 + 30)
        self.assertFalse(ok)
        self.assertIn("меньше минуты", why)
        self.assertTrue(live.request("tuic", t0 + 61)[0])

    def test_bad_name_and_stale_files(self):
        self.assertFalse(live.request("../etc/passwd")[0])
        live.req_dir().mkdir(parents=True)
        old = live.req_dir() / "tuic"
        old.write_text("1")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        (live.req_dir() / "bad name").write_text("1")
        (live.req_dir() / "hysteria2").write_text("1")
        self.assertEqual(live.pending_requests(), ["hysteria2"])
        self.assertFalse(old.exists(), "просроченная заявка удалена")
        self.assertFalse((live.req_dir() / "bad name").exists())

    def test_run_from_requests_measures_only_requested_and_clears_files(self):
        live.request("hysteria2", time.time())
        live.request("ghost", time.time())
        calls = []

        def fake_collect(user, only=None):
            calls.append(only)
            return [e for e in ENTRIES if not only or e["id"] in only]

        measured = []
        with mock.patch.object(probe_pkg, "collect_entries", fake_collect), \
                mock.patch.object(live, "measure", lambda entries, now=None: measured.append(
                    [e["id"] for e in entries]) or [{"proto": e["id"]} for e in entries]):
            rows = live.run(from_requests=True)
        self.assertEqual(calls[0], ["ghost", "hysteria2"])
        self.assertEqual(measured, [["hysteria2"]])
        self.assertEqual(len(rows), 1)
        self.assertEqual(os.listdir(live.req_dir()), [], "заявки убраны, в том числе на неизвестный протокол")


class LockTest(unittest.TestCase):
    def test_probe_proceeds_without_lock_after_timeout_and_live_skips(self):
        with ZooEnv():
            busy = mock.patch.object(live, "file_lock", side_effect=live.LockTimeout("занято"))
            with busy:
                ran = []
                with live.exclusive(0):
                    ran.append(1)
                self.assertEqual(ran, [1], "проверка не зависает вечно за замером")
                live.request("tuic", time.time())
                code, out, err = run_cli("live", "run", "--requests")
                self.assertEqual(code, 0, "таймер не должен краснеть из-за идущей проверки")
                self.assertIn("замер пропущен", out + err)
                self.assertEqual(os.listdir(live.req_dir()), [], "заявка сброшена: иначе zoo-live.path гонял бы юнит по кругу")

    def test_run_local_takes_the_lock(self):
        with ZooEnv() as env:
            seen = []

            class Spy:
                def __enter__(self_):
                    seen.append("enter")

                def __exit__(self_, *a):
                    seen.append("exit")

            with mock.patch.object(live, "exclusive", lambda *a, **k: Spy()),                     mock.patch.object(probe_pkg, "collect_entries", return_value=[]),                     mock.patch.object(engine, "run", return_value=[]),                     mock.patch.object(probe_pkg, "server_addresses", return_value=[]):
                probe_pkg.run_local(config.load(), record_history=False)
            self.assertEqual(seen, ["enter", "exit"])


class TodaySplitTest(unittest.TestCase):
    def test_service_user_is_excluded(self):
        with ZooEnv():
            users.bootstrap()
            now = int(time.time())
            con = traffic.connect()
            with con:
                samples = [traffic.Sample("hysteria2", "", 100, 200), traffic.Sample("hysteria2", "zoo-probe", 10, 50),
                           traffic.Sample("tuic", "", 5, 1000), traffic.Sample("xray", "zoo-probe", 3, 400)]
                deltas, new = traffic.compute_deltas({}, samples, now, known={"hysteria2", "tuic", "xray"})
                traffic.store(con, deltas, new, now)
            con.close()
            got = traffic.today_split({"tuic": 400, "hysteria2": 9999}, now)
            self.assertEqual(got["hysteria2"], (90, 150), "серия zoo-probe вычтена точно")
            self.assertEqual(got["tuic"], (5, 600), "без манифеста Xray — вычтены байты замеров")
            self.assertNotIn("xray", got.get("tuic", ()))
            self.assertEqual(traffic.today_split({"tuic": 5000}, now)["tuic"], (5, 0), "меньше нуля не бывает")

    def test_xray_probe_total_is_exact_and_split_by_weight(self):
        with ZooEnv() as env:
            users.bootstrap()
            for pid in ("vless-reality", "tuic"):
                env.add_manifest(pid)
            now = int(time.time())
            con = traffic.connect()
            with con:
                samples = [traffic.Sample("vless-reality", "", 1000, 10_000), traffic.Sample("tuic", "", 500, 8000),
                           traffic.Sample("xray", "zoo-probe", 100, 4000), traffic.Sample("xray", "masha", 7, 7)]
                deltas, new = traffic.compute_deltas({}, samples, now, known={"vless-reality", "tuic", "xray"})
                traffic.store(con, deltas, new, now)
            con.close()
            got = traffic.today_split({}, now, {"vless-reality": 3, "tuic": 1})
            self.assertEqual(got["vless-reality"], (925, 7000), "3/4 общего счётчика zoo-probe")
            self.assertEqual(got["tuic"], (475, 7000), "1/4")
            self.assertEqual(sum(v[1] for v in got.values()), 18_000 - 4000, "сумма вычета точна")
            even = traffic.today_split({}, now, {})
            self.assertEqual((even["vless-reality"][1], even["tuic"][1]), (8000, 6000), "весов нет — поровну")


    def test_own_bytes_count_only_since_first_collector_run(self):
        with ZooEnv():
            now = int(time.time())
            day = traffic.align(now, traffic.RES_1D)
            self.assertEqual(live.own_bytes_today(now), {}, "счётчики ещё не снимались")
            con = traffic.connect()
            with con:
                con.execute("INSERT INTO runs VALUES (?, 1, 1, 0, 0, 0.1, '{}')", (day + 600,))
            con.close()
            rows = [{"ts": day + 300, "proto": "tuic", "ok": 1, "rtt_ms": 1, "jitter_ms": 1, "mbps": 1.0,
                     "verdict": "OK", "bytes": 1_000_000},
                    {"ts": day + 900, "proto": "tuic", "ok": 1, "rtt_ms": 1, "jitter_ms": 1, "mbps": 1.0,
                     "verdict": "OK", "bytes": 1_000_000},
                    {"ts": day - 3600, "proto": "tuic", "ok": 1, "rtt_ms": 1, "jitter_ms": 1, "mbps": 1.0,
                     "verdict": "OK", "bytes": 5_000_000}]
            hc = history.connect()
            live.store(hc, rows)
            hc.close()
            self.assertEqual(live.own_bytes_today(day + 1200), {"tuic": 1_000_000},
                             "до первого снятия и со вчера — не считаются")


# ---------- protoctl ----------

PHASES = ("04-vless-reality", "04d-tuic", "05-hysteria2", "04c-ss2022", "06-amneziawg")


class ProtoEnvTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.scripts = protoctl.installed_scripts()
        self.scripts.mkdir(parents=True, exist_ok=True)
        for ph in PHASES:
            (self.scripts / f"{ph}.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")

    def manifest(self, pid, enabled=True, **kw):
        self.env.add_manifest(pid, enabled=enabled, name=kw.pop("name", pid), **kw)


class ControlsTest(ProtoEnvTest):
    def test_manifest_values_then_static_fallback(self):
        self.manifest("tuic", phase="04d-tuic", enable_var="ENABLE_TUIC")
        self.manifest("vless-reality")          # манифест без phase/enable_var: берём таблицу
        self.manifest("hysteria2", enabled=False)
        c = protoctl.controls()
        self.assertEqual((c["tuic"].phase, c["tuic"].var, c["tuic"].installed), ("04d-tuic", "ENABLE_TUIC", True))
        self.assertEqual((c["vless-reality"].phase, c["vless-reality"].var), ("04-vless-reality", "ENABLE_VLESS"))
        self.assertFalse(c["hysteria2"].enabled)
        self.assertIn("ss2022", c, "не установленный протокол из таблицы можно включить")
        self.assertFalse(c["ss2022"].installed)
        self.assertNotIn("hysteria2-obfs", c, "Salamander без включённой Hysteria2 не предлагается")

    def test_obfs_needs_enabled_hysteria(self):
        self.manifest("hysteria2")
        self.assertIn("hysteria2-obfs", protoctl.controls())

    def test_bad_manifest_values_are_ignored(self):
        self.manifest("tuic", phase="../../etc/evil", enable_var="PATH")
        self.manifest("vless-reality", phase="77-nothing", enable_var="ENABLE_VLESS")
        c = protoctl.controls()
        self.assertEqual((c["tuic"].phase, c["tuic"].var), ("04d-tuic", "ENABLE_TUIC"), "вместо чужой фазы — из таблицы")
        self.assertEqual(c["vless-reality"].phase, "04-vless-reality", "фазы без файла нет")

    def test_manifest_phase_never_widens_the_table(self):
        # годная по виду пара у id, которого нет в STATIC, и чужая годная пара у известного протокола
        self.manifest("unknown-proto", phase="04d-tuic", enable_var="ENABLE_X")
        self.manifest("ss2022", phase="06-amneziawg", enable_var="ENABLE_AWG")
        (self.scripts / "99-print-creds.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
        self.manifest("tuic", phase="99-print-creds", enable_var="ENABLE_TUIC")
        c = protoctl.controls()
        self.assertNotIn("unknown-proto", c, "id вне таблицы управлять нельзя, что бы ни лежало в манифесте")
        self.assertEqual((c["ss2022"].phase, c["ss2022"].var), ("04c-ss2022", "ENABLE_SS"))
        self.assertEqual(c["tuic"].phase, "04d-tuic")
        with self.assertRaises(protoctl.JobError):
            protoctl.check("unknown-proto", "disable", c)

    def test_phase_is_looked_up_in_installed_copy(self):
        (self.scripts / "04d-tuic.sh").unlink()
        (self.env.scripts / "04d-tuic.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
        self.manifest("tuic")
        self.assertNotIn("tuic", protoctl.controls(), "файл фазы вне zoo_home()/scripts не считается")

    def test_xhttp_fallback_requires_vless_reality(self):
        self.env.write_config({"XHTTP_PLACEMENT": "fallback"})
        (self.scripts / "04b-vless-xhttp.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
        self.manifest("vless-reality")
        self.manifest("vless-xhttp")
        self.assertEqual(protoctl.controls()["vless-xhttp"].requires, "vless-reality")
        with self.assertRaises(protoctl.JobError) as e:
            protoctl.check("vless-reality", "disable")
        self.assertIn("сначала выключите VLESS XHTTP", str(e.exception), "XHTTP-fallback без VLESS — не отдельный протокол")
        self.manifest("vless-xhttp", enabled=False)
        with self.assertRaises(protoctl.JobError) as e:
            protoctl.check("vless-reality", "disable")
        self.assertIn("последний", str(e.exception))
        self.manifest("vless-xhttp")
        self.manifest("tuic")
        with self.assertRaises(protoctl.JobError) as e:
            protoctl.check("vless-reality", "disable")
        self.assertIn("сначала выключите VLESS XHTTP (он работает через VLESS)", str(e.exception))
        with self.assertRaises(protoctl.JobError):
            protoctl.submit("vless-reality", "disable")
        self.assertEqual(protoctl.check("vless-xhttp", "disable").id, "vless-xhttp")
        self.manifest("vless-xhttp", enabled=False)
        self.assertEqual(protoctl.check("vless-reality", "disable").id, "vless-reality", "XHTTP выключен — можно")
        self.manifest("vless-xhttp")
        self.env.write_config({"XHTTP_PLACEMENT": "port"})
        self.assertIsNone(protoctl.controls()["vless-xhttp"].requires)
        self.manifest("tuic", enabled=False)
        self.assertEqual(protoctl.check("vless-reality", "disable").id, "vless-reality")

    def test_enable_needs_required_protocol_enabled(self):
        self.manifest("hysteria2", enabled=False)
        self.manifest("hysteria2-obfs", enabled=False)
        self.manifest("tuic")
        with self.assertRaises(protoctl.JobError) as e:
            protoctl.check("hysteria2-obfs", "enable")
        self.assertIn("без hysteria2", str(e.exception))
        self.assertEqual(protoctl.check("hysteria2", "enable").id, "hysteria2")
        self.manifest("hysteria2")
        self.assertEqual(protoctl.check("hysteria2-obfs", "enable").id, "hysteria2-obfs")

    def test_static_table_matches_manifest_writers(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "scripts" / "lib").glob("proto-*.sh"))
        for ctl in protoctl.STATIC.values():
            self.assertIn(f'"{ctl.phase}"', text, ctl.id)
            self.assertTrue((REPO / "scripts" / f"{ctl.phase}.sh").is_file(), ctl.phase)
            self.assertTrue(f'"{ctl.var}"' in text or re.search(rf"{ctl.var}\b", text), ctl.var)

    def test_check_guards(self):
        self.manifest("vless-reality")
        self.manifest("tuic", enabled=False)
        with self.assertRaises(protoctl.JobError) as e:
            protoctl.check("vless-reality", "disable")
        self.assertIn("последний", str(e.exception))
        self.assertEqual(protoctl.check("tuic", "enable").id, "tuic")
        with self.assertRaises(protoctl.JobError):
            protoctl.check("tuic", "disable")
        with self.assertRaises(protoctl.JobError):
            protoctl.check("vless-reality", "enable")
        for bad in ("nope", "../x", ""):
            with self.assertRaises(protoctl.JobError):
                protoctl.check(bad, "disable")
        with self.assertRaises(protoctl.JobError):
            protoctl.check("tuic", "restart")

    def test_disabling_hysteria_takes_salamander_with_it(self):
        self.manifest("hysteria2")
        self.manifest("hysteria2-obfs", phase="05-hysteria2", enable_var="ENABLE_HY2_OBFS")
        with self.assertRaises(protoctl.JobError):
            protoctl.check("hysteria2", "disable")
        self.manifest("tuic")
        self.assertEqual(protoctl.check("hysteria2", "disable").var, "ENABLE_HY2")

    def test_submit_checks_and_writes_under_submit_lock(self):
        self.manifest("vless-reality")
        self.manifest("tuic")
        events: list[str] = []

        @contextlib.contextmanager
        def fake_lock(path, timeout=30.0):
            events.append(f"lock {Path(path).name}")
            yield
            events.append("unlock")

        real_check, real_active, real_write = protoctl.check, protoctl.active, protoctl.atomic_write_json
        with mock.patch.object(protoctl, "file_lock", fake_lock), \
                mock.patch.object(protoctl, "check", lambda *a, **k: (events.append("check"), real_check(*a, **k))[1]), \
                mock.patch.object(protoctl, "active", lambda: (events.append("active"), real_active())[1]), \
                mock.patch.object(protoctl, "atomic_write_json",
                                  lambda *a, **k: (events.append("write"), real_write(*a, **k))[1]):
            protoctl.submit("tuic", "disable")
        self.assertEqual(events, ["lock submit.lock", "check", "active", "write", "unlock"])

    def test_submit_lock_timeout_is_a_job_error(self):
        self.manifest("vless-reality")
        self.manifest("tuic")

        @contextlib.contextmanager
        def busy(path, timeout=30.0):
            raise protoctl.LockTimeout("занято")
            yield

        with mock.patch.object(protoctl, "file_lock", busy):
            with self.assertRaises(protoctl.JobError):
                protoctl.submit("tuic", "disable")
        self.assertEqual(list(protoctl.jobs_dir().glob("*.json")), [])

    def test_submit_writes_job_and_refuses_second(self):
        self.manifest("vless-reality")
        self.manifest("tuic")
        jid = protoctl.submit("tuic", "disable")
        f = protoctl.jobs_dir() / f"{jid}.json"
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["proto"], "tuic")
        self.assertEqual(protoctl.active()["id"], jid)
        with self.assertRaises(protoctl.JobError) as e:
            protoctl.submit("vless-reality", "disable")
        self.assertIn("Сейчас идёт", str(e.exception))
        self.assertEqual(protoctl.active_by_proto()["tuic"]["action"], "disable")


FAKE_INSTALL = """#!/usr/bin/env bash
echo "install.sh $*"
for v in $(compgen -e | grep '^ENABLE_'); do
    { grep -v "^$v=" "$CONFIG_FILE" 2>/dev/null || true; echo "$v='${!v}'"; } > "$CONFIG_FILE.new"
    mv "$CONFIG_FILE.new" "$CONFIG_FILE"
done
echo "cfg: $(grep -h '^ENABLE_' "$CONFIG_FILE" | tr '\\n' ' ')"
echo "password=hunter2secret"
echo "$*" >> "$FAKE_STATE/install.calls"
exit "${FAKE_INSTALL_RC:-0}"
"""


@needs_bash
class RunnerTest(ProtoEnvTest):
    def setUp(self):
        super().setUp()
        (self.scripts / "install.sh").write_text(FAKE_INSTALL, encoding="utf-8", newline="\n")
        # чужой install.sh (рабочая копия репо, scripts_dir()) исполнитель запускать не должен
        (self.env.scripts / "install.sh").write_text('#!/usr/bin/env bash\necho WRONG-DIR >> "$FAKE_STATE/install.calls"\n',
                                                     encoding="utf-8", newline="\n")
        self.manifest("vless-reality")
        self.manifest("tuic")

    def calls(self) -> list[str]:
        f = self.env.fake / "install.calls"
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []

    def test_disable_sets_config_and_runs_only_its_phase(self):
        jid = protoctl.submit("tuic", "disable")
        self.assertEqual(protoctl.run_queue(), 0)
        self.assertEqual(self.calls(), ["--phase 04d-tuic"])
        self.assertEqual(config.load().get("ENABLE_TUIC"), "0")
        st = protoctl.get_state(jid)
        self.assertEqual((st["status"], st["rc"]), ("ok", 0))
        self.assertFalse((protoctl.jobs_dir() / f"{jid}.json").exists(), "заявка забрана")
        self.assertIn("ENABLE_TUIC='0'", protoctl.log_tail(jid))
        self.assertIsNone(protoctl.active())

    def test_enable_syncs_users_but_disable_does_not(self):
        self.manifest("tuic", enabled=False)
        with mock.patch.object(users, "sync_users", return_value=[]) as sync:
            jid = protoctl.submit("tuic", "enable")
            protoctl.run_queue()
            self.assertEqual(sync.call_count, 1)
            self.assertIn("zoo user sync", protoctl.log_tail(jid))
        self.manifest("tuic", enabled=True)
        with mock.patch.object(users, "sync_users", side_effect=RuntimeError("упал")) as sync:
            jid = protoctl.submit("tuic", "disable")
            protoctl.run_queue()
            sync.assert_not_called()
        self.manifest("tuic", enabled=False)
        with mock.patch.object(users, "sync_users", side_effect=RuntimeError("упал")):
            jid = protoctl.submit("tuic", "enable")
            protoctl.run_queue()
        self.assertEqual(protoctl.get_state(jid)["status"], "ok", "сбой sync задачу не проваливает")
        self.assertIn("не выполнен: упал", protoctl.log_tail(jid))

    def test_failure_is_reported_with_log(self):
        jid = protoctl.submit("tuic", "disable")
        with mock.patch.dict(os.environ, {"FAKE_INSTALL_RC": "3"}):
            protoctl.run_queue()
        st = protoctl.get_state(jid)
        self.assertEqual((st["status"], st["rc"]), ("fail", 3))
        self.assertIn("кодом 3", st["error"])
        self.assertEqual(protoctl.states()[0]["id"], jid)

    def test_forged_request_runs_nothing(self):
        d = protoctl.jobs_dir()
        d.mkdir(parents=True)
        jid = "a" * 32
        (d / f"{jid}.json").write_text(json.dumps({"id": jid, "proto": "evil; rm -rf /", "action": "disable"}),
                                       encoding="utf-8")
        wrong = "b" * 32
        (d / f"{wrong}.json").write_text(json.dumps({"id": jid, "proto": "tuic", "action": "disable"}),
                                         encoding="utf-8")
        protoctl.run_queue()
        self.assertEqual(self.calls(), [])
        self.assertEqual(protoctl.get_state(jid)["status"], "fail")
        self.assertEqual(protoctl.get_state(wrong)["status"], "fail")
        self.assertEqual(list(d.glob("*.json")), [], "заявки убраны")
        self.assertEqual(config.load().get("ENABLE_TUIC"), "")

    def symlink(self, link, target):
        try:
            Path(link).symlink_to(target, target_is_directory=Path(target).is_dir())
        except (OSError, NotImplementedError):
            self.skipTest("нет символьных ссылок")

    def test_symlinked_job_dirs_are_refused_by_the_root_runner(self):
        victim = self.env.root / "victim"
        victim.mkdir()
        jid = "a" * 32
        req = json.dumps({"id": jid, "proto": "tuic", "action": "disable"})
        (victim / f"{jid}.json").write_text(req, encoding="utf-8")
        self.symlink(protoctl.jobs_dir(), victim)          # jobs -> чужой каталог
        self.assertEqual(protoctl.run_queue(), 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(sorted(p.name for p in victim.iterdir()), [f"{jid}.json"], "в чужой каталог ничего не писали")
        protoctl.jobs_dir().unlink()
        protoctl.jobs_dir().mkdir()
        (protoctl.jobs_dir() / f"{jid}.json").write_text(req, encoding="utf-8")
        self.symlink(protoctl.state_dir(), victim)         # jobs/state -> чужой каталог
        self.assertEqual(protoctl.run_queue(), 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(sorted(p.name for p in victim.iterdir()), [f"{jid}.json"])
        protoctl.state_dir().unlink()
        # заявка-ссылка: читается как повреждённая, цель не читается и не трогается
        target = self.env.root / "secret.json"
        target.write_text(req, encoding="utf-8")
        (protoctl.jobs_dir() / f"{jid}.json").unlink()
        self.symlink(protoctl.jobs_dir() / f"{jid}.json", target)
        self.assertEqual(protoctl.run_queue(), 0)
        self.assertEqual(self.calls(), [], "по заявке-ссылке ничего не запускается")
        self.assertEqual(protoctl.get_state(jid)["status"], "fail")
        self.assertTrue(target.exists())
        # журнал задачи — ссылка: исполнитель не пишет через неё
        (protoctl.jobs_dir() / f"{jid}.json").unlink(missing_ok=True)
        jid2 = protoctl.submit("tuic", "disable")
        protoctl.state_dir().mkdir(exist_ok=True)
        before = target.read_bytes()
        self.symlink(protoctl.state_dir() / f"{jid2}.log", target)
        protoctl.run_queue()
        self.assertEqual(target.read_bytes(), before, "через ссылку журнал не писался")

    def test_phase_comes_from_manifest_not_from_request(self):
        d = protoctl.jobs_dir()
        d.mkdir(parents=True)
        jid = "c" * 32
        (d / f"{jid}.json").write_text(json.dumps({"id": jid, "proto": "tuic", "action": "disable",
                                                   "phase": "99-print-creds", "enable_var": "PATH"}), encoding="utf-8")
        protoctl.run_queue()
        self.assertEqual(self.calls(), ["--phase 04d-tuic"])
        self.assertEqual(config.load().get("PATH"), "")

    def test_runs_installed_copy_not_repo(self):
        protoctl.submit("tuic", "disable")
        protoctl.run_queue()
        self.assertEqual(self.calls(), ["--phase 04d-tuic"], "чужой install.sh (WRONG-DIR) не запускался")
        self.assertEqual(protoctl.install_script(), protoctl.installed_scripts() / "install.sh")

    def test_enable_var_goes_through_environment_not_config_before_phase(self):
        self.env.write_config({"ENABLE_TUIC": "1"})
        jid = protoctl.submit("tuic", "disable")
        seen: dict[str, str] = {}
        real = protoctl._run_phase

        def spy(phase, log, extra_env=None):
            seen["before"] = config.load().get("ENABLE_TUIC")
            seen["env"] = (extra_env or {}).get("ENABLE_TUIC", "")
            return real(phase, log, extra_env)

        with mock.patch.object(protoctl, "_run_phase", spy):
            protoctl.run_queue()
        self.assertEqual(seen, {"before": "1", "env": "0"}, "до фазы config.env не тронут, значение — в окружении")
        self.assertIn("ENABLE_TUIC='0'", protoctl.log_tail(jid))
        self.assertEqual(config.load().get("ENABLE_TUIC"), "0", "install.sh сам сохранил ключ")

    def test_failed_phase_keeps_old_value_in_config(self):
        self.env.write_config({"ENABLE_TUIC": "1"})
        jid = protoctl.submit("tuic", "disable")
        with mock.patch.dict(os.environ, {"FAKE_INSTALL_RC": "3"}):
            protoctl.run_queue()
        self.assertEqual(protoctl.get_state(jid)["status"], "fail")
        self.assertEqual(config.load().get("ENABLE_TUIC"), "1", "фаза упала — прежнее значение вернулось")

    def test_config_env_with_substitution_or_command_runs_nothing(self):
        for bad in ("ENABLE_TUIC='1' ; id", "echo hi", "A='x'$(id)", "KEY='multi", 'FOO="$(id)"', "FOO=`id`",
                    "FOO=$HOME", 'FOO="a${B}"', "FOO=a;id", "FOO=a|id", "FOO=a b", "FOO= id", "FOO=(a b)",
                    "FOO=~/x", "FOO=a\\", "FOO+=x", "foo=1", 'FOO="a\\"', "\x0cFOO=1", "FOO=1\0", "\r"):
            with self.subTest(bad=bad):
                cf = self.env.etc / "config.env"
                src = '# c\nSERVER_IP="1.2.3.4"\n' + bad + "\n"
                cf.write_text(src, encoding="utf-8", newline="")
                jid = protoctl.submit("tuic", "disable")
                protoctl.run_queue()
                st = protoctl.get_state(jid)
                self.assertEqual(st["status"], "fail")
                self.assertIn("config.env", st["error"])
                self.assertIn("строка 3", st["error"])
                self.assertEqual(self.calls(), [], "ни фаза, ни ключ не тронуты")
                self.assertEqual(open(cf, encoding="utf-8", newline="").read(), src, "файл не менялся")

    def test_config_env_v1_style_is_normalised_and_accepted(self):
        cf = self.env.etc / "config.env"
        cf.write_text('# v1\nSERVER_IP="1.2.3.4"\nPANEL_PORT=2053\nexport ENABLE_SS=1\n\n  A=b   # note\n'
                      "NOTE='it'\\''s \"q\" $x'\nQ=\"a\\$b \\\"c\\\" \\`d\\`\"\nH=a#b\nE=\n",
                      encoding="utf-8", newline="")
        protoctl.submit("tuic", "disable")
        protoctl.run_queue()
        self.assertEqual(self.calls(), ["--phase 04d-tuic"])
        text = open(cf, encoding="utf-8", newline="").read()
        self.assertEqual(text.split("\n"),
                         ["# v1", "SERVER_IP='1.2.3.4'", "PANEL_PORT='2053'", "ENABLE_SS='1'", "", "A='b'",
                          "NOTE='it'\\''s \"q\" $x'", "Q='a$b \"c\" `d`'", "H='a#b'", "E=''", "ENABLE_TUIC='0'", ""])
        v = config.load().values
        self.assertEqual((v["SERVER_IP"], v["PANEL_PORT"], v["ENABLE_SS"], v["A"]), ("1.2.3.4", "2053", "1", "b"))
        self.assertEqual((v["NOTE"], v["Q"], v["H"], v["E"]), ("it's \"q\" $x", 'a$b "c" `d`', "a#b", ""))

    @needs_bash
    def test_normalised_config_env_sources_to_the_same_values(self):
        cf = self.env.etc / "config.env"
        src = 'A="x y"\nB=2053\nC=\'q\'"\'"\'r\'\nD="a\\$b"\nE=a\\ b\n'
        cf.write_text(src, encoding="utf-8", newline="")
        out = []
        for _ in range(2):
            r = subprocess.run([BASH, "-c", 'set -e; . "$1"; printf "%s|" "$A" "$B" "$C" "$D" "$E"', "x", str(cf)],
                               capture_output=True, text=True, check=True)
            out.append(r.stdout)
            protoctl.normalise_config_file()
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0], "x y|2053|q'r|a$b|a b|")

    def test_config_env_values_with_odd_line_separators_are_preserved(self):
        cf = self.env.etc / "config.env"
        odd = "a\rb\x0cc\x85d e f"
        src = f"A='{odd}'\nB=\"{odd}\"\nC=x y\nD=z\r\n"
        cf.write_text(src, encoding="utf-8", newline="")
        protoctl.normalise_config_file()
        text = open(cf, encoding="utf-8", newline="").read()
        self.assertEqual(text, f"A='{odd}'\nB='{odd}'\nC='x y'\nD='z\r'\n")

    def test_normalise_is_idempotent_and_keeps_file_when_clean(self):
        config.config_set("SERVER_IP", "1.2.3.4")
        cf = self.env.etc / "config.env"
        before = cf.read_bytes()
        mtime = cf.stat().st_mtime_ns
        protoctl.normalise_config_file()
        self.assertEqual(cf.read_bytes(), before)
        self.assertEqual(cf.stat().st_mtime_ns, mtime, "чистый файл не перезаписывается")
        protoctl.normalise_config_file()
        self.assertEqual(cf.read_bytes(), before)

    def test_config_env_in_config_set_format_is_accepted(self):
        config.config_set("SERVER_IP", "1.2.3.4")
        config.config_set("NOTE", "it's $(not run) `x` \"q\"")
        (self.env.etc / "config.env").write_text(
            (self.env.etc / "config.env").read_text(encoding="utf-8") + "\n   \n# коммент\n", encoding="utf-8", newline="\n")
        protoctl.submit("tuic", "disable")
        protoctl.run_queue()
        self.assertEqual(self.calls(), ["--phase 04d-tuic"])

    def test_oneshot_queue_in_order_and_old_running_state_is_failed(self):
        protoctl.state_dir().mkdir(parents=True)
        stale = "d" * 32
        (protoctl.state_dir() / f"{stale}.json").write_text(
            json.dumps({"id": stale, "proto": "tuic", "action": "enable", "status": "running", "started": 1}),
            encoding="utf-8")
        self.assertIsNone(protoctl.active(), "зависшая «выполняется» старше таймаута не блокирует")
        protoctl.run_queue()
        self.assertEqual(protoctl.get_state(stale)["status"], "fail")

    def test_interrupted_job_is_not_replayed(self):
        jid = protoctl.submit("tuic", "disable")
        protoctl.state_dir().mkdir(parents=True, exist_ok=True)
        (protoctl.state_dir() / f"{jid}.json").write_text(   # перезагрузка посреди фазы: заявка и «выполняется» на месте
            json.dumps({"id": jid, "proto": "tuic", "action": "disable", "status": "running", "started": 1}),
            encoding="utf-8")
        protoctl.run_queue()
        self.assertEqual(self.calls(), [], "оборванная задача сама не повторяется")
        st = protoctl.get_state(jid)
        self.assertEqual(st["status"], "fail")
        self.assertIn("оборвана", st["error"])
        self.assertIsNone(protoctl.active())

    def test_unconsumed_queued_job_expires(self):
        jid = protoctl.submit("tuic", "disable")
        req = protoctl.jobs_dir() / f"{jid}.json"
        data = json.loads(req.read_text(encoding="utf-8"))
        data["created"] -= protoctl.QUEUE_STALE + 1
        req.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(protoctl.active(), "забытая заявка не держит переключатели")
        self.assertEqual(protoctl.active_by_proto(), {})
        protoctl.submit("tuic", "disable")   # не «Сейчас идёт…»
        protoctl.run_queue()
        self.assertEqual(self.calls(), ["--phase 04d-tuic"], "устаревшая не выполняется, свежая — да")
        self.assertEqual(protoctl.get_state(jid)["status"], "fail")
        self.assertFalse(req.exists())

    def test_request_removed_even_if_state_write_fails(self):
        jid = protoctl.submit("tuic", "disable")
        real = protoctl._write_state
        calls = []

        def flaky(j, st):
            calls.append(st["status"])
            if st.get("finished"):
                raise OSError(28, "No space left on device")
            real(j, st)
        with mock.patch.object(protoctl, "_write_state", flaky):
            with self.assertRaises(OSError):
                protoctl._run_one(protoctl.jobs_dir() / f"{jid}.json")
        self.assertFalse((protoctl.jobs_dir() / f"{jid}.json").exists(), "иначе path-юнит крутит её до упора")

    def test_cli_job_list(self):
        protoctl.submit("tuic", "disable")
        code, out, _ = run_cli("job", "list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["proto"], "tuic")
        code, out, _ = run_cli("job", "run")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls(), ["--phase 04d-tuic"])


class LiveCliTest(unittest.TestCase):
    def test_show_empty_and_with_rows(self):
        with ZooEnv():
            code, out, err = run_cli("live", "show")
            self.assertEqual(code, 0)
            self.assertIn("замеров ещё нет", out + err)
            con = history.connect()
            live.store(con, [{"ts": int(time.time()), "proto": "tuic", "ok": 1, "rtt_ms": 23.4, "jitter_ms": 4.0,
                              "mbps": 38.0, "verdict": "OK", "bytes": 0}])
            con.close()
            code, out, _ = run_cli("live", "show")
            self.assertEqual(code, 0)
            self.assertIn("tuic", out)
            self.assertIn("23 мс", out)
            code, out, _ = run_cli("live", "--json")
            self.assertEqual(json.loads(out)["tuic"]["mbps"], 38.0)


# ---------- страницы ----------

def seed_live(proto: str, now: float, rtt=23.0, jitter=4.0, mbps: float | None = 38.0, ok=1, verdict="OK",
              nbytes=0, age=0):
    con = history.connect()
    live.store(con, [{"ts": int(now - age), "proto": proto, "ok": ok, "rtt_ms": rtt, "jitter_ms": jitter,
                      "mbps": mbps, "verdict": verdict, "bytes": nbytes}])
    con.close()


class OverviewMetricsTest(AppTestBase):
    def setUp(self):
        super().setUp()
        for pid in ("vless-reality", "hysteria2"):
            self.env.add_manifest(pid)
        self.c.login()

    def test_card_shows_numbers_and_caption(self):
        now = time.time()
        seed_live("vless-reality", now, age=240)
        _, body = self.c.get("/")
        text = re.sub(r"<[^>]+>", "", body)
        self.assertIn("23 мс · ±4 · 4 мин назад", text)
        self.assertNotIn("38 Мбит/с", text, "живая скорость (1 МБ, медленный старт) занижена: не показывается")
        self.assertIn("нет замера", body, "у протокола без замеров — так, без нулей")
        self.assertIn("метрики раз в 10 мин · обновлено 4 мин назад", body)
        self.assertIn('action="/live/vless-reality"', body)
        self.assertIn('data-swap', body)

    def test_card_shows_server_capacity_from_the_selftest_not_live_speed(self):
        from zoolib import probe
        from zoolib.fsutil import atomic_write_json
        seed_live("vless-reality", time.time(), age=240)
        atomic_write_json(probe.selftest_file(), {"generated": "2026-10-06T10:00:00+00:00", "server_ip": "10.0.0.1",
                                                   "results": [{"id": "vless-reality", "verdict": "OK", "speed_mbps": 408.2},
                                                               {"id": "hysteria2", "verdict": "OK", "speed_mbps": None}]})
        _, body = self.c.get("/")
        text = re.sub(r"<[^>]+>", "", body)
        self.assertIn("сервер тянет ≈408 Мбит/с · 06.10", text)
        self.assertEqual(text.count("сервер тянет"), 1, "у протокола без скорости самопроверки — ничего")
        self.assertNotIn("38 Мбит/с", text)

    def test_traffic_in_card_excludes_service_user(self):
        users.bootstrap()
        now = int(time.time())
        con = traffic.connect()
        with con:
            samples = [traffic.Sample("vless-reality", "", 90 * 1024 ** 2, 1200 * 1024 ** 2)]
            deltas, new = traffic.compute_deltas({}, samples, now, known={"vless-reality"})
            traffic.store(con, deltas, new, now)
            con.execute("INSERT INTO runs VALUES (?, 1, 1, 0, 0, 0.1, '{}')", (traffic.align(now, traffic.RES_1D) + 1,))
        con.close()
        seed_live("vless-reality", now, nbytes=200 * 1024 ** 2)
        _, body = self.c.get("/")
        self.assertIn("↓1000 МБ ↑90 МБ", re.sub(r"<[^>]+>", "", body))   # 1200 МБ в счётчике минус 200 МБ, скачанных замерами
        self.assertIn('>≈1.1 ГБ<', body, "крупная цифра — тот же трафик без замеров, у Xray приблизительный")
        self.assertIn("≈↓1000", re.sub(r"<[^>]+>", "", body))

    def test_users_line_names_who_has_the_protocol(self):
        from tests.test_web import FAKE_SLOW
        proto = dict(FAKE_SLOW["protocols"][0], users=2, users_off=1, user_names=["owner", "masha"],
                     off_names=["kolya"], lacking=[["vasy", "нет в группе «Группа 2»"]])
        self.app.invalidate("status")
        with mock.patch("zoolib.status.collect_slow", return_value=dict(FAKE_SLOW, protocols=[proto])):
            _, body = self.c.get("/")
        self.assertIn("пользователей: 2", re.sub(r"<[^>]+>", "", body))
        self.assertIn("(+1 откл.)", body)
        self.assertIn("с протоколом: owner, masha" + chr(10) + "отключены: kolya" + chr(10) + "без протокола: vasy (нет в группе «Группа 2»)", body)

    def test_failed_and_stale_measurements(self):
        now = time.time()
        seed_live("vless-reality", now, rtt=None, jitter=None, mbps=None, ok=0, verdict="HANDSHAKE_FAIL")
        seed_live("hysteria2", now, age=3600)
        _, body = self.c.get("/")
        self.assertIn("сбой: HANDSHAKE_FAIL", body)
        self.assertIn('class="dot bad"', body)
        self.assertIn('class="dot muted"', body, "старый замер — серая точка")

    def test_stale_caption_hints_at_timer(self):
        seed_live("vless-reality", time.time(), age=3 * 3600)
        _, body = self.c.get("/")
        self.assertIn("замеры не идут", body)

    def test_refresh_button_posts_with_csrf_and_writes_request(self):
        resp, body = self.c.get("/")
        self.assertRegex(body, r'<form[^>]*action="/live/vless-reality"[^>]*>\s*<input type="hidden" name="csrf"')
        resp, _ = self.c.post("/live/vless-reality")
        self.assertEqual(resp.status, 303)
        self.assertTrue((live.req_dir() / "vless-reality").is_file())
        _, body = self.c.get("/")
        self.assertIn("Замер заказан", body)
        self.assertIn("меряю…", body)
        self.assertNotIn('action="/live/vless-reality"', body)
        # второй раз подряд — отказ, заявка одна
        self.c.post("/live/vless-reality")
        _, body = self.c.get("/")
        self.assertEqual(os.listdir(live.req_dir()), ["vless-reality"])

    def test_refresh_rate_limited_after_measurement(self):
        seed_live("vless-reality", time.time(), age=20)
        self.c.post("/live/vless-reality")
        self.assertFalse((live.req_dir() / "vless-reality").exists())
        _, body = self.c.get("/")
        self.assertIn("меньше минуты назад", body)

    def test_refresh_needs_csrf_login_and_known_protocol(self):
        resp, _ = self.c.post("/live/vless-reality", csrf=False)
        self.assertEqual(resp.status, 403)
        self.assertFalse(live.req_dir().exists())
        self.c.post("/live/ghost")
        self.assertFalse((live.req_dir() / "ghost").exists())
        anon = Client(self.app)
        resp, _ = anon.post("/live/vless-reality", {"csrf": "x"})
        self.assertEqual(resp.status, 401)
        resp, _ = self.c.post("/live/..%2Fevil")
        self.assertEqual(resp.status, 404)


class ToggleWebTest(AppTestBase):
    def setUp(self):
        super().setUp()
        protoctl.installed_scripts().mkdir(parents=True, exist_ok=True)
        for ph in PHASES:
            (protoctl.installed_scripts() / f"{ph}.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
        self.env.add_manifest("vless-reality")
        self.env.add_manifest("hysteria2")
        self.env.add_manifest("tuic", enabled=False, name="TUIC v5")
        self.c.login()

    def test_cards_offer_switch_with_confirm_and_off_list(self):
        _, body = self.c.get("/")
        self.assertIn('action="/protocols/vless-reality/disable"', body)
        self.assertIn('data-confirm="Выключить VLESS + REALITY?', body)
        self.assertIn("Выключены", body)
        self.assertIn('action="/protocols/tuic/enable"', body)
        self.assertIn("TUIC v5", body)
        self.assertIn("ss2022", body.lower().replace("shadowsocks-2022", "ss2022"), "не установленный протокол — в списке")

    def test_disable_creates_job_and_card_shows_progress(self):
        resp, _ = self.c.post("/protocols/hysteria2/disable")
        self.assertEqual(resp.status, 303)
        jobs = list(protoctl.jobs_dir().glob("*.json"))
        self.assertEqual(len(jobs), 1)
        data = json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertEqual((data["proto"], data["action"]), ("hysteria2", "disable"))
        _, body = self.c.get("/")
        self.assertIn("выключается…", body)
        self.assertNotIn('action="/protocols/vless-reality/disable"', body, "пока идёт задача кнопок нет")
        self.assertIn("Выключение", body)
        self.assertIn(f'href="/pjobs/{data["id"]}"', body)

    def test_last_enabled_protocol_cannot_be_disabled(self):
        self.env.add_manifest("hysteria2", enabled=False)
        _, body = self.c.get("/")
        self.assertIn("последний включённый", body)
        self.assertNotIn('action="/protocols/vless-reality/disable"', body)
        self.c.post("/protocols/vless-reality/disable")
        self.assertEqual(list(protoctl.jobs_dir().glob("*.json")) if protoctl.jobs_dir().exists() else [], [])
        _, body = self.c.get("/")
        self.assertIn("последний включённый протокол", body)

    def test_post_guard_and_whitelist(self):
        resp, _ = self.c.post("/protocols/tuic/enable", csrf=False)
        self.assertEqual(resp.status, 403)
        for path in ("/protocols/tuic/delete", "/protocols/..%2Fx/enable", "/protocols/TUIC/enable"):
            resp, _ = self.c.post(path)
            self.assertEqual(resp.status, 404, path)
        self.c.post("/protocols/ghost/enable")
        self.c.post("/protocols/vless-reality/enable")   # уже включён
        self.assertFalse(protoctl.jobs_dir().exists() and list(protoctl.jobs_dir().glob("*.json")))
        resp, _ = Client(self.app).post("/protocols/tuic/enable", {"csrf": "x"})
        self.assertEqual(resp.status, 401)

    def test_second_job_is_refused_while_one_runs(self):
        self.c.post("/protocols/tuic/enable")
        self.c.post("/protocols/hysteria2/disable")
        self.assertEqual(len(list(protoctl.jobs_dir().glob("*.json"))), 1)
        _, body = self.c.get("/")
        self.assertIn("включается…", body)

    def test_failed_job_alert_and_log_page_hides_secrets(self):
        protoctl.state_dir().mkdir(parents=True)
        jid = "e" * 32
        now = int(time.time())
        (protoctl.state_dir() / f"{jid}.json").write_text(json.dumps({
            "id": jid, "proto": "tuic", "action": "enable", "name": "TUIC v5", "status": "fail", "rc": 1,
            "started": now - 60, "finished": now, "error": "install.sh --phase 04d-tuic завершился с кодом 1"}),
            encoding="utf-8")
        (protoctl.state_dir() / f"{jid}.log").write_text(
            "$ install.sh --phase 04d-tuic\nошибка\npassword=hunter2secret\nvless://11111111-2222-3333-4444-555555555555@1.2.3.4:443?x=1#a\n",
            encoding="utf-8")
        _, body = self.c.get("/")
        self.assertIn("Включение TUIC v5 не удалось", body)
        resp, page = self.c.get(f"/pjobs/{jid}")
        self.assertEqual(resp.status, 200)
        self.assertIn("ошибка", page)
        self.assertNotIn("hunter2secret", page)
        self.assertNotIn("11111111-2222", page)
        self.assertEqual(self.c.get("/pjobs/" + "0" * 32)[0].status, 404)
        self.assertEqual(self.c.get("/pjobs/../../etc/passwd")[0].status, 404)

    def test_status_cache_dropped_when_job_finishes(self):
        calls = []
        real = self.app.invalidate
        self.app.invalidate = lambda *k: (calls.append(k), real(*k))[1]
        self.c.get("/")
        protoctl.state_dir().mkdir(parents=True)
        jid = "f" * 32
        (protoctl.state_dir() / f"{jid}.json").write_text(json.dumps(
            {"id": jid, "proto": "tuic", "action": "enable", "status": "ok", "rc": 0, "started": 1, "finished": 2}),
            encoding="utf-8")
        self.c.get("/")
        self.assertIn(("status",), calls)


class UnitFilesTest(unittest.TestCase):
    """Юниты читаются systemd: ловим опечатки и отступление от договорённостей, а не поведение systemd."""

    SD = REPO / "zoo" / "systemd"

    def unit(self, name):
        return (self.SD / name).read_text(encoding="utf-8")

    def test_timer_and_services(self):
        timer = self.unit("zoo-live.timer")
        for line in ("OnBootSec=2min", "OnUnitActiveSec=10min", "RandomizedDelaySec=30"):
            self.assertIn(line, timer)
        svc = self.unit("zoo-live.service")
        for line in ("Type=oneshot", "ExecStart=/usr/local/bin/zoo live run", "Nice=10", "CPUQuota=30%", "TimeoutStartSec="):
            self.assertIn(line, svc)
        self.assertIn("--requests", self.unit("zoo-live-req.service"))
        self.assertIn("Unit=zoo-live-req.service", self.unit("zoo-live.path"))
        self.assertIn("live-req", self.unit("zoo-live.path"))
        self.assertIn("ExecStart=/usr/local/bin/zoo job run", self.unit("zoo-job.service"))
        self.assertIn("Unit=zoo-job.service", self.unit("zoo-job.path"))

    def test_enable_list_and_install_glob(self):
        names = [ln.split()[0] for ln in self.unit("enable.list").splitlines() if ln.strip() and not ln.startswith("#")]
        for n in ("zoo-live.timer", "zoo-live.path", "zoo-job.path", "zoo-web.service", "zoo-collector.timer"):
            self.assertIn(n, names)
            self.assertTrue((self.SD / n).is_file(), n)
        self.assertIn('"$d"/*.path', (REPO / "scripts" / "09-zoo.sh").read_text(encoding="utf-8"))

    def test_web_has_no_systemctl_and_its_sandbox_covers_request_dirs(self):
        web = self.unit("zoo-web.service")
        self.assertIn("/var/lib/vpn-zoo", web.split("ReadWritePaths=")[1].splitlines()[0])
        for mod in ("protoviews.py",):
            src = (REPO / "zoo" / "zoolib" / "web" / mod).read_text(encoding="utf-8")
            for banned in ("subprocess", "os.system", "systemd-run", '"systemctl"'):
                self.assertNotIn(banned, src)

    def test_files_are_lf(self):
        for f in list(self.SD.iterdir()) + [REPO / "zoo" / "zoolib" / "protoctl.py",
                                            REPO / "zoo" / "zoolib" / "probe" / "live.py"]:
            self.assertNotIn(b"\r\n", f.read_bytes(), f.name)


if __name__ == "__main__":
    unittest.main()
