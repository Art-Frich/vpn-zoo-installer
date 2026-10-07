"""Дистрибутивы клиентов для групп «приложения ставит ИТ»: блок «Скачать дистрибутивы» (страница группы и
последний шаг мастера), скачивание файла залогиненным администратором и заявка «Обновить».
Админка сеть не трогает: файлы кладёт zoo clients --fetch-dist (zoolib/dist.py), здесь только чтение каталога."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .. import clients, dist, groups, output
from .html import Markup, card, csrf_input, t, table
from .views import ago

if TYPE_CHECKING:
    from .app import App, Request, Response

IOS_LINE = "iPhone: приложение ставится только из App Store."
IOS_DETAILS = ("Мимо App Store приложение на iPhone не поставить. Варианты: приложения из App Store вашей страны; "
               "корпоративный Apple ID другой страны, под которым ставится всё нужное; Apple Business Manager и MDM "
               "(рассылка приложений сотрудникам) — условия и доступность в вашей стране уточните у Apple, мы это не "
               "проверяли. Файла-дистрибутива для iPhone нет.")
BACK_RE = re.compile(r"/groups/[a-z0-9][a-z0-9_-]{0,31}|/connect/done\?[A-Za-z0-9=&%,._-]{0,400}")


def ios_note() -> Markup:
    """Одна строка про iPhone и подробности под спойлером."""
    return t("div", t("span", IOS_LINE, class_="hint"), " ",
             t("details", t("summary", "варианты"), t("p", IOS_DETAILS, class_="hint"), class_="more inline"),
             class_="ios-note")


def _links(links: list[dict[str, Any]]) -> Markup:
    return t("div", [t("a", clients.LINK_KINDS[ln["kind"]], href=ln["url"], target="_blank",
                       rel="noopener noreferrer", class_="chip info") for ln in links], class_="chips")


def _row(r: dict[str, Any], cat: clients.Catalog) -> list[Any]:
    c, plat, f = r["client"], r["platform"], r["file"]
    head = [t("strong", c["name"]), t("div", cat.platforms.get(plat, plat), class_="muted small")]
    if f:
        sha = t("code", f["sha256"], class_="sha",
                title="sha256 указан GitHub и совпал" if f.get("verified") else "sha256 посчитан при скачивании")
        return [head, t("span", f["version"], class_="mono"), t("a", f["name"], href=f["path"], class_="dl-name"),
                output.human_bytes(f["size"]), [sha, " ", t("span", "GitHub ✓" if f.get("verified") else "у нас", class_="muted small")],
                t("a", "Скачать", href=f["path"], class_="btn small primary")]
    if r["store"]:
        why: Any = t("span", "ставится из магазина", class_="muted")
    elif r["error"]:
        why = t("span", "не скачан: " + r["error"], class_="muted small")
    else:
        why = t("span", "ещё не скачан — «Обновить»", class_="muted")
    return [head, "—", [why, _links(r["links"])], "—", "—", ""]


def card_for(gs: groups.Groups, group_id: str | None, back: str, csrf: str) -> Markup | None:
    """Блок «Скачать дистрибутивы» по группе (или по всем группам «ставит ИТ»); None — таких групп нет."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return None
    rows = dist.listing(gs, cat, group_id)
    if not rows:
        return None
    checked = dist.status()["checked"]
    ok, why = dist.request_state()
    if ok:
        form: Markup = t("form", csrf_input(csrf), t("input", type="hidden", name="back", value=back),
                         t("button", "Обновить", type="submit", class_="btn small",
                           title="Скачать свежие версии с GitHub (сервер сам, через несколько минут)"),
                         method="post", action="/dist/refresh", class_="inline", data_swap=True)
    else:
        form = t("button", "Обновить", type="button", class_="btn small", disabled=True, title=why)
    ios = any(r["platform"] == "ios" for r in rows)
    state = t("span", f"скачано · {ago(checked)}" if checked else "ещё не скачивалось", class_="muted small")
    body = [t("div", state, form, class_="actions"),
            table(["приложение", "версия", "файл", "размер", "sha256", ""], [_row(r, cat) for r in rows], stack=True,
                  empty="нет файлов"),
            ios_note() if ios else None,
            t("p", "Файлы лежат на сервере и скачиваются через туннель; сверьте sha256 перед раздачей.", class_="hint")]
    return card("Скачать дистрибутивы", *body, id_="dist",
                help="Сервер скачивает последние релизы клиентов с GitHub раз в сутки и по кнопке; хранит две последние "
                     "версии, всего не больше 300 МБ. Нет подходящего файла — ссылка на страницу клиента.")


def download(app: "App", req: "Request", cid: str, ver: str, fname: str) -> "Response":
    from .app import Response
    f = dist.file_path(cid, ver, fname)
    if f is None:
        return app.error(req, 404, "Нет файла", "Такого дистрибутива нет.")
    return Response(200, b"", "application/octet-stream", file=f,
                    headers=[("Content-Disposition", f'attachment; filename="{fname}"')])


def refresh(app: "App", req: "Request") -> "Response":
    from .app import redirect
    ok, msg = dist.request_fetch()
    req.session.flash("ok" if ok else "warn", msg[:1].upper() + msg[1:])
    back = req.form.get("back", "")
    return redirect(back if BACK_RE.fullmatch(back) else "/groups")
