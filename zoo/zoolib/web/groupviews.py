"""Мастер «Подключить людей» (/connect/new) и страницы групп (/groups): веб поверх zoolib.groups.

Мастер — один адрес и четыре шага без перезагрузки (форма data-swap): вариант (кто ставит приложения и готовые
варианты) → приложения (устройство → одна строка; протоколы — подшаг «Своего набора» и ссылки «сменить протоколы») →
люди → раздача. Состояние между шагами лежит в скрытых полях формы (на сервере ничего не хранится), каждый шаг
проверяется сервером, запись — одна, в конце шага «Люди». Страница группы — те же блоки в одной форме.
Принцип: у каждой настройки одно место правки, одно название и один формат; на остальных экранах она только для
чтения, со ссылкой «изменить →» туда."""

from __future__ import annotations

import sqlite3
import statistics
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import allowlist, clients, groups, manifests, people, protolib, users
from ..fsutil import LockTimeout
from ..probe import history, live, rank, verdicts
from . import allowviews, clientviews, distviews, handoffviews, userviews
from .html import Markup, badge, card, csrf_input, post_button, t, table
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

TITLE = "Подключить людей"
PLAT_NAMES = {"android": "Android", "windows": "Windows"}
STEPS = ("Вариант", "Приложения", "Люди", "Раздача")
PROTO_STEP = "Протоколы"   # подшаг: только у «Своего набора» и по ссылке «сменить протоколы»
MODE_LABELS = {"self": "Ставят сами", "admin": "Ставит ИТ"}
MODE_WORDS = {"self": "ставят сами", "admin": "ставит ИТ"}
MODE_WHO = {"self": "сами", "admin": "ИТ"}   # «Ставит: …» в сводке
MODE_HINTS = {"self": "Приложения из магазина и один QR. Где магазина нет — предупредим.",
              "admin": "Сервер скачает APK и установщики — раздача из «Дистрибутивов»."}
PRESET_TITLES = {"simple": "Просто", "reliable": "Надёжно"}
SETS_SHOWN = 4   # наборов в «сменить» (не считая «Не нужен»)
# Полевой тест 05.10.2026 (находка 7): у VLESS+Vision новые соединения рвутся на части путей,
# Hysteria2 и XHTTP устойчивы: предвыбор — Hysteria2, XHTTP и AmneziaWG
DEFAULT_PROTOS = ("hysteria2", "vless-xhttp", "amneziawg")
# назначение протокола одной строкой (ориентир из PLAN-builder и каталога приложений, не замер)
PURPOSE = {
    "hysteria2": "Быстрый, основной (UDP)",
    "hysteria2-obfs": "Если Hysteria2 режут",
    "vless-xhttp": "Если режут UDP",
    "vless-reality": "Если режут UDP, запасной",
    "amneziawg": "Запасной",
    "tuic": "Запасной (UDP)",
    "ss2022": "По умолчанию выключен: в полевом тесте соединения теряли данные",
}
ERRORS_SHOWN = 6
DONE_ROWS_MAX = 3   # больше людей — на шаге раздачи не панели с QR каждого, а переход к карточкам
CATCH = (groups.GroupError, users.UserError, allowlist.AllowlistError, LockTimeout, protolib.ProtoError,
         clients.ClientsError)


def _redirect(location: str) -> "Response":
    from .app import redirect
    return redirect(location)


