"""Журнал атак (zoolib.journal): разбор строк, запись в SQLite, отчёты, оценка «свой/локальный», страна, страница.

Строки sshd, fail2ban, 3x-ui, Xray, zoo-web и помощника Hysteria сняты с Docker-стенда (сервер 24.04,
клиентский контейнер); строки ufw — по формату ядра: LOG из сети контейнера стенда не доходит до журнала.
"""

import contextlib
import io
import ipaddress
import json
import os
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.helpers import ZooEnv
from tests.test_web import TOKEN, AppTestBase, visible_words
from zoolib import cli, config, geoip, journal, traffic
from zoolib.journal import Event

NOW = 1_791_151_800  # 2026-10-04 22:10 UTC
A, B, C = "45.155.205.10", "91.240.118.5", "185.220.101.5"  # внешние адреса из публичных диапазонов

UFW = ("[UFW BLOCK] IN=eth0 OUT= MAC=02:42:ac:16:00:02:02:42:ac:16:00:04:08:00 SRC={src} DST=172.22.0.2 LEN=44 "
       "TOS=0x00 PREC=0x00 TTL=64 ID=1234 PROTO={proto} SPT=40000 DPT={dpt} {tail}")
UFW_TCP = " WINDOW=1024 RES=0x00 SYN URGP=0"


def ufw(src=A, dpt=3389, proto="TCP", iface="eth0", tail=UFW_TCP, tag="[UFW BLOCK]"):
    msg = UFW.format(src=src, dpt=dpt, proto=proto, tail=tail if proto == "TCP" else "LEN=8").replace("[UFW BLOCK]", tag)
    return msg.replace("IN=eth0", f"IN={iface}")


def jrow(msg, ts=NOW, cursor="c1"):
    return json.dumps({"MESSAGE": msg, "__REALTIME_TIMESTAMP": str(ts * 1_000_000), "__CURSOR": cursor})


class FakeJournal:
    """Подмена journalctl: на match-аргументы отдаёт заготовленные строки; запоминает вызовы."""

    def __init__(self, data=None):
        self.data = data or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        for key, rows in self.data.items():
            if key in args:
                if "--after-cursor" in args:
                    return 0, "", ""
                return 0, "\n".join(rows) + ("\n" if rows else ""), ""
        return 0, "", ""


class ParseSshTest(unittest.TestCase):
    def kind(self, msg):
        ev = journal.parse_ssh(msg, NOW)
        return (ev.kind, ev.ip) if ev else None

    def test_failed_auth_lines(self):
        for msg in ("Invalid user admin from 172.22.0.3 port 55432",
                    "Failed password for root from 172.22.0.3 port 57178 ssh2",
                    "Failed password for invalid user admin from 172.22.0.3 port 55432 ssh2",
                    "Connection closed by authenticating user root 172.22.0.3 port 57178 [preauth]",
                    "Connection closed by invalid user admin 172.22.0.3 port 55432 [preauth]",
                    "Disconnected from authenticating user root 172.22.0.3 port 1 [preauth]",
                    "error: maximum authentication attempts exceeded for root from 172.22.0.3 port 9 ssh2 [preauth]"):
            self.assertEqual(self.kind(msg), ("ssh-auth", "172.22.0.3"), msg)

    def test_scanner_lines(self):
        for msg in ("banner exchange: Connection from 172.22.0.4 port 45276: invalid format",
                    "Connection reset by 172.22.0.4 port 50896",
                    "ssh_dispatch_run_fatal: Connection from 172.22.0.4 port 36220: Broken pipe [preauth]",
                    "Unable to negotiate with 172.22.0.4 port 36230: no matching key exchange method found. "
                    "Their offer: diffie-hellman-group1-sha1,ext-info-c [preauth]",
                    "Did not receive identification string from 172.22.0.4 port 1"):
            self.assertEqual(self.kind(msg), ("ssh-scan", "172.22.0.4"), msg)

    def test_own_login_and_noise(self):
        self.assertEqual(self.kind("Accepted publickey for root from 172.22.0.4 port 56472 ssh2: ED25519 SHA256:abc"),
                         (journal.OWN_LOGIN, "172.22.0.4"))
        # пароль не делает адрес «своим»: это мог быть взломщик
        self.assertIsNone(self.kind("Accepted password for root from 172.22.0.4 port 56472 ssh2"))
        for msg in ("Server listening on 0.0.0.0 port 22.",
                    "pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=172.22.0.3  user=root",
                    "Received disconnect from 172.22.0.4 port 56472:11: disconnected by user",
                    "Disconnected from user root 172.22.0.4 port 56472",
                    "error: kex_exchange_identification: read: Connection reset by peer"):
            self.assertIsNone(self.kind(msg), msg)

    def test_ipv6_and_mapped(self):
        self.assertEqual(self.kind("Invalid user a from 2001:db8::5 port 22"), ("ssh-auth", "2001:db8::5"))
        self.assertEqual(self.kind("Invalid user a from ::ffff:1.2.3.4 port 22"), ("ssh-auth", "1.2.3.4"))

    def test_garbage_ip_is_dropped(self):
        self.assertIsNone(self.kind("Invalid user a from not-an-ip port 22"))
        self.assertIsNone(self.kind("Invalid user a from <script> port 22"))


