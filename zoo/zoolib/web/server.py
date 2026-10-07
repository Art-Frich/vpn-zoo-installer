"""HTTP-сервер админки: ThreadingHTTPServer, только loopback. Логика — в app.App."""

from __future__ import annotations

import gzip
import ipaddress
import os
import shutil
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App, Request, Response, text
from .auth import parse_cookies

MAX_BODY = 5 * 1024 * 1024
GZIP_MIN = 1000  # короче — выигрыш меньше заголовков
GZIP_LEVEL = 5
FILE_CHUNK = 1 << 20


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


class Handler(BaseHTTPRequestHandler):
    server_version = "zoo-web"
    protocol_version = "HTTP/1.1"  # keep-alive: через ssh-туннель каждое новое соединение — лишний RTT
    timeout = 15
    app: App  # задаётся в make_server

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self, head: bool = False) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        req = Request(self.command, url.path or "/", query,
                      headers={k.lower(): v for k, v in self.headers.items()},
                      cookies=parse_cookies(self.headers.get("Cookie")))
        if self.command == "POST":
            err = self._read_form(req)
            if err:
                self._send(err, head)
                return
        self._send(self.app.handle(req), head)

    def _read_form(self, req: Request) -> Response | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return _closing(text("неверный Content-Length", 400))
        if length < 0:  # read(-1) читал бы до закрытия соединения без предела
            return _closing(text("неверный Content-Length", 400))
        if length > MAX_BODY:
            return _closing(text("слишком большой запрос", 413))
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        body = self.rfile.read(length) if length else b""
        if body and ctype != "application/x-www-form-urlencoded":
            return text("ожидается application/x-www-form-urlencoded", 415)
        multi = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        req.multi = multi
        req.form = {k: v[-1] for k, v in multi.items()}
        return None

    def _accepts_gzip(self) -> bool:
        for part in (self.headers.get("Accept-Encoding") or "").split(","):
            name, _, q = part.strip().partition(";")
            if name.strip().lower() == "gzip":
                return q.replace(" ", "").lower() not in ("q=0", "q=0.0", "q=0.00", "q=0.000")
        return False

    def _send_file(self, resp: Response, head: bool) -> None:
        """Файл потоком, без чтения в память (дистрибутивы до сотен МБ). Без сжатия."""
        try:
            fd = os.open(resp.file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))  # type: ignore[arg-type]
            f = os.fdopen(fd, "rb")
            size = os.fstat(fd).st_size
        except OSError:
            self._send(text("файл недоступен", 404), head)
            return
        with f:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(size))
            for k, v in resp.headers:
                self.send_header(k, v)
            self.end_headers()
            if not head:
                try:
                    shutil.copyfileobj(f, self.wfile, FILE_CHUNK)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    self.close_connection = True

    def _send(self, resp: Response, head: bool = False) -> None:
        if resp.file is not None and resp.status == 200:
            self._send_file(resp, head)
            return
        body, headers = resp.body, list(resp.headers)
        ctype = resp.content_type.split(";", 1)[0].strip().lower()
        compressible = ctype.startswith("text/") or "javascript" in ctype or ctype == "image/svg+xml"
        if compressible and (len(body) > GZIP_MIN or resp.gz is not None):
            headers.append(("Vary", "Accept-Encoding"))
            if self._accepts_gzip():
                body = resp.gz if resp.gz is not None else gzip.compress(body, GZIP_LEVEL, mtime=0)
                headers.append(("Content-Encoding", "gzip"))
        self.send_response(resp.status)
        if resp.status != 304:  # 304 без тела и без длины: клиент берёт своё
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if not head and body and resp.status != 304:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, fmt: str, *args) -> None:
        # без query-строк: в них бывают имена пользователей и периоды, секретов нет, но журналу хватит пути
        sys.stderr.write("zoo-web: " + (fmt % args).split("?", 1)[0] + "\n")

    def log_request(self, code="-", size="-") -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/") or path == "/healthz":
            return
        sys.stderr.write(f"zoo-web: {self.command} {path} {code}\n")


def _closing(resp: Response) -> Response:
    """Тело запроса не прочитано: следующий запрос в этом соединении разобрать нельзя."""
    resp.headers.append(("Connection", "close"))
    return resp


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: App, bind: str, port: int) -> Server:
    if not is_loopback(bind):
        raise ValueError(f"адрес {bind} не loopback: админка слушает только 127.0.0.1 / ::1 (доступ — ssh -L)")
    handler = type("ZooHandler", (Handler,), {"app": app})
    server_cls = Server
    if ":" in bind.strip("[]"):
        server_cls = type("Server6", (Server,), {"address_family": socket.AF_INET6})
    return server_cls((bind.strip("[]"), port), handler)
