"""QR-коды через qrencode (ставит фаза 00). Текст идёт через stdin, не через argv:
в ссылках ключи, а argv виден всем процессам."""

from __future__ import annotations

import shutil
import subprocess

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


_svg_cache: dict[tuple[str, int], str] = {}


def svg(text: str, size: int = 6) -> str:
    """SVG-разметка (для веб-админки и файлов). --svg-path (qrencode >= 4.1) рисует модули
    одним path: файл в десятки раз меньше. Результат кэшируется по содержимому: запуск
    qrencode на каждый QR при каждой отрисовке страницы делал её медленной."""
    key = (text, size)
    if key in _svg_cache:
        return _svg_cache[key]
    try:
        out = _encode(text, "SVG", ["-s", str(size), "-m", "2", "--svg-path"])
    except QrError:
        out = _encode(text, "SVG", ["-s", str(size), "-m", "2"])
    if len(_svg_cache) > 256:
        _svg_cache.clear()
    _svg_cache[key] = out
    return out