class ParseOthersTest(unittest.TestCase):
    wan = {"eth0"}

    def test_ufw_block(self):
        ev = journal.parse_ufw(ufw(), NOW, self.wan)
        self.assertEqual((ev.kind, ev.ip, ev.port), ("port-scan", A, 3389))
        ev = journal.parse_ufw(ufw(dpt=53, proto="UDP"), NOW, self.wan)
        self.assertEqual((ev.kind, ev.port), ("port-scan", 53))
        ev = journal.parse_ufw(ufw(dpt=22, tag="[UFW LIMIT BLOCK]"), NOW, self.wan)
        self.assertEqual((ev.kind, ev.port), ("ssh-limit", 22))

    def test_ufw_vpn_traffic_is_never_recorded(self):
        # пакеты пользователей VPN (интерфейсы туннелей) — не «атака»; это правило приватности
        for iface in ("awg0", "tun0", "wg0", "lo", "docker0"):
            self.assertIsNone(journal.parse_ufw(ufw(iface=iface), NOW, self.wan), iface)
        self.assertIsNone(journal.parse_ufw(ufw(), NOW, set()))
        self.assertIsNone(journal.parse_ufw(ufw().replace("OUT=", "OUT=eth1"), NOW, self.wan), "пересылка")

    def test_ufw_tcp_without_syn_is_backscatter(self):
        for tail in (" WINDOW=0 RES=0x00 ACK RST URGP=0", " WINDOW=1024 RES=0x00 ACK SYN URGP=0",
                     " WINDOW=1024 RES=0x00 ACK FIN URGP=0"):
            self.assertIsNone(journal.parse_ufw(ufw(tail=tail), NOW, self.wan), tail)

    def test_ufw_late_udp_reply_is_not_an_attack(self):
        # ответ сайта на исходящий UDP пользователя после истечения conntrack: SRC — назначение пользователя
        msg = ufw(dpt=51234, proto="UDP").replace("SPT=40000", "SPT=443")
        self.assertIsNone(journal.parse_ufw(msg, NOW, self.wan))
        self.assertIsNone(journal.parse_ufw(ufw(dpt=32768, proto="UDP"), NOW, self.wan))
        self.assertEqual(journal.parse_ufw(ufw(dpt=32767, proto="UDP"), NOW, self.wan).port, 32767)
        self.assertEqual(journal.parse_ufw(ufw(dpt=51234), NOW, self.wan).port, 51234, "TCP SYN — всегда попытка")

    def test_ufw_other_lines(self):
        self.assertIsNone(journal.parse_ufw("veth1234: renamed from eth0", NOW, self.wan))
        self.assertIsNone(journal.parse_ufw(ufw(proto="ICMP"), NOW, self.wan))
        self.assertIsNone(journal.parse_ufw(ufw().replace("DST=172.22.0.2", "DST=224.0.0.1"), NOW, self.wan))
        self.assertIsNone(journal.parse_ufw("[UFW AUDIT] IN=eth0 SRC=1.2.3.4 PROTO=TCP DPT=1", NOW, self.wan))

    def test_fail2ban(self):
        line = "2026-10-04 21:57:01,876 fail2ban.actions        [2597]: NOTICE  [sshd] Ban 172.22.0.3"
        ev = journal.parse_f2b(line)
        self.assertEqual((ev.kind, ev.ip), ("ssh-ban", "172.22.0.3"))
        self.assertEqual(ev.ts, int(time.mktime(time.strptime("2026-10-04 21:57:01", "%Y-%m-%d %H:%M:%S"))))
        for bad in ("2026-10-04 21:57:01,126 fail2ban.filter         [2597]: INFO    [sshd] Found 172.22.0.3 - 2026-10-04 21:57:00",
                    "2026-10-04 22:00:01,000 fail2ban.actions        [2597]: NOTICE  [sshd] Unban 172.22.0.3",
                    "2026-10-04 22:00:01,000 fail2ban.actions        [2597]: NOTICE  [sshd] Restore Ban 172.22.0.3",
                    "2026-10-04 22:00:01,000 fail2ban.actions        [2597]: NOTICE  [sshd] Ban not-an-ip"):
            self.assertIsNone(journal.parse_f2b(bad), bad)

    def test_hysteria_helper_line(self):
        ev = journal.parse_hy2("reject 172.22.0.3:44546", NOW)
        self.assertEqual((ev.kind, ev.ip), ("hy2-auth", "172.22.0.3"))
        self.assertEqual(journal.parse_hy2("reject [2001:db8::1]:99", NOW).ip, "2001:db8::1")
        self.assertIsNone(journal.parse_hy2("reject ?", NOW))
        self.assertIsNone(journal.parse_hy2("client connected {addr: 1.2.3.4:5, id: owner}", NOW))

    def test_xray_reality_line(self):
        msg = ("2026/10/04 22:01:41.806069 [Info] transport/internet/tcp: REALITY: processed invalid connection "
               "from 172.22.0.3:35574: server name mismatch: ")
        ev = journal.parse_xray(msg, NOW)
        self.assertEqual((ev.kind, ev.ip), ("reality-probe", "172.22.0.3"))
        self.assertIsNone(journal.parse_xray("REALITY remoteAddr: 172.22.0.3:35574", NOW))

    def test_panel_logins(self):
        ev = journal.parse_xui('WARNING - failed login: username="nouser", IP="127.0.0.1", reason="invalid credentials"',
                               NOW)
        self.assertEqual((ev.kind, ev.ip), ("panel-login", "127.0.0.1"))
        self.assertNotIn("nouser", repr(ev))
        self.assertEqual(journal.parse_web("zoo-web: POST /login 401", NOW).kind, "web-login")
        self.assertEqual(journal.parse_web("zoo-web: POST /login 429", NOW).kind, "web-login")
        self.assertIsNone(journal.parse_web("zoo-web: POST /login 303", NOW))
        self.assertIsNone(journal.parse_web("zoo-web: GET /login 200", NOW))


