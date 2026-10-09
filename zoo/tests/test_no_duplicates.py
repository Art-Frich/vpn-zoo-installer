"""Сторож «без повторов»: на страницах админки подсказка (title=) не пересказывает видимый текст,
а одно и то же предложение не встречается в <main> дважды.

Правило для новых страниц: title= — только то, чего на экране нет; общая оговорка — один раз на страницу
(блок «Оговорки»/легенда), а не в каждой строке.
"""

import html
import json
import os
import re
import time
import unittest
from html.parser import HTMLParser
from unittest import mock

from tests.helpers import needs_bash
from tests.test_groupviews import GroupWebBase
from tests.test_history import report, result
from tests.test_live import seed_live
from tests.test_logs import BASE, FakeJournal, entry, write_lines
from zoolib import journal, logctl, traffic, users
from zoolib.journal import Event

# Что МОЖНО повторять. Список короткий: добавлять сюда только с причиной.
ALLOW_SENTENCE = (
    # имя лога или юнита: оно в списке источников и заголовком открытого лога — это название, а не пояснение
    re.compile(r"^[\w.-]+\.(log|service)$"),
)
ALLOW_TITLE = ()
# Взаимоисключающие «страницы» внутри страницы: на экране виден один вариант (платформа у человека — выбор из списка,
# платформа на карточке и инструкция платформы в редакторе группы — разные устройства одного человека, карточка для
# раздачи — отдельный лист бумаги другому человеку), и у каждого своё самодостаточное сообщение. Одно предложение
# в нескольких таких вариантах считается за одно, но внутри одного варианта и против остального текста страницы — повтор.
ALTERNATIVES = {"conn-plat": "platform", "hblock": "platform", "msg-edit": "platform", "hcard": "card"}

INLINE = {"a", "b", "strong", "em", "i", "code", "small", "mark", "kbd", "abbr", "label", "u", "s"}
VOID = {"br", "input", "img", "meta", "link", "hr", "source", "col", "wbr"}
SKIP = {"script", "style", "svg"}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s)).strip().casefold()


