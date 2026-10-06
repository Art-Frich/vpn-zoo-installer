"""Журнал атак (zoolib.journal): разбор строк, запись в SQLite, отчёты, оценка «свой/локальный», страна, страница.

Строки sshd, fail2ban, 3x-ui, Xray, zoo-web и помощника Hysteria сняты с Docker-стенда (сервер 24.04,
клиентский контейнер); строки ufw — по формату ядра: LOG из сети контейнера стенда не доходит до журнала.
"""

import contextlib
import io
import ipaddress
import json
import os
import re
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


class QueryParseTest(unittest.TestCase):
    def test_all_kinds_of_terms(self):
        q = journal.parse_query("185.220. cc:nl :22 port:443 ssh hy2 ssh-auth 1.2.3.4 2001:DB8::1")
        self.assertEqual(q.prefixes, ["185.220."])
        self.assertEqual(q.ccs, ["NL"])
        self.assertEqual(q.ports, [22, 443])
        self.assertEqual(q.services, ["ssh", "hysteria"])
        self.assertEqual(q.kinds, ["ssh-auth"])
        self.assertEqual(q.exact, ["1.2.3.4", "2001:db8::1"])
        self.assertEqual(q.bad, [])

    def test_aliases_and_numeric_prefix(self):
        q = journal.parse_query("f2b reality x-ui web 185")
        self.assertEqual(q.services, ["fail2ban", "xray", "3x-ui", "zoo-web"])
        self.assertEqual(q.prefixes, ["185"])

    def test_garbage_goes_to_bad_not_to_sql(self):
        q = journal.parse_query("cc:NLD :99999 :0 beef drop;table 'or'1'='1 %_ \"x")
        self.assertTrue(q.empty)
        self.assertEqual(len(q.bad), 8)
        self.assertTrue(journal.parse_query("   ").empty)

    def test_limits(self):
        q = journal.parse_query(" ".join(["1.1.1.1"] * 50))
        self.assertEqual(len(q.exact), journal.TOKENS_MAX)
        self.assertEqual(journal.parse_query("1.2.3.4 " * 100).exact, ["1.2.3.4"] * journal.TOKENS_MAX)


class KindHelpTest(unittest.TestCase):
    def test_every_kind_has_four_lines_and_nothing_extra(self):
        self.assertEqual(set(journal.KIND_HELP), set(journal.KINDS))
        self.assertEqual(len(journal.HELP_LABELS), 4)
        for kind, lines in journal.KIND_HELP.items():
            self.assertEqual(len(lines), 4, kind)
            self.assertTrue(all(isinstance(x, str) and len(x) > 2 for x in lines), kind)

    def test_facts_match_code(self):
        # справка не должна обещать больше, чем делает установщик: пароли выключает только SSH_HARDEN, limit — не ставится
        repo = Path(__file__).resolve().parents[2]
        fw = (repo / "scripts" / "01-firewall.sh").read_text(encoding="utf-8")
        self.assertNotIn("ufw limit", "\n".join(ln for ln in fw.splitlines() if not ln.lstrip().startswith("#")).replace(
            'grep -qE "ufw (allow|limit)', ""))
        self.assertIn("SSH_HARDEN=1", journal.KIND_HELP["ssh-auth"][1])
        self.assertIn("D37", journal.KIND_HELP["ssh-limit"][2])
        self.assertIn("maxretry = 5", fw)
        self.assertIn("bantime  = 1h", fw)
        self.assertIn("bantime  = 1w", fw)
        # banaction = ufw блокирует адрес на все порты (а не порт jail'а): справка не говорит «SSH на час»
        self.assertIn("banaction = ufw", fw)
        what, _, defence, _ = journal.KIND_HELP["ssh-ban"]
        self.assertIn("все порты на час", what)
        self.assertNotIn("SSH на час", what)
        self.assertIn("все порты", defence)
        self.assertIn("127.0.0.1", journal.KIND_HELP["panel-login"][1])
        self.assertIn("127.0.0.1", journal.KIND_HELP["web-login"][1])