class ScopeTest(unittest.TestCase):
    def test_scopes(self):
        nets = [ipaddress.ip_network("198.18.0.0/15")]
        own = {A}
        self.assertEqual(journal.scope_of(A, own, nets), "own")
        self.assertEqual(journal.scope_of(B, own, nets), "public")
        self.assertEqual(journal.scope_of("198.18.5.5", own, nets), "own", "сеть из journal-ignore.txt")
        for ip in ("127.0.0.1", "10.1.2.3", "172.22.0.3", "192.168.1.1", "100.64.0.9", "fe80::1", "::1"):
            self.assertEqual(journal.scope_of(ip, own, nets), "local", ip)
        # свой адрес важнее «локального»: владелец ходит и из локальной сети
        self.assertEqual(journal.scope_of("172.22.0.4", {"172.22.0.4"}, nets), "own")

    def test_ignore_file(self):
        with ZooEnv() as env:
            (env.etc / "journal-ignore.txt").write_text(
                "# мой офис\n203.0.113.0/24  # NAT\n198.51.100.7\nмусор\n2001:db8::/32\n", encoding="utf-8")
            nets = journal.load_ignore()
            self.assertEqual([str(n) for n in nets], ["203.0.113.0/24", "198.51.100.7/32", "2001:db8::/32"])
            self.assertEqual(journal.scope_of("203.0.113.9", set(), nets), "own")
            self.assertEqual(journal.scope_of("198.51.100.8", set(), nets), "local")  # документационный, не global
            self.assertEqual(journal.load_ignore(env.etc / "нет-такого"), [])