def _err(e: Exception) -> str:
    return f"{e} {e.short()}" if isinstance(e, protolib.ProtoError) else str(e)


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 приложение», «2 приложения», «5 приложений»."""
    word = one if n % 10 == 1 and n % 100 != 11 else (few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many)
    return f"{n} {word}"


def _num(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


# ---------- черновик мастера и формы группы ----------

@dataclass
class Draft:
    name: str = ""
    protocols: list[str] = field(default_factory=list)
    clients: dict[str, list[str]] = field(default_factory=dict)
    clients_for: str = ""   # протоколы и режим, под которые приложения уже выбраны на шаге «Приложения» (изменились — подбор заново)
    install_mode: str = "self"
    devices: list[str] = field(default_factory=lambda: list(groups.MAIN_DEVICES))
    allow_mode: str = "common"
    allow: dict[str, list[str]] = field(default_factory=lambda: {p: [] for p in allowlist.PLATFORMS})
    new_users: str = ""
    existing: list[str] = field(default_factory=list)
    custom: bool = False    # «Свой набор» или «сменить протоколы»: в степпере есть подшаг «Протоколы»
    preset: str = ""        # выбранный на шаге «Вариант» (simple, reliable, custom): «Назад» его показывает

    @classmethod
    def from_form(cls, req: "Request") -> "Draft":
        f, m = req.form, req.multi
        sets: dict[str, list[str]] = {}
        gone: set[str] = set()
        for k, vals in m.items():
            if k.startswith("set:"):    # устройство → набор «A+B»; none — устройство не нужно
                if vals and vals[-1] == "none":
                    gone.add(k[4:])
                elif ids := groups.client_ids(vals[-1][:200].split("+")[:groups.CLIENTS_MAX + 1]):
                    sets[k[4:]] = ids
        for k, vals in m.items():       # прежняя форма: галочки client:<платформа> (набор по умолчанию — set:)
            if k.startswith("client:") and k[7:] not in sets and k[7:] not in gone:
                if ids := groups.client_ids([v[:40] for v in vals][:groups.CLIENTS_MAX + 1]):
                    sets[k[7:]] = ids
        if f.get("devs") == "1":
            devices = list(dict.fromkeys(p[:20] for p in m.get("dev", [])))[:10]
        else:
            devices = list(sets) or list(groups.MAIN_DEVICES)
        devices = [p for p in devices if p not in gone]
        return cls(
            name=f.get("name", "")[:200].strip(),
            protocols=list(dict.fromkeys(p[:40] for p in m.get("proto", [])))[:20],
            clients={p: ids for p, ids in sets.items() if p in devices},
            clients_for=f.get("clients_for", "")[:400],
            install_mode=groups.clean_mode(f.get("mode")),
            devices=devices,
            allow_mode="own" if f.get("allow_mode") == "own" else "common",
            allow={p: [v[:128] for v in m.get(p, [])][:allowlist.LIST_MAX + 1] for p in allowlist.PLATFORMS},
            new_users=f.get("users_new", "")[:people.TEXT_MAX],
            existing=[n[:32] for n in m.get("existing", [])][:200],
            custom=f.get("custom") == "1",
            preset=f.get("preset", "")[:20])

    @classmethod
    def of_group(cls, g: groups.Group) -> "Draft":
        d = cls(name=g.name, protocols=list(g.protocols), clients={p: list(v) for p, v in g.clients.items()},
                install_mode=g.install_mode, devices=list(g.clients))
        if g.allowlist:
            d.allow_mode, d.allow = "own", {p: list(g.allowlist[p]) for p in allowlist.PLATFORMS}
        return d

    def resolved(self, managed: list[str]) -> list[str]:
        return groups.by_priority(groups.offered(self.protocols, managed))

    def allow_arg(self) -> dict[str, list[str]] | None:
        return dict(self.allow) if self.allow_mode == "own" else None


def _hidden(d: Draft, shown: int) -> list[Markup]:
    """Состояние шагов, которых сейчас не видно, — скрытыми полями (shown — номер шага формы: 0 вариант,
    1 протоколы, 2 приложения, 3 люди). «Кто ставит» на шагах 0 и 2 — переключатель, скрытого поля нет."""
    out: list[Markup] = []

    def add(name: str, value: str) -> None:
        out.append(t("input", type="hidden", name=name, value=value))

    if shown not in (0, 2):
        add("mode", d.install_mode)
    if d.custom:
        add("custom", "1")
    if d.preset:
        add("preset", d.preset)
    if shown != 3:
        add("name", d.name)
    if shown != 1:
        for p in d.protocols:
            add("proto", p)
    if shown != 2:
        if d.clients_for:
            add("clients_for", d.clients_for)
        add("devs", "1")
        for plat in d.devices:
            add("dev", plat)
        for plat, ids in d.clients.items():
            add(f"set:{plat}", "+".join(ids))
    if shown != 3:
        add("users_new", d.new_users)
        for n in d.existing:
            add("existing", n)
        add("allow_mode", d.allow_mode)
        if d.allow_mode == "own":
            for plat in allowlist.PLATFORMS:
                for v in d.allow[plat]:
                    add(plat, v)
    return out


# ---------- факты о протоколах ----------

def _rank_facts() -> dict[str, dict[str, Any]]:
    """По клиентским пробам за 30 дней: {протокол: n, ok, top (в скольких контекстах в топе), score, down и latency
    (медианы по контекстам: у людей)}."""
    out: dict[str, dict[str, Any]] = {}
    try:
        ranking = rank.load("30d")
    except (sqlite3.Error, OSError, ValueError, history.HistoryError):
        return out
    downs: dict[str, list[float]] = {}
    lats: dict[str, list[float]] = {}
    for c in ranking:
        for p in c["protocols"]:
            d = out.setdefault(p["proto"], {"n": 0, "ok": 0, "top": 0, "score": 0.0, "ctx": 0, "down": None, "latency": None})
            d["n"] += p["n"]
            d["ok"] += p["ok"]
            if p.get("down_mbps"):
                downs.setdefault(p["proto"], []).append(p["down_mbps"])
            if p.get("latency_ms"):
                lats.setdefault(p["proto"], []).append(p["latency_ms"])
            if c["ranked"]:
                d["ctx"] += 1
                d["score"] += p["score"]
                d["top"] += 1 if p in c["top"] else 0
    for pid, d in out.items():
        d["down"] = statistics.median(downs[pid]) if pid in downs else None
        d["latency"] = statistics.median(lats[pid]) if pid in lats else None
    return out


@dataclass
class Fact:
    id: str
    title: str
    live: dict[str, Any] | None
    rank: dict[str, Any] | None
    now: float

    @property
    def down(self) -> bool:
        d = self.live
        return bool(d and not d["ok"] and d["verdict"] not in verdicts.NOT_TESTED)

    @property
    def people(self) -> dict[str, Any] | None:
        """Данные клиентских проб, если их достаточно (от rank.LOW_SAMPLES за 30 дней)."""
        r = self.rank
        return r if r and r["n"] >= rank.LOW_SAMPLES and (r.get("down") or r.get("latency")) else None

    @property
    def people_speed(self) -> float:
        r = self.people
        return float(r["down"]) if r and r.get("down") else 0.0

    def chip(self) -> Markup:
        """Одна метка факта: «у людей ≈65 Мбит/с · 76 мс» (замеры с устройств, от 3 за 30 дней), иначе «работает · 63 мс»
        (замер с сервера), «не отвечает». Скорость самого сервера — только на «Проверке» и «Обзоре»."""
        r = self.people
        if r:
            parts = ([f"≈{_num(r['down'])} Мбит/с"] if r.get("down") else []) + ([f"{r['latency']:.0f} мс"] if r.get("latency") else [])
            return t("span", "у людей " + " · ".join(parts), class_="chip ok",
                     title=f"медианы замеров с устройств за 30 дней, замеров: {int(r['n'])}, удачных: {int(r['ok'])}")
        d = self.live
        if d is None:
            return t("span", "нет замера", class_="chip")
        if d["verdict"] in verdicts.NOT_TESTED:
            return t("span", "не проверяется", class_="chip")
        if not d["ok"]:
            return t("span", "не отвечает", class_="chip bad",
                     title=verdicts.DESCRIPTIONS.get(d["verdict"], d["verdict"]))
        stale = self.now - d["ts"] > live.FRESH
        text = "работает" + (f" · {d['rtt_ms']:.0f} мс" if d.get("rtt_ms") is not None else "") + (" (устарело)" if stale else "")
        return t("span", text, class_="chip" if stale else "chip ok")


def proto_facts() -> list[Fact]:
    """Выбираемые протоколы (модули и их варианты, например Salamander) в порядке PRIORITY."""
    mans = {m.id: m for m in manifests.load_all()[0]}
    latest, ranks, now = live.summary(), _rank_facts(), time.time()

    def fact(pid: str) -> Fact:
        m = mans.get(pid)
        return Fact(pid, manifests.TITLES.get(pid) or (m.short if m else pid), latest.get(pid), ranks.get(pid), now)

    return [fact(p) for p in groups.by_priority(users.selectable_protocols())]


def suggest(facts: list[Fact], only: set[str] | None = None) -> list[str]:
    """Предвыбор: топ по клиентским пробам (если они есть), иначе дефолты из рабочих протоколов.
    only — допустимые протоколы (людям, которые ставят сами, — те, что входят одним QR)."""
    facts = [f for f in facts if only is None or f.id in only]
    ranked = sorted((f for f in facts if f.rank and f.rank["top"] and not f.down),
                    key=lambda f: (-f.rank["top"], -f.rank["score"] / max(f.rank["ctx"], 1)))  # type: ignore[index]
    ids = [f.id for f in ranked][:3]
    if ids:
        return ids
    ok = [f.id for f in facts if not f.down]
    return ([p for p in DEFAULT_PROTOS if p in ok] or ok)[:3]


def _proto_row(f: Fact, checked: bool) -> Markup:
    purpose = PURPOSE.get(f.id)
    return t("label", t("input", type="checkbox", name="proto", value=f.id, checked=checked),
             t("span", t("span", t("strong", f.title), class_="opt-title"),
               t("div", f.chip(), class_="chips"),
               t("span", purpose, class_="hint") if purpose else None, class_="opt-body"),
             class_="opt")


def _protocols_block(facts: list[Fact], selected: list[str], carried: list[str] | tuple = ()) -> Markup:
    """Все выбираемые протоколы сервера строками-чекбоксами (Salamander — отдельная строка); выключенные — серые.
    carried — протоколы группы: выключенный, но оставшийся в группе, можно убрать отметкой «drop»."""
    items = [_proto_row(f, f.id in selected) for f in facts]
    if not items:
        return alert_list([("warn", "Нет включённых протоколов с пользователями.")])
    for pid, title, why in _not_selectable({f.id for f in facts}):
        kept = pid in carried and why.startswith("выключен")
        body = t("span", t("span", t("strong", title), class_="opt-title"),
                 t("span", why + (" В группе остаётся и вернётся участникам при включении; отметьте, чтобы убрать."
                                  if kept else ""), class_="hint"), class_="opt-body")
        items.append(t("label", t("input", type="checkbox", name="drop", value=pid), body, class_="opt off")
                     if kept else t("div", body, class_="opt off"))
    return t("div", items, class_="opts")


def _not_selectable(shown: set[str]) -> list[tuple[str, str, str]]:
    """Протоколы сервера, которых нет среди выбираемых: выключенные — серой строкой, чтобы были видны все."""
    out = []
    for m in manifests.load_all()[0]:
        if m.id in shown:
            continue
        out.append((m.id, manifests.TITLES.get(m.id, m.short),
                    "выключен на сервере — включается на «Обзоре»." if not m.enabled else "без учёток пользователей"))
    return out


# ---------- приложения: устройство → одна строка ----------

def _set_flag(cat: clients.Catalog, plat: str, ids: list[str], mode: str) -> tuple[str, list[str]] | None:
    """Подвох набора: (короткая метка, приложения) — нет в российском магазине (у ИТ на iPhone — «нужен иностранный
    Apple ID»); людям, которые ставят сами, — приложение не из магазина. Метка короткая, объяснение — один раз
    на экран (_flag_legend). На iPhone «ставится файлом» не бывает: приложения только из App Store."""
    cs = [c for c in (cat.client(i) for i in ids) if c]
    admin = mode == "admin"
    foreign = [(c["name"], clientviews.foreign_note(cat, c, plat, admin)) for c in cs]
    foreign = [(n, label) for n, label in foreign if label]
    if foreign:
        return foreign[0][1], [n for n, _ in foreign]
    if mode == "self":
        raw = [c["name"] for c in cs if not groups.has_store_link(c, plat)]
        if raw and plat == "ios":
            return "нет в App Store РФ", raw
        if raw and any(groups.has_store_link(c, plat) for c in cat.clients if plat in c["platforms"]):
            return FLAG_RAW, raw
        if raw:
            return FLAG_GITHUB, raw
    return None


FLAG_RAW = "не из магазина"
FLAG_GITHUB = "в магазине нет"
FLAG_EXPLAIN = {
    FLAG_RAW: "ставится файлом: в инструкции сказано, какой",
    FLAG_GITHUB: "установщик с GitHub: в инструкции сказано, какой файл",
}


def _flag_legend(flags: list[tuple[str, list[str]]]) -> list[str]:
    """Объяснения меток наборов, каждое один раз: «Happ, Hiddify — нет в App Store РФ: …». Метка без объяснения
    (Android, компьютер: «нет в магазине РФ») строкой не дублируется."""
    by: dict[str, list[str]] = {}
    for label, names in flags:
        by.setdefault(label, []).extend(n for n in names if n not in by.get(label, []))
    out = []
    for label, names in by.items():
        why = FLAG_EXPLAIN.get(label) or clientviews.FOREIGN_WHY.get(label, "")
        if why:
            out.append(f"{', '.join(names)} — {label}: {why}")
    return out


def _device_options(cat: clients.Catalog, plat: str, protocols: list[str], current: list[str], plan: list[str],
                    mode: str, used: set[str]) -> list[list[str]]:
    """Наборы для «сменить»: выбранный сейчас, предложенный подбором и лучшие из client_sets (до четырёх из 1–2
    приложений). Выбранный сейчас, даже если приложений больше двух (старая группа), идёт первым."""
    out: list[list[str]] = []
    for ids in (current, plan):
        if ids and sorted(ids) not in [sorted(x) for x in out]:
            out.append(ids)
    for r in groups.client_sets(cat, plat, protocols, mode, prefer=used):
        if len(out) >= SETS_SHOWN:
            break
        if sorted(r["ids"]) not in [sorted(x) for x in out]:
            out.append(r["ids"])
    return out


def _device_row(cat: clients.Catalog, plat: str, title: str, protocols: list[str], chosen: list[str] | None,
                options: list[list[str]], names: dict[str, str], mode: str,
                ios_hint: bool = True) -> tuple[Markup, list[tuple[str, list[str]]], bool]:
    """Строка устройства: название, приложения, один чип покрытия, чип про магазин («нет в App Store РФ», у ИТ — «нужен
    иностранный Apple ID») и «сменить» справа. В списке «сменить» у выбранного набора меток нет (они в строке), у
    остальных — только короткие; объяснения — один раз под строками. Возвращает строку, метки наборов и признак,
    что у выбранного набора есть метка из чипа строки (иностранный App Store).
    ios_hint — строка «iPhone: ставится только из App Store» (ставит ИТ): один раз на экран."""
    flags: list[tuple[str, list[str]]] = []
    chosen_flag = False
    cur_flag = _set_flag(cat, plat, chosen or options[0], mode) if options else None
    if not options:
        return t("div", t("strong", title, class_="dev-name"), t("span", "нет приложения под эти протоколы", class_="muted"),
                 class_="dev-row"), flags, False
    ids = chosen or options[0]
    label, kind = clientviews.coverage_label(cat, plat, protocols, ids, names)
    radios = []
    for opt in options:
        cur = opt == ids
        flag = _set_flag(cat, plat, opt, mode)
        if flag and (not cur or flag[0] in clientviews.FOREIGN_WHY):   # у выбранного — только то, что видно чипом строки
            flags.append(flag)
        if flag and cur and flag[0] in clientviews.FOREIGN_WHY:
            chosen_flag = True
        lab, k = clientviews.coverage_label(cat, plat, protocols, opt, names)
        radios.append(t("label", t("input", type="radio", name=f"set:{plat}", value="+".join(opt), checked=cur,
                                   data_auto=True),
                        t("span", t("strong", clientviews.app_names(cat, opt)), " ",
                          None if cur else [t("span", lab, class_=f"chip {k}"), " ",
                                            t("span", flag[0], class_="chip warn")
                                            if flag and not (cur_flag and cur_flag[0] == flag[0]) else None],
                          class_="opt-title"), class_="opt-row dev-opt"))
    radios.append(t("label", t("input", type="radio", name=f"set:{plat}", value="none", data_auto=True),
                    t("span", f"Не нужен: {title}", class_="opt-title"), class_="opt-row dev-opt"))
    foreign = next((n for i in ids if (n := clientviews.foreign_note(cat, cat.client(i) or {}, plat, mode == "admin"))), "")
    extra = distviews.ios_note() if plat == "ios" and mode == "admin" and ios_hint else None
    head = t("summary", t("strong", title, class_="dev-name"), t("span", clientviews.app_names(cat, ids), class_="dev-set"),
             t("span", label, class_=f"chip {kind}"),
             t("span", foreign, class_="chip warn") if foreign else None,
             t("span", "сменить", class_="dev-change"), class_="dev-head")
    return t("div", t("details", head, t("div", radios, class_="opts"), class_="dev-d"), extra, class_="dev-row"), flags, chosen_flag


def _clients_block(d: Draft, managed: list[str], ios_hint: bool = True) -> Markup:
    """«Приложения»: чипы устройств; по строке на устройство (что поставить, чип покрытия, «сменить»); «Всего N
    приложений: …» и список «Оговорки» — каждая один раз. Любая смена радиокнопки или чипа пересобирает блок:
    кнопка «Пересчитать», с JS она нажимается сама."""
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return alert_list([("bad", str(e))])
    names = clientviews.proto_names(cat)
    protocols = d.resolved(managed)
    devices = [p for p in d.devices if p in cat.platforms]
    plan = groups.suggest_set(cat, devices, protocols, d.install_mode)
    chips = t("div", [t("label", t("input", type="checkbox", name="dev", value=p, checked=p in devices, data_auto=True),
                        t("span", title), class_="chip-check") for p, title in cat.platforms.items()],
              class_="dev-chips")
    rows: list[Markup] = []
    flags: list[tuple[str, list[str]]] = []
    chosen_flag = False
    picked: dict[str, list[str]] = {}
    for plat in cat.platforms:
        if plat not in devices:
            continue
        chosen = [i for i in d.clients.get(plat, []) if cat.client(i)]
        used = {a for p, ids in d.clients.items() if p != plat for a in ids}
        opts = _device_options(cat, plat, protocols, chosen, plan.get(plat, []), d.install_mode, used)
        row, row_flags, row_chosen = _device_row(cat, plat, cat.platforms[plat], protocols, chosen or None, opts, names,
                                     d.install_mode, ios_hint)
        rows.append(row)
        flags += row_flags
        chosen_flag = chosen_flag or row_chosen
        if opts:
            picked[plat] = chosen or opts[0]
    line = groups.apps_line(cat, picked)
    count = len({c for ids in picked.values() for c in ids})
    total = (t("p", t("strong", f"Всего {plural(count, 'приложение', 'приложения', 'приложений')}: "), line, class_="dev-total")
             if line else t("p", "Устройства не выбраны.", class_="muted"))
    cav = clientviews.caveat_items(cat, picked, protocols, names)
    legend = _flag_legend(flags)
    caveats = t("div", t("span", "Оговорки" if cav or chosen_flag else "Про варианты в «сменить»", class_="label"),
                t("ul", [[t("li", text) for text, _ in cav], [t("li", line) for line in legend]], class_="cav-list"),
                t("details", t("summary", "подробнее"), t("ul", [t("li", f"{text}: {note}") for text, note in cav if note],
                                                           class_="cav-list"), class_="more"),
                class_="cav") if cav or legend else None
    unverified = any(not (cat.client(c) or {}).get("verified", {}).get("device", True) for ids in picked.values() for c in ids)
    return t("div", t("span", "Устройства", class_="label"), chips, t("div", rows, class_="dev-rows"), total, caveats,
             t("p", clientviews.UNVERIFIED, class_="hint") if unverified else None,
             t("button", "Пересчитать", type="submit", name="go", value="refresh", class_="btn small", data_refresh=True,
               formnovalidate=True))


def _clients_sig(d: Draft, managed: list[str]) -> str:
    """Под какие протоколы и режим «кто ставит» выбраны приложения (изменились — подбор заново)."""
    return ",".join(d.resolved(managed)) + "|" + d.install_mode


def normalize_clients(d: Draft, managed: list[str], fill: bool = True) -> None:
    """Выбор приложений под текущие протоколы и устройства: остаются подходящие. fill (мастер): пока приложения не
    выбраны под эти же протоколы и режим «кто ставит» (первый показ шага, протоколы или режим сменили) — предлагается
    подбор (suggest_set); после — отмеченное сохраняется. Устройство без выбора (только что добавленный чип)
    получает подбор; устройство, под которое приложений нет, остаётся без набора."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return
    protocols = d.resolved(managed)
    sig = _clients_sig(d, managed)
    stale = fill and d.clients_for != sig
    devices = [p for p in d.devices if p in cat.platforms]
    plan = groups.suggest_set(cat, devices, protocols, d.install_mode)
    fixed: dict[str, list[str]] = {}
    for plat in devices:
        opts = groups.client_options(cat, plat, protocols)
        if not opts:
            continue
        ids = {o["client"]["id"] for o in opts}
        got = [] if stale else [i for i in d.clients.get(plat, []) if i in ids]
        got = got or plan.get(plat, [])
        if got:
            fixed[plat] = got
    d.devices = devices
    d.clients = fixed
    if fill:
        d.clients_for = sig


