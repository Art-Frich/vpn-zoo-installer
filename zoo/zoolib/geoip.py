"""Страна по IP без сети и без новых скачиваний: из geoip.dat Xray, который уже лежит на сервере
(3x-ui, источник закреплён в versions.env).

.dat — protobuf v2ray: GeoIPList{entry=1: GeoIP{country_code=1, cidr=2: CIDR{ip=1, prefix=2}}}.
Разбор .dat в Python занимает секунды, поэтому из него один раз строится компактный индекс
(массивы начал/концов диапазонов, 4 байта на границу для IPv4); он пересобирается, когда
меняются размер или время файла (geo-таймер обновил его). Для IPv6 берётся старшая половина
адреса (/64): сети длиннее /64 в country-списках не встречаются.
ASN здесь нет: в geoip.dat его не хранят, а наборы вроде iptoasn меняются каждый час и под
sha256-закрепление (D1) не подходят.
"""

from __future__ import annotations

import bisect
import ipaddress
import os
import struct
import sys
from array import array
from pathlib import Path
from typing import Any

MAGIC = b"ZGEO1\n"
HEADER = struct.Struct("<6sQQIII")  # magic, размер .dat, mtime_ns, n4, n6, reserved
BACK = 16  # сколько соседей слева проверяем при вложенных диапазонах


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def _fields(b: bytes):
    """(номер поля, тип, значение) верхнего уровня сообщения."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
            if len(v) != ln:
                raise ValueError("обрезанный geoip.dat")
        elif wt == 5:
            v = b[i:i + 4]
            i += 4
        elif wt == 1:
            v = b[i:i + 8]
            i += 8
        else:
            raise ValueError(f"неизвестный wire type {wt}")
        yield f, wt, v


def parse_dat(data: bytes) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """(v4, v6) — списки (начало, конец, код страны как uint16). Не страны (private,
    cloudflare, telegram…) пропускаются."""
    v4: list[tuple[int, int, int]] = []
    v6: list[tuple[int, int, int]] = []
    for f, wt, entry in _fields(data):
        if f != 1 or wt != 2:
            raise ValueError("не geoip.dat")
        code = ""
        cidrs = []
        for f2, wt2, v2 in _fields(entry):
            if f2 == 1 and wt2 == 2:
                code = v2.decode("ascii", "replace").lower()
            elif f2 == 2 and wt2 == 2:
                cidrs.append(v2)
        if len(code) != 2 or not code.isalpha():
            continue
        cc = (ord(code[0].upper()) << 8) | ord(code[1].upper())
        for raw in cidrs:
            ip, pfx = b"", 0
            for f3, wt3, v3 in _fields(raw):
                if f3 == 1 and wt3 == 2:
                    ip = v3
                elif f3 == 2 and wt3 == 0:
                    pfx = v3
            if len(ip) == 4 and pfx <= 32:
                x = int.from_bytes(ip, "big")
                size = 1 << (32 - pfx)
                start = x & ~(size - 1)
                v4.append((start, start + size - 1, cc))
            elif len(ip) == 16 and pfx <= 128:
                x = int.from_bytes(ip[:8], "big")
                p = min(pfx, 64)
                size = 1 << (64 - p)
                start = x & ~(size - 1)
                v6.append((start, start + size - 1, cc))
    v4.sort()
    v6.sort()
    return v4, v6


def build_index(src: Path, dst: Path) -> None:
    """Собрать индекс dst из .dat src (атомарно, 0600)."""
    st = src.stat()
    v4, v6 = parse_dat(src.read_bytes())
    arrs = [array("I", (s for s, _, _ in v4)), array("I", (e for _, e, _ in v4)),
            array("H", (c for _, _, c in v4)),
            array("Q", (s for s, _, _ in v6)), array("Q", (e for _, e, _ in v6)),
            array("H", (c for _, _, c in v6))]
    tmp = dst.with_name(dst.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(HEADER.pack(MAGIC, st.st_size, st.st_mtime_ns, len(v4), len(v6), 0))
        for a in arrs:
            if sys.byteorder == "big":
                a.byteswap()
            a.tofile(f)
    os.replace(tmp, dst)


class GeoIndex:
    def __init__(self, path: Path) -> None:
        data = Path(path).read_bytes()
        magic, self.src_size, self.src_mtime, n4, n6, _ = HEADER.unpack_from(data, 0)
        if magic != MAGIC:
            raise ValueError("не индекс geoip")
        off = HEADER.size
        out = []
        for code, n in (("I", n4), ("I", n4), ("H", n4), ("Q", n6), ("Q", n6), ("H", n6)):
            a = array(code)
            size = a.itemsize * n
            a.frombytes(data[off:off + size])
            if len(a) != n:
                raise ValueError("обрезанный индекс geoip")
            if sys.byteorder == "big":
                a.byteswap()
            out.append(a)
            off += size
        self.s4, self.e4, self.c4, self.s6, self.e6, self.c6 = out

    @staticmethod
    def _find(starts: array, ends: array, codes: array, x: int) -> str:
        i = bisect.bisect_right(starts, x) - 1
        best, best_size = 0, None
        for j in range(i, max(-1, i - BACK), -1):
            if ends[j] >= x:
                size = ends[j] - starts[j]
                if best_size is None or size < best_size:
                    best, best_size = codes[j], size
        return chr(best >> 8) + chr(best & 0xFF) if best else ""

    def country(self, ip: str) -> str:
        """Двухбуквенный код страны или "" (не нашли, частный адрес)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        if isinstance(a, ipaddress.IPv6Address):
            if a.ipv4_mapped:
                a = a.ipv4_mapped
            else:
                return self._find(self.s6, self.e6, self.c6, int(a) >> 64)
        return self._find(self.s4, self.e4, self.c4, int(a))


def open_index(src: Path, index: Path) -> GeoIndex | None:
    """Индекс для src; пересобирается, если .dat изменился. Нет .dat → None."""
    try:
        st = src.stat()
    except OSError:
        return None
    try:
        idx = GeoIndex(index)
        if idx.src_size == st.st_size and idx.src_mtime == st.st_mtime_ns:
            return idx
    except (OSError, ValueError, struct.error):
        pass
    try:
        build_index(src, index)
        return GeoIndex(index)
    except (OSError, ValueError, IndexError, struct.error):
        return None


def candidates(extra: str | None = None) -> list[Path]:
    """Где искать geoip.dat: явный путь, каталог 3x-ui."""
    out = [Path(p) for p in (extra, os.environ.get("ZOO_GEOIP_DAT")) if p]
    out.append(Path(os.environ.get("XUI_DIR", "/usr/local/x-ui")) / "bin" / "geoip.dat")
    return out


def find_dat(extra: str | None = None) -> Path | None:
    for p in candidates(extra):
        if p.is_file():
            return p
    return None


def describe(idx: GeoIndex | None) -> dict[str, Any]:
    return {"ready": idx is not None, "v4": len(idx.s4) if idx else 0, "v6": len(idx.s6) if idx else 0}
