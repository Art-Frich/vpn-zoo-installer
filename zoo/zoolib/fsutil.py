"""Атомарная запись файлов и блокировки (flock)."""

from __future__ import annotations

import contextlib
import json
import os
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


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Эксклюзивный flock на файле path; ждёт до timeout секунд."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
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
