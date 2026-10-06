"""Живые метрики протоколов (D41): лёгкий замер каждого включённого протокола раз в 10 минут.

`zoo live run`            все включённые протоколы по очереди (zoo-live.timer);
`zoo live run --requests` только те, что заказал ↻ на карточке (zoo-live-req.service по zoo-live.path);
`zoo live show`           последние замеры.

Замер — тот же клиент пробника (учётка zoo-probe, с самого сервера): рукопожатие + 7 малых
запросов (2 для вердикта и 5 для задержки) → медиана задержки и джиттер. Раз в час (≥ 55 мин с
прошлой загрузки этого протокола) ещё и загрузка ~1 МБ → Мбит/с: она идёт на внешний CDN
через туннель, то есть это «скорость сервера по этому протоколу», а не канала до клиента.

Результат — таблица `live` в probe-history.sqlite: 7 суток, чистится при каждом запуске, в выгрузку
history/ и рейтинг не входит. Заявка ↻ — файл `live-req/<протокол>` (создаёт админка, у неё нет
права на systemctl; запускает замер zoo-live.path).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import output, paths, users
from ..fsutil import LockTimeout, file_lock
from . import engine, history, verdicts

KEEP_SECONDS = 7 * 86400
SPEED_EVERY = 55 * 60       # загрузка раз в час: не чаще, чем через 55 мин после прошлой
SPEED_BYTES = 1_000_000     # на 512 КБ slow start занижает скорость; менять — ZOO_LIVE_SPEED_BYTES
LATENCY_SAMPLES = 5
RATE_LIMIT = 60             # ↻ чаще раза в минуту на протокол не принимается
REQ_STALE = 300             # заявка старше — замер не пришёл (юнит не запущен): не «меряю…»
FRESH = 25 * 60             # замер старше — на карточке «устарело» (таймер раз в 10 мин)


def req_dir() -> Path:
    return paths.state_dir() / "live-req"


def lock_file() -> Path:
    return paths.state_dir() / "live.lock"


def speed_bytes() -> int:
    try:
        return min(max(int(os.environ.get("ZOO_LIVE_SPEED_BYTES", SPEED_BYTES)), 100_000), 5_000_000)
    except ValueError:
        return SPEED_BYTES


@contextlib.contextmanager
def exclusive(timeout: float = 300.0):
    """Замер и `zoo probe --local` ходят кредами одного пользователя: у пира AmneziaWG один endpoint,
    два клиента сразу мешали бы друг другу. Дольше timeout не ждём — работаем без блокировки."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(lock_file(), timeout=timeout))
        except LockTimeout:
            pass
        yield


def settings(speed: bool) -> engine.Settings:
    """Лёгкий замер: без IP выхода и поиска провайдера; большой запрос — только если пора."""
    return engine.Settings(mode="local", timeout=6.0, connect_timeout=4.0, stall=6.0,
                           large_bytes=speed_bytes() if speed else 0, ip_urls=(),
                           latency_samples=LATENCY_SAMPLES, latency_urls=engine.SMALL_URLS[:1],
                           lookup=False, progress=None)


# ---------- строка результата ----------

def row_of(r: dict[str, Any], ts: int) -> dict[str, Any]:
    """Результат engine.run по протоколу → строка таблицы live."""
    lat = (r.get("metrics") or {}).get("latency") or {}
    rtt = lat.get("median_ms")
    if rtt is None:
        rtt = r.get("latency_ms")
    return {"ts": ts, "proto": r["id"], "ok": int(r.get("verdict") in verdicts.WORKING),
            "rtt_ms": rtt, "jitter_ms": lat.get("jitter_ms"), "mbps": r.get("speed_mbps"),
            "verdict": r.get("verdict") or "", "bytes": int((r.get("large") or {}).get("bytes") or 0)}


