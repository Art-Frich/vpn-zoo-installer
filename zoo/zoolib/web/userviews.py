"""Страницы пользователей: список, добавление, вкл/выкл, удаление, ссылки и QR, трафик."""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import allowlist, manifests, paths, protolib, qr, traffic, users
from ..fsutil import LockTimeout
from ..output import human_bytes
from ..probe import rank
from . import charts
from .html import Markup, badge, card, csrf_input, join, kv, post_button, t, table
from .views import (ago, alert_list, chart_block, fmt_time, get_period, no_history_hint, page_head,
                    period_selector)

if TYPE_CHECKING:
    from .app import App, Request, Response

ACTION_TEXT = {"add": "добавлен", "adopt": "учтён (уже был)", "del": "удалён", "enable": "включён",
               "disable": "отключён", "rollback": "откат", "forget": "забыт"}
LINKS_TTL = 3600  # плюс проверка отметок времени файлов (см. _cached_links)


def _redirect(location: str) -> "Response":
    from .app import redirect
    return redirect(location)


def flash_report(req: "Request", rep: users.OpReport) -> None:
    """Итог операции и ошибки по протоколам — во flash-сообщения."""
    s = req.session
    if s is None:
        return
    if rep.ok:
        done = [st.proto_id for st in rep.steps if st.ok and st.action not in ("rollback", "forget")]
        s.flash("ok", f"{rep.user}: {rep.message}" + (f" ({', '.join(done)})" if done else ""))
    else:
        s.flash("bad", f"{rep.user}: {rep.message}")
    for st in rep.steps:
        if not st.ok:
            s.flash("bad", f"{st.proto_id}: {st.error}")
    if rep.rolled_back:
        s.flash("warn", "Изменения в остальных протоколах откатены.")


def _user_op(req: "Request", fn, *args, **kw) -> users.OpReport | None:
    try:
        rep = fn(*args, **kw)
    except (users.UserError, LockTimeout) as e:
        req.session.flash("bad", str(e))
        return None
    except protolib.ProtoError as e:
        req.session.flash("bad", f"{e} {e.short()}")
        return None
    flash_report(req, rep)
    return rep


def _disable_confirm(name: str) -> str:
    return f"Отключить {name}? Ссылки сохранятся, но подключиться он не сможет."


