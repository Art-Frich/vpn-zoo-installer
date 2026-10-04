import copy
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


if __name__ == "__main__":
    unittest.main()
