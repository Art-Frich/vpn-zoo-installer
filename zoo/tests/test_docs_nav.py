"""Документы называют страницы админки так же, как меню: README, REFERENCE, USER-GUIDE и RISK-REDUCTION.

Ловит старые имена вроде «админка → «Приложения»» для списка «через VPN» (страница давно называется «Через VPN»,
а «Приложения» — справочник VPN-приложений).
"""

import re
import unittest

from zoolib import userguide
from zoolib.web import groupviews, handoffviews, resendviews
from zoolib.web.views import NAV

ROOT = userguide.GUIDE_FILE.parent.parent
DOCS = ("README.md", "docs/REFERENCE.md", "docs/USER-GUIDE.md", "docs/RISK-REDUCTION.md")
MENU = {label for _, label in NAV}
# страницы вне меню: на них ведут кнопки и ссылки
OFF_MENU = {groupviews.TITLE, resendviews.TITLE, handoffviews.TITLE}
# «страница «X»», «админка → «X»», «в админке «X»», «пункт меню «X»»: X — название страницы в именительном падеже;
# «Настройки → Версии» в одних кавычках — страница и блок на ней
# (страница приложения в магазине — не админка)
PAGE_REF = re.compile(r"(?:[Сс]траниц[аыуе]|[Аа]дминк[аеиу]\s*(?:→|,)|в админке|пункт\w* меню)\s*\**«([^»]+)»"
                      r"(?!\s+в\s+(?:\w+\s+)?(?:App Store|Google Play|RuStore|Microsoft Store))")
# «на «Обзоре»» в документах для админа (README, REFERENCE): страница в предложном падеже
ON_PAGE = re.compile(r"(?<![\w-])на\s*\**«([А-ЯЁA-Z][^»]*)»")
LOCATIVE = {"Обзоре": "Обзор", "Пользователях": "Пользователи", "Группах": "Группы", "Проверке": "Проверка",
            "Настройках": "Настройки", "Атаках": "Атаки", "Логах": "Логи", "Трафике": "Трафик",
            "Приложениях": "Приложения"}
ADMIN_DOCS = ("README.md", "docs/REFERENCE.md")
# таблица страниц в REFERENCE: «| **Обзор** (`/`) | … |»
PAGE_ROW = re.compile(r"^\| \*\*([^*]+)\*\* \(`(/[a-z]*)`\) \|", re.M)


def page_name(ref: str) -> str:
    return ref.split("→")[0].strip()


class DocsNavTest(unittest.TestCase):
    def setUp(self):
        if not (ROOT / "docs" / "REFERENCE.md").is_file():
            self.skipTest("нет docs/ рядом с zoo (установленная копия)")
        self.texts = {name: (ROOT / name).read_text(encoding="utf-8") for name in DOCS}

    def test_page_names_match_menu(self):
        bad = []
        for name, text in self.texts.items():
            for m in PAGE_REF.finditer(text):
                page = page_name(m.group(1))
                if page not in MENU | OFF_MENU:
                    line = text.count("\n", 0, m.start()) + 1
                    bad.append(f"{name}:{line}: «{page}»")
        self.assertEqual(bad, [], "страницы нет в меню админки: " + "; ".join(bad))

    def test_on_page_names_match_menu(self):
        self.assertLessEqual(set(LOCATIVE.values()), MENU)
        bad = []
        for name in ADMIN_DOCS:
            text = self.texts[name]
            for m in ON_PAGE.finditer(text):
                page = page_name(m.group(1))
                if LOCATIVE.get(page, page) not in MENU | OFF_MENU:
                    line = text.count("\n", 0, m.start()) + 1
                    bad.append(f"{name}:{line}: «{page}»")
        self.assertEqual(bad, [], "страницы нет в меню админки: " + "; ".join(bad))

    def test_reference_lists_every_menu_page_in_order(self):
        rows = PAGE_ROW.findall(self.texts["docs/REFERENCE.md"])
        self.assertEqual([(path, label) for label, path in rows], list(NAV),
                         "таблица страниц в REFERENCE расходится с меню (views.NAV)")

    def test_lists_page_is_not_called_applications(self):
        # списки «через VPN» правятся на «Через VPN» (/apps); «Приложения» (/clients) — каталог VPN-приложений
        paths = dict(NAV)
        self.assertEqual(paths["/apps"], "Через VPN")
        self.assertEqual(paths["/clients"], "Приложения")
        for name, text in self.texts.items():
            self.assertNotRegex(text, r"«Приложения»\s*(?:\(списки|→ пользователь|\(что идёт через VPN)", name)

    def test_regex_catches_stale_names(self):
        sample = ("админка → «Клиенты»; страница «Пользователи»; «Настройки → Версии» на странице «Настройки → Версии»; "
                  "страница «Браузер Brave» в российском App Store")
        found = [page_name(m.group(1)) for m in PAGE_REF.finditer(sample)]
        self.assertEqual(found, ["Клиенты", "Пользователи", "Настройки"])
        found = [m.group(1) for m in ON_PAGE.finditer("на «Обзоре» и на «Клиентах»; Windows-на «x»")]
        self.assertEqual(found, ["Обзоре", "Клиентах"])


if __name__ == "__main__":
    unittest.main()
