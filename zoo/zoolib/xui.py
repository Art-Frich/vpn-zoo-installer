"""Минимальный клиент API 3x-ui (только чтение: статус, inbounds, клиенты, трафик).

Изменения в 3x-ui делаются только через scripts/lib/xui.sh (ARCHITECTURE §9) — из zoo
через protolib. Токен берётся из файла заголовка /etc/vpn-setup/xui-auth.hdr
(«Authorization: Bearer <tok>»), запасной вариант — XUI_API_TOKEN из config.env.
Ответ API всегда {success, msg, obj}.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import paths
from .config import Config


class XuiError(Exception):
    def __init__(self, message: str, http_code: int | None = None) -> None:
        super().__init__(message)
        self.http_code = http_code


def read_token(hdr_file: Path | None = None, cfg: Config | None = None) -> str:
    f = Path(hdr_file) if hdr_file else paths.xui_hdr_file()
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "authorization":
                value = value.strip()
                if value.lower().startswith("bearer "):
                    return value[7:].strip()
    except OSError:
        pass
    return cfg.get("XUI_API_TOKEN") if cfg else ""


class XuiClient:
    def __init__(self, port: int | str, base_path: str, token: str, timeout: float = 10.0,
                 host: str = "127.0.0.1") -> None:
        if not port:
            raise XuiError("PANEL_PORT не задан (фаза 03 не выполнена?)")
        if not token:
            raise XuiError("нет API-токена 3x-ui (xui-auth.hdr / XUI_API_TOKEN)")
        base_path = base_path.strip("/")
        self.base = f"http://{host}:{port}" + (f"/{base_path}" if base_path else "")
        self.token = token
        self.timeout = timeout
        # без системных прокси: панель на 127.0.0.1
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_config(cls, cfg: Config, timeout: float = 10.0) -> "XuiClient":
        return cls(cfg.get("PANEL_PORT"), cfg.get("PANEL_PATH"), read_token(cfg=cfg), timeout)

    def request(self, method: str, path: str, body: Any = None,
                headers: dict[str, str] | None = None) -> Any:
        """Вызов /panel/api/<path>; возвращает .obj. XuiError при HTTP != 200 или success=false."""
        url = f"{self.base}/panel/api/{path.lstrip('/')}"
        data = None
        hdrs = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            hint = {404: " (нет токена или неверный PANEL_PATH)", 401: " (токен отвергнут)"}.get(e.code, "")
            raise XuiError(f"{method} {path}: HTTP {e.code}{hint}", e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise XuiError(f"{method} {path}: панель недоступна ({getattr(e, 'reason', e)})") from None
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            raise XuiError(f"{method} {path}: ответ не JSON") from None
        if not isinstance(doc, dict) or doc.get("success") is not True:
            msg = doc.get("msg") if isinstance(doc, dict) else ""
            raise XuiError(f"{method} {path}: {msg or 'success=false'}", 200)
        return doc.get("obj")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    # ---------- чтение ----------

    def server_status(self) -> dict[str, Any]:
        """.xray.{state,version,errorMsg}, .panelVersion, cpu/mem и т. п."""
        return self.get("server/status") or {}

    def inbounds(self) -> list[dict[str, Any]]:
        return self.get("inbounds/list") or []

    def inbound(self, inbound_id: int) -> dict[str, Any]:
        return self.get(f"inbounds/get/{int(inbound_id)}") or {}

    def clients(self) -> list[dict[str, Any]]:
        """[{id, email, uuid, flow, subId, enable, inboundIds, traffic, ...}]"""
        return self.get("clients/list") or []

    def client(self, email: str) -> dict[str, Any]:
        return self.get(f"clients/get/{urllib.parse.quote(email, safe='')}") or {}

    def client_links(self, email: str, host: str) -> list[str]:
        """Ссылки клиента; хост в ссылке панель берёт из заголовка Host запроса."""
        obj = self.request("GET", f"clients/links/{urllib.parse.quote(email, safe='')}",
                           headers={"Host": host})
        return [str(x) for x in obj or []]
