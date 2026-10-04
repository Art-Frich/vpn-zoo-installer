"""Страницы пользователей: список, добавление, вкл/выкл, удаление, ссылки и QR, трафик."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import allowlist, manifests, paths, protolib, qr, traffic, users
from ..fsutil import LockTimeout
from ..output import human_bytes
from . import charts
from .html import Markup, badge, card, csrf_input, join, kv, post_button, t, table
from .views import ago, alert_list, chart_block, get_period, no_history_hint, page_head, period_selector

if TYPE_CHECKING:
    from .app import App, Request, Response

ACTION_TEXT = {"add": "добавлен", "adopt": "учтён (уже был)", "del": "удалён", "enable": "включён",
               "disable": "отключён", "rollback": "откат", "forget": "забыт"}


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


# ---------- список ----------

def users_list(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    reg = users.list_users()
    managed, skipped = users.managed_protocols()
    day = {r["key"]: r["total"] for r in traffic.report(period="24h", by="user")["rows"]}
    month = {r["key"]: r["total"] for r in traffic.report(period="30d", by="user")["rows"]}
    seen = traffic.last_seen()
    mx = max(day.values(), default=0)
    rows = []
    shown = reg.visible()
    for u in shown:
        toggle = post_button(f"/users/{u.name}/{'disable' if u.enabled else 'enable'}",
                             "Отключить" if u.enabled else "Включить", csrf, "btn small",
                             {"back": "/users"})
        rows.append([
            t("span", t("a", t("strong", u.name), href=f"/users/{u.name}"),
              t("span", u.note, class_="sub") if u.note else None),
            badge("включён", "ok") if u.enabled else badge("отключён", "muted"),
            t("div", [t("span", p, class_="chip") for p in u.protocols] or "—", class_="chips"),
            human_bytes(day.get(u.name, 0)), charts.bar(day.get(u.name, 0), mx),
            human_bytes(month.get(u.name, 0)), t("span", ago(seen.get(u.name)), class_="nowrap"),
            t("div", toggle, t("a", "Открыть", href=f"/users/{u.name}", class_="btn small"), class_="actions"),
        ])
    tbl = table(["пользователь", "статус", "протоколы", "24 ч", "", "30 дней", "активность", ""], rows,
                num=[3, 5], empty="пользователей нет")
    if not reg.exists:
        tbl = join(alert_list([("warn", "Реестра users.json ещё нет: он создастся при первом изменении "
                                         "(или фазой 09).")]), tbl)
    protos = [t("label", t("input", type="checkbox", name="proto", value=p, checked=True), p) for p in managed]
    add_form = t("form", csrf_input(csrf),
                 t("div",
                   t("div", t("label", "Имя", for_="name"),
                     t("input", type="text", name="name", id="name", required=True, maxlength="32",
                       pattern="[a-z0-9][a-z0-9_\\-]{0,31}", placeholder="masha", autocomplete="off",
                       autocapitalize="none", spellcheck="false"), class_="field"),
                   t("div", t("label", "Заметка", for_="note"),
                     t("input", type="text", name="note", id="note", maxlength="200", placeholder="кто это"),
                     class_="field grow"),
                   t("button", "Добавить", type="submit", class_="btn primary"), class_="form-row"),
                 t("div", t("span", "Протоколы:", class_="label"), t("div", protos, class_="checks"), class_="field")
                 if protos else alert_list([("warn", "Нет протоколов, куда можно добавить пользователя.")]),
                 t("p", "Латиница в нижнем регистре, цифры, «-» и «_». Пользователь получает креды во всех "
                        "отмеченных протоколах; при ошибке в одном изменения откатываются.", class_="hint"),
                 method="post", action="/users", class_="stack")
    parts: list[Any] = [page_head("Пользователи", f"всего {len(shown)}, включено {sum(u.enabled for u in shown)}",
                                  t("div", t("a", "Сверить с протоколами", href="/users?verify=1", class_="btn small"),
                                    post_button("/users/sync", "Синхронизировать", csrf, "btn small",
                                                title="Завести креды в протоколах, включённых после создания"),
                                    class_="actions")),
                        card("Новый пользователь", add_form)]
    if req.query.get("verify"):
        parts.append(verify_card())
    parts.append(card("Список", tbl))
    if skipped:
        parts.append(t("p", "Не участвуют: " + "; ".join(f"{k} — {v}" for k, v in skipped.items()), class_="hint"))
    system_users = [u for u in reg.users if u.system]
    if system_users:
        parts.append(t("p", "Служебный пользователь пробника скрыт из списка и отчётов трафика: ",
                       join(*[t("a", u.name, href=f"/users/{u.name}") for u in system_users]),
                       ". Его креды использует самопроверка; удалить можно только из консоли.", class_="hint"))
    return app.render(req, "Пользователи", parts, active="/users")


def verify_card() -> Markup:
    items: list[tuple[str, Any]] = []
    for pid, d in users.verify().items():
        if d["error"]:
            items.append(("warn", f"{pid}: не удалось сверить — {d['error']}"))
        for n in d["missing"]:
            items.append(("bad", f"{pid}: {n} есть в реестре, но нет в протоколе — «Синхронизировать»"))
        for n in d["extra"]:
            items.append(("warn", f"{pid}: {n} есть в протоколе, но нет в реестре"))
    return card("Сверка с протоколами", alert_list(items or [("ok", "Расхождений нет")]))


def user_add(app: "App", req: "Request") -> "Response":
    name = req.form.get("name", "").strip()
    note = req.form.get("note", "").strip()[:200]
    chosen = req.multi.get("proto", [])
    managed, _ = users.managed_protocols()
    only = None if not chosen or set(managed) <= set(chosen) else [p for p in chosen if p in managed]
    if chosen and only == []:
        req.session.flash("bad", "Не выбран ни один протокол")
        return _redirect("/users")
    rep = _user_op(req, users.add_user, name, note=note, only=only)
    app.invalidate("status")
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
    return _redirect("/users")


def user_toggle(app: "App", req: "Request", name: str, action: str) -> "Response":
    _user_op(req, users.set_enabled, name, action == "enable")
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
                           method="post", action=f"/users/{name}/delete", class_="inline"),
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
    return _redirect("/users" if rep and rep.ok else f"/users/{name}")


# ---------- страница пользователя ----------

_SVG_TAGS = {"svg", "g", "rect", "path"}


def clean_qr_svg(svg: str) -> Markup | None:
    """SVG от qrencode → встраиваемая разметка под CSP без 'unsafe-inline':
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


