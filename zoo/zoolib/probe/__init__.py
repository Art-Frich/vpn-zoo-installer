"""Пробник (ARCHITECTURE §7).

`zoo probe --local`        с сервера: работает ли протокол в принципе. Клиент поднимается на
                           самом сервере и идёт на публичный IP (без hairpin — на loopback).
`zoo probe --remote FILE`  с любой Linux-машины или из контейнера zoo-probe: блокируется ли
                           у этого провайдера. FILE — пакет `zoo export-probe`.
`zoo probe --compare L R`  сравнить отчёты сервера и клиента.
`zoo probe --rank`         лучшие протоколы по накопленной истории (rank.py).
`zoo export-probe`         пакет для клиента: probe-объекты манифестов (с ключами!) и итог
                           последней самопроверки сервера.

Модули: endpoints (адрес в probe), clients (xray/hysteria/sing-box/awg), fetch (HTTP через
SOCKS5 или интерфейс), engine (прогон), verdicts (классификация), report (вывод), metrics
(задержка, скорость), context (метки и провайдер), history (SQLite), rank (рейтинг), export
(анонимный jsonl и age для history/ в репо).
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__, manifests, output, paths, protolib, users
from ..config import Config
from ..fsutil import atomic_write_json, atomic_write_text, read_json
from . import context as ctx_mod
from . import engine, export, history, rank, report as report_mod, verdicts
from .verdicts import Thresholds

VERDICTS = verdicts.VERDICTS

EXPORT_TYPE = "zoo-probe-export"
REPORT_TYPE = "zoo-probe-report"
SECRET_WARNING = ("в пакете ключи доступа к серверу: передавайте его только по защищённому каналу "
                  "(scp) и удалите после пробы")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def selftest_file() -> Path:
    return paths.state_dir() / "probe-local.json"


def setup(cfg: Config) -> None:
    """Вызывается из `zoo setup` (фаза 09). Пробнику нечего готовить заранее."""


# ---------- протоколы сервера ----------

def _probe_via_module(m: manifests.Manifest, user: str, libs: set[str]) -> dict[str, Any]:
    """probe для пользователя через proto-<id>.sh; манифест-инстанс <модуль>-<инст> → probe NAME инст."""
    mod = m.id if m.id in libs else users.shared_module(m, libs)
    if not mod:
        raise protolib.ProtoError(m.id, "probe", "нет модуля протокола", kind="missing_lib")
    if mod == m.id:
        return protolib.probe(mod, user)
    extra = [m.id[len(mod) + 1:]] if m.id.startswith(mod + "-") else []
    out = protolib.call(mod, "probe", user, *extra).stdout
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or "kind" not in data:
        raise protolib.ProtoError(m.id, "probe", "вывод — не JSON с полем kind", kind="bad_output")
    return data


def collect_entries(user: str = users.OWNER, only: list[str] | None = None) -> list[dict[str, Any]]:
    """Записи протоколов сервера: probe из манифеста (если он для этого пользователя) или от модуля.
    Модуль не отдал probe пользователя (его нет в протоколе), а в манифесте есть probe
    owner — проба идёт им, с пометкой в записи."""
    good, _bad = manifests.load_all()
    libs = set(protolib.list_libs())
    entries = []
    for m in good:
        if (only and m.id not in only) or not m.enabled:
            continue
        entry: dict[str, Any] = {"id": m.id, "name": m.name, "layer": m.layer, "port": m.port, "probe": None}
        probe = m.probe or None
        if probe and probe.get("user", users.OWNER) == user:
            entry["probe"] = probe
        elif m.has_users:
            try:
                entry["probe"] = _probe_via_module(m, user, libs)
            except protolib.ProtoError as e:
                if probe:
                    entry["probe"] = probe
                    entry["note"] = (f"кредами {probe.get('user', users.OWNER)}: probe для {user} "
                                     f"не получен ({e.short()})")
                else:
                    entry["skip_reason"] = f"probe для {user} не получен: {e.short()}"
        elif probe:
            entry["probe"] = probe
        entries.append(entry)
    if not entries and not good:
        # манифестов нет — возможно, есть только экспорт фазы 99
        f = paths.probe_export_file()
        if f.is_file():
            entries, _ = load_bundle(read_json(f))
    return entries


def load_selftest() -> dict[str, Any] | None:
    try:
        data = read_json(selftest_file())
    except (OSError, ValueError):
        return None
    return {"generated": data.get("generated"), "server_ip": data.get("server_ip"),
            "verdicts": {r["id"]: r["verdict"] for r in data.get("results", []) if "id" in r}}


def _save_selftest(rep: dict[str, Any], partial: bool) -> None:
    data = rep
    if partial:
        try:
            old = read_json(selftest_file())
            fresh = {r["id"] for r in rep["results"]}
            data = dict(rep, results=[r for r in old.get("results", []) if r.get("id") not in fresh] + rep["results"])
        except (OSError, ValueError):
            pass
    try:
        atomic_write_json(selftest_file(), data, 0o600)
    except OSError as e:
        output.warn(f"не сохранил итог самопроверки в {selftest_file()}: {e}")


def export_bundle(cfg: Config, user: str | None = None, only: list[str] | None = None) -> dict[str, Any]:
    user = user or users.probe_user()
    entries = collect_entries(user, only)
    return {
        "schema": 1, "type": EXPORT_TYPE, "generated": _now(), "zoo": __version__,
        "server_ip": cfg.get("SERVER_IP"), "server_ips": server_addresses(), "label": cfg.get("LABEL"),
        "user": user, "protocols": entries, "selftest": load_selftest(),
    }


def server_addresses() -> list[str]:
    """Глобальные адреса сервера (IPv4 и IPv6): Xray выходит и с IPv6, это тоже «с сервера»."""
    import ipaddress
    import subprocess
    try:
        out = subprocess.run(["ip", "-o", "addr", "show", "scope", "global"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    addrs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
            ip = parts[3].split("/")[0]
            try:
                if ipaddress.ip_address(ip).is_global:
                    addrs.append(ip)
            except ValueError:
                pass
    return addrs


# ---------- пакет экспорта (в т. ч. от фазы 99) ----------

def load_bundle(data: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Нестрогий разбор: {protocols: [...]|{id: ...}}, {manifests: [...]} или список манифестов;
    элемент — манифест/запись с полем probe или сам probe-объект (с kind)."""
    meta: dict[str, Any] = data if isinstance(data, dict) else {}
    items: Any = data if isinstance(data, list) else (
        meta.get("protocols") or meta.get("manifests") or meta.get("entries") or [])
    if isinstance(items, dict):
        items = [dict(v, id=v.get("id", k)) for k, v in items.items() if isinstance(v, dict)]
    entries = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("enabled") is False:
            continue
        probe = it.get("probe") if "probe" in it else (it if "kind" in it else None)
        pid = it.get("id")
        if not pid:
            continue
        entry = {"id": str(pid), "name": it.get("name") or str(pid), "layer": manifests.norm_layer(it.get("layer")),
                 "port": it.get("port"), "probe": probe if isinstance(probe, dict) and probe else None}
        if it.get("skip_reason"):
            entry["skip_reason"] = it["skip_reason"]
        entries.append(entry)
    st = meta.get("selftest")
    if isinstance(st, dict) and "results" in st:
        st = {"generated": st.get("generated"), "verdicts": {r["id"]: r["verdict"] for r in st["results"]}}
    ips = meta.get("server_ips")
    info = {"server_ip": meta.get("server_ip") or meta.get("server") or meta.get("SERVER_IP"),
            "server_ips": [str(a) for a in ips if isinstance(a, str)] if isinstance(ips, list) else [],
            "label": meta.get("label") or meta.get("LABEL"), "user": meta.get("user"),
            "generated": meta.get("generated"), "selftest": st if isinstance(st, dict) else None}
    return sorted(entries, key=lambda e: e["id"]), info


