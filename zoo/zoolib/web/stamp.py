"""Отпечаток данных страницы для live (/api/stamp): app.js раз в 10 с спрашивает его и
перезапрашивает страницу, только если он изменился. Только os.stat файлов, без SQL и без рендера.

Карта «страница → источники» — PAGES. SQLite в режиме WAL меняет сначала «-wal», основной
файл — при контрольной точке, поэтому смотрим оба."""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import journal, paths, traffic
from .. import probe as probe_mod
from . import logs

if TYPE_CHECKING:
    from .app import App

JOURNALD = (Path("/var/log/journal"), Path("/run/log/journal"))
SCAN_LIMIT = 200  # записей каталога на источник: потолок на случай мусора в каталоге


def _now() -> float:
    return time.time()


def _db(p: Path) -> list[Path]:
    return [p, p.with_name(p.name + "-wal")]


# источник → пути (функции: окружение читается при каждом вызове)
SOURCES: dict[str, Callable[[], list[Path]]] = {
    "traffic": lambda: _db(traffic.db_path()),
    "journal": lambda: _db(journal.db_path()),
    "probe": lambda: _db(probe_mod.history.db_path()),
    "selftest": lambda: [probe_mod.selftest_file(), paths.probe_export_file()],
    "users": lambda: [paths.users_file()],
    "clients": lambda: [paths.clients_dir()],
    "manifests": lambda: [paths.manifest_dir()],
    "config": lambda: [paths.config_file()],
    "allowlist": lambda: [paths.allowlist_file()],
    "groups": lambda: [paths.groups_file()],
    "logs": lambda: [logs.log_dir()],
}
JOURNALD_BUCKET = 30  # journald пишет постоянно: логи подтягиваются не чаще раза в 30 с

# (шаблон пути, источники, окно в секундах: страница с «N мин назад» или метриками
# обновляется и без изменений данных; 0 — нет). Последняя строка — всё прочее.
PAGES: list[tuple[re.Pattern[str], tuple[str, ...], int]] = [(re.compile(p), s, w) for p, s, w in [
    (r"/", ("traffic", "journal", "probe", "selftest", "users", "manifests", "config"), 10),  # CPU, память
    (r"/users", ("users", "groups", "clients", "manifests", "traffic"), 60),
    (r"/users/[^/]+(/.*)?", ("users", "groups", "clients", "manifests", "traffic", "allowlist", "probe"), 60),
    (r"/groups(/[^/]+)?", ("groups", "users", "allowlist", "manifests", "clients", "probe"), 60),
    (r"/connect/.*", ("groups", "users", "allowlist", "manifests", "clients", "probe"), 60),
    (r"/apps", ("allowlist", "users"), 0),
    (r"/traffic", ("traffic", "manifests", "users"), 0),
    (r"/probe", ("probe", "selftest", "manifests", "config"), 60),
    (r"/journal", ("journal",), 60),
    (r"/logs", ("logs",), 0),
    (r"/settings", ("config", "manifests"), 30),  # состояния юнитов не в файлах
    (r".*", tuple(SOURCES), 60),
]]
JOB_RE = re.compile(r"/jobs/(\d+)")
JOBS_PAGES = ("/probe", "/settings")


def _stat(p: Path) -> Any:
    try:
        st = p.stat()
    except OSError:
        return None
    if p.name.endswith("-wal") and not st.st_size:
        return None  # пустой «-wal» бывает у читателя базы: не данные
    if not os.path.isdir(p):
        return (st.st_mtime_ns, st.st_size)
    kids = []
    try:
        with os.scandir(p) as it:
            for n, e in enumerate(it):
                if n >= SCAN_LIMIT:
                    break
                try:
                    s = e.stat()
                    kids.append((e.name, s.st_mtime_ns, s.st_size))
                except OSError:
                    continue
    except OSError:
        pass
    return (st.st_mtime_ns, sorted(kids))


def _journald() -> list[Any]:
    out = []
    for base in JOURNALD:
        try:
            for d in list(os.scandir(base))[:SCAN_LIMIT]:
                try:
                    out.append(os.stat(os.path.join(d.path, "system.journal")).st_mtime_ns // (JOURNALD_BUCKET * 10**9))
                except OSError:
                    continue
        except OSError:
            continue
    return out


def _jobs(app: "App") -> list[Any]:
    try:
        return [(j.id, j.running) for j in app.jobs.recent(10)]
    except RuntimeError:  # словарь задач изменился во время обхода: в следующий раз
        return []


def compute(app: "App", page: str) -> str:
    """Короткий отпечаток данных страницы page (путь без query)."""
    page = "/" + page.split("?", 1)[0].strip("/")
    parts: list[Any] = []
    job = JOB_RE.fullmatch(page)
    if job:
        j = app.jobs.get(int(job.group(1)))
        parts.append(None if j is None else (j.running, j.rc, len(j.stdout), len(j.stderr), len(j.error)))
        window = 0
    else:
        names, window = next((s, w) for rx, s, w in PAGES if rx.fullmatch(page))
        for n in names:
            parts.append([_stat(p) for p in SOURCES[n]()])
        if "logs" in names:
            parts.append(_journald())
        if page in JOBS_PAGES:
            parts.append(_jobs(app))
    if window:
        parts.append(int(_now() // window))
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:12]
