"""Состояние хоста: метрики из /proc, юниты systemd, слушающие сокеты, сертификаты, версии."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

PROC = Path(os.environ.get("ZOO_PROC", "/proc"))


def run(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Команда без shell; нет бинаря → rc 127."""
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return cp.returncode, cp.stdout, cp.stderr
    except FileNotFoundError:
        return 127, "", f"нет команды {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"таймаут {argv[0]}"


# ---------- метрики /proc ----------

def _cpu_times() -> tuple[int, int]:
    """(idle, total) из первой строки /proc/stat."""
    with open(PROC / "stat", encoding="ascii") as f:
        parts = f.readline().split()
    values = [int(x) for x in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return idle, sum(values[:8])


def cpu_percent(interval: float = 0.2) -> float:
    idle1, total1 = _cpu_times()
    time.sleep(interval)
    idle2, total2 = _cpu_times()
    dt = total2 - total1
    return round(100.0 * (1 - (idle2 - idle1) / dt), 1) if dt > 0 else 0.0


def meminfo() -> dict[str, int]:
    """Байты: total, available, used, swap_total, swap_free."""
    raw: dict[str, int] = {}
    with open(PROC / "meminfo", encoding="ascii") as f:
        for line in f:
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                raw[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    total = raw.get("MemTotal", 0)
    avail = raw.get("MemAvailable", raw.get("MemFree", 0))
    return {"total": total, "available": avail, "used": total - avail,
            "swap_total": raw.get("SwapTotal", 0), "swap_free": raw.get("SwapFree", 0)}


def disk_usage(path: str = "/") -> dict[str, int]:
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free}


def net_counters(include_lo: bool = False) -> dict[str, dict[str, int]]:
    """{iface: {rx_bytes, rx_packets, tx_bytes, tx_packets}} из /proc/net/dev."""
    out: dict[str, dict[str, int]] = {}
    with open(PROC / "net" / "dev", encoding="ascii") as f:
        for line in f.readlines()[2:]:
            name, _, data = line.partition(":")
            name = name.strip()
            if not include_lo and name == "lo":
                continue
            v = [int(x) for x in data.split()]
            if len(v) >= 10:
                out[name] = {"rx_bytes": v[0], "rx_packets": v[1], "tx_bytes": v[8], "tx_packets": v[9]}
    return out


def uptime_seconds() -> float:
    with open(PROC / "uptime", encoding="ascii") as f:
        return float(f.read().split()[0])


def loadavg() -> tuple[float, float, float]:
    with open(PROC / "loadavg", encoding="ascii") as f:
        a, b, c = f.read().split()[:3]
    return float(a), float(b), float(c)


def host_metrics(cpu_interval: float = 0.2) -> dict[str, Any]:
    """Сводка для status/web. Нечитаемый источник не роняет сводку — значение None."""
    def safe(fn, *a):
        try:
            return fn(*a)
        except (OSError, ValueError, IndexError):
            return None

    return {
        "cpu_percent": safe(cpu_percent, cpu_interval),
        "cpu_count": os.cpu_count(),
        "load": safe(loadavg),
        "mem": safe(meminfo),
        "disk": safe(disk_usage, "/"),
        "net": safe(net_counters),
        "uptime": safe(uptime_seconds),
    }


# ---------- systemd ----------

UNIT_PROPS = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "MainPID",
              "ActiveEnterTimestamp", "NRestarts")


def parse_systemctl_show(text: str) -> list[dict[str, str]]:
    """Блоки KEY=VALUE, разделённые пустой строкой (systemctl show u1 u2 ...)."""
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        blocks.append(cur)
    return blocks


def unit_states(units: list[str]) -> dict[str, dict[str, str]]:
    """{unit: {load, active, sub, enabled, pid, since, restarts}}. Нет systemctl → active=unknown."""
    units = [u for u in dict.fromkeys(units) if u]
    if not units:
        return {}
    rc, out, err = run(["systemctl", "show", "--no-pager", "-p", ",".join(UNIT_PROPS), *units])
    if rc != 0 and not out:
        return {u: {"load": "unknown", "active": "unknown", "sub": "", "enabled": "",
                    "pid": "", "since": "", "restarts": "", "error": err.strip()} for u in units}
    result: dict[str, dict[str, str]] = {}
    for unit, b in zip(units, parse_systemctl_show(out)):
        result[unit] = {
            "load": b.get("LoadState", ""), "active": b.get("ActiveState", ""),
            "sub": b.get("SubState", ""), "enabled": b.get("UnitFileState", ""),
            "pid": b.get("MainPID", ""), "since": b.get("ActiveEnterTimestamp", ""),
            "restarts": b.get("NRestarts", ""),
        }
    return result


def ufw_active() -> bool | None:
    """Включён ли UFW. Не по ufw.service: тот oneshot и остаётся inactive, если
    `ufw enable` выполнили после загрузки. None — ufw не установлен."""
    rc, out, _ = run(["ufw", "status"])
    if rc == 127:
        return None
    return out.strip().startswith("Status: active")


# ---------- сокеты ----------

