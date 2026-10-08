"""Раздача пачкой (BACKLOG п. 18): «Карточки для раздачи» — по карточке на человека (приложения, его QR и ссылки,
шаги из текста группы), печать на A4 и архив ZIP с папкой на каждого; статус «подключился» по трафику.

Страница и архив собираются на сервере из тех же ссылок, что страница пользователя (кэш _cached_links), и тем же
выбором клиентов (clientviews.build_pack): отличий от блока «Подключить» нет. Ключи в архиве и на странице — те же
ключи доступа, поэтому выгрузка — только POST с CSRF и предупреждением."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .. import clients, groups, paths, people, protolib, qr, traffic, users
from ..fsutil import LockTimeout
from . import clientviews, userviews
from .html import Markup, card, csrf_input, t
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

HANDOFF_MAX = 200
PAGE_BUDGET = 60.0      # секунд на сбор ссылок для страницы; не успевшие — «обновите»
EXPORT_BUDGET = 150.0   # то же для архива: не вошедшие перечисляются в README
WORKERS = 4
ZIP_MAX = 60 * 1024 * 1024
CONNECTED_DAYS = 30
PER_SHEET = ("2", "3")
NOTE_WARN = ("Файлы содержат ключи доступа: не отправляйте их через MAX и VK — лично, на бумаге или мессенджером "
             "со сквозным шифрованием.")
FORMULA = ("=", "+", "-", "@", "\t", "\r")
FILE_SAFE = re.compile(r"[^a-z0-9._-]+")


# ---------- статус «подключился» ----------

@dataclass
class Status:
    known: bool = False                   # есть ли вообще данные трафика
    on: set[str] = field(default_factory=set)
    total: int = 0

    def connected(self, name: str) -> bool:
        return name in self.on

    @property
    def counter(self) -> str:
        return f"подключились {len(self.on)} из {self.total}" if self.known else "подключения по трафику не видны"


def connection(names: list[str], now: float | None = None) -> Status:
    """Подключился — если за 30 дней был хоть байт трафика или есть отметка последней активности не старше 30 дней."""
    try:
        rep = traffic.report(period=f"{CONNECTED_DAYS}d", by="user")
        seen = traffic.last_seen()
    except (sqlite3.Error, OSError, ValueError):
        return Status(False, set(), len(names))
    if rep.get("empty"):
        return Status(False, set(), len(names))
    now = time.time() if now is None else now
    month = {r["key"]: r["total"] for r in rep["rows"]}
    on = {n for n in names if month.get(n, 0) > 0 or (n in seen and now - seen[n] <= CONNECTED_DAYS * 86400)}
    return Status(True, on, len(names))


def status_chip(st: Status, name: str) -> Markup | None:
    if not st.known:
        return None
    if st.connected(name):
        return t("span", "подключился", class_="chip ok", title="трафик за 30 дней есть")
    return t("span", "ещё нет", class_="chip", title="трафика за 30 дней не было")


# ---------- выбор людей ----------

@dataclass
class Selection:
    names: list[str] = field(default_factory=list)
    group: groups.Group | None = None
    only_pending: bool = False
    notes: list[str] = field(default_factory=list)
    query: dict[str, str] = field(default_factory=dict)   # как выбор записывается в адрес и скрытые поля


def select(group: str, listed: str, only: str) -> Selection:
    """Кого раздавать: участники группы или перечисленные имена. Сервер ничему не верит: имена — по реестру,
    служебные, owner (ключи самого админа) и отключённые не берутся, больше HANDOFF_MAX — отказ."""
    ureg = users.list_users()
    sel = Selection(only_pending=only == "pending")
    if group:
        gs = groups.Groups.load()
        g = gs.get(group[:40])
        if g is None:
            raise LookupError(f"группы «{group[:40]}» нет")
        sel.group = g
        cand = [u for u in groups.members_of(gs, ureg, g.id)]
        sel.query["group"] = g.id
    else:
        want = list(dict.fromkeys(n for n in listed.split(",") if n))[: HANDOFF_MAX + 1]
        cand = []
        for n in want:
            u = ureg.get(n) if users.NAME_RE.match(n) else None
            if u is None or u.system:
                sel.notes.append(f"«{n[:32]}» нет в реестре — пропущен")
            else:
                cand.append(u)
        sel.query["u"] = ",".join(u.name for u in cand if u.name != users.OWNER)
    if any(u.name == users.OWNER for u in cand):
        sel.notes.append(f"{users.OWNER} — ключи администратора, в раздачу не идут (они на его странице)")
        cand = [u for u in cand if u.name != users.OWNER]
    off = [u.name for u in cand if not u.enabled]
    cand = [u for u in cand if u.enabled]
    if off:
        sel.notes.append("отключённые не включены: " + ", ".join(off[:10]) + (f" и ещё {len(off) - 10}" if len(off) > 10 else ""))
    if sel.only_pending:
        st = connection([u.name for u in cand])
        if st.known:
            cand = [u for u in cand if not st.connected(u.name)]
            sel.query["only"] = "pending"
        else:
            sel.notes.append("данных трафика нет: показаны все")
            sel.only_pending = False
    if len(cand) > HANDOFF_MAX:
        raise groups.GroupError(f"за раз — не больше {HANDOFF_MAX} человек, выбрано {len(cand)}")
    sel.names = [u.name for u in cand]
    return sel


# ---------- карточки ----------

@dataclass
class CardKey:
    title: str
    qr: int | None = None
    tag: str = ""
    uri: str | None = None
    file: str | None = None


@dataclass
class CardApp:
    name: str
    version: str | None
    foreign: str   # подпись про магазин («нет в App Store РФ», у ИТ — «нужен иностранный Apple ID») или пусто
    stores: list[dict[str, Any]]
    keys: list[CardKey]


@dataclass
class Block:
    platforms: list[str]
    apps: list[CardApp]
    steps: list[str]
    via: str = ""   # «Через VPN — …» под названием платформы


@dataclass
class Card:
    user: users.User
    group: groups.Group | None
    links: list[protolib.Link] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    pending: bool = False   # ссылки не успели собрать

    @property
    def title(self) -> str:
        """Как к человеку обращаться: имя из списка, нет его — логин."""
        return self.user.label


SENT_RE = re.compile(r",? (?:который|которую) я пришлю")


def words(step: str, where: str) -> str:
    """Тексты групп написаны для сообщения («QR, который я пришлю»); на карточке и в папке ключ уже у человека."""
    return SENT_RE.sub(" " + where, step)


def _steps(ctx: clientviews.Ctx, g: groups.Group | None, plat: str, pack: clientviews.Pack, name: str) -> list[str]:
    """Шаги — строки инструкции группы (или пакета, если у человека свои протоколы), без заголовка, строки «Через VPN»
    (она на карточке отдельно) и номеров."""
    body = None
    if g is not None:
        gt = ctx.text(g, plat)
        if gt and clientviews.pack_sig(pack) == ctx.group_sig(g, plat):
            body = gt
    lines = clientviews.fill_name(body or pack.message, name).splitlines()[1:]
    return [re.sub(r"^\d+\)\s*", "", ln).strip() for ln in lines
            if ln.strip() and not ln.startswith(clientviews.VIA_PREFIX)]


def build_blocks(ctx: clientviews.Ctx, user: users.User, g: groups.Group | None,
                 links: list[protolib.Link]) -> list[Block]:
    """Платформы с одинаковыми приложениями и ключами сворачиваются в один блок («Android, iPhone — Happ»)."""
    prefer, order = clientviews.group_prefs(g)
    admin = bool(g and g.install_mode == "admin")   # ставит ИТ: человеку магазины не показываются, а iPhone — «нужен иностранный Apple ID»
    merged: dict[Any, Block] = {}
    for plat, title in ctx.cat.platforms.items():
        pack = clientviews.build_pack(ctx.cat, ctx.cache, plat, links, ctx.mans, prefer, order,
                                      clientviews.store_first(g), ctx.al, ctx.apps_for(plat, g, user.name), admin)
        if pack is None:
            continue
        keys = [clientviews._keys(s, plat, links) for s in pack.sections]
        apps = [CardApp(s.client["name"], s.version, clientviews.foreign_note(ctx.cat, s.client, plat, admin),
                        [] if admin else s.links, [CardKey(k.title, k.qr, k.qr_tag, k.uri, k.file) for k in ks])
                for s, ks in zip(pack.sections, keys)]
        steps = _steps(ctx, g, plat, pack, user.label)
        # сворачиваются только платформы с одинаковым всем, что видит человек: магазины и шаги у платформ свои
        sig = (tuple((a.name, a.version, a.foreign, tuple(ln["url"] for ln in a.stores),
                      tuple((k.title, k.qr, k.uri, k.file) for k in a.keys)) for a in apps), tuple(steps), pack.via)
        if sig in merged:
            merged[sig].platforms.append(title)
            continue
        merged[sig] = Block([title], apps, steps, pack.via)
    return list(merged.values())


def fetch_links(app: "App", names: list[str], budget: float) -> dict[str, tuple[list[protolib.Link], dict[str, str]] | None]:
    """Ссылки людей. Первый — один (xui_hdr создаётся без блокировки), остальные — по WORKERS параллельно;
    не уложились в budget — у оставшихся None (их сбор доделается в фоне и ляжет в кэш страницы пользователя)."""
    out: dict[str, Any] = {n: None for n in names}
    if not names:
        return out
    start = time.monotonic()

    def one(n: str) -> tuple[list[protolib.Link], dict[str, str]]:
        try:
            return userviews._cached_links(app, n)
        except (users.UserError, protolib.ProtoError, OSError, LockTimeout) as e:
            return [], {"": str(e)}

    out[names[0]] = one(names[0])
    rest = names[1:]
    if rest:
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        try:
            futs = {pool.submit(one, n): n for n in rest}
            done, _ = wait(futs, timeout=max(budget - (time.monotonic() - start), 0.1))
            for f in done:
                out[futs[f]] = f.result()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return out


def build_cards(app: "App", names: list[str], budget: float) -> list[Card]:
    ctx = clientviews.Ctx.load()
    ureg = users.list_users()
    gs = groups.Groups.load()
    got = fetch_links(app, names, budget)
    cards = []
    for n in names:
        u = ureg.get(n)
        if u is None:
            continue
        card_ = Card(u, gs.get(u.group))
        res = got.get(n)
        if res is None:
            card_.pending = True
        else:
            card_.links, card_.errors = res
            if ctx is not None:
                card_.blocks = build_blocks(ctx, u, card_.group, card_.links)
        cards.append(card_)
    return cards


# ---------- страница ----------

def _key_html(k: CardKey, name: str) -> Markup:
    parts: list[Any] = []
    if k.qr is not None:
        parts.append(t("img", class_="qr", src=f"/users/{name}/qr/{k.qr}?p={k.tag}", width=120, height=120,
                       alt=f"QR: {k.title}"))
    parts.append(t("div", k.title, class_="key-name"))
    if k.uri:
        parts.append(t("code", k.uri, class_="hlink"))   # целиком: обрезанная ссылка на печати не работает
    if k.file and clientviews.FILE_NAME_RE.fullmatch(k.file):
        parts.append(t("a", "Скачать файл", href=f"/users/{name}/file/{k.file}", class_="btn small noprint"))
    return t("div", parts, class_="hkey")


def _block_html(b: Block, name: str) -> Markup:
    apps = [t("div", t("strong", a.name), " " + a.version if a.version else None,
              t("span", " " + a.foreign, class_="chip warn") if a.foreign else None,
              t("div", [t("a", clients.LINK_KINDS[ln["kind"]], href=ln["url"], target="_blank",
                          rel="noopener noreferrer", class_="chip info noprint") for ln in a.stores],
                class_="chips") if a.stores else None,
              t("div", [_key_html(k, name) for k in a.keys], class_="hkeys"), class_="happ") for a in b.apps]
    return t("section", t("h4", ", ".join(b.platforms), class_="plat-title"),
             t("p", b.via, class_="hint") if b.via else None, apps,
             t("ol", [t("li", words(s, "с этой карточки")) for s in b.steps], class_="hsteps") if b.steps else None, class_="hblock")


def _card_html(c: Card, st: Status) -> Markup:
    u = c.user
    head = t("header", t("strong", u.label), t("span", u.name, class_="hnote") if u.display else None,
             t("span", u.note, class_="hnote") if u.note else None, status_chip(st, u.name), class_="hhead")
    if c.pending:
        body: Any = t("p", "Ссылки ещё собираются — обновите страницу.", class_="muted")
    elif c.group is not None and not c.group.clients:
        body = t("p", clientviews.no_apps(c.group))
    elif c.blocks:
        body = [_block_html(b, u.name) for b in c.blocks]
    else:
        body = alert_list([("warn", "Ссылок нет: " + ("; ".join(f"{k or 'модуль'}: {v}" for k, v in c.errors.items())
                                                       or "у пользователя нет протоколов"))])
    return t("article", head, body, class_="hcard", data_name=u.name)


def _post_form(path: str, csrf: str, sel: Selection, fmt: str, label: str, cls: str) -> Markup:
    fields = [t("input", type="hidden", name=k, value=v) for k, v in sel.query.items()]
    return t("form", csrf_input(csrf), fields, t("input", type="hidden", name="fmt", value=fmt),
             t("button", label, type="submit", class_=cls), method="post", action=path, class_="inline")


def _url(sel: Selection, **extra: str) -> str:
    from urllib.parse import urlencode
    return "/handoff?" + urlencode({**sel.query, **extra})


def cards_page(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    try:
        sel = select(req.query.get("group", ""), req.query.get("u", ""), req.query.get("only", ""))
    except LookupError as e:
        return app.error(req, 404, "Карточки для раздачи", str(e))
    except (groups.GroupError, users.UserError) as e:
        return app.error(req, 422, "Карточки для раздачи", str(e))
    per = req.query.get("per", "3") if req.query.get("per") in PER_SHEET else "3"
    st = connection(sel.names)
    back = (t("a", f"← {sel.group.name}", href=f"/groups/{sel.group.id}", class_="btn small", data_swap=True)
            if sel.group else t("a", "← Пользователи", href="/users", class_="btn small", data_swap=True))
    parts: list[Any] = [page_head("Карточки для раздачи", sel.group.name if sel.group else "выбранные", back, top=False)]
    if sel.notes:
        parts.append(alert_list([("warn", n) for n in sel.notes]))
    if not sel.names:
        parts.append(alert_list([("warn", "Раздавать некому" + (": все уже подключились" if sel.only_pending else "")
                                          + ".")]))
        return app.render(req, "Карточки для раздачи", parts, active="/groups")
    cards = build_cards(app, sel.names, PAGE_BUDGET)
    pending = sum(c.pending for c in cards)
    sheet = t("div", "На листе A4: ",
              [[t("a", n, href=_url(sel, per=n), class_="btn small" + (" primary" if n == per else ""), data_swap=True), " "]
               for n in PER_SHEET], class_="hper noprint")
    bar = t("div",
            t("span", st.counter, class_="chip" + (" ok" if st.known and st.on else ""),
              title="Подключился — за 30 дней был трафик"),
            t("button", "Печать", type="button", class_="btn primary", data_print=True, hidden=True),
            _post_form("/handoff/export", csrf, sel, "zip", "Скачать ZIP", "btn"),
            _post_form("/handoff/export", csrf, sel, "csv", "CSV: имя → ссылка", "btn"),
            t("a", "Только кто ещё не подключился", href=_url(sel, only="pending", per=per), class_="btn small",
              data_swap=True) if st.known and not sel.only_pending and len(st.on) < st.total else None,
            class_="actions noprint")
    parts += [card(f"Карточек: {len(cards)}", bar, sheet,
                   t("p", NOTE_WARN, " ZIP — папка на человека: QR, файлы и instruction.txt; index.csv — для рассылки.",
                     class_="hint"),
                   t("p", f"{pending} карточек ещё собираются — обновите страницу.", class_="hint") if pending else None,
                   cls="noprint"),
              t("div", [_card_html(c, st) for c in cards], class_=f"hcards per-{per}")]
    return app.render(req, "Карточки для раздачи", t("div", parts, class_="handoff", data_expanded=True),
                      active="/groups")


# ---------- выгрузка ----------

def _cell(v: str) -> str:
    """Ячейка CSV: начинающееся с «=», «+», «-», «@» Excel принял бы за формулу."""
    return "'" + v if v.startswith(FORMULA) else v


def csv_bytes(cards: list[Card], with_files: dict[str, dict[str, str]] | None = None) -> bytes:
    """index.csv: имя (как в списке);заметка;протокол;ссылка. Файловые ключи — путь внутри архива (with_files: {имя: {файл ключа:
    имя в папке}} — только то, что в архив попало), без ссылки — пропуск."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["имя", "заметка", "протокол", "ссылка"])
    for c in cards:
        seen: set[tuple[str, str]] = set()
        for b in c.blocks:
            for a in b.apps:
                for k in a.keys:
                    put_as = (with_files or {}).get(c.user.name, {}).get(k.file or "")
                    link = k.uri or (f"{c.user.name}/{put_as}" if put_as else "")
                    if link and (k.title, link) not in seen:
                        seen.add((k.title, link))
                        w.writerow([_cell(c.user.label), _cell(c.user.note), k.title, link])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _qr_file(payload: str, base: str) -> tuple[str, bytes] | None:
    """PNG через qrencode; если он PNG не умеет — SVG. Нет qrencode или длинно для QR — None."""
    try:
        return base + ".png", qr.png(payload)
    except qr.QrError:
        pass
    try:
        return base + ".svg", qr.svg(payload).encode("utf-8")
    except qr.QrError:
        return None