def seed_many(n_ips=40, now=NOW):
    """Адреса 45.<i>.0.7: у i-го события по (i // 4 + 1), так что у четвёрок одинаковый счёт (ничьи для keyset)."""
    con = journal.connect()
    ips = [f"45.{i}.0.7" for i in range(n_ips)]
    ev = []
    for i, ip in enumerate(ips):
        kind = ("port-scan", "ssh-auth", "hy2-auth", "port-scan")[i % 4]
        port = {"port-scan": 3389 if i % 8 else 22, "ssh-auth": 22, "hy2-auth": 443}[kind]
        ev += [Event(now - 3600 - i * 60, kind, ip, port)] * (i // 4 + 1)
    with con:
        journal.store(con, ev, now)
        con.execute("UPDATE ips SET cc = CASE WHEN CAST(substr(ip, 4, instr(substr(ip, 4), '.') - 1) AS INT) % 2 = 0 "
                    "THEN 'NL' ELSE 'DE' END")
    con.close()
    return ips


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.ips = seed_many()

    def s(self, q="", **kw):
        return journal.search(kw.pop("period", "24h"), q, now=NOW, **kw)

    def ips_of(self, res):
        return [r["ip"] for r in res["rows"]]

    def test_default_order_is_count_desc_then_ip(self):
        res = self.s(limit=50)
        rows = res["rows"]
        self.assertEqual(len(rows), 40)
        self.assertEqual([(-r["n"], r["ip"]) for r in rows], sorted((-r["n"], r["ip"]) for r in rows))
        self.assertEqual(rows[0]["n"], 10)
        self.assertEqual(rows[0]["ip"], "45.36.0.7")
        self.assertEqual(rows[0]["kinds"], {"port-scan": 10})

    def test_prefix_exact_cc_port_service(self):
        self.assertEqual(len(self.s("45.3", limit=50)["rows"]), 11)
        got = self.ips_of(self.s("45.3.", limit=50))
        self.assertEqual(got, ["45.3.0.7"])
        self.assertEqual(self.ips_of(self.s("45.12.0.7")), ["45.12.0.7"])
        nl = self.s("cc:NL", limit=100)
        self.assertEqual(len(nl["rows"]), 20)
        self.assertTrue(all(r["cc"] == "NL" for r in nl["rows"]))
        self.assertEqual(len(self.s("cc:nl cc:de", limit=100)["rows"]), 40, "внутри вида условий — «или»")
        p22 = self.s(":22", limit=100)["rows"]
        self.assertTrue(p22 and all(22 in r["ports"] for r in p22))
        self.assertEqual({r["ip"] for r in p22}, {f"45.{i}.0.7" for i in range(40) if i % 4 == 1 or i % 8 == 0})
        hy = self.s("hy2", limit=100)["rows"]
        self.assertEqual(len(hy), 10)
        self.assertTrue(all(set(r["kinds"]) == {"hy2-auth"} for r in hy))
        self.assertEqual(len(self.s("hysteria2", limit=100)["rows"]), 10)

    def test_filters_are_anded(self):
        both = self.s("cc:NL :22", limit=100)["rows"]
        self.assertTrue(both)
        self.assertTrue(all(r["cc"] == "NL" and 22 in r["ports"] for r in both))
        self.assertLess(len(both), len(self.s("cc:NL", limit=100)["rows"]))
        self.assertEqual(self.s("cc:NL hy2 :22")["rows"], [])

    def test_chips_service_and_kind(self):
        ssh = self.s(svc="ssh", limit=100)["rows"]
        self.assertEqual(len(ssh), 10)
        self.assertTrue(all(set(r["kinds"]) == {"ssh-auth"} for r in ssh))
        one = self.s(kind="ssh-auth", limit=100)["rows"]
        self.assertEqual([r["ip"] for r in one], [r["ip"] for r in ssh])
        self.assertEqual(self.s(svc="ssh", kind="hy2-auth")["rows"], [], "чипы сервиса и вида — «и»")
        self.assertEqual(len(self.s("ssh", svc="ssh", limit=100)["rows"]), 10)
        self.assertEqual(len(self.s(svc="bogus", limit=100)["rows"]), 40, "неизвестный чип игнорируется")

    def test_sort_by_recency(self):
        rows = self.s(sort="last", limit=50)["rows"]
        self.assertEqual(rows[0]["ip"], "45.0.0.7")  # самое свежее событие — у нулевого адреса
        lasts = [r["last"] for r in rows]
        self.assertEqual(lasts, sorted(lasts, reverse=True))

    def test_keyset_pages_cover_everything_without_overlap(self):
        for sort in ("n", "last"):
            seen, cur, pages = [], "", 0
            while True:
                res = self.s(sort=sort, after=cur, limit=15)
                seen += self.ips_of(res)
                pages += 1
                cur = res["next"]
                if not cur:
                    break
            self.assertEqual(pages, 3, sort)
            self.assertEqual(len(seen), 40, sort)
            self.assertEqual(len(set(seen)), 40, sort)
            self.assertEqual(seen, self.ips_of(self.s(sort=sort, limit=100)), sort)

    def test_keyset_is_stable_when_data_arrives_between_pages(self):
        first = self.s(limit=15)
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 10, "port-scan", "46.1.1.1", 80)] * 99, NOW)  # самый частый адрес
        con.close()
        second = self.s(after=first["next"], limit=15)
        self.assertNotIn("46.1.1.1", self.ips_of(second))
        self.assertFalse(set(self.ips_of(first)) & set(self.ips_of(second)))

    def test_no_offset_in_sql(self):
        stmts = []
        orig = journal._con

        def traced():
            con = orig()
            con.set_trace_callback(stmts.append)
            return con
        with mock.patch.object(journal, "_con", traced):
            res = self.s(limit=15)
            self.s(after=res["next"], limit=15)
        self.assertTrue(stmts)
        self.assertFalse([x for x in stmts if "OFFSET" in x.upper()])

    def test_injection_stays_data(self):
        evil = ["'; DROP TABLE hits; --", "x' OR '1'='1", "45.1.0.7' OR 1=1 --", "cc:NL') OR ('1'='1", ":22; DELETE FROM ips",
                "%", "_", "\\", "\x00", "ssh\" OR \"1"]
        stmts = []
        orig = journal._con

        def traced():
            con = orig()
            con.set_trace_callback(stmts.append)
            return con
        with mock.patch.object(journal, "_con", traced):
            for q in evil:
                res = self.s(q, svc=q, kind=q, after=q, sort=q, limit=15)
                self.assertTrue(len(res["rows"]) <= 15)
            self.s(q=" ".join(evil))
        self.assertTrue(stmts)
        self.assertGreater(self.sql_count("hits"), 0)
        self.assertEqual(self.sql_count("ips"), 40)
        self.assertEqual(len(self.s("45.1.0.7' OR 1=1 --", limit=100)["rows"]), 40, "мусор отброшен, остаётся «без фильтра»")

    def sql_count(self, table):
        con = journal.connect(create=False)
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()

    def test_hidden_do_not_eat_pages(self):
        con = journal.connect()
        with con:
            ev = []
            for i in range(30):
                ev += [Event(NOW - 30, "port-scan", f"10.0.{i}.1", 80)] * 50   # локальные, самые частые
            journal.store(con, ev, NOW)
        con.close()
        res = self.s(limit=15)
        self.assertEqual(len(res["rows"]), 15)
        self.assertTrue(all(r["scope"] == "public" for r in res["rows"]))
        self.assertTrue(res["next"])
        every = self.s(limit=100, include_local=True)
        self.assertEqual(len(every["rows"]), 70)
        self.assertEqual(every["rows"][0]["scope"], "local")

    def test_empty_and_missing_database(self):
        self.assertEqual(self.s("cc:ZZ")["rows"], [])
        with ZooEnv():
            res = journal.search("24h", "x", now=NOW)
            self.assertTrue(res["empty"])

    def test_bans_do_not_count_as_attempts(self):
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 60, "ssh-ban", "45.1.0.7", 0)] * 3, NOW)
        con.close()
        row = [r for r in self.s(limit=100)["rows"] if r["ip"] == "45.1.0.7"][0]
        self.assertEqual((row["n"], row["bans"]), (1, 3))
        bans = self.s(svc="fail2ban", limit=100)["rows"]
        self.assertEqual([r["ip"] for r in bans], ["45.1.0.7"])
        self.assertEqual(bans[0]["n"], 0)

    def test_index_covers_search(self):
        con = journal.connect()
        plan = " ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT ip, SUM(n) FROM hits WHERE res = 86400 AND ts + 0 >= 0 AND port IN (22) GROUP BY ip"))
        con.close()
        self.assertIn("COVERING INDEX hits_ip2", plan)
        con = journal.connect(create=False)
        self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE name = 'hits_ip'").fetchall(), [])
        con.close()


class IpCardTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        con = journal.connect()
        ev = []
        for h in range(30):
            ev += [Event(NOW - 3600 * h, "port-scan", A, 3389)] * 2 + [Event(NOW - 3600 * h - 5, "ssh-auth", A, 22)]
        ev += [Event(NOW - 100, "ssh-ban", A, 0), Event(NOW - 90, "port-scan", B, 80)]
        with con:
            journal.store(con, ev, NOW)
            con.execute("UPDATE ips SET cc = 'CN' WHERE ip = ?", (A,))
        con.close()

    def test_summary(self):
        d = journal.ip_card(A, "7d", now=NOW)
        self.assertEqual((d["cc"], d["n"], d["bans"], d["total"]), ("CN", 90, 1, 91))  # в «всего» входит и бан
        self.assertEqual(dict(d["by_service"]), {"ufw": 60, "ssh": 30})
        self.assertEqual(dict(d["by_kind"]), {"port-scan": 60, "ssh-auth": 30})
        self.assertEqual(dict(d["by_port"]), {3389: 60, 22: 30})
        self.assertEqual(d["scope"], "public")
        self.assertLessEqual(d["first"], d["last"])

    def test_feed_keyset(self):
        seen, cur = [], ""
        while True:
            d = journal.ip_card(A, "7d", cur, limit=15, now=NOW)
            seen += [(e["ts"], e["kind"], e["port"]) for e in d["feed"]]
            cur = d["next"]
            if not cur:
                break
        self.assertEqual(len(seen), 61)  # 30 часов × 2 вида + бан
        self.assertEqual(len(set(seen)), 61)
        self.assertEqual(seen, sorted(seen, reverse=True))

    def test_unknown_or_bad_address(self):
        for ip in ("203.0.113.9", "not-an-ip", "", "1.2.3.4' OR 1=1", "45.155.205.10/24"):
            self.assertIsNone(journal.ip_card(ip, "24h", now=NOW), ip)
        self.assertEqual(journal.ip_card(A, "24h", "bogus", now=NOW)["feed"][0]["kind"] in journal.KINDS, True)

    def test_other_ip_does_not_leak(self):
        d = journal.ip_card(B, "24h", now=NOW)
        self.assertEqual((d["n"], len(d["feed"])), (1, 1))


class RealityTrackedTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.xui = self.env.root / "xui"
        (self.xui / "bin").mkdir(parents=True)
        p = mock.patch.dict("os.environ", {"XUI_DIR": str(self.xui)})
        p.start()
        self.addCleanup(p.stop)

    def level(self, lvl):
        (self.xui / "bin" / "config.json").write_text(json.dumps({"log": {"loglevel": lvl}}), encoding="utf-8")

    def test_levels(self):
        self.assertFalse(journal.reality_tracked(), "нет файла")
        for lvl, want in (("warning", False), ("error", False), ("none", False), ("info", True), ("DEBUG", True)):
            self.level(lvl)
            self.assertEqual(journal.reality_tracked(), want, lvl)
        (self.xui / "bin" / "config.json").write_text("{ не json", encoding="utf-8")
        self.assertFalse(journal.reality_tracked())

    def test_report_flag_and_blind_list(self):
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 60, "port-scan", A, 80)], NOW)
        con.close()
        d = journal.report("24h", now=NOW)
        self.assertFalse(d["reality_tracked"])
        self.assertIn("REALITY", [b["what"] for b in d["blind"]])
        self.level("info")
        d = journal.report("24h", now=NOW)
        self.assertTrue(d["reality_tracked"])
        self.assertNotIn("REALITY", [b["what"] for b in d["blind"]])

    def test_recorded_probe_proves_tracking(self):
        con = journal.connect()
        with con:
            journal.store(con, [Event(NOW - 60, "reality-probe", A, 443)], NOW)
        con.close()
        self.assertTrue(journal.report("24h", now=NOW)["reality_tracked"])