def _created_local(created: str) -> str:
    """Дата создания из реестра (UTC, ISO) — в местном времени."""
    try:
        return fmt_time(datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return created or "—"


# ---------- список ----------

def _proto_chip(u: users.User, managed: list[str]) -> Markup:
    have = [p for p in managed if p in u.protocols]
    missing = [p for p in managed if p not in u.protocols]
    if not missing:
        return t("span", f"{len(have)}/{len(managed)}", class_="chip", title=", ".join(have) or None)
    shown = ", ".join(missing[:2]) + ("…" if len(missing) > 2 else "")
    return t("span", f"нет: {shown}", class_="chip warn", title=f"{len(have)}/{len(managed)}; нет в: {', '.join(missing)}")


def users_list(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    reg = users.list_users()
    managed, skipped = users.managed_protocols()
    day = {r["key"]: r["total"] for r in traffic.report(period="24h", by="user")["rows"]}
    month = {r["key"]: r["total"] for r in traffic.report(period="30d", by="user")["rows"]}
    seen = traffic.last_seen()
    mx = max(day.values(), default=0)
    rows, row_cls = [], []
    shown = reg.visible()
    for u in shown:
        toggle = post_button(f"/users/{u.name}/{'disable' if u.enabled else 'enable'}",
                             "Отключить" if u.enabled else "Включить", csrf, "btn small",
                             {"back": "/users"}, confirm=_disable_confirm(u.name) if u.enabled else None)
        rows.append([
            t("span", t("a", t("strong", u.name), href=f"/users/{u.name}"), " " if not u.enabled else None,
              badge("откл.", "muted") if not u.enabled else None,
              t("span", u.note, class_="sub") if u.note else None),
            _proto_chip(u, managed),
            human_bytes(day.get(u.name, 0)), charts.bar(day.get(u.name, 0), mx),
            human_bytes(month.get(u.name, 0)), t("span", ago(seen.get(u.name)), class_="nowrap"),
            toggle,
        ])
        row_cls.append(None if u.enabled else "off")
    tbl = table(["пользователь", "протоколы", "24 ч", "", "30 дней", "активность", ""], rows,
                num=[2, 4], empty="пользователей нет", stack=True, row_cls=row_cls)
    if not reg.exists:
        tbl = join(alert_list([("warn", "Реестра users.json ещё нет: он создастся при первом изменении "
                                         "(или фазой 09).")]), tbl)
    protos = [t("label", t("input", type="checkbox", name="proto", value=p, checked=True), p) for p in managed]
    add_form = t("form", csrf_input(csrf),
                 t("div",
                   t("div", t("label", "Имя", for_="name"),
                     t("input", type="text", name="name", id="name", required=True, maxlength="32",
                       pattern="[A-Za-z0-9][A-Za-z0-9_\\-]{0,31}", placeholder="masha", autocomplete="off",
                       autocapitalize="none", spellcheck="false",
                       title="Латиница, цифры, «-» и «_»; регистр не важен. Пользователь получает креды во всех "
                             "отмеченных протоколах; при ошибке в одном изменения откатываются."), class_="field"),
                   t("div", t("label", "Заметка", for_="note"),
                     t("input", type="text", name="note", id="note", maxlength="200", placeholder="кто это"),
                     class_="field grow"),
                   t("button", "Добавить", type="submit", class_="btn primary"), class_="form-row"),
                 t("div", t("span", "Протоколы:", class_="label"), t("div", protos, class_="checks"), class_="field")
                 if protos else alert_list([("warn", "Нет протоколов, куда можно добавить пользователя.")]),
                 method="post", action="/users", class_="stack", data_swap=True)
    verify, missing = (verify_card() if req.query.get("verify") else (None, False))
    head_actions = t("div", t("a", "Сверить", href="/users?verify=1", class_="btn small", data_swap=True,
                              title="Сверить реестр с тем, что есть в протоколах"),
                     post_button("/users/sync", "Синхронизировать", csrf, "btn small",
                                 title="Завести креды в протоколах, включённых после создания") if missing else None,
                     class_="actions")
    parts: list[Any] = [page_head("Пользователи", f"{sum(u.enabled for u in shown)}/{len(shown)} включено", head_actions)]
    if verify:
        parts.append(verify)
    parts.append(card("Список", tbl))
    parts.append(t("details", t("summary", "＋ Добавить пользователя"), add_form, class_="card more"))
    notes = [t("span", f"не участвует: {k}", class_="chip", title=v) for k, v in skipped.items()]
    notes += [t("a", f"служебный: {u.name}", href=f"/users/{u.name}", class_="chip",
                title="Служебный пользователь пробника: скрыт из списков и отчётов трафика, его креды "
                      "использует самопроверка; удалить можно только из консоли.")
              for u in reg.users if u.system]
    if notes:
        parts.append(t("div", notes, class_="chips"))
    return app.render(req, "Пользователи", parts, active="/users")


def verify_card() -> tuple[Markup, bool]:
    """(карточка, есть ли недостающие в протоколах — их лечит «Синхронизировать»)."""
    items: list[tuple[str, Any]] = []
    missing = False
    for pid, d in users.verify().items():
        if d["error"]:
            items.append(("warn", f"{pid}: не удалось сверить — {d['error']}"))
        for n in d["missing"]:
            missing = True
            items.append(("bad", f"{pid}: {n} есть в реестре, но нет в протоколе"))
        for n in d["extra"]:
            items.append(("warn", f"{pid}: {n} есть в протоколе, но нет в реестре"))
    return card("Сверка с протоколами", alert_list(items or [("ok", "Расхождений нет")])), missing


def user_add(app: "App", req: "Request") -> "Response":
    name = req.form.get("name", "").strip().lower()
    note = req.form.get("note", "").strip()[:200]
    chosen = req.multi.get("proto", [])
    managed, _ = users.managed_protocols()
    only = None if not chosen or set(managed) <= set(chosen) else [p for p in chosen if p in managed]
    if chosen and only == []:
        req.session.flash("bad", "Не выбран ни один протокол")
        return _redirect("/users")
    rep = _user_op(req, users.add_user, name, note=note, only=only)
    app.invalidate("status")
    app.invalidate_links(name)
    return _redirect(f"/users/{name}" if rep and rep.ok else "/users")


def users_sync(app: "App", req: "Request") -> "Response":
    try:
        reports = users.sync_users()
    except (users.UserError, LockTimeout) as e:
        req.session.flash("bad", str(e))
        return _redirect("/users")
    changed = [r for r in reports if r.steps]
    for r in changed:
        flash_report(req, r)
    if not changed:
        req.session.flash("ok", "Все пользователи уже во всех протоколах")
    app.invalidate("status")
    app.invalidate_links()
    return _redirect("/users")


def user_toggle(app: "App", req: "Request", name: str, action: str) -> "Response":
    _user_op(req, users.set_enabled, name, action == "enable")
    app.invalidate_links(name)
    back = req.form.get("back", "")
    return _redirect(back if back in ("/users", f"/users/{name}") else f"/users/{name}")


def user_delete_confirm(app: "App", req: "Request", name: str) -> "Response":
    user = users.list_users().get(name)
    if user is None:
        return app.error(req, 404, "Нет пользователя", f"Пользователя «{name}» нет.")
    csrf = req.session.csrf if req.session else ""
    if name == users.OWNER or user.system:
        why = ("На owner держатся ссылки по умолчанию. Его можно отключить" if name == users.OWNER else
               "Это служебный пользователь пробника: его кредами идёт самопроверка. Его нельзя отключить")
        body = card(f"{name} не удаляется", t("p", f"{why}; удалить — только из консоли: ",
                                              t("code", f"sudo zoo user del {name} --force")),
                    t("a", "← назад", href=f"/users/{name}", class_="btn"))
        return app.render(req, "Удаление", body, active="/users")
    body = card(f"Удалить «{name}»?",
                t("p", "Креды пользователя будут удалены из протоколов: ", t("strong", ", ".join(user.protocols) or "—"),
                  ". Его ссылки и QR перестанут работать. Отменить нельзя — только создать заново с новыми ключами."),
                t("div", t("form", csrf_input(csrf), t("button", "Удалить навсегда", type="submit", class_="btn danger-solid"),
                           method="post", action=f"/users/{name}/delete", class_="inline", data_swap=True),
                  t("a", "Отмена", href=f"/users/{name}", class_="btn"), class_="actions"),
                cls="danger-zone")
    return app.render(req, "Удаление", [page_head("Удаление пользователя"), body], active="/users")


def user_delete(app: "App", req: "Request", name: str) -> "Response":
    user = users.list_users().get(name)
    if name == users.OWNER or (user is not None and user.system):
        req.session.flash("bad", f"{name} не удаляется из админки")
        return _redirect(f"/users/{name}")
    rep = _user_op(req, users.delete_user, name)
    app.invalidate("status")
    app.invalidate_links(name)
    return _redirect("/users" if rep and rep.ok else f"/users/{name}")


# ---------- страница пользователя ----------

_SVG_TAGS = {"svg", "g", "rect", "path"}


def clean_qr_svg(svg: str) -> Markup | None:
    """SVG от qrencode → чистая разметка под CSP без 'unsafe-inline':
    style="stroke:#000" → stroke="#000", без XML-пролога, размеров и id."""
    s = re.sub(r"<\?xml.*?\?>|<!--.*?-->", "", svg, flags=re.S).strip()
    tags = {m.lower() for m in re.findall(r"</?\s*([a-zA-Z][\w:-]*)", s)}
    if not s.startswith("<svg") or not tags <= _SVG_TAGS or re.search(r"\son\w+\s*=", s, re.I):
        return None

    def style_attrs(m: re.Match[str]) -> str:
        out = []
        for decl in m.group(1).split(";"):
            k, _, v = decl.partition(":")
            k, v = k.strip(), v.strip()
            if k in ("fill", "stroke", "stroke-width") and re.fullmatch(r"[#\w.() ,%-]+", v):
                out.append(f'{k}="{v}"')
        return " ".join(out)

    s = re.sub(r'style="([^"]*)"', style_attrs, s)
    s = re.sub(r'\s(width|height)="[^"]*cm"', "", s, count=2)
    s = re.sub(r'\sid="[^"]*"', "", s)
    return Markup(s)


# выдаются только клиентские конфиги (AWG .conf) и правила v2rayN; прочее в clients/<name>/ —
# секреты модулей
CLIENT_FILE_SUFFIXES = {".conf"}
CLIENT_FILE_NAMES = {allowlist.V2RAYN_FILE}
FILE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _file_ok(path: str, name: str) -> Path | None:
    """Файл для выдачи — только clients/<name>/*.conf и правила v2rayN самого пользователя.
    config.env, ключи и чужие каталоги не отдаются, даже если путь придёт откуда угодно."""
    try:
        real = Path(path).resolve()
        real.relative_to((paths.clients_dir() / name).resolve())
    except (ValueError, OSError):
        return None
    if real.suffix not in CLIENT_FILE_SUFFIXES and real.name not in CLIENT_FILE_NAMES:
        return None
    return real if real.is_file() else None


def _payload(link: protolib.Link, name: str) -> str | None:
    if link.kind == "uri":
        return link.uri
    f = _file_ok(link.uri, name)
    if f and f.suffix == ".conf":
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def user_page(app: "App", req: "Request", name: str) -> "Response":
    reg = users.list_users()
    user = reg.get(name)
    if user is None:
        return app.error(req, 404, "Нет пользователя", f"Пользователя «{name}» нет в реестре.")
    csrf = req.session.csrf if req.session else ""
    period = get_period(req, "7d")
    seen = traffic.last_seen().get(name)

    toggle = post_button(f"/users/{name}/{'disable' if user.enabled else 'enable'}",
                         "Отключить" if user.enabled else "Включить", csrf, "btn",
                         confirm=_disable_confirm(name) if user.enabled else None)
    actions = t("div", toggle, t("a", "Удалить…", href=f"/users/{name}/delete", class_="btn danger"),
                class_="actions")
    if user.system:
        actions = badge("служебный: пробник", "muted")
    try:
        own = allowlist.Allowlist.load().own(name)
    except allowlist.AllowlistError:
        own = False
    info = card("Профиль", kv([
        ("статус", badge("включён", "ok") if user.enabled else
         t("span", "отключён", class_="badge muted", title="креды сохранены, доступ закрыт")),
        ("через VPN", t("a", "свой список приложений" if own else "общий список приложений",
                        href=f"/apps?user={name}")),
        ("заметка", user.note or "—"),
        ("создан", _created_local(user.created)),
        ("активность", ago(seen)),
    ]))

    rep = traffic.report(user=name, period=period, by="protocol")
    selector = period_selector(f"/users/{name}", period)
    if rep.get("empty"):
        tr_body: Any = no_history_hint(csrf)
        extra: Any = selector
    else:
        ts = traffic.timeseries(period, "protocol", user=name)
        tr_rows = [[r["title"], human_bytes(r["up"]), human_bytes(r["down"]), human_bytes(r["total"])]
                   for r in rep["rows"]]
        tr_body = [chart_block(ts, f"Трафик {name} по протоколам"),
                   table(["протокол", ("↑", "от клиента"), ("↓", "к клиенту"), ("Σ", "всего")], tr_rows,
                         num=[1, 2, 3], empty="трафика не было", stack=True)]
        extra = t("div", t("span", f"Σ {human_bytes(rep['total']['total'])}", class_="chip",
                           title=f"Итого {traffic.PERIOD_TITLES[period]}"), selector, class_="actions")
    tr_card = card("Трафик", tr_body, extra=extra)

    links, errors = _cached_links(app, name)
    err_list = alert_list([("warn", f"{pid}: ссылки не получены — {e}") for pid, e in errors.items()]) if errors else None
    links_card = card("Подключение", err_list, quick_start(links, name),
                      connect_tiles(links, manifests.load_all()[0], name) or t("p", "Ссылок нет.", class_="muted"),
                      help="Ссылки и QR — ключи доступа: показывайте только самому пользователю.")
    body = [page_head(name, user.note or None, actions), links_card, t("div", info, tr_card, class_="cols")]
    return app.render(req, name, body, active="/users")


def _cached_links(app: "App", name: str) -> tuple[list[protolib.Link], dict[str, str]]:
    """Ссылки собираются вызовами bash-модулей протоколов (сотни мс). Кэш на час, пока не менялись
    файлы пользователя, реестр пользователей и манифесты; удаление, отключение и смена списка
    приложений сбрасывают его сами (App.invalidate_links)."""
    def mtimes(d: Path) -> float:
        try:
            return max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()])
        except OSError:
            return 0.0
    stamp = (mtimes(paths.clients_dir() / name), mtimes(paths.manifest_dir()),
             mtimes(paths.users_file().parent) if paths.users_file().exists() else 0.0)
    key = f"links:{name}"
    hit = app.cache_get(key, LINKS_TTL)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    data = users.user_links(name)
    app.cache_put(key, (stamp, data), LINKS_TTL)
    return data


