"""tools/zoo-admin.sh и .ps1: ответ сервера (порт, ссылка) проверяется до ssh-туннеля и браузера,
путь ключа с пробелом доходит до ssh одним аргументом. ssh, curl, браузер подменены."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import BASH, REPO, needs_bash

POWERSHELL = shutil.which("powershell")
GOOD = {"port": "18080", "link": "http://127.0.0.1:18080/login?once=abc_D-1.1700000000.0f9a"}
EVIL = [
    {"port": "1 -oProxyCommand=calc.exe", "link": "http://127.0.0.1:1/login?once=a"},
    {"port": "18080", "link": "calc.exe"},
    {"port": "18080", "link": "\\\\host\\share\\x.exe"},
    {"port": "18080", "link": "http://127.0.0.1:18080/login?once=a b"},
    {"port": "18080", "link": "http://evil.example:18080/login?once=a"},
    {"port": "99999", "link": "http://127.0.0.1:99999/login?once=a"},
]


@needs_bash
class AdminShTest(unittest.TestCase):
    def run_sh(self, info: dict) -> tuple[subprocess.CompletedProcess, str]:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.log"
            log.touch()
            script = Path(tmp) / "t.sh"
            script.write_text(f'''
LOG="{log.as_posix()}"
ssh() {{ echo "ssh $*" >> "$LOG"; [ "$(grep -c '^ssh' "$LOG")" != 1 ] || printf '%s\\n' "$INFO"; }}
curl() {{ return 1; }}
open() {{ echo "OPEN $*" >> "$LOG"; }}
xdg-open() {{ echo "OPEN $*" >> "$LOG"; }}
cmd.exe() {{ echo "OPEN $*" >> "$LOG"; }}
export LOG
export -f ssh curl open xdg-open cmd.exe
bash "{(REPO / "tools" / "zoo-admin.sh").as_posix()}" root@h
''', encoding="utf-8", newline="\n")
            env = dict(os.environ, INFO=json.dumps(info), ZOO_ADMIN_NO_OPEN="0")
            r = subprocess.run([BASH, script.as_posix()], capture_output=True, text=True, encoding="utf-8",
                               env=env, timeout=60)
            return r, log.read_text(encoding="utf-8")

    def test_good_answer_opens_tunnel_and_link(self):
        r, calls = self.run_sh(GOOD)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-L 18080:127.0.0.1:18080 root@h", calls)
        self.assertIn("OPEN", calls)

    def test_bad_answer_stops_before_tunnel_and_browser(self):
        for info in EVIL:
            with self.subTest(info=info):
                r, calls = self.run_sh(info)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(calls.count("ssh "), 1, calls)
                self.assertNotIn("OPEN", calls)


@unittest.skipUnless(POWERSHELL, "нет Windows PowerShell")
class AdminPs1Test(unittest.TestCase):
    def run_ps(self, info: dict, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.log"
            wrapper = Path(tmp) / "t.ps1"
            wrapper.write_text('''
$global:n = 0
$global:up = $false
function ssh { $global:n++; $global:LASTEXITCODE = 0; if ($global:n -eq 1) { $env:ZINFO } }
function Invoke-WebRequest { if (-not $global:up) { throw 'down' } }
function Start-Sleep { }
function Start-Process { param($FilePath, $ArgumentList, $WindowStyle)
    Add-Content -LiteralPath $env:ZLOG -Value "SP|$FilePath|$ArgumentList" -Encoding UTF8
    $global:up = $true }
& $env:ZSCRIPT @args
exit $LASTEXITCODE
''', encoding="utf-8-sig", newline="\n")
            env = dict(os.environ, ZINFO=json.dumps(info), ZLOG=str(log),
                       ZSCRIPT=str(REPO / "tools" / "zoo-admin.ps1"))
            r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
                                *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=env, timeout=120)
            calls = log.read_text(encoding="utf-8-sig").splitlines() if log.exists() else []
            return r, calls

    def test_identity_with_space_stays_one_argument(self):
        r, calls = self.run_ps(GOOD, "root@h", "-Identity", "C:\\Users\\Ivan Petrov\\.ssh\\id")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].startswith("SP|ssh|"), calls)
        self.assertIn('-i "C:\\Users\\Ivan Petrov\\.ssh\\id" -N', calls[0])
        self.assertIn("-L 18080:127.0.0.1:18080 root@h", calls[0])
        self.assertEqual(calls[1], "SP|" + GOOD["link"] + "|")

    def test_bad_answer_stops_before_tunnel_and_browser(self):
        for info in EVIL:
            with self.subTest(info=info):
                r, calls = self.run_ps(info, "root@h")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
