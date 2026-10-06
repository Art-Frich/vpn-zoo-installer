"""bash-правки ревью: manifest_refresh Hysteria2 при ENABLE_HY2=0 и journald_limit с чужим SystemMaxUse.
Функции берутся из настоящих lib.sh и proto-hysteria2.sh, внешние команды подменены функциями."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import BASH, REPO, needs_bash

SCRIPTS = (REPO / "scripts").as_posix()


def run_bash(body: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "t.sh"
        script.write_text("set -euo pipefail\nNO_COLOR=1\n" + body, encoding="utf-8", newline="\n")
        return subprocess.run([BASH, script.as_posix()], capture_output=True, text=True, encoding="utf-8",
                              timeout=60)


@needs_bash
class Hy2ManifestRefreshTest(unittest.TestCase):
    def refresh(self, enable_hy2: str, enable_obfs: str) -> list[str]:
        r = run_bash(f'''
. "{SCRIPTS}/lib.sh"
. "{SCRIPTS}/lib/proto-hysteria2.sh"
_hy2_manifest_json() {{ echo "{{}}"; }}
manifest_write() {{ echo "write $1"; }}
manifest_del() {{ echo "del $1"; }}
ENABLE_HY2={enable_hy2}
ENABLE_HY2_OBFS={enable_obfs}
proto_hysteria2_manifest_refresh
''')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split("\n")[:-1]

    def test_obfs_manifest_only_when_hysteria_enabled(self):
        self.assertEqual(self.refresh("1", "1"), ["write hysteria2", "write hysteria2-obfs"])
        self.assertEqual(self.refresh("1", "0"), ["write hysteria2", "del hysteria2-obfs"])

    def test_no_obfs_manifest_when_hysteria_disabled(self):
        self.assertEqual(self.refresh("0", "1"), ["write hysteria2", "del hysteria2-obfs"])


@needs_bash
class JournaldLimitTest(unittest.TestCase):
    def run_limit(self, cat_config: str) -> str:
        r = run_bash(f'''
. "{SCRIPTS}/lib.sh"
systemd-analyze() {{ cat <<'EOF'
{cat_config}
EOF
}}
mkdir() {{ :; }}
install() {{ echo "INSTALL $*"; }}
systemctl() {{ return 0; }}
cmp() {{ return 1; }}
journald_limit
''')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_foreign_system_max_use_is_left_alone(self):
        out = self.run_limit("# /etc/systemd/journald.conf\n[Journal]\n#SystemMaxUse=\n"
                             "# /etc/systemd/journald.conf.d/10-host.conf\n[Journal]\nSystemMaxUse=2G\n")
        self.assertNotIn("INSTALL", out)
        self.assertIn("чужой", out)

    def test_foreign_value_next_to_ours_still_skips(self):
        out = self.run_limit("# /etc/systemd/journald.conf.d/10-host.conf\n[Journal]\nSystemMaxUse=2G\n"
                             "# /etc/systemd/journald.conf.d/50-vpn-zoo.conf\n[Journal]\nSystemMaxUse=500M\n")
        self.assertNotIn("INSTALL", out)

    def test_only_commented_default_or_our_dropin_installs(self):
        out = self.run_limit("# /etc/systemd/journald.conf\n[Journal]\n#SystemMaxUse=\n")
        self.assertIn("INSTALL", out)
        out = self.run_limit("# /etc/systemd/journald.conf\n[Journal]\n#SystemMaxUse=\n"
                             "# /etc/systemd/journald.conf.d/50-vpn-zoo.conf\n[Journal]\nSystemMaxUse=500M\n")
        self.assertIn("INSTALL", out)

    def test_rerun_with_our_own_comment_line_is_not_foreign(self):
        ours = ("# /etc/systemd/journald.conf.d/50-vpn-zoo.conf\n"
                "# vpn-zoo: потолок объёма системного журнала\n[Journal]\nSystemMaxUse=500M\n")
        out = self.run_limit(ours)
        self.assertIn("INSTALL", out)
        self.assertNotIn("чужой", out)
        out = self.run_limit("# /etc/systemd/journald.conf\n[Journal]\n#SystemMaxUse=\n" + ours)
        self.assertIn("INSTALL", out)
        self.assertNotIn("чужой", out)
        out = self.run_limit("# /etc/systemd/journald.conf.d/10-host.conf\n# note\n[Journal]\nSystemMaxUse=2G\n" + ours)
        self.assertNotIn("INSTALL", out, "чужое значение рядом с нашим комментарием по-прежнему чужое")


if __name__ == "__main__":
    unittest.main()
