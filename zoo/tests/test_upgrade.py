import copy
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ZooEnv, needs_bash
from tests.test_web import FAKE_STATUS
from zoolib import config, upgrade

INSTALLED = {"x-ui": "3.9.0", "xray": "26.9.30", "hysteria": "v2.12.3", "awg": "", "python": "3", "kernel": "6"}
PINNED = "XUI_VERSION=v3.9.0\nXUI_XRAY_VERSION=26.9.30\nHY2_VERSION=v2.12.3\nAWG_GO_REF=v3.1\nGEO_TAG=1\n"


class ComponentsTest(unittest.TestCase):
    def test_outdated(self):
        pinned = {"XUI_VERSION": "v3.9.0", "XUI_XRAY_VERSION": "26.9.30", "HY2_VERSION": "v2.13.0"}
        comp = upgrade.components(INSTALLED, pinned)
        self.assertFalse(comp["x-ui"]["outdated"])  # «v3.9.0» = «3.9.0»
        self.assertFalse(comp["xray"]["outdated"])
        self.assertTrue(comp["hysteria"]["outdated"])
        self.assertIsNone(comp["amneziawg-go"]["outdated"])
        self.assertEqual(upgrade.plan_phases(comp, [], []), ["05-hysteria2", "09-zoo"])

    def test_awg_tools_version_parsed(self):
        inst = dict(INSTALLED, awg="amneziawg-tools v3.1.20260812 - https://amnezia.org")
        comp = upgrade.components(inst, {"AWG_TOOLS_REF": "v3.1.20260812", "AWG_GO_REF": "v3.1.20260828"})
        self.assertEqual(comp["amneziawg-tools"]["installed"], "v3.1.20260812")
        self.assertFalse(comp["amneziawg-tools"]["outdated"])
        self.assertEqual(comp["amneziawg-go"]["pinned"], "v3.1.20260828")
        comp = upgrade.components(inst, {"AWG_TOOLS_REF": "v3.1.20261001"})
        self.assertTrue(comp["amneziawg-tools"]["outdated"])
        self.assertIn("06-amneziawg", upgrade.plan_phases(comp, [], []))

    def test_plan_from_versions_diff(self):
        comp = upgrade.components(INSTALLED, {})
        self.assertEqual(upgrade.plan_phases(comp, ["AWG_GO_REF", "GEO_TAG", "XUI_SHA256_amd64"], []),
                         ["03-3xui", "06-amneziawg", "07-routing", "09-zoo"])
        self.assertEqual(upgrade.plan_phases(comp, [], ["zoo/zoolib/web/app.py"]), ["09-zoo"])
        self.assertEqual(upgrade.plan_phases(comp, [], []), [])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.repo = self.env.root / "repo"
        home = Path(self.env.root / "opt")
        for base in (self.repo, home):
            (base / "scripts").mkdir(parents=True)
            (base / "zoo").mkdir(parents=True)
            (base / "scripts" / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (base / "scripts" / "versions.env").write_text(PINNED, encoding="utf-8")
            (base / "zoo" / "zoo").write_text("x", encoding="utf-8")
        (home / "INSTALL.json").write_text('{"source": "%s"}' % self.repo.as_posix(), encoding="utf-8")
        self.home = home
        p = mock.patch("zoolib.system.component_versions", return_value=INSTALLED)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_up_to_date(self):
        d = upgrade.check(config.load())
        self.assertEqual(d["repo"], str(self.repo))
        self.assertEqual(d["phases"], [])
        self.assertEqual(d["changed"], [])

    def test_versions_bumped_in_repo(self):
        (self.repo / "scripts" / "versions.env").write_text(PINNED.replace("v2.12.3", "v2.13.0"), encoding="utf-8")
        (self.repo / "zoo" / "zoo").write_text("y", encoding="utf-8")
        d = upgrade.check(config.load())
        self.assertEqual(d["version_diff"], ["HY2_VERSION"])
        self.assertTrue(d["components"]["hysteria"]["outdated"])
        self.assertIn("zoo/zoo", d["changed"])
        self.assertEqual(d["phases"], ["05-hysteria2", "09-zoo"])

    def test_no_repo(self):
        shutil.rmtree(self.repo)
        d = upgrade.check(config.load())
        self.assertIsNone(d["repo"])
        with self.assertRaises(RuntimeError):
            upgrade.apply(config.load(), verbose=False)

    @needs_bash
    def test_apply_runs_phases_and_hints_rollback(self):
        log = self.env.root / "phases.log"
        (self.repo / "scripts" / "install.sh").write_text(
            '#!/bin/bash\necho "$2" >> "%s"\n[ "$2" != "09-zoo" ] || exit 3\n' % log.as_posix(), encoding="utf-8")
        smoke_ok = {"ok": True, "checks": [{"name": "a", "ok": True, "detail": ""}], "probe": None, "ts": 0}
        with mock.patch("zoolib.upgrade.smoke", return_value=smoke_ok):
            res = upgrade.apply(config.load(), phases=["05-hysteria2", "09-zoo"], verbose=False)
        self.assertEqual(log.read_text(encoding="utf-8").split(), ["05-hysteria2", "09-zoo"])
        self.assertFalse(res["ok"])
        self.assertEqual([p["rc"] for p in res["phases"]], [0, 3])
        self.assertTrue(any("install.sh --phase 09-zoo" in h for h in res["rollback"]))


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def run_smoke(self, status, probe_results=None, probe_exc=None):
        rep = {"results": probe_results or []}
        with mock.patch("zoolib.status.collect", return_value=status), \
                mock.patch("zoolib.probe.run_local", return_value=rep, side_effect=probe_exc):
            return upgrade.smoke(config.load())

    def test_failures_detected(self):
        d = self.run_smoke(FAKE_STATUS, [{"id": "vless-reality", "verdict": "OK"},
                                         {"id": "hysteria2", "verdict": "SERVER_DOWN"}])
        self.assertFalse(d["ok"])
        by = {c["name"]: c for c in d["checks"]}
        self.assertTrue(by["протокол vless-reality"]["ok"])
        self.assertFalse(by["протокол hysteria2"]["ok"])
        self.assertFalse(by["лишние listen на всех адресах"]["ok"])
        self.assertFalse(by["zoo probe --local"]["ok"])
        self.assertIn("hysteria2: SERVER_DOWN", by["zoo probe --local"]["detail"])

    def test_all_good(self):
        st = copy.deepcopy(FAKE_STATUS)
        st["protocols"] = [st["protocols"][0]]
        st["exposed"] = []
        d = self.run_smoke(st, [{"id": "vless-reality", "verdict": "OK"}, {"id": "awg", "verdict": "SKIPPED"}])
        self.assertTrue(d["ok"], d["checks"])

    def test_probe_crash_is_a_failed_check(self):
        st = copy.deepcopy(FAKE_STATUS)
        st["protocols"], st["exposed"] = [st["protocols"][0]], []
        d = self.run_smoke(st, probe_exc=RuntimeError("нет xray"))
        self.assertFalse(d["ok"])
        self.assertIn("нет xray", d["checks"][-1]["detail"])


class UpstreamTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.tags = {"MHSanaei/3x-ui": "v3.10.0", "XTLS/Xray-core": "v26.9.30", "HyNetworks/hysteria": "v2.12.3",
                     "amnezia-vpn/amneziawg-tools": "v3.1.20260812", "amnezia-vpn/amneziawg-go": "v3.1.20260901",
                     "SagerNet/sing-box": "v1.14.2"}

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_latest_tag_parses_and_uses_timeout(self):
        for raw, want in (("app/v2.12.3", "v2.12.3"), ("v26.9.30", "v26.9.30"), ("1.14.2", "1.14.2")):
            with mock.patch.object(upgrade, "_http_json", return_value={"tag_name": raw}):
                self.assertEqual(upgrade.latest_tag("a/b"), want)
        with mock.patch.object(upgrade, "_http_json", return_value={"tag_name": "nightly"}):
            with self.assertRaises(ValueError):
                upgrade.latest_tag("a/b")
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"tag_name": "v1.2.3"}')
        with mock.patch("urllib.request.urlopen", return_value=resp) as op:
            self.assertEqual(upgrade.latest_tag("a/b"), "v1.2.3")
        self.assertEqual(op.call_args.kwargs["timeout"], 10)
        self.assertIn("repos/a/b/releases/latest", op.call_args.args[0].full_url)

    def test_refresh_writes_cache(self):
        with mock.patch.object(upgrade, "latest_tag", side_effect=lambda repo: self.tags[repo]) as lt:
            data = upgrade.refresh_upstream(config.Config(), now=1000)
        self.assertEqual(data["ts"], 1000)
        self.assertEqual(data["items"]["x-ui"], {"repo": "MHSanaei/3x-ui", "tag": "v3.10.0", "error": ""})
        self.assertEqual(data["items"]["hysteria"]["repo"], "HyNetworks/hysteria")
        self.assertEqual(lt.call_count, 6)
        self.assertEqual(upgrade.read_upstream(), data)

    def test_failure_keeps_previous_tag(self):
        with mock.patch.object(upgrade, "latest_tag", side_effect=lambda repo: self.tags[repo]):
            upgrade.refresh_upstream(config.Config(), now=1000)

        def flaky(repo):
            if repo == "XTLS/Xray-core":
                raise OSError("timed out")
            return self.tags[repo]

        with mock.patch.object(upgrade, "latest_tag", side_effect=flaky):
            data = upgrade.refresh_upstream(config.Config(), now=2000)
        self.assertEqual(data["items"]["xray"], {"repo": "XTLS/Xray-core", "tag": "v26.9.30", "error": "timed out"})
        self.assertEqual(data["items"]["x-ui"]["error"], "")

    def test_hysteria_source_follows_engine(self):
        self.assertEqual(upgrade.upstream_repos(config.Config())["hysteria"], "HyNetworks/hysteria")
        self.assertEqual(upgrade.upstream_repos(config.Config({"HY2_ENGINE": "apernet"}))["hysteria"],
                         "HyNetworks/hysteria")
        self.assertIsNone(upgrade.upstream_repos(config.Config({"HY2_ENGINE": "other"}))["hysteria"])
        with mock.patch.object(upgrade, "latest_tag", side_effect=lambda repo: self.tags[repo]):
            data = upgrade.refresh_upstream(config.Config({"HY2_ENGINE": "other"}), now=1)
        self.assertEqual(data["items"]["hysteria"]["tag"], "")
        self.assertIn("HY2_ENGINE=other", data["items"]["hysteria"]["error"])

    def comps(self, cfg=None, up=None):
        comp = upgrade.components(INSTALLED, {"XUI_VERSION": "v3.9.0", "XUI_XRAY_VERSION": "26.9.30",
                                              "HY2_VERSION": "v2.12.3", "AWG_GO_REF": "v3.1.20260828"})
        upgrade.with_upstream(comp, up if up is not None else {"items": {
            n: {"tag": t} for n, t in (("x-ui", "v3.10.0"), ("xray", "v26.9.30"), ("hysteria", "v2.12.3"),
                                       ("amneziawg-go", "v3.1.20260901"))}}, cfg or config.Config())
        return comp

    def test_labels(self):
        comp = self.comps()
        self.assertEqual(comp["x-ui"]["label"], "v3.10.0 ↑")
        self.assertTrue(comp["x-ui"]["newer"])
        self.assertEqual(comp["xray"]["label"], "—")      # «v26.9.30» = «26.9.30»
        self.assertEqual(comp["hysteria"]["label"], "—")
        self.assertEqual(comp["amneziawg-go"]["label"], "v3.1.20260901 ↑")

    def test_not_checked(self):
        comp = self.comps(up={})
        self.assertTrue(all(c["label"] == "не проверено" and c["newer"] is None for c in comp.values()))

    def test_kernel_engine_ignores_amneziawg_go(self):
        comp = self.comps(config.Config({"AWG_ENGINE_ACTIVE": "kernel"}))
        self.assertEqual(comp["amneziawg-go"]["label"], "не используется (ядро)")
        self.assertEqual(comp["amneziawg-go"]["unused"], "kernel")
        self.assertFalse(comp["amneziawg-go"]["newer"])
        comp = self.comps(config.Config({"AWG_ENGINE_ACTIVE": "userspace"}))
        self.assertEqual(comp["amneziawg-go"]["label"], "v3.1.20260901 ↑")

    def test_check_never_goes_to_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("сеть на чтении")):
            d = upgrade.check(config.load())
        self.assertIn("label", d["components"]["x-ui"])

    def test_cli_check_upstream(self):
        from tests.test_cli import run_cli
        with mock.patch.object(upgrade, "latest_tag", side_effect=lambda repo: self.tags[repo]):
            code, out, _ = run_cli("upgrade", "--check-upstream", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["items"]["sing-box"]["tag"], "v1.14.2")
        with mock.patch.object(upgrade, "latest_tag", side_effect=OSError("нет сети")):
            code, _, _ = run_cli("upgrade", "--check-upstream", "--json")
        self.assertEqual(code, 1, "ни один не ответил — код 1 (юнит покажет сбой)")

    def test_timer_units(self):
        d = Path(__file__).resolve().parent.parent / "systemd"
        self.assertIn("zoo upgrade --check-upstream", (d / "zoo-upstream.service").read_text(encoding="utf-8"))
        self.assertIn("OnCalendar=daily", (d / "zoo-upstream.timer").read_text(encoding="utf-8"))
        self.assertIn("zoo-upstream.timer", (d / "enable.list").read_text(encoding="utf-8").split())


if __name__ == "__main__":
    unittest.main()
