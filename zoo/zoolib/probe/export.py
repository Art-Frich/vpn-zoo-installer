"""Выгрузка истории для history/ в репозитории: анонимные строки и зашифрованные сырые отчёты.

Раскладка (одна и та же на сервере, в контейнере пробника и в репо):
    YYYY-MM.jsonl        по строке на пару «прогон, протокол»; ключи — в history/README.md
    raw/<id>.json.age    полный отчёт (с IP!), age на ключи из recipients.txt (armor, текст)

Анонимизация: в строку попадают только перечисленные поля; каждое текстовое значение проходит
scrub() (IPv4/IPv6 → «x»); время округлено до часа (UTC). Сырые отчёты читает только владелец
приватных ключей из recipients.txt. Шифрует бинарь age (versions.env, фаза 09 кладёт его в
/usr/local/lib/vpn-zoo/bin, в образе zoo-probe — в PATH): на чистом stdlib age с ключами SSH не сделать.
Повторная выгрузка безопасна: строки объединяются без дублей, существующие .age не перезаписываются.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import output
from ..fsutil import atomic_write_text
from . import context as ctx_mod
from . import history, metrics

AGE_BIN_SERVER = "/usr/local/lib/vpn-zoo/bin/age"
RECIPIENT_KINDS = ("ssh-ed25519", "ssh-rsa", "age1")
PRIVATE_MARKERS = ("AGE-SECRET-KEY-", "-----BEGIN", "AGE-PLUGIN-")
NUMS = ("latency_ms", "p90_ms", "jitter_ms", "loss_pct", "rtt_ms", "down_mbps", "up_mbps")


class ExportError(history.HistoryError):
    pass


# ---------- анонимизация ----------

def scrub(value: Any) -> Any:
    """Строка без IP-адресов; не строки — как есть."""
    if not isinstance(value, str):
        return value
    return ctx_mod.IPV6_RE.sub("x", ctx_mod.IPV4_RE.sub("x", value))


def hour_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%HZ")


def rows_for_report(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """Анонимные строки отчёта. Известные поля вычищены; адреса сервера, прямой IP и IP выхода не берутся."""
    c = history.context_of(rep)
    pid = history.report_uid(rep)
    label = ctx_mod.clean_text(rep.get("label"), 40)
    base: dict[str, Any] = {"ts": hour_label(history.report_ts(rep)), "id": pid,
                            "mode": "local" if rep.get("mode") == "local" else "remote",
                            "server": scrub(label) or "server"}
    for k in ("isp", "asn", "country", "net", "tag", "device"):
        if c.get(k) is not None:
            base[k] = scrub(c[k])
    rows = []
    for r in rep["results"]:
        row = dict(base, proto=scrub(r["id"]), verdict=scrub(r["verdict"]))
        for k, v in metrics.result_numbers(r).items():
            if k in NUMS and isinstance(v, (int, float)):
                row[k] = v
        rows.append(row)
    return rows


def to_line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def no_ip(row: dict[str, Any]) -> bool:
    """Контроль на выходе: ни в одном текстовом значении нет IP-подобной подстроки."""
    return not any(isinstance(v, str) and (ctx_mod.IPV4_RE.search(v) or ctx_mod.IPV6_RE.search(v))
                   for v in row.values())


# ---------- age ----------

def find_age() -> str | None:
    env = os.environ.get("ZOO_AGE_BIN", "")
    if env:
        return env if os.access(env, os.X_OK) else None
    if os.access(AGE_BIN_SERVER, os.X_OK):
        return AGE_BIN_SERVER
    return shutil.which("age")


def parse_recipients(text: str, allow_empty: bool = False) -> list[str]:
    """Строки публичных ключей из recipients.txt. Приватный ключ в файле — ошибка (его нельзя ни хранить в репо, ни слать)."""
    keys = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if any(s.startswith(m) for m in PRIVATE_MARKERS):
            raise ExportError("в списке получателей приватный ключ — нужны только публичные "
                              "(ssh-ed25519 …, ssh-rsa …, age1…)")
        if not s.startswith(RECIPIENT_KINDS):
            raise ExportError(f"получатель не распознан: {s[:40]!r} (ssh-ed25519, ssh-rsa или age1…)")
        keys.append(s)
    if not keys and not allow_empty:
        raise ExportError("в списке получателей нет ключей: добавьте публичный ключ "
                          "(cat ~/.ssh/id_ed25519.pub) в history/recipients.txt")
    return keys


def encrypt(data: bytes, recipients: list[str], age_bin: str) -> bytes:
    """age -a -R файл: шифрование на публичные ключи; файл получателей — временный."""
    with tempfile.TemporaryDirectory(prefix="zoo-age-") as d:
        rf = Path(d) / "recipients.txt"
        rf.write_text("\n".join(recipients) + "\n", encoding="utf-8")
        cp = subprocess.run([age_bin, "-a", "-R", str(rf)], input=data, capture_output=True, timeout=60)
    if cp.returncode != 0:
        raise ExportError(f"age: {cp.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return cp.stdout


# ---------- запись ----------

def _merge_month(path: Path, lines: Iterable[str]) -> int:
    """Дописать строки в файл месяца без дублей, порядок — по строке (ts первым ключом = по времени). → число новых."""
    old = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
    new = set(lines) - old
    if new:
        atomic_write_text(path, "\n".join(sorted(old | new)) + "\n", 0o644)
    return len(new)


def write_dir(out: Path, reports: Iterable[dict[str, Any]], recipients: list[str] | None = None,
              age_bin: str | None = None) -> dict[str, int]:
    """Дополнить out отчётами. recipients=None — только jsonl. → счётчики."""
    if recipients is not None and not age_bin:
        raise ExportError("нет бинаря age: фаза 09 ставит его на сервер (/usr/local/lib/vpn-zoo/bin/age), "
                          "на своей машине — winget install FiloSottile.age; без шифрования — --no-raw")
    by_month: dict[str, list[str]] = {}
    stats = {"reports": 0, "rows": 0, "raw_new": 0, "raw_have": 0, "dropped": 0}
    out.mkdir(parents=True, exist_ok=True)
    for rep in reports:
        stats["reports"] += 1
        for row in rows_for_report(rep):
            if not no_ip(row):
                stats["dropped"] += 1
                continue
            by_month.setdefault(row["ts"][:7], []).append(to_line(row))
        if recipients is not None and age_bin:
            f = out / "raw" / f"{history.report_uid(rep)}.json.age"
            if f.exists():
                stats["raw_have"] += 1
                continue
            blob = encrypt(history.canonical(rep).encode("utf-8"), recipients, age_bin)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(blob)
            stats["raw_new"] += 1
    for month, lines in by_month.items():
        stats["rows"] += _merge_month(out / f"{month}.jsonl", lines)
    return stats


def tar_to(out: Path, fileobj: Any) -> None:
    with tarfile.open(fileobj=fileobj, mode="w|") as tf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                info = tf.gettarinfo(str(f), arcname=f.relative_to(out).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mode = 0o644
                with open(f, "rb") as fh:
                    tf.addfile(info, fh)


# ---------- команды ----------

def load_recipients_arg(value: str | None) -> list[str]:
    if not value:
        raise ExportError("укажите --recipients FILE (history/recipients.txt) или --no-raw")
    try:
        text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    except OSError as e:
        raise ExportError(f"{value}: {e}") from None
    return parse_recipients(text)


def cmd_export(reps: list[dict[str, Any]], args: argparse.Namespace) -> int:
    recipients = None if args.no_raw else load_recipients_arg(args.recipients)
    age_bin = find_age() if recipients is not None else None
    if not args.out and not args.tar:
        raise ExportError("укажите --out DIR или --tar")
    # служебные сообщения — в stderr: в режиме --tar stdout занят архивом
    say = (lambda m: print(m, file=sys.stderr)) if args.tar else output.info
    if args.tar:
        with tempfile.TemporaryDirectory(prefix="zoo-hist-") as d:
            stats = write_dir(Path(d), reps, recipients, age_bin)
            tar_to(Path(d), sys.stdout.buffer)
    else:
        stats = write_dir(Path(args.out), reps, recipients, age_bin)
    if args.json and not args.tar:
        output.print_json(stats)
        return 0
    say(f"отчётов: {stats['reports']}, новых строк jsonl: {stats['rows']}"
        + (f", сырых .age: {stats['raw_new']} (уже были {stats['raw_have']})" if recipients is not None else ""))
    if stats["dropped"]:
        say(f"отброшено строк с IP-подобным текстом: {stats['dropped']}")
    return 0


def decrypt(blob: bytes, identity: str, age_bin: str) -> bytes:
    """Для тестов и ручной проверки: age -d -i ключ."""
    cp = subprocess.run([age_bin, "-d", "-i", identity], input=blob, capture_output=True, timeout=60)
    if cp.returncode != 0:
        raise ExportError(f"age -d: {cp.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return cp.stdout

