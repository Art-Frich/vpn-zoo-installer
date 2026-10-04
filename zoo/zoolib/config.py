"""/etc/vpn-setup/config.env: чтение (shlex) и атомарная запись ключа.

Формат пишет config_set из scripts/lib.sh: KEY='значение', ' внутри → '\\''.
Строки первой версии инсталлера (KEY="v", KEY=v) тоже читаются.
Подстановки $VAR не раскрываются: config_set их не пишет.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .fsutil import file_lock

KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HEADER = "# vpn-zoo config — ключи пишет config_set, правка руками допустима"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    values: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    errors: list[str] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def flag(self, key: str, default: bool = False) -> bool:
        """ENABLE_*=1 → True. Отсутствующий ключ → default."""
        if key not in self.values:
            return default
        return self.values[key].strip().lower() in ("1", "true", "yes", "on")

    def int(self, key: str, default: int | None = None) -> int | None:
        try:
            return int(self.values[key])
        except (KeyError, ValueError):
            return default


def parse_line(line: str) -> tuple[str, str] | None:
    """KEY=value → (KEY, value); комментарии и пустые строки → None. ValueError при мусоре."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("export "):
        s = s[len("export "):].lstrip()
    key, sep, rest = s.partition("=")
    if not sep or not KEY_RE.match(key):
        raise ValueError(f"не KEY=value: {line.rstrip()[:80]}")
    tokens = shlex.split(rest, comments=True, posix=True)
    return key, " ".join(tokens)


def parse_text(text: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        try:
            kv = parse_line(line)
        except ValueError as e:
            errors.append(f"строка {n}: {e}")
            continue
        if kv:
            # как при source: последнее присваивание побеждает
            values[kv[0]] = kv[1]
    return values, errors


def load(path: Path | None = None) -> Config:
    """Прочитать config.env. Нет файла → пустой Config (без исключения)."""
    path = Path(path) if path else paths.config_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return Config(path=path)
    except PermissionError as e:
        raise ConfigError(f"нет доступа к {path} — запусти через sudo") from e
    values, errors = parse_text(text)
    return Config(values=values, path=path, errors=errors)


def quote(value: str) -> str:
    """Как _config_quote в lib.sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def config_set(key: str, value: str, path: Path | None = None) -> None:
    """Атомарно записать один ключ, остальные строки не трогать (как config_set в lib.sh)."""
    if not KEY_RE.match(key):
        raise ConfigError(f"недопустимое имя ключа: {key}")
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ConfigError(f"перевод строки в значении {key}")
    path = Path(path) if path else paths.config_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    new_line = f"{key}={quote(value)}"
    with file_lock(paths.config_lock_file()):
        try:
            old = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            old = None
        if old is None:
            out = [HEADER, new_line]
        else:
            out, done = [], False
            for line in old:
                if line.startswith(key + "="):
                    if not done:
                        out.append(new_line)
                        done = True
                    continue
                out.append(line)
            if not done:
                out.append(new_line)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(out) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
