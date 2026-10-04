import contextlib
import io
import json
import time
import unittest
from unittest import mock

from tests.helpers import ZooEnv, needs_bash
from zoolib import config, traffic
from zoolib.traffic import Counter, Sample, compute_deltas


def S(proto, user, up, down, epoch="", seen=None):
    return Sample(proto, user, up, down, epoch, seen)


class DeltaTest(unittest.TestCase):
    def test_first_run_is_baseline(self):
        deltas, new = compute_deltas({}, [S("xray", "masha", 1000, 5000)], now=100)
        self.assertEqual([(d.up, d.down) for d in deltas], [(0, 0)])
        self.assertEqual(new[("xray", "masha")].up, 1000)

    def test_normal_increment(self):
        prev = {("xray", "masha"): Counter(1000, 5000, "", 100)}
        deltas, _ = compute_deltas(prev, [S("xray", "masha", 1500, 9000)], now=400)
        self.assertEqual((deltas[0].up, deltas[0].down, deltas[0].reset), (500, 4000, False))

    def test_counter_reset_counts_current_value(self):
        prev = {("hysteria2", "masha"): Counter(10_000, 50_000, "", 100)}
        deltas, _ = compute_deltas(prev, [S("hysteria2", "masha", 300, 700)], now=400)
        self.assertEqual((deltas[0].up, deltas[0].down, deltas[0].reset), (300, 700, True))

    def test_epoch_change_is_reset_even_if_value_grew(self):
        # сервис перезапустился и за 5 минут набрал больше, чем было до рестарта
        prev = {("hysteria2", "masha"): Counter(100, 100, "pid1", 100)}
        deltas, new = compute_deltas(prev, [S("hysteria2", "masha", 900, 800, epoch="pid2")], now=400)
        self.assertEqual((deltas[0].up, deltas[0].down, deltas[0].reset), (900, 800, True))
        self.assertEqual(new[("hysteria2", "masha")].epoch, "pid2")

    def test_new_user_of_known_protocol_counts_from_zero(self):
        prev = {("hysteria2", "owner"): Counter(1, 1, "", 100)}
        deltas, _ = compute_deltas(prev, [S("hysteria2", "owner", 1, 1), S("hysteria2", "kolya", 40, 60)], now=400)
        by_user = {d.user: (d.up, d.down) for d in deltas}
        self.assertEqual(by_user, {"owner": (0, 0), "kolya": (40, 60)})

    def test_first_traffic_of_polled_protocol_counts(self):
        # Hysteria до первого трафика отдаёт {} — протокол опрошен, серий нет
        deltas, _ = compute_deltas({}, [S("hysteria2", "owner", 3000, 2_000_000)], now=400, known={"hysteria2"})
        self.assertEqual((deltas[0].up, deltas[0].down), (3000, 2_000_000))

    def test_duplicate_series_are_summed(self):
        prev = {("amneziawg", "?x"): Counter(10, 10, "", 100)}
        deltas, _ = compute_deltas(prev, [S("amneziawg", "?x", 10, 20), S("amneziawg", "?x", 5, 5)], now=400)
        self.assertEqual((deltas[0].up, deltas[0].down), (5, 15))

    def test_seen_updates_only_on_activity(self):
        prev = {("hysteria2", "a"): Counter(10, 10, "", 100, seen=100), ("hysteria2", "b"): Counter(5, 5, "", 100, 90)}
        _, new = compute_deltas(prev, [S("hysteria2", "a", 10, 10), S("hysteria2", "b", 6, 5)], now=400)
        self.assertEqual(new[("hysteria2", "a")].seen, 100)
        self.assertEqual(new[("hysteria2", "b")].seen, 400)

    def test_proto_totals_from_users(self):
        deltas = [traffic.Delta("hysteria2", "a", 1, 2), traffic.Delta("hysteria2", "b", 10, 20),
                  traffic.Delta("vless-reality", "", 7, 7)]
        out = traffic.with_proto_totals(deltas, {"hysteria2"})
        totals = {(d.proto, d.user): (d.up, d.down) for d in out}
        self.assertEqual(totals[("hysteria2", "")], (11, 22))
        self.assertNotIn(("vless-reality", "x"), totals)


class AlignTest(unittest.TestCase):
    def test_buckets(self):
        ts = 1_700_000_123
        self.assertEqual(traffic.align(ts, 300) % 300, 0)
        self.assertLessEqual(traffic.align(ts, 300), ts)
        day = traffic.align(ts, 86400)
        self.assertEqual(time.localtime(day).tm_hour, 0)
        self.assertEqual(time.localtime(day).tm_min, 0)

    def test_window(self):
        since, step, n, res = traffic.window("24h", now=1_700_000_000)
        self.assertEqual((step, n, res), (3600, 24, 3600))
        self.assertGreater(1_700_000_000, since + (n - 1) * step - 1)
        with self.assertRaises(ValueError):
            traffic.window("5y")
        self.assertEqual(traffic.resolve_period("day"), "24h")