# ---------- прогоны ----------

def make_report(mode: str, results: list[dict[str, Any]], st: engine.Settings, **meta: Any) -> dict[str, Any]:
    """Схема 1; поля context и metrics (задержка p90/джиттер, отдача) добавлены позже и необязательны."""
    return {
        "schema": 1, "type": REPORT_TYPE, "mode": mode, "generated": _now(), "zoo": __version__,
        "host": socket.gethostname(), **meta,
        "settings": {"timeout": st.timeout, "stall": st.stall, "large_bytes": st.large_bytes,
                     "slow_mbps": st.thresholds.min_mbps, "latency_samples": st.latency_samples,
                     "upload_bytes": st.upload_bytes},
        "results": results, "summary": report_mod.summary(results),
    }


def run_local(cfg: Config, protocols: list[str] | None = None, user: str | None = None,
              st: engine.Settings | None = None, record_history: bool = True) -> dict[str, Any]:
    st = st or engine.Settings()
    st.mode = "local"
    user = user or users.probe_user()
    entries = collect_entries(user, protocols)
    server_ip = cfg.get("SERVER_IP") or None
    results = engine.run(entries, st, server_ip=server_ip, server_ips=server_addresses())
    rep = make_report("local", results, st, server_ip=server_ip, label=cfg.get("LABEL"), user=user,
                      context=ctx_mod.build(st.tag, st.device))
    _save_selftest(rep, partial=bool(protocols))
    if record_history:
        err = history.record_safely(rep, "local")
        if err:
            output.warn(f"не записал прогон в историю ({history.db_path()}): {err}")
    return rep


