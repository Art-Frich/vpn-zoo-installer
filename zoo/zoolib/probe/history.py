"""История проб на сервере: SQLite `/var/lib/vpn-zoo/probe-history.sqlite`.

Сюда попадает каждый прогон `zoo probe --local` и каждый присланный клиентский отчёт
(`zoo history add FILE`, страница «Проверка» админки). Таблицы:
    reports   один отчёт целиком (raw — JSON как есть, с IP: файл 0600) и его контекст
    results   по строке на протокол: вердикт и числа (задержка, p90, джиттер, потери, скорости)
    live      лёгкие замеры раз в 10 мин (probe/live.py): 7 суток, в выгрузку и рейтинг не попадают
По этим данным считаются «Лучшие протоколы» (rank.py), тренды в админке и выгрузка в репо (export.py).
Один и тот же отчёт дважды не записывается (uid = sha256 содержимого).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import output, paths
from . import context as ctx_mod
from . import metrics

DB_NAME = "probe-history.sqlite"
SCHEMA = 1
DEFAULT_KEEP_DAYS = 365     # отчёт ~15 КБ: даже сотня прогонов в день — десятки МБ за год

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL, mode TEXT NOT NULL,
    source TEXT NOT NULL, server TEXT, tag TEXT, device TEXT, country TEXT, asn INTEGER, isp TEXT,
    net TEXT, zoo TEXT, raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_ts ON reports (ts);
CREATE TABLE IF NOT EXISTS results (
    report_id INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE, proto TEXT NOT NULL,
    verdict TEXT NOT NULL, latency_ms REAL, p90_ms REAL, jitter_ms REAL, loss_pct REAL, rtt_ms REAL,
    down_mbps REAL, up_mbps REAL, PRIMARY KEY (report_id, proto)
);
CREATE INDEX IF NOT EXISTS results_proto ON results (proto);
CREATE TABLE IF NOT EXISTS live (
    ts INTEGER NOT NULL, proto TEXT NOT NULL, ok INTEGER NOT NULL, rtt_ms REAL, jitter_ms REAL, mbps REAL,
    verdict TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS live_proto_ts ON live (proto, ts);
"""


class HistoryError(Exception):
    pass


def db_path() -> Path:
    return paths.state_dir() / DB_NAME


def keep_days() -> int:
    """Срок хранения отчётов (ZOO_PROBE_KEEP_DAYS, 0 — хранить всё)."""
    try:
        d = int(os.environ.get("ZOO_PROBE_KEEP_DAYS", DEFAULT_KEEP_DAYS))
    except ValueError:
        d = DEFAULT_KEEP_DAYS
    return max(d, 0)


def prune(con: sqlite3.Connection, now: float | None = None) -> int:
    """Удалить отчёты старше срока (results — каскадом). → сколько удалено."""
    days = keep_days()
    if not days:
        return 0
    cur = con.execute("DELETE FROM reports WHERE ts < ?", (int((now or time.time()) - days * 86400),))
    return cur.rowcount


def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection | None:
    """Соединение с БД; нет файла и create=False → None."""
    path = Path(path) if path else db_path()
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 10000")
    con.execute("PRAGMA foreign_keys = ON")
    if create:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(_DDL)
        con.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
        con.commit()
    return con


# ---------- разбор отчёта ----------