def store(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    with con:
        con.executemany("INSERT INTO live (ts, proto, ok, rtt_ms, jitter_ms, mbps, verdict, bytes) "
                        "VALUES (:ts, :proto, :ok, :rtt_ms, :jitter_ms, :mbps, :verdict, :bytes)", rows)


def prune(con: sqlite3.Connection, now: float | None = None) -> int:
    with con:
        return con.execute("DELETE FROM live WHERE ts < ?", (int((now or time.time()) - KEEP_SECONDS),)).rowcount


# ---------- когда качать ----------

def _speed_key(proto: str) -> str:
    return f"live_speed:{proto}"


def speed_due(con: sqlite3.Connection, proto: str, now: float) -> bool:
    r = con.execute("SELECT value FROM meta WHERE key = ?", (_speed_key(proto),)).fetchone()
    try:
        return r is None or now - float(r[0]) >= SPEED_EVERY
    except ValueError:
        return True


def mark_speed(con: sqlite3.Connection, proto: str, now: float) -> None:
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (_speed_key(proto), str(int(now))))


# ---------- заявки ↻ ----------

def pending_requests() -> list[str]:
    """Протоколы с заявкой, которой не больше REQ_STALE секунд; старые заявки удаляются."""
    d, out, now = req_dir(), [], time.time()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for name in names:
        f = d / name
        try:
            age = now - f.stat().st_mtime
            if not history.PROTO_RE.match(name) or age > REQ_STALE:
                f.unlink()
            else:
                out.append(name)
        except OSError:
            pass
    return out


def clear_request(proto: str) -> None:
    try:
        (req_dir() / proto).unlink()
    except OSError:
        pass


def is_pending(proto: str, now: float | None = None) -> bool:
    try:
        return (time.time() if now is None else now) - (req_dir() / proto).stat().st_mtime <= REQ_STALE
    except OSError:
        return False