class JournalSearchPageTest(AppTestBase):
    def setUp(self):
        super().setUp()
        self.c.login()
        now = int(time.time())
        con = journal.connect()
        ev = []
        for i in range(40):
            ev += [Event(now - 600 - i, "port-scan", f"45.{i}.0.7", 3389)] * (i + 1)
        ev += [Event(now - 30, "ssh-auth", "91.240.118.5", 22)] * 3 + [Event(now - 20, "hy2-auth", C, 443)] * 2
        with con:
            journal.store(con, ev, now)
            con.execute("UPDATE ips SET cc = 'CN' WHERE ip LIKE '45.%'")
            con.execute("UPDATE ips SET cc = 'DE' WHERE ip = '91.240.118.5'")
        con.close()

    def test_filter_in_url_and_markup(self):
        _, body = self.c.get("/journal?period=24h&q=cc%3ADE")
        self.assertIn("91.240.118.5", body)
        self.assertNotIn("45.5.0.7", body)
        self.assertIn('value="cc:DE"', body)
        self.assertIn("сбросить", body)
        _, body = self.c.get("/journal?period=24h&svc=ssh")
        self.assertIn("91.240.118.5", body)
        self.assertNotIn("<code>45.", body)
        self.assertIn("aria-pressed", body)
        _, body = self.c.get("/journal?svc=hysteria")
        self.assertIn("Hysteria2: неверный ключ</a>", body, "после выбора сервиса — чипы его видов")

    def test_empty_result(self):
        _, body = self.c.get("/journal?q=cc%3AZZ")
        self.assertIn("Ничего не нашлось", body)
        self.assertNotIn("показать ещё", body)

    def test_bad_terms_are_reported(self):
        resp, body = self.c.get("/journal?q=" + "%27%3B+DROP+TABLE+hits%3B+--+zzz")
        self.assertEqual(resp.status, 200)
        self.assertIn("Не понял: ", body)
        self.assertNotIn("<script>", body)

    def test_injection_and_html_in_every_param(self):
        evil = "%22%3E%3Cscript%3Ealert(1)%3C%2Fscript%3E"
        for q in (f"q={evil}", f"svc={evil}", f"kind={evil}", f"after={evil}", f"ip={evil}", f"n={evil}", f"sort={evil}",
                  "q=%27+OR+1%3D1+--", "after=1%3A%27%3BDROP", "n=99999", "n=-5", "ip=1.2.3.4%27--"):
            resp, body = self.c.get("/journal?" + q)
            self.assertEqual(resp.status, 200, q)
            self.assertNotIn("<script>alert", body, q)
        _, body = self.c.get("/journal?q=" + evil)
        self.assertIn("&lt;script&gt;", body)
        con = journal.connect(create=False)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM hits").fetchone()[0], 40)
        con.close()

    def test_more_link_walks_pages_without_offset(self):
        _, p1 = self.c.get("/journal?period=24h")
        self.assertEqual(p1.count("data-more>"), 1)
        m = re.search(r'href="([^"]*)" class="btn small" data-more', p1)
        self.assertTrue(m)
        href = m.group(1).replace("&amp;", "&")
        self.assertIn("after=", href)
        self.assertNotIn("offset", href.lower())
        seen = set(re.findall(r"<code>([0-9.]+)</code>", p1.split('data-more-box')[1].split("</table>")[0]))
        self.assertEqual(len(seen), 15)
        _, p2 = self.c.get(href)
        seen2 = set(re.findall(r"<code>([0-9.]+)</code>", p2.split('data-more-box')[1].split("</table>")[0]))
        self.assertEqual(len(seen2), 15)
        self.assertFalse(seen & seen2)
        self.assertIn("← с начала", p2)

    def test_row_count_and_sort_selectors(self):
        _, body = self.c.get("/journal?n=50&sort=last")
        self.assertEqual(len(re.findall(r'data-label="адрес"', body)), 42)
        self.assertNotIn("показать ещё", body)
        self.assertIn('n=100', body)
        self.assertIn('aria-label="Строк на странице"', body)

    def test_ip_card_page(self):
        _, body = self.c.get("/journal?ip=45.39.0.7&period=24h")
        self.assertIn("45.39.0.7", body)
        self.assertIn("Первый раз", body)
        self.assertIn("По портам", body)
        self.assertIn("3389", body)
        self.assertIn("← все адреса", body)
        _, body = self.c.get("/journal?ip=45.250.0.7")
        self.assertIn("Адреса нет в журнале", body)
        resp, body = self.c.get("/journal?ip=garbage")
        self.assertEqual(resp.status, 200)
        self.assertIn("Источники", body, "не адрес — обычная выдача")

    def test_ip_links_lead_to_card_and_keep_state(self):
        _, body = self.c.get("/journal?period=7d&q=cc%3ACN")
        self.assertIn('href="/journal?period=7d&amp;ip=45.', body)
        self.assertIn("data-swap", body)

    def test_help_dialogs_for_every_kind_and_no_inline_js_or_css(self):
        _, body = self.c.get("/journal")
        for kind in journal.KINDS:
            self.assertIn(f'id="kh-{kind}"', body, kind)
        self.assertIn('data-dialog="kh-port-scan"', body)
        self.assertEqual(body.count("Что это. "), len(journal.KINDS))
        self.assertEqual(body.count("Что делать. "), len(journal.KINDS))
        for bad in (" style=", " onclick=", " onchange=", " onsubmit=", "javascript:", "<style"):
            self.assertNotIn(bad, body, bad)
        _, ip_body = self.c.get("/journal?ip=45.1.0.7")
        for bad in (" style=", " onclick=", "<style"):
            self.assertNotIn(bad, ip_body, bad)

    def test_quiet_groups_collapse_and_reality_is_honest(self):
        _, body = self.c.get("/journal")
        self.assertIn("панели — попыток не было", body)
        self.assertIn("REALITY — не отслеживается (так задумано: иначе в логах были бы сайты пользователей)", body)
        self.assertNotIn("тихо:", body)
        self.assertNotIn("Проверяли REALITY и Hysteria2", body)
        self.assertIn("Проверяли, прокси ли это", body)
        xui = self.env.root / "xui"
        (xui / "bin").mkdir(parents=True)
        (xui / "bin" / "config.json").write_text('{"log": {"loglevel": "info"}}', encoding="utf-8")
        with mock.patch.dict("os.environ", {"XUI_DIR": str(xui)}):
            self.app.invalidate()
            _, body = self.c.get("/journal")
        self.assertNotIn("не отслеживается", body)
        self.assertIn("Прокси (REALITY, Hysteria2) проверяли чужие клиенты: 2 попытки с 1 адреса", body)

    def test_search_results_cached_per_state(self):
        self.c.get("/journal?q=cc%3ACN")
        with mock.patch.object(journal, "search", side_effect=AssertionError("из кэша")):
            self.assertEqual(self.c.get("/journal?q=cc%3ACN")[0].status, 200)
            self.assertEqual(self.c.get("/journal?q=cc%3ADE")[0].status, 500, "другое состояние — новый запрос")


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
        for text in (A, B, C, "CN", "Стучались в закрытые порты", "Перебор SSH", "Проверяли, прокси ли это",
                     "Чего мы не видим", "3389"):
            self.assertIn(text, body)
        self.assertNotIn("172.22.0.4", body, "локальные скрыты по умолчанию")
        self.assertIn("скрыты свои и служебные адреса — служебных 3", body)
        resp, body = self.c.get("/journal?period=7d&all=1")
        self.assertIn("172.22.0.4", body)
        self.assertIn("служебный", body)

    def test_page_is_short_with_verdict(self):
        self.seed()
        _, body = self.c.get("/journal?period=24h")
        self.assertLessEqual(visible_words(body), 200)
        self.assertIn("Прокси (Hysteria2) проверяли чужие клиенты: 1 попытка с 1 адреса.", body)
        self.assertIn("ничего делать не нужно", body)
        self.assertNotIn("Щупают", body)
        self.assertIn('class="info"', body.split("Прокси (Hysteria2)")[0][-80:], "одна попытка — не тревога")
        self.assertIn("панели — попыток не было", body)
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