def canonical(rep: dict[str, Any]) -> str:
    return json.dumps(rep, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def report_uid(rep: dict[str, Any]) -> str:
    """Стабильный идентификатор отчёта: по нему отчёт не записывается дважды, он же — имя
    зашифрованного файла в history/raw. Хеш всего отчёта: по нему IP не восстановить."""
    return hashlib.sha256(canonical(rep).encode("utf-8")).hexdigest()[:16]


PROTO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
VERDICT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


def validate(rep: Any) -> dict[str, Any]:
    """Отчёт пробы с results [{id, verdict}]; иначе HistoryError. id и вердикт — короткие
    идентификаторы: они попадают в рейтинг и в открытый jsonl."""
    if isinstance(rep, list):
        rep = {"results": rep}
    res = rep.get("results") if isinstance(rep, dict) else None
    if not isinstance(res, list) or not all(isinstance(r, dict) and isinstance(r.get("id"), str)
                                            and isinstance(r.get("verdict"), str) for r in res):
        raise HistoryError("в отчёте нет списка results вида {id, verdict}")
    bad = [r["id"][:40] for r in res if not PROTO_RE.match(r["id"]) or not VERDICT_RE.match(r["verdict"])]
    if bad:
        raise HistoryError(f"в отчёте непонятные id или вердикт протокола: {', '.join(map(repr, bad[:3]))}")
    return rep


def report_ts(rep: dict[str, Any]) -> int:
    try:
        d = datetime.fromisoformat(str(rep.get("generated")))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except (ValueError, TypeError):
        return int(time.time())


def context_of(rep: dict[str, Any]) -> dict[str, Any]:
    """Контекст отчёта, приведённый к ожидаемым типам: присланный отчёт мог собрать кто угодно,
    а эти поля идут в БД, в рейтинг и открытым текстом в history/*.jsonl."""
    c = rep.get("context") if isinstance(rep.get("context"), dict) else {}
    asn, net, country = c.get("asn"), c.get("net"), c.get("country")
    return {"tag": ctx_mod.clean_text(c.get("tag"), 40), "device": ctx_mod.clean_text(c.get("device"), 40),
            "net": net if net in ctx_mod.NET_TYPES else None,
            "country": country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else None,
            "asn": asn if isinstance(asn, int) and not isinstance(asn, bool) and 0 < asn < 4_294_967_296 else None,
            "isp": ctx_mod.clean_text(c.get("isp"))}


def apply_labels(rep: dict[str, Any], tag: str | None, device: str | None) -> dict[str, Any]:
    """Метки, заданные при добавлении отчёта, вписываются в его context и перекрывают записанные (старые клиенты меток не пишут)."""
    if tag or device:
        c = dict(rep.get("context") or {})
        if tag:
            c["tag"] = ctx_mod.clean_label(tag, "tag")
        if device:
            c["device"] = ctx_mod.clean_label(device, "device")
        rep = dict(rep, context=c)
    return rep


# ---------- запись ----------

def record(rep: Any, source: str, con: sqlite3.Connection | None = None, tag: str | None = None,
           device: str | None = None) -> tuple[int | None, str]:
    """→ (id записи или None, если такой отчёт уже есть; uid). source: local | upload | cli."""
    rep = apply_labels(validate(rep), tag, device)
    uid = report_uid(rep)
    own = con is None
    con = con or connect()
    assert con is not None
    try:
        if con.execute("SELECT 1 FROM reports WHERE uid = ?", (uid,)).fetchone():
            return None, uid
        c = context_of(rep)
        label = ctx_mod.clean_text(rep.get("label"), 40)
        cur = con.execute(
            "INSERT INTO reports (uid, ts, mode, source, server, tag, device, country, asn, isp, net, zoo, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, report_ts(rep), "local" if rep.get("mode") == "local" else "remote", source, label,
             c["tag"], c["device"], c["country"], c["asn"], c["isp"], c["net"], rep.get("zoo"), canonical(rep)))
        rid = cur.lastrowid
        for r in rep["results"]:
            n = metrics.result_numbers(r)
            con.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, r["id"], r["verdict"], n["latency_ms"], n["p90_ms"], n["jitter_ms"],
                         n["loss_pct"], n["rtt_ms"], n["down_mbps"], n["up_mbps"]))
        prune(con)
        con.commit()
        return rid, uid
    finally:
        if own:
            con.close()


def record_safely(rep: Any, source: str, **kw: Any) -> str | None:
    """Запись, не ломающая основной сценарий: сбой → сообщение (текст ошибки), успех → None."""
    try:
        record(rep, source, **kw)
        return None
    except (HistoryError, sqlite3.Error, OSError, ValueError) as e:
        return str(e)


