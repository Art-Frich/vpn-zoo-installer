"""Сборка HTML без шаблонизатора: всё, что не Markup, экранируется.

    t("a", "текст", href="/x", class_="btn")  →  <a href="/x" class="btn">текст</a>

class_ → class, data_x → data-x; True — атрибут без значения, None/False — нет атрибута.
Дети: строки, Markup, числа, списки (разворачиваются), None (пропускается).
"""

from __future__ import annotations

import html
from typing import Any, Iterable

VOID = {"br", "hr", "img", "input", "meta", "link"}


class Markup(str):
    """Готовая разметка: не экранируется повторно. Сложение с обычной строкой экранирует её
    (иначе str.__add__ вернул бы str, и разметка экранировалась бы дальше как текст)."""

    def __add__(self, other: Any) -> "Markup":
        return Markup(str.__add__(self, esc(other)))

    def __radd__(self, other: Any) -> "Markup":
        return Markup(str.__add__(esc(other), self))


def esc(value: Any) -> Markup:
    if isinstance(value, Markup):
        return value
    return Markup(html.escape("" if value is None else str(value), quote=True))


def _flatten(children: Iterable[Any]) -> Iterable[Any]:
    for c in children:
        if c is None or c is False:
            continue
        if isinstance(c, (list, tuple)):
            yield from _flatten(c)
        else:
            yield c


def attrs(**kw: Any) -> str:
    parts = []
    for k, v in kw.items():
        if v is None or v is False:
            continue
        name = k.rstrip("_").replace("_", "-")
        parts.append(f" {name}" if v is True else f' {name}="{esc(v)}"')
    return "".join(parts)


def t(tag: str, *children: Any, **kw: Any) -> Markup:
    open_ = f"<{tag}{attrs(**kw)}>"
    if tag in VOID:
        return Markup(open_)
    return Markup(open_ + "".join(esc(c) for c in _flatten(children)) + f"</{tag}>")


def join(*parts: Any) -> Markup:
    return Markup("".join(esc(p) for p in _flatten(parts)))


# ---------- частые элементы ----------

def badge(text: str, kind: str = "muted") -> Markup:
    """kind: ok | warn | bad | muted | info"""
    return t("span", text, class_=f"badge {kind}")


def state_badge(ok: bool | None, yes: str = "работает", no: str = "сбой", unknown: str = "—") -> Markup:
    if ok is None:
        return badge(unknown, "muted")
    return badge(yes, "ok") if ok else badge(no, "bad")


def _hdr(h: Any) -> tuple[Any, str | None]:
    """Заголовок колонки: «текст» или («текст», «подсказка в title»)."""
    return (h[0], h[1]) if isinstance(h, tuple) else (h, None)


def empty(text: Any, hint: Any = None) -> Markup:
    """Единое пустое состояние: строка и, если нужно, подсказка или кнопка под ней."""
    return t("div", t("p", text), t("div", hint, class_="hint") if hint else None, class_="empty-state")


_empty = empty  # в table() параметр empty заслоняет функцию


def table(headers: list[Any], rows: list[list[Any]], num: Iterable[int] = (), cls: str = "",
          empty: Any = "пусто", stack: bool = False, row_cls: list[str | None] | None = None) -> Markup:
    """Таблица в прокручиваемой обёртке; num — индексы числовых колонок (вправо).
    Заголовок — текст или (текст, title). stack=True: на телефоне строки складываются в карточки
    (подписи берутся из заголовков, data-label). Без строк вместо таблицы — пустое состояние."""
    if not rows:
        return _empty(empty)
    num = set(num)
    hs = [_hdr(h) for h in headers]
    head = t("tr", [t("th", label, class_="num" if i in num else None, scope="col", title=title)
                    for i, (label, title) in enumerate(hs)])
    labels = [(str(label) if isinstance(label, str) else "") for label, _ in hs]
    body = [t("tr", [t("td", c, class_="num" if i in num else None,
                       data_label=labels[i] if stack and i < len(labels) else None)
                     for i, c in enumerate(r)], class_=(row_cls[n] if row_cls else None))
            for n, r in enumerate(rows)]
    klass = " ".join(x for x in (cls, "stack" if stack else "") if x)
    return t("div", t("table", t("thead", head), t("tbody", body), class_=klass or None), class_="table-wrap")


def csrf_input(token: str) -> Markup:
    return t("input", type="hidden", name="csrf", value=token)


def post_button(action: str, label: str, csrf: str, cls: str = "btn", fields: dict[str, str] | None = None,
                title: str | None = None, confirm: str | None = None, swap: bool = True) -> Markup:
    """Кнопка-форма POST (без JS — обычная форма). confirm — вопрос окна подтверждения (data-confirm);
    swap — с JS отправить без перезагрузки и подменить <main> (data-swap)."""
    hidden = [t("input", type="hidden", name=k, value=v) for k, v in (fields or {}).items()]
    return t("form", csrf_input(csrf), hidden, t("button", label, type="submit", class_=cls, title=title),
             method="post", action=action, class_="inline", data_confirm=confirm, data_swap=swap or None)


def card(title: Any, *body: Any, cls: str = "", extra: Any = None, help: Any = None, tip: str | None = None,
         id_: str | None = None) -> Markup:
    """help — пояснение в свёрнутом «?» у заголовка: на карточке видна одна строка, остальное тут.
    tip — подсказка заголовка (title=), id_ — якорь для ссылок."""
    tail = [extra, t("details", t("summary", "?", aria_label="Пояснение"), t("div", help, class_="hint"),
                     class_="help") if help else None]
    head = t("div", t("h3", title, title=tip), t("div", tail, class_="head-end") if extra or help else None,
             class_="card-head")
    return t("section", head, *body, class_=f"card {cls}".strip(), id=id_)


def kv(rows: list[tuple[Any, Any]]) -> Markup:
    return t("dl", [[t("dt", k), t("dd", v)] for k, v in rows], class_="kv")
