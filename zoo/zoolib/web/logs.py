"""Журналы для админки: логи инсталлера и journald юнитов, с маскировкой секретов."""

from __future__ import annotations

import re
from typing import Callable

from .. import logread
from ..config import Config

MASK = "•••"
SECRET_KEY_RE = re.compile(r"(PASS|SECRET|TOKEN|KEY|PSK|UUID|PRIV|SALT|AUTH|PIN|SHORT_?ID|_SID$|_USER$|SUB_?PATH|PANEL_PATH)",
                           re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# QR из блочных символов (qrencode -t UTF8 в журнале установки): прячем каждую строку отдельно, а не «три подряд»,
# иначе строка поиска, край страницы или выгрузка по одной строке показали бы кусок QR
_QR_RE = re.compile(r"^(?=[ ]*[█▀▄])[ █▀▄]{8,}(?=\r?$)", re.M)
_PATTERNS = [
    # ссылки протоколов целиком: в них ключи и пароли
    (re.compile(r"\b(vless|vmess|trojan|ss|hysteria2|hy2|tuic|vpn|wireguard|awg|tg)://[^\s\"'<>]+", re.I), r"\1://" + MASK),
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


def cleaner(cfg: Config | None = None) -> Callable[[str], str]:
    """sanitize с заранее собранными секретами config.env: для поиска по сотням тысяч строк."""
    secrets = secret_values(cfg) if cfg else []

    def clean(text: str) -> str:
        text = _ANSI_RE.sub("", text)
        text = _QR_RE.sub("[QR скрыт]", text)
        for v in secrets:
            text = text.replace(v, MASK)
        for rx, repl in _PATTERNS:
            text = rx.sub(repl, text)
        return text
    return clean


def sanitize(text: str, cfg: Config | None = None) -> str:
    return cleaner(cfg)(text)


log_dir = logread.log_dir
log_files = logread.log_files
