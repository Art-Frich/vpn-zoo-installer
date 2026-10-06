"""Чистка логов из админки (D45).

Логи установки (`install-*.log` в LOG_DIR) админка удаляет сама: каталог входит в ReadWritePaths
zoo-web.service, а имена проверяются по белому списку (только install-*.log, обычные файлы, не ссылки;
самый свежий файл не трогается — там может идти установка).

journald — другое дело: `journalctl --vacuum-*` пишет в /var/log/journal, а песочница админки
(ProtectSystem=strict) его не открывает. Поэтому админка только кладёт заявку `logs-req/<uuid>.json`,
`zoo-logs.path` запускает `zoo-logs.service` (root) → `zoo logs run`. Что можно запросить, решает
таблица ACTIONS (действие и значение из фиксированного набора), из заявки берутся только они:

    logs-req/<uuid>.json         заявка {id, action, value, created}
    logs-req/state/<uuid>.json   итог: ok | fail, сколько освобождено, ошибка
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import output, paths, system
from .fsutil import LockTimeout, atomic_write_json, file_lock

JOURNALD_DIRS = ("/var/log/journal", "/run/log/journal")
INSTALL_RE = re.compile(r"^install-[A-Za-z0-9._-]{1,100}\.log\Z")
ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
KEEP_STATES = 20
PENDING_STALE = 10 * 60
RUN_TIMEOUT = 300
OLDER_DAYS = (1, 7, 30, 90)

# действие → (ключ journalctl, допустимые значения, подпись)
ACTIONS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "vacuum-time": ("--vacuum-time", ("1d", "3d", "7d", "14d", "30d", "90d"), "старше"),
    "vacuum-size": ("--vacuum-size", ("50M", "100M", "200M", "500M", "1G"), "оставить не больше"),
}
VALUE_TITLES = {"1d": "1 дня", "3d": "3 дней", "7d": "7 дней", "14d": "14 дней", "30d": "30 дней", "90d": "90 дней"}


class CleanError(Exception):
    """Очистку нельзя принять или выполнить; текст — по-русски, для человека."""


def req_dir() -> Path:
    return paths.state_dir() / "logs-req"


def state_dir() -> Path:
    return req_dir() / "state"


def _du(base: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(base):
        for name in files:
            if ".journal" not in name:
                continue
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def journald_size() -> int:
    return sum(_du(Path(d)) for d in JOURNALD_DIRS)


def label(action: str, value: str) -> str:
    if action == "vacuum-time":
        return f"записи старше {VALUE_TITLES.get(value, value)}"
    return f"оставить не больше {value}"


def check(action: str, value: str) -> tuple[str, str]:
    spec = ACTIONS.get(action)
    if spec is None or value not in spec[1]:
        raise CleanError("такой очистки журнала нет")
    return spec[0], value


# ---------- логи установки (админка делает сама) ----------

def install_logs() -> list[Path]:
    """install-*.log, старые первыми."""
    d = paths.log_dir()
    try:
        files = [f for f in d.iterdir() if INSTALL_RE.match(f.name) and f.is_file() and not f.is_symlink()]
        return sorted(files, key=lambda f: (f.stat().st_mtime, f.name))
    except OSError:
        return []


def clean_files(names: list[str] | None = None, older_days: int | None = None, keep: int = 1,
                now: float | None = None) -> dict[str, Any]:
    """Удалить логи установки: выбранные по имени и/или старше older_days. Самые свежие `keep` (не меньше одного)
    остаются всегда. → {deleted: [имена], freed: байты, skipped: [имена], errors: [текст]}."""
    now = time.time() if now is None else now
    files = install_logs()
    protected = {f.name for f in files[max(0, len(files) - max(1, keep)):]}
    by_name = {f.name: f for f in files}
    pick: dict[str, Path] = {}
    out: dict[str, Any] = {"deleted": [], "freed": 0, "skipped": [], "errors": []}
    for n in names or []:
        if n in by_name:
            pick[n] = by_name[n]
        else:
            out["errors"].append(f"{n[:60]}: такого лога установки нет")
    if older_days is not None:
        if older_days not in OLDER_DAYS:
            raise CleanError("такого срока нет")
        for f in files:
            try:
                if f.stat().st_mtime < now - older_days * 86400:
                    pick[f.name] = f
            except OSError:
                continue
    for name, f in pick.items():
        if name in protected:
            out["skipped"].append(name)
            continue
        try:
            size = f.stat().st_size
            f.unlink()
        except OSError as e:
            out["errors"].append(f"{name}: {e.strerror or e}")
            continue
        out["deleted"].append(name)
        out["freed"] += size
    return out


# ---------- journald (заявка → root) ----------

def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pending() -> list[dict[str, Any]]:
    out = []
    try:
        names = sorted(os.listdir(req_dir()))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and ID_RE.match(n[:-5]):
            d = _read(req_dir() / n)
            if d and d.get("id") == n[:-5]:
                out.append({"id": d["id"], "action": d.get("action"), "value": d.get("value"),
                            "created": d.get("created") or 0})
    return out


def states(limit: int = 5) -> list[dict[str, Any]]:
    """Итоги прошлых очисток, новые первыми."""
    out = []
    try:
        files = [f for f in state_dir().iterdir() if f.suffix == ".json" and ID_RE.match(f.stem)]
    except OSError:
        return out
    for f in sorted(files, key=lambda f: -f.stat().st_mtime)[:limit]:
        d = _read(f)
        if d and d.get("id") == f.stem:
            out.append(d)
    return out


def submit(action: str, value: str, now: float | None = None) -> str:
    """Принять заявку на очистку журнала: проверка по белому списку, одна за раз. → id."""
    check(action, value)
    now = time.time() if now is None else now
    req_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with file_lock(req_dir() / "submit.lock", timeout=10):
            for p in pending():
                if now - p["created"] < PENDING_STALE:
                    raise CleanError("Предыдущая очистка журнала ещё не выполнена — подождите минуту.")
            rid = uuid.uuid4().hex
            atomic_write_json(req_dir() / f"{rid}.json",
                              {"id": rid, "action": action, "value": value, "created": int(now)}, 0o600)
    except LockTimeout:
        raise CleanError("Заявки принимаются по одной, повторите через несколько секунд.") from None
    return rid


# ---------- исполнитель (zoo logs run: root, вне песочницы) ----------

def vacuum(action: str, value: str) -> dict[str, Any]:
    """journalctl --rotate и --vacuum-*; → {freed, before, after}. CleanError — не удалось."""
    flag, value = check(action, value)
    before = journald_size()
    system.run(["journalctl", "--rotate"], timeout=60)   # активный файл станет архивным и попадёт под чистку
    rc, _out, err = system.run(["journalctl", f"{flag}={value}"], timeout=RUN_TIMEOUT)
    if rc == 127:
        raise CleanError("journalctl недоступен")
    if rc != 0:
        raise CleanError((err.strip() or f"journalctl вернул код {rc}")[:300])
    after = journald_size()
    return {"freed": max(0, before - after), "before": before, "after": after}


def _prune() -> None:
    files = sorted(state_dir().glob("*.json"), key=lambda f: -f.stat().st_mtime)
    for f in files[KEEP_STATES:]:
        try:
            f.unlink()
        except OSError:
            pass


def _run_one(req: Path) -> None:
    rid = req.stem
    data = _read(req) or {}
    st: dict[str, Any] = {"id": rid, "action": str(data.get("action") or ""), "value": str(data.get("value") or ""),
                          "started": int(time.time())}
    try:
        if data.get("id") != rid:
            raise CleanError("заявка повреждена")
        st.update(vacuum(st["action"], st["value"]), status="ok")
    except CleanError as e:
        st.update(status="fail", error=str(e))
    st["finished"] = int(time.time())
    state_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(state_dir() / f"{rid}.json", st, 0o600)
    try:
        req.unlink()
    except OSError:
        pass
    _prune()


def run_queue() -> int:
    """Выполнить заявки по порядку; уже работающий исполнитель — выйти (он дочитает очередь)."""
    req_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with file_lock(req_dir() / "runner.lock", timeout=0):
            for _ in range(10):
                reqs = sorted((f for f in req_dir().glob("*.json") if ID_RE.match(f.stem)),
                              key=lambda f: f.stat().st_mtime)
                if not reqs:
                    break
                _run_one(reqs[0])
    except LockTimeout:
        pass
    return 0


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", nargs="?", choices=("run", "list", "clean", "vacuum"), default="list",
                   help="run — выполнить заявки админки (zoo-logs.service), list — итоги, "
                        "clean — удалить логи установки, vacuum — почистить журнал journald (root)")
    p.add_argument("--older", type=int, choices=OLDER_DAYS, metavar="ДНЕЙ", help="clean: старше стольких дней")
    p.add_argument("--keep", type=int, default=1, metavar="N", help="clean: оставить N самых свежих (не меньше 1)")
    p.add_argument("--time", choices=ACTIONS["vacuum-time"][1], dest="vtime", help="vacuum: записи старше срока")
    p.add_argument("--size", choices=ACTIONS["vacuum-size"][1], dest="vsize", help="vacuum: оставить не больше")


def cmd_logs(args: argparse.Namespace, cfg: Any) -> int:
    if args.action == "run":
        return run_queue()
    if args.action == "clean":
        if args.older is None and args.keep <= 1:
            output.error("укажите --older ДНЕЙ или --keep N")
            return 2
        names = [f.name for f in install_logs()] if args.keep > 1 else None
        res = clean_files(names, args.older, args.keep)
        if args.json:
            output.print_json(res)
        else:
            output.info(f"удалено: {len(res['deleted'])}, освобождено {output.human_bytes(res['freed'])}")
        return 0
    if args.action == "vacuum":
        action, value = ("vacuum-time", args.vtime) if args.vtime else ("vacuum-size", args.vsize)
        if not value:
            output.error("укажите --time или --size")
            return 2
        try:
            res = vacuum(action, value)
        except CleanError as e:
            output.error(str(e))
            return 1
        if args.json:
            output.print_json(res)
        else:
            output.info(f"освобождено {output.human_bytes(res['freed'])} (было {output.human_bytes(res['before'])})")
        return 0
    rows = pending() + states()
    if args.json:
        output.print_json(rows)
    elif rows:
        print(output.table([[(r.get("id") or "")[:8], label(str(r.get("action")), str(r.get("value"))),
                             r.get("status") or "ждёт", output.human_bytes(int(r.get("freed") or 0))]
                            for r in rows], ["заявка", "очистка", "итог", "освобождено"]))
    else:
        output.info("очисток журнала не было")
    return 0
