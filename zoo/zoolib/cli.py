"""CLI zoo: разбор аргументов и команды status, user, links, version, setup.

Команды traffic/journal/probe/export-probe/web/upgrade/smoke живут в своих модулях: модуль даёт
add_arguments(parser) и cmd_<name>(args, cfg) -> int; cli.py их только подключает.
Коды выхода: 0 — успех, 1 — ошибка или найдены проблемы, 2 — неверные аргументы.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable

from . import MIN_PYTHON, __version__, allowlist, manifests, output, paths, protolib, qr, status, system, users
from . import journal, protoctl, traffic, upgrade
from . import probe as probe_mod
from . import web as web_mod
from .config import Config, ConfigError
from .config import load as load_config
from .fsutil import LockTimeout

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


# ---------- status ----------

def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    data = status.collect(cfg, with_xui=not args.no_api)
    output.emit(data, args.json, _render_status)
    return EXIT_FAIL if data["problems"] else EXIT_OK


def _render_status(d: dict[str, Any]) -> None:
    srv, host = d["server"], d["host"]
    print(f"{srv['label'] or 'vpn'}  {srv['ip'] or '?'}  ({srv['hostname']})   zoo {d['zoo']['version']}")
    mem, disk, load = host.get("mem") or {}, host.get("disk") or {}, host.get("load")
    print(f"CPU {host.get('cpu_percent', '—')}% ×{host.get('cpu_count')}   "
          f"load {' '.join(f'{x:.2f}' for x in load) if load else '—'}   "
          f"RAM {output.human_bytes(mem.get('used'))}/{output.human_bytes(mem.get('total'))}   "
          f"диск {output.human_bytes(disk.get('used'))}/{output.human_bytes(disk.get('total'))}   "
          f"аптайм {output.human_duration(host.get('uptime'))}")
    print()
    if d["protocols"]:
        rows = []
        for p in d["protocols"]:
            listen = " ".join(f"{k}:{'да' if v else 'НЕТ'}" for k, v in p["listening"].items())
            svc = ", ".join(f"{u} {s}" for u, s in p["services"].items()) or "—"
            rows.append([p["id"], f"{p['port']}/{p['layer']}", svc, listen,
                         "" if p["users"] is None else p["users"],
                         output.mark(p["ok"]) if p["enabled"] else "выкл"])
        print(output.table(rows, ["протокол", "порт", "сервис", "слушает", "польз.", "итог"], right=(4,)))
    else:
        print("Манифестов протоколов нет (фазы 04–06 не выполнены?)")
    print()
    rows = []
    for unit, st in d["services"].items():
        if st.get("load") == "not-found":
            continue
        rows.append([unit, st.get("active"), st.get("sub"), st.get("enabled")])
    if rows:
        print(output.table(rows, ["юнит", "состояние", "подробно", "автозапуск"]))
    fw = d["firewall"]["ufw_active"]
    print(f"\nUFW: {'не установлен' if fw is None else ('включён' if fw else 'ВЫКЛЮЧЕН')}")
    x = d["xray"]
    if x:
        print(f"\nXray: {x.get('state') or '—'} {x.get('version') or ''} "
              f"{('панель ' + x['panel']) if x.get('panel') else ''} {x.get('error') or ''}".rstrip())
    if d["certs"]:
        print("\nСертификаты:")
        for c in d["certs"]:
            left = "не читается" if c["days_left"] is None else f"осталось {c['days_left']} дн."
            print(f"  {c['path']}: {left}")
    u = d["users"]
    print(f"\nПользователи: {u['total']} (включено {u['enabled']})"
          + ("" if u.get("registry") else " — реестр users.json не создан"))
    inst = {k: v for k, v in d["versions"]["installed"].items() if v}
    print("Версии: " + ", ".join(f"{k} {v}" for k, v in inst.items()))
    if d["problems"]:
        print()
        for p in d["problems"]:
            output.warn(p)
    else:
        print()
        output.ok("проблем не найдено")


# ---------- user ----------

ACTION_DONE = {"add": "добавлен", "adopt": "учтён", "del": "удалён", "enable": "включён",
               "disable": "отключён", "rollback": "откат", "forget": "забыт"}
ACTION_NOUN = {"add": "добавление", "adopt": "учёт", "del": "удаление", "enable": "включение",
               "disable": "отключение", "rollback": "откат"}


def _report(rep: users.OpReport, as_json: bool) -> int:
    if as_json:
        output.print_json(rep.to_dict())
    else:
        for s in rep.steps:
            if s.ok:
                text = f"{s.proto_id}: {ACTION_DONE.get(s.action, s.action)}" + (f" ({s.error})" if s.error else "")
                output.ok(text)
            else:
                output.error(f"{s.proto_id}: ошибка ({ACTION_NOUN.get(s.action, s.action)}) — {s.error}")
        for pid, why in rep.skipped.items():
            output.info(f"{pid}: пропущен ({why})")
        (output.ok if rep.ok else output.error)(f"{rep.user}: {rep.message}")
    return EXIT_OK if rep.ok else EXIT_FAIL


def cmd_user_add(args: argparse.Namespace, cfg: Config) -> int:
    rep = users.add_user(args.name, note=args.note or "", only=args.proto, partial=args.partial)
    code = _report(rep, args.json)
    if rep.ok and not args.json:
        output.info(f"ссылки: zoo links {args.name} --qr")
    return code


def cmd_user_del(args: argparse.Namespace, cfg: Config) -> int:
    return _report(users.delete_user(args.name, force=args.force), args.json)


def cmd_user_enable(args: argparse.Namespace, cfg: Config) -> int:
    return _report(users.set_enabled(args.name, True, partial=args.partial), args.json)


def cmd_user_disable(args: argparse.Namespace, cfg: Config) -> int:
    if args.name == users.OWNER and not args.json:
        output.warn("owner отключается: ссылки по умолчанию перестанут работать")
    return _report(users.set_enabled(args.name, False, partial=args.partial), args.json)


def cmd_user_sync(args: argparse.Namespace, cfg: Config) -> int:
    reports = users.sync_users(args.names or None)
    if args.json:
        output.print_json([r.to_dict() for r in reports])
        return EXIT_OK if all(r.ok for r in reports) else EXIT_FAIL
    code = EXIT_OK
    for r in reports:
        code = max(code, _report(r, False))
    return code


def cmd_user_list(args: argparse.Namespace, cfg: Config) -> int:
    reg = users.list_users()
    drift = users.verify() if args.verify else None
    shown = reg.users if args.all else reg.visible()
    data: dict[str, Any] = {"registry": str(reg.path), "exists": reg.exists,
                            "users": [u.to_dict() for u in shown]}
    if drift is not None:
        data["verify"] = drift
    if args.json:
        output.print_json(data)
    else:
        if not reg.exists:
            output.warn(f"реестра {reg.path} нет — он создаётся фазой 09 (или: zoo setup)")
        rows = [[u.name, "да" if u.enabled else "нет", ", ".join(u.protocols) or "—",
                 u.created[:10], u.note] for u in shown]
        print(output.table(rows, ["имя", "вкл", "протоколы", "создан", "заметка"]))
        hidden = len(reg.users) - len(shown)
        if hidden:
            print(output.color(f"служебных скрыто: {hidden} (--all — показать)", "dim"))
        for pid, d in (drift or {}).items():
            if d["error"]:
                output.warn(f"{pid}: не удалось сверить — {d['error']}")
            for n in d["missing"]:
                output.warn(f"{pid}: {n} есть в реестре, но нет в протоколе (zoo user sync)")
            for n in d["extra"]:
                output.warn(f"{pid}: {n} есть в протоколе, но нет в реестре")
    bad = drift and any(d["missing"] or d["extra"] or d["error"] for d in drift.values())
    return EXIT_FAIL if bad else EXIT_OK


def cmd_user_show(args: argparse.Namespace, cfg: Config) -> int:
    reg = users.list_users()
    user = reg.require(args.name)
    links, errors = users.user_links(args.name)
    data = {**user.to_dict(), "links": [x.to_dict() for x in links], "errors": errors}
    if args.json:
        output.print_json(data)
        return EXIT_OK
    print(f"{user.name}  {'включён' if user.enabled else 'ОТКЛЮЧЁН'}  создан {user.created}")
    if user.note:
        print(f"заметка: {user.note}")
    print(f"протоколы: {', '.join(user.protocols) or '—'}")
    _render_links(links, errors, qr_mode=None)
    return EXIT_OK


# ---------- links ----------

def cmd_links(args: argparse.Namespace, cfg: Config) -> int:
    name = args.name or users.OWNER
    reg = users.list_users()
    if reg.exists:
        reg.require(name)
    links, errors = users.user_links(name, args.proto)
    if args.svg_dir:
        _write_svgs(name, links, Path(args.svg_dir))
    if args.json:
        output.print_json({"user": name, "links": [x.to_dict() for x in links], "errors": errors})
    else:
        mode = ("inv" if args.invert else "std") if args.qr else None
        _render_links(links, errors, qr_mode=mode)
    return EXIT_FAIL if errors and not links else EXIT_OK


def qr_payload(link: protolib.Link) -> str | None:
    """Что кодировать в QR: ссылку или содержимое клиентского .conf (импорт в AmneziaWG/WG)."""
    if link.kind == "uri":
        return link.uri
    if link.uri.endswith(".conf"):
        try:
            return Path(link.uri).read_text(encoding="utf-8")
        except OSError as e:
            output.warn(f"{link.uri}: {e}")
    return None


def _render_links(links: list[protolib.Link], errors: dict[str, str], qr_mode: str | None) -> None:
    by_id = {m.id: m for m in manifests.load_all()[0]}
    current = None
    for link in links:
        if link.proto_id != current:
            current = link.proto_id
            m = by_id.get(current)
            name = m.name if m else ("Приложения через VPN" if current == allowlist.V2RAYN_PROTO else "")
            print(f"\n{output.color(current, 'bold')}  {name}")
            if m and m.notes:
                width = min(shutil.get_terminal_size((100, 20)).columns, 100)
                print(output.color(textwrap.fill(m.notes, width, initial_indent="  ",
                                                 subsequent_indent="  "), "dim"))
        prefix = link.label + ": " if link.label else ("файл: " if link.kind == "file" else "")
        print(f"  {prefix}{link.uri}")
        payload = qr_payload(link) if qr_mode else None
        if payload:
            _print_qr(payload, qr_mode == "inv")
    for pid, err in errors.items():
        output.warn(f"{pid}: ссылки не получены — {err}")


def _print_qr(text: str, invert: bool) -> None:
    try:
        print(qr.utf8(text, invert=invert))
    except qr.QrError as e:
        output.warn(f"QR: {e}")


def _write_svgs(name: str, links: list[protolib.Link], out_dir: Path) -> None:
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for i, link in enumerate(links, 1):
        payload = qr_payload(link)
        if not payload:
            continue
        target = out_dir / f"{name}-{link.proto_id}-{i}.svg"
        try:
            data = qr.svg(payload)
            # в QR — ключи доступа: файл сразу 0600, без окна с правами по umask
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.chmod(target, 0o600)
            output.ok(f"QR: {target}")
        except qr.QrError as e:
            output.warn(f"{target.name}: {e}")


# ---------- allow ----------

def _allow_user(args: argparse.Namespace) -> str | None:
    user = getattr(args, "user", None) or None
    allowlist.check_user(user)
    return user


def cmd_allow_list(args: argparse.Namespace, cfg: Config) -> int:
    al = allowlist.Allowlist.load()
    user = _allow_user(args)
    data = {**al.to_dict(), "default": allowlist.defaults(),
            "catalog": [{"key": a.key, "title": a.title, "android": a.android, "windows": a.windows,
                         "note": a.note} for a in allowlist.CATALOG]}
    if user:
        data["effective"] = {"user": user, "own": al.own(user),
                             **{p: al.effective(p, user) for p in allowlist.PLATFORMS}}
    if args.json:
        output.print_json(data)
        return EXIT_OK
    if not al.exists:
        output.warn(f"реестра {al.path} ещё нет — действует пресет (реестр создаёт фаза 09 или первое изменение)")
    if user:
        print(f"{user}: {'свой список' if al.own(user) else 'общий список'}")
    for p in allowlist.PLATFORMS:
        print(f"\n{output.color(allowlist.PLATFORM_TITLE[p], 'bold')}")
        for i in al.effective(p, user):
            title = allowlist.title_of(p, i)
            print(f"  {i}" + (output.color(f"  {title}", "dim") if title else ""))
    if not user and al.users:
        print(f"\n{output.color('Свои списки пользователей', 'bold')}")
        for n, own in sorted(al.users.items()):
            print(f"  {n}: " + "; ".join(f"{p}: {', '.join(v)}" for p, v in own.items()))
    if args.catalog:
        rows = [[a.key, a.title, a.android or "—", a.windows or "—", a.note] for a in allowlist.CATALOG]
        print()
        print(output.table(rows, ["ключ", "приложение", "Android", "Windows", "заметка"]))
    else:
        print(output.color("\nкаталог известных приложений: zoo allow list --catalog", "dim"))
    return EXIT_OK


def _allow_report(ch: allowlist.Change, as_json: bool) -> int:
    ap = ch.applied
    awg_failed = str(ap.get("amneziawg", "")).startswith("ошибка")
    if as_json:
        output.print_json(ch.to_dict())
        return EXIT_FAIL if awg_failed else EXIT_OK
    who = f"{ch.user}: " if ch.user else "общий список: "
    for p, i in ch.added:
        output.ok(f"{who}+ {i} ({p})")
    for p, i in ch.removed:
        output.ok(f"{who}- {i} ({p})")
    for p, i in ch.unchanged:
        output.info(f"{who}{i} ({p}) — без изменений")
    output.info(who + ch.message)
    if ap:
        output.info(f"AmneziaWG: {ap.get('amneziawg')}; правила v2rayN: {len(ap.get('v2rayn', []))} файл(ов)")
        if awg_failed:
            output.warn("Android-конфиги не пересобраны: sudo zoo allow apply")
            return EXIT_FAIL
        output.info("пользователям — новые QR и файлы (zoo links ИМЯ --qr): старый QR работает со старым списком")
    return EXIT_OK


def cmd_allow_add(args: argparse.Namespace, cfg: Config) -> int:
    return _allow_report(allowlist.change("add", args.apps, _allow_user(args), args.platform), args.json)


def cmd_allow_del(args: argparse.Namespace, cfg: Config) -> int:
    return _allow_report(allowlist.change("del", args.apps, _allow_user(args), args.platform), args.json)


def cmd_allow_reset(args: argparse.Namespace, cfg: Config) -> int:
    return _allow_report(allowlist.reset(_allow_user(args)), args.json)


def cmd_allow_apply(args: argparse.Namespace, cfg: Config) -> int:
    allowlist.ensure_file()
    res = allowlist.apply(awg=not args.no_awg)
    if args.json:
        output.print_json(res)
    else:
        output.info(f"AmneziaWG: {res['amneziawg']}; правила v2rayN: {len(res['v2rayn'])} файл(ов)")
        for n, e in res["errors"].items():
            output.warn(f"{n}: {e}")
    return EXIT_FAIL if res["errors"] or str(res["amneziawg"]).startswith("ошибка") else EXIT_OK


# ---------- version / setup ----------

def install_info() -> dict[str, Any]:
    f = paths.zoo_home() / "INSTALL.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def cmd_version(args: argparse.Namespace, cfg: Config) -> int:
    data = {"zoo": __version__, "python": sys.version.split()[0], "home": str(paths.zoo_home()),
            "scripts": str(paths.scripts_dir()), "install": install_info()}
    if args.all:
        data["installed"] = system.component_versions()
        data["pinned"] = system.pinned_versions(paths.scripts_dir() / "versions.env")
    if args.json:
        output.print_json(data)
        return EXIT_OK
    print(f"zoo {__version__} (Python {data['python']})")
    if data["install"]:
        i = data["install"]
        print(f"установлен {i.get('installed', '?')} из {i.get('source', '?')} {i.get('git', '')}".rstrip())
    if args.all:
        for k, v in data["installed"].items():
            print(f"  {k}: {v or '—'}")
        for k, v in data["pinned"].items():
            print(f"  {k} (pin): {v}")
    return EXIT_OK


def cmd_setup(args: argparse.Namespace, cfg: Config) -> int:
    """Служебная: вызывает фаза 09. Реестр с owner, служебный пользователь пробника,
    setup() модулей трафика, пробника и админки."""
    rep = users.bootstrap(users.OWNER)
    # owner не найден в каком-то протоколе — предупреждение, а не провал установки
    _report(rep, args.json)
    try:
        if allowlist.ensure_file():
            output.info(f"приложения через VPN: {paths.allowlist_file()} (пресет; zoo allow list)")
    except allowlist.AllowlistError as e:
        output.warn(f"приложения через VPN: {e}")
    probe_rep = users.ensure_probe_user()
    if probe_rep is not None:
        _report(probe_rep, args.json)
        if not probe_rep.ok:
            output.warn(f"{users.PROBE_USER} заведён не везде: самопроверка этих протоколов пойдёт кредами owner")
    # обновление (zoo upgrade перезапускает только 09): у старых пользователей появятся
    # правила v2rayN и Android-вариант AWG, у всех — файлы нового формата
    try:
        res = allowlist.apply()
        bad = dict(res["errors"])
        if str(res["amneziawg"]).startswith("ошибка"):
            bad["amneziawg"] = res["amneziawg"]
        for n, e in bad.items():
            output.warn(f"приложения через VPN: {n}: {e} (повторить: sudo zoo allow apply)")
    except (allowlist.AllowlistError, LockTimeout, OSError) as e:
        output.warn(f"приложения через VPN: файлы не пересобраны: {e} (sudo zoo allow apply)")
    for mod in (traffic, journal, probe_mod, web_mod):
        mod.setup(cfg)
    return EXIT_OK


# ---------- разбор аргументов ----------

def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="вывод в JSON")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="zoo", description="Управление VPN-зоопарком: статус, пользователи, ссылки, трафик, пробник.",
    )
    parser.add_argument("--json", action="store_true", default=False, help="вывод в JSON")
    sub = parser.add_subparsers(dest="command", metavar="КОМАНДА")

    def add(name: str, handler: Callable, help_: str, **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        p.set_defaults(handler=handler)
        return p

    p = add("status", cmd_status, "сервисы, порты, сертификаты, версии, метрики хоста")
    p.add_argument("--no-api", action="store_true", help="не опрашивать API 3x-ui")

    pu = sub.add_parser("user", parents=[common], help="пользователи во всех протоколах",
                        description="Пользователи во всех протоколах")
    usub = pu.add_subparsers(dest="user_command", metavar="ДЕЙСТВИЕ")
    pu.set_defaults(handler=lambda a, c: (pu.print_help(), EXIT_USAGE)[1])

    def uadd(name: str, handler: Callable, help_: str) -> argparse.ArgumentParser:
        p = usub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(handler=handler)
        return p

    p = uadd("add", cmd_user_add, "добавить пользователя во все включённые протоколы")
    p.add_argument("name")
    p.add_argument("--note", help="заметка (кто это)")
    p.add_argument("--proto", action="append", help="только этот протокол (можно несколько раз)")
    p.add_argument("--partial", action="store_true", help="не откатывать при ошибке в части протоколов")
    p = uadd("del", cmd_user_del, "удалить пользователя из всех протоколов")
    p.add_argument("name")
    p.add_argument("--force", action="store_true",
                   help=f"удалить из реестра даже при ошибках (и owner, {users.PROBE_USER})")
    for verb, handler, help_ in (("disable", cmd_user_disable, "отключить (креды сохраняются)"),
                                 ("enable", cmd_user_enable, "включить обратно")):
        p = uadd(verb, handler, help_)
        p.add_argument("name")
        p.add_argument("--partial", action="store_true", help="не откатывать при ошибке в части протоколов")
    p = uadd("list", cmd_user_list, "список пользователей")
    p.add_argument("--verify", action="store_true", help="сверить реестр с протоколами")
    p.add_argument("--all", action="store_true", help=f"со служебными ({users.PROBE_USER})")
    p = uadd("show", cmd_user_show, "пользователь, его протоколы, ссылки и файлы")
    p.add_argument("name")
    p = uadd("sync", cmd_user_sync, "завести креды в протоколах, включённых после создания пользователя")
    p.add_argument("names", nargs="*", metavar="name")

    p = add("links", cmd_links, "ссылки и QR пользователя (по умолчанию owner)")
    p.add_argument("name", nargs="?")
    p.add_argument("--proto", action="append", help="только этот протокол (можно несколько раз)")
    p.add_argument("--qr", action="store_true", help="QR-коды в терминале")
    p.add_argument("--invert", action="store_true", help="QR для светлого фона терминала")
    p.add_argument("--svg-dir", metavar="DIR", help="сохранить QR в SVG-файлы")

    pa = sub.add_parser("allow", parents=[common], help="приложения через VPN (allowlist)",
                        description="Приложения через VPN: только они идут в туннель (Android — "
                                    "AmneziaWG, Windows — v2rayN), всё остальное мимо")
    asub = pa.add_subparsers(dest="allow_command", metavar="ДЕЙСТВИЕ")
    pa.set_defaults(handler=lambda a, c: (pa.print_help(), EXIT_USAGE)[1])

    def aadd(name: str, handler: Callable, help_: str, user: bool = True) -> argparse.ArgumentParser:
        p = asub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(handler=handler)
        if user:
            p.add_argument("--user", metavar="ИМЯ", help="свой список пользователя вместо общего")
        return p

    p = aadd("list", cmd_allow_list, "текущие списки (общий и свои у пользователей)")
    p.add_argument("--catalog", action="store_true", help="каталог известных приложений с ключами")
    for verb, handler, help_ in (("add", cmd_allow_add, "пустить приложения через VPN"),
                                 ("del", cmd_allow_del, "убрать приложения из VPN")):
        p = aadd(verb, handler, help_)
        p.add_argument("apps", nargs="+", metavar="ПРИЛОЖЕНИЕ",
                       help="ключ каталога (brave, youtube…), пакет Android или процесс Windows (name.exe)")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--android", dest="platform", action="store_const", const="android", help="только Android")
        g.add_argument("--windows", dest="platform", action="store_const", const="windows", help="только Windows")
    aadd("reset", cmd_allow_reset, "общий список — к пресету; с --user — пользователь на общий список")
    p = aadd("apply", cmd_allow_apply, "пересобрать Android-конфиги AmneziaWG и правила v2rayN всех пользователей",
             user=False)
    p.add_argument("--no-awg", action="store_true", help="только правила v2rayN")

    p = add("traffic", traffic.cmd_traffic, "трафик по пользователям и протоколам")
    traffic.add_arguments(p)
    p = add("journal", journal.cmd_journal, "журнал атак: кто и чем пробовал сервер снаружи")
    journal.add_arguments(p)
    p = add("probe", probe_mod.cmd_probe, "проверка протоколов: --local с сервера, --remote с клиента")
    probe_mod.add_arguments(p)
    p = add("live", probe_mod.live.cmd_live, "живые метрики протоколов: run — замер, show — последние")
    probe_mod.live.add_arguments(p)
    p = add("job", protoctl.cmd_job, "заявки админки на вкл/выкл протокола: run — выполнить очередь, list — состояния")
    protoctl.add_arguments(p)
    p = add("export-probe", probe_mod.cmd_export_probe, "пакет для клиентского пробника")
    probe_mod.add_export_arguments(p)
    p = add("history", probe_mod.history.cmd_history, "история проб: add, list, export (для history/ в репо)")
    probe_mod.history.add_arguments(p)
    p = add("web", web_mod.cmd_web, "веб-админка на 127.0.0.1 (доступ через ssh -L)")
    web_mod.add_arguments(p)
    p = add("upgrade", upgrade.cmd_upgrade, "обновление закреплённых версий")
    upgrade.add_arguments(p)
    p = add("smoke", upgrade.cmd_smoke, "смоук-проверка после установки или обновления")
    upgrade.add_smoke_arguments(p)

    p = add("version", cmd_version, "версия zoo и компонентов")
    p.add_argument("--all", action="store_true", help="с версиями компонентов и пинами")
    # служебная, для фазы 09
    p = sub.add_parser("setup", parents=[common])
    p.set_defaults(handler=cmd_setup)
    return parser


NO_CONFIG_COMMANDS = {"version"}


def _need_root(command: str) -> str | None:
    """Сообщение об ошибке, если без root команда не прочитает /etc/vpn-setup."""
    if command in NO_CONFIG_COMMANDS or not hasattr(os, "geteuid") or os.geteuid() == 0:
        return None
    etc = paths.etc()
    if etc.exists() and not os.access(etc, os.R_OK | os.X_OK):
        return f"нет доступа к {etc} — запусти через sudo: sudo zoo {command}"
    return None


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        output.error(f"нужен Python >= {'.'.join(map(str, MIN_PYTHON))}, сейчас {sys.version.split()[0]}")
        return EXIT_FAIL
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE
    args.json = bool(getattr(args, "json", False))
    msg = _need_root(args.command)
    if msg:
        output.error(msg)
        return EXIT_FAIL
    try:
        cfg = load_config() if args.command not in NO_CONFIG_COMMANDS else Config()
        return int(args.handler(args, cfg) or 0)
    except (users.UserError, ConfigError, LockTimeout, allowlist.AllowlistError) as e:
        output.error(str(e))
        return EXIT_FAIL
    except protolib.ProtoError as e:
        output.error(f"{e} {e.short() if e.stderr else ''}".rstrip())
        return EXIT_FAIL
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # zoo ... | head: тихо выходим
        try:
            sys.stdout.close()
        except OSError:
            pass
        return EXIT_OK