# ---------- «Через VPN», люди ----------

def _apps_block(d: Draft) -> Markup:
    """«Через VPN» (страница группы): общий список или свой, с таблицей приложений."""
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return alert_list([("bad", str(e))])
    own = d.allow_mode == "own"
    base = {p: (d.allow[p] if own and d.allow[p] else al.common(p)) for p in allowlist.PLATFORMS}
    on = {p: {i.lower() for i in base[p]} for p in allowlist.PLATFORMS}
    rows = [allowviews._row_cells(r, on, on)
            for r in allowviews._rows(al, {p: al.common(p) for p in allowlist.PLATFORMS}, base, al.titles)]
    common = " · ".join(f"{PLAT_NAMES.get(p, p)} {len(al.common(p))}" for p in allowlist.PLATFORMS)
    return t("div",
             t("label", t("input", type="radio", name="allow_mode", value="common", checked=not own),
               t("span", "Общий список ", t("span", f"({common})", class_="muted small"), class_="opt-title"),
               class_="opt-row"),
             t("label", t("input", type="radio", name="allow_mode", value="own", checked=own),
               t("span", "Свой список группы", class_="opt-title"), class_="opt-row"),
             t("details", t("summary", "Приложения своего списка"),
               table(["приложение", "Android", "Windows"], rows, num=[1, 2], cls="apps"),
               t("p", "Нужен хотя бы один пункт на Android и на Windows. Свой список пользователя "
                      "потом можно задать отдельно («Через VPN»).", class_="hint"),
               open=own or None, class_="more"))


def _via_counts(al: allowlist.Allowlist, own: dict[str, list[str]] | None = None) -> str:
    """«Android 12 · Windows 6»: сколько приложений в списке «через VPN» на платформе."""
    return " · ".join(f"{PLAT_NAMES.get(p, p)} {len((own or {}).get(p) or al.common(p))}" for p in allowlist.PLATFORMS)


def _via_line(d: Draft) -> Markup | None:
    """Шаг «Люди»: одна строка «Через VPN: общий список (Telegram, YouTube… ещё 10) · Android 12 · Windows 6». Группы ещё
    нет, поэтому правка — в её настройках после создания (на «Раздаче» там же ссылка); приложения, которые список не
    умеют, — «В Hiddify через VPN идёт всё»."""
    try:
        al = allowlist.Allowlist.load()
        cat = clients.load()
    except (allowlist.AllowlistError, clients.ClientsError):
        return None
    pairs = [(c, plat) for plat, ids in d.clients.items() if plat in allowlist.PLATFORMS for cid in ids if (c := cat.client(cid))]
    plain = list(dict.fromkeys(c["name"] for c, plat in pairs if not clientviews.supports_list(cat, c, plat)))
    works = [1 for c, plat in pairs if clientviews.supports_list(cat, c, plat)]
    if pairs and not works:
        return t("p", f"В {', '.join(f'«{n}»' for n in plain)} через VPN идёт всё.", class_="hint")
    own = d.allow_mode == "own"
    titles = clientviews.via_vpn_names(al, "android", d.allow["android"] if own and d.allow["android"] else al.common("android"))
    shown = ", ".join(titles[:2]) + (f"… ещё {len(titles) - 2}" if len(titles) > 2 else "")
    counts = _via_counts(al, d.allow if own else None)
    return t("p", "Через VPN: " + ("свой список" if own else f"общий список ({shown})")
             + f" · {counts} · изменить — в настройках группы после создания",
             t("br") if plain else None,
             f"В {', '.join(f'«{n}»' for n in plain)} через VPN идёт всё." if plain else None,
             class_="hint")


def _mode_seg(mode: str) -> Markup:
    """«Кто ставит»: один переключатель на всех экранах; смена отправляет форму (с JS сама), черновик сохраняется."""
    return t("div", t("span", "Кто ставит:", class_="label"),
             t("div", [t("label", t("input", type="radio", name="mode", value=m, checked=m == mode, data_auto=True),
                         t("span", title)) for m, title in MODE_LABELS.items()],
               class_="seg", role="radiogroup", aria_label="Кто ставит приложения"),
             class_="seg-row")


def _picker(label: str, name: str, items: list[tuple[str, str, Any, bool, str]]) -> Markup:
    """Список с поиском и галочками: (значение, подпись, пояснение справа, отмечен, текст для поиска). Без JS — просто
    прокручиваемый список галочек; с JS — строка поиска и счётчик отмеченных (app.js, [data-picker])."""
    rows = [t("label", t("input", type="checkbox", name=name, value=v, checked=on), t("span", shown),
              t("span", sub, class_="muted small") if sub else None, class_="pick-item",
              data_find=" ".join(dict.fromkeys(x for x in (v, shown, find) if x)).lower()) for v, shown, sub, on, find in items]
    return t("div", t("span", label, class_="label") if label else None,
             t("div", t("input", type="search", data_filter=True, placeholder="найти", autocomplete="off",
                        aria_label="Найти в списке", class_="pick-find"),
               t("span", data_picked=True, class_="muted small"), class_="pick-head"),
             t("div", rows, class_="pick-list"), class_="field picker", data_picker=True)


