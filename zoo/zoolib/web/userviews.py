"""Страницы пользователей: список, добавление, вкл/выкл, удаление, ссылки и QR, трафик."""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import allowlist, clients, groups, manifests, paths, protolib, qr, traffic, users
from ..fsutil import LockTimeout
from ..output import human_bytes
from ..probe import rank
from . import charts, clientviews
from . import table as tbl
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


def _group_cell(gs: groups.Groups, u: users.User) -> Markup | str:
    g = gs.get(u.group)
    if g is None:
        return t("span", "—", class_="muted")
    return t("a", g.name, href=f"/groups/{g.id}", class_="nowrap")


def _group_link(user: users.User) -> Markup | str:
    try:
        g = groups.Groups.load().get(user.group)
    except groups.GroupError:
        g = None
    if g is None:
        return "—"
    return t("span", t("a", g.name, href=f"/groups/{g.id}"),
             t("span", " · свой набор протоколов", class_="muted small") if user.custom else None)


def _group_of(user: users.User) -> groups.Group | None:
    try:
        return groups.Groups.load().get(user.group)
    except groups.GroupError:
        return None


def _disable_confirm(name: str) -> str:
    return f"Отключить {name}? Ссылки сохранятся, но подключиться он не сможет."


def _created_local(created: str) -> str:
    """Дата создания из реестра (UTC, ISO) — в местном времени."""
    try:
        return fmt_time(datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return created or "—"


# ---------- список ----------

def _expected_protocols(u: users.User, managed: list[str], gs: groups.Groups) -> list[str]:
    """Что у пользователя должно быть: протоколы его группы среди включённых; «*» или нет группы — все
    включённые; свой набор — тоже все включённые (его отличие от них и есть то, что стоит показать)."""
    g = None if u.custom else gs.get(u.group)
    return list(managed) if g is None or g.all_protocols else g.resolve(managed)


def _proto_chip(u: users.User, managed: list[str], gs: groups.Groups) -> Markup:
    want = _expected_protocols(u, managed, gs)
    have = [p for p in want if p in u.protocols]
    missing = [p for p in want if p not in u.protocols]
    if not missing:
        return t("span", f"{len(have)}/{len(want)}", class_="chip", title=", ".join(have) or None)
    shown = ", ".join(missing[:2]) + ("…" if len(missing) > 2 else "")
    return t("span", f"нет: {shown}", class_="chip warn",
             title=f"{len(have)}/{len(want)}; нет в: {', '.join(missing)}"
                   + ("; свой набор — сверка со всеми включёнными" if u.custom else ""))


def _users_spec(csrf: str, managed: list[str], mx: int, gs: groups.Groups) -> tbl.Spec:
    def name_cell(r: dict[str, Any]) -> Markup:
        u = r["user"]
        return t("span", t("a", t("strong", u.name), href=f"/users/{u.name}"), " " if not u.enabled else None,
                 badge("откл.", "muted") if not u.enabled else None,
                 t("span", u.note, class_="sub") if u.note else None)

    def toggle(r: dict[str, Any]) -> Markup:
        u = r["user"]
        return post_button(f"/users/{u.name}/{'disable' if u.enabled else 'enable'}",
                           "Отключить" if u.enabled else "Включить", csrf, "btn small", {"back": "/users"},
                           confirm=_disable_confirm(u.name) if u.enabled else None)

    cols = [
        tbl.Col("name", "пользователь", cell=name_cell, value=lambda r: r["name"], find=lambda r: r["user"].note,
                sort=True, search=True),
        tbl.Col("access", "доступ", sort=True, chip=True, hidden=True),
        tbl.Col("group", "группа", cell=lambda r: _group_cell(gs, r["user"]), sort=True, chip=True),
        tbl.Col("protos", "протоколы", cell=lambda r: _proto_chip(r["user"], managed, gs),
                value=lambda r: ", ".join(r["user"].protocols), secondary=True),
        tbl.Col("day", "24 ч", cell=lambda r: human_bytes(r["day"]), value=lambda r: r["day"], num=True, sort=True,
                first_desc=True),
        tbl.Col("bar", "", cell=lambda r: charts.bar(r["day"], mx), secondary=True, export=False),
        tbl.Col("month", "30 дней", cell=lambda r: human_bytes(r["month"]), value=lambda r: r["month"], num=True,
                sort=True, secondary=True),
        tbl.Col("seen", "активность", cell=lambda r: t("span", ago(r["seen"]), class_="nowrap"),
                value=lambda r: fmt_time(r["seen"]) if r["seen"] else "", num=True, left=True, sort=True,
                first_desc=True, secondary=True),
        tbl.Col("act", "", cell=toggle, export=False),
    ]
    return tbl.Spec(path="/users", cols=cols, sort="name", id_key="name", paged=False, empty="пользователей нет",
                    placeholder="имя или заметка", href=lambda r: f"/users/{r['name']}",
                    row_cls=lambda r: None if r["user"].enabled else "off", name="users",
                    select="names", select_form="bulk")


def _user_rows(shown: list[users.User], gs: groups.Groups) -> list[dict[str, Any]]:
    day = {r["key"]: r["total"] for r in traffic.report(period="24h", by="user")["rows"]}
    month = {r["key"]: r["total"] for r in traffic.report(period="30d", by="user")["rows"]}
    seen = traffic.last_seen()
    return [{"name": u.name, "user": u, "access": "включён" if u.enabled else "отключён", "day": day.get(u.name, 0),
             "month": month.get(u.name, 0), "seen": seen.get(u.name),
             "group": g.name if (g := gs.get(u.group)) else ""} for u in shown]


def users_list(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    reg = users.list_users()
    try:
        gs = groups.ensure()
    except (users.UserError, LockTimeout, OSError):
        gs = groups.Groups(paths.groups_file())
    managed, skipped = users.managed_protocols()
    shown = reg.visible()
    rows = _user_rows(shown, gs)
    spec = _users_spec(csrf, managed, max((r["day"] for r in rows), default=0), gs)
    ex = tbl.memory_export(spec, req.query, rows)
    if ex is not None:
        return ex
    opts = tbl.options_from_rows(spec, rows)
    st = tbl.parse(spec, req.query, opts)
    tbl_html = tbl.render(spec, st, tbl.memory_page(spec, st, rows, opts))
    if not reg.exists:
        tbl_html = join(alert_list([("warn", "Реестра users.json ещё нет: он создастся при первом изменении "
                                             "(или фазой 09).")]), tbl_html)
    first = gs.get(groups.MAIN_ID) or (gs.groups[0] if gs.groups else None)
    preset = set(first.resolve(managed)) if first else set(managed)
    protos = [t("label", t("input", type="checkbox", name="proto", value=p, checked=p in preset), p) for p in managed]
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
                   t("div", t("label", "Группа", for_="group"),
                     t("select", [t("option", g.name, value=g.id, selected=g is first,
                                    data_protos=" ".join(g.resolve(managed))) for g in gs.groups],
                       name="group", id="group", data_group=True,
                       title="Протоколы и приложения — как у группы"), class_="field")
                   if gs.groups else None,
                   t("button", "Добавить", type="submit", class_="btn primary"), class_="form-row"),
                 t("div", t("span", "Протоколы:", class_="label"), t("div", protos, class_="checks"),
                   t("div", "Отмечены протоколы группы; другой набор станет «своим», и группа его не тронет.",
                     class_="hint"), class_="field")
                 if protos else alert_list([("warn", "Нет протоколов, куда можно добавить пользователя.")]),
                 method="post", action="/users", class_="stack", data_swap=True)
    verify, missing = (verify_card() if req.query.get("verify") else (None, False))
    head_actions = t("div",
                     t("a", "Проверить учётки", href="/users?verify=1", class_="btn small", data_swap=True,
                              title="Есть ли у каждого пользователя учётка во всех включённых протоколах. Если чего-то не хватает, появится кнопка, которая заведёт недостающее"),
                     post_button("/users/sync", "Синхронизировать", csrf, "btn small",
                                 title="Завести креды в протоколах, включённых после создания") if missing else None,
                     class_="actions")
    parts: list[Any] = [page_head("Пользователи", f"{sum(u.enabled for u in shown)}/{len(shown)} включено", head_actions)]
    if verify:
        parts.append(verify)
    parts.append(card("Список", tbl_html, bulk_bar(csrf, gs) if rows else None))
    parts.append(t("details", t("summary", "＋ Добавить пользователя"), add_form, class_="card more"))
    notes = [t("span", f"не участвует: {k}", class_="chip", title=v) for k, v in skipped.items()]
    notes += [t("a", f"служебный: {u.name}", href=f"/users/{u.name}", class_="chip",
                title="Служебный пользователь пробника: скрыт из списков и отчётов трафика, его креды "
                      "использует самопроверка; удалить можно только из консоли.")
              for u in reg.users if u.system]
    if notes:
        parts.append(t("div", notes, class_="chips"))
    return app.render(req, "Пользователи", parts, active="/users")


