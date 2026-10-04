"""HTTP-сервер админки: ThreadingHTTPServer, только loopback. Логика — в app.App."""

from __future__ import annotations

import ipaddress
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App, Request, Response, text
from .auth import parse_cookies

MAX_BODY = 5 * 1024 * 1024


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


class Handler(BaseHTTPRequestHandler):
    server_version = "zoo-web"
    timeout = 60
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
            return text("неверный Content-Length", 400)
        if length < 0:  # read(-1) читал бы до закрытия соединения без предела
            return text("неверный Content-Length", 400)
        if length > MAX_BODY:
            return text("слишком большой запрос", 413)
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        body = self.rfile.read(length) if length else b""
        if body and ctype != "application/x-www-form-urlencoded":
            return text("ожидается application/x-www-form-urlencoded", 415)
        multi = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        req.multi = multi
        req.form = {k: v[-1] for k, v in multi.items()}
        return None

    def _send(self, resp: Response, head: bool = False) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        for k, v in resp.headers:
            self.send_header(k, v)
        self.end_headers()
        if not head and resp.body:
            try:
                self.wfile.write(resp.body)
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