def _person(u: users.User) -> str:
    """«Имя (логин)», если у человека есть имя из списка; иначе логин."""
    return f"{u.display} ({u.name})" if u.display else u.name


def _existing_block(d: Draft, group_id: str | None = None) -> Markup | None:
    """«Перевести уже существующих (N)»: свёрнутый список без owner и служебных; у каждого группа, откуда он перейдёт."""
    gs = groups.Groups.load()
    items = [(u.name, _person(u), t("span", g.name, class_="chip") if (g := gs.get(u.group)) else None,
              u.name in d.existing, g.name if g else "")
             for u in users.list_users().visible() if u.name != users.OWNER and not (group_id and u.group == group_id)]
    if not items:
        return None
    return t("details", t("summary", f"Перевести уже существующих ({len(items)})"),
             _picker("", "existing", items),
             t("p", "Протоколы и приложения станут как у этой группы — понадобится новый QR.", class_="hint"),
             open=bool(d.existing) or None, class_="more")


def _users_block(d: Draft, group_id: str | None = None) -> Markup:
    return t("div",
             t("div", t("label", "Новые люди", for_="users_new"),
               t("textarea", d.new_users, name="users_new", id="users_new", rows="8", maxlength=str(people.TEXT_MAX),
                 placeholder="Иван Петров; бухгалтерия; android, windows\nМария Сидорова\nОльга; склад; iphone",
                 autocomplete="off", spellcheck="false"),
               t("div", f"По строке на человека: «Имя; заметка; устройства» (android, iphone, windows). Без устройств — "
                        f"как у группы. До {people.LINES_MAX}.", class_="hint"),
               class_="field"),
             _existing_block(d, group_id), class_="stack")


def _preview_rows(plan: people.Plan, group_devs: list[str] | None = None) -> Markup:
    """Предпросмотр: № · Имя · Логин · Заметка · Устройства; чипы только у проблемных строк (совпавшее имя, ошибка,
    устройство без приложений группы)."""
    rows, cls = [], []
    for r in plan.rows:
        lack = [d for d in r.devices if group_devs is not None and d not in group_devs]
        chips = [t("span", f"{r.clash}: добавлен номер", class_="chip warn", title="такое имя уже есть: добавлен номер")
                 if r.clash else None,
                 t("span", f"нет приложений для {people.device_titles(lack)}", class_="chip warn") if lack else None,
                 t("span", r.problem, class_="chip bad") if r.problem else None]
        devs = people.device_titles(r.devices) if r.devices else t("span", "как у группы", class_="muted")
        rows.append([str(r.line), t("strong", r.display), t("code", r.name) if r.name else "—", r.note or "—", devs, chips])
        cls.append("row-bad" if r.problem else ("row-warn" if r.clash or lack else None))
    return table(["№", "Имя", "Логин", "Заметка", "Устройства", ""], rows, num=[0], cls="preview", row_cls=cls, stack=True)


def _preview_summary(plan: people.Plan, existing: int = 0) -> str:
    text = f"Будет создано: {len(plan.rows)}"
    if existing:
        text += f", переведено из имеющихся: {existing}"
    if plan.renamed:
        text += f". Совпали имена, добавлен номер: {len(plan.renamed)}"
    return text + ". Логин — имя латиницей, нужен только системе: в инструкциях будет имя."


def _count_label(verb: str, new: int, moved: int, joiner: str = "") -> str:
    """Подпись кнопки: «Создать группу и 3 чел.»; есть переводимые — «Создать группу: 3 новых + 2 перевести»."""
    if moved:
        return f"{verb}: {plural(new, 'новый', 'новых', 'новых')} + {moved} перевести"
    return f"{verb} {joiner + ' ' if joiner else ''}{new} чел."


def _preview(app: "App", req: "Request", d: Draft, plan: people.Plan) -> "Response":
    """Шаг «Люди» → создание: что получится из списка; создаёт только кнопка под таблицей (confirm=1). Таблица внутри
    формы: панель кнопок прилипает к низу экрана, пока список не кончился."""
    csrf = req.session.csrf if req.session else ""
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="step", value="3"),
             t("input", type="hidden", name="confirm", value="1"), _hidden(d, 4),
             _preview_rows(plan, [p for p, ids in d.clients.items() if ids]),
             t("div", t("button", _count_label("Создать группу", len(plan.rows), len(d.existing), "и"), type="submit", name="go",
                        value="create", class_="btn primary"),
               t("button", "← Изменить список", type="submit", name="go", value="edit", class_="btn", formnovalidate=True),
               class_="wiz-nav"),
             method="post", action="/connect/new", class_="stack", data_swap=True)
    body = card("Люди: проверьте список", t("p", _preview_summary(plan, len(d.existing)), class_="hint"), form)
    return app.render(req, TITLE, t("div", [page_head(TITLE), _stepper(3, d.custom), body], class_="wizard",
                                    data_expanded=True), active="/groups")


# ---------- мастер ----------

def _stepper(step: int, custom: bool = False, final: bool = False) -> Markup:
    """step — внутренний шаг: 0 вариант, 1 протоколы, 2 приложения, 3 люди; final — раздача. «Протоколы» — только у
    «Своего набора» (custom)."""
    names = [STEPS[0], *([PROTO_STEP] if custom else []), *STEPS[1:]]
    now = {0: STEPS[0], 1: PROTO_STEP, 2: STEPS[1], 3: STEPS[2]}[step] if not final else STEPS[3]
    pos = names.index(now) if now in names else 0
    return t("ol", [t("li", t("span", str(i + 1), class_="n"), name,
                      class_="cur" if i == pos else ("done" if i < pos else None),
                      aria_current="step" if i == pos else None) for i, name in enumerate(names)],
             class_="stepper")


NO_PEOPLE_DATA = "Замеров с устройств пока нет — такие варианты выбраны по надёжности."


def _preset_why(pr: dict[str, Any], names: dict[str, str], by_id: dict[str, Fact]) -> str:
    """Строка «почему» из того же источника, по которому выбрано: клиентские пробы. Пустая — проб нет (одна общая
    строка под вариантами, не по строке на вариант)."""
    mine = [(p, by_id[p].people_speed) for p in pr["protocols"] if p in by_id and by_id[p].people_speed]
    if not mine:
        return ""
    best = max((f.people_speed for f in by_id.values()), default=0.0)
    pid, speed = max(mine, key=lambda x: x[1])
    if speed >= best:
        return f"быстрее всего у людей: ≈{_num(speed)} Мбит/с"
    return f"у людей: {names.get(pid, pid)} ≈{_num(speed)} Мбит/с"


def _preset_row(pr: dict[str, Any], cat: clients.Catalog, names: dict[str, str], by_id: dict[str, Fact],
                chosen: bool, rec: bool, admin: bool, via: bool = True) -> tuple[Markup, list[str]]:
    """Готовый вариант: название, протоколы чипами, приложения одной строкой, что идёт через VPN на каждом устройстве
    (via=False — у всех вариантов одинаково, строка одна под вариантами), «почему». Один primary и «рекомендуем» — у
    варианта без оговорок (rec); есть оговорки — чип «с оговорками». Возвращает и сами оговорки (магазин iPhone
    и прочие): _start_block выводит их одним списком под вариантами, каждую один раз."""
    issues, foreign = [], []
    if not pr["complete"]:
        issues.append("не на всех устройствах есть приложение")
    for plat, ids in pr["plan"].items():
        for i in ids:
            note = clientviews.foreign_note(cat, cat.client(i) or {}, plat, admin)
            if note:
                foreign.append(f"{cat.platforms.get(plat, plat)}: {note}")
        issues += [f"{app}: {names.get(p, p)} {short}" for p, app, short, _ in groups.caveats(cat, plat, pr["protocols"], ids)]
    foreign = list(dict.fromkeys(foreign))
    if pr["foreign"] and not foreign:
        issues.append("есть приложения не из магазина РФ")
    flag = t("span", "с оговорками", class_="chip warn") if issues or pr["foreign"] else None
    why = _preset_why(pr, names, by_id)
    return t("div", t("span",
                      t("span", t("strong", PRESET_TITLES[pr["id"]]),
                        t("span", "рекомендуем", class_="chip info") if rec else None,
                        t("span", "выбрано", class_="chip") if chosen else None, flag, class_="opt-title"),
                      t("div", [t("span", names.get(p, p), class_="chip") for p in pr["protocols"]], class_="chips"),
                      t("span", groups.apps_line(cat, pr["plan"]), class_="hint"),
                      t("span", line, class_="hint") if via and (line := groups.via_line(cat, pr["plan"])) else None,
                      t("span", why, class_="hint") if why else None, class_="opt-body"),
             t("button", "Выбрать", type="submit", name="go", value=pr["id"], class_="btn primary" if rec else "btn"),
             class_="opt preset" + (" sel" if chosen else "")), list(dict.fromkeys([*foreign, *issues]))


