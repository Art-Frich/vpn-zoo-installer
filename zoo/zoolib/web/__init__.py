"""Веб-админка (ARCHITECTURE §6): `zoo web` — http.server на 127.0.0.1:$ZOO_WEB_PORT.

Вход по токену ZOO_WEB_TOKEN (config.env) или по одноразовой ссылке (`zoo web --link`),
доступ только через туннель:
    ssh -t -L 7070:127.0.0.1:7070 root@SERVER zoo web --link  →  ссылка входа, туннель открыт
    ssh -N -L 7070:127.0.0.1:7070 root@SERVER  →  http://127.0.0.1:7070/
Порт по умолчанию — 7070 (DEFAULT_PORT): админ знает команду заранее, по одному адресу и паролю.
Страницы и действия используют те же функции zoolib, что CLI (status, users, traffic,
upgrade); юнит zoo-web.service ставит фаза 09.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .. import output, paths, system
from ..config import Config, config_set
from ..config import load as load_config

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 7070
BANNED_PORTS = {1080, 3128, 8080, 9050, 2053, 54321}
UNIT = "zoo-web.service"


def _ephemeral_start() -> int:
    try:
        with open(system.PROC / "sys" / "net" / "ipv4" / "ip_local_port_range", encoding="ascii") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 32768


def _legacy_range() -> tuple[int, int]:
    """Диапазон, из которого старые версии выбирали случайный порт админки."""
    hi = _ephemeral_start() - 1
    if hi < 25000:
        hi = 60999
    return 20000, min(hi, 65535)


def _taken_ports(cfg: Config) -> set[int]:
    taken = {s.port for s in system.listening_sockets() if s.proto == "tcp"}
    taken |= {p for proto, p in system.registry_ports() if proto == "tcp"}
    taken |= {int(v) for k, v in cfg.values.items() if k.endswith("PORT") and k != "ZOO_WEB_PORT" and v.isdigit()}
    return taken


def pick_port(cfg: Config) -> int:
    """7070, а если занят — ближайший свободный выше (7071, 7072…)."""
    taken = _taken_ports(cfg)
    for p in range(DEFAULT_PORT, DEFAULT_PORT + 200):
        if p not in taken and p not in BANNED_PORTS:
            return p
    raise RuntimeError("не нашёл свободный порт для админки")


def is_auto_port(cfg: Config, port: int) -> bool:
    """Порт выбрал сам zoo, а не владелец: метка ZOO_WEB_PORT_AUTO=1; у старых установок
    метки нет — тогда порт из прежнего случайного диапазона. ZOO_WEB_PORT_AUTO=0 — порт закреплён."""
    mark = cfg.get("ZOO_WEB_PORT_AUTO").strip()
    if mark:
        return mark == "1"
    lo, hi = _legacy_range()
    return lo <= port <= hi


def _restart_web() -> None:
    if system.unit_states([UNIT]).get(UNIT, {}).get("active") == "active":
        rc, _, err = system.run(["systemctl", "restart", UNIT], timeout=30)
        if rc != 0:
            output.warn(f"{UNIT} не перезапустился: {err.strip()[:200]} (sudo systemctl restart {UNIT})")


def setup(cfg: Config) -> None:
    """Вызывается из `zoo setup` (фаза 09): порт и токен админки.
    Нет порта — 7070 или ближайший свободный выше. Порт, выбранный не владельцем, переезжает
    на 7070, если он свободен; заданный владельцем не трогаем."""
    from .auth import new_token
    old = cfg.int("ZOO_WEB_PORT")
    if not old:
        port = pick_port(cfg)
        config_set("ZOO_WEB_PORT", str(port))
        config_set("ZOO_WEB_PORT_AUTO", "1")
        if port != DEFAULT_PORT:
            output.warn(f"порт {DEFAULT_PORT} занят, админка на {port}. Команда входа: "
                        f"{access_info(load_config())['command']}")
    elif old != DEFAULT_PORT and is_auto_port(cfg, old) and DEFAULT_PORT not in _taken_ports(cfg):
        config_set("ZOO_WEB_PORT", str(DEFAULT_PORT))
        config_set("ZOO_WEB_PORT_AUTO", "1")
        output.ok(f"админка теперь на порту {DEFAULT_PORT} (был {old}). "
                  f"Команда входа: {access_info(load_config())['command']}")
        _restart_web()
    if len(cfg.get("ZOO_WEB_TOKEN")) < 16:
        config_set("ZOO_WEB_TOKEN", new_token())


def access_info(cfg: Config) -> dict[str, Any]:
    port = cfg.get("ZOO_WEB_PORT") or str(DEFAULT_PORT)
    ip = cfg.get("SERVER_IP") or "SERVER_IP"
    ssh_port = cfg.ssh_login_port()
    # под sudo — тот, кто вошёл по SSH (при PermitRootLogin no root@ не пустит), как в фазе 99
    who = os.environ.get("SUDO_USER") or "root"
    host = f"[{ip}]" if ":" in ip else ip
    ssh = (f"-p {ssh_port} " if ssh_port != "22" else "") + f"{who}@{host}"
    token = cfg.get("ZOO_WEB_TOKEN")
    link = ""
    if len(token) >= 16:
        from .auth import make_once
        link = f"http://127.0.0.1:{port}/login?once={make_once(token)}"
    sudo = "" if who == "root" else "sudo "
    return {"port": port, "url": f"http://127.0.0.1:{port}/", "ssh": ssh,
            "command": f"ssh -t -L {port}:127.0.0.1:{port} {ssh} {sudo}zoo web --link",
            "tunnel": f"ssh -N -L {port}:127.0.0.1:{port} {ssh}", "token": token, "link": link}


def sessions_file():
    return paths.state_dir() / "web-sessions.json"


def serve(cfg: Config, bind: str = DEFAULT_BIND, port: int | None = None) -> None:
    from .app import App
    from .server import make_server
    port = port or cfg.int("ZOO_WEB_PORT")
    if not port:
        raise RuntimeError("ZOO_WEB_PORT не задан: sudo zoo setup (или install.sh --phase 09)")
    token = cfg.get("ZOO_WEB_TOKEN")
    if len(token) < 16:
        raise RuntimeError("ZOO_WEB_TOKEN не задан: sudo zoo setup (или install.sh --phase 09)")
    httpd = make_server(App(token, load_config), bind, port)
    output.info(f"админка: http://{bind}:{port}/ (Ctrl+C — остановить)")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--info", action="store_true", help="как подключиться: туннель, адрес, ссылка входа, токен")
    p.add_argument("--link", action="store_true", help="одноразовая ссылка входа (живёт 3 минуты)")
    p.add_argument("--new-token", action="store_true", help="выпустить новый токен (сессии закроются)")
    p.add_argument("--bind", default=DEFAULT_BIND, help="адрес (только loopback)")
    p.add_argument("--port", type=int, help="порт (по умолчанию ZOO_WEB_PORT)")


def _render_info(d: dict[str, Any]) -> None:
    print("Веб-админка zoo\n")
    print("С вашего компьютера, одной командой (пароль сервера; окно не закрывайте):")
    print(f"   {d['command']}")
    print("Появится ссылка входа — откройте её в браузере.\n")
    print("Или по шагам:")
    print(f"1. Туннель: {d['tunnel']}")
    print(f"2. Адрес: {d['url']}")
    if d.get("link"):
        print(f"3. Вход по ссылке (3 минуты, один раз): {d['link']}")
        print(f"   или токен: {d['token']}")
    else:
        print("3. Токен для входа: — (не задан: sudo zoo setup)")
    print(f"\nСервис {UNIT}: {d['service']}")


def cmd_web(args: argparse.Namespace, cfg: Config) -> int:
    from .auth import new_token
    if args.new_token:
        config_set("ZOO_WEB_TOKEN", new_token())
        try:
            sessions_file().unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            output.warn(f"не удалил {sessions_file()}: {e} (сессии и так не подойдут к новому токену)")
        cfg = load_config()
        if system.unit_states([UNIT]).get(UNIT, {}).get("active") == "active":
            system.run(["systemctl", "restart", UNIT], timeout=30)
        output.ok("новый токен записан в config.env, сессии админки закрыты")
        args.info = True
    if args.link:
        d = access_info(cfg)
        if not d["link"]:
            output.error("ZOO_WEB_TOKEN не задан: sudo zoo setup")
            return 1
        output.emit({"link": d["link"], "expires_in": 180}, args.json, lambda x: print(x["link"]))
        if not args.json:
            print(f"порт админки {d['port']}; команда входа — zoo web --info", file=sys.stderr)
        # ssh -t -L …: туннель живёт, пока жива команда. В терминале ждём, иначе ssh закроется сразу после ссылки
        if not args.json and sys.stdin.isatty() and sys.stdout.isatty():
            print("Откройте ссылку в браузере. Админка работает, пока это окно открыто; закрыть — Ctrl+C.", flush=True)
            try:
                while sys.stdin.read(1):
                    pass
            except KeyboardInterrupt:
                pass
        return 0
    if args.info:
        d = access_info(cfg)
        d["service"] = system.unit_states([UNIT]).get(UNIT, {}).get("active") or "unknown"
        output.emit(d, args.json, _render_info)
        return 0
    try:
        serve(cfg, args.bind, args.port)
    except (RuntimeError, ValueError, OSError) as e:
        output.error(str(e))
        return 1
    except KeyboardInterrupt:
        pass
    return 0
