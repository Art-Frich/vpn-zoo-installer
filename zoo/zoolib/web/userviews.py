"""Страницы пользователей: список, добавление, вкл/выкл, удаление, ссылки и QR, трафик."""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import allowlist, clients, groups, manifests, paths, people, protolib, qr, resend, support, traffic, users
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
    u = users.list_users().get(rep.user)
    who = u.label if u else rep.user   # к человеку — по имени; id протоколов — только в ошибках, где они нужны для починки
    s.flash("ok" if rep.ok else "bad", f"{who}: {rep.message}")
    for st in rep.steps:
        if not st.ok:
            s.flash("bad", f"{manifests.proto_title(st.proto_id)}: {st.error}")
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


def device_choices(cat: clients.Catalog, g: groups.Group | None, own: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Какие устройства можно отметить: устройства группы (без группы — все платформы каталога) и уже свои."""
    base = set(g.app_devices) if g is not None and g.app_devices else set(cat.platforms)
    return [p for p in cat.platforms if p in base or p in own]


def device_boxes(cat: clients.Catalog, choices: list[str], checked: list[str] | None) -> Markup:
    return t("span", [t("label", t("input", type="checkbox", name="dev", value=p,
                                   checked=checked is None or p in checked), " " + cat.platforms[p], class_="chk")
                      for p in choices], class_="dev-boxes")


def _devices_form(user: users.User, g: groups.Group | None, csrf: str) -> Markup | str:
    """Устройства человека: отметить и сохранить; совпали с группой — «как у группы»."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return "—"
    return t("form", csrf_input(csrf),
             device_boxes(cat, device_choices(cat, g, user.devices), groups.devices_of(user, g)),
             t("button", "Сохранить", type="submit", class_="btn small"),
             t("span", "свои" if user.devices else "как у группы" if g else "", class_="muted small"),
             method="post", action=f"/users/{user.name}/devices", class_="inline", data_swap=True)


def devices_flash(cat: clients.Catalog, stored: list[str]) -> str:
    return ("Устройства: " + ", ".join(cat.platforms.get(d, d) for d in stored)) if stored else "Устройства — как у группы"


def user_devices(app: "App", req: "Request", name: str) -> "Response":
    devs = [d[:16] for d in req.multi.get("dev", [])][:8]
    try:
        stored = groups.set_devices([name], devs)[name]
        req.session.flash("ok", devices_flash(clients.load(), stored))
    except (users.UserError, LockTimeout, clients.ClientsError) as e:
        req.session.flash("bad", str(e))
    return _redirect(f"/users/{name}")


def _disable_confirm(label: str) -> str:
    return (f"Отключить «{label}»? Подключиться он не сможет. «Включить» вернёт доступ с теми же ключами — "
            "если телефон потерян, выдайте новые ключи.")


def _rekey_confirm(label: str) -> str:
    return (f"Новые ключи для «{label}»? Старые ссылки, QR и файлы перестанут работать сразу, на всех его устройствах — "
            "и телефон, и компьютер придётся настроить заново по новому сообщению.")


def user_rekey(app: "App", req: "Request", name: str) -> "Response":
    """«Новые ключи»: старые креды удаляются во всех протоколах, заводятся новые; человек отмечается «переслать»."""
    s = req.session
    try:
        rep = users.rekey(name)
    except (users.UserError, LockTimeout) as e:
        s.flash("bad", str(e))
        return _redirect(f"/users/{name}")
    except protolib.ProtoError as e:
        s.flash("bad", f"{e} {e.short()}")
        return _redirect(f"/users/{name}")
    app.invalidate("status")
    app.invalidate_links(name)
    u = users.list_users().get(name)
    label = u.label if u else name
    if rep.ok:
        s.flash("ok", f"{label}: новые ключи выданы, старые не работают. Отправьте новое сообщение — в нём сказано "
                      "удалить старые подключения. Новый телефон другой (Android ↔ iPhone)? Смените «устройства» в «Профиле».",
                [("карточка", "/handoff?" + urllib.parse.urlencode({"u": name}))])
    else:
        s.flash("bad", f"{label}: {rep.message}")
        for st in rep.failed:
            s.flash("bad", f"{st.proto_id}: {st.error}")
    return _redirect(f"/users/{name}")


def resend_alert(user: users.User, g: groups.Group | None, csrf: str) -> Markup | None:
    """«Переслать: …» на странице человека, с карточкой и «Отправлено»."""
    if not user.resend:
        return None
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None
    return alert_list([("warn", "Переслать: " + "; ".join(resend.describe(cat, user, g)),
                        [clientviews.zip_button(user.name, csrf, "ZIP", hint=False),
                         t("a", "Карточка", href="/handoff?" + urllib.parse.urlencode({"u": user.name}), class_="btn small"),
                         post_button("/resend", "Отправлено", csrf, "btn small",
                                     {"names": user.name, "back": f"/users/{user.name}"})])])


def contact_text(c: support.Contacts, user: users.User) -> str:
    """«12 мин назад · Hysteria2», «ни разу» или «—» (сбор трафика ещё не шёл)."""
    seen = c.of(user.name)
    if seen is None:
        return "ни разу" if c.known else "—"
    return f"{ago(seen[0])} · {support.via_title(seen[1], user)}"


def _broken(app: "App") -> set[str]:
    """Протоколы, которые сейчас не работают на самом сервере (сводка «Обзора», кэш минуту)."""
    try:
        from .views import status_data
        return {p["id"] for p in status_data(app)["protocols"] if p.get("enabled") and not p.get("ok")}
    except Exception:  # noqa: BLE001 — подсказка не должна ломать страницу человека
        return set()


def trouble_card(user: users.User, g: groups.Group | None, c: support.Contacts,
                 app: "App | None" = None) -> tuple[Markup | None, bool]:
    """«Если у человека не работает»: работают ли его протоколы на сервере, доходит ли он, что поменять по его
    устройствам (UDP/TCP, другое приложение — со ссылкой на настройки группы и сколько человек получат новое
    сообщение), что спросить. Есть предупреждение — блок раскрыт (второе значение: страница ставит его наверх)."""
    if user.system:
        return None, False
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None
    tips = support.checklist(user, g, c, cat, users.selectable_protocols(), users.variant_modules(),
                             broken=_broken(app) if app is not None else set())
    mem: list[users.User] = []
    if g is not None:
        try:
            mem = [u for u in groups.members_of(groups.Groups.load(), users.list_users(), g.id) if u.name != users.OWNER]
        except (groups.GroupError, users.UserError):
            mem = []
    devices = groups.devices_of(user, g) or []
    names = [cat.platforms.get(d, d) for d in devices] if cat else devices
    items: list[tuple[Any, ...]] = []
    if not user.devices and g is not None and len(g.devices) > 1:
        items.append(("info", "Устройства не отмечены — подсказки по всем устройствам группы. Отметьте его устройства "
                              "в «Профиле»: подсказки станут точнее."))
    said = False   # «касается всей группы» — один раз, у остальных правок только ссылка
    for kind, text, fix in tips:
        if fix and g is not None:
            n = sum(1 for u in mem if fix in (groups.devices_of(u, g) or []))
            dev = cat.platforms.get(fix, fix) if cat else fix
            link = t("a", "Настройки группы", href=f"/groups/{g.id}#settings",
                     title=f"новое сообщение получат {n} чел. с {dev}")
            tail = "" if said else (f" Правка — на всю группу «{g.name}»: новое сообщение получат те, у кого {dev} "
                                    f"({n} чел.). Нужно только ему — переведите его в отдельную группу.")
            said = True
            items.append((kind, text + tail, link))
        elif text.startswith("Ни разу") and user.enabled:
            items.append((kind, text, t("a", "Сообщение", href=f"/handoff?u={user.name}")))
        else:
            items.append((kind, text))
    try:
        from .. import journal
        rejects = journal.key_rejects()
    except Exception:  # noqa: BLE001
        rejects = 0
    if rejects and any(p.startswith("hysteria2") for p in user.protocols):
        items.append(("info", f"Hysteria2 отказал по неверному ключу {rejects} раз за сутки (чей ключ, не видно): если "
                              "это он, у него старый ключ — перешлите сообщение.", t("a", "Атаки", href="/journal")))
    if user.name != users.OWNER:
        items.append(("info", lost_text(devices, names)))
    items += [("info", "Спросите: Wi-Fi или мобильный и какой оператор, приложение, снимок ошибки, с какого времени; "
                       "если медленно — скорость на speedtest.net с VPN и без."),
              ("info", "Режет ли сеть у человека — пробник: Docker на компьютере в той же сети (на телефон не ставится).",
               t("a", "Проверка", href="/probe"))]
    alarm = any(it[0] in ("warn", "bad") for it in items)
    return t("details", t("summary", "Если у человека не работает"), alert_list(items), class_="card more trouble",
             open=alarm or None), alarm


PHONES = ("android", "ios")


def lost_text(devices: list[str], names: list[str]) -> str:
    """«Потерял …» по его устройствам: что отключат «Новые ключи» и что настроить заново."""
    phone = any(d in PHONES for d in devices)
    comp = any(d not in PHONES for d in devices)
    what = "телефон или компьютер" if phone and comp else "телефон" if phone else "компьютер" if comp else "устройство"
    if len(devices) > 1:
        return (f"Потерял {what} — «Новые ключи»: старые перестанут работать на всех его устройствах "
                f"({', '.join(names)}); оставшиеся настроить заново по новому сообщению.")
    return f"Потерял {what} — «Новые ключи»: старый ключ перестанет работать; новый — в новом сообщении."


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
        return t("span", f"{len(have)} из {len(want)}", class_="chip")
    lacking = [manifests.proto_title(p) for p in missing]
    shown = ", ".join(lacking[:2]) + ("…" if len(lacking) > 2 else "")
    return t("span", f"нет: {shown}", class_="chip warn",
             title=f"{len(have)} из {len(want)}" + (f"; нет в: {', '.join(lacking)}" if len(lacking) > 2 else "")
                   + ("; свой набор — сверка со всеми включёнными" if u.custom else ""))


def _users_spec(csrf: str, managed: list[str], mx: int, gs: groups.Groups) -> tbl.Spec:
    def name_cell(r: dict[str, Any]) -> Markup:
        u = r["user"]
        sub = " · ".join(x for x in (u.name if u.display else "", u.note) if x)
        return t("span", t("a", t("strong", u.label), href=f"/users/{u.name}"), " " if not u.enabled else None,
                 badge("откл.", "muted") if not u.enabled else None, " " if u.resend else None,
                 t("a", "переслать", href="/resend", class_="chip warn") if u.resend else None,
                 t("span", sub, class_="sub") if sub else None)

    def toggle(r: dict[str, Any]) -> Markup:
        u = r["user"]
        return post_button(f"/users/{u.name}/{'disable' if u.enabled else 'enable'}",
                           "Отключить" if u.enabled else "Включить", csrf, "btn small", {"back": "/users"},
                           confirm=_disable_confirm(u.label) if u.enabled else None)

    cols = [
        tbl.Col("name", "пользователь", cell=name_cell, value=lambda r: r["user"].label,
                find=lambda r: f"{r['name']} {r['user'].note}", sort=True, search=True),
        tbl.Col("login", "логин", value=lambda r: r["name"], hidden=True),
        tbl.Col("access", "доступ", sort=True, chip=True, hidden=True),
        tbl.Col("group", "группа", cell=lambda r: _group_cell(gs, r["user"]), sort=True, chip=True),
        tbl.Col("protos", "ключи", cell=lambda r: _proto_chip(r["user"], managed, gs),
                value=lambda r: ", ".join(r["user"].protocols), secondary=True,
                hint="в скольких протоколах группы у человека заведён ключ; «нет: …» — где не хватает"),
        tbl.Col("day", "24 ч", cell=lambda r: human_bytes(r["day"]), value=lambda r: r["day"], num=True, sort=True,
                first_desc=True),
        tbl.Col("bar", "", cell=lambda r: charts.bar(r["day"], mx), secondary=True, export=False),
        tbl.Col("month", "30 дней", cell=lambda r: human_bytes(r["month"]), value=lambda r: r["month"], num=True,
                sort=True, secondary=True),
        tbl.Col("seen", "подключался",
                cell=lambda r: t("span", ago(r["seen"]) if r["seen"] else r["never"], class_="nowrap",
                                 title=r["via"] or None),
                value=lambda r: datetime.fromtimestamp(r["seen"]).strftime("%Y-%m-%d %H:%M") if r["seen"] else "",
                sort=True, first_desc=True, secondary=True),
        tbl.Col("act", "", cell=toggle, export=False),
    ]
    return tbl.Spec(path="/users", cols=cols, sort="name", id_key="name", paged=False, empty="пользователей нет",
                    placeholder="имя или заметка", href=lambda r: f"/users/{r['name']}",
                    row_cls=lambda r: None if r["user"].enabled else "off", name="users",
                    select="names", select_form="bulk")


def _user_rows(shown: list[users.User], gs: groups.Groups) -> list[dict[str, Any]]:
    day = {r["key"]: r["total"] for r in traffic.report(period="24h", by="user")["rows"]}
    month = {r["key"]: r["total"] for r in traffic.report(period="30d", by="user")["rows"]}
    c = support.contacts()
    xui = support.xui_protocols()
    out = []
    for u in shown:
        seen = c.of(u.name)
        out.append({"name": u.name, "user": u, "access": "включён" if u.enabled else "отключён",
                    "day": day.get(u.name, 0), "month": month.get(u.name, 0), "seen": seen[0] if seen else None,
                    "via": support.via_title(seen[1], u, xui) if seen else "", "never": "ни разу" if c.known else "—",
                    "group": g.name if (g := gs.get(u.group)) else ""})
    return out


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
    variants = users.variant_modules()
    preset = set(first.resolve(managed, variants)) if first else set(managed)
    protos = [t("label", t("input", type="checkbox", name="proto", value=p, checked=p in preset),
                t("span", manifests.proto_title(p))) for p in managed]
    add_form = t("form", csrf_input(csrf),
                 t("div",
                   t("div", t("label", "Имя", for_="display"),
                     t("input", type="text", name="display", id="display", required=True, maxlength="100",
                       placeholder="Иван Петров", autocomplete="off",
                       title="Как к человеку обращаться: любые буквы. Ключи заводятся во всех отмеченных "
                             "протоколах; при ошибке в одном изменения откатываются."), class_="field"),
                   t("div", t("label", "Логин", for_="name"),
                     t("input", type="text", name="name", id="name", maxlength="32",
                       pattern="[A-Za-z0-9][A-Za-z0-9_\\-]{0,31}", placeholder="из имени", autocomplete="off",
                       autocapitalize="none", spellcheck="false",
                       title="Латиница, цифры, «-» и «_»; регистр не важен. Пусто — получится из имени."),
                     class_="field"),
                   t("div", t("label", "Заметка", for_="note"),
                     t("input", type="text", name="note", id="note", maxlength="200", placeholder="кто это"),
                     class_="field grow"),
                   t("div", t("label", "Группа", for_="group"),
                     t("select", [t("option", g.name, value=g.id, selected=g is first,
                                    data_protos=" ".join(g.resolve(managed, variants))) for g in gs.groups],
                       name="group", id="group", data_group=True,
                       title="Протоколы и приложения — как у группы"), class_="field")
                   if gs.groups else None,
                   t("button", "Добавить", type="submit", class_="btn primary"), class_="form-row"),
                 _add_devices(),
                 t("div", t("span", "Протоколы:", class_="label"), t("div", protos, class_="checks"),
                   t("div", "Отмечены протоколы группы; другой набор станет «своим», и группа его не тронет.",
                     class_="hint"), class_="field")
                 if protos else alert_list([("warn", "Нет протоколов, куда можно добавить пользователя.")]),
                 method="post", action="/users", class_="stack", data_swap=True)
    verify, missing = (verify_card() if req.query.get("verify") else (None, False))
    waiting = sum(1 for u in shown if u.resend)
    head_actions = t("div",
                     t("a", f"Кому переслать: {waiting}", href="/resend", class_="btn small primary", data_swap=True)
                     if waiting else None,
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


def _add_devices() -> Markup | None:
    """«Устройства» в форме добавления: без них человек получил бы инструкции для всех устройств группы."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return None
    return t("div", t("span", "Устройства:", class_="label"), t("input", type="hidden", name="devs", value="1"),
             device_boxes(cat, list(cat.platforms), []),
             t("div", "Какие у человека: инструкции и ключи будут только для них.", class_="hint"), class_="field")


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
             t("button", "Новые ключи", type="submit", name="action", value="rekey", class_="btn small",
               title="Старые перестанут работать; спросим подтверждение"),
             t("button", "Раздать", type="submit", name="action", value="handoff", class_="btn small primary",
               title="Карточки с QR и ссылками для отмеченных: печать, ZIP"),
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
    display = re.sub(r"\s+", " ", req.form.get("display", "")).strip()[:100]
    name = req.form.get("name", "").strip().lower()
    note = req.form.get("note", "").strip()[:200]
    if not name:   # логин из имени, как в списке людей: транслит, при совпадении — номер
        name, _ = people.login_for(display, set(users.list_users().names()))
        if not name:
            req.session.flash("bad", "Введите имя или логин" if not display
                              else "В имени нет ни букв, ни цифр: введите логин")
            return _redirect("/users")
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
    devs = [d[:16] for d in req.multi.get("dev", [])][:8]
    if req.form.get("devs") == "1" and not devs:
        req.session.flash("bad", "Отметьте устройства человека: инструкции и ключи будут только для них")
        return _redirect("/users")
    rep = _user_op(req, users.add_user, name, note=note, only=only, group=group,
                   display=display if display != name else "")
    app.invalidate("status")
    app.invalidate_links(name)
    if not (rep and rep.ok):
        return _redirect("/users")
    if devs:
        try:
            groups.set_devices([name], devs)
        except (users.UserError, LockTimeout, clients.ClientsError) as e:
            req.session.flash("bad", str(e))
    req.session.flash("info", "Раздать: сообщение с его ссылками — ниже, карточка и ZIP — по ссылке",
                      [("карточка", "/handoff?" + urllib.parse.urlencode({"u": name}))])
    return _redirect(f"/users/{name}")


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
    body = card(f"Удалить «{user.label}»?",
                t("p", "VPN перестанет работать на всех устройствах человека: ссылки, QR и файлы больше не подключат. "
                       "Отменить нельзя — только завести заново и раздать новые ключи."),
                t("p", "Временно закрыть доступ — «Отключить» на его странице.", class_="hint"),
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
BULK_DONE = {"delete": "Удалено", "disable": "Отключено", "enable": "Включено", "rekey": "Новые ключи"}
BULK_ASK = {   # действия, которые нельзя отменить: (заголовок, текст, кнопка, заголовок страницы)
    "delete": ("Удалить пользователей", "VPN перестанет работать на всех их устройствах: ссылки, QR и файлы больше не "
               "подключат. Отменить нельзя — только завести заново и раздать новые ключи.", "Удалить навсегда", "Удаление"),
    "rekey": ("Выдать новые ключи", "Старые ссылки, QR и файлы перестанут работать сразу, на всех их устройствах. "
              "Новые нужно будет отправить каждому заново.", "Выдать новые ключи", "Новые ключи"),
}


def _bulk_confirm(req: "Request", app: "App", op: str, names: list[str], skipped: list[str]) -> "Response":
    csrf = req.session.csrf if req.session else ""
    head, text, button, page = BULK_ASK[op]
    form = t("form", csrf_input(csrf), t("input", type="hidden", name="action", value=op),
             t("input", type="hidden", name="confirm", value="1"),
             [t("input", type="hidden", name="names", value=n) for n in names],
             t("button", button, type="submit", class_="btn danger-solid"),
             method="post", action="/users/bulk", class_="inline", data_swap=True)
    reg = users.list_users()
    body = card(f"{head}: {len(names)}?", t("p", text),
                t("div", [t("span", u.label if (u := reg.get(n)) else n, class_="chip") for n in names], class_="chips"),
                t("p", "Пропущены: " + "; ".join(skipped), class_="hint") if skipped else None,
                t("div", form, t("a", "Отмена", href="/users", class_="btn", data_swap=True), class_="actions"),
                cls="danger-zone")
    return app.render(req, page, [page_head(page if op == "rekey" else "Удаление пользователей"), body], active="/users")


def users_bulk(app: "App", req: "Request") -> "Response":
    """Одно действие над отмеченными: delete (через страницу подтверждения), disable, enable, move:<группа>.
    Сервер ничему из формы не верит: имена проверяются по реестру, owner и служебные для удаления и
    отключения отклоняются, остальные идут дальше; итог — одно сообщение, отказы — отдельными."""
    s = req.session
    action = req.form.get("action", "")[:60]
    op, _, gid = action.partition(":")
    if op not in (*users.BULK_OPS, "move", "handoff") or (op == "move") != bool(gid):
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
    if op == "handoff":
        if not todo:
            s.flash("warn", "Раздавать некому")
            for e in errors:
                s.flash("bad", e)
            return _redirect("/users")
        return _redirect("/handoff?" + urllib.parse.urlencode({"u": ",".join(todo)}))
    if op in BULK_ASK and todo and not req.form.get("confirm"):
        return _bulk_confirm(req, app, op, todo, errors)
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
        s.flash("ok" if len(done) == len(names) else "warn", f"{verb}: {len(done)} из {len(names)} — {', '.join(done)}",
                [("кому переслать", "/resend")] if op == "rekey" else ())
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
    seen = support.contacts()

    toggle = post_button(f"/users/{name}/{'disable' if user.enabled else 'enable'}",
                         "Отключить" if user.enabled else "Включить", csrf, "btn",
                         confirm=_disable_confirm(user.label) if user.enabled else None)
    rekey = None if name == users.OWNER else post_button(f"/users/{name}/rekey", "Новые ключи", csrf, "btn",
                                                         confirm=_rekey_confirm(user.label))
    actions = t("div", rekey, toggle, t("a", "Удалить…", href=f"/users/{name}/delete", class_="btn danger"),
                class_="actions")
    if user.system:
        actions = badge("служебный: пробник", "muted")
    try:
        al = allowlist.Allowlist.load()
        apps = "свой список" if al.own(name) else ("список группы" if al.from_group(name) else "общий список")
    except allowlist.AllowlistError:
        apps = "общий список"
    grp = _group_of(user)
    info = card("Профиль", kv([
        ("статус", badge("включён", "ok") if user.enabled else
         t("span", "отключён", class_="badge muted", title="креды сохранены, доступ закрыт")),
        ("группа", _group_link(user)),
        *([] if user.system else [("устройства", _devices_form(user, grp, csrf))]),
        ("через VPN", t("a", apps, href=f"/apps?user={name}")),
        *([("имя", user.display)] if user.display else []),
        ("заметка", user.note or "—"),
        ("создан", _created_local(user.created)),
        ("подключался", contact_text(seen, user)),
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
    show = link_filter(user, grp)
    ctx = clientviews.Ctx.load()
    shown: dict[str, str] = {}
    devices = groups.devices_of(user, grp)
    update = resend.update_line(ctx.cat if ctx else None, user, grp)
    connect = clientviews.connect_card(links, name, ctx, grp, label=user.label, shown=shown, devices=devices,
                                       csrf=csrf, update=update)
    where = used_where(links, ctx, grp, devices, name) if ctx is not None and grp is not None and grp.clients else None
    if where:   # только ключи и файлы его устройств: чужой файл «Windows» у человека с одним телефоном путает
        base = show
        split = any(Path(ln.uri).name.endswith("-android.conf") for ln in links if ln.kind == "file")
        show = lambda ln: ((base is None or base(ln)) and ln.variant in where   # noqa: E731
                           and (not split or _for_devices(ln, where[ln.variant])))
    tiles = connect_tiles(links, manifests.load_all()[0], name, show, shown, bool(connect), where,
                          mine=[ctx.cat.platforms.get(d, d) for d in devices] if ctx is not None and devices else None)
    # плитки — «всё как есть» для тех, кому нужен конкретный вариант; без нового блока они остаются главными
    advanced = t("details", t("summary", "Все ссылки и QR"), quick_start(links, name, show, ctx, grp, devices), tiles,
                 t("p", clientviews.SEND_WARN, class_="hint") if connect is None else None,
                 class_="card more", open=connect is None or None) if tiles else None
    sub = " · ".join(x for x in ((name if user.display else ""), user.note) if x)
    trouble, alarm = trouble_card(user, grp, seen, app)
    body = [page_head(user.label, sub or None, actions, top=False), resend_alert(user, grp, csrf), err_list,
            trouble if alarm else None, connect, advanced,
            None if connect or advanced else card("Подключить", t("p", "Ключей нет.", class_="muted")),
            None if alarm else trouble,
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
PLATFORMS = {
    "vless-reality": "iPhone, Android, Windows",
    "vless-xhttp": "iPhone, Android, Windows",
    "hysteria2": "iPhone, Android, Windows",
    "hysteria2-obfs": "Android, Windows",
    "amneziawg": "Android, iPhone, Windows",
    "tuic": "iPhone, Android, Windows",
    "ss2022": "iPhone, Android, Windows",
    allowlist.V2RAYN_PROTO: "Windows",
}


QR_STEP_RE = re.compile(r"нажмите (.+?) и (?:наведите|отсканируйте)")


def qr_caption(client: dict[str, Any], platform: str) -> str:
    """Подпись к быстрому QR из шага импорта каталога: «Android: Happ → «+» → «Сканировать QR»»."""
    chain = (m.group(1) if (m := QR_STEP_RE.search(client.get("import", {}).get("qr", ""))) else "")
    if chain and "канир" not in chain.lower():
        chain += " → сканировать"
    return f"{platform}: {client['name']} → {chain}" if chain else f"{platform}: {client['name']} — сканировать QR"


def _variant_order(link: protolib.Link) -> int:
    """Файлы раньше ссылок, Android-конфиг — первым: так окно AmneziaWG открывается на нём."""
    if link.kind == "file":
        return 0 if Path(link.uri).name.endswith("-android.conf") else 1
    return 2


def _variant_label(link: protolib.Link, uri_n: int, devs: list[str] | None = None) -> tuple[str, str | None]:
    """(текст вкладки, подсказка): как в остальных экранах — «Ссылка», «Ссылка 2», «Файл Android»; термины — в подсказку.
    devs — его устройства, которым нужен этот ключ: общий файл подписан только ими (у человека с Mac — не «Windows, iPhone»)."""
    if link.kind == "file":
        fname = Path(link.uri).name
        if fname.endswith("-android.conf"):
            return "Файл Android", "список приложений Android внутри файла"
        if fname.endswith(".conf"):
            others = [d for d in (devs if devs is not None else ["Windows", "iPhone"]) if d != "Android"]
            return ("Файл " + ", ".join(others) if others else "Файл"), "общий .conf без списка приложений"
        return "Файл правил", None
    if link.uri.startswith("vpn://"):
        return "Ключ AmneziaVPN", None
    why = link.label or None
    if "obfs=salamander" in link.uri:
        why = "Salamander: обфускация Hysteria2"
    elif re.search(r"@[^/?#]*:\d+,\d", link.uri):
        why = "Port hopping: порт меняется"
    return ("Ссылка" if uri_n == 1 else f"Ссылка {uri_n}"), why


def qr_url(name: str, idx: int, link: protolib.Link) -> str:
    """Адрес QR ссылки: номер в списке и её метка (сдвиг списка → 404, а не чужой QR)."""
    return f"/users/{name}/qr/{idx}?p={link.tag}"


def _variant(link: protolib.Link, idx: int, name: str, vid: str, hidden: bool, rules_text: str = "",
             shown: dict[str, str] | None = None) -> Markup:
    fname = Path(link.uri).name if link.kind == "file" else ""
    if link.kind == "file":
        action = (t("a", "Скачать файл", href=f"/users/{name}/file/{fname}", class_="btn primary")
                  if FILE_NAME_RE.fullmatch(fname) else t("p", "Файл недоступен для скачивания", class_="muted small"))
    elif shown and link.uri in shown:
        # ссылка уже видна в блоке «Подключить»: здесь только QR и кнопка, копирующая то же поле
        action = t("div", t("button", "Копировать", type="button", class_="btn primary", data_copy=shown[link.uri]),
                   class_="link-uri")
    else:
        uri_id = f"uri-{idx}"
        action = t("div", t("input", type="text", id=uri_id, value=link.uri, readonly=True, data_select=True,
                            aria_label="Ссылка"),
                   t("button", "Копировать", type="button", class_="btn primary", data_copy=uri_id),
                   class_="link-uri")
    if link.proto_id == allowlist.V2RAYN_PROTO:
        qr_block: Any = t("p", rules_text, class_="hint") if rules_text else None
    elif link.kind == "uri" and len(link.uri.encode("utf-8")) > qr.MAX_BYTES:
        qr_block = t("p", "Ссылка слишком длинная для QR", class_="muted small")
    elif link.kind == "file" and not fname.endswith(".conf"):
        qr_block = None
    else:
        # QR рисуется по требованию: app.js ставит src видимому варианту при открытии окна и смене вкладки
        qr_block = t("img", class_="qr", data_src=qr_url(name, idx, link), width=240, height=240, alt="QR")
    return t("div", qr_block, action, class_="variant", id=vid, hidden=hidden or None)


def used_where(links: list[protolib.Link], ctx: clientviews.Ctx, g: groups.Group | None, devices: list[str] | None,
               name: str) -> dict[str, str]:
    """{протокол или файл: «Android, Windows»} — что из его ключей берут приложения группы на его устройствах (как в
    «Подключить»): плитки показывают только это и подписаны его устройствами."""
    prefer, order = clientviews.group_prefs(g)
    out: dict[str, list[str]] = {}
    for plat, title in ctx.cat.platforms.items():
        if devices is not None and plat not in devices:
            continue
        pack = clientviews.build_pack(ctx.cat, ctx.cache, plat, links, ctx.mans, prefer, order, clientviews.store_first(g),
                                      ctx.al, ctx.apps_for(plat, g, name), bool(g and g.install_mode == "admin"))
        for sec in (pack.sections if pack else ()):
            for it in [*sec.items, *sec.extras]:
                if title not in out.setdefault(it.proto, []):
                    out[it.proto].append(title)
    return {k: ", ".join(v) for k, v in out.items()}


def _for_devices(link: protolib.Link, titles: str) -> bool:
    """Файл AmneziaWG, когда их два: «-android.conf» (список приложений внутри) — только если у человека Android, общий
    .conf — только если есть другое устройство."""
    if link.kind != "file" or not link.uri.endswith(".conf"):
        return True
    devs = [x.strip() for x in titles.split(",")]
    android = "Android" in devs
    return android if Path(link.uri).name.endswith("-android.conf") else any(d != "Android" for d in devs)


def link_filter(user: users.User, g: groups.Group | None) -> Callable[[protolib.Link], bool] | None:
    """Какие ссылки показывать человеку: Hysteria2 и Salamander — только выбранные группой (ссылки обоих отдаёт
    один модуль). Свой набор, владелец, группа «всех» и человек без группы видят всё; остальные протоколы не трогаются."""
    if g is None or g.all_protocols or user.custom or user.name == users.OWNER:
        return None
    variants = users.variant_modules()
    family = {*variants, *variants.values()}
    offered = set(g.offered(users.selectable_protocols()))
    return lambda ln: ln.variant not in family or ln.variant in offered


def _quick_pick(links: list[protolib.Link], ctx: clientviews.Ctx, g: groups.Group | None,
                show: Callable[[protolib.Link], bool],
                devices: list[str] | None = None) -> tuple[int, protolib.Link, str] | None:
    """Лучший QR среди приложений набора группы: (номер ссылки, ссылка, подпись). Телефон раньше компьютера, протокол —
    лучший по замерам с устройств, иначе по порядку раздачи (PRIORITY). Ссылку берёт то приложение, которое её
    открывает (клиент и платформа выбраны набором группы), чужая под подписью приложения не окажется."""
    top: list[str] = []
    try:
        for c in rank.load("30d"):
            top += [p["proto"] for p in c["top"]]
    except (sqlite3.Error, OSError, ValueError):
        pass
    order_p = [*top, *groups.PRIORITY]
    prefer, order = clientviews.group_prefs(g)
    best: tuple[tuple[int, int], int, protolib.Link, str] | None = None
    for plat, title in ctx.cat.platforms.items():
        if devices is not None and plat not in devices:   # «Быстрый старт Android» человеку с одним iPhone не нужен
            continue
        pack = clientviews.build_pack(ctx.cat, ctx.cache, plat, links, ctx.mans, prefer, order, clientviews.store_first(g),
                                      ctx.al, None, bool(g and g.install_mode == "admin"))
        for sec in (pack.sections if pack else ()):
            if "qr" not in sec.client.get("import", {}):
                continue
            for it in sec.items:
                i = clientviews.pick_link(it.proto, sec.client, plat, links, "qr")
                if i is None or not show(links[i]):
                    continue
                key = ({"android": 0, "ios": 1}.get(plat, 2), order_p.index(it.proto) if it.proto in order_p else 99)
                if best is None or key < best[0]:
                    best = (key, i, links[i], qr_caption(sec.client, title))
    return best[1:] if best else None


COPY_ALL = ".pdlg [data-copy]"   # «Скопировать всё»: все поля ссылок из окон протоколов, без дублей


def quick_start(links: list[protolib.Link], name: str, show: Callable[[protolib.Link], bool] | None = None,
                ctx: clientviews.Ctx | None = None, g: groups.Group | None = None,
                devices: list[str] | None = None) -> Markup | None:
    """Один QR лучшего протокола из набора приложений группы и «скопировать всё». Нет набора приложений — «Приложения
    не выбраны → Настроить»; без каталога (ctx) — только «скопировать всё»."""
    show = show or (lambda ln: True)
    all_uris = "\n".join(l.uri for l in links if show(l) and l.kind == "uri")
    if g is not None and not g.clients:
        pick, note = None, clientviews.no_apps(g)
    else:
        pick = _quick_pick(links, ctx, g, show, devices) if ctx is not None else None
        note = t("p", pick[2], class_="muted") if pick else None
    if pick is None and not all_uris and note is None:
        return None
    copy = t("div", t("button", "Скопировать всё", type="button", class_="btn", data_copy_all=COPY_ALL,
                      title="Все ссылки по одной в строке"), class_="actions") if all_uris else None
    if pick is None and note is None:   # без QR «Быстрый старт» — пустой заголовок: остаётся одна кнопка
        return copy
    img = (t("img", class_="qr", src=qr_url(name, pick[0], pick[1]), width=160, height=160, alt="QR",
             title=manifests.proto_title(pick[1].variant)) if pick else None)
    return t("div", img, t("div", t("h3", "Быстрый старт"), note, copy), class_="quick")


def connect_tiles(links: list[protolib.Link], mans: list[Any], name: str,
                  show: Callable[[protolib.Link], bool] | None = None, shown: dict[str, str] | None = None,
                  has_connect: bool = False, where: dict[str, str] | None = None,
                  mine: list[str] | None = None) -> Markup | None:
    """Плитки по протоколам; клик — окно протокола: вкладки вариантов, QR, копировать/скачать.
    show — какие ссылки показывать (номера для QR остаются по полному списку); shown — ссылки, уже выведенные полями
    в «Подключить»; has_connect — шаги импорта правил уже в инструкции там же. where — {протокол: его устройства};
    mine — названия его устройств: подписи плиток и файлов — только ими."""
    if not links:
        return None
    by_id = {m.id: m for m in mans}
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None  # без каталога плитки без подсказки о клиентах
    by_proto: dict[str, list[tuple[int, protolib.Link]]] = {}
    for i, link in enumerate(links):
        if show is None or show(link):
            by_proto.setdefault(link.variant, []).append((i, link))
    if not by_proto:
        return None
    # протоколы — в порядке раздачи (PRIORITY), файл правил v2rayN — не протокол: отдельной строкой «Файлы»
    order = [*groups.by_priority(p for p in by_proto if p != allowlist.V2RAYN_PROTO),
             *([allowlist.V2RAYN_PROTO] if allowlist.V2RAYN_PROTO in by_proto else [])]
    rules_text = clientviews.rules_text(cat) if cat and not has_connect else ""
    sections: dict[bool, list[Markup]] = {True: [], False: []}
    dialogs = []
    for n, pid in enumerate(order):
        m = by_id.get(pid)
        title = ("Правила v2rayN" if pid == allowlist.V2RAYN_PROTO else manifests.proto_title(pid))
        dlg_id = f"dlg-{n}"
        static = [x.strip() for x in PLATFORMS.get(pid, "").split(",") if x.strip()]
        devs = ([x.strip() for x in where[pid].split(",")] if where and where.get(pid) else
                [x for x in static if mine is None or x in mine])
        sections[pid != allowlist.V2RAYN_PROTO].append(t(
            "button", t("span", title, class_="ptile-name"),
            t("span", ", ".join(devs) or "—", class_="ptile-sub"),
            type="button", class_=f"ptile acc{n % 8 + 1}", data_dialog=dlg_id))
        items = sorted(by_proto[pid], key=lambda it: _variant_order(it[1]))  # sorted стабилен: порядок модуля цел
        tabs_data, uri_n = [], 0
        for k, (_, link) in enumerate(items):
            if link.kind == "uri" and not link.uri.startswith("vpn://"):
                uri_n += 1
            tabs_data.append(_variant_label(link, uri_n, devs if (where or mine is not None) else None))
        tabs = t("div", [t("button", label, type="button", data_tab=f"{dlg_id}-v{k}", title=why,
                           class_="tab active" if k == 0 else "tab")
                         for k, (label, why) in enumerate(tabs_data)], class_="tabs", role="tablist") \
            if len(items) > 1 else None
        variants = [_variant(link, i, name, f"{dlg_id}-v{k}", k > 0, rules_text, shown) for k, (i, link) in enumerate(items)]
        more = t("details", t("summary", "подробнее"), t("p", m.notes, class_="hint")) if m and m.notes else None
        dialogs.append(t("dialog",
                         t("div", t("h3", title), t("button", "✕", type="button", class_="btn small", data_close=True,
                                                    aria_label="Закрыть"), class_="dlg-head"),
                         tabs, variants, more, id=dlg_id, class_="pdlg"))
    out = [t("div", sections[True], class_="ptiles")] if sections[True] else []
    if sections[False]:
        out += [t("h3", "Файлы", class_="sub-h"), t("div", sections[False], class_="ptiles")]
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