def _start_block(d: Draft, facts: list[Fact], managed: list[str]) -> Markup:
    """Шаг «Вариант»: кто ставит приложения и готовые варианты («Просто», «Надёжно», «Свой набор»). Советуется вариант без
    оговорок; у обоих оговорки — никакой."""
    try:
        cat = clients.load()
    except clients.ClientsError as e:
        return alert_list([("bad", str(e))])
    names = clientviews.proto_names(cat)
    by_id = {f.id: f for f in facts}
    prs = groups.presets(cat, managed, d.install_mode)
    best = groups.recommended_preset(prs)
    vias = {groups.via_line(cat, pr["plan"]) for pr in prs}
    same = len(prs) > 1 and len(vias) == 1
    made = [_preset_row(pr, cat, names, by_id, d.preset == pr["id"], pr["id"] == best, d.install_mode == "admin", not same)
            for pr in prs]
    rows = [m[0] for m in made]
    where: dict[str, list[str]] = {}
    for pr, (_, notes) in zip(prs, made):
        for n in notes:
            where.setdefault(n, []).append(PRESET_TITLES[pr["id"]])
    flagged = len({x for ws in where.values() for x in ws})
    legend = [t("li", n + (f" ({', '.join(ws)})" if len(ws) < flagged else "")) for n, ws in where.items()]
    rows.append(t("div", t("span", t("span", t("strong", "Свой набор"),
                                    t("span", "выбрано", class_="chip") if d.preset == "custom" else None, class_="opt-title"),
                           t("span", "Протоколы и приложения выбираю сам.", class_="hint"), class_="opt-body"),
                  t("button", "Выбрать", type="submit", name="go", value="custom", class_="btn", formnovalidate=True),
                  class_="opt preset" + (" sel" if d.preset == "custom" else "")))
    return t("div", _mode_seg(d.install_mode), t("p", MODE_HINTS[d.install_mode], class_="hint"),
             t("button", "Пересчитать", type="submit", name="go", value="refresh", class_="btn small", data_refresh=True,
               formnovalidate=True),
             t("div", rows, class_="opts"),
             t("p", next(iter(vias)), class_="hint") if same and next(iter(vias)) else None,
             t("div", t("span", "Оговорки", class_="label"), t("ul", legend, class_="cav-list"), class_="cav")
             if legend else None,
             t("p", NO_PEOPLE_DATA, class_="hint") if any(not _preset_why(pr, names, by_id) for pr in prs) else None)


def _proto_summary(d: Draft, managed: list[str], names: dict[str, str]) -> Markup:
    """Шаг «Приложения»: протоколы группы чипами и «сменить протоколы» (возврат на подшаг «Протоколы»)."""
    return t("div", t("span", "Протоколы:", class_="label"),
             [t("span", names.get(p, p), class_="chip") for p in d.resolved(managed)],
             t("button", "сменить протоколы", type="submit", name="go", value="protocols", class_="linkbtn",
               formnovalidate=True), class_="proto-sum")


def _wizard(app: "App", req: "Request", step: int, d: Draft, errors: list[str] | None = None,
            status: int = 200) -> "Response":
    csrf = req.session.csrf if req.session else ""
    facts = proto_facts()
    managed = [f.id for f in facts]
    gs = groups.Groups.load()
    nav_next = True
    if step == 0:
        body, title, hint = [_start_block(d, facts, managed)], "Вариант", None
        nav_next = False
    elif step == 1:
        d.custom = True
        if not d.protocols:
            d.protocols = suggest(facts, _easy_only(d, managed))
        body = [_protocols_block(facts, d.protocols)]
        title, hint = PROTO_STEP, "Что отдаём людям."
    elif step == 2:
        normalize_clients(d, managed)
        try:
            names = clientviews.proto_names(clients.load())
        except clients.ClientsError:
            names = {}
        body = [t("input", type="hidden", name="clients_for", value=d.clients_for),
                t("input", type="hidden", name="devs", value="1"),   # чипы устройств — источник правды, как на странице группы
                _mode_seg(d.install_mode), _proto_summary(d, managed, names), _clients_block(d, managed)]
        title, hint = STEPS[1], "Что поставить на каждое устройство."
    else:
        if not d.name:
            d.name = gs.next_name()
        body = [t("div", t("label", "Название группы", for_="name"),
                  t("input", type="text", name="name", id="name", value=d.name, required=True, maxlength=str(groups.NAME_MAX),
                    autocomplete="off"), class_="field"),
                _users_block(d), _via_line(d)]
        title, hint = STEPS[2], "Кого подключаем."
    last = step == 3
    nav = t("div",
            t("button", "Проверить список →" if last else "Далее →", type="submit", name="go",
              value="create" if last else "next", class_="btn primary") if nav_next else None,
            t("button", "← Назад", type="submit", name="go", value="back", class_="btn", formnovalidate=True)
            if step > 0 else None, class_="wiz-nav") if step > 0 else None
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="step", value=str(step)),
             _hidden(d, step), body, nav, method="post", action="/connect/new", class_="stack", data_swap=True)
    parts: list[Any] = [page_head(TITLE)]
    pick = _group_pick(gs) if step == 0 else None
    if pick is not None:   # сначала — куда: в группу, где уже есть люди, или новая (варианты ниже)
        parts.append(pick)
        hint = "Или новая группа — выберите вариант."
    parts.append(_stepper(step, d.custom))
    if errors:
        parts.append(alert_list([("bad", e) for e in errors]))
    parts.append(card(title, t("p", hint, class_="hint") if hint else None, form))
    # data-expanded: живое обновление не заменяет страницу, пока идёт мастер (иначе шаг сбросился бы на первый)
    return app.render(req, TITLE, t("div", parts, class_="wizard", data_expanded=True),
                      active="/groups", status=status)


def _group_pick(gs: groups.Groups) -> Markup | None:
    """«В существующую группу»: только когда в какой-то группе уже есть люди (первый запуск — сразу новая группа).
    Выбрана — форма «Добавить людей списком» на странице группы. Заранее выбрана самая большая."""
    ureg = users.list_users()
    count = {g.id: sum(1 for u in groups.members_of(gs, ureg, g.id) if u.name != users.OWNER) for g in gs.groups}
    if not any(count.values()):
        return None
    best = max(gs.groups, key=lambda g: count[g.id])
    return card("В существующую группу",
                t("form", t("select", [t("option", f"{g.name} · {count[g.id]} чел.", value=g.id, selected=g is best)
                                       for g in gs.groups], name="to", aria_label="Группа"),
                  t("button", "Добавить людей →", type="submit", class_="btn primary"),
                  method="get", action="/connect/new", class_="actions"))


def _easy_only(d: Draft, managed: list[str]) -> set[str] | None:
    """Протоколы по умолчанию: людям, которые ставят сами, — только те, что входят одним QR из магазинного приложения."""
    if d.install_mode != "self":
        return None
    try:
        easy = groups.easy_protocols(clients.load())
    except clients.ClientsError:
        return None
    return {p for p in managed if p in easy} or None


def connect_page(app: "App", req: "Request") -> "Response":
    if not users.managed_protocols()[0]:
        return app.render(req, TITLE, [page_head(TITLE), card(
            "Нет протоколов", alert_list([("warn", "Нет включённых протоколов, куда можно добавить пользователя.")]))],
            active="/groups")
    try:
        gs = groups.ensure()
    except CATCH:
        gs = None
    to = req.query.get("to", "")[:40]
    if to and gs is not None and (g := gs.get(to)) is not None:
        return _redirect(f"/groups/{g.id}?add=1#add")
    return _wizard(app, req, 0, Draft(install_mode=groups.clean_mode(req.query.get("mode"))))


def _check(d: Draft, step: int) -> list[str]:
    """Первая ошибка шага (и предыдущих): сервер не верит скрытым полям. Название группы — на шаге «Люди»."""
    try:
        gs = groups.Groups.load()
        if step >= 1:
            groups.clean_protocols(d.protocols)
        if step >= 3:
            groups.clean_name(d.name, gs)
        if step >= 2:
            groups.clean_clients(d.clients, d.protocols)
        if step >= 3:
            groups.clean_allow(d.allow_arg())
            groups.check_members(groups.parse_new_users(d.new_users), d.existing)
            if not groups.parse_new_users(d.new_users) and not d.existing:
                raise groups.GroupError("Добавьте хотя бы одного пользователя")
    except CATCH as e:
        return [_err(e)]
    return []


def _start(app: "App", req: "Request", d: Draft, go: str) -> "Response":
    """Шаг «Вариант»: выбран готовый вариант («Просто», «Надёжно») — протоколы заданы, дальше сразу приложения; «Свой
    набор» — шаг протоколов с предвыбором под режим (вернулись со следующего шага — прежний выбор сохраняется)."""
    facts = proto_facts()
    managed = [f.id for f in facts]
    again = d.preset == "custom" and bool(d.protocols)
    d.devices, d.clients, d.clients_for = list(groups.MAIN_DEVICES), {}, ""
    d.preset = go
    d.custom = go == "custom"
    if go == "custom":
        d.protocols = d.protocols if again else suggest(facts, _easy_only(d, managed))
        return _wizard(app, req, 1, d)
    try:
        pr = next(p for p in groups.presets(clients.load(), managed, d.install_mode) if p["id"] == go)
    except (StopIteration, clients.ClientsError):
        d.preset = ""
        return _wizard(app, req, 0, d, ["Такого варианта нет: выберите ещё раз"], 422)
    d.protocols = list(pr["protocols"])
    d.clients = {plat: list(ids) for plat, ids in pr["plan"].items()}   # приложения варианта, как показаны на «Варианте»
    d.clients_for = _clients_sig(d, managed)
    return _wizard(app, req, 2, d)


def connect_post(app: "App", req: "Request") -> "Response":
    try:
        groups.ensure()
    except CATCH:
        pass
    d = Draft.from_form(req)
    try:
        step = min(max(int(req.form.get("step", "1")), 0), 3)
    except ValueError:
        step = 1
    go = req.form.get("go", "next")
    if go == "back":   # «Назад» с приложений — на «Вариант» (после готового варианта) или на «Протоколы» (после своего)
        return _wizard(app, req, {3: 2, 2: 1 if d.custom else 0, 1: 0}.get(step, 0), d)
    if go == "edit":
        return _wizard(app, req, 3, d)
    if go == "protocols":
        return _wizard(app, req, 1, d)
    if go == "refresh":
        return _wizard(app, req, step if step in (0, 2) else 2, d)
    if step == 0:
        return _start(app, req, d, go)
    errors = _check(d, step)
    if errors:
        return _wizard(app, req, step, d, errors, 422)
    if go != "create" or step < 3:
        return _wizard(app, req, min(step + 1, 3), d)
    plan = people.plan_for_registry(d.new_users)
    if plan.rows and not req.form.get("confirm"):
        return _preview(app, req, d, plan)
    try:
        rep = groups.connect(d.name, d.protocols, d.clients, d.allow_arg(), plan.entries(), d.existing, d.install_mode)
    except CATCH as e:
        return _wizard(app, req, 3, d, [_err(e)], 422)
    app.invalidate("status")
    app.invalidate_links()
    if rep.removed:
        # никого не добавили, пустая группа убрана: форма остаётся на шаге «Люди», повтор с тем же названием возможен
        return _wizard(app, req, 3, d, rep.errors or ["Никого не удалось добавить"], 422)
    flash_report(req, rep, _renamed(plan))
    if rep.crashed:
        return _redirect(f"/groups/{rep.group.id}")
    who = ",".join(rep.created + rep.moved)
    return _redirect("/connect/done?" + urllib.parse.urlencode({"group": rep.group.id, "u": who}))


