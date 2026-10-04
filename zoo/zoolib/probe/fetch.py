"""HTTP(S)-замер через SOCKS5 или через сокет, привязанный к интерфейсу туннеля.

Модуль самостоятельный (только stdlib, без импортов zoolib): для AWG в сетевом
неймспейсе его запускают отдельным процессом —
`ip netns exec NS python3 fetch.py < задания.json` — и читают JSON со stdout.

fetch() не бросает исключений: всё, что случилось, — в полях результата.
error_kind: socks | dns | refused | connect_timeout | tls | tls_timeout | reset | eof |
            header_timeout | http | stall | timeout | error
"""

from __future__ import annotations

import ipaddress
import json
import random
import socket
import ssl
import struct
import sys
import time
from typing import Any
from urllib.parse import urlsplit

SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
USER_AGENT = "zoo-probe/2"


class FetchError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ---------- DNS (A-запрос по UDP через нужный интерфейс) ----------

def _qname(host: str) -> bytes:
    out = b""
    for part in host.rstrip(".").split("."):
        raw = part.encode("idna")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def _skip_name(data: bytes, i: int) -> int:
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def parse_dns_a(data: bytes, qid: int) -> list[str]:
    """IPv4-адреса из ответа DNS (без рекурсии по CNAME: резолвер отдаёт A в том же ответе)."""
    if len(data) < 12:
        raise FetchError("dns", "короткий ответ DNS")
    rid, flags, qd, an = struct.unpack("!HHHH", data[:8])
    if rid != qid:
        raise FetchError("dns", "чужой ответ DNS")
    if flags & 0x000F:
        raise FetchError("dns", f"DNS rcode {flags & 0x000F}")
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    ips = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return ips


def resolve(host: str, dns: str, bind_dev: str | None = None, timeout: float = 3.0, tries: int = 2) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    last = "нет ответа DNS"
    for _ in range(tries):
        qid = random.randrange(1, 0xFFFF)
        query = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _qname(host) + struct.pack("!HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if bind_dev:
                s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, bind_dev.encode())
            s.settimeout(timeout)
            s.sendto(query, (dns, 53))
            data, _ = s.recvfrom(4096)
            ips = parse_dns_a(data, qid)
            if ips:
                return ips[0]
            last = f"нет A-записи для {host}"
        except socket.timeout:
            last = f"DNS {dns}: таймаут"
        except OSError as e:
            last = f"DNS {dns}: {e}"
        finally:
            s.close()
    raise FetchError("dns", last)


# ---------- SOCKS5 с паролем (RFC 1928/1929), имя резолвит прокси ----------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise FetchError("socks", "SOCKS5: соединение закрыто")
        buf += chunk
    return buf


def socks5_connect(sock: socket.socket, host: str, port: int, user: str, password: str) -> None:
    sock.sendall(b"\x05\x01\x02")
    ver, method = _recv_exact(sock, 2)
    if ver != 5 or method != 2:
        raise FetchError("socks", f"SOCKS5: метод {method} вместо пароля")
    u, p = user.encode(), password.encode()
    sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
    if _recv_exact(sock, 2)[1] != 0:
        raise FetchError("socks", "SOCKS5: пароль не принят")
    h = host.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack("!H", port))
    _ver, rep, _rsv, atyp = _recv_exact(sock, 4)
    if rep != 0:
        raise FetchError("socks", f"SOCKS5: отказ прокси (код {rep})")
    size = {1: 4, 4: 16}.get(atyp)
    if size is None:
        size = _recv_exact(sock, 1)[0]
    _recv_exact(sock, size + 2)


# ---------- HTTP/1.1 ----------

def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise FetchError("http", f"не HTTP-ответ: {lines[0][:60]!r}")
    headers = {}
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()
    return int(parts[1]), headers


