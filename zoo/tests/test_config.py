import os
import shutil
import subprocess
import unittest

from tests.helpers import BASH, ZooEnv, needs_bash
from zoolib import config

TRICKY = ["простое", "с пробелом и 'кавычкой'", "a'b''c", 'двойные "кавычки"', "$HOME `id` \\n \\t",
          "tab\there", "", "#не комментарий", "x=y=z"]


class ParseTest(unittest.TestCase):
    def test_formats(self):
        text = "\n".join([
            "# комментарий",
            "",
            "A='single'",
            "B='it'\\''s'",
            'C="legacy double"',
            "D=plain",
            "export E='exported'",
            "F=",
            "G='x' # хвост",
            "A='second wins'",
        ])
        values, errors = config.parse_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(values, {"A": "second wins", "B": "it's", "C": "legacy double", "D": "plain",
                                  "E": "exported", "F": "", "G": "x"})

    def test_bad_lines_are_reported(self):
        values, errors = config.parse_text("OK='1'\nlower='x'\nBROKEN='unterminated\n")
        self.assertEqual(values, {"OK": "1"})
        self.assertEqual(len(errors), 2)

    def test_flag_and_int(self):
        c = config.Config({"ENABLE_X": "1", "ENABLE_Y": "0", "PORT": "443", "BAD": "x"})
        self.assertTrue(c.flag("ENABLE_X"))
        self.assertFalse(c.flag("ENABLE_Y"))
        self.assertTrue(c.flag("ENABLE_Z", default=True))
        self.assertEqual(c.int("PORT"), 443)
        self.assertIsNone(c.int("BAD"))

    def test_ssh_login_port(self):
        self.assertEqual(config.Config({}).ssh_login_port(), "22")
        self.assertEqual(config.Config({"SSH_PORTS": "2222,22"}).ssh_login_port(), "2222")
        # фаза 01b до подтверждения: порты оба, входить — на старый
        c = config.Config({"SSH_PORTS": "22,30366", "SSH_LOGIN_PORT": "22"})
        self.assertEqual(c.ssh_login_port(), "22")
        self.assertEqual(config.Config({"SSH_PORTS": "22", "SSH_LOGIN_PORT": "x"}).ssh_login_port(), "22")

    def test_missing_file(self):
        with ZooEnv() as env:
            os.remove(env.etc / "config.env")
            cfg = config.load()
            self.assertEqual(cfg.values, {})


class SetTest(unittest.TestCase):
    def test_roundtrip_python(self):
        with ZooEnv() as env:
            for i, v in enumerate(TRICKY):
                config.config_set(f"K{i}", v)
            config.config_set("K0", "заменено")
            cfg = config.load()
            self.assertEqual(cfg["K0"], "заменено")
            for i, v in enumerate(TRICKY[1:], 1):
                self.assertEqual(cfg[f"K{i}"], v)
            self.assertEqual(cfg["SERVER_IP"], "10.0.0.1")
            text = (env.etc / "config.env").read_text(encoding="utf-8")
            self.assertEqual(text.count("K0="), 1)
            self.assertTrue(text.startswith("# test"))

    def test_rejects_bad_input(self):
        with ZooEnv():
            with self.assertRaises(config.ConfigError):
                config.config_set("bad-key", "v")
            with self.assertRaises(config.ConfigError):
                config.config_set("K", "a\nb")

    def test_new_file_gets_header(self):
        with ZooEnv() as env:
            os.remove(env.etc / "config.env")
            config.config_set("A", "1")
            self.assertEqual((env.etc / "config.env").read_text(encoding="utf-8").splitlines(),
                             [config.HEADER, "A='1'"])

    @needs_bash
    def test_python_writes_bash_reads(self):
        with ZooEnv():
            for i, v in enumerate(TRICKY):
                config.config_set(f"K{i}", v)
            for i, v in enumerate(TRICKY):
                out = subprocess.run([BASH, "-c", 'set -u; . "$CONFIG_FILE"; printf %s "${!1}"', "x", f"K{i}"],
                                     capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(out.stdout, v, f"K{i}")

    @unittest.skipUnless(BASH and shutil.which("flock"), "нужны bash и flock")
    def test_bash_writes_python_reads(self):
        with ZooEnv() as env:
            for i, v in enumerate(TRICKY):
                subprocess.run([BASH, "-c", '. "$1/lib.sh"; config_set "$2" "$3"', "x",
                                env.scripts.as_posix(), f"K{i}", v], check=True)
            cfg = config.load()
            for i, v in enumerate(TRICKY):
                self.assertEqual(cfg[f"K{i}"], v, f"K{i}")


if __name__ == "__main__":
    unittest.main()