def connect_done(app: "App", req: "Request") -> "Response":
    """Раздача. Порядок один для любого числа людей: «Группа «X» готова: N чел.» → «Раздать доступы» с главной
    кнопкой → «Инструкция» (свёрнута) → «Дистрибутивы» (только для «ставит ИТ»). Панели по людям — при N ≤ 3."""
    gs = groups.Groups.load()
    g = gs.get(req.query.get("group"))
    if g is None:
        return app.error(req, 404, "Нет группы", "Такой группы нет.")
    ureg = users.list_users()
    wanted = [n for n in req.query.get("u", "").split(",") if n][:groups.NEW_USERS_MAX * 2]
    members = [u for u in ureg.visible() if u.group == g.id and u.name in wanted]
    ctx = clientviews.Ctx.load()
    dist = distviews.card_for(gs, g.id, "/connect/done?" + urllib.parse.urlencode({"group": g.id, "u": req.query.get("u", "")}),
                              req.session.csrf if req.session else "") if g.install_mode == "admin" else None
    ready = card(f"Группа «{g.name}» готова: {len(members)} чел.", _summary(g),
                 extra=t("a", "Настроить", href=f"/groups/{g.id}#settings", class_="btn small", data_swap=True))
    head = [page_head(TITLE), _stepper(3, False, final=True), ready]
    if not members:
        return app.render(req, TITLE, [*head, alert_list([("warn", "Никого не добавили — раздавать нечего.")]), dist],
                          active="/groups")
    st = handoffviews.connection([u.name for u in members])
    link = "/handoff?" + urllib.parse.urlencode({"group": g.id})
    rows: list[Any] = []
    if len(members) <= DONE_ROWS_MAX and ctx and g.clients:
        for u in members:
            links, _ = userviews._cached_links(app, u.name)
            panel = clientviews.connect_panel(links, u.name, ctx, g, uid=f"{u.name}-", label=u.label, primary=False,
                                              devices=groups.devices_of(u, g))
            rows.append(t("details", t("summary", t("strong", u.label),
                                       t("span", f" {u.name}", class_="muted small") if u.display else None,
                                       t("span", f" {u.note}", class_="muted small") if u.note else None),
                          panel or t("p", "Ссылок пока нет.", class_="muted"),
                          t("p", t("a", "Страница пользователя →", href=f"/users/{u.name}"), class_="small"),
                          name="conn-user", open=len(members) == 1 or None, class_="urow"))
    hand = card("Раздать доступы",
                t("p", clientviews.no_apps(g)) if not g.clients else t(
                    "p", "Каждому — сообщение: ZIP (папка на человека — ссылки, файлы, инструкция) или CSV для рассылки. "
                         "На бумаге — только QR для телефона.", class_="hint"),
                t("div", t("a", "Карточки (печать, ZIP, CSV)", href=link, class_="btn primary", data_swap=True),
                  t("span", st.counter, class_="chip ok") if st.known and st.on else None, class_="actions"),
                t("p", clientviews.SEND_WARN, class_="hint"),
                t("p", clientviews.NO_KEYS, class_="hint") if rows else None,
                t("div", rows, class_="urows") if rows else None, clientviews.hints(ctx)[1:] if rows and ctx else None)
    body = [*head, hand, _texts_card(g, ctx) if ctx and g.clients else None, dist]
    return app.render(req, TITLE, body, active="/groups")


def _texts_card(g: groups.Group, ctx: clientviews.Ctx) -> Markup | None:
    """«Инструкция» группы: свёрнута, только для чтения. Править её — на странице группы."""
    items = []
    for plat, title in ctx.cat.platforms.items():
        text = ctx.text(g, plat)
        if text:
            items.append(t("div", t("h4", title, class_="plat-title"),
                           clientviews.text_block(clientviews.editor_text(text), f"msg-g-{plat}", None, primary=False),
                           class_="conn-plat"))
    if not items:
        return None
    return t("details", t("summary", "Инструкция"), t("div", items),
             t("p", t("a", "Изменить для группы →", href=f"/groups/{g.id}#text", data_swap=True), class_="small"),
             class_="card more")


def _summary(g: groups.Group) -> Markup:
    """Что настроено у группы, только для чтения, подписи как в мастере: «Протоколы», «Приложения», «Через VPN», «Ставит»."""
    selectable = users.selectable_protocols()
    try:
        cat = clients.load()
        apps = groups.apps_line(cat, g.clients)
    except clients.ClientsError:
        apps = " · ".join(f"{p}: {'+'.join(ids)}" for p, ids in g.clients.items())
    try:
        counts = f" ({_via_counts(allowlist.Allowlist.load(), g.allowlist)})"
    except allowlist.AllowlistError:
        counts = ""
    chips = [t("span", manifests.proto_title(p), class_="chip") for p in g.offered(selectable)]
    return t("div",
             t("p", t("strong", "Протоколы: "), t("span", chips, class_="chips inline")),
             t("p", t("strong", "Приложения: "), apps) if apps else t("p", t("strong", "Приложения: "), clientviews.no_apps(g)),
             t("p", t("strong", "Через VPN: "), ("свой список группы" if g.allowlist else "общий список") + counts + " · ",
               t("a", "изменить →", href=f"/groups/{g.id}#settings", data_swap=True)),
             t("p", t("strong", "Ставит: "), MODE_WHO[g.install_mode]))


def _list(names: list[str], limit: int = 8) -> str:
    """«a, b, c»; длинный список — число, начало и «и ещё N»."""
    if len(names) <= limit:
        return ", ".join(names)
    return f"{len(names)}: " + ", ".join(names[:5]) + f" и ещё {len(names) - 5}"


def _renamed(plan: people.Plan) -> list[str]:
    return [f"{r.display} → {r.name}" for r in plan.renamed]


def flash_report(req: "Request", rep: groups.GroupReport, renamed: list[str] | None = None) -> None:
    """Один итог на всё действие; ошибки по людям — по одной, но не больше ERRORS_SHOWN."""
    s = req.session
    if s is None:
        return
    reg = users.list_users()
    who = lambda ns: [u.label if (u := reg.get(n)) else n for n in ns]   # имя из списка, нет его — логин
    parts = [f"«{rep.group.name}»: {rep.message}"]
    if rep.created:
        parts.append("создано: " + _list(who(rep.created)))
    if rep.moved:
        parts.append("переведено: " + _list(who(rep.moved)))
    s.flash("ok" if rep.ok else "warn", ". ".join(parts))
    if rep.resend and not rep.created:
        s.flash("warn", "Переслать: " + _list(who(rep.resend)), [("кому и что", "/resend")])
    if renamed:
        s.flash("warn", "Совпали имена, добавлен номер: " + _list(renamed, 5))
    if rep.skipped:
        s.flash("warn", "Свой набор протоколов, группа его не тронула: " + _list(who(rep.skipped))
                + ". «Как у группы» в списке участников вернёт.")
    for e in rep.errors[:ERRORS_SHOWN]:
        s.flash("bad", e)
    if len(rep.errors) > ERRORS_SHOWN:
        s.flash("bad", f"…и ещё ошибок: {len(rep.errors) - ERRORS_SHOWN}")


# ---------- страницы групп ----------

def groups_list(app: "App", req: "Request") -> "Response":
    try:
        gs = groups.ensure()
    except CATCH as e:
        return app.render(req, "Группы", [page_head("Группы"), card("Группы", alert_list([("bad", _err(e))]))],
                          active="/groups")
    ureg = users.list_users()
    selectable = users.selectable_protocols()
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None
    rows = []
    for g in gs.groups:
        mem = groups.members_of(gs, ureg, g.id)
        protos = t("div", [t("span", manifests.proto_title(p), class_="chip") for p in g.offered(selectable)], class_="chips")
        apps = (groups.apps_line(cat, g.clients) if cat else " · ".join(f"{p}: {'+'.join(ids)}" for p, ids in g.clients.items())
                ) if g.clients else ""
        people = [[t("a", u.label, href=f"/users/{u.name}", title=u.name if u.display else None), ", "] for u in mem[:8]]
        if len(mem) > 8:
            people.append(f"и ещё {len(mem) - 8}")
        elif people:
            people[-1] = people[-1][0]
        rows.append([t("a", t("strong", g.name), href=f"/groups/{g.id}", data_swap=True), protos,
                     apps or t("a", "не выбраны", href=f"/groups/{g.id}#settings", class_="muted", data_swap=True),
                     "свой список" if g.allowlist else "общий", MODE_WORDS[g.install_mode],
                     people or t("span", "пусто", class_="muted"),
                     _delete_link(g, "btn small danger")])
    head = t("a", TITLE, href="/connect/new", class_="btn primary", data_swap=True,
             title="Протоколы, приложения, люди и что им отправить")
    parts: list[Any] = [page_head("Группы", f"{len(gs.groups)}", head),
                        card("Список", table(["группа", "протоколы", "приложения", "через VPN",
                                              ("ставит", "кто ставит приложения: ИТ или люди сами"),
                                              "участники", ""], rows,
                                             stack=True, empty="групп нет"),
                             help="Группа задаёт протоколы, приложения и список «через VPN» сразу всем участникам.")]
    lone = [u.name for u in ureg.visible() if not u.group]
    if lone:
        parts.append(alert_list([("warn", "Без группы: " + ", ".join(lone))]))
    return app.render(req, "Группы", parts, active="/groups")