def last_ts(proto: str) -> int | None:
    con = _read()
    if con is None:
        return None
    try:
        r = con.execute("SELECT MAX(ts) FROM live WHERE proto = ?", (proto,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return r[0] if r and r[0] is not None else None


def request(proto: str, now: float | None = None) -> tuple[bool, str]:
    """Заказать замер одного протокола (вызывает админка). → (принято, пояснение).
    Не чаще раза в минуту: проверяются заявка и время последнего замера."""
    now = time.time() if now is None else now
    if not history.PROTO_RE.match(proto):
        return False, "неверное имя протокола"
    f = req_dir() / proto
    if is_pending(proto, now):
        return False, "замер уже заказан"
    last = last_ts(proto)
    if last is not None and now - last < RATE_LIMIT:
        return False, "замер был меньше минуты назад"
    req_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    f.write_text(str(int(now)), encoding="utf-8")
    os.chmod(f, 0o600)
    os.utime(f, (now, now))
    return True, "замер заказан"


# ---------- чтение для админки ----------

def _read() -> sqlite3.Connection | None:
    try:
        return history.connect(create=False)
    except sqlite3.Error:
        return None


def latest(now: float | None = None) -> dict[str, dict[str, Any]]:
    """{протокол: последний замер + последняя скорость за 2 часа}. Нет базы или таблицы — пусто."""
    now = time.time() if now is None else now
    con = _read()
    if con is None:
        return {}
    try:
        rows = con.execute("SELECT l.* FROM live l JOIN (SELECT proto, MAX(ts) AS ts FROM live GROUP BY proto) m "
                           "ON l.proto = m.proto AND l.ts = m.ts ORDER BY l.proto").fetchall()
        speeds = con.execute("SELECT proto, mbps, ts FROM live WHERE mbps IS NOT NULL AND ts >= ? ORDER BY ts",
                             (int(now - 2 * 3600),)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    out = {r["proto"]: dict(r, speed=None, speed_ts=None) for r in rows}
    for r in speeds:
        if r["proto"] in out:
            out[r["proto"]].update(speed=r["mbps"], speed_ts=r["ts"])
    return out


def own_bytes_today(now: float | None = None) -> dict[str, int]:
    """Сколько байт скачали сами замеры сегодня (по локальным суткам) с первого снятия счётчиков:
    их вычитает трафик протоколов, у которых нельзя отделить служебного пользователя (общий счётчик
    Xray). Замеры до первого снятия в счётчики не попали — их вычитать нельзя."""
    from .. import traffic
    now = time.time() if now is None else now
    day = traffic.align(now, traffic.RES_1D)
    since = traffic.first_run_since(day)
    con = _read() if since is not None else None
    if con is None:
        return {}
    try:
        rows = con.execute("SELECT proto, SUM(bytes) AS b FROM live WHERE ts >= ? GROUP BY proto",
                           (since,)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {r["proto"]: int(r["b"] or 0) for r in rows}


# ---------- прогон ----------

def measure(entries: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    """Замер протоколов по очереди и запись в БД. Протоколы, которым пора качать, идут отдельным
    проходом с большим запросом. → строки live."""
    from .. import probe as probe_pkg
    cfg_ip = None
    try:
        from ..config import load as load_config
        cfg_ip = load_config().get("SERVER_IP") or None
    except Exception:  # без config.env вердикты считаются без SERVER_IP
        pass
    now = int(time.time() if now is None else now)
    con = history.connect()
    assert con is not None
    rows: list[dict[str, Any]] = []
    try:
        due = {e["id"] for e in entries if speed_due(con, e["id"], now)}
        server_ips = probe_pkg.server_addresses()
        for speed in (False, True):
            group = [e for e in entries if (e["id"] in due) == speed]
            if not group:
                continue
            res = engine.run(group, settings(speed), server_ip=cfg_ip, server_ips=server_ips)
            for r in res:
                rows.append(row_of(r, int(time.time())))
                if speed:
                    mark_speed(con, r["id"], now)
                clear_request(r["id"])
        if rows:
            store(con, rows)
        prune(con, now)
    finally:
        con.close()
    return rows


def run(only: list[str] | None = None, from_requests: bool = False) -> list[dict[str, Any]]:
    """Один проход под блокировкой; заявки, пришедшие во время прохода, обрабатываются следующим."""
    from .. import probe as probe_pkg
    rows: list[dict[str, Any]] = []
    with file_lock(lock_file(), timeout=240):  # LockTimeout: идёт `zoo probe --local` — выше его ловит CLI
        for _ in range(3 if from_requests else 1):
            names = pending_requests() if from_requests else only
            if from_requests and not names:
                break
            try:
                entries = probe_pkg.collect_entries(users.probe_user(), names or None)
                if entries:
                    rows += measure(entries)
            finally:
                for n in names or []:
                    clear_request(n)  # замерено, протокол выключен или сбой: заявка не должна висеть
    return rows


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", nargs="?", choices=("run", "show"), default="show",
                   help="run — замер (юнит zoo-live.timer), show — последние замеры (по умолчанию)")
    p.add_argument("--proto", action="append", help="run: только этот протокол (можно несколько раз)")
    p.add_argument("--requests", action="store_true", help="run: только протоколы, замер которых заказали в админке")


def _fmt(v: Any, unit: str = "", nd: int = 0) -> str:
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def render(latest_: dict[str, dict[str, Any]]) -> None:
    rows = [[pid, d["verdict"], _fmt(d["rtt_ms"], " мс"), _fmt(d["jitter_ms"], " мс", 1),
             _fmt(d["speed"], " Мбит/с", 1), output.human_duration(max(time.time() - d["ts"], 0)) + " назад"]
            for pid, d in latest_.items()]
    print(output.table(rows, ["протокол", "итог", "задержка", "джиттер", "скорость", "замер"], right=(2, 3, 4)))


def cmd_live(args: argparse.Namespace, cfg: Any) -> int:
    if args.action == "run":
        try:
            rows = run(args.proto, args.requests)
        except LockTimeout:
            for n in pending_requests() if args.requests else []:
                clear_request(n)  # иначе zoo-live.path сразу запустил бы замер снова
            output.warn("замер пропущен: сейчас идёт проверка протоколов (zoo probe --local)")
            return 0
        if args.json:
            output.print_json({"rows": rows})
        elif rows:
            for r in rows:
                output.info(f"{r['proto']}: {r['verdict']} {_fmt(r['rtt_ms'], ' мс')}"
                            + (f" {r['mbps']:.1f} Мбит/с" if r["mbps"] is not None else ""))
        return 0
    data = latest()
    if args.json:
        output.print_json(data)
    elif data:
        render(data)
    else:
        output.info("замеров ещё нет: sudo zoo live run")
    return 0
