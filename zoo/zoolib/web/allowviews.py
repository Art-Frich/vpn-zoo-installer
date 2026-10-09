"""Страница «Через VPN»: одна таблица по приложениям, черновик и одно сохранение. Вкладки — общий список и группы
(свой список группы — в groups.json, его сохраняет groups.update); свой список человека — по ссылке с его страницы
(?user=). Под списком — как он действует на устройствах группы и что переслать после изменения (resend)."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import allowlist, clients, groups, protolib, resend, users
from ..fsutil import LockTimeout
from .html import Markup, badge, card, csrf_input, post_button, t, table
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

SHORT = {"android": "Android", "windows": "Windows"}
PLACEHOLDER = {"android": "com.example.app", "windows": "program.exe"}
HINT = {"android": "Пакет Android — как в адресе Google Play: play.google.com/store/apps/details?id=ПАКЕТ.",
        "windows": "Имя процесса как в Диспетчере задач (вкладка «Подробности»), с учётом регистра."}
HELP = t("p", "Включённое идёт через VPN, остальное — напрямую. В каждом списке должно остаться хотя бы одно приложение.")
PENDING = ("custom_title", "custom_android", "custom_windows")  # поля «Своё приложение»
CATCH = (allowlist.AllowlistError, users.UserError, LockTimeout)


@dataclass
class _Row:
    title: str
    ids: dict[str, str | None] = field(default_factory=dict)
    custom: bool = False


@dataclass
class Scope:
    """Чей список: общий (никто), группа или человек."""
    user: str | None = None
    group: groups.Group | None = None

    def query(self) -> dict[str, str]:
        return {"user": self.user} if self.user else ({"group": self.group.id} if self.group else {})

    @property
    def url(self) -> str:
        q = self.query()
        return "/apps" + (f"?{urllib.parse.urlencode(q)}" if q else "")


def _back(scope: Scope) -> "Response":
    from .app import redirect
    return redirect(scope.url)


def _scope(user: str | None, group: str | None) -> tuple[Scope, str]:
    """(чей список, ошибка): только пользователь из реестра и группа из groups.json."""
    name, gid = (user or "").strip(), (group or "").strip()[:40]
    if name:
        u = users.list_users().get(name)
        if u is None:
            return Scope(), f"Пользователя «{name}» нет в реестре"
        if u.system:
            return Scope(), f"{name} — служебный пользователь, своего списка у него нет"
        return Scope(user=name), ""
    if gid:
        g = groups.Groups.load().get(gid)
        return (Scope(group=g), "") if g else (Scope(), f"Группы «{gid}» нет")
    return Scope(), ""


def _current(al: allowlist.Allowlist, scope: Scope) -> dict[str, list[str]]:
    if scope.group is not None:
        return {p: list((scope.group.allowlist or {}).get(p) or al.common(p)) for p in allowlist.PLATFORMS}
    return {p: al.effective(p, scope.user) for p in allowlist.PLATFORMS}


def _rows(al: allowlist.Allowlist, current: dict[str, list[str]], draft: dict[str, list[str]] | None,
          titles: dict[str, str]) -> list[_Row]:
    """Весь каталог, затем свои приложения из списка (и из черновика), не из каталога."""
    rows = [_Row(a.title, {p: getattr(a, p) for p in allowlist.PLATFORMS}) for a in allowlist.CATALOG]
    known = {(p, i.lower()) for r in rows for p, i in r.ids.items() if i}
    extra: dict[str, _Row] = {}
    for p in allowlist.PLATFORMS:
        for ident in [*current[p], *(draft or {}).get(p, [])]:
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
    def field_(key: str, label: str, hint: str | None = None) -> Markup:
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


def _list_form(al: allowlist.Allowlist, scope: Scope, csrf: str, draft: dict[str, list[str]] | None,
               titles: dict[str, str], pending: dict[str, str]) -> Markup:
    current = _current(al, scope)
    was = {p: {i.lower() for i in current[p]} for p in allowlist.PLATFORMS}
    on = {p: {i.lower() for i in draft[p]} for p in allowlist.PLATFORMS} if draft else was
    rows = [_row_cells(r, was, on) for r in _rows(al, current, draft, titles)]
    head = (f"Список {scope.user}" if scope.user else
            f"Список группы «{scope.group.name}»" if scope.group else "Общий список")
    box = card(head, table(["приложение", "Android", "Windows"], rows, num=[1, 2], cls="apps"),
               _custom_box(pending), help=HELP)
    bar = t("div", t("span", "", class_="count", data_count=True, aria_live="polite"),
            t("button", "Сохранить", type="submit", class_="btn primary", data_save=True),
            t("a", "Отменить", href=scope.url, class_="btn", data_cancel=True), class_="savebar")
    return t("form", csrf_input(csrf), t("input", type="hidden", name="action", value="save"),
             [t("input", type="hidden", name=k, value=v) for k, v in scope.query().items()], box, bar,
             method="post", action="/apps", class_="stack", data_draft=True, data_swap=True,
             data_re_android=allowlist.ANDROID_RE.pattern, data_re_windows=allowlist.WINDOWS_RE.pattern)


def _who_nav(gs: groups.Groups, scope: Scope, label: str = "") -> Markup:
    """Чей список — сегментами-ссылками: «Общий» и по одному на группу; свой список человека — его вкладкой."""
    links = [t("a", "Общий", href="/apps", class_="active" if not scope.user and not scope.group else None,
               title="для всех, у кого нет списка группы")]
    for g in gs.groups:
        links.append(t("a", g.name, href=Scope(group=g).url,
                       class_="active" if scope.group is not None and scope.group.id == g.id else None))
    if scope.user:
        links.append(t("a", label or scope.user, href=scope.url, class_="active"))
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


def _items(cat: clients.Catalog, gs: groups.Groups, scope: Scope) -> list[resend.Item]:
    """Приложения, к которым относится список: человека, группы или всех групп на общем списке."""
    if scope.user:
        u = users.list_users().get(scope.user)
        found = resend.apps_of(cat, u, gs.get(u.group)) if u else []
    elif scope.group is not None:
        found = resend.apps_of(cat, None, scope.group)
    else:
        found = [i for g in gs.groups if not g.allowlist for i in resend.apps_of(cat, None, g)]
    seen: set[tuple[str, str]] = set()
    out = []
    for i in sorted(found, key=lambda i: list(cat.platforms).index(i.platform) if i.platform in cat.platforms else 99):
        if (i.platform, i.client["id"]) not in seen:
            seen.add((i.platform, i.client["id"]))
            out.append(i)
    return out


def effects_card(gs: groups.Groups, scope: Scope) -> Markup | None:
    """Как список действует на устройствах (по приложениям группы) и что переслать после изменения."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return None
    items = _items(cat, gs, scope)
    if not items:
        return None
    via = cat.raw.get("via", {})
    lines: dict[str, dict[str, list[str]]] = {}   # как действует → устройство → приложения
    for i in items:
        mode = cat.via(i.client, i.platform)
        how = (f"{resend.EFFECT_LIST[i.effect]}; после изменения — {resend.EFFECT_SEND[i.effect]}"
               if i.effect != resend.NONE else
               resend.EFFECT_LIST[resend.NONE] + (f": через VPN {via[mode]}" if mode in via else ""))
        lines.setdefault(how, {}).setdefault(cat.platforms.get(i.platform, i.platform), []).append(i.client["name"])
    items_html = [t("li", t("strong", "; ".join(f"{dev}: {', '.join(apps)}" for dev, apps in where.items())), " — ", how)
                  for how, where in lines.items()]
    return t("details", t("summary", "Где список действует"), t("ul", items_html, class_="cav-list"),
             t("p", t("a", "Кому переслать →", href="/resend", data_swap=True), class_="small")
             if resend.pending(users.list_users()) else None, class_="card more")