def run_remote(export: Any, protocols: list[str] | None = None,
               st: engine.Settings | None = None) -> dict[str, Any]:
    st = st or engine.Settings()
    st.mode = "remote"
    entries, meta = load_bundle(export)
    everything = entries
    if protocols:
        entries = [e for e in entries if e["id"] in protocols]
    selftest = (meta.get("selftest") or {}).get("verdicts") or None
    direct = ctx_mod.direct_context(st.ip_urls, st.lookup)
    my_ip = direct["ip"]
    intercepted = engine.tcp_intercepted(meta["server_ip"])
    results = engine.run(entries, st, server_ip=meta["server_ip"], selftest=selftest, my_ip=my_ip,
                         context=everything if protocols else None, server_ips=meta.get("server_ips"))
    rep = make_report("remote", results, st, server_ip=meta["server_ip"], label=meta.get("label"),
                      user=meta.get("user"), direct_ip=my_ip, tcp_intercepted=intercepted,
                      context=ctx_mod.build(st.tag, st.device, direct, ctx_mod.net_hint()),
                      selftest_generated=(meta.get("selftest") or {}).get("generated"))
    if selftest:
        rep["compare"] = report_mod.compare(selftest, rep)
    return rep


def all_working(rep: dict[str, Any]) -> bool:
    return all(r["verdict"] in verdicts.WORKING or r["verdict"] == verdicts.SKIPPED for r in rep["results"])


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="самопроверка с сервера (по умолчанию)")
    mode.add_argument("--remote", metavar="FILE", help="клиентский прогон по пакету export-probe")
    mode.add_argument("--compare", nargs=2, metavar=("LOCAL", "REMOTE"),
                      help="сравнить отчёты сервера и клиента (JSON)")
    mode.add_argument("--rank", action="store_true",
                      help="лучшие протоколы по истории проб (по контекстам: метка или провайдер)")
    p.add_argument("--proto", action="append", help="только этот протокол (можно несколько раз)")
    p.add_argument("--user", help=f"чьими кредами проверять (--local; по умолчанию {users.PROBE_USER}, "
                                  "без него — owner)")
    p.add_argument("--timeout", type=float, default=10.0, help="таймаут рукопожатия и малого запроса, с")
    p.add_argument("--stall", type=float, default=8.0, help="застой большого запроса, с")
    p.add_argument("--speed-mb", type=float, default=5.0,
                   help="объём загрузки для замера скорости, МБ (по умолчанию 5; на мобильном интернете — меньше)")
    p.add_argument("--large-bytes", type=int, help="то же в байтах (перекрывает --speed-mb)")
    p.add_argument("--upload-mb", type=float, default=0.0,
                   help="замер отдачи: столько МБ на speed.cloudflare.com/__up (0 — не мерить)")
    p.add_argument("--upload-url", help="адрес приёма для замера отдачи (POST, любое тело, ответ 2xx)")
    p.add_argument("--latency-samples", type=int, default=5, metavar="N",
                   help="замеров задержки на каждую из двух целей (0 — не мерить)")
    p.add_argument("--tag", type=ctx_mod.label_arg("tag"), metavar="МЕТКА",
                   help="метка условий пробы (mobile-mts, cafe-wifi); с --rank — только эта метка")
    p.add_argument("--device", type=ctx_mod.label_arg("device"), metavar="УСТРОЙСТВО",
                   help="устройство, с которого идёт проба (pixel7, laptop)")
    p.add_argument("--no-lookup", action="store_true",
                   help="--remote: не узнавать провайдера и страну (запрос к speed.cloudflare.com/meta)")
    p.add_argument("--no-history", action="store_true", help="--local: не записывать прогон в историю")
    p.add_argument("--history-dir", metavar="DIR",
                   help="--remote: дописать анонимный jsonl и зашифрованный сырой отчёт в DIR (история в репо)")
    p.add_argument("--history-recipients", metavar="FILE",
                   help="публичные ключи для сырого отчёта (по умолчанию DIR/recipients.txt)")
    p.add_argument("--period", default="30d", help="--rank: период (24h, 30d, 2w, all)")
    p.add_argument("--by", choices=rank.BY_CHOICES, default="context",
                   help="--rank: чем группировать (по умолчанию метка, а без неё провайдер)")
    p.add_argument("--with-local", action="store_true", help="--rank: учесть и прогоны с сервера")
    p.add_argument("--slow-mbps", type=float, default=2.0, help="ниже этой скорости — SLOW")
    p.add_argument("--no-fallback", action="store_true", help="--local: не повторять через loopback")
    p.add_argument("--small-url", action="append", help="URL малого запроса (вместо стандартных)")
    p.add_argument("--large-url", action="append", help="URL большого запроса; {bytes} — объём")
    p.add_argument("--ip-url", action="append", help="URL с ответом ip=… (cdn-cgi/trace)")
    p.add_argument("--out", metavar="FILE", help="записать отчёт JSON")
    p.add_argument("--md", metavar="FILE", help="записать отчёт Markdown")
    p.add_argument("--quiet", action="store_true", help="без хода проверки в stderr")
    p.add_argument("--summary", action="store_true",
                   help="--local: короткая сводка «работает в принципе» и что делать дальше (так печатает install.sh)")
    p.add_argument("--export", metavar="FILE",
                   help="--local: после проверки записать пакет export-probe с её итогом в FILE")