def bulk_bar(csrf: str, gs: groups.Groups) -> Markup:
    """Панель действий над отмеченными строками: без JS — обычная форма (галочки привязаны атрибутом form=),
    с JS — прячется, пока ничего не отмечено, и липнет к низу экрана."""
    move = t("details", t("summary", "В группу ▾", class_="btn small"),
             t("div", [t("button", g.name, type="submit", name="action", value=f"move:{g.id}", class_="btn small")
                       for g in gs.groups], class_="menu"), class_="bulk-move") if gs.groups else None
    return t("form", csrf_input(csrf),
             t("button", "Удалить", t("span", data_bulk_n=True), type="submit", name="action", value="delete",
               class_="btn small danger", title="Спросим подтверждение; owner и служебные не удаляются"),
             t("button", "Отключить", type="submit", name="action", value="disable", class_="btn small",
               title="Ссылки сохранятся, подключиться они не смогут; owner не отключается"),
             t("button", "Включить", type="submit", name="action", value="enable", class_="btn small"),
             move, method="post", action="/users/bulk", class_="bulkbar", id="bulk", data_swap=True, data_bulk_bar=True,
             aria_label="Действия с отмеченными")


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
    group = req.form.get("group") or None
    # only — только если владелец отметил не то, что у группы: иначе пользователь стал бы «своим»
    # без JS форма не перерисовывает галочки при смене группы: они остаются от группы по умолчанию, и этот
    # набор при другой выбранной группе — «не менялось», а не «свой»
    try:
        gs = groups.Groups.load()
        grp = gs.get(group or groups.MAIN_ID)
        first = gs.get(groups.MAIN_ID) or (gs.groups[0] if gs.groups else None)
    except groups.GroupError:
        grp = first = None
    base = set(grp.resolve(managed) if grp else managed)
    picked = [p for p in chosen if p in managed]
    untouched = grp is not None and first is not None and grp.id != first.id and set(picked) == set(first.resolve(managed))
    only = None if not chosen or set(picked) == base or untouched else picked
    if chosen and not picked:
        req.session.flash("bad", "Не выбран ни один протокол")
        return _redirect("/users")
    rep = _user_op(req, users.add_user, name, note=note, only=only, group=group)
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