def fetch(url: str, socks: dict[str, Any] | None = None, bind_dev: str | None = None,
          dns: str | None = None, connect_timeout: float = 10.0, stall: float = 8.0,
          max_time: float = 60.0, keep_body: int = 0, limit: int = 0) -> dict[str, Any]:
    """GET url. socks={host, port, user, password} — через прокси; bind_dev — сокет на интерфейсе
    туннеля (DNS тогда через dns по тому же интерфейсу). limit — хватит стольких байт тела."""
    u = urlsplit(url)
    https = u.scheme == "https"
    host = u.hostname or ""
    port = u.port or (443 if https else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    res: dict[str, Any] = {"url": url, "ok": False, "status": None, "bytes": 0, "expected": None,
                           "seconds": None, "connect_ms": None, "ttfb_ms": None, "body_seconds": None,
                           "stalled": False, "error": "", "error_kind": "", "body": ""}
    t0 = time.monotonic()
    deadline = t0 + max_time
    sock: socket.socket | None = None
    stage = "connect"
    try:
        if socks:
            sock = socket.create_connection((socks["host"], int(socks["port"])), timeout=connect_timeout)
            socks5_connect(sock, host, port, socks["user"], socks["password"])
        else:
            ip = resolve(host, dns, bind_dev) if dns else socket.gethostbyname(host)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if bind_dev:
                sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, bind_dev.encode())
            sock.settimeout(connect_timeout)
            sock.connect((ip, port))
        res["connect_ms"] = round((time.monotonic() - t0) * 1000, 1)
        if https:
            stage = "tls"
            sock.settimeout(connect_timeout)
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        stage = "header"
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.netloc.rsplit('@', 1)[-1]}\r\nUser-Agent: {USER_AGENT}\r\n"
               "Accept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n")
        t_req = time.monotonic()
        sock.sendall(req.encode())
        sock.settimeout(min(stall, max(0.5, deadline - time.monotonic())))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise FetchError("eof", "соединение закрыто до ответа")
            if not buf:
                res["ttfb_ms"] = round((time.monotonic() - t_req) * 1000, 1)
            buf += chunk
            if len(buf) > 65536:
                raise FetchError("http", "слишком длинные заголовки")
        head, _, body = buf.partition(b"\r\n\r\n")
        status, headers = _parse_head(head)
        res["status"] = status
        cl = headers.get("content-length", "")
        expected = int(cl) if cl.isdigit() else None
        res["expected"] = expected
        stage = "body"
        t_body = time.monotonic()
        got = res["bytes"] = len(body)
        kept = body[:keep_body]
        while (expected is None or got < expected) and not (limit and got >= limit):
            if time.monotonic() >= deadline:
                raise FetchError("timeout", f"дольше {max_time:.0f} с")
            sock.settimeout(min(stall, max(0.5, deadline - time.monotonic())))
            try:
                chunk = sock.recv(262144)
            except socket.timeout:
                if time.monotonic() >= deadline - 0.01:
                    raise FetchError("timeout", f"дольше {max_time:.0f} с") from None
                res["stalled"] = True
                raise FetchError("stall", f"нет данных {stall:.0f} с") from None
            if not chunk:
                if expected is not None and not (limit and got >= limit):
                    raise FetchError("eof", "соединение закрыто посреди ответа")
                break
            got += len(chunk)
            res["bytes"] = got
            if len(kept) < keep_body:
                kept += chunk[:keep_body - len(kept)]
        res["bytes"] = got
        res["body_seconds"] = round(time.monotonic() - t_body, 3)
        if keep_body:
            res["body"] = kept.decode("utf-8", "replace")
        if not 200 <= status < 300:
            raise FetchError("http", f"HTTP {status}")
        res["ok"] = True
    except FetchError as e:
        res["error_kind"], res["error"] = e.kind, str(e)
    except socket.timeout:
        kind = {"connect": "connect_timeout", "tls": "tls_timeout"}.get(stage, "header_timeout")
        res["error_kind"], res["error"] = kind, f"таймаут ({stage})"
    except ConnectionRefusedError:
        res["error_kind"], res["error"] = "refused", "соединение отвергнуто"
    except ConnectionResetError:
        res["error_kind"], res["error"] = "reset", f"соединение сброшено (RST, {stage})"
    except ssl.SSLError as e:
        eof = "EOF" in str(e) or "UNEXPECTED_EOF" in str(e)
        res["error_kind"], res["error"] = ("eof" if eof else "tls"), f"TLS: {e.reason or e}"
    except OSError as e:
        res["error_kind"], res["error"] = "error", f"{stage}: {e.strerror or e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if res["body_seconds"] is None and res["bytes"]:
        res["body_seconds"] = round(time.monotonic() - t0, 3)
    res["seconds"] = round(time.monotonic() - t0, 3)
    return res


def main() -> int:
    """stdin: {"jobs": [{url, ...аргументы fetch}]} → stdout: JSON-список результатов."""
    spec = json.load(sys.stdin)
    out = [fetch(**job) for job in spec.get("jobs", [])]
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