class AggregateTest(unittest.TestCase):
    def test_hour_and_day_rows(self):
        evs = [Event(NOW, "port-scan", A, 3389), Event(NOW + 5, "port-scan", A, 3389), Event(NOW, "ssh-auth", A, 22)]
        rows = journal.aggregate(evs)
        hour, day = traffic.align(NOW, 3600), traffic.align(NOW, 86400)
        self.assertEqual(rows[(3600, hour, "ufw", "port-scan", A, 3389)], 2)
        self.assertEqual(rows[(86400, day, "ufw", "port-scan", A, 3389)], 2)
        self.assertEqual(rows[(3600, hour, "ssh", "ssh-auth", A, 22)], 1)
        self.assertEqual(sum(rows.values()), 6)

    def test_port_cap_folds_into_port_zero(self):
        evs = [Event(NOW, "port-scan", A, p) for p in range(1, 1001)]
        rows = journal.aggregate(evs)
        hour = traffic.align(NOW, 3600)
        ports = {k[5] for k in rows if k[0] == 3600}
        self.assertEqual(len(ports), journal.PORT_CAP + 1)
        self.assertEqual(rows[(3600, hour, "ufw", "port-scan", A, 0)], 1000 - journal.PORT_CAP)
        self.assertEqual(sum(n for k, n in rows.items() if k[0] == 3600), 1000, "события не теряются")
        # другой адрес получает свои 32 порта
        rows = journal.aggregate(evs + [Event(NOW, "port-scan", B, 5)])
        self.assertIn((3600, hour, "ufw", "port-scan", B, 5), rows)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.env.write_config({"SERVER_IP": "10.0.0.1", "SSH_PORTS": "2222", "HY2_PORT": "443"})
        for p in (mock.patch("zoolib.journal.wan_ifaces", return_value={"eth0"}),
                  mock.patch("zoolib.journal.F2B_LOG", self.env.root / "fail2ban.log")):
            p.start()
            self.addCleanup(p.stop)

    def run_collect(self, data, now=NOW + 60):
        fj = FakeJournal(data)
        with mock.patch("zoolib.journal.journalctl", fj):
            res = journal.collect(config.load(), now=now)
        return res, fj

    def sql(self, q, *a):
        con = journal.connect()
        try:
            return [tuple(r) for r in con.execute(q, a)]
        finally:
            con.close()

    def test_collect_all_sources_into_db(self):
        data = {
            "_TRANSPORT=kernel": [jrow(ufw(A, 3389)), jrow(ufw(A, 3390)), jrow(ufw(B, 22, tag="[UFW LIMIT BLOCK]")),
                                  jrow(ufw(C, 80, iface="awg0")), jrow("usb 1-1: new device")],
            "ssh.service": [jrow("Invalid user admin from %s port 1111" % A), jrow("Failed password for invalid user admin from %s port 1111 ssh2" % A),
                            jrow("Connection closed by invalid user admin %s port 1111 [preauth]" % A),
                            jrow("Invalid user root from %s port 2222" % A),
                            jrow("banner exchange: Connection from %s port 3333: invalid format" % B)],
            "SYSLOG_IDENTIFIER=zoo-hy2-auth": [jrow("reject %s:5555" % C)],
            "x-ui.service": [jrow('WARNING - failed login: username="secretuser", IP="127.0.0.1", reason="invalid credentials"')],
            "zoo-web.service": [jrow("zoo-web: POST /login 401"), jrow("zoo-web: GET / 200")],
        }
        (self.env.root / "fail2ban.log").write_text(
            "2026-10-04 21:57:01,876 fail2ban.actions        [2597]: NOTICE  [sshd] Ban %s\n"
            "2026-10-04 21:57:02,000 fail2ban.filter         [2597]: INFO    [sshd] Found %s - x\n" % (A, A),
            encoding="utf-8")
        res, _ = self.run_collect(data)
        self.assertEqual(res["errors"], {})
        kinds = dict(self.sql("SELECT kind, SUM(n) FROM hits WHERE res = 3600 GROUP BY kind"))
        self.assertEqual(kinds, {"port-scan": 2, "ssh-limit": 1, "ssh-auth": 2, "ssh-scan": 1, "ssh-ban": 1,
                                 "hy2-auth": 1, "panel-login": 1, "web-login": 1})
        self.assertEqual(res["events"], 10)
        # VPN-интерфейс отброшен: адрес C виден только как Hysteria
        self.assertEqual(self.sql("SELECT kind FROM hits WHERE res = 3600 AND ip = ?", C), [("hy2-auth",)])
        # порты назначения: у sshd и Hysteria берутся из config.env
        self.assertEqual(self.sql("SELECT port FROM hits WHERE kind = 'ssh-auth' AND res = 3600"), [(2222,)])
        self.assertEqual(self.sql("SELECT port FROM hits WHERE kind = 'hy2-auth' AND res = 3600"), [(443,)])
        # в базе нет ни строк журнала, ни имён
        con = journal.connect()
        dump = "\n".join(con.iterdump())
        con.close()
        for secret in ("admin", "Invalid user", "secretuser", "invalid credentials", "failed login"):
            self.assertNotIn(secret, dump)
        self.assertEqual(self.sql("SELECT ok FROM runs"), [(1,)])

    def test_ssh_connection_counts_once(self):
        data = {"ssh.service": [jrow("Invalid user a from %s port 1111" % A), jrow("Failed password for invalid user a from %s port 1111 ssh2" % A),
                                jrow("Invalid user a from %s port 1112" % A)]}
        self.run_collect(data)
        self.assertEqual(self.sql("SELECT SUM(n) FROM hits WHERE kind = 'ssh-auth' AND res = 3600"), [(2,)])

    def test_cursor_is_used_on_next_run(self):
        data = {"ssh.service": [jrow("Invalid user a from %s port 1" % A, cursor="cur-1"),
                                jrow("Invalid user b from %s port 2" % A, cursor="cur-2")]}
        _, fj = self.run_collect(data)
        first = [c for c in fj.calls if "ssh.service" in c][0]
        self.assertIn("--since", first)
        _, fj = self.run_collect(data, now=NOW + 400)
        second = [c for c in fj.calls if "ssh.service" in c][0]
        self.assertEqual(second[second.index("--after-cursor") + 1], "cur-2")
        self.assertEqual(self.sql("SELECT SUM(n) FROM hits WHERE kind = 'ssh-auth' AND res = 3600"), [(2,)])

    def test_lost_cursor_falls_back_to_last_hour(self):
        data = {"ssh.service": [jrow("Invalid user a from %s port 1" % A, cursor="cur-1")]}
        self.run_collect(data)
        calls = []

        def fake(args):
            calls.append(args)
            if "--after-cursor" in args:
                return 1, "", "Failed to seek to cursor"
            return 0, jrow("Invalid user z from %s port 9" % B, cursor="cur-9") + "\n", ""
        with mock.patch("zoolib.journal.journalctl", fake):
            journal.collect(config.load(), now=NOW + 400)
        self.assertTrue(any("-1h" in c for c in calls))

    def test_journal_error_is_reported_not_raised(self):
        with mock.patch("zoolib.journal.journalctl", return_value=(1, "", "Failed to open journal")):
            res = journal.collect(config.load(), now=NOW)
        self.assertIn("ssh", res["errors"])
        self.assertEqual(self.sql("SELECT ok FROM runs"), [(0,)])
        with mock.patch("zoolib.journal.journalctl", return_value=(127, "", "нет команды journalctl")):
            res = journal.collect(config.load(), now=NOW + 1)
        self.assertEqual(res["errors"]["ssh"], "нет journalctl")

    def test_no_wan_interface_skips_ufw_with_message(self):
        with mock.patch("zoolib.journal.wan_ifaces", return_value=set()):
            res, fj = self.run_collect({"_TRANSPORT=kernel": [jrow(ufw())]})
        self.assertIn("ufw", res["errors"])
        self.assertFalse(any("_TRANSPORT=kernel" in c for c in fj.calls))

    def test_cap_marks_error_and_keeps_going(self):
        rows = [jrow("Invalid user a from %s port %d" % (A, i), cursor=f"c{i}") for i in range(1, 30)]
        with mock.patch("zoolib.journal.MAX_LINES", 10):
            res, _ = self.run_collect({"ssh.service": rows})
        self.assertIn("больше 10", res["errors"]["ssh"])
        self.assertEqual(self.sql("SELECT SUM(n) FROM hits WHERE kind = 'ssh-auth' AND res = 3600"), [(10,)])
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'cursor:ssh'"), [("c10",)])

    def test_lag_over_limit_drops_backlog(self):
        rows = [jrow("Invalid user a from %s port %d" % (A, i), ts=NOW - 8 * 3600, cursor=f"c{i}") for i in range(1, 30)]
        with mock.patch("zoolib.journal.MAX_LINES", 10):
            res, _ = self.run_collect({"ssh.service": rows})
        self.assertIn("пропущено", res["errors"]["ssh"])
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'cursor:ssh'"), [])
        # следующий запуск — с последнего часа, а не снова с первых суток (иначе то же самое посчиталось бы дважды)
        _, fj = self.run_collect({"ssh.service": [jrow("Invalid user z from %s port 9" % B, cursor="c99")]},
                                 now=NOW + 400)
        call = [c for c in fj.calls if "ssh.service" in c][0]
        self.assertEqual(call[call.index("--since") + 1], "@%d" % (NOW + 60 - 3600))
        _, fj = self.run_collect({"ssh.service": []}, now=NOW + 700)
        call = [c for c in fj.calls if "ssh.service" in c][0]
        self.assertEqual(call[call.index("--after-cursor") + 1], "c99")

    def test_journalctl_output_is_bounded(self):
        with mock.patch("zoolib.system.run", return_value=(0, "", "")) as run:
            journal.journalctl(["--since", "-3d", "-u", "ssh.service"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("-n") + 1], str(journal.MAX_LINES + 1))
        self.assertIn("--output-fields=MESSAGE", argv)

    def test_fail2ban_log_incremental_and_rotation(self):
        log = self.env.root / "fail2ban.log"
        ban = "2026-10-04 21:57:01,876 fail2ban.actions        [1]: NOTICE  [sshd] Ban %s\n"
        log.write_text(ban % A, encoding="utf-8")
        con = journal.connect()
        evs, n, err = journal.read_f2b(con, log)
        self.assertEqual([e.ip for e in evs], [A])
        evs, n, err = journal.read_f2b(con, log)
        self.assertEqual(evs, [], "уже прочитанное повторно не берётся")
        with open(log, "a", encoding="utf-8") as f:
            f.write(ban % B)
            f.write("2026-10-04 21:59:00,000 fail2ban.actions        [1]: NOTICE  [sshd] Ban " + C)  # без \n: недописана
        evs, _, _ = journal.read_f2b(con, log)
        self.assertEqual([e.ip for e in evs], [B])
        with open(log, "a", encoding="utf-8") as f:
            f.write("\n")
        evs, _, _ = journal.read_f2b(con, log)
        self.assertEqual([e.ip for e in evs], [C], "недописанная строка доехала в следующий раз")
        log.write_text(ban % A, encoding="utf-8")  # ротация: файл стал короче
        log.unlink()
        log.write_text(ban % B, encoding="utf-8")
        evs, _, _ = journal.read_f2b(con, log)
        self.assertEqual([e.ip for e in evs], [B])
        con.close()
        con = journal.connect()
        self.assertEqual(journal.read_f2b(con, self.env.root / "нет.log"), ([], 0, ""))
        con.close()

    def test_retention(self):
        old = NOW - 100 * 86400
        con = journal.connect()
        with con:
            journal.store(con, [Event(old, "port-scan", A, 1), Event(NOW - 10 * 86400, "port-scan", B, 2),
                                Event(NOW, "port-scan", C, 3)], NOW)
            con.execute("INSERT INTO own VALUES (?, ?)", ("1.1.1.1", old))
            con.execute("INSERT INTO own VALUES (?, ?)", ("2.2.2.2", NOW - 40 * 86400))
            con.execute("INSERT INTO own VALUES (?, ?)", ("3.3.3.3", NOW - 10 * 86400))
            journal.prune(con, NOW)
        rows = [tuple(r) for r in con.execute("SELECT res, ip FROM hits ORDER BY res, ip")]
        con.close()
        # часовые — 8 суток, суточные — 90: адрес B остался только в суточных, A исчез совсем
        self.assertEqual(rows, [(3600, C), (86400, C), (86400, B)])
        self.assertEqual(self.sql("SELECT ip FROM ips ORDER BY ip"), [(C,), (B,)])
        self.assertEqual(self.sql("SELECT ip FROM own"), [("3.3.3.3",)], "свой адрес забывается через 30 дней")
        with mock.patch.dict("os.environ", {"ZOO_JOURNAL_KEEP_DAYS": "400"}):
            self.assertEqual(journal.keep_days(), 365)
        with mock.patch.dict("os.environ", {"ZOO_JOURNAL_KEEP_DAYS": "x"}):
            self.assertEqual(journal.keep_days(), 90)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        con = journal.connect()
        with con:
            ev = [Event(NOW - 600, "port-scan", A, 3389)] * 5 + [Event(NOW - 500, "port-scan", A, 22)] * 3
            ev += [Event(NOW - 300, "ssh-auth", A, 22)] * 4 + [Event(NOW - 200, "ssh-ban", A, 0)]
            ev += [Event(NOW - 100, "hy2-auth", B, 443)] * 2 + [Event(NOW - 90, "port-scan", B, 3389)]
            ev += [Event(NOW - 80, "port-scan", "172.22.0.4", 81)] * 7        # контейнер стенда
            ev += [Event(NOW - 70, "ssh-auth", "203.0.113.50", 22)] * 2         # свой адрес (по входу ключом)
            ev += [Event(NOW - 60, journal.OWN_LOGIN, "203.0.113.50", 0)]
            journal.store(con, ev, NOW)
            con.execute("UPDATE ips SET cc = 'CN' WHERE ip = ?", (A,))
            con.execute("UPDATE ips SET cc = 'DE' WHERE ip = ?", (B,))
        con.close()

    def rep(self, **kw):
        return journal.report("24h", now=NOW, **kw)

    def test_public_only_by_default(self):
        d = self.rep()
        t = d["totals"]
        self.assertEqual((t["events"], t["ips"], t["bans"]), (15, 2, 1))
        self.assertEqual(d["hidden"], {"own": 2, "local": 7})
        self.assertEqual([i["ip"] for i in d["top_ips"]], [A, B])
        a = d["top_ips"][0]
        self.assertEqual((a["n"], a["cc"], a["scope"], a["bans"]), (12, "CN", "public", 1))
        self.assertEqual(a["ports"], [22, 3389])
        self.assertEqual(a["kinds"], {"port-scan": 8, "ssh-auth": 4})
        groups = {g["key"]: (g["n"], g["ips"]) for g in d["groups"]}
        self.assertEqual(groups, {"ports": (9, 2), "ssh": (4, 1), "proxy": (2, 1), "login": (0, 0)})
        ports = {p["port"]: p["n"] for p in d["top_ports"]}
        self.assertEqual(ports, {3389: 6, 22: 7, 443: 2})
        self.assertEqual({c["cc"]: c["n"] for c in d["countries"]}, {"CN": 12, "DE": 3})

    def test_include_local_marks_scopes(self):
        d = self.rep(include_local=True)
        by_ip = {i["ip"]: i["scope"] for i in d["top_ips"]}
        self.assertEqual(by_ip, {A: "public", B: "public", "172.22.0.4": "local", "203.0.113.50": "own"})
        self.assertEqual(d["totals"]["events"], 24)
        self.assertEqual([o["ip"] for o in d["own_ips"]], ["203.0.113.50"])

    def test_timeline_series_match_totals(self):
        d = self.rep()
        tl = d["timeline"]
        self.assertEqual(len(tl["buckets"]), 24)
        self.assertEqual(sum(sum(s["values"]) for s in tl["series"]), d["totals"]["events"])
        self.assertEqual({s["key"] for s in tl["series"]}, {"ports", "ssh", "proxy"})

    def test_longer_periods_read_day_rows(self):
        for p in ("7d", "30d", "90d"):
            d = journal.report(p, now=NOW)
            self.assertEqual(d["totals"]["events"], 15, p)
        with self.assertRaises(ValueError):
            journal.report("1h", now=NOW)
        with self.assertRaises(ValueError):
            journal.report("bogus", now=NOW)

    def test_empty_database(self):
        with ZooEnv():
            d = journal.report("24h", now=NOW)
            self.assertTrue(d["empty"])
            self.assertEqual(d["totals"]["events"], 0)
            self.assertEqual(journal.alerts(NOW), [])

    def test_spike_alert(self):
        # обычный фон: по 2 попытки в час за неделю; сейчас — 300 за час
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 3600 * h, "port-scan", C, 80) for h in range(2, 7 * 24)] * 2, NOW)
            journal.store(con, [Event(NOW - 120, "port-scan", C, 81)] * 300, NOW)
        con.close()
        al = journal.alerts(NOW)
        self.assertEqual(len(al), 1)
        self.assertEqual(al[0][0], "warn")
        self.assertIn("315", al[0][1])  # 300 новых + 15 из setUp в этом же часу

    def test_short_history_is_not_diluted_to_a_week(self):
        # первый разбор взял 3 суток по 30 в час; 130 за час — обычный шум, а не всплеск
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 3600 * h, "port-scan", C, 80) for h in range(1, 73)] * 30, NOW)
            journal.store(con, [Event(NOW - 120, "port-scan", C, 81)] * 115, NOW)
        con.close()
        self.assertEqual(journal.alerts(NOW), [])

    def test_no_alert_for_quiet_or_usual_noise(self):
        self.assertEqual(journal.alerts(NOW), [])
        con = journal.connect()
        with con:  # постоянный шум: 120 в час каждый час — всплеском не считается
            journal.store(con, [Event(NOW - 3600 * h - 60, "port-scan", C, 80) for h in range(0, 7 * 24)] * 120, NOW)
        con.close()
        self.assertEqual(journal.alerts(NOW), [])

    def test_local_noise_never_alerts(self):
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 60, "port-scan", "172.22.0.9", 80)] * 500, NOW)
        con.close()
        self.assertEqual(journal.alerts(NOW), [])


