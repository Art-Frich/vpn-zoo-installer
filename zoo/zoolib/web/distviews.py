"""Дистрибутивы приложений для групп «ставит ИТ»: блок «Дистрибутивы» (страница группы и последний шаг мастера) с
памяткой «Как установить (для ИТ)», скачивание файла залогиненным администратором и заявка «Скачать на сервер» / «Обновить».
Админка сеть не трогает: файлы кладёт zoo clients --fetch-dist (zoolib/dist.py), здесь только чтение каталога."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .. import clients, dist, groups, output
from . import clientviews
from .html import Markup, card, csrf_input, t, table
from .views import ago

if TYPE_CHECKING:
    from .app import App, Request, Response

IOS_LINE = "iPhone: приложение ставится только из App Store."
IOS_DETAILS = ("Мимо App Store приложение на iPhone не поставить. Варианты: приложения из App Store вашей страны; "
               "корпоративный Apple ID другой страны, под которым ставится всё нужное; Apple Business Manager и MDM "
               "(рассылка приложений сотрудникам) — условия и доступность в вашей стране уточните у Apple, мы это не "
               "проверяли. Файла-дистрибутива для iPhone нет.")
# в /connect/done — имена всей команды (до 2 × 200 по 32 знака): короткий потолок уводил большую команду на /groups
BACK_RE = re.compile(r"/groups/[a-z0-9][a-z0-9_-]{0,31}|/connect/done\?[A-Za-z0-9=&%,._-]{0,16000}")


def ios_note() -> Markup:
    """Одна строка про iPhone и подробности под спойлером."""
    return t("div", t("span", IOS_LINE, class_="hint"), " ",
             t("details", t("summary", "варианты"), t("p", IOS_DETAILS, class_="hint"), class_="more inline"),
             class_="ios-note")


def _links(links: list[dict[str, Any]]) -> Markup:
    return t("div", [t("a", clients.LINK_KINDS[ln["kind"]], href=ln["url"], target="_blank",
                       rel="noopener noreferrer", class_="chip info") for ln in links], class_="chips")


def _head(r: dict[str, Any], cat: clients.Catalog) -> list[Any]:
    return [t("strong", r["client"]["name"]), t("div", cat.platforms.get(r["platform"], r["platform"]), class_="muted small")]


def _row(r: dict[str, Any], cat: clients.Catalog) -> list[Any]:
    """Строка таблицы, когда хоть что-то скачано: приложение, версия, файл (sha256 — в подсказке имени), размер."""
    f = r["file"]
    head = _head(r, cat)
    if f:
        sha = (f"sha256 {f['sha256']} — " + ("указан GitHub и совпал" if f.get("verified") else "посчитан при скачивании"))
        return [head, t("span", f["version"], class_="mono"),
                t("a", f["name"], href=f["path"], class_="dl-name", title=sha),
                output.human_bytes(f["size"]), t("a", "Скачать", href=f["path"], class_="btn small primary")]
    return [head, "—", [_why(r), _links(r["links"])], "—", ""]


def _why(r: dict[str, Any]) -> Any:
    if r["store"]:
        return t("span", "ставится из магазина", class_="muted")
    if r["error"]:
        return t("span", "не скачан: " + r["error"], class_="muted small")
    return t("span", "Файла пока нет", class_="muted")


def _list(rows: list[dict[str, Any]], cat: clients.Catalog) -> Markup:
    """Пока ничего не скачано: приложение, где его взять и «Файла пока нет» — без пустых столбцов версии, размера, sha256."""
    return t("ul", [t("li", t("strong", r["client"]["name"]), " ", t("span", cat.platforms.get(r["platform"], r["platform"]),
                                                                       class_="muted small"),
                      " ", _why(r), _links(r["links"]), class_="dist-item") for r in rows], class_="dist-list")


def _how(r: dict[str, Any], cat: clients.Catalog) -> str:
    c, plat = r["client"], r["platform"]
    where = cat.platforms.get(plat, plat)
    if plat == "ios":
        how = "из App Store" + (" — в российском его нет, нужен аккаунт другой страны" if cat.no_ru_store(c, plat) else "")
    elif r["store"] or not any(ln["kind"] == "github" for ln in r["links"]):
        how = "из магазина (ссылка в таблице)"
    else:
        how = ("скачайте " + clientviews.GITHUB_FILE.get(plat, "установщик") + " (кнопка «Скачать» или страница GitHub), "
               "передайте на устройство и откройте" + ("; разрешите установку из неизвестных источников"
                                                       if plat == "android" else ""))
    return f"{where} — «{c['name']}»: {how}."


def memo(rows: list[dict[str, Any]], cat: clients.Catalog) -> Markup:
    """«Как установить (для ИТ)»: шаги установки по приложениям — людям в инструкцию они не попадают."""
    return t("details", t("summary", "Как установить (для ИТ)"), t("ol", [t("li", _how(r, cat)) for r in rows], class_="hint"),
             class_="more")


def card_for(gs: groups.Groups, group_id: str | None, back: str, csrf: str) -> Markup | None:
    """Блок «Дистрибутивы» по группе (или по всем группам «ставит ИТ»); None — таких групп нет."""
    try:
        cat = clients.load()
    except clients.ClientsError:
        return None
    rows = dist.listing(gs, cat, group_id)
    if not rows:
        return None
    have = any(r["file"] for r in rows)
    label = "Обновить" if have else "Скачать на сервер"
    checked = dist.status()["checked"]
    ok, why = dist.request_state()
    if ok:
        form: Markup = t("form", csrf_input(csrf), t("input", type="hidden", name="back", value=back),
                         t("button", label, type="submit", class_="btn small" + ("" if have else " primary"),
                           title="Скачать свежие версии с GitHub (сервер сам, через несколько минут)"),
                         method="post", action="/dist/refresh", class_="inline", data_swap=True)
    else:
        form = t("button", label, type="button", class_="btn small", disabled=True, title=why)
    ios = any(r["platform"] == "ios" for r in rows)
    state = t("span", f"скачано · {ago(checked)}" if checked else "ещё не скачивалось", class_="muted small")
    body = [t("div", state, form, class_="actions"),
            table(["приложение", "версия", "файл", "размер", ""], [_row(r, cat) for r in rows], stack=True,
                  empty="нет файлов") if have else _list(rows, cat),
            memo(rows, cat),
            ios_note() if ios else None,
            t("p", "Файлы лежат на сервере и скачиваются через туннель; sha256 — в подсказке имени файла, сверьте его "
                   "перед раздачей." if have else "Файлы появятся на сервере через несколько минут после кнопки.",
              class_="hint")]
    return card("Дистрибутивы", *body, id_="dist",
                help="Сервер скачивает последние релизы приложений с GitHub раз в сутки и по кнопке; хранит две последние "
                     "версии, всего не больше 300 МБ. Нет подходящего файла — ссылка на страницу приложения.")


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
