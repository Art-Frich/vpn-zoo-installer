"""Страница «Приложения через VPN»: одна таблица по приложениям, черновик и одно сохранение
(общий список и свои списки пользователей, zoo allow)."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import allowlist, protolib, users
from ..fsutil import LockTimeout
from .html import Markup, badge, card, csrf_input, join, post_button, t, table
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

SHORT = {"android": "Android", "windows": "Windows"}
PLACEHOLDER = {"android": "com.example.app", "windows": "program.exe"}
HINT = {"android": "Пакет Android — как в адресе Google Play: play.google.com/store/apps/details?id=ПАКЕТ.",
        "windows": "Имя процесса как в Диспетчере задач (вкладка «Подробности»), с учётом регистра."}
HELP = join(t("p", "Включённое идёт через VPN, остальное — напрямую. В каждом списке должно остаться "
                   "хотя бы одно приложение."),
            t("p", "Android — AmneziaWG и WG Tunnel, Windows — v2rayN. После сохранения нужны новые QR "
                   "и файл v2rayN."))
PENDING = ("custom_title", "custom_android", "custom_windows")  # поля «Своё приложение»


@dataclass
class _Row:
    title: str
    ids: dict[str, str | None] = field(default_factory=dict)
    custom: bool = False


def _url(user: str | None) -> str:
    return "/apps" + (f"?{urllib.parse.urlencode({'user': user})}" if user else "")


def _back(user: str | None) -> "Response":
    from .app import redirect
    return redirect(_url(user))


def _user_arg(value: str | None) -> tuple[str | None, str]:
    """(имя или None, ошибка): только пользователь из реестра."""
    name = (value or "").strip()
    if not name:
        return None, ""
    u = users.list_users().get(name)
    if u is None:
        return None, f"Пользователя «{name}» нет в реестре"
    if u.system:
        return None, f"{name} — служебный пользователь, своего списка у него нет"
    return name, ""


def _rows(al: allowlist.Allowlist, user: str | None, draft: dict[str, list[str]] | None,
          titles: dict[str, str]) -> list[_Row]:
    """Весь каталог, затем свои приложения из списка (и из черновика), не из каталога."""
    rows = [_Row(a.title, {p: getattr(a, p) for p in allowlist.PLATFORMS}) for a in allowlist.CATALOG]
    known = {(p, i.lower()) for r in rows for p, i in r.ids.items() if i}
    extra: dict[str, _Row] = {}
    for p in allowlist.PLATFORMS:
        for ident in [*al.effective(p, user), *(draft or {}).get(p, [])]:
            if (p, ident.lower()) in known:
                continue
            known.add((p, ident.lower()))
            title = titles.get(ident.lower(), "")
            key = title.lower() or ident.lower()
            if key in extra and p in extra[key].ids:  # два id одной платформы под одним названием — две строки
                key = f"{key}\0{p}\0{ident.lower()}"
            extra.setdefault(key, _Row(title, {}, True)).ids[p] = ident
    return rows + list(extra.values())


def _toggle(platform: str, ident: str, name: str, was: bool, on: bool) -> Markup:
    return t("label", t("input", type="checkbox", name=platform, value=ident, checked=on,
                        data_was="1" if was else "0", aria_label=f"{SHORT[platform]}: {name}"),
             t("i"), class_="tgl")


def _row_cells(r: _Row, was: dict[str, set[str]], on: dict[str, set[str]]) -> list[Any]:
    ids = [i for i in r.ids.values() if i]
    if r.custom:
        name = r.title or ids[0]
        head = [t("span", name), t("span", " · ".join(ids), class_="sub") if r.title else None,
                [t("input", type="hidden", name=f"title:{i}", value=r.title) for i in ids] if r.title else None]
    else:
        name = r.title
        head = t("span", name, title=" · ".join(ids))
    cells: list[Any] = [head]
    for p in allowlist.PLATFORMS:
        ident = r.ids.get(p)
        cells.append(_toggle(p, ident, name, ident.lower() in was[p], ident.lower() in on[p]) if ident
                     else t("span", "—", class_="muted"))
    return cells


def _custom_box(pending: dict[str, str]) -> Markup:
    def field_(key: str, label: str, hint: str | None = None, grow: bool = False) -> Markup:
        p = key.removeprefix("custom_")
        return t("div", t("label", label, for_=key),
                 t("input", type="text", name=key, id=key, value=pending.get(key) or None, data_pend=True,
                   placeholder=PLACEHOLDER.get(p), maxlength="128", autocomplete="off",
                   autocapitalize="none", spellcheck="false"),
                 t("div", hint, class_="hint") if hint else None, class_="field grow")
    return t("details",
             t("summary", "Своё приложение"),
             t("div", field_("custom_title", "Название (необязательно)"),
               field_("custom_android", "Пакет Android", HINT["android"]),
               field_("custom_windows", "Процесс Windows", HINT["windows"]),
               t("p", "Добавится в список после «Сохранить».", class_="hint"),
               t("button", "Добавить", type="button", class_="btn", hidden=True, data_add=True),
               t("p", "", class_="hint err", hidden=True, data_cu_err=True), class_="stack"),
             open=any(pending.values()) or None, class_="custom")


def _list_form(al: allowlist.Allowlist, user: str | None, csrf: str, draft: dict[str, list[str]] | None,
               titles: dict[str, str], pending: dict[str, str]) -> Markup:
    was = {p: {i.lower() for i in al.effective(p, user)} for p in allowlist.PLATFORMS}
    on = {p: {i.lower() for i in draft[p]} for p in allowlist.PLATFORMS} if draft else was
    rows = [_row_cells(r, was, on) for r in _rows(al, user, draft, titles)]
    box = card(f"Список {user}" if user else "Общий список",
               table(["приложение", "Android", "Windows"], rows, num=[1, 2], cls="apps"),
               _custom_box(pending), help=HELP)
    bar = t("div", t("span", "", class_="count", data_count=True, aria_live="polite"),
            t("button", "Сохранить", type="submit", class_="btn primary", data_save=True),
            t("a", "Отменить", href=_url(user), class_="btn", data_cancel=True), class_="savebar")
    return t("form", csrf_input(csrf), t("input", type="hidden", name="action", value="save"),
             t("input", type="hidden", name="user", value=user) if user else None, box, bar,
             method="post", action="/apps", class_="stack", data_draft=True, data_swap=True,
             data_re_android=allowlist.ANDROID_RE.pattern, data_re_windows=allowlist.WINDOWS_RE.pattern)


def _who_nav(reg: users.Registry, user: str | None) -> Markup:
    """Чей список — сегментами-ссылками: «Общий» и по одному на пользователя."""
    links = [t("a", "Общий", href="/apps", class_="active" if not user else None, title="список для всех")]
    for u in reg.visible():
        links.append(t("a", u.name, href=_url(u.name), class_="active" if u.name == user else None))
    return t("nav", links, class_="seg", aria_label="Чей список")


def _diff(al: allowlist.Allowlist, user: str) -> tuple[int, int]:
    """(+добавлено, −убрано) относительно списка без своего: группы или общего."""
    add = rem = 0
    for p in allowlist.PLATFORMS:
        cur, base = {i.lower() for i in al.effective(p, user)}, {i.lower() for i in al.baseline(p, user)}
        add, rem = add + len(cur - base), rem + len(base - cur)
    return add, rem


def baseline_name(al: allowlist.Allowlist, user: str) -> str:
    return "как у группы" if al.from_group(user) else "как общий"


def _page(app: "App", req: "Request", user: str | None, err: str = "", draft: dict[str, list[str]] | None = None,
          titles: dict[str, str] | None = None, pending: dict[str, str] | None = None,
          errors: list[str] | None = None, status: int = 200) -> "Response":
    csrf = req.session.csrf if req.session else ""
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return app.error(req, 500, "Список приложений не читается", str(e))
    reg = users.list_users()
    parts: list[Any] = [page_head("Приложения через VPN",
                                  "Через VPN идут только отмеченные приложения, остальное — напрямую "
                                  "(банки, Госуслуги, MAX).", _who_nav(reg, user))]
    alerts = [("bad", m) for m in ([err] if err else []) + (errors or [])]
    if not al.exists:
        alerts.append(("info", f"{al.path.name} ещё нет: действует пресет."))
    if alerts:
        parts.append(alert_list(alerts))
    if user:
        own = al.own(user)
        add, rem = _diff(al, user)
        grp = al.from_group(user)
        reset = post_button("/apps", "Вернуть как у группы" if grp else "Вернуть общий", csrf, "btn small",
                            {"action": "reset", "user": user},
                            title="Удалить свой список: пользователь снова на списке " + ("группы" if grp else "общем"),
                            confirm=f"Вернуть {user} список {'группы' if grp else 'общий'}? Его свой список будет удалён.",
                            swap=True) if own else None
        parts.append(t("div", badge(f"свой (отличается: +{add} −{rem})", "info") if own and (add or rem)
                       else badge(baseline_name(al, user), "muted"), reset,
                       t("a", f"Ссылки и QR {user} →", href=f"/users/{user}"), class_="actions"))
    merged = {**al.titles, **{k.lower(): allowlist.clean_title(v) for k, v in (titles or {}).items()}}
    parts.append(_list_form(al, user, csrf, draft, merged, pending or {}))
    if not user and al.users:
        own_rows = [[t("a", n, href=_url(n), data_swap=True), "+{} −{}".format(*_diff(al, n))]
                    for n in sorted(al.users)]
        parts.append(card("Свои списки пользователей",
                          table(["пользователь", "отличия от списка группы или общего"], own_rows, stack=True),
                          extra=post_button("/apps", "Сбросить к пресету", csrf, "btn small", {"action": "reset"},
                                            title="Общий список снова как в пресете", swap=True,
                                            confirm="Сбросить общий список к пресету? Свой список придётся собирать заново.")))
    elif not user:
        parts.append(t("div", post_button("/apps", "Сбросить к пресету", csrf, "btn small", {"action": "reset"},
                                          title="Общий список снова как в пресете", swap=True,
                                          confirm="Сбросить общий список к пресету?"), class_="actions"))
    return app.render(req, "Приложения через VPN", parts, active="/apps", status=status)


def apps_page(app: "App", req: "Request") -> "Response":
    user, err = _user_arg(req.query.get("user"))
    return _page(app, req, user, err)


def _finish(app: "App", req: "Request", ch: allowlist.Change, text: str, user: str | None) -> "Response":
    """Одна строка результата: что сделано и чьи QR / файлы теперь новые (ссылки на их страницы)."""
    app.invalidate_links()  # клиентские файлы пересобраны: старые ссылки и QR не годятся
    if ch.affected:
        req.session.flash("ok", f"{text}. Новые QR/файлы нужны:", [(n, f"/users/{n}") for n in ch.affected])
    else:
        req.session.flash("ok", text)
    awg = str(ch.applied.get("amneziawg", ""))
    if awg.startswith("ошибка"):
        req.session.flash("bad", f"AmneziaWG: {awg}. Повторить: sudo zoo allow apply")
    return _back(user)


def _save(app: "App", req: "Request", user: str | None) -> "Response":
    """Весь список обеих платформ одним запросом: одна запись, одна пересборка файлов."""
    lists = {p: list(req.multi.get(p, [])) for p in allowlist.PLATFORMS}
    titles = {k[6:]: v for k, v in req.form.items() if k.startswith("title:")}
    pending = {k: (req.form.get(k) or "").strip() for k in PENDING}
    errors: list[str] = []
    had_ids = any(pending[f"custom_{p}"] for p in allowlist.PLATFORMS)
    for p in allowlist.PLATFORMS:
        v = pending[f"custom_{p}"]
        if not v:
            continue
        if not allowlist.valid(p, v):
            errors.append(f"«{v[:60]}»: не пакет Android (com.example.app)" if p == "android"
                          else f"«{v[:60]}»: не процесс Windows (name.exe)")
            continue
        lists[p].append(v)
        if pending["custom_title"]:
            titles[v] = pending["custom_title"]
        pending[f"custom_{p}"] = ""
    if pending["custom_title"] and not had_ids:
        errors.append("Для своего приложения нужен пакет Android или процесс Windows")
    elif had_ids and not any(pending[f"custom_{p}"] for p in allowlist.PLATFORMS):
        pending["custom_title"] = ""  # переехало в черновик
    if not errors:
        try:
            ch = allowlist.set_lists(lists, user, titles)
        except (allowlist.AllowlistError, users.UserError, LockTimeout) as e:
            errors.append(str(e))
        except protolib.ProtoError as e:
            req.session.flash("bad", f"{e} {e.short()}")
            return _back(user)
    if errors:
        draft = {p: [v for v in lists[p] if allowlist.valid(p, v)] for p in allowlist.PLATFORMS}
        return _page(app, req, user, draft=draft, titles=titles, pending=pending, errors=errors, status=422)
    return _finish(app, req, ch, "Сохранено" if ch.added or ch.removed else "Без изменений", user)


def apps_post(app: "App", req: "Request") -> "Response":
    action = req.form.get("action", "")
    user, err = _user_arg(req.form.get("user"))
    if err:
        req.session.flash("bad", err)
        return _back(None)
    if action == "save":
        return _save(app, req, user)
    if action != "reset":
        req.session.flash("bad", "Неизвестное действие")
        return _back(user)
    try:
        ch = allowlist.reset(user)
    except (allowlist.AllowlistError, users.UserError, LockTimeout) as e:
        req.session.flash("bad", str(e))
        return _back(user)
    except protolib.ProtoError as e:
        req.session.flash("bad", f"{e} {e.short()}")
        return _back(user)
    return _finish(app, req, ch, f"{user or 'Общий список'}: {ch.message}", user)
