"""Рукопожатие MTProto Fake-TLS (mtg-multi, mtglib/internal/tls/fake): принял ли сервер секрет пользователя — без
клиента Telegram.

Секрет — «ee» + 16 байт ключа + домен в hex. ClientHello — TLS-запись с SNI домена; поле random = HMAC-SHA256(ключ,
запись с нулевым random) XOR (28 нулевых байт + время unix, 4 байта little-endian; mtg допускает расхождение часов
в 3 с). Сервер, принявший секрет, отвечает тремя записями — ServerHello, ChangeCipherSpec, ApplicationData — и его
random = HMAC-SHA256(ключ, random клиента + весь ответ с нулевым random). Чужой секрет mtg не отвергает, а пересылает
соединение на настоящий сайт домена: тогда приходит его ServerHello, и подпись не сходится.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import time
from typing import Callable

RANDOM_OFFSET = 11   # тип записи (1) + версия (2) + длина (2) + тип рукопожатия (1) + длина (3) + версия клиента (2)
RANDOM_LEN = 32
MAX_RECORD = 16384 + 2048
CIPHERS = (0x1301, 0x1302, 0x1303, 0xC02B, 0xC02F, 0xC02C, 0xC030, 0xCCA9, 0xCCA8)
SIG_ALGS = (0x0403, 0x0804, 0x0401, 0x0503, 0x0805, 0x0501, 0x0806, 0x0601)


def parse_secret(secret: str) -> tuple[bytes, str]:
    """(ключ 16 байт, домен) из секрета Fake-TLS; не тот формат — ValueError."""
    s = secret.strip().lower()
    if not s.startswith("ee"):
        raise ValueError("секрет не Fake-TLS (нет префикса ee)")
    raw = bytes.fromhex(s[2:])
    if len(raw) <= 16:
        raise ValueError("в секрете нет домена")
    return raw[:16], raw[16:].decode("ascii")


def _ext(kind: int, data: bytes) -> bytes:
    return struct.pack(">HH", kind, len(data)) + data


def _list16(items: tuple[int, ...]) -> bytes:
    body = b"".join(struct.pack(">H", x) for x in items)
    return struct.pack(">H", len(body)) + body


def client_hello(key: bytes, domain: str, now: float | None = None,
                 rand: Callable[[int], bytes] = os.urandom) -> bytes:
    """Запись ClientHello с подписанным random (как у клиента Telegram)."""
    name = domain.encode("ascii")
    sni = struct.pack(">HBH", len(name) + 3, 0, len(name)) + name
    alpn_list = b"\x02h2\x08http/1.1"
    exts = b"".join((
        _ext(0x0000, sni),
        _ext(0x000A, _list16((0x001D, 0x0017))),
        _ext(0x000B, b"\x01\x00"),
        _ext(0x000D, _list16(SIG_ALGS)),
        _ext(0x0010, struct.pack(">H", len(alpn_list)) + alpn_list),
        _ext(0x002B, b"\x04\x03\x04\x03\x03"),
        _ext(0x002D, b"\x01\x01"),
        _ext(0x0033, struct.pack(">HHH", 36, 0x001D, 32) + rand(32)),
    ))
    session = rand(32)
    body = (b"\x03\x03" + bytes(RANDOM_LEN) + bytes([len(session)]) + session + _list16(CIPHERS)
            + b"\x01\x00" + struct.pack(">H", len(exts)) + exts)
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    record = bytearray(b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake)
    digest = hmac.new(key, bytes(record), hashlib.sha256).digest()
    stamp = bytes(28) + struct.pack("<I", int(time.time() if now is None else now) & 0xFFFFFFFF)
    record[RANDOM_OFFSET:RANDOM_OFFSET + RANDOM_LEN] = bytes(a ^ b for a, b in zip(digest, stamp))
    return bytes(record)


def server_accepted(key: bytes, hello: bytes, answer: bytes) -> bool:
    """Ответ — ServerHello от mtg, принявшего этот секрет (подпись random сходится)."""
    if len(answer) < RANDOM_OFFSET + RANDOM_LEN:
        return False
    got = answer[RANDOM_OFFSET:RANDOM_OFFSET + RANDOM_LEN]
    zeroed = answer[:RANDOM_OFFSET] + bytes(RANDOM_LEN) + answer[RANDOM_OFFSET + RANDOM_LEN:]
    client_random = hello[RANDOM_OFFSET:RANDOM_OFFSET + RANDOM_LEN]
    return hmac.compare_digest(hmac.new(key, client_random + zeroed, hashlib.sha256).digest(), got)


def _read_records(sock: socket.socket, count: int) -> tuple[bytes, list[int]]:
    """Первые count TLS-записей ответа целиком: (байты, типы записей). Обрыв — что успело прийти."""
    buf = b""
    types: list[int] = []
    pos = 0
    while len(types) < count:
        while len(buf) < pos + 5 or len(buf) < pos + 5 + int.from_bytes(buf[pos + 3:pos + 5], "big"):
            chunk = sock.recv(65536)
            if not chunk:
                return buf[:pos], types
            buf += chunk
            if len(buf) >= pos + 5 and int.from_bytes(buf[pos + 3:pos + 5], "big") > MAX_RECORD:
                return buf[:pos], types
        size = int.from_bytes(buf[pos + 3:pos + 5], "big")
        types.append(buf[pos])
        pos += 5 + size
    return buf[:pos], types


def handshake(host: str, port: int, secret: str, timeout: float) -> tuple[str, str, float | None]:
    """→ (статус, ошибка, мс) в терминах движка пробы: ok | rejected | fail | timeout."""
    key, domain = parse_secret(secret)
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            hello = client_hello(key, domain)
            sock.sendall(hello)
            answer, types = _read_records(sock, 3)
    except socket.timeout:
        return "timeout", f"нет ответа на ClientHello за {timeout:g} с", None
    except OSError as e:
        return "fail", f"соединение оборвалось: {e}", None
    ms = round((time.monotonic() - t0) * 1000, 1)
    if not types:
        return "fail", "сервер закрыл соединение без ответа на ClientHello", ms
    if types == [0x16, 0x14, 0x17] and server_accepted(key, hello, answer):
        return "ok", "", ms
    if types[0] == 0x16:
        return ("rejected", f"ответил обычный TLS ({domain}): секрет не принят — пользователь отключён, ключ сменился "
                            "или часы этой машины расходятся с сервером больше чем на 3 с", ms)
    return "fail", f"ответ не TLS (запись типа {types[0]:#04x})", ms