def qr_markup(payload: str | None) -> Markup:
    if not payload:
        return t("p", "QR для этого файла не строится", class_="muted small")
    try:
        svg = clean_qr_svg(qr.svg(payload))
    except qr.QrError as e:
        return t("p", f"QR: {e}", class_="muted small")
    return t("div", svg, class_="qr") if svg else t("p", "QR: неожиданный формат qrencode", class_="muted small")


# выдаются только клиентские конфиги (AWG .conf) и правила v2rayN; прочее в clients/<name>/ —
# секреты модулей
CLIENT_FILE_SUFFIXES = {".conf"}
CLIENT_FILE_NAMES = {allowlist.V2RAYN_FILE}


def _file_ok(path: str, name: str) -> Path | None:
    """Файл для выдачи — только clients/<name>/*.conf и правила v2rayN самого пользователя.
    config.env, ключи и чужие каталоги не отдаются, даже если модуль по ошибке вернёт такой путь."""
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
                         "Отключить" if user.enabled else "Включить", csrf, "btn")
    actions = t("div", toggle, t("a", "Удалить…", href=f"/users/{name}/delete", class_="btn danger"),
                class_="actions")
    if user.system:
        actions = badge("служебный: пробник", "muted")
    try:
        own = allowlist.Allowlist.load().own(name)
    except allowlist.AllowlistError:
        own = False
    info = card("Профиль", kv([
        ("статус", badge("включён", "ok") if user.enabled else badge("отключён — креды сохранены, доступ закрыт", "muted")),
        ("через VPN", t("a", "свой список приложений" if own else "общий список приложений",
                        href=f"/apps?user={name}")),
        ("заметка", user.note or "—"),
        ("создан", user.created.replace("T", " ").rstrip("Z") or "—"),
        ("протоколы", t("div", [t("span", p, class_="chip") for p in user.protocols] or "—", class_="chips")),
        ("активность", ago(seen)),
    ]))

    rep = traffic.report(user=name, period=period, by="protocol")
    if rep.get("empty"):
        tr_body: Any = no_history_hint()
    else:
        ts = traffic.timeseries(period, "protocol", user=name)
        tr_rows = [[r["title"], human_bytes(r["up"]), human_bytes(r["down"]), human_bytes(r["total"])]
                   for r in rep["rows"]]
        tot = rep["total"]
        tr_body = [chart_block(ts, f"Трафик {name} по протоколам"),
                   table(["протокол", "↑ от клиента", "↓ к клиенту", "всего"], tr_rows, num=[1, 2, 3],
                         empty="трафика не было"),
                   t("p", f"Итого {traffic.PERIOD_TITLES[period]}: {human_bytes(tot['total'])}", class_="hint")]
    tr_card = card("Трафик", tr_body, extra=period_selector(f"/users/{name}", period))

    links, errors = users.user_links(name)
    by_id = {m.id: m for m in manifests.load_all()[0]}
    blocks = []
    for i, link in enumerate(links):
        m = by_id.get(link.proto_id)
        v2rayn = link.proto_id == allowlist.V2RAYN_PROTO
        base = m.name if m else ("Приложения через VPN" if v2rayn else link.proto_id)
        title = base + (f" · {link.label}" if link.label else "")
        uri_id = f"uri-{i}"
        if link.kind == "file":
            fname = Path(link.uri).name
            main = t("div", t("span", fname, class_="mono small"),
                     t("a", "Скачать", href=f"/users/{name}/file/{i}", class_="btn small primary"),
                     class_="link-uri")
            qr_label = ("QR-код для AmneziaWG / WG Tunnel на Android" if fname.endswith("-android.conf")
                        else "QR-код файла (импорт в AmneziaWG, AmneziaVPN)")
        else:
            main = t("div", t("input", type="text", id=uri_id, value=link.uri, readonly=True, data_select=True,
                              aria_label=f"Ссылка {title}"),
                     t("button", "Копировать", type="button", class_="btn small", data_copy=uri_id),
                     class_="link-uri")
            qr_label = "QR-код"
        extra = (t("p", "v2rayN → «Настройки» → «Настройки маршрутизации» → «Добавить набор правил» → "
                        "«Импорт правил из файла». Подробно — docs/USER-GUIDE.md, раздел про Windows.",
                   class_="hint") if v2rayn else
                 t("details", t("summary", qr_label), qr_markup(_payload(link, name))))
        blocks.append(t("div",
                        t("div", t("h3", title), t("span", link.proto_id, class_="chip"), class_="link-head"),
                        t("div", m.notes, class_="notes") if m and m.notes and _first_of(links, i) else None,
                        main, extra,
                        class_="link"))
    err_list = alert_list([("warn", f"{pid}: ссылки не получены — {e}") for pid, e in errors.items()]) if errors else None
    links_card = card("Ссылки и QR", err_list,
                      t("p", "Ссылки — это ключи доступа: отправляйте их только самому пользователю.", class_="hint"),
                      blocks or t("p", "Ссылок нет.", class_="muted"))
    body = [page_head(name, user.note or None, actions), t("div", info, tr_card, class_="cols"), links_card]
    if not user.enabled:
        body.insert(1, alert_list([("warn", "Пользователь отключён: ссылки сохранены, но не подключаются.")]))
    return app.render(req, name, body, active="/users")


def _first_of(links: list[protolib.Link], i: int) -> bool:
    """Заметки протокола — один раз, у первой его ссылки."""
    return all(links[j].proto_id != links[i].proto_id for j in range(i))


def user_file(app: "App", req: "Request", name: str, idx: str) -> "Response":
    from .app import Response
    if users.list_users().get(name) is None:
        return app.error(req, 404, "Нет пользователя", f"Пользователя «{name}» нет.")
    links, _ = users.user_links(name)
    i = int(idx)
    if i >= len(links) or links[i].kind != "file":
        return app.error(req, 404, "Нет файла", "Такого файла у пользователя нет.")
    f = _file_ok(links[i].uri, name)
    if f is None:
        return app.error(req, 404, "Нет файла", "Файл не найден или лежит вне каталога клиента.")
    data = f.read_bytes()
    fname = f"{name}-{f.name}"
    return Response(200, data, "application/octet-stream",
                    headers=[("Content-Disposition", f'attachment; filename="{fname}"')])