# Плитки «Подключение»: протокол → платформы (на плитке) и клиенты (в title)
MAIN_PROTOS = ("vless-reality", "vless-xhttp", "hysteria2", "amneziawg")
PLATFORMS = {
    "vless-reality": "iPhone, Android, Windows",
    "vless-xhttp": "iPhone, Android, Windows",
    "hysteria2": "iPhone, Android, Windows",
    "amneziawg": "Android, iPhone, Windows",
    "tuic": "iPhone, Android, Windows",
    "ss2022": "iPhone, Android, Windows",
    allowlist.V2RAYN_PROTO: "Windows",
}
CLIENTS = {
    "vless-reality": "Happ, v2rayNG, v2rayN",
    "vless-xhttp": "Happ, v2rayNG, v2rayN",
    "hysteria2": "Happ, v2rayNG, Hiddify",
    "amneziawg": "AmneziaWG, AmneziaVPN, WG Tunnel",
    "tuic": "Hiddify, Karing, sing-box",
    "ss2022": "Happ, v2rayNG",
    allowlist.V2RAYN_PROTO: "v2rayN",
}
QUICK_CAPTION = "Happ → «+» → сканировать"


def _variant_order(link: protolib.Link) -> int:
    """Файлы раньше ссылок, Android-конфиг — первым: так окно AmneziaWG открывается на нём."""
    if link.kind == "file":
        return 0 if Path(link.uri).name.endswith("-android.conf") else 1
    return 2


