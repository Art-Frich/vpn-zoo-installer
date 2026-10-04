"""Журналы для админки: логи инсталлера и journald юнитов, с маскировкой секретов."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import Config
from ..system import run

MASK = "•••"
SECRET_KEY_RE = re.compile(r"(PASS|SECRET|TOKEN|KEY|PSK|UUID|PRIV|SALT|AUTH|PIN|SHORT_?ID|_SID$|_USER$|SUB_?PATH|PANEL_PATH)",
                           re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PATTERNS = [
    # ссылки протоколов целиком: в них ключи и пароли
    (re.compile(r"\b(vless|vmess|trojan|ss|hysteria2|hy2|tuic|vpn|wireguard|awg)://[^\s\"'<>]+", re.I), r"\1://" + MASK),
    (re.compile(r"(Authorization\s*:\s*)(\S.*)", re.I), r"\1" + MASK),
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1" + MASK),
    (re.compile(r"\b(PrivateKey|PresharedKey|Password)(\s*[=:]\s*)\S+", re.I), r"\1\2" + MASK),
    (re.compile(r"(\"?(?:password|passwd|secret|token|apiToken|privateKey|psk|auth|uuid)\"?\s*[:=]\s*\"?)"
                r"([^\s\",}]{6,})", re.I), r"\1" + MASK),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), MASK),
]


def secret_values(cfg: Config) -> list[str]:
    """Значения секретных ключей config.env (длинные сначала: подстроки не мешают)."""
    vals = {v for k, v in cfg.values.items() if SECRET_KEY_RE.search(k) and len(v) >= 6}
    return sorted(vals, key=len, reverse=True)


def sanitize(text: str, cfg: Config | None = None) -> str:
    text = _ANSI_RE.sub("", text)
    for v in secret_values(cfg) if cfg else ():
        text = text.replace(v, MASK)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


def log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR", "/var/log/vpn-zoo"))


def log_files(limit: int = 30) -> list[Path]:
    d = log_dir()
    try:
        files = [f for f in d.iterdir() if f.is_file() and f.suffix == ".log"]
    except OSError:
        return []
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:limit]


def tail_file(path: Path, lines: int = 400, max_bytes: int = 512 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError as e:
        return f"не прочитать {path}: {e}"
    text = data.decode("utf-8", "replace")
    out = text.splitlines()[-lines:]
    if size > max_bytes and len(out) == len(text.splitlines()):
        out = out[1:]  # первая строка могла обрезаться
    return "\n".join(out)


def journal(unit: str, lines: int = 400) -> str:
    rc, out, err = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=15)
    if rc == 127:
        return "journalctl недоступен"
    return out if out.strip() else (err.strip() or "записей нет")