def instruction_text(c: Card, qr_files: dict[Any, str], files: dict[str, str]) -> str:
    u = c.user
    who = u.label + (f" ({u.name})" if u.display else "")
    out = [who + (f" — {u.note}" if u.note else ""), ("Группа: " + c.group.name) if c.group else "", ""]
    for b in c.blocks:
        out.append(f"{', '.join(b.platforms)}: " + ", ".join(f"«{a.name}»" for a in b.apps))
        if b.via:
            out.append(f"  {b.via}")
        out += [f"  {i}) {words(s, 'из этой папки')}" for i, s in enumerate(b.steps, 1)]
        out.append("")
    out.append("Ключи доступа (никому не пересылайте):")
    # один ключ на нескольких платформах — одна запись, QR-картинки собираются к ней
    keys: dict[Any, list[str]] = {}
    for b in c.blocks:
        for a in b.apps:
            for k in a.keys:
                qrs = keys.setdefault((k.title, k.uri, k.file), [])
                pic = qr_files.get((k.title, k.qr)) if k.qr is not None else None
                if pic and pic not in qrs:
                    qrs.append(pic)
    for (title, uri, file), qrs in keys.items():
        out.append(f"  {title}")
        out += [f"    QR: {q}" for q in qrs]
        if uri:
            out.append(f"    ссылка: {uri}")
        if file and file in files:
            out.append(f"    файл: {files[file]}")
    out += ["", clientviews.SEND_WARN]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def _free_base(base: str, used: set[str]) -> str:
    cand, n = base, 1
    while f"{cand}.png" in used or f"{cand}.svg" in used:
        n += 1
        cand = f"{base}-{n}"
    return cand