class OwnAddressesTest(AppTestBase):
    """Тревоги и вывод страницы считают только внешние адреса: свои, служебные, тестовые и сам сервер не в счёт."""

    SERVER = "45.9.9.9"
    NOISE = ("192.0.2.5", "198.51.100.7", "203.0.113.9", "10.1.1.1", "172.22.0.4", "127.0.0.1", "100.64.0.9",
             "2001:db8::5", "198.18.0.7")

    def setUp(self):
        super().setUp()
        self.env.write_config({"SERVER_IP": self.SERVER, "LABEL": "t"})
        journal._ifaces = None
        self.addCleanup(setattr, journal, "_ifaces", None)
        self.real_ifaces = journal._iface_addrs
        p = mock.patch("zoolib.journal._iface_addrs", return_value=["10.0.0.5", "127.0.0.1", "46.8.8.8"])
        p.start()
        self.addCleanup(p.stop)
        self.c.login()

    def seed(self, extra=()):
        now = int(time.time())
        ev = []
        for ip in (*self.NOISE, self.SERVER, "46.8.8.8", "203.0.113.50"):
            ev += [Event(now - 60, "hy2-auth", ip, 443)] * 30 + [Event(now - 50, "panel-login", ip, 0)] * 3
        con = journal.connect()
        with con:
            journal.store(con, [*ev, *extra, Event(now - 10, journal.OWN_LOGIN, "203.0.113.50", 0)], now)
        con.close()

    def test_documentation_private_and_own_addresses_are_not_public(self):
        nets = journal.ignore_nets()
        for ip in self.NOISE:
            self.assertEqual(journal.scope_of(ip, set(), nets), "local", ip)
        for ip in (self.SERVER, "46.8.8.8"):
            self.assertEqual(journal.scope_of(ip, set(), nets), "own", ip)
        self.assertEqual(journal.scope_of("8.8.8.8", set(), nets), "public")
        self.assertEqual({str(n) for n in journal.server_nets()}, {self.SERVER + "/32", "46.8.8.8/32"},
                         "из интерфейсов — только публичные, локальные и так служебные")

    def test_only_external_address_makes_the_verdict(self):
        self.seed()
        for q in ("", "&all=1"):
            _, body = self.c.get("/journal?period=24h" + q)
            self.assertIn("Обычный фон: сканеры и перебор SSH", body, q)
            self.assertNotIn("проверяли чужие клиенты", body, q)
            self.assertNotIn("Пробовали войти", body, q)
        now = int(time.time())
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 5, "hy2-auth", "8.8.4.4", 443)] * 2, now)
        con.close()
        for q in ("", "&all=1"):
            self.app.invalidate()
            _, body = self.c.get("/journal?period=24h" + q)
            self.assertIn("Прокси (Hysteria2) проверяли чужие клиенты: 2 попытки с 1 адреса.", body, q)
            self.assertNotIn("Пробовали войти", body, q)

    def test_external_panel_login_is_a_warning_in_plain_words(self):
        now = int(time.time())
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 5, "panel-login", "8.8.4.4", 0)] * 3 + [Event(now - 4, "panel-login", "8.8.8.8", 0)],
                          now)
        con.close()
        _, body = self.c.get("/journal?period=24h")
        self.assertIn("Пробовали войти в панель с внешних адресов: 4 попытки с 2 адресов.", body)
        self.assertIn("слушать только 127.0.0.1", body)
        self.assertIn('class="warn"', body.split("Пробовали войти")[0][-80:])

    def test_toggle_wording_and_tooltip(self):
        self.seed()
        _, body = self.c.get("/journal?period=24h")
        self.assertIn("свои и служебные адреса: показать", body)
        self.assertNotIn("показать локальные", body)
        m = re.search(r'<a href="/journal\?period=24h&amp;all=1" title="([^"]+)"', body)
        self.assertIsNotNone(m)
        for word in ("SSH-ключу", "сам сервер", "контейнеры", "192.0.2.0/24"):
            self.assertIn(word, m.group(1))
        _, body = self.c.get("/journal?period=24h&all=1")
        self.assertIn("свои и служебные адреса: скрыть", body)
        self.assertIn("служебный", body)

    def test_spike_alert_ignores_noise_addresses(self):
        now = int(time.time())
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 60, "port-scan", ip, 80) for ip in self.NOISE] * 200
                          + [Event(now - 60, "port-scan", self.SERVER, 80)] * 500, now)
        con.close()
        self.assertEqual(journal.alerts(now), [])
        con = journal.connect()
        with con:
            journal.store(con, [Event(now - 30, "port-scan", "8.8.4.4", 80)] * 150, now)
        con.close()
        al = journal.alerts(now)
        self.assertEqual(len(al), 1)
        self.assertIn("150", al[0][1])

    def test_ru_plural(self):
        from zoolib.web.journalviews import _ru
        got = [_ru(n, "попытка", "попытки", "попыток") for n in (1, 2, 4, 5, 11, 12, 21, 22, 25, 100, 101)]
        self.assertEqual(got, ["1 попытка", "2 попытки", "4 попытки", "5 попыток", "11 попыток", "12 попыток",
                               "21 попытка", "22 попытки", "25 попыток", "100 попыток", "101 попытка"])

    def test_iface_parsing_is_cached_and_survives_missing_ip(self):
        out = ("1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 45.1.2.3/24 brd 45.1.2.255 scope global eth0\n"
               "2: eth0    inet6 2a01:db8::1/64 scope global\n")
        journal._ifaces = None
        with mock.patch("zoolib.journal.system.run", return_value=(0, out, "")) as run:
            self.assertEqual(self.real_ifaces(1000.0), ["127.0.0.1", "45.1.2.3", "2a01:db8::1"])
            self.real_ifaces(1100.0)
            self.assertEqual(run.call_count, 1, "раз в пять минут, не на каждую страницу")
            self.real_ifaces(1000.0 + journal.IFACE_TTL + 1)
            self.assertEqual(run.call_count, 2)
        journal._ifaces = None
        with mock.patch("zoolib.journal.system.run", return_value=(127, "", "нет ip")):
            self.assertEqual(self.real_ifaces(5.0), [])


if __name__ == "__main__":
    unittest.main()
