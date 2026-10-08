"""tools/zoo-admin.sh и .ps1: ответ сервера (порт, ссылка) проверяется до ssh-туннеля и браузера,
путь ключа с пробелом доходит до ssh одним аргументом, --setup проверяет аргументы и повторяем.
ssh, ssh-keygen, ssh-copy-id, curl, браузер подменены, HOME — во временной папке."""

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
    def run_sh(self, info: dict, args: str = "root@h") -> tuple[subprocess.CompletedProcess, str]:
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
bash "{(REPO / "tools" / "zoo-admin.sh").as_posix()}" {args}
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

    def test_no_arguments_means_host_alias_zoo(self):
        r, calls = self.run_sh(GOOD, "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ssh zoo zoo web --info", calls)
        self.assertIn("-L 18080:127.0.0.1:18080 zoo", calls)

    def test_ssh_options_without_host_keep_alias_zoo(self):
        r, calls = self.run_sh(GOOD, "-p 2222")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ssh -p 2222 zoo zoo web --info", calls)
        self.assertIn("-p 2222 -f -N", calls)


# ssh-keygen и ssh-copy-id только пишут журнал; ssh выполняет команду записи ключа у «сервера» (своя папка HOME)
STUBS = {
    "ssh-keygen": r'''#!/usr/bin/env bash
echo "ssh-keygen $*" >> "$STUB_LOG"
f=""; while [ $# -gt 0 ]; do [ "$1" != "-f" ] || f="$2"; shift; done
mkdir -p "$(dirname "$f")"
echo PRIVATE > "$f"
if [ -n "${STUB_CRLF:-}" ]; then printf 'ssh-ed25519 AAAATESTKEY zoo-admin@test\r\n' > "$f.pub"
else echo 'ssh-ed25519 AAAATESTKEY zoo-admin@test' > "$f.pub"; fi
''',
    "ssh-copy-id": r'''#!/usr/bin/env bash
echo "ssh-copy-id $*" >> "$STUB_LOG"
''',
    "ssh": r'''#!/usr/bin/env bash
echo "ssh $*" >> "$STUB_LOG"
[ -z "${STUB_SSH_RC:-}" ] || exit "$STUB_SSH_RC"
for a in "$@"; do
    case "$a" in *zoo-key.tmp*) HOME="$SERVER_HOME" bash -c "$a"; exit $? ;; esac
done
''',
}


