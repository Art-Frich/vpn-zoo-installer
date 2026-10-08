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


def page(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    secs = sections()
    if not secs:
        return app.render(req, TITLE, [page_head(TITLE), card("Всё отправлено", t("p", "Пересылать никому не нужно.",
                                                                                     class_="muted"))], active="/users")
    blocks = []
    total = len({u.name for _, people in secs for u in people})
    for what, people in secs:
        names = [u.name for u in people]
        blocks.append(card(what,
                           t("div", [t("label", t("input", type="checkbox", name="names", value=u.name),
                                       t("a", u.label, href=f"/users/{u.name}"), class_="chk") for u in people],
                             class_="chips"),
                           extra=t("a", "Карточки", href="/handoff?" + urllib.parse.urlencode({"u": ",".join(names)}),
                                   class_="btn small", data_swap=True)))
    form = t("form", csrf_input(csrf), blocks,
             t("div", t("button", "Отправлено отмеченным", type="submit", class_="btn primary"), class_="actions"),
             method="post", action="/resend", class_="stack", data_swap=True)
    return app.render(req, TITLE, [page_head(TITLE, f"{total} чел."),
                                   t("p", "Отправили — отметьте: человек пропадёт из списка.", class_="hint"), form],
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