def _variant_label(link: protolib.Link, uri_n: int) -> tuple[str, str | None]:
    """(текст вкладки, подсказка): термины — в подсказку, на вкладке «Обычная» / «Запасная N»."""
    if link.kind == "file":
        fname = Path(link.uri).name
        if fname.endswith("-android.conf"):
            return "Android", "список приложений Android внутри файла"
        if fname.endswith(".conf"):
            return "Компьютер, iPhone", "общий .conf без списка приложений"
        return "Файл правил", None
    if link.uri.startswith("vpn://"):
        return "Ключ AmneziaVPN", None
    why = link.label or None
    if "obfs=salamander" in link.uri:
        why = "Salamander: обфускация Hysteria2"
    elif re.search(r"@[^/?#]*:\d+,\d", link.uri):
        why = "Port hopping: порт меняется"
    return ("Обычная" if uri_n == 1 else f"Запасная {uri_n}"), why


def _variant(link: protolib.Link, idx: int, name: str, vid: str, hidden: bool) -> Markup:
    fname = Path(link.uri).name if link.kind == "file" else ""
    if link.kind == "file":
        action = (t("a", "Скачать файл", href=f"/users/{name}/file/{fname}", class_="btn primary")
                  if FILE_NAME_RE.fullmatch(fname) else t("p", "Файл недоступен для скачивания", class_="muted small"))
    else:
        uri_id = f"uri-{idx}"
        action = t("div", t("input", type="text", id=uri_id, value=link.uri, readonly=True, data_select=True,
                            aria_label="Ссылка"),
                   t("button", "Копировать", type="button", class_="btn primary", data_copy=uri_id),
                   class_="link-uri")
    if link.proto_id == allowlist.V2RAYN_PROTO:
        qr_block: Any = t("p", "v2rayN → Маршрутизация → Импорт из файла.", class_="hint")
    elif link.kind == "uri" and len(link.uri.encode("utf-8")) > qr.MAX_BYTES:
        qr_block = t("p", "Ссылка слишком длинная для QR", class_="muted small")
    elif link.kind == "file" and not fname.endswith(".conf"):
        qr_block = None
    else:
        # QR рисуется по требованию: app.js ставит src видимому варианту при открытии окна и смене вкладки
        qr_block = t("img", class_="qr", data_src=f"/users/{name}/qr/{idx}", width=240, height=240, alt="QR")
    return t("div", qr_block, action, class_="variant", id=vid, hidden=hidden or None)