@dataclass
class Socket:
    proto: str      # tcp | udp
    addr: str       # 0.0.0.0, ::, 127.0.0.1, *
    port: int
    process: str = ""
    pid: int = 0

    @property
    def public(self) -> bool:
        """Слушает на всех адресах (0.0.0.0 / :: / *)."""
        return self.addr in ("*", "0.0.0.0", "::", "[::]", "::ffff:0.0.0.0")

    @property
    def loopback(self) -> bool:
        a = self.addr.strip("[]")
        return a.startswith("127.") or a in ("::1", "::ffff:127.0.0.1") or a.startswith("::ffff:127.")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["public"] = self.public
        return d


_PROC_RE = re.compile(r'\("([^"]*)",pid=(\d+)')


def parse_ss(text: str) -> list[Socket]:
    """Вывод `ss -tulnpH`: Netid State Recv-Q Send-Q Local Peer [Process]."""
    socks: list[Socket] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] not in ("tcp", "udp"):
            continue
        local = parts[4]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        addr = addr.split("%", 1)[0]  # 127.0.0.53%lo
        if addr.startswith("[") and addr.endswith("]"):
            addr = addr[1:-1]
        m = _PROC_RE.search(line)
        socks.append(Socket(parts[0], addr or "*", int(port),
                            m.group(1) if m else "", int(m.group(2)) if m else 0))
    return socks


def listening_sockets() -> list[Socket]:
    rc, out, _ = run(["ss", "-tulnpH"])
    return parse_ss(out) if rc == 0 else []


def registry_ports(ports_file: Path | None = None) -> set[tuple[str, int]]:
    """(proto, port) из ports.tsv (порт/proto<TAB>...); диапазоны A:B раскрываются."""
    f = Path(ports_file) if ports_file else paths.ports_file()
    out: set[tuple[str, int]] = set()
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        spec = line.split("\t", 1)[0].strip()
        m = re.match(r"^(\d+)(?::(\d+))?/(tcp|udp)$", spec)
        if not m:
            continue
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        out.update((m.group(3), p) for p in range(lo, min(hi, 65535) + 1))
    return out


def unexpected_public(sockets: list[Socket], allowed: set[tuple[str, int]]) -> list[Socket]:
    """Сокеты на 0.0.0.0/::, которых нет среди разрешённых (реестр портов, SSH, манифесты)."""
    seen: set[tuple[str, int]] = set()
    out = []
    for s in sockets:
        key = (s.proto, s.port)
        if s.public and key not in allowed and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def ssh_ports(sockets: list[Socket]) -> set[int]:
    return {s.port for s in sockets if s.proto == "tcp" and s.process.startswith("sshd")} or {22}


# ---------- сертификаты ----------

def cert_expiry(path: str | Path) -> datetime | None:
    """notAfter сертификата (UTC) через openssl; не сертификат → None."""
    rc, out, _ = run(["openssl", "x509", "-enddate", "-noout", "-in", str(path)])
    if rc != 0 or "notAfter=" not in out:
        return None
    value = out.strip().split("=", 1)[1]
    try:
        # пробел в формате strptime совпадает с любым числом пробелов: «Oct  4 ...»
        return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cert_info(path: str | Path) -> dict[str, Any]:
    exp = cert_expiry(path)
    days = None
    if exp:
        days = int((exp - datetime.now(timezone.utc)).total_seconds() // 86400)
    return {"path": str(path), "not_after": exp.isoformat() if exp else None, "days_left": days}


# ---------- версии ----------

def _first_line(argv: list[str]) -> str:
    rc, out, err = run(argv, timeout=5)
    text = (out or err).strip()
    return text.splitlines()[0] if rc == 0 and text else ""


def _hysteria_version() -> str:
    """`hysteria version` печатает «Version:<TAB>v2.12.3» среди прочих строк."""
    if not shutil.which("hysteria"):
        return ""
    rc, out, _ = run(["hysteria", "version"], timeout=5)
    for line in out.splitlines():
        if line.lower().startswith("version"):
            return line.split()[-1]
    return ""


SINGBOX_PROBE_BIN = "/usr/local/lib/vpn-zoo/bin/sing-box"


def component_versions() -> dict[str, str]:
    """Установленные версии компонентов (пусто — компонента нет)."""
    xui_bin = Path("/usr/local/x-ui/x-ui")
    xray = ""
    for arch in ("amd64", "arm64"):
        b = xui_bin.parent / "bin" / f"xray-linux-{arch}"
        if b.exists():
            # «Xray 26.9.30 (Xray, Penetrates Everything.) ...» → 26.9.30
            parts = _first_line([str(b), "version"]).split()
            xray = parts[1] if len(parts) > 1 else ""
            break
    # «sing-box version 1.14.2» → 1.14.2; клиент самопроверки TUIC (фаза 04d)
    sb_bin = Path(SINGBOX_PROBE_BIN)
    sb = _first_line([str(sb_bin), "version"]).split()[-1:] if sb_bin.exists() else []
    return {
        "x-ui": _first_line([str(xui_bin), "-v"]) if xui_bin.exists() else "",
        "xray": xray,
        "hysteria": _hysteria_version(),
        "awg": _first_line(["awg", "--version"]) if shutil.which("awg") else "",
        "sing-box": sb[0] if sb else "",
        "python": platform.python_version(),
        "kernel": platform.release(),
    }


def pinned_versions(versions_env: Path) -> dict[str, str]:
    """*_VERSION / *_REF / *_TAG из versions.env."""
    out: dict[str, str] = {}
    try:
        lines = versions_env.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^([A-Z0-9_]+_(?:VERSION|REF|TAG))=['\"]?([^'\"\s]*)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out
