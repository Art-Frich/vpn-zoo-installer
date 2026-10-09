"""Разделы docs/USER-GUIDE.md из каталога клиентов (zoo/data/clients.json): шаги по платформам и «что прислать».

Те же шаги, что в инструкциях админки (clientviews.build_pack): импорт, выбор приложений, настройка, проверка. Блоки
между метками `<!-- zoo docs: ИМЯ -->` и `<!-- /zoo docs: ИМЯ -->` пересобирает `zoo docs --user-guide`; тест сверяет
файл в репозитории с выводом генератора, поэтому руками их не правят.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from . import clients, output, paths

GUIDE_FILE = paths.ZOO_PKG_ROOT.parent / "docs" / "USER-GUIDE.md"
SENT_RE = re.compile(r",? (?:который|которую) я пришлю")
APPS_WORD = "приложения из списка администратора"
DESKTOP = ("windows", "macos", "linux")
BOTH = ("vless-xhttp", "vless-reality")   # основной и запасной ключ VLESS — в одном приложении
# список у каждого свой (у кого-то только Telegram): проверка — без привязки к Brave
GUIDE_CHECK = "Проверьте: приложение из вашего списка (например, Brave или Telegram) загружает новое — значит, VPN работает."


class GuideError(Exception):
    pass


def _neutral(step: str) -> str:
    """Шаги каталога написаны для сообщения («ссылку из сообщения»); в общем документе — «из сообщения администратора»."""
    step = step.replace("{login}-", "ваше-имя-")   # файл правил приходит под именем человека: «ivan-v2rayn-routing.json»
    return SENT_RE.sub(" от администратора", step.replace("из сообщения", "из сообщения администратора"))


def _imports(c: dict[str, Any], plat: str) -> tuple[list[tuple[str, list[str]]], list[str] | None]:
    """Как в админке (clientviews.pick_method): ссылка или файл из сообщения; QR — с другого экрана, только телефонам."""
    main = next(m for m in ("link", "file", "qr") if m in c["import"])
    alt = [] if plat not in DESKTOP and main != "qr" and "qr" in c["import"] else None
    return [(main, [])], alt


def _where(c: dict[str, Any], plat: str) -> str:
    links = sorted(c["platforms"][plat], key=lambda ln: not ln["checked"])
    return " · ".join(f"[{clients.LINK_KINDS[ln['kind']]}]({ln['url']})" + ("" if ln["checked"] else " (ссылка не проверена)")
                      for ln in links)


def _via(cat: clients.Catalog, mode: str) -> str:
    if mode == "apps":
        return "Через VPN — только приложения из списка, остальное напрямую."
    return f"Через VPN — {cat.raw['via'][mode]}." if mode else ""


def _variant(cat: clients.Catalog, plat: str, n: int, cid: str) -> list[str]:
    c = cat.client(cid)
    assert c is not None
    mode = cat.via(c, plat)
    out = [f"### Вариант {n}{' (рекомендуем)' if n == 1 else ''}: {c['name']}", ""]
    out += [line for line in (_via(cat, mode), f"Где взять: {_where(c, plat)}.") if line]
    if cat.no_ru_store(c, plat):
        out.append("В российском App Store его нет: нужен Apple ID другой страны, подделки с похожим названием не ставьте.")
    out.append("")
    asset = (c.get("asset") or {}).get(plat)
    install = f"Установите «{c['name']}»" + (f": в «Assets» скачайте {asset}." if asset else ".")
    if note := cat.install_note(c, plat):
        install += f" {note}"
    imports, alt = _imports(c, plat)
    steps = [install] + cat.steps(c, plat, imports, APPS_WORD, alt_qr=alt)
    if all(cat.status(c, p, plat) in ("ok", "warn") for p in BOTH):   # VLESS-приложение: ключей обычно два
        steps[1] += " " + cat.raw["both_guide"]
    if mode == "apps" and plat in cat.raw.get("rules", {}):
        steps.append(cat.raw["rules"][plat])
    steps.append(GUIDE_CHECK if mode == "apps" else cat.check(mode, True))
    out += [f"{i}. {_neutral(s)}" for i, s in enumerate(steps, 1)]
    return out + [""]


def _unfit(cat: clients.Catalog, plat: str) -> str:
    """«Не подходят: …» — приложения платформы, у которых наш основной VLESS «не работает»."""
    bad = [c["name"] for c in cat.clients if plat in c["platforms"]
           and c["protocols"].get("vless-reality", {}).get("s") == "no"]
    if not bad:
        return ""
    they = "они показывают" if len(bad) > 1 else "оно показывает"
    return (f"**Не подходят:** {', '.join(bad)} — с нашими VLESS {they} «подключено», но интернета нет.")


def platforms(cat: clients.Catalog) -> str:
    out: list[str] = []
    for plat, g in cat.raw.get("guide", {}).items():
        out += [f"## Шаг 2. {cat.platforms[plat]}", ""]
        if g.get("note"):
            out += [g["note"], ""]
        for n, cid in enumerate(g["apps"], 1):
            out += _variant(cat, plat, n, cid)
        if unfit := _unfit(cat, plat):
            out += [unfit, ""]
        tips = g.get("tips", [])
        if tips:
            out += ["**Полезно знать:**", "", *[f"- {x}" for x in tips], ""]
        out += ["---", ""]
    return "\n".join(out).rstrip("\n-").rstrip() + "\n"


def report(cat: clients.Catalog) -> str:
    return cat.report("guide") + "\n"


BLOCKS = {"platforms": platforms, "report": report}


def update(text: str, cat: clients.Catalog) -> str:
    """Текст документа с пересобранными блоками; метки остаются на месте. Нет метки — GuideError."""
    for name, make in BLOCKS.items():
        begin, end = f"<!-- zoo docs: {name} -->", f"<!-- /zoo docs: {name} -->"
        i, j = text.find(begin), text.find(end)
        if i < 0 or j < i:
            raise GuideError(f"в документе нет меток блока {name}: {begin} … {end}")
        text = text[:i + len(begin)] + "\n\n" + make(cat) + "\n" + text[j:]
    return text


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user-guide", action="store_true", help="пересобрать разделы docs/USER-GUIDE.md из каталога клиентов")
    p.add_argument("--file", type=Path, default=None, help="другой файл вместо docs/USER-GUIDE.md")
    p.add_argument("--check", action="store_true", help="только сверить: код 1, если файл отстал от каталога")


def cmd_docs(args: argparse.Namespace, cfg: Any) -> int:
    if not args.user_guide:
        output.error("укажите, что собрать: --user-guide")
        return 2
    path = args.file or GUIDE_FILE
    try:
        text = path.read_text(encoding="utf-8")
        new = update(text, clients.load())
    except (OSError, GuideError, clients.ClientsError) as e:
        output.error(str(e))
        return 1
    if args.check:
        (output.ok if new == text else output.warn)(f"{path.name}: " + ("совпадает с каталогом" if new == text
                                                                         else "отстал — zoo docs --user-guide"))
        return 0 if new == text else 1
    if new != text:
        path.write_text(new, encoding="utf-8", newline="\n")
    output.ok(f"{path.name}: " + ("обновлён" if new != text else "без изменений"))
    return 0