def build_zip(app: "App", cards: list[Card], skipped: list[str], budget: float = EXPORT_BUDGET) -> bytes:
    """Архив: на человека папка (QR, .conf, instruction.txt) + index.csv и README.txt в корне."""
    buf = io.BytesIO()
    start = time.monotonic()
    stamp = datetime.now().timetuple()[:6]
    problems: list[str] = []
    left: list[str] = []
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(path: str, data: bytes) -> None:
            nonlocal total
            zi = zipfile.ZipInfo(path, stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o600 << 16
            z.writestr(zi, data)
            total += len(data)

        done: list[Card] = []
        archived: dict[str, dict[str, str]] = {}
        for c in cards:
            if time.monotonic() - start > budget or total > ZIP_MAX:
                left.append(c.user.name)
                continue
            if c.pending or not c.blocks:
                problems.append(f"{c.user.name}: ссылок нет" + (" (не успели собрать)" if c.pending else ""))
                continue
            name = c.user.name
            qr_files: dict[Any, str] = {}
            qr_failed: set[Any] = set()   # платформы с тем же ключом — одна строка в README, не по строке на блок
            files: dict[str, str] = {}
            missing: set[str] = set()
            used: set[str] = set()
            for b in c.blocks:
                for a in b.apps:
                    for k in a.keys:
                        if k.qr is not None and (k.title, k.qr) not in qr_files and (k.title, k.qr) not in qr_failed:
                            payload = userviews._payload(c.links[k.qr], name) if k.qr < len(c.links) else None
                            base = _free_base(f"qr-{people.slug(k.title)}".strip("-"), used)
                            made = _qr_file(payload, base) if payload else None
                            if made:
                                used.add(made[0])
                                put(f"{name}/{made[0]}", made[1])
                                qr_files[(k.title, k.qr)] = made[0]
                            else:
                                qr_failed.add((k.title, k.qr))
                                problems.append(f"{name}: QR «{k.title}» не построен (нет qrencode или ключ длинный)")
                        if k.file and k.file not in files and clientviews.FILE_NAME_RE.fullmatch(k.file):
                            # .conf и правила v2rayN (v2rayn-routing.json): инструкция велит импортировать оба
                            f = userviews._file_ok(str(paths.clients_dir() / name / k.file), name)
                            if f is None:
                                if k.file not in missing:
                                    missing.add(k.file)
                                    problems.append(f"{name}: файла {k.file} нет на сервере — в архив не вошёл")
                            else:
                                try:
                                    put(f"{name}/{f.name}", f.read_bytes())
                                    files[k.file] = f.name
                                except OSError as e:
                                    problems.append(f"{name}: {k.file}: {e}")
            put(f"{name}/instruction.txt", instruction_text(c, qr_files, files).encode("utf-8"))
            archived[name] = files
            done.append(c)
        put("index.csv", csv_bytes(done, with_files=archived))
        readme = ["Раздача VPN: " + str(len(done)) + " человек.", "", NOTE_WARN, "",
                  "index.csv — имя;заметка;протокол;ссылка (откройте в Excel: разделитель «;»).",
                  "В папке человека: QR-картинки, файлы ключей (.conf, правила v2rayN — где нужны) и instruction.txt.", ""]
        if left:
            readme += [f"Не вошли (не хватило времени, повторите): {', '.join(left)}", ""]
        if skipped:
            readme += skipped + [""]
        if problems:
            readme += ["Проблемы:"] + [f"  {p}" for p in problems] + [""]
        put("README.txt", "\n".join(readme).encode("utf-8"))
    return buf.getvalue()


def export(app: "App", req: "Request") -> "Response":
    from .app import Response
    fmt = req.form.get("fmt", "zip")
    try:
        sel = select(req.form.get("group", ""), req.form.get("u", ""), req.form.get("only", ""))
    except (LookupError, groups.GroupError, users.UserError) as e:
        req.session.flash("bad", str(e))
        return _redirect("/groups")
    if fmt not in ("zip", "csv") or not sel.names:
        req.session.flash("warn", "Раздавать некому")
        return _redirect(_url(sel))
    cards = build_cards(app, sel.names, EXPORT_BUDGET)
    day = datetime.now().strftime("%Y%m%d")
    tag = FILE_SAFE.sub("-", sel.group.id if sel.group else "vybor")
    if fmt == "csv":
        done = [c for c in cards if not c.pending]
        return Response(200, csv_bytes(done), "text/csv; charset=utf-8", headers=[
            ("Content-Disposition", f'attachment; filename="vpn-{tag}-{day}.csv"')])
    data = build_zip(app, cards, sel.notes)
    return Response(200, data, "application/zip", headers=[
        ("Content-Disposition", f'attachment; filename="vpn-{tag}-{day}.zip"')])


def _redirect(location: str) -> "Response":
    from .app import redirect
    return redirect(location)
