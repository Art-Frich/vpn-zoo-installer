import unittest

from zoolib import people, users


class SlugTest(unittest.TestCase):
    def test_translit_of_russian_names(self):
        for raw, want in (("Иван Петров", "ivan-petrov"), ("Мария", "mariya"), ("Щукин", "shchukin"),
                          ("Юлия Ёлкина", "yuliya-elkina"), ("Жданов", "zhdanov"), ("Ольга Н.", "olga-n"),
                          ("Дарья-Анна", "darya-anna"), ("Съезд Ь", "sezd"), ("Masha_K", "masha_k"),
                          ("  MIXed  Имя 42 ", "mixed-imya-42"), ("Іван Їжак", "ivan-yizhak")):
            self.assertEqual(people.slug(raw), want, raw)

    def test_slug_is_always_a_valid_name_or_empty(self):
        for raw in ("***", "   ", "名前", "-_-", "a" * 80, "é à ü", "x/../y", "ИВАН\x00\x1fПЕТРОВ", "<script>alert(1)</script>"):
            s = people.slug(raw)
            self.assertTrue(s == "" or users.NAME_RE.match(s), (raw, s))
        self.assertEqual(len(people.slug("a" * 80)), 32)
        self.assertEqual(people.slug("é à ü"), "e-a-u")
        self.assertEqual(people.slug("x/../y"), "x-y")

    def test_no_letters_means_empty(self):
        self.assertEqual(people.slug("***"), "")
        self.assertEqual(people.slug("名前"), "")


class LoginForTest(unittest.TestCase):
    def test_login_from_a_free_name_busy_one_and_system_one(self):
        self.assertEqual(people.login_for("Иван Петров"), ("ivan-petrov", ""))
        self.assertEqual(people.login_for("Иван Петров", {"ivan-petrov"}), ("ivan-petrov-2", "занято"))
        self.assertEqual(people.login_for("Иван Петров", {"ivan-petrov", "ivan-petrov-2"}), ("ivan-petrov-3", "занято"))
        self.assertEqual(people.login_for("owner")[1], "занято" if "owner" in users.SYSTEM_USERS else "")
        self.assertEqual(people.login_for("zoo-probe")[1], "служебное")
        self.assertEqual(people.login_for("***"), ("", ""), "в имени нет ни букв, ни цифр")
        self.assertEqual(people.login_for("Иван, Петров"), ("ivan-petrov", ""), "запятая — часть имени, не разделитель")

    def test_same_login_as_the_wizard_gives(self):
        for raw in ("Иван Петров", "Юлия Ёлкина", "Masha_K"):
            self.assertEqual(people.login_for(raw)[0], people.build(raw).rows[0].name)


