"""Сводка `zoo status`: протоколы, сервисы, порты, сертификаты, версии, метрики хоста.

collect() возвращает словарь (его же отдаёт `zoo status --json` и веб-админка),
problems — список того, что требует внимания.
"""

from __future__ import annotations

import glob
import platform
from pathlib import Path
from typing import Any

from . import __version__, groups, manifests, output, paths, protolib, system, traffic
from .config import Config
from .users import OWNER, Registry, UserError, users_module
from .xui import XuiClient, XuiError

# ufw проверяется через `ufw status` (system.ufw_active), а не по юниту
BASE_UNITS = ["x-ui.service", "fail2ban.service"]
ZOO_UNITS = ["zoo-web.service", "zoo-collector.timer"]
CERT_GLOBS = ["/etc/hysteria/cert.pem"]
CERT_WARN_DAYS = 14


def _unit(name: str) -> str:
    return name if "." in name else name + ".service"


def _manifest_certs(m: manifests.Manifest) -> list[str]:
    """Пути сертификатов из манифеста: cert (строка/список) или tls.cert_path."""
    c = m.raw.get("cert")
    if isinstance(c, str):
        return [c]
    if isinstance(c, list):
        return [str(x) for x in c]
    tls = m.raw.get("tls")
    if isinstance(tls, dict) and isinstance(tls.get("cert_path"), str):
        return [tls["cert_path"]]
    return []


def protocol_users(reg: Registry | None, m: manifests.Manifest, libs: set[str],
                   groups_by_id: dict[str, Any] | None = None) -> dict[str, Any]:
    """Кто сидит на протоколе по реестру: включённые обычные пользователи с ним в списке, отдельно отключённые
    и те, у кого его нет (с причиной). Служебные (zoo-probe) не считаются, трафик не смотрится."""
    if reg is None:
        return {"users": None, "users_off": 0, "user_names": [], "off_names": [], "lacking": []}
    ids = {m.id, users_module(m, libs)}
    have, off, lacking = [], [], []
    for u in reg.visible():
        if ids & set(u.protocols):
            (have if u.enabled else off).append(u.name)
        elif u.enabled:
            g = (groups_by_id or {}).get(u.group)
            if u.custom and u.name != OWNER:
                why = "свой набор протоколов"
            elif g is not None and not g.all_protocols and not (
                    ids & set(g.protocols) or any(x.startswith(users_module(m, libs) + "-") for x in g.protocols)):
                why = f"нет в группе «{g.name}»"
            else:
                why = "не заведён (zoo user sync)"
            lacking.append([u.name, why])
    return {"users": len(have), "users_off": len(off), "user_names": have, "off_names": off, "lacking": lacking}


def collect(cfg: Config, cpu_interval: float = 0.2, with_xui: bool = True) -> dict[str, Any]:
    data = collect_slow(cfg, with_xui)
    data["host"] = system.host_metrics(cpu_interval)
    return data