class CliTest(unittest.TestCase):
    def test_cli_json_and_text(self):
        with ZooEnv():
            con = journal.connect()
            with con:
                now = int(time.time())
                journal.store(con, [Event(now - 60, "port-scan", A, 3389)] * 3, now)
            con.close()
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("zoolib.journal.health", return_value=[]):
                self.assertEqual(cli.main(["journal", "--json", "--period", "24h"]), 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["totals"]["events"], 3)
            self.assertEqual(data["top_ips"][0]["ip"], A)
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("zoolib.journal.health", return_value=[]),                     mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(cli.main(["journal"]), 0)
            self.assertIn(A, buf.getvalue())
            self.assertIn("Стучались в закрытые порты", buf.getvalue())
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(cli.main(["journal", "--period", "1h"]), 2)

    def test_cli_without_db_warns(self):
        with ZooEnv():
            with mock.patch("sys.stderr", new=io.StringIO()) as err, redirect_stdout(io.StringIO()),                     mock.patch("zoolib.journal.health", return_value=[]):
                self.assertEqual(cli.main(["journal"]), 0)
            self.assertIn("журнала атак нет", err.getvalue())


def tiny_dat():
    """geoip.dat в формате v2ray на троих: A → CN, B → DE (вложенная сеть — RU), 2001:db8::/32 → US."""
    def varint(n):
        out = bytearray()
        while True:
            c = n & 0x7F
            n >>= 7
            out.append(c | 0x80 if n else c)
            if not n:
                return bytes(out)

    def field(num, payload, wt=2):
        return varint(num << 3 | wt) + (varint(len(payload)) + payload if wt == 2 else payload)

    def cidr(ip, pfx):
        return field(2, field(1, ip) + field(2, varint(pfx), 0))

    def entry(code, cidrs):
        return field(1, field(1, code.encode()) + b"".join(cidrs))

    n4 = lambda s: ipaddress.ip_address(s).packed
    return (entry("cn", [cidr(n4("45.155.0.0"), 16)]) + entry("de", [cidr(n4("91.240.0.0"), 16)])
            + entry("ru", [cidr(n4("91.240.118.0"), 24)]) + entry("us", [cidr(n4("2001:db8::"), 32)])
            + entry("private", [cidr(n4("10.0.0.0"), 8)]) + entry("cloudflare", [cidr(n4("45.155.205.0"), 24)]))