def _delete_link(g: groups.Group, cls: str) -> Markup:
    """«Удалить» — страница подтверждения; «Основная» не удаляется (кнопка серая, причина в подсказке).
    Из списка групп ссылка помечена from=list: «Отмена» вернёт в список, а не в группу."""
    small = "small" in cls
    if g.id == groups.MAIN_ID:
        return t("span", "Удалить" if small else "Удалить группу", class_=cls, aria_disabled="true",
                 title=groups.MAIN_KEEP)
    return t("a", "Удалить" if small else "Удалить группу",
             href=f"/groups/{g.id}/delete" + ("?from=list" if small else ""), class_=cls,
             data_swap=True)


def _members_card(g: groups.Group, gs: groups.Groups, ureg: users.Registry, al: allowlist.Allowlist,
                  csrf: str) -> Markup:
    """Участники: число и имена чипами «Имя (логин)»; действия над отмеченными — одним списком с поиском."""
    others = [x for x in gs.groups if x.id != g.id]
    mem = groups.members_of(gs, ureg, g.id)
    if not mem:
        return card("Участники", t("p", "В группе никого нет.", class_="muted"))
    st = handoffviews.connection([u.name for u in mem])
    chips = []
    for u in mem:
        notes = (["свой набор протоколов: группа его не меняет"] if u.custom else []) + (
            ["свой список приложений"] if al.own(u.name) else []) + (
            ["уже подключился"] if st.connected(u.name) else [])
        chips.append(t("a", _person(u), t("span", " · " + people.device_titles(u.devices), class_="small")
                       if u.devices else None, href=f"/users/{u.name}",
                       class_="chip warn" if u.custom else ("chip ok" if st.connected(u.name) else "chip"),
                       title="; ".join(notes) or None))
    acts = [t("select", [t("option", x.name, value=x.id) for x in others], name="to", aria_label="В группу")
            if others else None,
            t("button", "Перевести", type="submit", name="act", value="move", class_="btn small") if others else None,
            t("button", "Убрать из группы", type="submit", name="act", value="remove", class_="btn small",
              title=f"Перевести в группу «{groups.MAIN_NAME}»") if g.id != groups.MAIN_ID else None,
            t("button", "Как у группы", type="submit", name="act", value="reset", class_="btn small",
              title="Вернуть протоколы и приложения группы") if any(u.custom for u in mem) else None]
    devs = None
    if len(g.devices) > 1:
        try:
            cat = clients.load()
            devs = t("div", t("span", "Устройства:", class_="label"), userviews.device_boxes(cat, g.devices, None),
                     t("button", "Задать отмеченным", type="submit", name="act", value="devices", class_="btn small"),
                     class_="actions")
        except clients.ClientsError:
            pass
    form = t("form", csrf_input(csrf), _picker("Отметьте участников:", "user", [(u.name, _person(u), "", False, u.note)
                                                                              for u in mem]),
             t("div", acts, class_="actions"), devs, method="post", action=f"/groups/{g.id}/move", class_="stack",
             data_swap=True)
    cards_url = "/handoff?" + urllib.parse.urlencode({"group": g.id})
    hand = [u.name for u in mem if u.name != users.OWNER]   # owner в раздачу не идёт (handoffviews.select)
    left = sum(1 for n in hand if not st.connected(n))
    go = t("div", t("a", "Карточки для раздачи", href=cards_url, class_="btn small primary", data_swap=True)
           if hand else None,
           t("a", f"Ещё не подключились: {left}", href=cards_url + "&only=pending", class_="btn small", data_swap=True)
           if st.known and 0 < left < len(hand) else None, class_="actions")
    return card("Участники", t("div", chips, class_="chips"),
                t("p", "Зелёные уже подключились (трафик за 30 дней).", class_="hint") if st.on else None,
                go,
                t("details", t("summary", "Действия с участниками"), form, class_="more"),
                extra=t("span", f"{st.counter}" if st.known else str(len(mem)), class_="chip" + (" ok" if st.on else ""),
                        title=None if not st.known else "Подключился — за 30 дней был трафик"))


def _messages_card(g: groups.Group, ctx: clientviews.Ctx, csrf: str, open_plat: str) -> Markup | None:
    """«Инструкция» по платформам: одна на группу, единственное место правки; человеку подставляется его имя вместо
    «{имя}». Своя инструкция — до «вернуть»."""
    if not g.clients:
        return card("Инструкция", t("p", clientviews.no_apps(g)), id_="text")
    items = []
    for plat, title in ctx.cat.platforms.items():
        default = ctx.default_text(g, plat)
        if default is None:
            continue
        mine = g.messages.get(plat)
        body = clientviews.editor_text(mine or default)
        stale = bool(mine and g.msg_sigs.get(plat) and g.msg_sigs[plat] != ctx.group_sig(g, plat))
        form = t("form", csrf_input(csrf), t("input", type="hidden", name="platform", value=plat),
                 alert_list([("warn", clientviews.STALE)]) if stale else None,
                 t("textarea", body, name="text", rows=str(min(16, len(body.splitlines()) + 2)),
                   maxlength=str(groups.MESSAGE_MAX), spellcheck="false", class_="msg-edit",
                   aria_label=f"Инструкция: {title}"),
                 t("div", t("button", "Сохранить", type="submit", class_="btn primary small"),
                   t("button", "Вернуть по умолчанию", type="submit", name="reset", value="1", class_="btn small")
                   if mine else None, class_="actions"),
                 method="post", action=f"/groups/{g.id}/message", class_="stack", data_swap=True)
        items.append(t("details", t("summary", title, " ", badge("своя", "info") if mine else None,
                                     *([" ", badge("проверьте", "warn")] if stale else [])), form,
                       open=plat == open_plat or None, class_="more"))
    if not items:
        return None
    return card("Инструкция", t("p", f"Одна на платформу для всех. «{clientviews.NAME_TOKEN_RU}» заменится именем человека; "
                                     "ключей в инструкции нет.", class_="hint"),
                t("div", items), id_="text")


def group_page(app: "App", req: "Request", gid: str, d: Draft | None = None, errors: list[str] | None = None,
               status: int = 200, add_draft: Draft | None = None) -> "Response":
    try:
        gs = groups.ensure()
    except CATCH as e:
        return app.error(req, 500, "Группы не читаются", _err(e))
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    csrf = req.session.csrf if req.session else ""
    d = d or Draft.of_group(g)
    facts = proto_facts()
    managed = [f.id for f in facts]
    selected = d.resolved(managed)
    ureg = users.list_users()
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return app.error(req, 500, "Список «через VPN» не читается", str(e))
    normalize_clients(d, managed, fill=False)
    dist_card = distviews.card_for(gs, g.id, f"/groups/{g.id}", csrf) if g.install_mode == "admin" else None
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="action", value="save"),
             t("input", type="hidden", name="devs", value="1"),
             t("button", "Сохранить настройки", type="submit", hidden=True, tabindex="-1"),   # Enter в названии сохраняет, а не «Пересчитать»
             t("div", t("label", "Название группы", for_="name"),
               t("input", type="text", name="name", id="name", value=d.name, required=True,
                 maxlength=str(groups.NAME_MAX), autocomplete="off"), class_="field"),
             _mode_seg(d.install_mode),
             t("h3", "Протоколы", class_="sub-h"), _protocols_block(facts, selected, g.protocols),
             t("h3", "Приложения", class_="sub-h"),
             _clients_block(Draft(protocols=selected, clients=d.clients, devices=d.devices, install_mode=d.install_mode),
                            managed, ios_hint=dist_card is None),
             t("h3", "Через VPN", class_="sub-h"), _apps_block(d),
             t("div", t("button", "Сохранить настройки", type="submit", class_="btn primary"), class_="actions"),
             method="post", action=f"/groups/{g.id}", class_="stack", data_swap=True)
    add = t("details", t("summary", "＋ Добавить людей списком"),
            t("form", csrf_input(csrf), _users_block(add_draft or Draft(new_users="", existing=[]), g.id),
              t("button", "Проверить список →", type="submit", class_="btn primary"),
              method="post", action=f"/groups/{g.id}/members", class_="stack", data_swap=True),
            class_="card more", open=bool(add_draft or req.query.get("add")) or None, id="add")
    others = [x for x in gs.groups if x.id != g.id]
    mem_users = groups.members_of(gs, ureg, g.id)
    names = [u.label for u in mem_users]
    own = [u.label for u in mem_users if u.custom]
    who = ", ".join(names[:8]) + (f" и ещё {len(names) - 8}" if len(names) > 8 else "")
    merge = t("details", t("summary", "Объединить с…"),
              t("form", csrf_input(csrf),
                t("p", "Участники этой группы перейдут в выбранную и получат её протоколы и приложения, эта группа "
                       "будет удалена, настройки выбранной не изменятся."
                       + (f" Свой набор протоколов сохранится: {', '.join(own)}." if own else ""), class_="hint"),
                t("select", [t("option", x.name, value=x.id) for x in others], name="to", aria_label="Объединить с группой"),
                t("button", "Объединить", type="submit", class_="btn small"),
                method="post", action=f"/groups/{g.id}/merge", class_="actions", data_swap=True,
                data_confirm=f"Объединить: участники «{g.name}» ({who or 'никого нет'}) перейдут в выбранную "
                             f"группу и получат её протоколы и приложения, «{g.name}» будет удалена."
                             + (f" Свой набор протоколов сохранится: {', '.join(own)}." if own else "")
                             + " Отменить нельзя."),
              class_="more") if others and g.id != groups.MAIN_ID else None
    delete = _delete_link(g, "btn danger")
    parts: list[Any] = [page_head(g.name, "группа", t("a", "← Группы", href="/groups", class_="btn small", data_swap=True), top=False)]
    if errors:
        parts.append(alert_list([("bad", e) for e in errors]))
    ctx = clientviews.Ctx.load()
    parts += [_members_card(g, gs, ureg, al, csrf),
              add, dist_card,
              _messages_card(g, ctx, csrf, req.query.get("m", "")) if ctx else None,
              card("Настройки группы", form, id_="settings",
                   help="Сохранение применяется ко всем участникам один раз; кому что переслать — в «Кому переслать»."),
              t("div", delete, merge, class_="actions")]
    return app.render(req, g.name, parts, active="/groups", status=status)


