"""Метрики пробы, контекст, история (SQLite), рейтинг, анонимный экспорт и шифрование age.
Без сети: HTTP — на 127.0.0.1; age и ssh-keygen нужны только тесту шифрования (иначе он пропускается)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.helpers import ZooEnv
from tests.test_cli import run_cli
from tests.test_probe import ENTRIES, FakeClient
from zoolib.probe import clients, context, engine, export, fetch, history, metrics, rank, verdicts

SERVER_IP = "203.0.113.50"
MY_IP = "198.51.100.77"
EGRESS_IP = "192.0.2.9"
IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def result(pid, verdict="OK", lat=80.0, down=40.0, up=None, **kw):
    m = {}
    if lat is not None:
        m["latency"] = {"median_ms": lat, "p90_ms": lat * 1.5, "jitter_ms": 4.0, "loss_pct": 0.0}
    if down is not None:
        m["download"] = {"mbps": down}
    if up is not None:
        m["upload"] = {"mbps": up}
    r = {"id": pid, "name": pid, "layer": "tcp", "port": 443, "verdict": verdict,
         "latency_ms": None if lat is None else lat + 100, "speed_mbps": down, "egress_ip": EGRESS_IP,
         "l4": {"status": "ok", "rtt_ms": 12.0}, "host": SERVER_IP, "metrics": m}
    r.update(kw)
    return r


def report(results, ts="2026-10-05T12:34:56+00:00", mode="remote", tag="mobile-mts", device="pixel7",
           isp="MTS PJSC", asn=8359, label="vpn", **kw):
    ctx = {"tag": tag, "device": device, "net": "cellular", "country": "RU", "asn": asn, "isp": isp}
    return dict({"schema": 1, "type": "zoo-probe-report", "mode": mode, "generated": ts, "zoo": "x", "host": "box",
                 "server_ip": SERVER_IP, "label": label, "user": "zoo-probe", "direct_ip": MY_IP, "context": ctx,
                 "results": results}, **kw)


# ---------- метрики ----------

class MetricsTest(unittest.TestCase):
    def test_percentile(self):
        xs = [10, 20, 30, 40, 50]
        self.assertEqual(metrics.percentile(xs, 0), 10)
        self.assertEqual(metrics.percentile(xs, 50), 30)
        self.assertEqual(metrics.percentile(xs, 100), 50)
        self.assertAlmostEqual(metrics.percentile(xs, 90), 46.0)
        self.assertAlmostEqual(metrics.percentile([5.0], 90), 5.0)
        self.assertIsNone(metrics.percentile([], 90))

    def test_jitter(self):
        self.assertAlmostEqual(metrics.jitter([10, 20, 10, 20]), 10.0)
        self.assertAlmostEqual(metrics.jitter([50, 50, 50]), 0.0)
        self.assertIsNone(metrics.jitter([5]))

    def test_latency_stats_and_loss(self):
        s = metrics.latency_stats([100, 120, 110, 300], sent=5, connect=[40, 60])
        self.assertEqual((s["sent"], s["ok"], s["loss_pct"]), (5, 4, 20.0))
        self.assertEqual((s["min_ms"], s["median_ms"], s["max_ms"]), (100.0, 115.0, 300.0))
        self.assertAlmostEqual(s["p90_ms"], 246.0)
        self.assertAlmostEqual(s["jitter_ms"], (20 + 10 + 190) / 3, places=1)
        self.assertEqual(s["connect_median_ms"], 50.0)
        empty = metrics.latency_stats([], sent=2)
        self.assertEqual((empty["ok"], empty["loss_pct"], empty["median_ms"]), (0, 100.0, None))

    def test_latency_from_jobs_per_target(self):
        jobs = [{"url": "https://a.test/g", "ok": True, "ttfb_ms": 100, "connect_ms": 30},
                {"url": "https://b.test/g", "ok": True, "ttfb_ms": 200, "connect_ms": 50},
                {"url": "https://a.test/g", "ok": False, "ttfb_ms": None},
                {"url": "https://b.test/g", "ok": True, "ttfb_ms": 220, "connect_ms": 54}]
        m = metrics.latency_from_jobs(jobs)
        self.assertEqual((m["sent"], m["ok"], m["loss_pct"]), (4, 3, 25.0))
        self.assertEqual(m["median_ms"], 200.0)
        self.assertEqual(m["per_target"]["a.test"]["ok"], 1)
        self.assertEqual(m["per_target"]["a.test"]["loss_pct"], 50.0)
        self.assertEqual(m["per_target"]["b.test"]["median_ms"], 210.0)

    def test_speed(self):
        self.assertEqual(metrics.mbps(5_000_000, 2.0), 20.0)
        self.assertIsNone(metrics.mbps(0, 1))
        self.assertIsNone(metrics.mbps(100, None))
        d = metrics.download_block({"ok": True, "bytes": 1_000_000, "body_seconds": 1.0, "seconds": 1.2, "ttfb_ms": 50})
        self.assertEqual((d["mbps"], d["ttfb_ms"]), (8.0, 50))
        self.assertIsNone(metrics.download_block({"ok": False, "bytes": 10, "seconds": 1})["mbps"])
        u = metrics.upload_block({"ok": True, "sent_bytes": 2_000_000, "upload_seconds": 4.0})
        self.assertEqual(u["mbps"], 4.0)

    def test_result_numbers_old_and_new(self):
        n = metrics.result_numbers({"latency_ms": 150, "speed_mbps": 9.5, "l4": {"rtt_ms": 20}})
        self.assertEqual((n["latency_ms"], n["down_mbps"], n["rtt_ms"], n["up_mbps"]), (150, 9.5, 20, None))
        n = metrics.result_numbers(result("x", lat=70, down=33, up=5))
        self.assertEqual((n["latency_ms"], n["down_mbps"], n["up_mbps"], n["p90_ms"]), (70, 33, 5, 105.0))


# ---------- метки и контекст ----------

class ContextTest(unittest.TestCase):
    def test_labels(self):
        for ok in ("mobile-mts", "pixel7", "кафе Wi-Fi", "work/laptop (hub)", "a.b_c+d@e"):
            self.assertEqual(context.clean_label(ok), ok)
        self.assertEqual(context.clean_label("  mobile   mts "), "mobile mts")
        for bad in ("", "   ", "x" * 41, "a;b", "a:b", "a\x00b","<b>", "home 192.168.1.5", "10.0.0.1", "ip::1", "a'b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                context.clean_label(bad)

    def test_parse_meta(self):
        body = json.dumps({"clientIp": "1.2.3.4", "asn": 8359, "asOrganization": "MTS\x00 PJSC", "country": "RU",
                           "city": "Moscow"})
        self.assertEqual(context.parse_meta(body), {"asn": 8359, "isp": "MTS PJSC", "country": "RU"})
        self.assertEqual(context.parse_meta("{}"), {})
        self.assertEqual(context.parse_meta("<html>"), {})
        self.assertEqual(context.parse_meta('{"asn": "x", "country": "Russia"}'), {})

    def test_build_has_no_ip(self):
        c = context.build("t", "d", {"ip": MY_IP, "country": "RU", "asn": 1, "isp": "X"}, "wifi")
        self.assertNotIn(MY_IP, json.dumps(c))
        self.assertEqual(c["net"], "wifi")

    def test_net_hint_env(self):
        self.assertEqual(context.net_hint({"ZOO_PROBE_NET": "Cellular"}), "cellular")
        with mock.patch.object(context, "_default_iface", return_value=None):
            self.assertIsNone(context.net_hint({}))

    def test_direct_context_with_fakes(self):
        def fake(url, **kw):
            if "meta" in url:
                return {"ok": True, "body": json.dumps({"asn": 12389, "asOrganization": "Rostelecom", "country": "RU"})}
            return {"ok": True, "body": f"ip={MY_IP}\nloc=RU\n"}
        with mock.patch.object(context, "fetch", fake):
            d = context.direct_context(("https://x/trace",))
            self.assertEqual((d["ip"], d["country"], d["asn"], d["isp"]), (MY_IP, "RU", 12389, "Rostelecom"))
            d = context.direct_context(("https://x/trace",), lookup=False)
            self.assertEqual((d["ip"], d["asn"]), (MY_IP, None))


# ---------- fetch: отдача и заголовки ----------

class _Post(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        got = 0
        while got < n:
            chunk = self.rfile.read(min(65536, n - got))
            if not chunk:
                break
            got += len(chunk)
        body = json.dumps({"got": got, "referer": self.headers.get("Referer"),
                           "type": self.headers.get("Content-Type")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = json.dumps({"referer": self.headers.get("Referer")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FetchUploadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), _Post)
        cls.http.daemon_threads = True
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.http.server_address[1]}/__up"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def test_post_bytes(self):
        r = fetch.fetch(self.url, post_bytes=300_000, keep_body=200)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sent_bytes"], 300_000)
        self.assertEqual(json.loads(r["body"])["got"], 300_000)
        self.assertEqual(json.loads(r["body"])["type"], "application/octet-stream")
        self.assertIsNotNone(r["upload_seconds"])
        self.assertGreaterEqual(r["upload_seconds"], 0)
        self.assertEqual(metrics.upload_block(r)["bytes"], 300_000)

    def test_extra_headers_and_get_unchanged(self):
        r = fetch.fetch(self.url, extra_headers={"Referer": "https://speed.test/"}, keep_body=200)
        self.assertEqual(json.loads(r["body"])["referer"], "https://speed.test/")
        self.assertEqual((r["sent_bytes"], r["upload_seconds"]), (0, None))


# ---------- движок: расширенные метрики, вердикт не меняется ----------

class MetricsFakeClient(FakeClient):
    """Малые запросы с ttfb, отдача — по сценарию."""

    def __init__(self, scenario, log):
        super().__init__(scenario)
        self.log = log

    def run_jobs(self, jobs):
        self.log.append([j["url"] for j in jobs])
        out = []
        for j in jobs:
            if j.get("post_bytes"):
                out.append(self.s.get("upload", {"ok": True, "sent_bytes": j["post_bytes"], "upload_seconds": 2.0,
                                                 "url": j["url"]}))
            elif "generate_204" in j["url"] and self.s.get("small"):
                out.append(dict(self.s["small"], url=j["url"]))
            elif "generate_204" in j["url"]:
                host = j["url"].split("/")[2]
                out.append({"ok": True, "url": j["url"], "seconds": 0.3, "ttfb_ms": 100.0 if "gstatic" in host else 140.0,
                            "connect_ms": 20.0})
            else:
                out.extend(FakeClient.run_jobs(self, [j]))
        return out


class EngineMetricsTest(unittest.TestCase):
    def run_engine(self, st, scenario=None, only=("vless-reality",)):
        log = []
        entries = [e for e in ENTRIES if e["id"] in only]

        def fake_make(probe, workdir, mode_, stall):
            sc = dict(scenario or {})
            sc["handshake"] = ("unknown", "", None)
            return MetricsFakeClient(sc, log)
        with mock.patch.object(clients, "make_client", fake_make), \
                mock.patch.object(engine, "tcp_check", lambda h, p, t, tries=3: ("ok", 12.0)):
            return engine.run(entries, st, server_ip="198.51.100.1"), log

    def test_metrics_added_and_verdict_same(self):
        base, _ = self.run_engine(engine.Settings(progress=None, latency_samples=0))
        res, log = self.run_engine(engine.Settings(progress=None, latency_samples=4, upload_bytes=1_000_000))
        self.assertEqual(base[0]["verdict"], res[0]["verdict"])
        self.assertNotIn("latency", base[0]["metrics"])
        m = res[0]["metrics"]
        self.assertEqual(m["latency"]["sent"], 8)
        self.assertEqual(m["latency"]["ok"], 8)
        self.assertEqual(m["latency"]["median_ms"], 120.0)
        self.assertEqual(set(m["latency"]["per_target"]), {"www.gstatic.com", "cp.cloudflare.com"})
        self.assertEqual(m["download"]["mbps"], 40.0)
        self.assertEqual(m["upload"]["mbps"], 4.0)
        self.assertEqual(res[0]["speed_mbps"], 40.0)
        # старые поля на месте
        for k in ("latency_ms", "speed_mbps", "egress_ip", "verdict"):
            self.assertIn(k, res[0])

    def test_defaults_off_and_no_upload(self):
        res, log = self.run_engine(engine.Settings(progress=None))
        self.assertNotIn("upload", res[0]["metrics"])
        self.assertEqual(engine.Settings().large_bytes, 5_000_000)
        self.assertEqual(engine.Settings().upload_bytes, 0)

    def test_latency_stops_if_first_round_fails(self):
        log = []
        st = engine.Settings(progress=None, latency_samples=5)
        client = MetricsFakeClient({"small": {"ok": False, "seconds": 1.0, "error_kind": "timeout"}}, log)
        m = engine.measure_latency(client, st)
        self.assertEqual((m["sent"], m["ok"], m["loss_pct"]), (2, 0, 100.0))
        self.assertEqual(len(log), 1)

    def test_failed_protocol_has_no_metrics(self):
        res, _ = self.run_engine(engine.Settings(progress=None),
                                 {"small": {"ok": False, "error_kind": "reset", "error": "RST"}})
        self.assertNotIn("metrics", res[0])
        self.assertEqual(res[0]["verdict"], verdicts.HANDSHAKE_FAIL)

    def test_upload_only_when_download_ok(self):
        res, log = self.run_engine(
            engine.Settings(progress=None, upload_bytes=1_000_000, latency_samples=0),
            {"large": {"ok": False, "bytes": 11000, "stalled": True, "error_kind": "stall", "error": "x"}})
        self.assertEqual(res[0]["verdict"], verdicts.FREEZE_16K)
        self.assertFalse(any("__up" in u for batch in log for u in batch))


# ---------- история ----------

class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

    def test_record_dedupe_and_rows(self):
        rep = report([result("vless-reality"), result("hysteria2", "UDP_BLOCKED", down=None),
                      result("tuic", "SKIPPED")])
        rid, uid = history.record(rep, "upload")
        self.assertIsNotNone(rid)
        self.assertEqual(history.record(rep, "upload")[0], None)
        con = history.connect(create=False)
        self.assertEqual(history.counts(con), {"reports": 1, "results": 3})
        rows = {r["proto"]: r for r in history.fetch_results(con)}
        self.assertEqual(rows["vless-reality"]["latency_ms"], 80.0)
        self.assertEqual(rows["vless-reality"]["p90_ms"], 120.0)
        self.assertEqual(rows["vless-reality"]["tag"], "mobile-mts")
        self.assertEqual((rows["vless-reality"]["isp"], rows["vless-reality"]["asn"]), ("MTS PJSC", 8359))
        self.assertEqual(rows["hysteria2"]["verdict"], "UDP_BLOCKED")
        self.assertEqual(history.fetch_results(con, mode="local"), [])
        self.assertEqual(len(history.fetch_results(con, tag="nope")), 0)
        raw = next(history.raw_reports(con))
        self.assertEqual(raw["direct_ip"], MY_IP)
        self.assertEqual(oct(history.db_path().stat().st_mode & 0o777) if os.name != "nt" else "0o600", "0o600")
        con.close()

    def test_prune_by_age(self):
        old = report([result("a")], ts="2025-01-01T00:00:00+00:00")
        new = report([result("a")], ts="2026-10-01T00:00:00+00:00")
        history.record(old, "upload")
        history.record(new, "upload")
        con = history.connect(create=False)
        now = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
        self.assertEqual(history.prune(con, now), 0)            # ZOO_PROBE_KEEP_DAYS=0 — хранить всё
        with mock.patch.dict(os.environ, {"ZOO_PROBE_KEEP_DAYS": "365"}):
            self.assertEqual(history.prune(con, now), 1)
        con.commit()
        self.assertEqual(history.counts(con), {"reports": 1, "results": 1})   # results ушли каскадом
        con.close()

    def test_labels_override_and_validation(self):
        rep = report([result("a")], tag=None, device=None)
        history.record(rep, "upload", tag="cafe-wifi", device="laptop")
        con = history.connect(create=False)
        r = history.fetch_results(con)[0]
        self.assertEqual((r["tag"], r["device"]), ("cafe-wifi", "laptop"))
        con.close()
        with self.assertRaises(ValueError):
            history.record(report([result("b")]), "upload", tag="10.1.2.3")

    def test_validate(self):
        for bad in ({}, {"results": "x"}, {"results": [{"id": 1, "verdict": "OK"}]}, {"results": [{"id": "a"}]}):
            with self.assertRaises(history.HistoryError):
                history.record(bad, "upload")
        history.record([{"id": "a", "verdict": "OK"}], "upload")  # голый список results

    def test_record_safely_reports_error(self):
        self.assertIsNotNone(history.record_safely({"x": 1}, "upload"))
        self.assertIsNone(history.record_safely(report([result("a")]), "upload"))

    def test_record_notice(self):
        rep = report([result("a")])
        self.assertIn("записан", history.record_notice(rep, "upload"))
        self.assertIn("уже есть", history.record_notice(rep, "upload"))
        self.assertIn("не записан", history.record_notice({"x": 1}, "upload"))

    def test_bad_ids_rejected(self):
        for rid, verdict in (("<script>", "OK"), ("a" * 41, "OK"), ("a", "ok"), ("a", "OK<b>"), ("", "OK")):
            with self.assertRaises(history.HistoryError):
                history.record(report([result("vless-reality"), dict(result(rid), verdict=verdict)]), "upload")
        history.record(report([result("vless-reality", "FREEZE_16K"), result("hysteria2-obfs", "UDP_BLOCKED")]),
                       "upload")

    def test_malformed_numbers_and_context_sanitised(self):
        # битый присланный отчёт не должен ни падать, ни отравлять рейтинг и страницу
        bad = dict(result("vless-reality"), metrics="x", latency_ms="abc", speed_mbps=float("inf"), l4=[1])
        odd = dict(result("hysteria2"), metrics={"latency": [1], "download": {"mbps": True}, "upload": "y"})
        nan = dict(result("ss2022"), metrics={"latency": {"median_ms": float("nan")}, "download": {"mbps": "9"}})
        rep = report([bad, odd, nan])
        rep["context"] = {"tag": "x" * 500, "device": ["d"], "net": "<b>", "country": "ru", "asn": True,
                          "isp": "Evil\x00\nISP " + "y" * 200}
        history.record(rep, "upload")
        con = history.connect(create=False)
        rows = history.fetch_results(con)
        con.close()
        for r in rows:
            for k in ("latency_ms", "p90_ms", "jitter_ms", "loss_pct", "rtt_ms", "down_mbps", "up_mbps"):
                self.assertTrue(r[k] is None or isinstance(r[k], float), (r["proto"], k, r[k]))
        r = rows[0]
        self.assertEqual(len(r["tag"]), 40)
        self.assertEqual((r["device"], r["net"], r["country"], r["asn"]), (None, None, None, None))
        self.assertTrue(r["isp"].startswith("Evil ISP") and len(r["isp"]) <= 60)
        rank.rank(rows)  # не падает
        line = export.rows_for_report(rep)[0]
        self.assertEqual(len(line["tag"]), 40)
        self.assertNotIn("net", line)

    def test_since_ts(self):
        now = 1_000_000_000
        self.assertEqual(history.since_ts("24h", now), now - 86400)
        self.assertEqual(history.since_ts("2w", now), now - 14 * 86400)
        self.assertIsNone(history.since_ts("all"))
        for bad in ("30", "d", "0d", "5y"):
            with self.assertRaises(history.HistoryError):
                history.since_ts(bad)

    def test_old_report_without_context(self):
        rep = {"type": "zoo-probe-report", "mode": "remote", "generated": "2026-10-01T00:00:00+00:00",
               "results": [{"id": "vless-reality", "verdict": "OK", "latency_ms": 200, "speed_mbps": 12.5}]}
        history.record(rep, "upload")
        con = history.connect(create=False)
        r = history.fetch_results(con)[0]
        self.assertEqual((r["latency_ms"], r["down_mbps"], r["tag"]), (200, 12.5, None))
        con.close()

    def test_local_run_records(self):
        from zoolib import probe
        from zoolib.config import load as load_config
        self.env.add_manifest("vless-reality", probe={"kind": "xray", "outbound": {"protocol": "vless"}})
        fake = [dict(result("vless-reality"), target="public", notes=[])]
        with mock.patch.object(engine, "run", return_value=fake):
            rep = probe.run_local(load_config(), st=engine.Settings(progress=None, tag="srv"))
            self.assertEqual(rep["context"]["tag"], "srv")
            con = history.connect(create=False)
            self.assertEqual(history.fetch_results(con, mode="local")[0]["tag"], "srv")
            con.close()
            probe.run_local(load_config(), st=engine.Settings(progress=None), record_history=False)
            con = history.connect(create=False)
            self.assertEqual(history.counts(con)["reports"], 1)
            con.close()


# ---------- рейтинг ----------

def rows_for(entries):
    """entries: [(report_id, tag, proto, verdict, lat, down)]."""
    return [{"report_id": rid, "ts": rid, "mode": "remote", "tag": tag, "isp": isp, "device": None, "net": None,
             "country": None, "asn": None, "server": "vpn", "source": "upload", "proto": p, "verdict": v,
             "latency_ms": lat, "down_mbps": down, "p90_ms": None, "jitter_ms": None, "loss_pct": None,
             "rtt_ms": None, "up_mbps": None}
            for rid, tag, isp, p, v, lat, down in entries]


class RankTest(unittest.TestCase):
    def test_formula(self):
        # a: быстрее и стабильнее; b: вдвое медленнее и в 2 раза выше задержка; c: 50% успеха
        rows = []
        for i in range(10):
            rows += rows_for([(i, "t", None, "a", "OK", 100.0, 40.0), (i, "t", None, "b", "OK", 200.0, 20.0)])
            rows += rows_for([(i, "t", None, "c", "OK" if i % 2 else "UDP_BLOCKED", 100.0, 40.0)])
        ctx = rank.rank(rows)[0]
        by = {p["proto"]: p for p in ctx["protocols"]}
        self.assertEqual(by["a"]["score"], 100.0)
        self.assertEqual(by["b"]["score"], 50.0)        # 100 × 1,0 × (0,6×0,5 + 0,4×0,5)
        self.assertEqual(by["c"]["score"], 50.0)        # 100 × 0,5 × (0,6×1 + 0,4×1)
        self.assertEqual(by["c"]["success_pct"], 50.0)
        self.assertEqual([p["proto"] for p in ctx["protocols"]], ["a", "b", "c"])
        self.assertEqual(by["a"]["confidence"], "high")
        self.assertFalse(by["a"]["low_confidence"])
        self.assertEqual(ctx["reports"], 10)

    def test_top3_and_failing_excluded_from_top(self):
        rows = []
        for i in range(5):
            for n, (lat, down) in enumerate([(50, 90), (60, 80), (70, 70), (80, 60)]):
                rows += rows_for([(i, "t", None, f"p{n}", "OK", float(lat), float(down))])
            rows += rows_for([(i, "t", None, "dead", "IP_BLOCKED", None, None)])
        ctx = rank.rank(rows)[0]
        self.assertEqual([p["proto"] for p in ctx["top"]], ["p0", "p1", "p2"])
        dead = next(p for p in ctx["protocols"] if p["proto"] == "dead")
        self.assertEqual((dead["score"], dead["ok"]), (0.0, 0))
        self.assertNotIn(dead, ctx["top"])

    def test_low_confidence_and_skipped_not_counted(self):
        rows = rows_for([(1, "t", None, "a", "OK", 100.0, 10.0), (2, "t", None, "a", "SKIPPED", None, None),
                         (3, "t", None, "a", "CLIENT_ERROR", None, None)])
        p = rank.rank(rows)[0]["protocols"][0]
        self.assertEqual((p["n"], p["ok"], p["confidence"], p["low_confidence"]), (1, 1, "low", True))
        self.assertEqual(rank.confidence(3), "medium")
        self.assertEqual(rank.confidence(10), "high")

    def test_missing_metrics_renormalised(self):
        rows = rows_for([(i, "t", None, "a", "OK", None, 10.0) for i in range(3)]
                        + [(i, "t", None, "b", "OK", None, 5.0) for i in range(3)])
        by = {p["proto"]: p["score"] for p in rank.rank(rows)[0]["protocols"]}
        self.assertEqual(by, {"a": 100.0, "b": 50.0})
        only = rows_for([(i, "t", None, "z", "OK", None, None) for i in range(3)])
        self.assertEqual(rank.rank(only)[0]["protocols"][0]["score"], 50.0)   # нет метрик: множитель 0,5

    def test_contexts_do_not_mix(self):
        rows = []
        for i in range(4):
            rows += rows_for([(i, "mobile", None, "hy2", "OK", 40.0, 30.0), (i, "mobile", None, "vless", "SLOW", 90.0, 3.0)])
            rows += rows_for([(10 + i, "home", None, "hy2", "UDP_BLOCKED", None, None),
                              (10 + i, "home", None, "vless", "OK", 20.0, 80.0)])
        ranking = {c["context"]: c for c in rank.rank(rows)}
        self.assertEqual(ranking["mobile"]["top"][0]["proto"], "hy2")
        self.assertEqual(ranking["home"]["top"][0]["proto"], "vless")
        self.assertEqual([p["proto"] for p in ranking["home"]["top"]], ["vless"])

    def test_context_label_fallbacks(self):
        self.assertEqual(rank.context_label({"tag": "t", "isp": "i"}), "t")
        self.assertEqual(rank.context_label({"tag": None, "isp": "MTS"}), "MTS")
        self.assertEqual(rank.context_label({}), rank.NO_LABEL)
        self.assertEqual(rank.context_label({"tag": "t", "isp": "i"}, "isp"), "i")

    def test_load_and_cli(self):
        with ZooEnv():
            code, out, _ = run_cli("probe", "--rank")
            self.assertEqual(code, 0)
            self.assertIn("нет клиентских проб", out)
            now = datetime.now(timezone.utc)
            for i in range(4):
                ts = now.replace(microsecond=0).isoformat()
                rep = report([result("hysteria2", lat=40, down=30), result("vless-reality", "SLOW", lat=90, down=1.5),
                              result("amneziawg", "HANDSHAKE_FAIL", lat=None, down=None)], ts=ts)
                rep["host"] = f"box{i}"       # разное содержимое — разные отчёты
                history.record(rep, "upload")
            history.record(report([result("hysteria2")], mode="local", tag=None), "local")
            code, out, _ = run_cli("--json", "probe", "--rank", "--period", "7d")
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(len(data["rank"]), 1)
            c = data["rank"][0]
            self.assertEqual(c["context"], "mobile-mts")
            self.assertEqual(c["reports"], 4)
            self.assertEqual(c["top"][0]["proto"], "hysteria2")
            self.assertEqual(c["top"][0]["score"], 100.0)
            self.assertEqual(c["protocols"][-1]["proto"], "amneziawg")
            code, out, _ = run_cli("probe", "--rank", "--tag", "mobile-mts")
            self.assertIn("hysteria2", out)
            self.assertIn("оценка =", out)
            code, out, _ = run_cli("--json", "probe", "--rank", "--tag", "nope")
            self.assertEqual(json.loads(out)["rank"], [])
            code, out, _ = run_cli("--json", "probe", "--rank", "--with-local", "--by", "tag")
            self.assertEqual(len(json.loads(out)["rank"]), 2)
            code, _, err = run_cli("probe", "--rank", "--period", "5y")
            self.assertEqual(code, 2)


class RankPlacesTest(unittest.TestCase):
    """Меньше 3 прогонов — ориентир, не рейтинг; от 3 — медиана с разбросом."""

    def test_one_run_is_not_a_ranking(self):
        rows = rows_for([(1, "t", None, "a", "OK", 100.0, 40.0), (1, "t", None, "b", "OK", 200.0, 20.0)])
        ctx = rank.rank(rows)[0]
        self.assertFalse(ctx["ranked"])
        self.assertEqual(ctx["top"], [])
        self.assertEqual(ctx["protocols"][0]["latency_p25"], None)

    def test_three_runs_rank_with_spread(self):
        rows = []
        for i, lat in enumerate([80.0, 100.0, 130.0]):
            rows += rows_for([(i, "t", None, "a", "OK", lat, 40.0 + i), (i, "t", None, "b", "OK", 300.0, 10.0)])
        ctx = rank.rank(rows)[0]
        self.assertTrue(ctx["ranked"])
        self.assertEqual([p["proto"] for p in ctx["top"]], ["a", "b"])
        a = ctx["protocols"][0]
        self.assertEqual((a["latency_ms"], a["latency_p25"], a["latency_p75"]), (100.0, 90.0, 115.0))
        self.assertEqual(rank.spread_text(a, "latency", ".0f", " мс"), "100 мс (90–115)")
        self.assertEqual(rank.spread_text({"latency_ms": 5.0}, "latency", ".0f", " мс"), "5 мс")
        self.assertEqual(rank.spread_text({}, "down", ".1f", ""), "—")

    def test_protocol_with_two_runs_gets_no_place(self):
        rows = []
        for i in range(3):
            rows += rows_for([(i, "t", None, "a", "OK", 100.0, 40.0)])
        rows += rows_for([(0, "t", None, "new", "OK", 10.0, 90.0), (1, "t", None, "new", "OK", 10.0, 90.0)])
        ctx = rank.rank(rows)[0]
        self.assertEqual([p["proto"] for p in ctx["top"]], ["a"])

    def test_plural(self):
        self.assertEqual([rank.plural_runs(n) for n in (1, 2, 5, 11, 21)],
                         ["1 замер", "2 замера", "5 замеров", "11 замеров", "21 замер"])

    def test_cli_low_data_block(self):
        out = io.StringIO()
        rows = rows_for([(1, "t", None, "a", "OK", 100.0, 40.0)])
        with contextlib.redirect_stdout(out):
            rank.render(rank.rank(rows), "30d")
        self.assertIn("1 замер — ориентир, не рейтинг", out.getvalue())
        self.assertNotIn("место", out.getvalue())


SERVERS = [
    {"id": "hysteria2", "name": "Hysteria2", "full": "Hysteria2", "enabled": True},
    {"id": "hysteria2-obfs", "name": "HY2 + Salamander", "full": "Hysteria2 + Salamander", "enabled": True},
    {"id": "tuic", "name": "TUIC v5", "full": "TUIC v5 (3x-ui native)", "enabled": True},
    {"id": "ss2022", "name": "Shadowsocks-2022", "full": "Shadowsocks-2022 (x)", "enabled": False},
]


class RankViewTest(unittest.TestCase):
    def render(self, rows, servers=SERVERS):
        from zoolib.web import probeviews
        on, off = probeviews.split_rows(servers, rows)
        return (str(probeviews.best_card(rank.rank(on), "30d", servers, rank.rank(off))),
                str(probeviews.trends_card(rows, servers)))

    def test_low_data_block(self):
        best, trends = self.render(rows_for([(1, "t", None, "hysteria2", "OK", 339.0, 40.0),
                                             (1, "t", None, "ss2022", "UDP_BLOCKED", None, None)]))
        self.assertIn("ориентир, не рейтинг", best)
        self.assertEqual(best.count("ориентир, не рейтинг"), 1)
        self.assertEqual(best.count("мало данных</span>"), 1)
        self.assertNotIn(">место<", best)
        self.assertNotIn(">оценка<", best)
        self.assertIn("HY2 + Salamander", best)
        self.assertIn("TUIC v5", best)
        self.assertEqual(best.lower().count("нет замеров с устройств"), 1, "без замеров — одной строкой, а не в каждой таблице")
        self.assertRegex(best, r"Нет замеров с устройств: HY2 \+ Salamander, TUIC v5")
        self.assertNotIn("hysteria2-obfs", best.replace('title="', ""))
        self.assertIn('class="dim"', best)       # выключенный ss2022 — серым, со старым вердиктом
        self.assertNotIn("<strong>TUIC v5</strong>", best, "TUIC без замеров — в строке под таблицами, не строкой")
        self.assertNotIn("нет замеров с устройств", trends.lower(), "строки без замеров в трендах не нужны")
        self.assertIn('href="#client-probe"', best)

    def test_ranked_block_with_spread_and_empty_places(self):
        rows = []
        for i, lat in enumerate([80.0, 100.0, 130.0]):
            rows += rows_for([(i, "t", None, "hysteria2", "OK", lat, 50.0), (i, "t", None, "tuic", "OK", 200.0, 20.0)])
            rows += rows_for([(i, "t", None, "ss2022", "OK", 90.0, 30.0)])
        best, trends = self.render(rows)
        self.assertIn(">место<", best)
        self.assertIn("100 мс (90–115)", best)
        self.assertIn("100 мс (90–115)", trends)
        self.assertNotIn(">—<", best)                                      # пустое место, не «—» в колонке мест
        self.assertNotIn("Shadowsocks-2022", best.split('class="dim"')[0])  # выключенный не занимает место
        self.assertIn('data-label="место">1</td>', best)
        self.assertEqual(best.count("мало данных</span>"), 0)
        self.assertIn("нет замеров с устройств", best.lower())              # HY2 + Salamander без замеров

    def test_without_manifests_everything_in_history_is_shown(self):
        best, trends = self.render(rows_for([(1, "t", None, "a", "OK", 100.0, 40.0)]), servers=[])
        self.assertIn(">a<", best.replace("<strong>", ">").replace("</strong>", "<"))
        self.assertIn("a", trends)


# ---------- анонимизация и экспорт ----------

def has_age():
    return bool(export.find_age()) and bool(shutil.which("ssh-keygen"))


class ExportTest(unittest.TestCase):
    def sample(self):
        return [report([result("vless-reality"), result("hysteria2", "UDP_BLOCKED", down=None, up=3.2)]),
                report([result("vless-reality", "SLOW", lat=900, down=1.2)], ts="2026-09-30T23:59:59+00:00",
                       tag="cafe-wifi", device="laptop", isp="Beeline", asn=3216, label="vpn-main")]

    def test_no_ip_leaks_into_jsonl(self):
        rep = self.sample()[0]
        # ловушки: IP в произвольных полях — тег/устройство обходят проверку (старый клиент, ручная правка),
        # провайдер и метка сервера приходят снаружи
        rep["context"].update(tag="home 192.168.1.5", isp=f"Net {SERVER_IP} Ltd", device="fe80::1")
        rep["label"] = f"srv-{MY_IP}"
        rep["results"][0]["notes"] = [f"выход с {EGRESS_IP}"]
        with tempfile.TemporaryDirectory() as d:
            stats = export.write_dir(Path(d), [rep] + self.sample()[1:])
            text = "".join(p.read_text(encoding="utf-8") for p in sorted(Path(d).glob("*.jsonl")))
            self.assertGreater(stats["rows"], 0)
            for ip in (SERVER_IP, MY_IP, EGRESS_IP, "192.168.1.5", "fe80::1"):
                self.assertNotIn(ip, text)
            self.assertIsNone(IP_RE.search(text), text)
            for line in text.splitlines():
                row = json.loads(line)
                for k, v in row.items():
                    if isinstance(v, str):
                        self.assertIsNone(context.IPV4_RE.search(v), (k, v))
                        self.assertIsNone(context.IPV6_RE.search(v), (k, v))
                for forbidden in ("server_ip", "direct_ip", "egress_ip", "host", "user", "notes", "raw"):
                    self.assertNotIn(forbidden, row)

    def test_row_fields_and_precision(self):
        rows = export.rows_for_report(self.sample()[0])
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual(r["ts"], "2026-10-05T12Z")           # до часа, UTC
        self.assertEqual(list(r)[0], "ts")                      # ts первым: сортировка строк = по времени
        self.assertEqual((r["server"], r["isp"], r["asn"], r["country"]), ("vpn", "MTS PJSC", 8359, "RU"))
        self.assertEqual((r["tag"], r["device"], r["proto"], r["verdict"]), ("mobile-mts", "pixel7", "vless-reality", "OK"))
        self.assertEqual((r["latency_ms"], r["down_mbps"], r["p90_ms"]), (80.0, 40.0, 120.0))
        self.assertEqual(rows[1]["up_mbps"], 3.2)
        self.assertNotIn("down_mbps", rows[1])                   # нет значения — нет ключа
        self.assertEqual(r["id"], history.report_uid(self.sample()[0]))
        self.assertRegex(r["id"], r"^[0-9a-f]{16}$")

    def test_merge_idempotent_sorted_by_month(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            export.write_dir(out, self.sample())
            first = {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.jsonl")}
            self.assertEqual(set(first), {"2026-09.jsonl", "2026-10.jsonl"})
            stats = export.write_dir(out, self.sample())
            self.assertEqual(stats["rows"], 0)
            self.assertEqual(first, {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.jsonl")})
            later = report([result("ss2022")], ts="2026-10-05T09:00:00+00:00")
            export.write_dir(out, [later])
            lines = (out / "2026-10.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, sorted(lines))
            self.assertEqual(json.loads(lines[0])["ts"], "2026-10-05T09Z")

    def test_recipients_parse(self):
        keys = export.parse_recipients("# comment\n\nssh-ed25519 AAAAC3Nz owner@pc\nage1abcdef\n")
        self.assertEqual(len(keys), 2)
        for bad in ("AGE-SECRET-KEY-1ABC\n", "-----BEGIN OPENSSH PRIVATE KEY-----\n", "hello\n", "# only\n"):
            with self.assertRaises(export.ExportError):
                export.parse_recipients(bad)

    def test_raw_requires_age(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(export, "find_age", return_value=None):
            with self.assertRaises(export.ExportError):
                export.write_dir(Path(d), self.sample(), ["ssh-ed25519 AAAA"], None)

    @unittest.skipUnless(has_age(), "нет age и ssh-keygen")
    def test_age_roundtrip(self):
        age = export.find_age()
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "id"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            pub = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
            out = Path(d) / "hist"
            reps = self.sample()
            stats = export.write_dir(out, reps, export.parse_recipients(pub + "\n"), age)
            self.assertEqual(stats["raw_new"], 2)
            for rep in reps:
                f = out / "raw" / f"{history.report_uid(rep)}.json.age"
                blob = f.read_bytes()
                self.assertTrue(blob.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"))
                self.assertNotIn(MY_IP.encode(), blob)
                plain = export.decrypt(blob, str(key), age)
                self.assertEqual(json.loads(plain), rep)
            # повторная выгрузка не перезаписывает файлы
            before = (out / "raw" / f"{history.report_uid(reps[0])}.json.age").read_bytes()
            stats = export.write_dir(out, reps, export.parse_recipients(pub + "\n"), age)
            self.assertEqual((stats["raw_new"], stats["raw_have"]), (0, 2))
            self.assertEqual(before, (out / "raw" / f"{history.report_uid(reps[0])}.json.age").read_bytes())
            # чужой ключ расшифровать не может
            other = Path(d) / "other"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
            with self.assertRaises(export.ExportError):
                export.decrypt(blob, str(other), age)

    def test_cli_add_list_export(self):
        with ZooEnv() as env:
            f = env.root / "r.json"
            f.write_text(json.dumps(self.sample()[0]), encoding="utf-8")
            code, out, err = run_cli("history", "add", str(f), "--device", "tablet")
            self.assertEqual(code, 0, err)
            code, _, err = run_cli("history", "add", str(f), "--device", "tablet")
            self.assertIn("уже есть", err)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_cli("history", "add", str(f), "--tag", "1.2.3.4")
            code, out, _ = run_cli("--json", "history", "list", "--period", "all")
            rows = json.loads(out)["reports"]
            self.assertEqual((len(rows), rows[0]["device"], rows[0]["working"]), (1, "tablet", 1))
            code, out, _ = run_cli("history", "list", "--period", "all")
            self.assertIn("mobile-mts", out)
            dest = env.root / "hist"
            code, _, err = run_cli("history", "export", "--out", str(dest))
            self.assertEqual(code, 1)
            self.assertIn("--recipients", err)
            code, out, _ = run_cli("--json", "history", "export", "--no-raw", "--out", str(dest))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["rows"], 2)
            self.assertTrue(list(dest.glob("*.jsonl")))
            self.assertFalse((dest / "raw").exists())
            code, _, err = run_cli("history", "export", "--no-raw")
            self.assertEqual(code, 1)

    def test_cli_export_empty_history(self):
        with ZooEnv():
            code, _, err = run_cli("history", "export", "--no-raw", "--out", "x")
            self.assertEqual(code, 1)
            self.assertIn("пуста", err)

    def test_probe_history_dir(self):
        from zoolib import probe
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(history_dir=d, history_recipients=None)
            probe._write_history_dir(self.sample()[0], args)
            self.assertTrue(list(Path(d).glob("*.jsonl")))
            self.assertFalse((Path(d) / "raw").exists())      # нет recipients.txt — только jsonl
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "recipients.txt").write_text("# шаблон: ключей ещё нет\n", encoding="utf-8")
            probe._write_history_dir(self.sample()[0], mock.Mock(history_dir=d, history_recipients=None))
            self.assertTrue(list(Path(d).glob("*.jsonl")))    # шаблон без ключей не мешает истории
            self.assertFalse((Path(d) / "raw").exists())
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "recipients.txt").write_text("AGE-SECRET-KEY-1ABC\n", encoding="utf-8")
            probe._write_history_dir(self.sample()[0], mock.Mock(history_dir=d, history_recipients=None))
            self.assertEqual(list(Path(d).glob("*.jsonl")), [])   # приватный ключ в списке — запись отменена


# ---------- вывод отчёта ----------

class ReportRenderTest(unittest.TestCase):
    def render(self, rep):
        from zoolib.probe import report as rp
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rp.render(rep)
        return buf.getvalue(), rp.markdown(rep)

    def test_metrics_columns_and_context(self):
        rep = report([result("vless-reality", up=5.5), result("hysteria2", "UDP_BLOCKED", down=None, lat=None)])
        text, md = self.render(rep)
        for s in ("Условия: метка mobile-mts, устройство pixel7, провайдер AS8359 MTS PJSC, RU, сеть: мобильная сеть",
                  "ответ p50/p90, мс", "джиттер, мс", "отдача, Мбит/с", "80/120"):
            self.assertIn(s, text)
        self.assertIn("- условия: метка mobile-mts", md)
        self.assertIn("| Ответ p50/p90, мс | Джиттер, мс |", md)

    def test_old_report_unchanged(self):
        rep = {"mode": "remote", "server_ip": "1.2.3.4", "user": "u",
               "results": [{"id": "a", "verdict": "OK", "port": 1, "layer": "tcp", "latency_ms": 100, "speed_mbps": 5.0,
                            "l4": {"rtt_ms": 3}}]}
        text, md = self.render(rep)
        self.assertNotIn("p50/p90", text)
        self.assertNotIn("Условия", text)
        self.assertIn("| Протокол | Порт | Итог | RTT, мс | Задержка, мс | Мбит/с | IP выхода | Причина |", md)


# ---------- веб ----------

class WebHistoryTest(unittest.TestCase):
    def test_probe_page_blocks_and_compare_records(self):
        from tests.test_web import FAKE_STATUS, TOKEN, Client
        from zoolib import config
        from zoolib.web.app import App
        with ZooEnv():
            with mock.patch("zoolib.status.collect", return_value=FAKE_STATUS):
                app = App(TOKEN, config.load)
                c = Client(app)
                c.login()
                resp, body = c.get("/probe")
                self.assertEqual(resp.status, 200)
                self.assertIn("Лучшие протоколы", body)
                self.assertIn("Нет клиентских проб", body)
                rep = report([result("hysteria2", lat=40, down=30), result("vless-reality", "FREEZE_16K", down=None)],
                             ts=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
                resp, body = c.post("/probe/compare", {"report": json.dumps(rep), "tag": "cafe-wifi", "device": ""})
                self.assertEqual(resp.status, 200)
                self.assertIn("записан в историю", body)
                resp, body = c.post("/probe/compare", {"report": json.dumps(rep), "tag": "1.2.3.4"})
                self.assertIn("IP-адрес", body)
                resp, body = c.get("/probe")
                self.assertIn("cafe-wifi", body)
                self.assertIn("hysteria2", body)
                self.assertIn("Тренды по протоколам", body)
                self.assertIn("История прогонов", body)
                self.assertIn("<svg", body)
                self.assertNotIn("style=", body)
                for p in ("7d", "all", "bogus"):
                    resp, body = c.get(f"/probe?rp={p}")
                    self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