class BuildTest(unittest.TestCase):
    def test_separators_semicolon_tab_comma(self):
        plan = people.build("Иван Петров; бухгалтерия\nМария\tотдел продаж\nОльга, склад, 2 этаж\nПётр")
        self.assertTrue(plan.ok)
        self.assertEqual([(r.name, r.note) for r in plan.rows], [
            ("ivan-petrov", "бухгалтерия"), ("mariya", "отдел продаж"), ("olga", "склад, 2 этаж"), ("petr", "")])
        self.assertEqual([r.display for r in plan.rows], ["Иван Петров", "Мария", "Ольга", "Пётр"])
        self.assertEqual([r.converted for r in plan.rows], [True] * 4)

    def test_devices_column(self):
        plan = people.build("Иван Петров; бухгалтерия; android,windows\nСергей; iPhone + Windows\n"
                            "Анна, склад, android, айфон\nОльга, склад, 2 этаж\nМаша; Android; склад\nПётр")
        self.assertEqual([(r.note, r.devices) for r in plan.rows], [
            ("бухгалтерия", ["android", "windows"]), ("", ["ios", "windows"]), ("склад", ["android", "ios"]),
            ("склад, 2 этаж", []), ("Android; склад", []), ("", [])])
        self.assertEqual(plan.entries()[0], ("ivan-petrov", "бухгалтерия", "Иван Петров", ("android", "windows")))
        self.assertEqual(people.device_titles(["ios", "windows"]), "iPhone, Windows")
        from zoolib import clients
        self.assertEqual(people.DEVICES, clients.load().platforms, "устройства — платформы каталога")

    def test_first_separator_wins(self):
        plan = people.build("masha, a; b")
        self.assertEqual(plan.pairs(), [("masha", "a; b")])
        plan = people.build("masha; a, b")
        self.assertEqual(plan.pairs(), [("masha", "a, b")])

    def test_plain_latin_keeps_note_as_written(self):
        plan = people.build("Masha; сестра\nkolya")
        self.assertEqual(plan.pairs(), [("masha", "сестра"), ("kolya", "")])
        self.assertFalse(any(r.converted for r in plan.rows), "регистр — не преобразование")

    def test_blank_lines_and_bom_skipped(self):
        plan = people.build("﻿\n  \nmasha\n\n\nkolya\n")
        self.assertEqual([r.name for r in plan.rows], ["masha", "kolya"])
        self.assertEqual([r.line for r in plan.rows], [1, 2])

    def test_dedupe_inside_list_and_against_registry(self):
        plan = people.build("Иван\nИван\nИван\nowner\nzoo-probe\nmasha", taken={"owner", "ivan-3"})
        self.assertEqual([r.name for r in plan.rows], ["ivan", "ivan-2", "ivan-4", "owner-2", "zoo-probe-2", "masha"])
        self.assertEqual([r.clash for r in plan.rows],
                         ["", "повтор в списке", "повтор в списке", "занято", "служебное", ""])
        self.assertEqual(len(plan.renamed), 4)
        self.assertTrue(plan.ok)

    def test_suffix_fits_in_32_characters(self):
        long = "a" * 40
        plan = people.build(f"{long}\n{long}\n{long}")
        names = [r.name for r in plan.rows]
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(users.NAME_RE.match(n) for n in names), names)
        self.assertEqual(names[1], "a" * 30 + "-2")

    def test_original_name_is_kept_apart_from_the_note_and_note_is_capped(self):
        plan = people.build("Иван Петров; " + "я" * 400)
        self.assertEqual(len(plan.rows[0].note), people.NOTE_MAX)
        self.assertEqual(plan.rows[0].note, "я" * people.NOTE_MAX, "имя в заметке не повторяется")
        self.assertEqual(plan.triples()[0][0::2], ("ivan-petrov", "Иван Петров"))

    def test_problems_block_the_plan(self):
        plan = people.build("masha\n***\n; заметка")
        self.assertFalse(plan.ok)
        self.assertEqual([r.problem for r in plan.rows], ["", "в имени нет ни букв, ни цифр", "нет имени"])
        self.assertIn("строка 2", plan.error)

    def test_limit(self):
        plan = people.build("\n".join(f"u{i}" for i in range(people.LINES_MAX)))
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.rows), people.LINES_MAX)
        plan = people.build("\n".join(f"u{i}" for i in range(people.LINES_MAX + 1)))
        self.assertFalse(plan.ok)
        self.assertIn("не больше 200", plan.error)
        self.assertEqual(len(plan.rows), people.LINES_MAX, "лишнее не разбирается")

    def test_long_line_is_cut_and_controls_removed(self):
        plan = people.build("masha; " + "x" * 5000 + "\x00\x1b[31m")
        self.assertTrue(plan.ok)
        self.assertLessEqual(len(plan.rows[0].note), people.NOTE_MAX)
        plan = people.build("ma\x00sha; no\x07te")
        self.assertEqual(plan.pairs(), [("ma-sha", "no te")])
        self.assertEqual(plan.triples(), [("ma-sha", "no te", "ma sha")])

    def test_hostile_text_is_just_text(self):
        plan = people.build('"><img src=x onerror=alert(1)>; <b>x</b>')
        self.assertTrue(plan.ok)
        self.assertEqual(plan.rows[0].name, "img-src-x-onerror-alert-1")
        self.assertTrue(users.NAME_RE.match(plan.rows[0].name))

    def test_thirty_three_office_names(self):
        names = ["Иванов Иван", "Петрова Анна", "Сидоров Пётр", "Смирнова Ольга"] * 8 + ["Кузнецов Алексей"]
        plan = people.build("\n".join(f"{n}; офис" for n in names))
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.rows), 33)
        self.assertEqual(len({r.name for r in plan.rows}), 33)
        self.assertEqual(plan.rows[0].name, "ivanov-ivan")
        self.assertEqual(plan.rows[4].name, "ivanov-ivan-2")


if __name__ == "__main__":
    unittest.main()
