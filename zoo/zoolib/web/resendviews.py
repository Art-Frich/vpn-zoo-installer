"""«Кому переслать» (D57): после новых ключей, смены группы, протоколов, приложений или списка «через VPN» — кто и что
должен получить заново. Люди собраны по тому, что им отправить: так рассылка идёт пачкой, а не по одному."""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

from .. import clients, groups, resend, users
from ..fsutil import LockTimeout
from .html import Markup, card, csrf_input, t
from .views import page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

TITLE = "Кому переслать"
PANELS_MAX = 3   # людей на странице не больше — их сообщения прямо здесь, с «Скопировать сообщение»
BACK_RE = re.compile(r"/users/[a-z0-9][a-z0-9_-]{0,31}")


def _redirect(location: str) -> "Response":
    from .app import redirect
    return redirect(location)


def sections() -> list[tuple[str, list[users.User]]]:
    """[(что отправить, кому)]: «сообщение целиком» первыми, дальше по приложениям."""
    reg = users.list_users()
    try:
        cat: clients.Catalog | None = clients.load()
    except clients.ClientsError:
        cat = None
    gs = groups.Groups.load()
    out: dict[str, list[users.User]] = {}
    for u in resend.pending(reg):
        for line in resend.describe(cat, u, gs.get(u.group)):
            out.setdefault(line, []).append(u)
    full = set(resend.KIND_TEXT.values())
    return sorted(out.items(), key=lambda kv: (kv[0] not in full, kv[0]))


def waiting() -> int:
    return len(resend.pending(users.list_users()))


def link(n: int) -> Markup | None:
    """«Кому переслать: N →» для Обзора и списков."""
    return t("a", f"Кому переслать: {n} →", href="/resend", data_swap=True) if n else None


def _person_row(u: users.User, panel: Markup | None) -> Markup:
    """Человек в разделе: отметка «отправлено», главное — его сообщение («Скопировать сообщение» на его странице или
    прямо здесь) и ZIP; карточки для печати — второй ссылкой раздела."""
    zip_btn = t("button", "ZIP", type="submit", class_="btn small", form=f"zip-{u.name}")
    head = t("div", t("label", t("input", type="checkbox", name="names", value=u.name), t("strong", u.label), class_="chk"),
             t("a", "Скопировать сообщение →", href=f"/users/{u.name}", class_="btn small primary") if panel is None
             else None, zip_btn, class_="actions")
    return t("div", head, t("details", t("summary", "Сообщение"), panel, open=True, class_="more") if panel else None,
             class_="resend-row")


def _zip_forms(csrf: str, names: list[str]) -> list[Markup]:
    """Формы ZIP по человеку — вне общей формы «Отправлено» (вложенных форм не бывает): кнопки ссылаются на них form=."""
    return [t("form", csrf_input(csrf), t("input", type="hidden", name="u", value=n),
              t("input", type="hidden", name="fmt", value="zip"), id=f"zip-{n}", method="post",
              action="/handoff/export", hidden=True) for n in names]


def _panels(app: "App", people: list[users.User]) -> dict[str, Markup | None]:
    """Сообщения прямо на странице, если людей немного (их ссылки собираются вызовами модулей протоколов)."""
    from . import clientviews, userviews
    out: dict[str, Markup | None] = {u.name: None for u in people}
    if len(people) > PANELS_MAX:
        return out
    ctx = clientviews.Ctx.load()
    if ctx is None:
        return out
    gs = groups.Groups.load()
    for u in people:
        g = gs.get(u.group)
        links, _ = userviews._cached_links(app, u.name)
        out[u.name] = clientviews.connect_panel(links, u.name, ctx, g, uid=f"{u.name}-", label=u.label, primary=False,
                                                devices=groups.devices_of(u, g), who=u)
    return out


def page(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    secs = sections()
    if not secs:
        return app.render(req, TITLE, [page_head(TITLE), card("Всё отправлено", t("p", "Пересылать никому не нужно.",
                                                                                     class_="muted"))], active="/users")
    everyone = list({u.name: u for _, people in secs for u in people}.values())
    panels = _panels(app, everyone)
    shown: set[str] = set()
    blocks = []
    for what, people in secs:
        names = [u.name for u in people]
        rows = [_person_row(u, None if u.name in shown else panels.get(u.name)) for u in people]
        shown.update(names)
        blocks.append(card(what, t("div", rows, class_="stack"),
                           extra=t("a", "Карточки (печать)", href="/handoff?" + urllib.parse.urlencode({"u": ",".join(names)}),
                                   class_="small", data_swap=True)))
    form = t("form", csrf_input(csrf), blocks,
             t("div", t("button", "Отправлено отмеченным", type="submit", class_="btn primary"), class_="actions"),
             method="post", action="/resend", class_="stack", data_swap=True)
    return app.render(req, TITLE, [page_head(TITLE, f"{len(everyone)} чел."),
                                   t("p", "Каждому — его сообщение: «Скопировать сообщение» (в нём сказано, что сделать со "
                                          "старым) и файл или ZIP. Отправили — отметьте: человек пропадёт из списка.",
                                     class_="hint"), form, _zip_forms(csrf, [u.name for u in everyone])],
                      active="/users")


def post(app: "App", req: "Request") -> "Response":
    names = list(dict.fromkeys(n[:32] for n in req.multi.get("names", [])[:300]))
    back = req.form.get("back", "")
    back = back if BACK_RE.fullmatch(back) else "/resend"
    if not names:
        req.session.flash("warn", "Никого не отмечено")
        return _redirect(back)
    try:
        done = users.clear_resend(names)
    except (users.UserError, LockTimeout) as e:
        req.session.flash("bad", str(e))
        return _redirect(back)
    reg = users.list_users()
    who = [u.label if (u := reg.get(n)) else n for n in done]
    req.session.flash("ok", "Отправлено: " + ", ".join(who) if who else "Уже было отмечено")
    return _redirect(back)