def record_notice(rep: Any, source: str, **kw: Any) -> str:
    """Запись присланного отчёта → фраза для пользователя (записан, уже был, ошибка)."""
    try:
        rid, _ = record(rep, source, **kw)
    except (HistoryError, sqlite3.Error, OSError, ValueError) as e:
        return f"В историю отчёт не записан: {e}"
    return "Отчёт записан в историю." if rid is not None else "Этот отчёт уже есть в истории — повторно не записан."


# ---------- чтение ----------

def since_ts(period: str | None, now: float | None = None) -> int | None:
    """«30d», «12h», «2w», «all» → граница времени (None — без границы)."""
    if not period or period == "all":
        return None
    unit = {"h": 3600, "d": 86400, "w": 7 * 86400}.get(period[-1:])
    if not unit or not period[:-1].isdigit() or int(period[:-1]) <= 0:
        raise HistoryError("период: число и h, d или w (например 24h, 30d, 2w) либо all")
    return int((time.time() if now is None else now) - int(period[:-1]) * unit)


def fetch_results(con: sqlite3.Connection, since: int | None = None, mode: str | None = "remote",
                  tag: str | None = None) -> list[dict[str, Any]]:
    """Строки «протокол в прогоне» с контекстом прогона, от старых к новым."""
    sql = ("SELECT r.id AS report_id, r.ts, r.mode, r.source, r.server, r.tag, r.device, r.country, r.asn, "
           "r.isp, r.net, x.proto, x.verdict, x.latency_ms, x.p90_ms, x.jitter_ms, x.loss_pct, x.rtt_ms, "
           "x.down_mbps, x.up_mbps FROM results x JOIN reports r ON r.id = x.report_id WHERE 1 = 1")
    args: list[Any] = []
    if since is not None:
        sql += " AND r.ts >= ?"
        args.append(since)
    if mode:
        sql += " AND r.mode = ?"
        args.append(mode)
    if tag:
        sql += " AND r.tag = ?"
        args.append(tag)
    return [dict(r) for r in con.execute(sql + " ORDER BY r.ts, r.id, x.proto", args)]


def list_reports(con: sqlite3.Connection, limit: int = 30, since: int | None = None) -> list[dict[str, Any]]:
    """Последние отчёты: контекст и сколько протоколов работает из проверенных."""
    sql = ("SELECT r.id, r.ts, r.mode, r.source, r.server, r.tag, r.device, r.country, r.asn, r.isp, r.net, "
           "(SELECT COUNT(*) FROM results x WHERE x.report_id = r.id) AS total, "
           "(SELECT COUNT(*) FROM results x WHERE x.report_id = r.id AND x.verdict IN ('OK','SLOW')) AS working, "
           "(SELECT COUNT(*) FROM results x WHERE x.report_id = r.id AND x.verdict IN ('SKIPPED','CLIENT_ERROR')) "
           "AS untested FROM reports r")
    args: list[Any] = []
    if since is not None:
        sql += " WHERE r.ts >= ?"
        args.append(since)
    return [dict(r) for r in con.execute(sql + " ORDER BY r.ts DESC, r.id DESC LIMIT ?", args + [limit])]


def raw_reports(con: sqlite3.Connection, since: int | None = None) -> Iterable[dict[str, Any]]:
    sql = "SELECT raw FROM reports" + (" WHERE ts >= ?" if since is not None else "") + " ORDER BY ts, id"
    for row in con.execute(sql, [since] if since is not None else []):
        yield json.loads(row["raw"])


def counts(con: sqlite3.Connection) -> dict[str, int]:
    return {k: con.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ("reports", "results")}