def _scope_bar(al: allowlist.Allowlist, scope: Scope, csrf: str) -> Markup | None:
    """Под вкладками: чей это список и «вернуть» (свой список человека, свой список группы)."""
    if scope.user:
        user = scope.user
        own = al.own(user)
        add, rem = _diff(al, user)
        grp = al.from_group(user)
        reset = post_button("/apps", "Вернуть как у группы" if grp else "Вернуть общий", csrf, "btn small",
                            {"action": "reset", "user": user},
                            title="Удалить свой список: пользователь снова на списке " + ("группы" if grp else "общем"),
                            confirm=f"Вернуть {user} список {'группы' if grp else 'общий'}? Его свой список будет удалён.",
                            swap=True) if own else None
        return t("div", badge(f"свой (отличается: +{add} −{rem})", "info") if own and (add or rem)
                 else badge(baseline_name(al, user), "muted"), reset,
                 t("a", f"Ссылки и QR {user} →", href=f"/users/{user}"), class_="actions")
    if scope.group is not None:
        g = scope.group
        reset = post_button("/apps", "Вернуть общий", csrf, "btn small", {"action": "reset", "group": g.id},
                            confirm=f"Группе «{g.name}» — общий список? Её свой список будет удалён.",
                            swap=True) if g.allowlist else None
        return t("div", badge("свой список группы", "info") if g.allowlist else badge("как общий", "muted"), reset,
                 t("a", f"Группа «{g.name}» →", href=f"/groups/{g.id}", data_swap=True), class_="actions")
    return None