def quick_start(links: list[protolib.Link], name: str) -> Markup | None:
    """Один QR лучшего протокола (по истории проб, иначе VLESS REALITY) и «скопировать всё»."""
    cand = [(i, l) for i, l in enumerate(links)
            if l.kind == "uri" and not l.uri.startswith("vpn://") and len(l.uri.encode("utf-8")) <= qr.MAX_BYTES]
    if not cand:
        return None
    prefer: list[str] = []
    try:
        for c in rank.load("30d"):
            prefer += [p["proto"] for p in c["top"]]
    except (sqlite3.Error, OSError, ValueError):
        pass
    idx, link = next(((i, l) for pid in (*prefer, "vless-reality") for i, l in cand if l.proto_id == pid), cand[0])
    all_uris = "\n".join(l.uri for l in links if l.kind == "uri")
    return t("div",
             t("img", class_="qr", src=f"/users/{name}/qr/{idx}", width=160, height=160, alt="QR",
               title=link.proto_id),
             t("div", t("h3", "Быстрый старт"), t("p", QUICK_CAPTION, class_="muted"),
               t("textarea", all_uris, id="copy-all", hidden=True, readonly=True),
               t("div", t("button", "Скопировать всё", type="button", class_="btn", data_copy="copy-all",
                          title="Все ссылки по одной в строке"), class_="actions")),
             class_="quick")


