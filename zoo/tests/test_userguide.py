"""docs/USER-GUIDE.md: разделы по платформам собраны из каталога клиентов и не расходятся с инструкциями админки."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from zoolib import cli, clients, userguide


class UserGuideTest(unittest.TestCase):
    def setUp(self):
        if not userguide.GUIDE_FILE.is_file():
            self.skipTest("нет docs/ рядом с zoo (установленная копия)")
        self.text = userguide.GUIDE_FILE.read_text(encoding="utf-8")
        self.cat = clients.load()

    def test_committed_guide_equals_generator_output(self):
        self.assertEqual(userguide.update(self.text, self.cat), self.text,
                         "docs/USER-GUIDE.md отстал от zoo/data/clients.json: zoo docs --user-guide")

    def test_guide_has_catalog_steps_and_no_admin_console(self):
        for plat, g in self.cat.raw["guide"].items():
            for cid in g["apps"]:
                c = self.cat.client(cid)
                for step in self.cat.setup(c, plat):
                    self.assertIn(userguide._neutral(step), self.text, (cid, plat))
        self.assertIn(self.cat.report("guide"), self.text)
        for gone in ("sudo zoo", "Для владельца", "как обычно.\n", "я пришлю", "ещё 4"):
            self.assertNotIn(gone, self.text)
        self.assertIn("«Устаревшая защита TUN»", self.text)
        self.assertIn("«Очистить системный прокси»", self.text)
        self.assertIn("«РФ напрямую»", self.text)

    def test_missing_markers_and_check_mode(self):
        with self.assertRaises(userguide.GuideError):
            userguide.update("# без меток\n", self.cat)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "guide.md"
            stale = self.text.replace("«Устаревшая защита TUN»", "«что-то старое»", 1)
            f.write_text(stale, encoding="utf-8", newline="\n")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(cli.main(["docs", "--user-guide", "--check", "--file", str(f)]), 1)
                self.assertEqual(f.read_text(encoding="utf-8"), stale, "--check файл не трогает")
                self.assertEqual(cli.main(["docs", "--user-guide", "--file", str(f)]), 0)
                self.assertEqual(f.read_text(encoding="utf-8"), self.text)
                self.assertEqual(cli.main(["docs", "--user-guide", "--check", "--file", str(f)]), 0)


if __name__ == "__main__":
    unittest.main()