class StoreReportTest(unittest.TestCase):
    """Запись приращений в SQLite и отчёты (без источников: подаём снятия руками)."""

    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.now = int(time.time())

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def snap(self, samples, now, sum_protos=("hysteria2",)):
        con = traffic.connect()
        with con:
            deltas, new = compute_deltas(traffic.load_counters(con), samples, now)
            deltas = traffic.with_proto_totals(deltas, set(sum_protos))
            traffic.store(con, deltas, new, now)
        con.close()

    def fill(self):
        t0 = self.now - 1200
        self.snap([S("xray", "owner", 0, 0), S("xray", "masha", 0, 0), S("vless-reality", "", 0, 0),
                   S("hysteria2", "masha", 0, 0), S(traffic.HOST, "", 0, 0)], t0)
        self.snap([S("xray", "owner", 1000, 9000), S("xray", "masha", 100, 900), S("vless-reality", "", 1100, 9900),
                   S("hysteria2", "masha", 50, 450), S(traffic.HOST, "", 30000, 30000)], t0 + 300)
        # рестарт hysteria: счётчик меньше прежнего
        self.snap([S("xray", "owner", 2000, 18000), S("xray", "masha", 100, 900), S("vless-reality", "", 2100, 18900),
                   S("hysteria2", "masha", 10, 90), S(traffic.HOST, "", 60000, 60000)], t0 + 600)

    def test_report_by_user(self):
        self.fill()
        rep = traffic.report(period="24h")
        rows = {r["key"]: (r["up"], r["down"]) for r in rep["rows"]}
        self.assertEqual(rows["owner"], (2000, 18000))
        self.assertEqual(rows["masha"], (100 + 50 + 10, 900 + 450 + 90))
        self.assertEqual(rep["rows"][0]["key"], "owner")  # сортировка по объёму
        self.assertNotIn("", rows)

    def test_report_by_protocol_and_user_protocols(self):
        self.fill()
        rep = traffic.report(period="24h", by="protocol")
        rows = {r["key"]: r["total"] for r in rep["rows"]}
        self.assertEqual(rows, {"vless-reality": 2100 + 18900, "hysteria2": 60 + 540})
        self.assertNotIn(traffic.HOST, rows)
        mine = traffic.report(user="masha", period="7d")
        self.assertEqual(mine["by"], "protocol")
        self.assertEqual({r["key"] for r in mine["rows"]}, {"xray", "hysteria2"})
        self.assertEqual(mine["total"]["total"], 160 + 1440)

    def test_timeseries_and_today(self):
        self.fill()
        ts = traffic.timeseries("1h", "user")
        self.assertEqual(len(ts["buckets"]), 12)
        totals = {s["key"]: sum(s["values"]) for s in ts["series"]}
        self.assertEqual(totals["owner"], 20000)
        host = traffic.timeseries("24h", "host")
        self.assertEqual(sum(host["series"][0]["values"]), 120000)
        today = traffic.today("protocol")
        # корзины 20-минутной давности могут попасть во вчера только около полуночи
        if time.localtime(self.now - 1200).tm_yday == time.localtime(self.now).tm_yday:
            self.assertEqual(today["hysteria2"], 600)
        self.assertIn("masha", traffic.last_seen())

    def test_timeseries_top_folds_rest(self):
        self.snap([S("xray", f"u{i}", 0, 0) for i in range(10)], self.now - 600)
        self.snap([S("xray", f"u{i}", i + 1, 0) for i in range(10)], self.now - 300)
        ts = traffic.timeseries("1h", "user", top=4)
        self.assertEqual(len(ts["series"]), 4)
        self.assertEqual(ts["series"][-1]["title"], "прочие")
        self.assertEqual(sum(sum(s["values"]) for s in ts["series"]), sum(range(1, 11)))

    def test_prune(self):
        con = traffic.connect()
        with con:
            old = self.now - 10 * 86400
            con.execute("INSERT INTO traffic VALUES (300, ?, 'xray', 'a', 1, 1)", (old,))
            con.execute("INSERT INTO traffic VALUES (3600, ?, 'xray', 'a', 1, 1)", (old,))
            traffic.prune(con, self.now)
        left = {r[0] for r in con.execute("SELECT res FROM traffic")}
        con.close()
        self.assertEqual(left, {3600})

    def test_report_without_db(self):
        rep = traffic.report(period="24h")
        self.assertTrue(rep.get("empty"))
        self.assertEqual(traffic.today(), {})
        self.assertIsNone(traffic.last_run())