def connect_tiles(links: list[protolib.Link], mans: list[Any], name: str) -> Markup | None:
    """Плитки по протоколам; клик — окно протокола: вкладки вариантов, QR, копировать/скачать."""
    if not links:
        return None
    by_id = {m.id: m for m in mans}
    groups: dict[str, list[tuple[int, protolib.Link]]] = {}
    for i, link in enumerate(links):
        groups.setdefault(link.proto_id, []).append((i, link))
    order = sorted(groups, key=lambda p: (p not in MAIN_PROTOS,
                                          MAIN_PROTOS.index(p) if p in MAIN_PROTOS else 99, p))
    sections: dict[bool, list[Markup]] = {True: [], False: []}
    dialogs = []
    for n, pid in enumerate(order):
        m = by_id.get(pid)
        title = (m.name if m else ("Приложения через VPN" if pid == allowlist.V2RAYN_PROTO else pid)).partition(" (")[0]
        dlg_id = f"dlg-{n}"
        sections[pid in MAIN_PROTOS].append(t(
            "button", t("span", title, class_="ptile-name"),
            t("span", PLATFORMS.get(pid, "—"), class_="ptile-sub"),
            type="button", class_=f"ptile acc{n % 8 + 1}", data_dialog=dlg_id,
            title=f"Клиенты: {CLIENTS[pid]}" if pid in CLIENTS else None))
        items = sorted(groups[pid], key=lambda it: _variant_order(it[1]))  # sorted стабилен: порядок модуля цел
        tabs_data, uri_n = [], 0
        for k, (_, link) in enumerate(items):
            if link.kind == "uri" and not link.uri.startswith("vpn://"):
                uri_n += 1
            tabs_data.append(_variant_label(link, uri_n))
        tabs = t("div", [t("button", label, type="button", data_tab=f"{dlg_id}-v{k}", title=why,
                           class_="tab active" if k == 0 else "tab")
                         for k, (label, why) in enumerate(tabs_data)], class_="tabs", role="tablist") \
            if len(items) > 1 else None
        variants = [_variant(link, i, name, f"{dlg_id}-v{k}", k > 0) for k, (i, link) in enumerate(items)]
        more = t("details", t("summary", "подробнее"), t("p", m.notes, class_="hint")) if m and m.notes else None
        dialogs.append(t("dialog",
                         t("div", t("h3", title), t("button", "✕", type="button", class_="btn small", data_close=True,
                                                    aria_label="Закрыть"), class_="dlg-head"),
                         tabs, variants, more, id=dlg_id, class_="pdlg"))
    out = []
    for main, label in ((True, "Основные"), (False, "Запасные")):
        if sections[main]:
            out += [t("h3", label, class_="sub-h"), t("div", sections[main], class_="ptiles")]
    return t("div", out, dialogs)