def _page(app: "App", req: "Request", scope: Scope, err: str = "", draft: dict[str, list[str]] | None = None,
          titles: dict[str, str] | None = None, pending: dict[str, str] | None = None,
          errors: list[str] | None = None, status: int = 200) -> "Response":
    csrf = req.session.csrf if req.session else ""
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return app.error(req, 500, "Список «через VPN» не читается", str(e))
    try:
        gs = groups.ensure()
    except (groups.GroupError, LockTimeout):
        gs = groups.Groups.load()
    reg = users.list_users()
    u = reg.get(scope.user) if scope.user else None
    parts: list[Any] = [page_head("Через VPN",
                                  "Через VPN идут только отмеченные приложения, остальное — напрямую "
                                  "(банки, Госуслуги, MAX).", _who_nav(gs, scope, u.label if u else ""))]
    alerts = [("bad", m) for m in ([err] if err else []) + (errors or [])]
    if not al.exists:
        alerts.append(("info", "Общий список ещё не меняли — действует список по умолчанию."))
    if alerts:
        parts.append(alert_list(alerts))
    parts.append(_scope_bar(al, scope, csrf))
    merged = {**al.titles, **{k.lower(): allowlist.clean_title(v) for k, v in (titles or {}).items()}}
    parts.append(_list_form(al, scope, csrf, draft, merged, pending or {}))
    parts.append(effects_card(gs, scope))
    if not scope.user and not scope.group:
        reset = post_button("/apps", "Сбросить к пресету", csrf, "btn small", {"action": "reset"},
                            title="Общий список снова как в пресете", swap=True,
                            confirm="Сбросить общий список к пресету?" + (
                                " Свой список придётся собирать заново." if al.users else ""))
        if al.users:
            own_rows = [[t("a", n, href=Scope(user=n).url, data_swap=True), "+{} −{}".format(*_diff(al, n))]
                        for n in sorted(al.users)]
            parts.append(card("Свои списки пользователей",
                              table(["пользователь", "отличия от списка группы или общего"], own_rows, stack=True),
                              extra=reset))
        else:
            parts.append(t("div", reset, class_="actions"))
    return app.render(req, "Через VPN", parts, active="/apps", status=status)


def apps_page(app: "App", req: "Request") -> "Response":
    scope, err = _scope(req.query.get("user"), req.query.get("group"))
    return _page(app, req, scope, err)


def flash_resend(req: "Request", text: str, marked: list[str], affected: bool) -> None:
    """Итог сохранения и кому что переслать — одной строкой со ссылкой на «Кому переслать»."""
    if marked:
        reg = users.list_users()
        who = [u.label if (u := reg.get(n)) else n for n in marked]
        shown = ", ".join(who[:5]) + (f" и ещё {len(who) - 5}" if len(who) > 5 else "")
        req.session.flash("ok", f"{text}. Переслать: {shown}", [("кому и что", "/resend")])
    elif affected:
        req.session.flash("ok", f"{text}. Пересылать ничего не нужно")
    else:
        req.session.flash("ok", text)


def _unaffected(scope: Scope) -> list[str]:
    """Люди, у которых список не действует ни на одном устройстве (iPhone, AmneziaVPN на Windows): «Имя (iPhone — всё,
    кроме российских сайтов)». Молчать про них — неправда умолчанием: через VPN у них по-прежнему идёт всё."""
    try:
        cat = clients.load()
        gs = groups.Groups.load()
    except (clients.ClientsError, groups.GroupError):
        return []
    reg = users.list_users()
    if scope.user:
        people = [u for u in [reg.get(scope.user)] if u]
    elif scope.group is not None:
        people = groups.members_of(gs, reg, scope.group.id)
    else:
        people = [u for g in gs.groups if not g.allowlist for u in groups.members_of(gs, reg, g.id)]
    out = []
    for u in people:
        if u.name == users.OWNER or not u.enabled:
            continue
        found = resend.apps_of(cat, u, gs.get(u.group))
        if found and all(i.effect == resend.NONE for i in found):
            via = cat.raw.get("via", {})
            how = "; ".join(dict.fromkeys(f"{cat.platforms.get(i.platform, i.platform)} — {via.get(cat.via(i.client, i.platform), 'всё')}"
                                          for i in found))
            out.append(f"{u.label} ({how})")
    return out


