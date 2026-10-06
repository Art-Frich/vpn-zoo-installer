import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import ZOO_DIR, ZooEnv, needs_bash
from zoolib import cli, config, output, status, system


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliBasicsTest(unittest.TestCase):
    def test_version_json(self):
        code, out, _ = run_cli("version", "--json")
        self.assertEqual(code, 0)
        self.assertIn("zoo", json.loads(out))

    def test_json_flag_before_command(self):
        code, out, _ = run_cli("--json", "version")
        self.assertEqual(code, 0)
        json.loads(out)

    def test_admin_commands_without_install(self):
        # traffic/web/upgrade на пустой системе: подсказки, а не трассировки
        with ZooEnv():
            code, _, err = run_cli("traffic")
            self.assertEqual(code, 0)
            self.assertIn("коллектор ещё не запускался", err)
            code, _, err = run_cli("traffic", "--period", "5y")
            self.assertEqual(code, 2)
            code, out, _ = run_cli("web", "--info", "--json")
            self.assertEqual(code, 0)
            self.assertIn("tunnel", json.loads(out))
            code, _, err = run_cli("web")
            self.assertEqual(code, 1)
            self.assertIn("ZOO_WEB_PORT", err)
            code, out, _ = run_cli("upgrade", "--json")
            self.assertEqual(code, 0)
            self.assertIn("components", json.loads(out))

    def test_request_commands_run_with_broken_config_and_clear_requests(self):
        # битый config.env не должен оставлять заявку: .path-юнит гонял бы сервис по кругу
        from zoolib import clients, logctl
        from zoolib.probe import live
        with ZooEnv():
            boom = mock.patch("zoolib.cli.load_config", side_effect=config.ConfigError("битый config.env"))
            with boom:
                code, _, err = run_cli("version")
                self.assertEqual(code, 0)
                self.assertTrue(clients.request_check()[0])
                self.assertTrue(clients.req_file().exists())
                with mock.patch.object(clients, "check_upstream", return_value={"errors": {}}):
                    code, _, err = run_cli("clients", "--check-upstream")
                self.assertEqual(code, 0, err)
                self.assertFalse(clients.req_file().exists())
                rid = logctl.submit("vacuum-time", "7d")
                with mock.patch.object(logctl, "vacuum", return_value={"freed": 0, "before": 0, "after": 0}):
                    code, _, err = run_cli("logs", "run")
                self.assertEqual(code, 0, err)
                self.assertEqual(logctl.pending(), [])
                self.assertEqual(logctl.states()[0]["id"], rid)
                with mock.patch.object(live, "run", return_value=[]) as run:
                    code, _, err = run_cli("live", "run", "--requests")
                self.assertEqual(code, 0, err)
                run.assert_called_once()
                code, _, err = run_cli("traffic")
                self.assertEqual(code, 1, "остальным командам config.env по-прежнему нужен")
                self.assertIn("битый config.env", err)

    def test_no_command(self):
        code, out, _ = run_cli()
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_entrypoint(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(ZOO_DIR / "zoo"), "version", "--json"],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(json.loads(cp.stdout)["python"], sys.version.split()[0])