def user_file(app: "App", req: "Request", name: str, fname: str) -> "Response":
    """Файл по имени из clients/<name>/: модули протоколов не вызываются (быстро, и нечему врать про путь)."""
    from .app import Response
    if users.list_users().get(name) is None:
        return app.error(req, 404, "Нет пользователя", f"Пользователя «{name}» нет.")
    f = _file_ok(str(paths.clients_dir() / name / fname), name)
    if f is None:
        return app.error(req, 404, "Нет файла", "Файл не найден или лежит вне каталога клиента.")
    data = f.read_bytes()
    out = f"{name}-{f.name}"
    return Response(200, data, "application/octet-stream",
                    headers=[("Content-Disposition",
                              f"attachment; filename=\"{out}\"; filename*=UTF-8''{urllib.parse.quote(out)}")])


def user_qr(app: "App", req: "Request", name: str, idx: str) -> "Response":
    """QR одного варианта (по номеру в списке ссылок пользователя) — картинка для <img>."""
    from .app import Response, text
    if users.list_users().get(name) is None:
        return text("нет пользователя", 404)
    links, _ = _cached_links(app, name)
    i = int(idx)
    if i >= len(links) or links[i].proto_id == allowlist.V2RAYN_PROTO:
        return text("нет такого варианта", 404)
    payload = _payload(links[i], name)
    if not payload:
        return text("QR для этого варианта не строится", 404)
    try:
        svg = clean_qr_svg(qr.svg(payload))
    except qr.QrError as e:
        return text(f"QR: {e}", 502)
    if svg is None:
        return text("QR: неожиданный формат qrencode", 502)
    return Response(200, str(svg).encode("utf-8"), "image/svg+xml")