def flash_unaffected(req: "Request", scope: Scope) -> None:
    if lost := _unaffected(scope):
        shown = ", ".join(lost[:5]) + (f" и ещё {len(lost) - 5}" if len(lost) > 5 else "")
        req.session.flash("info", f"Список не действует: {shown}.")


def _finish(app: "App", req: "Request", ch: allowlist.Change, text: str, scope: Scope) -> "Response":
    app.invalidate_links()  # клиентские файлы пересобраны: старые ссылки и QR не годятся
    flash_resend(req, text, ch.resend, bool(ch.affected))
    if ch.added or ch.removed:
        flash_unaffected(req, scope)
    awg = str(ch.applied.get("amneziawg", ""))
    if awg.startswith("ошибка"):
        req.session.flash("bad", f"AmneziaWG: {awg}. Повторить: sudo zoo allow apply")
    return _back(scope)


def _group_done(app: "App", req: "Request", rep: groups.GroupReport, changed: bool, scope: Scope) -> "Response":
    app.invalidate("status")
    app.invalidate_links()
    flash_resend(req, "Сохранено" if changed else "Без изменений", rep.resend, changed)
    if changed:
        flash_unaffected(req, scope)
    for e in rep.errors:
        req.session.flash("bad", e)
    return _back(scope)


def _save_group(app: "App", req: "Request", scope: Scope, lists: dict[str, list[str]],
                titles: dict[str, str]) -> tuple["Response | None", list[str]]:
    """Список группы: совпал с общим — группа снова на общем; иначе свой список группы (groups.update)."""
    g = scope.group
    assert g is not None
    try:
        al = allowlist.Allowlist.load()
        new = {p: allowlist.normalize(p, lists[p]) for p in allowlist.PLATFORMS}
        same = all({x.lower() for x in new[p]} == {x.lower() for x in al.common(p)} for p in allowlist.PLATFORMS)
        before = g.allowlist
        rep = groups.update(g.id, allow=None if same else new)
        allowlist.remember_titles(titles)
    except (*CATCH, groups.GroupError) as e:
        return None, [str(e)]
    except protolib.ProtoError as e:
        return None, [f"{e} {e.short()}"]
    return _group_done(app, req, rep, rep.group.allowlist != before, scope), []


def _save(app: "App", req: "Request", scope: Scope) -> "Response":
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
    if not errors and scope.group is not None:
        done, errors = _save_group(app, req, scope, lists, titles)
        if done is not None:
            return done
    elif not errors:
        try:
            ch = allowlist.set_lists(lists, scope.user, titles)
        except CATCH as e:
            errors.append(str(e))
        except protolib.ProtoError as e:
            req.session.flash("bad", f"{e} {e.short()}")
            return _back(scope)
    if errors:
        draft = {p: [v for v in lists[p] if allowlist.valid(p, v)] for p in allowlist.PLATFORMS}
        return _page(app, req, scope, draft=draft, titles=titles, pending=pending, errors=errors, status=422)
    return _finish(app, req, ch, "Сохранено" if ch.added or ch.removed else "Без изменений", scope)


def apps_post(app: "App", req: "Request") -> "Response":
    action = req.form.get("action", "")
    scope, err = _scope(req.form.get("user"), req.form.get("group"))
    if err:
        req.session.flash("bad", err)
        return _back(Scope())
    if action == "save":
        return _save(app, req, scope)
    if action != "reset":
        req.session.flash("bad", "Неизвестное действие")
        return _back(scope)
    if scope.group is not None:
        try:
            rep = groups.update(scope.group.id, allow=None)
        except (*CATCH, groups.GroupError) as e:
            req.session.flash("bad", str(e))
            return _back(scope)
        except protolib.ProtoError as e:
            req.session.flash("bad", f"{e} {e.short()}")
            return _back(scope)
        return _group_done(app, req, rep, scope.group.allowlist is not None, scope)
    try:
        ch = allowlist.reset(scope.user)
    except CATCH as e:
        req.session.flash("bad", str(e))
        return _back(scope)
    except protolib.ProtoError as e:
        req.session.flash("bad", f"{e} {e.short()}")
        return _back(scope)
    return _finish(app, req, ch, f"{scope.user or 'Общий список'}: {ch.message}", scope)