def add_export_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", metavar="PATH", help="куда записать пакет (по умолчанию stdout)")
    p.add_argument("--user", help=f"чьи креды положить в пакет (по умолчанию {users.PROBE_USER}, без него — owner)")
    p.add_argument("--quiet", action="store_true", help="без предупреждений (для install.sh)")
    p.add_argument("--proto", action="append", help="только этот протокол (можно несколько раз)")


def settings_from_args(args: argparse.Namespace) -> engine.Settings:
    large = args.large_bytes or int(args.speed_mb * 1_000_000)
    if large <= 0 or args.latency_samples < 0 or args.upload_mb < 0:
        raise ValueError("--speed-mb, --upload-mb, --latency-samples: нужны положительные числа")
    st = engine.Settings(timeout=args.timeout, stall=args.stall, large_bytes=large,
                         thresholds=Thresholds(min_mbps=args.slow_mbps), fallback=not args.no_fallback,
                         latency_samples=args.latency_samples, upload_bytes=int(args.upload_mb * 1_000_000),
                         tag=args.tag, device=args.device, lookup=not args.no_lookup)
    if args.upload_url:
        st.upload_url = args.upload_url
    if args.small_url:
        st.small_urls = tuple(args.small_url)
    if args.large_url:
        st.large_urls = tuple(args.large_url)
    if args.ip_url:
        st.ip_urls = tuple(args.ip_url)
    if args.quiet:
        st.progress = None
    return st


def _load_json(path: str) -> Any:
    """JSON из файла; None (с сообщением) — если не прочитать."""
    try:
        return read_json(Path(path))
    except FileNotFoundError:
        output.error(f"нет файла {path}")
    except (OSError, ValueError) as e:
        output.error(f"{path}: {e}")
    return None