def collect_slow(cfg: Config, with_xui: bool = True) -> dict[str, Any]:
    """Всё, кроме метрик хоста (/proc): системные вызовы, сокеты, сертификаты, API 3x-ui.
    Веб-админка кэширует эту часть на минуту, а метрики считает на каждый запрос."""
    problems: list[str] = []
    good, bad = manifests.load_all()
    for b in bad:
        problems.append(f"манифест {b.id}: {'; '.join(b.errors)}")
    for e in cfg.errors:
        problems.append(f"config.env: {e}")

    units = list(BASE_UNITS)
    for m in good:
        units += [_unit(s) for s in m.services]
    states = system.unit_states(units + ZOO_UNITS)

    sockets = system.listening_sockets()
    listen = {(s.proto, s.port) for s in sockets}

    try:
        reg = Registry.load()
        visible = reg.visible()
        user_counts: dict[str, Any] = {"total": len(visible),
                                       "enabled": sum(u.enabled for u in visible),
                                       "registry": reg.exists,
                                       "system": [u.name for u in reg.users if u.system]}
    except (UserError, OSError, ValueError) as e:
        reg = None
        user_counts = {"total": 0, "enabled": 0, "registry": False, "error": str(e)}
        problems.append(f"users.json: {e}")

    protocols = []
    libs = set(protolib.list_libs())
    try:
        groups_by_id = {g.id: g for g in groups.Groups.load().groups} if reg else {}
    except (UserError, OSError, ValueError):
        groups_by_id = {}
    for m in good:
        svc = {u: states.get(_unit(u), {}).get("active", "unknown") for u in m.services}
        listening = {p: (p, m.port) in listen for p in m.protos}
        # awg-quick@ — oneshot: при падении amneziawg-go юнит остаётся active, а интерфейса нет
        iface = m.raw.get("interface")
        iface_up = None if not isinstance(iface, str) or not iface else (Path("/sys/class/net") / iface).exists()
        ok = (m.enabled and all(v == "active" for v in svc.values()) and all(listening.values())
              and iface_up is not False)
        if m.enabled:
            for u, st in svc.items():
                if st != "active":
                    problems.append(f"{m.id}: сервис {u} — {st}")
            for p, on in listening.items():
                if not on:
                    problems.append(f"{m.id}: никто не слушает {m.port}/{p}")
            if iface_up is False:
                problems.append(f"{m.id}: нет интерфейса {iface} (systemctl restart {', '.join(m.services) or iface})")
        protocols.append({
            "id": m.id, "name": m.name, "short": m.short, "port": m.port, "layer": m.layer, "engine": m.engine,
            "services": svc, "enabled": m.enabled, "listening": listening, "ok": ok,
            **protocol_users(reg, m, libs, groups_by_id),
        })

    for u in BASE_UNITS:
        st = states.get(u, {})
        if st.get("load") == "loaded" and st.get("active") != "active":
            problems.append(f"сервис {u} — {st.get('active')}")
    timer = states.get("zoo-collector.timer", {})
    if timer.get("load") == "loaded" and timer.get("enabled") == "enabled":
        if timer.get("active") != "active":
            problems.append(f"zoo-collector.timer — {timer.get('active')}: трафик не собирается")
        run = traffic.last_run()
        if run and run["stale"]:
            problems.append(f"коллектор трафика молчит {output.human_duration(run['age'])} "
                            "(journalctl -u zoo-collector)")
        for src, err in ((run or {}).get("errors") or {}).items():
            problems.append(f"трафик, {src}: {err}")
    firewall = system.ufw_active()
    if firewall is False:
        problems.append("UFW выключен: открыты все порты (sudo ufw enable или фаза 01)")

    allowed = system.registry_ports() | {("tcp", p) for p in system.ssh_ports(sockets)}
    for m in good:
        allowed |= {(p, m.port) for p in m.protos}
    exposed = [s.to_dict() for s in system.unexpected_public(sockets, allowed)]
    for s in exposed:
        problems.append(f"лишний listen на всех адресах: {s['port']}/{s['proto']} ({s['process'] or '?'})")

    cert_paths: list[str] = []
    for m in good:
        cert_paths += _manifest_certs(m)
    for g in CERT_GLOBS:
        cert_paths += sorted(glob.glob(g))
    certs = [system.cert_info(p) for p in dict.fromkeys(cert_paths)]
    for c in certs:
        if c["days_left"] is not None and c["days_left"] < CERT_WARN_DAYS:
            problems.append(f"сертификат {c['path']}: осталось {c['days_left']} дн.")

    xray: dict[str, Any] = {}
    if with_xui and cfg.get("PANEL_PORT"):
        try:
            st = XuiClient.from_config(cfg, timeout=5).server_status()
            x = st.get("xray") or {}
            xray = {"state": x.get("state", ""), "version": x.get("version", ""),
                    "error": x.get("errorMsg", ""), "panel": st.get("panelVersion", "")}
            if xray["state"] != "running":
                problems.append(f"Xray: {xray['state'] or '?'} {xray['error']}".strip())
        except XuiError as e:
            xray = {"error": str(e)}
            problems.append(f"API 3x-ui: {e}")

    return {
        "server": {"ip": cfg.get("SERVER_IP"), "label": cfg.get("LABEL"), "hostname": platform.node()},
        "zoo": {"version": __version__, "home": str(paths.zoo_home())},
        "protocols": protocols,
        "services": states,
        "firewall": {"ufw_active": firewall},
        "xray": xray,
        "exposed": exposed,
        "certs": certs,
        "versions": {"installed": system.component_versions(),
                     "pinned": system.pinned_versions(paths.scripts_dir() / "versions.env")},
        "users": user_counts,
        "problems": problems,
    }
