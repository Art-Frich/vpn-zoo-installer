"""Блок адресов сервера из туннеля (D25) и исключение для MTProxy (D63): ACL Hysteria и finalRules Xray из
настоящего lib/routing.sh. Команда ip подменена функцией, шаблон Xray собирает jq (на Windows его обычно нет)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import BASH, REPO, needs_bash

SCRIPTS = (REPO / "scripts").as_posix()
SELF = "203.0.113.5"
SELF6 = "2001:db8::5"
MT_PORT = "34567"


def run_routing(body: str, **env: str) -> str:
    sets = "".join(f"{k}='{v}'\n" for k, v in env.items())
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "t.sh"
        script.write_text(f'''set -euo pipefail
NO_COLOR=1
. "{SCRIPTS}/lib.sh"
. "{SCRIPTS}/lib/routing.sh"
ip() {{ printf '1: lo    inet 127.0.0.1/8 scope host lo\\n2: eth0    inet {SELF}/24 brd x scope global eth0\\n2: eth0    inet6 {SELF6}/64 scope global\\n2: eth0    inet6 fe80::1/64 scope link\\n'; }}
SERVER_IP={SELF}
SSH_PORTS=22
{sets}{body}
''', encoding="utf-8", newline="\n")
        r = subprocess.run([BASH, script.as_posix()], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout


@needs_bash
class Hy2AclSelfTest(unittest.TestCase):
    def self_rules(self, **env: str) -> list[str]:
        out = run_routing("routing_hy2_acl_text", **env)
        return [ln for ln in out.splitlines() if SELF in ln or SELF6 in ln]

    def test_mtproto_port_allowed_before_reject(self):
        self.assertEqual(self.self_rules(ENABLE_MTPROTO="1", MTPROTO_PORT=MT_PORT), [
            f"direct({SELF6}/128, tcp/22)", f"direct({SELF6}/128, tcp/{MT_PORT})", f"reject({SELF6}/128)",
            f"direct({SELF}/32, tcp/22)", f"direct({SELF}/32, tcp/{MT_PORT})", f"reject({SELF}/32)"])

    def test_no_mtproto_rule_when_disabled_or_port_bad(self):
        for env in ({"ENABLE_MTPROTO": "0", "MTPROTO_PORT": MT_PORT}, {"MTPROTO_PORT": MT_PORT},
                    {"ENABLE_MTPROTO": "1", "MTPROTO_PORT": "abc"}, {"ENABLE_MTPROTO": "1", "MTPROTO_PORT": "70000"}):
            with self.subTest(env=env):
                rules = self.self_rules(**env)
                self.assertNotIn(f"direct({SELF}/32, tcp/{MT_PORT})", rules)
                self.assertEqual([r for r in rules if r.startswith("direct(")],
                                 [f"direct({SELF6}/128, tcp/22)", f"direct({SELF}/32, tcp/22)"])


@needs_bash
@unittest.skipUnless(shutil.which("jq"), "нет jq")
class XrayFinalRulesSelfTest(unittest.TestCase):
    OWNER_RULE = {"action": "block", "network": "udp", "port": "25"}

    def build(self, template: dict, **env: str) -> dict:
        tmpl = json.dumps(template).replace("'", "'\\''")
        return json.loads(run_routing(f"routing_xray_template_build '{tmpl}'", **env))

    @staticmethod
    def final_rules(cfg: dict) -> list[dict]:
        return next(o for o in cfg["outbounds"] if o["tag"] == "direct")["settings"]["finalRules"]

    def template(self) -> dict:
        return {"routing": {"rules": []}, "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {"finalRules": [self.OWNER_RULE]}},
            {"tag": "blocked", "protocol": "blackhole", "settings": {}}]}

    ON = {"ENABLE_MTPROTO": "1", "MTPROTO_PORT": MT_PORT, "MTPROTO_DOMAIN": "front.example.com"}

    def test_mtproto_allow_rule_between_ssh_and_block(self):
        rules = self.final_rules(self.build(self.template(), **self.ON))
        ips = [SELF6, SELF]
        self.assertEqual(rules[0], {"action": "allow", "network": "tcp", "port": "22", "ip": ips})
        self.assertEqual(rules[1], {"action": "allow", "network": "tcp", "port": MT_PORT, "ip": ips})
        self.assertEqual(rules[2], {"action": "block", "ip": ["geoip:private", *ips]})
        self.assertEqual(rules[3:], [self.OWNER_RULE])

    def test_sniffed_fake_tls_domain_goes_to_mtg(self):
        # мост TUIC подменяет адрес доменом из SNI: домен Fake-TLS + порт MTProxy → mtg на loopback
        cfg = self.build(self.template(), **self.ON)
        self.assertEqual(cfg["routing"]["rules"][0], {"type": "field", "ruleTag": "zoo-mtproto", "network": "tcp",
                                                      "port": MT_PORT, "domain": ["full:front.example.com"],
                                                      "outboundTag": "zoo-mtproto"})
        # без своего allow freedom Xray 26 для входов vless/hysteria/shadowsocks… сам блокирует частные адреса
        self.assertIn({"tag": "zoo-mtproto", "protocol": "freedom", "settings": {
            "redirect": f"127.0.0.1:{MT_PORT}",
            "finalRules": [{"action": "allow", "network": "tcp", "port": MT_PORT, "ip": ["127.0.0.1"]}]}},
            cfg["outbounds"])
        self.assertEqual(cfg["outbounds"][0]["tag"], "direct")

    def test_disable_removes_mtproto_rules(self):
        on = self.build(self.template(), **self.ON)
        off = self.build(on, **{**self.ON, "ENABLE_MTPROTO": "0"})
        rules = self.final_rules(off)
        self.assertEqual([r.get("port") for r in rules if r["action"] == "allow"], ["22"])
        self.assertEqual(len([r for r in rules if r["action"] == "block" and "geoip:private" in r.get("ip", [])]), 1)
        self.assertEqual(rules[-1], self.OWNER_RULE)
        self.assertNotIn("zoo-mtproto", [r.get("ruleTag") for r in off["routing"]["rules"]])
        self.assertNotIn("zoo-mtproto", [o["tag"] for o in off["outbounds"]])


if __name__ == "__main__":
    unittest.main()