# ---------- CLI: zoo history ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="history_command", metavar="ДЕЙСТВИЕ")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="вывод в JSON")

    a = sub.add_parser("add", parents=[common], help="записать отчёт(ы) пробы в историю",
                       description="Записать клиентские отчёты (probe-report.json) в историю сервера; «-» — stdin")
    a.add_argument("files", nargs="+", metavar="FILE")
    a.add_argument("--tag", type=ctx_mod.label_arg("tag"), help="метка отчёта (перекрывает записанную в нём), mobile-mts")
    a.add_argument("--device", type=ctx_mod.label_arg("device"), help="устройство (перекрывает записанное в отчёте)")

    li = sub.add_parser("list", parents=[common], help="последние записанные отчёты")
    li.add_argument("--period", default="30d", help="за какой период (24h, 30d, all)")
    li.add_argument("--limit", type=int, default=20)

    ex = sub.add_parser(
        "export", parents=[common], help="выгрузка для history/ в репо: анонимный jsonl + сырые отчёты (age)",
        description="Анонимные строки (без IP) в YYYY-MM.jsonl и сырые отчёты в raw/<id>.json.age, "
                    "зашифрованные age на ключи из --recipients (ssh-ed25519, ssh-rsa, age1…)")
    ex.add_argument("--recipients", metavar="FILE", help="файл публичных ключей (history/recipients.txt); «-» — stdin")
    ex.add_argument("--no-raw", action="store_true", help="только анонимный jsonl, без сырых отчётов")
    g = ex.add_mutually_exclusive_group()
    g.add_argument("--out", metavar="DIR", help="каталог вывода (дополняет существующие файлы)")
    g.add_argument("--tar", action="store_true", help="tar-архив в stdout (для scripts/history.sh pull)")
    ex.add_argument("--since", default="all", metavar="ПЕРИОД", help="за какой период (30d, all)")


def _read_report_files(files: list[str]) -> list[tuple[str, Any]]:
    out = []
    for f in files:
        try:
            text = sys.stdin.read() if f == "-" else Path(f).read_text(encoding="utf-8")
            out.append((f, json.loads(text)))
        except (OSError, ValueError) as e:
            raise HistoryError(f"{f}: {e}") from None
    return out


def cmd_history(args: argparse.Namespace, cfg: Any) -> int:
    cmd = getattr(args, "history_command", None)
    try:
        if cmd == "add":
            return _cmd_add(args)
        if cmd == "list":
            return _cmd_list(args)
        if cmd == "export":
            return _cmd_export(args)
    except HistoryError as e:
        output.error(str(e))
        return 1
    output.error("zoo history add|list|export")
    return 2


def _cmd_add(args: argparse.Namespace) -> int:
    added = dup = 0
    for name, data in _read_report_files(args.files):
        rid, uid = record(data, "upload", tag=args.tag, device=args.device)
        if rid is None:
            dup += 1
            output.info(f"{name}: уже есть в истории ({uid})")
        else:
            added += 1
            output.ok(f"{name}: записан ({uid})")
    return 0 if added or dup else 1


def _cmd_list(args: argparse.Namespace) -> int:
    since = since_ts(args.period)
    con = connect(create=False)
    try:
        rows = list_reports(con, args.limit, since) if con else []
    finally:
        if con:
            con.close()
    if args.json:
        output.print_json({"reports": rows})
        return 0
    if not rows:
        print("История пуста: zoo probe --local записывает прогоны сама, клиентские отчёты — zoo history add FILE")
        return 0
    table = []
    for r in rows:
        when = datetime.fromtimestamp(r["ts"]).strftime("%d.%m.%Y %H:%M")
        table.append([r["id"], when, r["mode"], r["tag"] or "—", r["isp"] or "—", r["country"] or "—",
                      r["device"] or "—", f"{r['working']}/{r['total'] - r['untested']}"])
    print(output.table(table, ["#", "когда", "режим", "метка", "провайдер", "страна", "устройство", "работает"]))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from . import export
    since = since_ts(args.since)
    con = connect(create=False)
    if con is None:
        raise HistoryError("история пуста: нечего выгружать")
    try:
        reps = list(raw_reports(con, since))
    finally:
        con.close()
    return export.cmd_export(reps, args)

