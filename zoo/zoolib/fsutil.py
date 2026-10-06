"""Атомарная запись файлов и блокировки (flock)."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: только для локальных юнит-тестов
    fcntl = None  # type: ignore[assignment]


class LockTimeout(Exception):
    pass


class UnsafePath(Exception):
    """Каталог заявок подменён ссылкой: root-исполнитель туда не пойдёт."""


NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)   # на Windows (юнит-тесты) флага нет


def check_real_dirs(*dirs: Path) -> None:
    """Исполнитель работает от root в каталогах, куда может писать админка: если любой из них стал ссылкой
    (или не каталогом), его файлы оказались бы там, куда ссылка указывает. Нет каталога — не ошибка."""
    for d in dirs:
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            continue
        except OSError as e:
            raise UnsafePath(f"{d}: {e.strerror or e}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{d} — не обычный каталог (ссылка?), заявки не выполняются")


def read_text_nofollow(path: Path, limit: int = 64 * 1024) -> str:
    """Прочитать небольшой обычный файл, не идя по ссылке (OSError — ссылка, не файл или нет доступа)."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError("не обычный файл")
    fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        return f.read(limit).decode("utf-8", "replace")


def open_append_nofollow(path: Path) -> Any:
    """Файл для дописывания (создаётся 0600), ссылку по пути не открывает."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW, 0o600), "ab")


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0, nofollow: bool = False) -> Iterator[None]:
    """Эксклюзивный flock на файле path; ждёт до timeout секунд. nofollow — не открывать ссылку
    (для root-исполнителей в каталогах, доступных админке)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | (NOFOLLOW if nofollow else 0), 0o600)
    try:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"не дождался блокировки {path} за {timeout:.0f} с") from None
                    time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def atomic_write_text(path: Path, data: str, mode: int = 0o600) -> None:
    """Запись через временный файл в том же каталоге и rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n", mode)


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
