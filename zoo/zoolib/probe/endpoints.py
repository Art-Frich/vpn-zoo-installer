"""Адрес сервера в probe-объекте манифеста: прочитать и подменить (для --local и стенда).

probe.kind (ARCHITECTURE §4):
    xray      {outbound: Xray outbound}                 vnext[]/servers[] или плоский settings.address
    hysteria  {client: {server: "host:port[,hop]", ...}}
    sing-box  {outbound: {server, server_port, type}}
    awg       {conf: полный клиентский .conf, endpoint: "host:port"}
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

UDP_SINGBOX = {"tuic", "hysteria", "hysteria2", "wireguard"}
UDP_XRAY_NETWORKS = {"kcp", "mkcp", "quic"}


class EndpointError(ValueError):
    pass


@dataclass
class Endpoint:
    host: str
    port: int
    layer: str  # tcp | udp

    def __str__(self) -> str:
        h = f"[{self.host}]" if ":" in self.host else self.host
        return f"{h}:{self.port}/{self.layer}"


def split_hostport(value: str) -> tuple[str, int]:
    """«1.2.3.4:443», «[::1]:443», «host:443,20000-30000» (хвост hop отбрасывается)."""
    s = str(value).strip().split(",", 1)[0]
    m = re.match(r"^\[([^\]]+)\]:(\d+)$", s) or re.match(r"^([^:]+):(\d+)$", s)
    if not m:
        raise EndpointError(f"не host:port: {value!r}")
    return m.group(1), int(m.group(2))


def _join(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _xray_server(ob: dict[str, Any]) -> dict[str, Any]:
    settings = ob.get("settings") or {}
    for key in ("vnext", "servers"):
        items = settings.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    if "address" in settings:
        return settings
    raise EndpointError("в outbound нет адреса сервера (vnext/servers/address)")


def conf_endpoint(conf: str) -> str:
    for line in conf.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip().lower() == "endpoint":
            return v.strip()
    raise EndpointError("в .conf нет Endpoint")


def endpoint_of(probe: dict[str, Any]) -> Endpoint:
    kind = probe.get("kind")
    if kind == "xray":
        ob = probe.get("outbound") or {}
        srv = _xray_server(ob)
        network = str((ob.get("streamSettings") or {}).get("network", "tcp")).lower()
        layer = "udp" if network in UDP_XRAY_NETWORKS else "tcp"
        return Endpoint(str(srv["address"]), int(srv["port"]), layer)
    if kind == "hysteria":
        host, port = split_hostport((probe.get("client") or {}).get("server", ""))
        return Endpoint(host, port, "udp")
    if kind == "sing-box":
        ob = probe.get("outbound") or {}
        if not ob.get("server") or not ob.get("server_port"):
            raise EndpointError("в outbound sing-box нет server/server_port")
        layer = "udp" if ob.get("type") in UDP_SINGBOX else "tcp"
        return Endpoint(str(ob["server"]), int(ob["server_port"]), layer)
    if kind == "awg":
        host, port = split_hostport(probe.get("endpoint") or conf_endpoint(probe.get("conf", "")))
        return Endpoint(host, port, "udp")
    raise EndpointError(f"неизвестный probe.kind: {kind!r}")


def with_host(probe: dict[str, Any], host: str) -> dict[str, Any]:
    """Копия probe с другим адресом сервера (порт, ключи и SNI — без изменений)."""
    p = copy.deepcopy(probe)
    kind = p.get("kind")
    if kind == "xray":
        _xray_server(p["outbound"])["address"] = host
    elif kind == "hysteria":
        _, port = split_hostport(p["client"]["server"])
        p["client"]["server"] = _join(host, port)
        p.pop("hop", None)
    elif kind == "sing-box":
        p["outbound"]["server"] = host
    elif kind == "awg":
        _, port = split_hostport(p.get("endpoint") or conf_endpoint(p.get("conf", "")))
        p["endpoint"] = _join(host, port)
        p["conf"] = re.sub(r"(?im)^(\s*Endpoint\s*=\s*).*$", lambda m: m.group(1) + _join(host, port),
                           p.get("conf", ""))
    else:
        raise EndpointError(f"неизвестный probe.kind: {kind!r}")
    return p
