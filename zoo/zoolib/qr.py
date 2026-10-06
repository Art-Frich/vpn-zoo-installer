"""QR-коды через qrencode (ставит фаза 00). Текст идёт через stdin, не через argv:
в ссылках ключи, а argv виден всем процессам."""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections import OrderedDict

# Предел QR версии 40 с уровнем L — 2953 байта; AWG .conf и ссылки заметно меньше
MAX_BYTES = 2900


class QrError(Exception):
    pass


def available() -> bool:
    return shutil.which("qrencode") is not None


def _encode(text: str, fmt: str, extra: list[str] | None = None) -> str:
    if not available():
        raise QrError("нет qrencode (apt install qrencode)")
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise QrError(f"слишком длинно для QR: {len(data)} байт")
    try:
        cp = subprocess.run(["qrencode", "-t", fmt, "-o", "-", *(extra or [])], input=data,
                            capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise QrError(f"qrencode: {e}") from None
    if cp.returncode != 0:
        raise QrError("qrencode: " + cp.stderr.decode("utf-8", "replace").strip())
    return cp.stdout.decode("utf-8", "replace")


def utf8(text: str, invert: bool = False) -> str:
    """QR для терминала. invert=True — для светлого фона терминала."""
    return _encode(text, "UTF8i" if invert else "UTF8", ["-m", "2"])


SVG_CACHE_SIZE = 64
_svg_cache: "OrderedDict[tuple[str, int], str]" = OrderedDict()
_svg_lock = threading.Lock()


def clear_cache() -> None:
    with _svg_lock:
        _svg_cache.clear()


def svg(text: str, size: int = 6) -> str:
    """SVG-разметка (для веб-админки и файлов). --svg-path (qrencode >= 4.1) рисует модули
    одним path, --rle склеивает соседние модули: файл в десятки раз меньше. Результат
    кэшируется по содержимому (LRU на 64): запуск qrencode на каждый показ был медленным."""
    key = (text, size)
    with _svg_lock:
        if key in _svg_cache:
            _svg_cache.move_to_end(key)
            return _svg_cache[key]
    try:
        out = _encode(text, "SVG", ["-s", str(size), "-m", "2", "--svg-path", "--rle"])
    except QrError:
        out = _encode(text, "SVG", ["-s", str(size), "-m", "2", "--rle"])
    with _svg_lock:
        _svg_cache[key] = out
        while len(_svg_cache) > SVG_CACHE_SIZE:
            _svg_cache.popitem(last=False)
    return out