BULK_MAX = 300
BULK_DONE = {"delete": "Удалено", "disable": "Отключено", "enable": "Включено"}


def _bulk_confirm(req: "Request", app: "App", names: list[str], skipped: list[str]) -> "Response":
    csrf = req.session.csrf if req.session else ""
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="action", value="delete"),
             t("input", type="hidden", name="confirm", value="1"),
             [t("input", type="hidden", name="names", value=n) for n in names],
             t("button", "Удалить навсегда", type="submit", class_="btn danger-solid"),
             method="post", action="/users/bulk", class_="inline", data_swap=True)
    body = card(f"Удалить пользователей: {len(names)}?",
                t("p", "Их креды будут удалены из всех протоколов, ссылки и QR перестанут работать. Отменить нельзя — "
                       "только создать заново с новыми ключами."),
                t("div", [t("span", n, class_="chip") for n in names], class_="chips"),
                t("p", "Пропущены: " + "; ".join(skipped), class_="hint") if skipped else None,
                t("div", form, t("a", "Отмена", href="/users", class_="btn", data_swap=True), class_="actions"),
                cls="danger-zone")
    return app.render(req, "Удаление", [page_head("Удаление пользователей"), body], active="/users")


def users_bulk(app: "App", req: "Request") -> "Response":
    """Одно действие над отмеченными: delete (через страницу подтверждения), disable, enable, move:<группа>.
    Сервер ничему из формы не верит: имена проверяются по реестру, owner и служебные для удаления и
    отключения отклоняются, остальные идут дальше; итог — одно сообщение, отказы — отдельными."""
    s = req.session
    action = req.form.get("action", "")[:60]
    op, _, gid = action.partition(":")
    if op not in (*users.BULK_OPS, "move") or (op == "move") != bool(gid):
        s.flash("bad", "Неизвестное действие")
        return _redirect("/users")
    names = list(dict.fromkeys(req.multi.get("names", [])[:BULK_MAX]))
    if not names:
        s.flash("warn", "Никого не выбрано")
        return _redirect("/users")
    reg = users.list_users()
    todo: list[str] = []
    errors: list[str] = []
    for n in names:
        why = "некорректное имя" if not users.NAME_RE.match(n) else users.bulk_refusal(reg, n, op)
        if why:
            errors.append(f"{n[:32]}: {why}")
        else:
            todo.append(n)
    if op == "delete" and todo and not req.form.get("confirm"):
        return _bulk_confirm(req, app, todo, errors)
    done: list[str] = []
    verb = BULK_DONE.get(op, "")
    try:
        if todo and op == "move":
            rep = groups.move_many(todo, gid)
            done, errors, verb = rep.moved, errors + rep.errors, f"В группе «{rep.group.name}»"
        elif todo:
            for r in users.bulk(op, todo):
                if r.ok:
                    done.append(r.user)
                else:
                    errors.append(f"{r.user}: {r.message}" + "".join(f" ({st.proto_id}: {st.error})" for st in r.failed))
    except (users.UserError, LockTimeout) as e:
        errors.append(str(e))
    except protolib.ProtoError as e:
        errors.append(f"{e} {e.short()}")
    app.invalidate("status")
    app.invalidate_links()
    if done:
        s.flash("ok" if len(done) == len(names) else "warn", f"{verb}: {len(done)} из {len(names)} — {', '.join(done)}")
    for e in errors:
        s.flash("bad", e)
    if not done and not errors:
        s.flash("warn", "Ничего не изменилось")
    return _redirect("/users")


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
FILE_NAME_RE = clientviews.FILE_NAME_RE


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
        al = allowlist.Allowlist.load()
        apps = "свой список приложений" if al.own(name) else (
            "список приложений группы" if al.from_group(name) else "общий список приложений")
    except allowlist.AllowlistError:
        apps = "общий список приложений"
    info = card("Профиль", kv([
        ("статус", badge("включён", "ok") if user.enabled else
         t("span", "отключён", class_="badge muted", title="креды сохранены, доступ закрыт")),
        ("группа", _group_link(user)),
        ("через VPN", t("a", apps, href=f"/apps?user={name}")),
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
    connect = clientviews.connect_card(links, name, clientviews.Ctx.load(), _group_of(user))
    tiles = connect_tiles(links, manifests.load_all()[0], name)
    # плитки — «всё как есть» для тех, кому нужен конкретный вариант; без нового блока они остаются главными
    advanced = t("details", t("summary", "Все ссылки и QR"), quick_start(links, name), tiles,
                 t("p", "Ссылки и QR — ключи доступа: показывайте только самому пользователю.", class_="hint"),
                 class_="card more", open=connect is None or None) if tiles else None
    body = [page_head(name, user.note or None, actions), err_list, connect, advanced,
            None if connect or advanced else card("Подключить", t("p", "Ссылок нет.", class_="muted")),
            t("div", info, tr_card, class_="cols")]
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


def qr_url(name: str, idx: int, link: protolib.Link) -> str:
    """Адрес QR ссылки: номер в списке и её метка (сдвиг списка → 404, а не чужой QR)."""
    return f"/users/{name}/qr/{idx}?p={link.tag}"


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
        qr_block = t("img", class_="qr", data_src=qr_url(name, idx, link), width=240, height=240, alt="QR")
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
             t("img", class_="qr", src=qr_url(name, idx, link), width=160, height=160, alt="QR",
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
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None  # без каталога плитки без подсказки о клиентах
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
            title=f"Клиенты: {cat.names_for(pid)}" if cat and cat.names_for(pid) else None))
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
    if i >= len(links) or links[i].proto_id == allowlist.V2RAYN_PROTO or links[i].tag != req.query.get("p", ""):
        return text("нет такого варианта", 404)   # метка не сошлась: список ссылок изменился, страницу нужно обновить
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