class Page(HTMLParser):
    """Достаёт из <main>: элементы с title и их видимый текст; текстовые блоки с номером «варианта»."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_main = 0
        self.skip = 0
        self.stack = []          # [tag, title, [text...], skipped, scope]
        self.titles = []         # (tag, title, text)
        self.text = [["", None]]
        self.alt = 0

    def scope(self):
        return next((e[4] for e in reversed(self.stack) if e[4]), None)

    def cut(self):
        self.text.append(["", self.scope()])

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.in_main += 1
        if not self.in_main:
            return
        a = dict(attrs)
        if tag in SKIP:
            self.skip += 1
        if tag not in INLINE:
            self.cut()
        if tag in VOID:
            if tag == "input" and a.get("type") in (None, "text") and a.get("value") and not self.skip:
                self.cut()
                self.text[-1][0] += a["value"]
                self.cut()
            return
        if a.get("title") and not self.skip:   # подсказка — тоже текст: одна и та же у многих строк — повтор
            self.cut()
            self.text[-1][0] += a["title"]
            self.cut()
        scope = None
        for cls in (a.get("class") or "").split():
            if cls in ALTERNATIVES:
                self.alt += 1
                scope = (ALTERNATIVES[cls], self.alt)
        self.stack.append([tag, a.get("title"), [], bool(self.skip), scope])
        if scope:
            self.cut()

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main -= 1
            return
        if not self.in_main:
            return
        if tag in SKIP and self.skip:
            self.skip -= 1
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                el = self.stack[i]
                del self.stack[i:]
                if el[1] is not None and not el[3]:
                    self.titles.append((tag, el[1], "".join(el[2])))
                if self.stack:
                    self.stack[-1][2].append("".join(el[2]))
                break
        if tag not in INLINE:
            self.cut()

    def handle_data(self, data):
        if not self.in_main or self.skip:
            return
        self.text[-1][0] += data
        if self.stack:
            self.stack[-1][2].append(data)


def sentences(blocks):
    for block, scope in blocks:
        for line in re.split(r"\n|[.!?]+\s|[.!?]$", block):
            s = norm(line).strip(" .:;,—-·")
            if len(s) >= 25:
                yield s, scope


def violations(body: str) -> list[str]:
    p = Page()
    p.feed(body)
    out = []
    for tag, title, text in p.titles:
        tt, vt = norm(title), norm(text)
        if not tt or not vt or any(r.search(tt) for r in ALLOW_TITLE):
            continue
        if tt == vt or tt.startswith(vt) or vt.startswith(tt):
            out.append(f"title повторяет текст: <{tag}> «{text.strip()[:60]}» / «{title[:60]}»")
    plain, alts = {}, {}
    for s, scope in sentences(p.text):
        if scope is None:
            plain[s] = plain.get(s, 0) + 1
        else:
            per = alts.setdefault(s, {}).setdefault(scope[0], {})
            per[scope[1]] = per.get(scope[1], 0) + 1
    for s in set(plain) | set(alts):
        n = plain.get(s, 0) + sum(max(per.values()) for per in alts.get(s, {}).values())
        if n > 1 and not any(r.search(s) for r in ALLOW_SENTENCE):
            out.append(f"предложение {n}× : «{s[:100]}»")
    return out


@needs_bash
class NoDuplicatesTest(GroupWebBase):
    def setUp(self):
        super().setUp()
        self.env.add_protocol("tuic", name="tuic v5 (3x-ui native)", layer="udp", port=8443)
        self.env.add_protocol("ss2022", name="shadowsocks-2022 (2022-blake3-aes-128-gcm)", port=8388)
        self.env.add_protocol("amneziawg", name="amneziawg 3.1 (профиль 2.0 — для клиентов awg 2.0+)",
                              layer="udp", port=51820, engine="awg", probe={"kind": "awg"})
        self.env.add_protocol("vless-xhttp", name="VLESS XHTTP + REALITY", port=2443)
        users.sync_users()
        users.add_user("lena")
        resp, _ = self.create_group(name="Семья", proto=["vless-reality", "hysteria2", "amneziawg", "tuic"],
                                    client__android="happ", client__windows="v2rayn", client__ios="happ",
                                    users_new="masha; сестра\nkolya\npetya")
        self.assertEqual(resp.status, 303)
        self.gid = [g for g in self.groups_json() if g["name"] == "Семья"][0]["id"]
        resp, _ = self.create_group(name="Офис", mode="admin", proto=["hysteria2", "vless-reality"],
                                    client__android="hiddify", client__ios="hiddify", client__windows="hiddify",
                                    users_new="Иванов Иван\nПетрова Анна")
        self.assertEqual(resp.status, 303)
        self.gid_admin = [g for g in self.groups_json() if g["name"] == "Офис"][0]["id"]
        self.name = "masha"
        now = time.time()
        for pid, rtt in (("vless-reality", 23.0), ("hysteria2", 31.0), ("amneziawg", 40.0)):
            seed_live(pid, now, rtt=rtt, age=240)
        con = traffic.connect()
        with con:
            # отметки подключений: masha — только что, kolya пропал (10 дней), остальные — ни разу
            seen = {("hysteria2", "masha"): traffic.Counter(50, 450, "", int(now), int(now) - 300),
                    ("xray", "kolya"): traffic.Counter(0, 0, "", int(now), int(now) - 10 * 86400)}
            traffic.store(con, [traffic.Delta("xray", "owner", 1000, 9000), traffic.Delta("xray", "masha", 100, 900),
                                traffic.Delta("hysteria2", "masha", 50, 450)], seen, int(now))
        con.close()
        jnow = int(now)
        con = journal.connect()
        with con:
            journal.store(con, [Event(jnow - 60, "port-scan", "45.155.205.10", 3389)] * 4
                          + [Event(jnow - 50, "ssh-auth", "91.240.118.5", 22)] * 2
                          + [Event(jnow - 40, "port-scan", "45.155.205.11", 81)] * 3
                          + [Event(jnow - 30, "hy2-auth", "185.220.101.5", 443)], jnow)
            con.execute("UPDATE ips SET cc = 'CN' WHERE ip = ?", ("45.155.205.10",))
        con.close()
        self.logs_fixture()
        reg = users.Registry.load()   # «Кому переслать»: все виды отметок
        users.mark_resend(reg, {"kolya": ["keys"], "petya": ["all"], "masha": ["apps:android", "apps:windows"]})
        reg.save()
        iso =time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        for tag, dev in (("mobile-mts", "pixel7"), ("cafe-wifi", "iphone")):
            rep = report([result("hysteria2", lat=40, down=30), result("vless-reality", "FREEZE_16K", down=None),
                          result("amneziawg", lat=55, down=20)], ts=iso, tag=tag, device=dev)
            self.c.post("/probe/compare", {"report": json.dumps(rep), "tag": tag, "device": dev})
        self.app.invalidate()

    def logs_fixture(self):
        logdir = self.env.root / "logs"
        logdir.mkdir()
        p = mock.patch.dict(os.environ, {"LOG_DIR": logdir.as_posix()})
        p.start()
        self.addCleanup(p.stop)
        for i in (1, 2, 3):
            f = logdir / f"install-2026100{i}-100000.log"
            write_lines(f, [f"строка {n}" for n in range(30)])
            os.utime(f, (BASE + i * 100000, BASE + i * 100000))
        fj = FakeJournal([entry(i, "zoo-web.service", f"web {i:03d}") for i in range(1, 50)])
        p = mock.patch("zoolib.logread._popen", side_effect=fj.popen)
        p.start()
        self.addCleanup(p.stop)
        jdir = self.env.root / "journal"
        (jdir / "m").mkdir(parents=True)
        (jdir / "m" / "system.journal").write_bytes(b"j" * 10_000)
        p = mock.patch.object(logctl, "JOURNALD_DIRS", (str(jdir),))
        p.start()
        self.addCleanup(p.stop)

    def pages(self):
        """(подпись, адрес для GET или готовый HTML шага мастера)."""
        g, n, ga = self.gid, self.name, self.gid_admin
        for path in ("/", "/users", "/users/" + n, "/users/owner", "/users?verify=1", "/users/" + n + "/delete",
                     "/groups", "/groups/main", "/groups/" + g, "/groups/" + ga, "/groups/" + g + "/delete",
                     "/connect/new", "/connect/done?group=" + g, "/connect/done?group=" + g + "&u=" + n,
                     "/connect/done?group=" + ga,
                     "/clients", "/apps", "/apps?user=" + n, "/apps?group=" + g, "/apps?group=" + ga, "/resend",
                     "/traffic", "/traffic?period=7d",
                     "/probe", "/probe?run=1", "/journal", "/journal?period=24h", "/journal?ip=45.155.205.10",
                     "/journal?own=1", "/logs", "/logs?src=file:install-20261002-100000.log",
                     "/logs?q=%D1%81%D1%82%D1%80%D0%BE%D0%BA%D0%B0&in=all", "/settings",
                     "/handoff?group=" + g, "/handoff?group=" + ga, "/handoff?u=masha,kolya"):
            yield path, path
        yield from self.wizard_pages()

    def wizard_pages(self):
        protos = ["vless-reality", "hysteria2", "amneziawg", "tuic"]
        for mode in ("self", "admin"):
            yield f"мастер: шаг 0 ({mode})", self.c.get("/connect/new?mode=" + mode)[1]
            yield f"мастер: протоколы ({mode})", self.wiz(0, go="custom", mode=mode)[1]
            yield f"мастер: приложения ({mode})", self.wiz(1, proto=protos, mode=mode)[1]
            yield f"мастер: люди ({mode})", self.wiz(2, proto=protos, mode=mode, client__android="happ",
                                                      client__windows="v2rayn", client__ios="happ",
                                                      allow_mode="common")[1]

    def test_no_redundant_text(self):
        bad = {}
        for label, what in self.pages():
            body = what
            if what == label:
                resp, body = self.c.get(what)
                self.assertEqual(resp.status, 200, label)
            if found := violations(body):
                bad[label] = found
        if bad:
            lines = [f"{k}: " + "; ".join(v) for k, v in bad.items()]
            self.fail("повторы:\n" + "\n".join(lines))


class GuardTest(unittest.TestCase):
    """Сторож сам ловит то, что должен, и пропускает допустимое."""

    def check(self, main):
        return violations(f"<html><body><main>{main}</main></body></html>")

    def test_title_that_repeats_the_text(self):
        self.assertTrue(self.check('<div title="CPU">CPU</div>'))
        self.assertTrue(self.check('<button title="Сравнить с самопроверкой">Сравнить</button>'))
        self.assertTrue(self.check('<span title="Hysteria2">Hysteria2 + Salamander</span>'))
        self.assertFalse(self.check('<span title="работает 4 мин назад">в сети</span>'))
        self.assertFalse(self.check('<button title="Что это значит"></button>'))

    def test_repeated_sentence(self):
        line = "Эта оговорка слишком длинная, чтобы повторяться"
        self.assertTrue(self.check(f"<p>{line}.</p><ul><li>{line}</li></ul>"))
        self.assertTrue(self.check(f'<p>{line}.</p><span title="{line}">?</span>'))
        self.assertFalse(self.check("<p>Короткое</p><p>Короткое</p>"))

    def test_platform_panels_are_alternatives(self):
        line = "Проверьте: откройте заблокированный сайт — он должен открыться"
        panels = "".join(f'<section class="conn-plat"><pre>{line}</pre></section>' for _ in range(3))
        self.assertFalse(self.check(panels))
        self.assertTrue(self.check(panels + f"<p>{line}</p>"), "но не против остального текста страницы")
        self.assertTrue(self.check(f'<section class="conn-plat"><pre>{line}\n{line}</pre></section>'))