class GeoTest(unittest.TestCase):
    def test_index_lookup(self):
        with ZooEnv() as env:
            dat, idx_path = env.root / "geoip.dat", env.root / "geoip.bin"
            dat.write_bytes(tiny_dat())
            idx = geoip.open_index(dat, idx_path)
            self.assertEqual(idx.country("45.155.205.10"), "CN", "не-страны (cloudflare) пропускаются")
            self.assertEqual(idx.country("91.240.5.5"), "DE")
            self.assertEqual(idx.country("91.240.118.5"), "RU", "самая узкая сеть побеждает")
            self.assertEqual(idx.country("2001:db8::7"), "US")
            self.assertEqual(idx.country("::ffff:45.155.1.1"), "CN")
            for ip in ("8.8.8.8", "10.1.1.1", "bogus", "2a00::1"):
                self.assertEqual(idx.country(ip), "", ip)

    def test_index_rebuilt_when_dat_changes(self):
        with ZooEnv() as env:
            dat, idx_path = env.root / "geoip.dat", env.root / "geoip.bin"
            dat.write_bytes(tiny_dat())
            geoip.open_index(dat, idx_path)
            self.assertEqual(geoip.GeoIndex(idx_path).src_mtime, dat.stat().st_mtime_ns)
            with mock.patch("zoolib.geoip.build_index", side_effect=AssertionError("пересборка")):
                geoip.open_index(dat, idx_path)  # тот же .dat — индекс не пересобирается
            os.utime(dat, ns=(5_000_000_000, 5_000_000_000))
            geoip.open_index(dat, idx_path)
            self.assertEqual(geoip.GeoIndex(idx_path).src_mtime, dat.stat().st_mtime_ns)
            self.assertEqual(dat.stat().st_mtime_ns, 5_000_000_000)

    def test_broken_inputs(self):
        with ZooEnv() as env:
            self.assertIsNone(geoip.open_index(env.root / "нет.dat", env.root / "i.bin"))
            bad = env.root / "bad.dat"
            bad.write_bytes(b"\x0a\xff\xff")
            self.assertIsNone(geoip.open_index(bad, env.root / "i.bin"))
            self.assertFalse((env.root / "i.bin").exists())

    def test_enrich_fills_country_once(self):
        with ZooEnv() as env:
            dat = env.root / "geoip.dat"
            dat.write_bytes(tiny_dat())
            con = journal.connect()
            with con:
                journal.store(con, [Event(NOW, "port-scan", A, 1), Event(NOW, "port-scan", "10.1.1.1", 1)], NOW)
                with mock.patch("zoolib.geoip.candidates", return_value=[dat]):
                    self.assertEqual(journal.enrich(con), "")
                self.assertEqual({r[0]: r[1] for r in con.execute("SELECT ip, cc FROM ips")},
                                 {A: "CN", "10.1.1.1": ""})
            con.close()

    def test_enrich_without_dat_leaves_unknown(self):
        with ZooEnv():
            con = journal.connect()
            with con:
                journal.store(con, [Event(NOW, "port-scan", A, 1)], NOW)
                with mock.patch("zoolib.geoip.candidates", return_value=[Path("/нет/geoip.dat")]):
                    self.assertEqual(journal.enrich(con), "")
                self.assertEqual(list(con.execute("SELECT cc FROM ips"))[0][0], None)
            con.close()


class JournalPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.c.login()

    def seed(self):
        now = int(time.time())
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 60, "port-scan", A, 3389)] * 4 + [Event(now - 50, "ssh-auth", B, 22)] * 2
                          + [Event(now - 40, "port-scan", "172.22.0.4", 81)] * 3
                          + [Event(now - 30, "hy2-auth", C, 443)], now)
            con.execute("UPDATE ips SET cc = 'CN' WHERE ip = ?", (A,))
        con.close()

    def test_empty_page(self):
        resp, body = self.c.get("/journal")
        self.assertEqual(resp.status, 200)
        self.assertIn("Нет данных · сбор каждые 5 мин", body)
        self.assertIn('href="/journal"', body, "пункт меню")

    def test_page_with_data(self):
        self.seed()
        resp, body = self.c.get("/journal?period=24h")
        self.assertEqual(resp.status, 200)
        for text in (A, B, C, "CN", "Стучались в закрытые порты", "Перебор SSH", "Проверяли REALITY и Hysteria2",
                     "Чего мы не видим", "3389"):
            self.assertIn(text, body)
        self.assertNotIn("172.22.0.4", body, "локальные скрыты по умолчанию")
        self.assertIn("скрыто: локальных 3", body)
        resp, body = self.c.get("/journal?period=7d&all=1")
        self.assertIn("172.22.0.4", body)
        self.assertIn("локальный", body)

    def test_page_is_short_with_verdict(self):
        self.seed()
        _, body = self.c.get("/journal?period=24h")
        self.assertLessEqual(visible_words(body), 200)
        self.assertIn("Щупают прокси и панели: REALITY/Hy2 — 1 (адресов: 1)", body)
        self.assertIn("тихо: Входы в панели", body)
        self.assertNotIn("Чего мы не видим</strong>", body.split("<details")[0])
        self.assertIn("Чего мы не видим", body)  # в «?» у таблицы источников
        self.assertNotIn(">попыток<", body.split("<table")[0].split("Источники")[0])

    def test_quiet_verdict_and_port_cap(self):
        now = int(time.time())
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 60, "port-scan", A, p) for p in (80, 81, 82, 83)]
                          + [Event(now - 50, "ssh-auth", B, 22)], now)
        con.close()
        _, body = self.c.get("/journal?period=24h")
        self.assertIn("Обычный фон: сканеры и перебор SSH", body)
        self.assertIn('<span title="80, 81, 82, 83">80, 81 +2</span>', body)
        self.assertIn("Источник", body)

    def test_page_cached_until_next_run(self):
        self.seed()
        resp, first = self.c.get("/journal?period=24h")
        with mock.patch.object(journal, "report", side_effect=AssertionError("из кэша")),                 contextlib.redirect_stderr(io.StringIO()):
            _, again = self.c.get("/journal?period=24h")
            self.assertEqual(self.c.get("/journal?period=7d")[0].status, 500, "другой период — новый расчёт")
        self.assertEqual(first, again)

    def test_bad_period_falls_back(self):
        self.seed()
        for q in ("period=1h", "period=bogus", "period=", "all=1&period=%3Cscript%3E"):
            resp, body = self.c.get("/journal?" + q)
            self.assertEqual(resp.status, 200, q)
            self.assertNotIn("<script>", body)

    def test_overview_shows_spike(self):
        now = int(time.time())
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 30, "port-scan", A, p % 200 + 1) for p in range(400)], now)
        con.close()
        self.app.invalidate()  # тревоги журнала кэшируются на минуту: вход уже открыл обзор
        resp, body = self.c.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("Журнал атак: за этот час 400", body)

    def test_page_requires_login(self):
        self.c.cookies.clear()
        resp, _ = self.c.get("/journal")
        self.assertEqual(resp.status, 303)


if __name__ == "__main__":
    unittest.main()