@needs_bash
class AdminShSetupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.server = self.tmp / "server"
        self.stubs = self.tmp / "bin"
        for d in (self.home, self.server, self.stubs):
            d.mkdir()
        self.log = self.tmp / "stub.log"
        self.log.touch()
        for name, body in STUBS.items():
            (self.stubs / name).write_text(body, encoding="utf-8", newline="\n")
            (self.stubs / name).chmod(0o755)   # на Linux неисполняемая заглушка пропускается и зовётся настоящий ssh

    def run_setup(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, HOME=self.home.as_posix(), SERVER_HOME=self.server.as_posix(),
                   STUB_LOG=self.log.as_posix(), PATH=self.stubs.as_posix() + os.pathsep + os.environ["PATH"],
                   **extra)
        return subprocess.run([BASH, (REPO / "tools" / "zoo-admin.sh").as_posix(), "--setup", *args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_bad_arguments_stop_before_any_change(self):
        bad = [
            [], ["rootserver"], ["root@"], ["@h"], ["root@bad host"], ["root@h;reboot"], ["-oProxyCommand=x@h"],
            ["root@h", "-p", "abc"], ["root@h", "-p", "0"], ["root@h", "-p", "70000"], ["root@h", "-p"],
            ["root@h", "--name", "a b"], ["root@h", "--name", "1x"], ["root@h", "--name", "../x"],
            ["root@h", "extra@h2"], ["root@h", "--bogus"],
        ]
        for args in bad:
            with self.subTest(args=args):
                r = self.run_setup(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.home / ".ssh").exists())

    def test_good_targets_pass_validation(self):
        for target in ["root@203.0.113.5", "admin@vpn.example.com", "root@[2001:db8::1]", "root@2001:db8::1"]:
            with self.subTest(target=target):
                r = self.run_setup(target, "-p", "2222", ZOO_ADMIN_NO_COPY_ID="1")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_setup_with_ssh_copy_id(self):
        r = self.run_setup("root@203.0.113.5", "-p", "2222")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertEqual(sum(c.startswith("ssh-keygen") for c in calls), 1)
        self.assertTrue(any(c.startswith("ssh-copy-id") and "-p 2222 root@203.0.113.5" in c for c in calls), calls)
        cfg = (self.home / ".ssh" / "config").read_text(encoding="utf-8")
        self.assertEqual(cfg, "Host zoo\n    HostName 203.0.113.5\n    User root\n    Port 2222\n"
                              "    IdentityFile ~/.ssh/id_ed25519\n    ServerAliveInterval 20\n")
        installed = self.home / ".local" / "bin" / "zoo-admin"
        self.assertEqual(installed.read_bytes(), (REPO / "tools" / "zoo-admin.sh").read_bytes())
        self.assertIn("ssh zoo", r.stdout)
        self.assertNotIn("PRIVATE", r.stdout + r.stderr)

    def test_setup_twice_changes_nothing_more(self):
        for _ in range(2):
            r = self.run_setup("root@203.0.113.5", ZOO_ADMIN_NO_COPY_ID="1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sum(c.startswith("ssh-keygen") for c in self.calls()), 1)
        keys = (self.server / ".ssh" / "authorized_keys").read_text(encoding="utf-8").splitlines()
        self.assertEqual(keys, ["ssh-ed25519 AAAATESTKEY zoo-admin@test"])
        self.assertFalse((self.server / ".ssh" / "zoo-key.tmp").exists())
        cfg = (self.home / ".ssh" / "config").read_text(encoding="utf-8")
        self.assertEqual(cfg.count("Host zoo"), 1)
        self.assertIn("Port 22\n", cfg)

    def test_crlf_in_key_is_stripped_on_server(self):
        r = self.run_setup("root@203.0.113.5", ZOO_ADMIN_NO_COPY_ID="1", STUB_CRLF="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        raw = (self.server / ".ssh" / "authorized_keys").read_bytes()
        self.assertEqual(raw, b"ssh-ed25519 AAAATESTKEY zoo-admin@test\n")

    def test_existing_key_and_config_entry_are_kept(self):
        ssh = self.home / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519").write_text("MINE", encoding="utf-8")
        (ssh / "id_ed25519.pub").write_text("ssh-ed25519 MINEKEY me@pc\n", encoding="utf-8")
        mine = "Host zoo\n    HostName 198.51.100.1\n"
        (ssh / "config").write_text(mine, encoding="utf-8")
        r = self.run_setup("root@203.0.113.5", ZOO_ADMIN_NO_COPY_ID="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(c.startswith("ssh-keygen") for c in self.calls()))
        self.assertEqual((ssh / "id_ed25519").read_text(encoding="utf-8"), "MINE")
        self.assertEqual((ssh / "config").read_text(encoding="utf-8"), mine)
        self.assertEqual((self.server / ".ssh" / "authorized_keys").read_text(encoding="utf-8"),
                         "ssh-ed25519 MINEKEY me@pc\n")

    def test_custom_name_is_added_next_to_existing_entries(self):
        ssh = self.home / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host zoo\n    HostName 198.51.100.1\n", encoding="utf-8")
        r = self.run_setup("root@203.0.113.5", "--name", "office", ZOO_ADMIN_NO_COPY_ID="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = (ssh / "config").read_text(encoding="utf-8")
        self.assertIn("\n\nHost office\n    HostName 203.0.113.5\n", cfg)
        self.assertEqual(cfg.count("Host zoo"), 1)
        self.assertIn("zoo-admin office", r.stdout)

    def test_failed_key_copy_leaves_config_and_command_alone(self):
        r = self.run_setup("root@203.0.113.5", ZOO_ADMIN_NO_COPY_ID="1", STUB_SSH_RC="255")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.home / ".ssh" / "config").exists())
        self.assertFalse((self.home / ".local" / "bin" / "zoo-admin").exists())


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


@unittest.skipUnless(POWERSHELL, "нет Windows PowerShell")
class AdminPs1SetupTest(unittest.TestCase):
    WRAPPER = r'''
$tmp = $env:ZTMP
if (-not $HOME.StartsWith($tmp)) { Write-Host "HOME вне временной папки: $HOME"; exit 99 }
$PROFILE = Join-Path $tmp 'Documents\WindowsPowerShell\profile.ps1'
function ssh { Add-Content -LiteralPath $env:ZLOG -Value ('SSH|' + ($args -join ' ') + '|' + (@($input) -join '~')); $global:LASTEXITCODE = 0 }
function ssh-keygen {
    Add-Content -LiteralPath $env:ZLOG -Value ('KEYGEN|' + ($args -join ' '))
    $f = $args[[array]::IndexOf($args, '-f') + 1]
    Set-Content -LiteralPath $f -Value 'PRIVATE'
    Set-Content -LiteralPath "$f.pub" -Value 'ssh-ed25519 AAAATESTKEY zoo-admin@test'
    $global:LASTEXITCODE = 0
}
& $env:ZSCRIPT @args
exit $LASTEXITCODE
'''

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.log = self.tmp / "calls.log"
        self.wrapper = self.tmp / "t.ps1"
        self.wrapper.write_text(self.WRAPPER, encoding="utf-8-sig", newline="\n")

    def run_setup(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, USERPROFILE=str(self.tmp), HOMEDRIVE=self.tmp.drive, HOMEPATH=str(self.tmp)[len(self.tmp.drive):],
                   ZTMP=str(self.tmp), ZLOG=str(self.log), ZSCRIPT=str(REPO / "tools" / "zoo-admin.ps1"))
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.wrapper),
                               *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=120)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8-sig").splitlines() if self.log.exists() else []

    def test_bad_arguments_stop_before_any_change(self):
        bad = [["-Setup", "rootserver"], ["-Setup", "root@"], ["-Setup", "root@bad host"], ["-Setup", "root@h;reboot"],
               ["-Setup", "root@h", "-Port", "70000"], ["-Setup", "root@h", "-Port", "-5"],
               ["-Setup", "root@h", "-Name", "a b"], ["-Setup", "root@h", "-Name", "1x"]]
        for args in bad:
            with self.subTest(args=args):
                r = self.run_setup(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.tmp / ".ssh").exists())

    def test_setup_is_idempotent(self):
        for _ in range(2):
            r = self.run_setup("-Setup", "root@203.0.113.5", "-Port", "2222")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertEqual(sum(c.startswith("KEYGEN|") for c in calls), 1, calls)
        copies = [c for c in calls if c.startswith("SSH|-p 2222 root@203.0.113.5 umask 077;")]
        self.assertEqual(len(copies), 2, calls)
        self.assertTrue(all(c.endswith("|ssh-ed25519 AAAATESTKEY zoo-admin@test") for c in copies), copies)
        self.assertNotIn('"', copies[0])
        cfg = (self.tmp / ".ssh" / "config").read_text(encoding="utf-8")
        self.assertEqual(cfg.count("Host zoo"), 1)
        self.assertIn("HostName 203.0.113.5", cfg)
        self.assertIn("Port 2222", cfg)
        self.assertEqual((self.tmp / "zoo-admin.ps1").read_bytes(), (REPO / "tools" / "zoo-admin.ps1").read_bytes())
        profile = (self.tmp / "Documents" / "WindowsPowerShell" / "profile.ps1").read_text(encoding="utf-8-sig")
        self.assertEqual(profile.count("function zoo-admin"), 1)
        self.assertIn('-File "$HOME\\zoo-admin.ps1" @args', profile)


if __name__ == "__main__":
    unittest.main()