def cmd_probe(args: argparse.Namespace, cfg: Config) -> int:
    if args.compare:
        local, remote = (_load_json(x) for x in args.compare)
        if local is None or remote is None:
            return 1
        rows = report_mod.compare(local, remote)
        if args.json:
            output.print_json({"compare": rows})
        else:
            report_mod.render_compare(rows)
        return 0
    if args.rank:
        return rank.cmd_rank(args)
    try:
        st = settings_from_args(args)
    except ValueError as e:
        output.error(str(e))
        return 2
    if args.remote:
        export = _load_json(args.remote)
        if export is None:
            return 1
        rep = run_remote(export, args.proto, st)
    else:
        rep = run_local(cfg, args.proto, args.user, st, record_history=not args.no_history)
    if args.out:
        atomic_write_json(Path(args.out), rep, 0o644)
    if args.md:
        atomic_write_text(Path(args.md), report_mod.markdown(rep), 0o644)
    bundle_path = None
    if args.export and not args.remote:
        bundle = export_bundle(cfg, rep.get("user"))
        if any(e.get("probe") for e in bundle["protocols"]):
            atomic_write_text(Path(args.export), json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", 0o600)
            bundle_path = args.export
    if args.history_dir:
        _write_history_dir(rep, args)
    if args.json:
        output.print_json(rep)
    elif args.summary and not args.remote:
        report_mod.render_summary(rep)
        report_mod.render_next_steps(rep, bundle_path, ssh_port=_ssh_port(cfg), repo_url=_repo_url())
    else:
        report_mod.render(rep)
    if not rep["results"]:
        output.warn("нет протоколов для проверки" + ("" if args.remote else " (манифесты protocols.d пусты?)"))
        return 1
    return 0 if all_working(rep) else 1


def _write_history_dir(rep: dict[str, Any], args: argparse.Namespace) -> None:
    """Клиентский прогон → history/ (jsonl без IP; сырой отчёт под age, если есть recipients.txt и age)."""
    out = Path(args.history_dir)
    rfile = Path(args.history_recipients) if args.history_recipients else out / "recipients.txt"
    try:
        recipients = export.parse_recipients(rfile.read_text(encoding="utf-8"), allow_empty=True) if rfile.is_file() else None
        age_bin = export.find_age() if recipients else None
        if recipients and not age_bin:
            output.warn("нет бинаря age: сырой отчёт не сохранён, в историю пошёл только анонимный jsonl")
            recipients = None
        elif not recipients:
            recipients = None
            output.info(f"в {rfile} нет ключей получателей: сырой отчёт не сохранён (только анонимный jsonl)")
        stats = export.write_dir(out, [rep], recipients, age_bin)
        output.ok(f"история: {out} (строк {stats['rows']}, сырых отчётов {stats['raw_new']})")
    except (history.HistoryError, OSError, subprocess.SubprocessError) as e:
        output.warn(f"история в {out} не записана: {e}")


DEFAULT_REPO_URL = "https://github.com/Art-Frich/vpn-zoo-installer.git"


def _repo_url() -> str:
    """Что клонировать на машине пользователя: origin рабочей копии сервера (https, без логина и
    токена в адресе; git@host:path → https://host/path), иначе адрес проекта."""
    from .. import upgrade  # upgrade импортирует probe — только здесь
    repo = upgrade.repo_dir()
    if not repo:
        return DEFAULT_REPO_URL
    rc, url = upgrade._git(repo, "remote", "get-url", "origin", timeout=5)
    if rc != 0 or not url:
        return DEFAULT_REPO_URL
    m = re.match(r"^[\w.-]+@([\w.-]+):(?!/)(.+)$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    u = urllib.parse.urlsplit(url)
    if u.scheme in ("https", "http") and u.hostname:
        return urllib.parse.urlunsplit((u.scheme, u.hostname + (f":{u.port}" if u.port else ""), u.path, "", ""))
    return DEFAULT_REPO_URL


def _ssh_port(cfg: Config) -> str:
    return cfg.ssh_login_port()


def cmd_export_probe(args: argparse.Namespace, cfg: Config) -> int:
    bundle = export_bundle(cfg, args.user, args.proto)
    usable = [e for e in bundle["protocols"] if e.get("probe")]
    for e in bundle["protocols"]:
        if not e.get("probe"):
            output.warn(f"{e['id']}: {e.get('skip_reason') or 'нет probe в манифесте'}")
    if not usable:
        output.error("нет ни одного протокола с probe — пакет пуст")
        return 1
    text = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        atomic_write_text(Path(args.out), text, 0o600)
        output.ok(f"пакет: {args.out} ({len(usable)} протоколов)")
    else:
        print(text, end="")
    output.warn(SECRET_WARNING)
    if not bundle["selftest"]:
        output.info("самопроверки сервера ещё не было: zoo probe --local — тогда отчёт клиента сравнит результаты")
    return 0