@needs_bash
class CliUsersTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        for pid in ("vless-reality", "amneziawg"):
            self.env.add_protocol(pid)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_user_flow(self):
        code, out, _ = run_cli("setup", "--json")
        self.assertEqual(code, 0, out)
        code, out, err = run_cli("user", "add", "masha", "--note", "тест", "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["ok"])
        code, out, _ = run_cli("user", "list", "--json", "--verify")
        data = json.loads(out)
        self.assertEqual([u["name"] for u in data["users"]], ["owner", "masha"])
        self.assertEqual(data["verify"]["amneziawg"]["missing"], [])
        self.assertEqual(data["verify"]["amneziawg"]["extra"], [])
        # служебный пользователь пробника заведён фазой 09 (setup), но скрыт
        code, out, _ = run_cli("user", "list", "--all", "--json")
        self.assertIn({"name": "zoo-probe", "system": True},
                      [{"name": u["name"], "system": u.get("system")} for u in json.loads(out)["users"]])
        self.assertEqual(self.env.proto_users("amneziawg").get("zoo-probe"), "true")
        code, _, err = run_cli("user", "del", "zoo-probe")
        self.assertEqual(code, 1)
        self.assertIn("служебный", err)
        code, _, err = run_cli("user", "add", "zoo-probe")
        self.assertEqual(code, 1)
        code, out, _ = run_cli("user", "list")
        self.assertIn("masha", out)
        self.assertIn("тест", out)
        code, out, _ = run_cli("links", "masha", "--json")
        data = json.loads(out)
        self.assertEqual({x["proto"] for x in data["links"]}, {"vless-reality", "amneziawg", "allowlist"})
        files = [x["uri"] for x in data["links"] if x["kind"] == "file"]
        self.assertEqual(len(files), 2)  # .conf AWG и правила v2rayN
        self.assertTrue(files[0].endswith("amneziawg.conf"))
        code, out, _ = run_cli("links", "masha")
        self.assertIn("файл: ", out)
        code, out, _ = run_cli("links", "masha", "--proto", "vless-reality")
        self.assertIn("vless://masha@10.0.0.1:443", out)
        self.assertNotIn("amneziawg.conf", out)
        code, out, _ = run_cli("user", "show", "masha", "--json")
        self.assertEqual(json.loads(out)["note"], "тест")
        code, _, err = run_cli("user", "disable", "masha")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.env.proto_users("vless-reality")["masha"], "false")
        code, _, _ = run_cli("user", "enable", "masha")
        self.assertEqual(code, 0)
        code, _, _ = run_cli("user", "del", "masha")
        self.assertEqual(code, 0)
        self.assertNotIn("masha", self.env.proto_users("vless-reality"))

    def test_errors_exit_1(self):
        code, _, err = run_cli("user", "del", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("ghost", err)
        self.env.fail("vless-reality:user_add")
        code, out, err = run_cli("user", "add", "masha")
        self.assertEqual(code, 1)
        self.assertIn("vless-reality: ошибка (добавление) — vless-reality: искусственная ошибка user_add", err)
        self.assertIn("amneziawg: откат", err)
        code, _, err = run_cli("user", "show", "masha")
        self.assertEqual(code, 1)
        code, _, err = run_cli("links", "nobody")
        self.assertEqual(code, 1)


class StatusTest(unittest.TestCase):
    def test_status_collect_and_render(self):
        ss = system.parse_ss("tcp LISTEN 0 4096 *:443 *:* users:((\"xray\",pid=1,fd=3))\n"
                             "tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=2,fd=3))\n"
                             "tcp LISTEN 0 4096 0.0.0.0:2096 0.0.0.0:* users:((\"x-ui\",pid=3,fd=3))\n")
        units = {u: {"load": "loaded", "active": "active", "sub": "running", "enabled": "enabled"}
                 for u in ("x-ui.service", "fail2ban.service")}
        units["hysteria-server.service"] = {"load": "loaded", "active": "failed", "sub": "failed", "enabled": ""}
        with ZooEnv() as env, \
                mock.patch.object(system, "listening_sockets", return_value=ss), \
                mock.patch.object(system, "ufw_active", return_value=False), \
                mock.patch.object(system, "unit_states", side_effect=lambda us: {u: units.get(u, {
                    "load": "not-found", "active": "inactive"}) for u in us}), \
                mock.patch.object(system, "component_versions", return_value={"x-ui": "3.9.0"}):
            env.add_manifest("vless-reality")
            env.add_manifest("hysteria2", layer="udp", service="hysteria-server")
            data = status.collect(config.load(), cpu_interval=0)
            by_id = {p["id"]: p for p in data["protocols"]}
            self.assertTrue(by_id["vless-reality"]["ok"])
            self.assertFalse(by_id["hysteria2"]["ok"])
            problems = "\n".join(data["problems"])
            self.assertIn("hysteria-server — failed", problems)
            self.assertIn("никто не слушает 443/udp", problems)
            self.assertIn("2096/tcp", problems)
            self.assertNotIn("22/tcp", problems)
            self.assertIn("UFW выключен", problems)
            json.dumps(data, default=str)
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                cli._render_status(data)
            self.assertIn("vless-reality", out.getvalue())


class StatusExtraTest(unittest.TestCase):
    def test_stale_collector_and_missing_interface(self):
        units = {u: {"load": "loaded", "active": "active", "sub": "running", "enabled": "enabled"}
                 for u in ("x-ui.service", "fail2ban.service", "awg-quick@awg-nope.service",
                           "zoo-collector.timer")}
        ss = system.parse_ss("udp UNCONN 0 0 0.0.0.0:51820 0.0.0.0:* users:((\"amneziawg-go\",pid=1,fd=3))\n")
        run = {"ts": 0, "age": 7200, "stale": True, "errors": {"amneziawg": "awg: нет доступа"}}
        with ZooEnv() as env, \
                mock.patch.object(system, "listening_sockets", return_value=ss), \
                mock.patch.object(system, "ufw_active", return_value=True), \
                mock.patch.object(system, "unit_states", side_effect=lambda us: {u: units.get(u, {
                    "load": "not-found", "active": "inactive"}) for u in us}), \
                mock.patch.object(system, "component_versions", return_value={}), \
                mock.patch("zoolib.traffic.last_run", return_value=run):
            env.add_manifest("amneziawg", layer="udp", port=51820, service="awg-quick@awg-nope",
                             interface="awg-nope")
            data = status.collect(config.load(), cpu_interval=0, with_xui=False)
            problems = "\n".join(data["problems"])
            self.assertIn("нет интерфейса awg-nope", problems)
            self.assertIn("коллектор трафика молчит", problems)
            self.assertIn("трафик, amneziawg: awg: нет доступа", problems)
            self.assertFalse(data["protocols"][0]["ok"])


class OutputTest(unittest.TestCase):
    def test_table_alignment(self):
        t = output.table([["owner", "да"], ["маша", "\033[32mOK\033[0m"]], ["имя", "вкл"])
        lines = t.splitlines()
        self.assertEqual(lines[0], "имя    вкл")
        self.assertEqual(output.width(lines[3]), len("маша   OK"))

    def test_human(self):
        self.assertEqual(output.human_bytes(512), "512 Б")
        self.assertEqual(output.human_bytes(1536), "1.5 КБ")
        self.assertEqual(output.human_bytes(None), "—")
        self.assertEqual(output.human_duration(90061), "1 д 1 ч")
        self.assertEqual(output.human_duration(3700), "1 ч 1 мин")


if __name__ == "__main__":
    unittest.main()


class StatusUsersTest(unittest.TestCase):
    def test_obfs_card_counts_shared_users_and_has_short_name(self):
        users = {"schema": 1, "users": [
            {"name": n, "created": "x", "enabled": True, "note": "", "protocols": ["hysteria2"]}
            for n in ("owner", "masha")]}
        units = {"x-ui.service": {"load": "loaded", "active": "active"}}
        with ZooEnv() as env, \
                mock.patch.object(system, "listening_sockets", return_value=[]), \
                mock.patch.object(system, "ufw_active", return_value=True), \
                mock.patch.object(system, "unit_states", side_effect=lambda us: {u: units.get(u, {
                    "load": "not-found", "active": "inactive"}) for u in us}), \
                mock.patch.object(system, "component_versions", return_value={}):
            env.add_proto("hysteria2")
            env.add_manifest("hysteria2", layer="udp", users_backend="hysteria-command", engine="hysteria")
            env.add_manifest("hysteria2-obfs", layer="udp", users_backend="hysteria-command", engine="hysteria",
                             name="Hysteria2 + Salamander", short="HY2 + Salamander")
            env.add_manifest("ss2022", name="Shadowsocks-2022 (2022-blake3-aes-128-gcm)")
            (env.etc / "users.json").write_text(json.dumps(users), encoding="utf-8")
            by_id = {p["id"]: p for p in status.collect(config.load(), cpu_interval=0, with_xui=False)["protocols"]}
        self.assertEqual((by_id["hysteria2"]["users"], by_id["hysteria2-obfs"]["users"]), (2, 2))
        self.assertEqual(by_id["hysteria2-obfs"]["short"], "HY2 + Salamander")
        self.assertEqual(by_id["ss2022"]["short"], "Shadowsocks-2022")
