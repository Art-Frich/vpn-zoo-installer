"""Объём данных zoo: разделы, общий бюджет, чистка по лимиту (docs/PLAN-admin.md §13).

Разделы: трафик, журнал атак, история проб (в ней же таблица live), логи установки, дистрибутивы клиентов
(каталог dist, их скачивает `zoo clients --fetch-dist`; чистка — версиями, самые старые; последняя версия платформы
бюджетом не удаляется, а качается не больше доли раздела — dist.cap()).
Бюджет — `ZOO_DATA_LIMIT` в config.env (по умолчанию 1 ГБ, но не больше 5 % диска), у каждого
раздела своя доля. Чистка начинается, только когда превышен общий бюджет: режется раздел,
который сильнее всех вылез за свою долю, самые старые записи (у трафика и журнала сначала
детальные разрешения), затем VACUUM. Проверка «в бюджете ли» — один os.stat на файл.

`zoo storage` — сводка, `--enforce` — чистка (её зовёт zoo-collector после снятия данных),
`--clear РАЗДЕЛ` — очистить раздел, `--limit 2G` — задать бюджет.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import dist, geoip, journal, output, paths, traffic
from .config import Config, config_set
from .fsutil import LockTimeout, atomic_write_json, file_lock
from .probe import history

LIMIT_KEY = "ZOO_DATA_LIMIT"
DEFAULT_LIMIT = 1 << 30
MIN_LIMIT = 16 << 20        # меньше — чистка съест всё полезное
MAX_DISK_SHARE = 0.05       # умолчание не больше 5 % диска
LOW_DISK = 0.10             # меньше свободного — тревога
HYSTERESIS = 0.9            # режем до 90 % бюджета, чтобы не чистить каждые 5 минут
SHARES = {"traffic": 20, "journal": 20, "probe": 20, "logs": 10, "dist": 30}
JOURNALD_DIRS = ("/var/log/journal", "/run/log/journal")
STATE_FILE = "storage.json"
KEEP_TRIMS = 7 * 86400
TOO_OFTEN = 2               # чисток раздела за сутки — «бюджет мал»
DIST_PARTIAL_AGE = 3600     # версия без meta.json моложе — её сейчас качают (zoo-clients: до 20 мин)

_UNITS = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
_CYR = {"К": "K", "М": "M", "Г": "G", "Т": "T"}
_SIZE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([KMGTКМГТ]?)(?:I?B|Б)?$", re.I)


# ---------- размеры ----------

def parse_size(text: str | None) -> int | None:
    """«1G», «500 МБ», «1,5g», «1048576» (байты) → байты; мусор и ноль → None."""
    m = _SIZE_RE.match((text or "").strip().upper())
    if not m:
        return None
    unit = _CYR.get(m.group(2), m.group(2))
    n = int(float(m.group(1).replace(",", ".")) * _UNITS.get(unit, 1))
    return n if n > 0 else None


def format_size(n: int) -> str:
    """Как пишем в config.env: «1G», «500M», иначе байты."""
    for unit in ("T", "G", "M", "K"):
        if n % _UNITS[unit] == 0:
            return f"{n // _UNITS[unit]}{unit}"
    return str(n)


def _du(path: Path, pattern: str = "*") -> int:
    total = 0
    try:
        for f in path.rglob(pattern):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------- разделы ----------

class Section:
    id = ""
    title = ""
    available = True        # False — раздела нет: не режем и не очищаем
    lock_name: str | None = None

    @property
    def share(self) -> int:
        return SHARES[self.id]

    def files(self) -> list[Path]:
        return []

    def path(self) -> str:
        f = self.files()
        return str(f[0]) if f else ""

    def size(self) -> int:
        return sum(_size(f) for f in self.files())

    def oldest(self) -> int | None:
        return None

    def rows(self) -> int:
        return 0

    def delete_oldest(self, n: int) -> int:
        """Удалить n самых старых записей; → сколько удалено."""
        return 0

    def compact(self) -> None:
        """Вернуть место файловой системе (VACUUM)."""


class SqliteSection(Section):
    """Раздел — одна SQLite. steps — (DELETE …LIMIT ?, параметры до лимита) по порядку: сначала
    то, что можно потерять без вреда (детальные разрешения), последним — самое долгоживущее."""

    def __init__(self, id_: str, title: str, db: Callable[[], Path], connect: Callable[..., Any],
                 lock_name: str | None, oldest_sql: str, rows_sql: str | tuple[str, ...], steps: list[tuple[str, tuple]]):
        self.id, self.title, self.lock_name = id_, title, lock_name
        self._db, self._connect = db, connect
        self._oldest_sql, self._rows_sql, self._steps = oldest_sql, rows_sql, steps

    def files(self) -> list[Path]:
        db = self._db()
        return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]

    def _query(self, sql: str) -> Any:
        try:
            con = self._connect(create=False)
        except sqlite3.Error:
            return None
        if con is None:
            return None
        try:
            r = con.execute(sql).fetchone()
            return r[0] if r else None
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def oldest(self) -> int | None:
        v = self._query(self._oldest_sql)
        return int(v) if v else None

    def rows(self) -> int:
        sqls = (self._rows_sql,) if isinstance(self._rows_sql, str) else self._rows_sql
        return sum(int(self._query(q) or 0) for q in sqls)    # нет таблицы (старая база) — 0

    def delete_oldest(self, n: int) -> int:
        con = self._connect(create=False)
        if con is None:
            return 0
        done = 0
        try:
            with con:
                for sql, params in self._steps:
                    if done >= n:
                        break
                    try:
                        done += con.execute(sql, (*params, n - done)).rowcount
                    except sqlite3.OperationalError as e:
                        if "no such table" not in str(e):
                            raise
        finally:
            con.close()
        return done

    def compact(self) -> None:
        con = self._connect(create=False)
        if con is None:
            return
        try:
            con.commit()
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()


class LogsSection(Section):
    id, title = "logs", "Логи установки"

    def _logs(self) -> list[Path]:
        d = paths.log_dir()
        try:
            return sorted((f for f in d.glob("install-*.log") if f.is_file()), key=lambda f: f.stat().st_mtime)
        except OSError:
            return []

    def files(self) -> list[Path]:
        return self._logs()

    def path(self) -> str:
        return str(paths.log_dir())

    def oldest(self) -> int | None:
        logs = self._logs()
        return int(logs[0].stat().st_mtime) if logs else None

    def rows(self) -> int:
        return len(self._logs())

    def delete_oldest(self, n: int) -> int:
        done = 0
        for f in self._logs()[:-1][:n]:     # самый свежий (возможно, идущая установка) не трогаем
            try:
                f.unlink()
                done += 1
            except OSError:
                pass
        return done


class DistSection(Section):
    """Версии дистрибутивов клиентов (dist/<клиент>/<версия>): запись — одна версия, самые старые первыми.
    Последняя версия платформы бюджетом не удаляется (иначе zoo-clients скачает её снова и так каждый день,
    а между этим установщика нет); её убирает только явная очистка раздела (keep_latest=False)."""
    id, title = "dist", "Дистрибутивы"
    keep_latest = True

    @property
    def available(self) -> bool:   # type: ignore[override]
        """Раздела нет, пока ни одной группы «ставит ИТ» не было: на странице данных его не видно."""
        return dist.root().is_dir()

    def _versions(self, everything: bool = False) -> list[tuple[bool, float, Path]]:
        out = []
        try:
            clients_ = [d for d in dist.root().iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            return []
        now = time.time()
        for c in clients_:
            latest = dist.newest_per_platform(dist.versions(c.name))
            try:
                for v in c.iterdir():
                    if not v.is_dir() or v.is_symlink():
                        continue
                    mtime = v.stat().st_mtime
                    if not (v / dist.META).exists() and now - mtime < DIST_PARTIAL_AGE:
                        continue   # zoo-clients сейчас качает сюда первый файл (meta.json пишется после него)
                    if everything or not (self.keep_latest and v in latest):
                        out.append((v in latest, mtime, v))
            except OSError:
                pass
        return sorted(out)

    def files(self) -> list[Path]:
        try:
            return [f for f in dist.root().rglob("*") if f.is_file() and not f.is_symlink()]
        except OSError:
            return []

    def path(self) -> str:
        return str(dist.root())

    def oldest(self) -> int | None:
        have = self._versions(everything=True)
        return int(min(m for _, m, _ in have)) if have else None

    def rows(self) -> int:
        return len(self._versions())

    def delete_oldest(self, n: int) -> int:
        done = 0
        for _, _, d in self._versions()[:n]:
            shutil.rmtree(d, ignore_errors=True)
            done += 1
        return done


def sections() -> list[Section]:
    steps_t = [("DELETE FROM traffic WHERE rowid IN (SELECT rowid FROM traffic WHERE res = ? "
                "ORDER BY ts LIMIT ?)", (res,)) for res in traffic.RESOLUTIONS]
    steps_j = [("DELETE FROM hits WHERE rowid IN (SELECT rowid FROM hits WHERE res = ? ORDER BY ts LIMIT ?)",
                (res,)) for res in (journal.RES_1H, journal.RES_1D)]
    steps_j.append(("DELETE FROM ips WHERE rowid IN (SELECT rowid FROM ips ORDER BY last LIMIT ?)", ()))
    return [
        SqliteSection("traffic", "Трафик", traffic.db_path, traffic.connect, "collector.lock",
                      "SELECT MIN(ts) FROM traffic", "SELECT COUNT(*) FROM traffic", steps_t),
        SqliteSection("journal", "Журнал атак", journal.db_path, journal.connect, "journal.lock",
                      "SELECT MIN(ts) FROM hits",
                      "SELECT (SELECT COUNT(*) FROM hits) + (SELECT COUNT(*) FROM ips)", steps_j),
        SqliteSection("probe", "История проб", history.db_path, history.connect, None,
                      "SELECT MIN(ts) FROM reports",
                      ("SELECT COUNT(*) FROM reports", "SELECT COUNT(*) FROM live"),
                      # live — таблица той же базы (D41): дешевле всего терять её, отчёты — после
                      [("DELETE FROM live WHERE rowid IN (SELECT rowid FROM live ORDER BY ts LIMIT ?)", ()),
                       ("DELETE FROM reports WHERE id IN (SELECT id FROM reports ORDER BY ts LIMIT ?)", ())]),
        LogsSection(),
        DistSection(),
    ]


def section(sid: str) -> Section | None:
    return next((s for s in sections() if s.id == sid), None)


# ---------- бюджет и диск ----------

def disk(path: Path | None = None) -> dict[str, int] | None:
    p = Path(path or paths.state_dir())
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        u = shutil.disk_usage(p)
    except OSError:
        return None
    return {"total": u.total, "free": u.free, "used": u.used}


def budget(cfg: Config) -> dict[str, Any]:
    """{limit, configured, capped, too_small}: заданный лимит как есть, умолчание — не больше 5 % диска.
    Заданный меньше MIN_LIMIT не принимается (чистка съела бы всё полезное): берём умолчание."""
    configured = parse_size(cfg.get(LIMIT_KEY))
    too_small = bool(configured) and configured < MIN_LIMIT
    if configured and not too_small:
        return {"limit": configured, "configured": True, "capped": False, "too_small": False}
    d = disk()
    cap = int(d["total"] * MAX_DISK_SHARE) if d else None
    limit = min(DEFAULT_LIMIT, cap) if cap else DEFAULT_LIMIT
    return {"limit": limit, "configured": False, "capped": limit < DEFAULT_LIMIT, "too_small": too_small}


def outside() -> list[dict[str, Any]]:
    """Что занимает место, но в бюджет не входит и не чистится нами."""
    prev = paths.state_dir() / "geo" / "prev"
    geo = geoip.find_dat()
    rows = [
        {"id": "journald", "title": "journald", "size": sum(_du(Path(d), "*.journal*") for d in JOURNALD_DIRS)},
        {"id": "geo-prev", "title": "geo/prev (откат geo-файлов)", "size": _du(prev)},
        {"id": "geoip", "title": "geoip.dat и индекс стран",
         "size": (_size(geo) if geo else 0) + _size(journal.geo_index_file())},
    ]
    return [r for r in rows if r["size"] > 0]


def _state() -> dict[str, Any]:
    try:
        data = json.loads((paths.state_dir() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"trims": {}}
    return data if isinstance(data.get("trims"), dict) else {"trims": {}}


def _note_trim(sid: str, now: int) -> None:
    st = _state()
    mine = [t for t in st["trims"].get(sid, []) if isinstance(t, int) and t > now - KEEP_TRIMS]
    st["trims"][sid] = mine + [now]
    try:
        atomic_write_json(paths.state_dir() / STATE_FILE, st)
    except OSError:
        pass


# ---------- сводка ----------

def report(cfg: Config, with_oldest: bool = True) -> dict[str, Any]:
    b = budget(cfg)
    limit = b["limit"]
    secs = []
    for s in sections():
        size = s.size()
        share_bytes = limit * s.share // 100
        secs.append({"id": s.id, "title": s.title, "path": s.path(), "size": size, "share": s.share,
                     "share_bytes": share_bytes, "over": size > share_bytes, "available": s.available,
                     "oldest": s.oldest() if with_oldest and s.available else None})
    total = sum(x["size"] for x in secs)
    d = disk()
    return {"limit": limit, "configured": b["configured"], "capped": b["capped"], "total": total,
            "over": total > limit, "sections": secs, "outside": outside(),
            "disk": ({**d, "free_pct": round(d["free"] / d["total"] * 100, 1)} if d and d["total"] else None),
            "trims": _state()["trims"]}


def alerts(cfg: Config, now: float | None = None) -> list[tuple[str, str]]:
    now = time.time() if now is None else now
    out: list[tuple[str, str]] = []
    if budget(cfg)["too_small"]:
        out.append(("warn", f"{LIMIT_KEY}={cfg.get(LIMIT_KEY)} меньше {output.human_bytes(MIN_LIMIT)} — "
                            f"не применён, действует умолчание ({output.human_bytes(budget(cfg)['limit'])})"))
    d = disk()
    if d and d["total"] and d["free"] / d["total"] < LOW_DISK:
        pct = d["free"] / d["total"] * 100
        out.append(("bad" if pct < 5 else "warn",
                    f"Мало места на диске: свободно {output.human_bytes(d['free'])} ({pct:.0f} %)"))
    titles = {s.id: s.title for s in sections()}
    for sid, stamps in _state()["trims"].items():
        if sum(1 for t in stamps if isinstance(t, int) and t > now - 86400) >= TOO_OFTEN:
            out.append(("warn", f"«{titles.get(sid, sid)}» чистится чаще раза в сутки — бюджет данных мал "
                                f"(ZOO_DATA_LIMIT, сейчас {output.human_bytes(budget(cfg)['limit'])})"))
    return out


# ---------- чистка ----------

def trim(sec: Section, target: int) -> int:
    """Удалять старейшее, пока раздел не станет ≤ target байт (или нечего удалять). → записей удалено."""
    removed = 0
    lock = paths.state_dir() / sec.lock_name if sec.lock_name else None
    for _ in range(6):
        size = sec.size()
        rows = sec.rows()
        if size <= target or rows <= 0:
            break
        n = min(rows, max(1, math.ceil((size - target) / (size / rows) * 1.2)))
        if lock:
            with file_lock(lock, timeout=60):
                k = sec.delete_oldest(n)
                if k > 0:
                    sec.compact()
        else:
            k = sec.delete_oldest(n)
            if k > 0:
                sec.compact()
        if k <= 0:
            break
        removed += k
    return removed


def clear(sid: str) -> dict[str, Any]:
    sec = section(sid)
    if sec is None:
        raise ValueError(f"нет раздела {sid}: " + ", ".join(SHARES))
    if not sec.available:
        return {"section": sid, "removed": 0, "size": 0}
    before = sec.size()
    if isinstance(sec, DistSection):
        sec.keep_latest = False
    with file_lock(paths.state_dir() / "storage.lock", timeout=60):
        removed = trim(sec, 0)
    return {"section": sid, "removed": removed, "before": before, "size": sec.size()}


def enforce(cfg: Config, now: int | None = None) -> dict[str, Any]:
    """Уложить данные в бюджет. В бюджете — только os.stat, ничего не трогаем."""
    now = int(time.time()) if now is None else now
    limit = budget(cfg)["limit"]
    secs = [s for s in sections() if s.available]
    sizes = {s.id: s.size() for s in secs}
    res: dict[str, Any] = {"limit": limit, "total": sum(sizes.values()), "trimmed": {}, "errors": {}}
    if res["total"] <= limit:
        return res
    goal = int(limit * HYSTERESIS)
    failed: set[str] = set()
    with file_lock(paths.state_dir() / "storage.lock", timeout=60):
        for _ in range(len(secs) * 3):
            total = sum(sizes.values())
            if total <= goal:
                break
            cand = [s for s in secs if s.id not in failed and sizes[s.id] > 0]
            if not cand:
                break
            pick = max(cand, key=lambda s: sizes[s.id] - limit * s.share // 100)
            over = sizes[pick.id] - limit * pick.share // 100
            cut = total - goal if over <= 0 else min(total - goal, over)
            try:
                removed = trim(pick, max(0, sizes[pick.id] - cut))
            except (sqlite3.Error, OSError, LockTimeout) as e:
                res["errors"][pick.id] = str(e)
                failed.add(pick.id)
                continue
            sizes[pick.id] = pick.size()
            if removed <= 0:
                failed.add(pick.id)
                continue
            res["trimmed"][pick.id] = res["trimmed"].get(pick.id, 0) + removed
            _note_trim(pick.id, now)
    res["total"] = sum(sizes.values())
    return res


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--enforce", action="store_true", help="уложить данные в бюджет (запускает zoo-collector)")
    p.add_argument("--clear", metavar="РАЗДЕЛ", help="очистить раздел: " + ", ".join(SHARES))
    p.add_argument("--limit", metavar="РАЗМЕР", help="бюджет данных, например 1G или 500M (ключ ZOO_DATA_LIMIT)")


def _fmt_date(ts: int | None) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts)) if ts else "—"


def _render(d: dict[str, Any]) -> None:
    rows = [[s["title"], output.human_bytes(s["size"]) if s["available"] else "—",
             f"{s['share']} % · {output.human_bytes(s['share_bytes'])}", _fmt_date(s["oldest"])]
            for s in d["sections"]]
    print(output.table(rows, ["раздел", "занято", "доля бюджета", "хранится с"], right=(1,)))
    lim = "ZOO_DATA_LIMIT" if d["configured"] else ("1 ГБ по умолчанию, не больше 5 % диска"
                                                   if d["capped"] else "1 ГБ по умолчанию")
    line = f"занято {output.human_bytes(d['total'])} из {output.human_bytes(d['limit'])} ({lim})"
    if d["disk"]:
        line += f" · свободно на диске {output.human_bytes(d['disk']['free'])}"
    print("\n" + line)
    if d["outside"]:
        print("вне бюджета: " + ", ".join(f"{o['title']} {output.human_bytes(o['size'])}" for o in d["outside"]))


def cmd_storage(args: argparse.Namespace, cfg: Config) -> int:
    if args.limit is not None:
        n = parse_size(args.limit)
        if n is None or n < MIN_LIMIT:
            output.error(f"не понял размер «{args.limit}» или он меньше 16M: пример 1G, 500M")
            return 2
        config_set(LIMIT_KEY, format_size(n))
        output.ok(f"бюджет данных: {output.human_bytes(n)} ({LIMIT_KEY}={format_size(n)})")
        return 0
    if args.clear:
        try:
            res = clear(args.clear)
        except ValueError as e:
            output.error(str(e))
            return 2
        except (sqlite3.Error, OSError, LockTimeout) as e:
            output.error(f"не очистил: {e}")
            return 1
        output.emit(res, args.json, lambda r: print(f"{r['section']}: удалено записей {r['removed']}"))
        return 0
    if args.enforce:
        res = enforce(cfg)
        # ошибка раздела не валит юнит коллектора: остальные уже вычищены
        output.emit(res, args.json, lambda r: print(
            "в бюджете" if not r["trimmed"] and not r["errors"] else
            "почищено: " + ", ".join(f"{k} {v}" for k, v in r["trimmed"].items())
            + "".join(f"; ошибка {k}: {v}" for k, v in r["errors"].items())))
        return 0
    output.emit(report(cfg), args.json, _render)
    return 0