def group_get(app: "App", req: "Request", gid: str) -> "Response":
    return group_page(app, req, gid)


def _done(app: "App", req: "Request", rep: groups.GroupReport, back: str) -> "Response":
    app.invalidate("status")
    app.invalidate_links()
    flash_report(req, rep)
    return _redirect(back)


def group_save(app: "App", req: "Request", gid: str) -> "Response":
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    d = Draft.from_form(req)
    if req.form.get("go") == "refresh":
        return group_page(app, req, gid, d)
    try:
        selectable = users.selectable_protocols()
        # «Основная» следует за включением протоколов, пока в форме отмечено всё включённое
        protos = [groups.ALL] if g.all_protocols and set(selectable) <= set(d.protocols) else d.protocols
        rep = groups.update(g.id, name=d.name, protocols=protos, clients=d.clients, allow=d.allow_arg(),
                            install_mode=d.install_mode, drop=req.multi.get("drop", [])[:20])
    except CATCH as e:
        return group_page(app, req, gid, d, [_err(e)], 422)
    return _done(app, req, rep, f"/groups/{g.id}")


def group_message(app: "App", req: "Request", gid: str) -> "Response":
    plat = req.form.get("platform", "")[:20]
    back = f"/groups/{gid}?" + urllib.parse.urlencode({"m": plat}) + "#text"
    try:
        g = groups.Groups.load().require(gid)
        text: str | None = (None if req.form.get("reset")
                            else groups.clean_message(clientviews.stored_text(req.form.get("text", ""))))
        ctx = clientviews.Ctx.load()
        if text and ctx is not None and text == ctx.default_text(g, plat):
            text = None  # совпал с умолчанием — не замораживаем версии приложений в файле
        groups.set_message(g.id, plat, text, ctx.group_sig(g, plat) if ctx is not None else None)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(back)
    req.session.flash("ok", "Инструкция сохранена" if text else "Инструкция по умолчанию")
    return _redirect(back)


def group_members(app: "App", req: "Request", gid: str) -> "Response":
    """«Добавить людей списком»: без confirm — предпросмотр (что получится из текста), с confirm — создание."""
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    text = req.form.get("users_new", "")[:people.TEXT_MAX]
    existing = [n[:32] for n in req.multi.get("existing", [])][:200]
    try:
        plan = people.plan_for_registry(text)
        if not plan.ok:   # список не теряется: форма открыта с тем же текстом
            return group_page(app, req, gid, errors=[plan.error], status=422,
                              add_draft=Draft(new_users=text, existing=existing))
        if req.form.get("go") == "edit":
            return group_page(app, req, gid, add_draft=Draft(new_users=text, existing=existing))
        if plan.rows and not req.form.get("confirm"):
            return _members_preview(app, req, g, plan, text, existing)
        rep = groups.add_members(g.id, plan.entries(), existing)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{g.id}")
    rep.message = "участники добавлены"
    app.invalidate("status")
    app.invalidate_links()
    flash_report(req, rep, _renamed(plan))
    return _redirect(f"/groups/{g.id}")


def _members_preview(app: "App", req: "Request", g: groups.Group, plan: people.Plan, text: str,
                     existing: list[str]) -> "Response":
    csrf = req.session.csrf if req.session else ""
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="users_new", value=text),
             t("input", type="hidden", name="confirm", value="1"),
             [t("input", type="hidden", name="existing", value=n) for n in existing],
             _preview_rows(plan, g.devices),
             t("div", t("button", _count_label("Добавить", len(plan.rows), len(existing)), type="submit",
                        class_="btn primary"),
               t("button", "← Изменить список", type="submit", name="go", value="edit", class_="btn", formnovalidate=True),
               class_="wiz-nav"),
             method="post", action=f"/groups/{g.id}/members", class_="stack", data_swap=True)
    body = card(f"Проверьте список: группа «{g.name}»", t("p", _preview_summary(plan, len(existing)), class_="hint"), form)
    return app.render(req, g.name, t("div", [page_head(g.name, "добавление людей"), body], data_expanded=True),
                      active="/groups")


def _names(req: "Request", key: str) -> list[str]:
    return list(dict.fromkeys(n[:32] for n in req.multi.get(key, [])[:200]))


def group_move(app: "App", req: "Request", gid: str) -> "Response":
    act = req.form.get("act", "move")
    to = {"remove": groups.MAIN_ID, "reset": gid}.get(act, req.form.get("to", "")[:40])
    names = _names(req, "user")
    if act == "remove" and gid == groups.MAIN_ID:
        req.session.flash("bad", f"Из «{groups.MAIN_NAME}» убирать некуда: переведите в другую группу")
        return _redirect(f"/groups/{gid}")
    if not names:
        req.session.flash("warn", "Никого не выбрано")
        return _redirect(f"/groups/{gid}")
    try:
        gs = groups.Groups.load()
        here = {u.name for u in groups.members_of(gs, users.list_users(), gid)}
        if any(n not in here for n in names):
            raise groups.GroupError("отмеченные не из этой группы: обновите страницу")
        if act == "devices":
            stored = groups.set_devices(names, [d[:16] for d in req.multi.get("dev", [])][:8])
            req.session.flash("ok", f"{userviews.devices_flash(clients.load(), stored[names[0]])} — {len(names)} чел.")
            return _redirect(f"/groups/{gid}")
        rep = groups.move_many(names, to)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    rep.message = "готово"
    return _done(app, req, rep, f"/groups/{gid}")


def group_delete_confirm(app: "App", req: "Request", gid: str) -> "Response":
    gs = groups.Groups.load()
    g = gs.get(gid)
    if g is None:
        return app.error(req, 404, "Нет группы", f"Группы «{gid}» нет.")
    csrf = req.session.csrf if req.session else ""
    back = t("a", "Отмена", href="/groups" if req.query.get("from") == "list" else f"/groups/{g.id}",
             class_="btn", data_swap=True)
    if g.id == groups.MAIN_ID:
        body = card(f"«{g.name}» не удаляется", t("p", groups.MAIN_KEEP + "."), back)
        return app.render(req, "Удаление группы", [page_head("Удаление группы"), body], active="/groups")
    members = groups.members_of(gs, users.list_users(), g.id)
    mem = [u.name for u in members]
    own = [u.name for u in members if u.custom]
    if mem:
        gone = [n for n in mem if n != users.OWNER]
        kept_own = [n for n in own if n not in gone]
        choice: Any = t("div",
                        t("label", t("input", type="radio", name="members", value="move", checked=True),
                          t("span", f"Перевести в группу «{groups.MAIN_NAME}»",
                            t("span", f" ({len(mem)}): " + ", ".join(mem), class_="muted small"),
                            t("span", "Свой набор протоколов сохранится: " + ", ".join(own), class_="hint")
                            if own else None, class_="opt-title"),
                          class_="opt-row"),
                        t("label", t("input", type="radio", name="members", value="delete"),
                          t("span", "Удалить вместе с группой",
                            t("span", " (" + (", ".join(gone) or "никого") + ")", class_="muted small"),
                            t("span", f"{users.OWNER} не удаляется — он перейдёт в «{groups.MAIN_NAME}»"
                              + (" (свой набор протоколов сохранится)" if kept_own else ""), class_="hint")
                            if users.OWNER in mem else None,
                            t("span", "Креды удалённых будут стёрты из протоколов, ссылки и QR перестанут работать.",
                              class_="hint") if gone else None, class_="opt-title"),
                          class_="opt-row"))
    else:
        choice = t("input", type="hidden", name="members", value="move")
    shown = [t("input", type="hidden", name="shown", value="1")] + [
        t("input", type="hidden", name="expected", value=n) for n in mem]
    form = t("form", csrf_input(csrf), shown, choice,
             t("div", t("button", "Удалить группу", type="submit", class_="btn danger-solid"), back, class_="actions"),
             method="post", action=f"/groups/{g.id}/delete", class_="stack", data_swap=True)
    body = card(f"Удалить группу «{g.name}»?",
                t("p", f"В группе участников: {len(mem)}. Что с ними сделать?" if mem else
                  "В группе никого нет. Группа будет удалена."), form, cls="danger-zone")
    return app.render(req, "Удаление группы", [page_head("Удаление группы"), body], active="/groups")


def group_delete(app: "App", req: "Request", gid: str) -> "Response":
    mode = req.form.get("members", "move")
    s = req.session
    try:
        if mode not in ("move", "delete"):
            raise groups.GroupError("выберите, что сделать с участниками")
        expected = list(dict.fromkeys(req.multi.get("expected", []))) if req.form.get("shown") == "1" else None
        rep = groups.delete_group(gid, mode, expected)
    except groups.MembersChanged as e:
        s.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}/delete")
    except CATCH as e:
        s.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    app.invalidate("status")
    app.invalidate_links()
    parts = [rep.message]
    if rep.deleted:
        parts.append("удалены: " + ", ".join(rep.deleted))
    if rep.moved:
        parts.append(f"переведены в «{rep.group.name}»: " + ", ".join(rep.moved))
    s.flash("ok" if rep.removed and not rep.errors else "warn", ". ".join(parts))
    if rep.skipped:
        s.flash("warn", "Свой набор протоколов, группа его не тронула: " + ", ".join(rep.skipped))
    for e in rep.errors:
        s.flash("bad", e)
    return _redirect("/groups" if rep.removed else f"/groups/{gid}")


def group_merge(app: "App", req: "Request", gid: str) -> "Response":
    to = req.form.get("to", "")[:40]
    try:
        rep = groups.merge_groups(gid, to)
    except CATCH as e:
        req.session.flash("bad", _err(e))
        return _redirect(f"/groups/{gid}")
    return _done(app, req, rep, f"/groups/{rep.group.id}")
