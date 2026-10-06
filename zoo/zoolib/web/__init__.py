"""Веб-админка (ARCHITECTURE §6): `zoo web` — http.server на 127.0.0.1:$ZOO_WEB_PORT.

Вход по токену ZOO_WEB_TOKEN (config.env) или по одноразовой ссылке (`zoo web --link`),
доступ только через туннель:
    ssh -N -L PORT:127.0.0.1:PORT root@SERVER  →  http://127.0.0.1:PORT/
    ssh -t -L PORT:127.0.0.1:PORT root@SERVER sudo zoo web --link  →  ссылка входа, туннель открыт
Страницы и действия используют те же функции zoolib, что CLI (status, users, traffic,
upgrade); юнит zoo-web.service ставит фаза 09.
"""

from __future__ import annotations

import argparse
import os
import random
from typing import Any

from .. import output, paths, system
from ..config import Config, config_set
from ..config import load as load_config

DEFAULT_BIND = "127.0.0.1"
BANNED_PORTS = {1080, 3128, 8080, 9050, 2053, 54321}
UNIT = "zoo-web.service"


def _ephemeral_start() -> int:
    try:
        with open(system.PROC / "sys" / "net" / "ipv4" / "ip_local_port_range", encoding="ascii") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 32768


def rand_port(cfg: Config) -> int:
    """Свободный порт 20000..начало эфемерного диапазона, как rand_port в lib.sh."""
    hi = _ephemeral_start() - 1
    if hi < 25000:
        hi = 60999
    hi = min(hi, 65535)
    taken = {s.port for s in system.listening_sockets()} | {p for _, p in system.registry_ports()}
    taken |= {int(v) for k, v in cfg.values.items() if k.endswith("PORT") and v.isdigit()}
    rng = random.SystemRandom()
    for _ in range(500):
        p = rng.randint(20000, hi)
        if p not in BANNED_PORTS and p not in taken:
            return p
    raise RuntimeError("не нашёл свободный порт для админки")


def setup(cfg: Config) -> None:
    """Вызывается из `zoo setup` (фаза 09): порт и токен админки, если их ещё нет."""
    from .auth import new_token
    if not cfg.get("ZOO_WEB_PORT"):
        config_set("ZOO_WEB_PORT", str(rand_port(cfg)))
    if len(cfg.get("ZOO_WEB_TOKEN")) < 16:
        config_set("ZOO_WEB_TOKEN", new_token())


def access_info(cfg: Config) -> dict[str, Any]:
    port = cfg.get("ZOO_WEB_PORT") or "PORT"
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
    return {"port": port, "url": f"http://127.0.0.1:{port}/", "ssh": ssh,
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
    print("1. На своём компьютере откройте туннель (окно оставьте открытым):")
    print(f"   {d['tunnel']}")
    print(f"2. Откройте в браузере: {d['url']}")
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