class GatherTest(unittest.TestCase):
    """Источники: 3x-ui API и trafficStats Hysteria подменяются."""

    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.env.add_manifest("vless-reality", xui_inbound_id=3)
        self.env.add_manifest("ss2022", xui={"inbound_id": 5}, port=30001)
        self.env.add_manifest("vless-xhttp", params={"inbound_id": 9}, port=443)
        self.env.add_manifest("hysteria2", engine="hysteria", users_backend="hysteria-command",
                              service="hysteria-server.service", layer="udp")
        self.env.write_config({"SERVER_IP": "10.0.0.1", "PANEL_PORT": "1", "PANEL_PATH": "p",
                               "XUI_API_TOKEN": "t" * 20, "HY2_STATS_PORT": "25000", "HY2_STATS_SECRET": "s" * 20})

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_gather(self):
        clients = [{"email": "owner", "traffic": {"up": 10, "down": 20, "lastOnline": 1_700_000_000_000}},
                   {"email": "masha", "traffic": {"up": 1, "down": 2}}, {"email": "ghost"}]
        inbounds = [{"id": 3, "port": 443, "up": 100, "down": 200}, {"id": 5, "port": 30001, "up": 5, "down": 6},
                    {"id": 9, "port": 41000, "up": 7, "down": 8}]
        with mock.patch("zoolib.traffic.XuiClient.clients", return_value=clients), \
                mock.patch("zoolib.traffic.XuiClient.inbounds", return_value=inbounds), \
                mock.patch("zoolib.traffic.hysteria_stats", return_value={"masha": {"tx": 3, "rx": 4}}) as hs, \
                mock.patch("zoolib.traffic._unit_epoch", return_value="123@x"), \
                mock.patch("zoolib.traffic._host_sample", return_value=None):
            g = traffic.gather(config.load())
        got = {(s.proto, s.user): (s.up, s.down) for s in g.samples}
        self.assertEqual(got[("xray", "owner")], (10, 20))
        self.assertEqual(got[("vless-reality", "")], (100, 200))
        self.assertEqual(got[("ss2022", "")], (5, 6))
        self.assertEqual(got[("vless-xhttp", "")], (7, 8))
        self.assertEqual(got[("hysteria2", "masha")], (3, 4))
        self.assertNotIn(("xray", "ghost"), got)
        self.assertEqual(g.sum_protos, {"hysteria2"})
        self.assertEqual(g.errors, {})
        hs.assert_called_once_with(25000, "s" * 20)
        seen = {s.user: s.seen for s in g.samples if s.proto == "xray"}
        self.assertEqual(seen["owner"], 1_700_000_000)

    def test_xui_error_is_reported_not_raised(self):
        from zoolib.xui import XuiError
        with mock.patch("zoolib.traffic.XuiClient.clients", side_effect=XuiError("панель недоступна")), \
                mock.patch("zoolib.traffic.hysteria_stats", side_effect=OSError("refused")), \
                mock.patch("zoolib.traffic._host_sample", return_value=None):
            g = traffic.gather(config.load())
        self.assertIn("3x-ui", g.errors)
        self.assertIn("hysteria2", g.errors)
        self.assertEqual(g.samples, [])

    def test_default_iface(self):
        d = self.env.root / "proc" / "net"
        d.mkdir(parents=True)
        (d / "route").write_text("Iface\tDestination\tGateway\n"
                                 "docker0\t000011AC\t00000000\neth0\t00000000\t0101A8C0\n", encoding="utf-8")
        self.assertEqual(traffic.default_iface(self.env.root / "proc"), "eth0")


@needs_bash
class CollectTest(unittest.TestCase):
    """Сквозной collect: протокол через фейковый модуль proto-<id>.sh."""

    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.env.add_protocol("amneziawg", users=("owner", "masha"), engine="amneziawg-go",
                              users_backend="awg", layer="udp")

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_collect_and_cli(self):
        with mock.patch("zoolib.traffic._host_sample", return_value=None):
            r1 = traffic.collect(config.load(), now=self.now_minus(600))
            r2 = traffic.collect(config.load(), now=self.now_minus(300))
        self.assertEqual(r1["errors"], {})
        self.assertEqual(r1["up"], 0)  # первый запуск — только база
        self.assertEqual(r2["up"], 0)  # фейк отдаёт постоянные 10/20
        self.assertEqual(traffic.last_run()["series"], 2)
        from zoolib import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["traffic", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("last_run", json.loads(out.getvalue()))

    @staticmethod
    def now_minus(sec):
        return int(time.time()) - sec


if __name__ == "__main__":
    unittest.main()
