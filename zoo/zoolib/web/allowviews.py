"""Страница «Приложения через VPN»: общий список и свои списки пользователей (zoo allow)."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any

from .. import allowlist, protolib, users
from ..fsutil import LockTimeout
from .html import Markup, badge, card, csrf_input, post_button, t, table
from .views import alert_list, page_head

if TYPE_CHECKING:
    from .app import App, Request, Response

PLACEHOLDER = {"android": "com.example.app", "windows": "program.exe"}
HINT = {"android": "Пакет Android — как в адресе Google Play: play.google.com/store/apps/details?id=ПАКЕТ.",
        "windows": "Имя процесса как в Диспетчере задач (вкладка «Подробности»), с учётом регистра."}


def _back(user: str | None) -> "Response":
    from .app import redirect
    return redirect("/apps" + (f"?{urllib.parse.urlencode({'user': user})}" if user else ""))


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


def _platform_card(al: allowlist.Allowlist, platform: str, user: str | None, csrf: str) -> Markup:
    items = al.effective(platform, user)
    hidden = {"user": user} if user else {}
    rows = []
    for ident in items:
        rows.append([t("code", ident), allowlist.title_of(platform, ident) or t("span", "—", class_="muted"),
                     t("form", csrf_input(csrf),
                       t("input", type="hidden", name="action", value="del"),
                       t("input", type="hidden", name="platform", value=platform),
                       t("input", type="hidden", name="app", value=ident),
                       t("input", type="hidden", name="user", value=user) if user else None,
                       t("button", "✕", type="submit", class_="btn small", aria_label=f"Убрать {ident}",
                         title="Убрать из списка"), method="post", action="/apps", class_="inline")
                     if len(items) > 1 else t("span", "последнее", class_="muted small",
                                             title="Пустой список не допускается")])
    options = [t("option", "— из каталога —", value="")]
    options += [t("option", f"{a.title} ({getattr(a, platform)})", value=getattr(a, platform))
                for a in allowlist.CATALOG
                if getattr(a, platform) and getattr(a, platform).lower() not in {i.lower() for i in items}]
    form = t("form", csrf_input(csrf),
             t("input", type="hidden", name="action", value="add"),
             t("input", type="hidden", name="platform", value=platform),
             t("input", type="hidden", name="user", value=user) if user else None,
             t("div",
               t("div", t("label", "Приложение", for_=f"pick-{platform}"),
                 t("select", options, name="app", id=f"pick-{platform}"), class_="field"),
               t("div", t("label", "или вручную", for_=f"custom-{platform}"),
                 t("input", type="text", name="custom", id=f"custom-{platform}", placeholder=PLACEHOLDER[platform],
                   title=HINT[platform], maxlength="128", autocomplete="off", autocapitalize="none",
                   spellcheck="false"), class_="field grow"),
               t("button", "Добавить", type="submit", class_="btn primary"), class_="form-row"),
             method="post", action="/apps", class_="stack")
    return card(allowlist.PLATFORM_TITLE[platform],
                table(["id", "приложение", ""], rows, empty="список пуст", stack=True), form,
                help=HINT[platform])


def _who_nav(al: allowlist.Allowlist, reg: users.Registry, user: str | None) -> Markup:
    """Чей список — ссылками-сегментами: «общий» и по одному на пользователя (свой список — со звёздочкой)."""
    links = [t("a", "общий", href="/apps", class_="active" if not user else None, title="список для всех")]
    for u in reg.visible():
        own = al.own(u.name)
        links.append(t("a", u.name + (" ★" if own else ""), href=f"/apps?{urllib.parse.urlencode({'user': u.name})}",
                       class_="active" if u.name == user else None, title="свой список" if own else "общий список"))
    return t("nav", links, class_="seg", aria_label="Чей список")


def apps_page(app: "App", req: "Request") -> "Response":
    csrf = req.session.csrf if req.session else ""
    user, err = _user_arg(req.query.get("user"))
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError as e:
        return app.error(req, 500, "Список приложений не читается", str(e))
    reg = users.list_users()
    parts: list[Any] = [page_head("Приложения через VPN", "остальное — напрямую (банки, Госуслуги, MAX)",
                                  _who_nav(al, reg, user))]
    if err:
        parts.append(alert_list([("bad", err)]))
    if not al.exists:
        parts.append(alert_list([("info", f"{al.path.name} ещё нет: действует пресет.")]))
    if user:
        own = al.own(user)
        reset = post_button("/apps", "Вернуть общий", csrf, "btn small", {"action": "reset", "user": user},
                            title="Удалить свой список: пользователь снова на общем",
                            confirm=f"Вернуть {user} общий список? Его свой список будет удалён.") if own else None
        parts.append(t("div", badge("свой список", "info") if own else badge("общий список", "muted"), reset,
                       t("a", f"Ссылки и QR {user} →", href=f"/users/{user}"), class_="actions"))
    parts.append(t("div", *[_platform_card(al, p, user, csrf) for p in allowlist.PLATFORMS], class_="cols"))
    if not user and al.users:
        own_rows = [[t("a", n, href=f"/apps?{urllib.parse.urlencode({'user': n})}"),
                     "; ".join(f"{p}: {', '.join(v)}" for p, v in o.items())] for n, o in sorted(al.users.items())]
        parts.append(card("Свои списки пользователей", table(["пользователь", "список"], own_rows, stack=True),
                          extra=post_button("/apps", "Сбросить к пресету", csrf, "btn small", {"action": "reset"},
                                            title="Общий список снова как в пресете",
                                            confirm="Сбросить общий список к пресету? Свой список придётся собирать заново.")))
    elif not user:
        parts.append(t("div", post_button("/apps", "Сбросить к пресету", csrf, "btn small", {"action": "reset"},
                                          title="Общий список снова как в пресете",
                                          confirm="Сбросить общий список к пресету?"), class_="actions"))
    return app.render(req, "Приложения через VPN", parts, active="/apps")


def apps_post(app: "App", req: "Request") -> "Response":
    action = req.form.get("action", "")
    user, err = _user_arg(req.form.get("user"))
    if err:
        req.session.flash("bad", err)
        return _back(None)
    platform = req.form.get("platform") or None
    if platform not in (None, *allowlist.PLATFORMS):
        req.session.flash("bad", "Неизвестная платформа")
        return _back(user)
    ident = (req.form.get("custom") or "").strip() or (req.form.get("app") or "").strip()
    try:
        if action in ("add", "del"):
            if not ident:
                req.session.flash("bad", "Выберите приложение из каталога или впишите id")
                return _back(user)
            ch = allowlist.change(action, [ident], user, platform)
        elif action == "reset":
            ch = allowlist.reset(user)
        else:
            req.session.flash("bad", "Неизвестное действие")
            return _back(user)
    except (allowlist.AllowlistError, users.UserError, LockTimeout) as e:
        req.session.flash("bad", str(e))
        return _back(user)
    except protolib.ProtoError as e:
        req.session.flash("bad", f"{e} {e.short()}")
        return _back(user)
    app.invalidate_links()  # клиентские файлы пересобраны: старые ссылки и QR не годятся
    who = user or "общий список"
    done = [f"+{i}" for _, i in ch.added] + [f"−{i}" for _, i in ch.removed]
    req.session.flash("ok", f"{who}: {ch.message}" + (f" ({', '.join(done)})" if done else ""))
    awg = str(ch.applied.get("amneziawg", ""))
    if awg.startswith("ошибка"):
        req.session.flash("bad", f"AmneziaWG: {awg}. Повторить: sudo zoo allow apply")
    elif ch.applied:
        req.session.flash("warn", "Разошлите новый QR / файл v2rayN")
    return _back(user)
