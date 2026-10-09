"""Клиенты протоколов для пробы.

Xray, Hysteria, sing-box — отдельный процесс с SOCKS5 на 127.0.0.1:<случайный порт> с
паролем (без пароля любой локальный процесс мог бы выйти через туннель). Конфиги с секретами —
во временном каталоге 0700, удаляются после пробы.

AmneziaWG — интерфейс awg (модуль ядра или amneziawg-go):
  netns — на сервере: клиент в отдельном сетевом неймспейсе, связь с хостом через veth;
          весь трафик неймспейса идёт в туннель, сервер и его маршруты не затрагиваются.
  bind  — в контейнере пробника (netns там создать нельзя без SYS_ADMIN): интерфейс в
          основном неймспейсе, в туннель уходят только сокеты, привязанные к нему
          (SO_BINDTODEVICE + своя таблица маршрутизации), DNS — тоже через туннель.

MTProxy — без процесса: рукопожатие Fake-TLS прямо из Python (MtprotoClient, handshake_only).

У каждого клиента: start(), handshake(timeout) → (статус, ошибка, мс), run_jobs(jobs) →
результаты fetch(), stop(), log_tail().
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import fetch as fetch_mod
from . import mtproto
from .endpoints import endpoint_of, split_hostport

BIN_ENV = {"xray": "ZOO_XRAY_BIN", "hysteria": "ZOO_HYSTERIA_BIN", "sing-box": "ZOO_SINGBOX_BIN",
           "amneziawg-go": "ZOO_AWG_GO_BIN", "awg": "ZOO_AWG_BIN"}
# /usr/local/lib/vpn-zoo/bin — клиенты только для самопроверки сервера (sing-box для TUIC, фаза 04d)
BIN_DIRS = ("/opt/zoo-probe/bin", "/usr/local/bin", "/usr/local/sbin", "/usr/bin", "/usr/sbin",
            "/usr/local/lib/vpn-zoo/bin")
AWG_SOCK_DIR = "/var/run/amneziawg"
# ключи wg-quick, которых не понимает `awg setconf`
WG_QUICK_KEYS = {"address", "dns", "mtu", "table", "preup", "postup", "predown", "postdown", "saveconfig"}


class ClientError(Exception):
    """Проблема на стороне пробника (не сеть): конфиг, запуск, права."""


class ClientMissing(ClientError):
    """Нет нужного бинаря на этой машине — протокол здесь не проверить."""


def machine_arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "amd64": "amd64"}.get(m, m)


def find_binary(name: str) -> str | None:
    env = os.environ.get(BIN_ENV.get(name, ""), "")
    if env:
        return env if os.access(env, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    for d in BIN_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    if name == "xray":
        # Xray из 3x-ui на сервере
        for p in [f"/usr/local/x-ui/bin/xray-linux-{machine_arch()}"] + sorted(glob.glob("/usr/local/x-ui/bin/xray-linux-*")):
            if os.access(p, os.X_OK):
                return p
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_private(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _tail(path: Path, n: int = 6) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(x.rstrip() for x in lines[-n:] if x.strip())


def _run(argv: list[str], timeout: float = 15.0, check: bool = True, input_text: str | None = None) -> str:
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text,
                            stdin=None if input_text is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise ClientError(f"нет команды {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise ClientError(f"{' '.join(argv[:3])}: таймаут") from None
    if check and cp.returncode != 0:
        raise ClientError(f"{' '.join(argv[:4])}: {(cp.stderr or cp.stdout).strip()[-300:]}")
    return cp.stdout


class _Process:
    def __init__(self, argv: list[str], log: Path, env: dict[str, str] | None = None) -> None:
        self.log = log
        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                                         env={**os.environ, **(env or {})}, start_new_session=True)
        except OSError as e:
            raise ClientError(f"не запустить {argv[0]}: {e}") from None
        finally:
            os.close(fd)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (OSError, AttributeError):
            self.proc.terminate()
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                self.proc.kill()
            self.proc.wait(3)


def _wait_port(port: int, proc: _Process, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not proc.alive():
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ---------- клиенты с локальным SOCKS5 ----------

class SocksClient:
    binary = ""
    label = ""

    def __init__(self, probe: dict[str, Any], workdir: Path, stall: float = 8.0) -> None:
        self.probe = probe
        self.workdir = workdir
        self.stall = stall
        self.port = free_port()
        self.user = "zp" + secrets.token_hex(4)
        self.password = secrets.token_urlsafe(18)
        self.cfg_path = workdir / f"{self.label}-{self.port}.json"
        self.log_path = workdir / f"{self.label}-{self.port}.log"
        self.proc: _Process | None = None

    # для fetch()
    @property
    def socks(self) -> dict[str, Any]:
        return {"host": "127.0.0.1", "port": self.port, "user": self.user, "password": self.password}

    def config(self) -> dict[str, Any]:
        raise NotImplementedError

    def argv(self, binary: str) -> list[str]:
        raise NotImplementedError

    env: dict[str, str] = {}

    def start(self, timeout: float) -> None:
        binary = find_binary(self.binary)
        if not binary:
            raise ClientMissing(f"нет клиента {self.binary} на этой машине")
        _write_private(self.cfg_path, json.dumps(self.config(), ensure_ascii=False, indent=1))
        self.proc = _Process(self.argv(binary), self.log_path, self.env)
        self._wait_ready(timeout)

    def _wait_ready(self, timeout: float) -> None:
        assert self.proc
        if not _wait_port(self.port, self.proc, min(timeout, 8.0)):
            raise ClientError(f"{self.binary} не запустился: {self.log_tail() or 'нет вывода'}")

    def handshake(self, timeout: float) -> tuple[str, str, float | None]:
        """Xray и sing-box подключаются лениво: рукопожатие видно по первому запросу."""
        return "unknown", "", None

    def run_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [fetch_mod.fetch(socks=self.socks, **job) for job in jobs]

    def log_tail(self, n: int = 6) -> str:
        return _tail(self.log_path, n)

    def stop(self) -> None:
        if self.proc:
            self.proc.stop()
        for p in (self.cfg_path,):
            try:
                p.unlink()
            except OSError:
                pass

    def __enter__(self) -> "SocksClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class XrayClient(SocksClient):
    binary = "xray"
    label = "xray"

    def config(self) -> dict[str, Any]:
        ob = dict(self.probe["outbound"])
        ob.setdefault("tag", "proxy")
        return {
            "log": {"loglevel": "warning"},
            "inbounds": [{
                "tag": "zoo-probe-in", "listen": "127.0.0.1", "port": self.port, "protocol": "socks",
                "settings": {"auth": "password", "udp": False,
                             "accounts": [{"user": self.user, "pass": self.password}]},
            }],
            "outbounds": [ob],
        }

    def argv(self, binary: str) -> list[str]:
        return [binary, "run", "-c", str(self.cfg_path)]


class SingBoxClient(SocksClient):
    binary = "sing-box"
    label = "sing-box"

    def config(self) -> dict[str, Any]:
        ob = dict(self.probe["outbound"])
        ob["tag"] = "proxy"
        return {
            "log": {"level": "warn", "timestamp": True},
            "inbounds": [{"type": "socks", "tag": "zoo-probe-in", "listen": "127.0.0.1", "listen_port": self.port,
                          "users": [{"username": self.user, "password": self.password}]}],
            "outbounds": [ob],
            "route": {"final": "proxy"},
        }

    def argv(self, binary: str) -> list[str]:
        return [binary, "run", "-c", str(self.cfg_path)]


class HysteriaClient(SocksClient):
    """Hysteria подключается сразу при старте (lazy: false) и пишет в лог итог рукопожатия."""
    binary = "hysteria"
    label = "hysteria"
    env = {"HYSTERIA_DISABLE_UPDATE_CHECK": "1", "HYSTERIA_LOG_LEVEL": "info"}

    # только поля подключения к серверу: tun/tcpTProxy/tcpForwarding/http из пакета пробник
    # (root на машине пользователя) включать не должен
    CLIENT_KEYS = {"server", "auth", "tls", "obfs", "bandwidth", "quic", "fastOpen", "transport"}

    def config(self) -> dict[str, Any]:
        cfg = {k: v for k, v in dict(self.probe["client"]).items() if k in self.CLIENT_KEYS}
        cfg["lazy"] = False
        cfg["socks5"] = {"listen": f"127.0.0.1:{self.port}", "username": self.user, "password": self.password,
                         "disableUDP": True}
        return cfg

    def argv(self, binary: str) -> list[str]:
        return [binary, "client", "-c", str(self.cfg_path), "--disable-update-check"]

    def _wait_ready(self, timeout: float) -> None:
        """Порт SOCKS ждём в handshake(): до рукопожатия hysteria его не открывает."""

    def handshake(self, timeout: float) -> tuple[str, str, float | None]:
        assert self.proc
        t0 = time.monotonic()
        end = t0 + timeout
        while time.monotonic() < end:
            text = self._log_text()
            if "connected to server" in text:
                ms = round((time.monotonic() - t0) * 1000, 1)
                if _wait_port(self.port, self.proc, 5.0):
                    return "ok", "", ms
                return "fail", f"hysteria: SOCKS не открылся: {self.log_tail(3)}", ms
            if not self.proc.alive():
                return hysteria_failure(text)
            time.sleep(0.1)
        return "timeout", f"hysteria: нет рукопожатия за {timeout:.0f} с", None

    def _log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def hysteria_failure(log: str) -> tuple[str, str, float | None]:
    """Разбор FATAL-строки клиента hysteria: timeout — сервер молчит, rejected — ответил отказом."""
    line = ""
    for x in log.splitlines():
        if "FATAL" in x or "ERROR" in x or "error" in x:
            line = x.strip()
    m = re.search(r'"error":\s*"([^"]*)"', line)
    msg = m.group(1) if m else (line[-200:] or "клиент завершился")
    low = msg.lower()
    if "timeout" in low or "no recent network activity" in low or "deadline" in low:
        return "timeout", f"hysteria: {msg}", None
    if any(k in low for k in ("auth", "certificate", "pin", "x509", "tls", "crypto_error", "status code")):
        return "rejected", f"hysteria: {msg}", None
    if "connection refused" in low or "unreachable" in low:
        return "timeout", f"hysteria: {msg}", None
    return "fail", f"hysteria: {msg}", None


# ---------- AmneziaWG ----------

def parse_conf(conf: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """.conf → [(секция, [(ключ, значение)])] с сохранением порядка и регистра ключей."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for raw in conf.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            sections.append((line[1:-1].strip(), []))
            continue
        k, sep, v = line.partition("=")
        if sep and sections:
            sections[-1][1].append((k.strip(), v.strip()))
    return sections


def strip_conf(conf: str) -> str:
    """Как `awg-quick strip`: без Address/DNS/MTU/Table/Pre*/Post*/SaveConfig."""
    out = []
    for name, items in parse_conf(conf):
        out.append(f"[{name}]")
        out += [f"{k} = {v}" for k, v in items if k.lower() not in WG_QUICK_KEYS]
        out.append("")
    return "\n".join(out)


def conf_value(conf: str, section: str, key: str) -> str:
    for name, items in parse_conf(conf):
        if name.lower() == section.lower():
            for k, v in items:
                if k.lower() == key.lower():
                    return v
    return ""


class AwgTunnel:
    GATEWAY = "@gateway"  # подставить адрес хоста на veth (проба «через loopback» на сервере)

    def __init__(self, probe: dict[str, Any], workdir: Path, mode: str, stall: float = 8.0) -> None:
        self.probe = probe
        self.workdir = workdir
        self.mode = mode
        self.stall = stall
        conf = probe.get("conf") or ""
        if not conf:
            raise ClientError("в probe нет conf")
        self.conf = conf
        tag = f"{os.getpid() % 10000}{secrets.randbelow(100):02d}"
        self.iface = f"zpw{tag}"
        self.ns = f"zoo-probe-{tag}"
        self.veth_host = f"zpv{tag}h"
        self.veth_ns = f"zpv{tag}n"
        self.table = 7000 + secrets.randbelow(900)
        self.gw = ""
        self.ns_ip = ""
        addr = conf_value(conf, "Interface", "Address").split(",")[0].strip()
        self.address = addr if "/" in addr else (addr + "/32" if addr else "")
        dns = conf_value(conf, "Interface", "DNS").split(",")[0].strip()
        self.dns = dns or "1.1.1.1"
        self.mtu = conf_value(conf, "Interface", "MTU") or str(probe.get("mtu") or 1280)
        self.go: _Process | None = None
        self.log_path = workdir / f"{self.iface}.log"
        self._made_ns = self._made_veth = self._made_iface = self._made_rule = False

    # ---------- подготовка ----------

    def _ip(self, *args: str, ns: bool = False, check: bool = True) -> str:
        argv = ["ip"] + (["-n", self.ns] if ns else []) + list(args)
        return _run(argv, check=check)

    def _in_ns(self, argv: list[str]) -> list[str]:
        return ["ip", "netns", "exec", self.ns] + argv if self.mode == "netns" else argv

    def start(self, timeout: float) -> None:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise ClientError("AmneziaWG: нужен root (создание интерфейса)")
        if not shutil.which("ip"):
            raise ClientError("нет команды ip (iproute2)")
        awg = find_binary("awg")
        if not awg:
            raise ClientMissing("нет утилиты awg (amneziawg-tools)")
        if not self.address:
            raise ClientError("в .conf нет Address")
        if self.mode == "netns":
            self._setup_netns()
        self._create_iface()
        conf = self.conf
        if self.GATEWAY in conf:
            conf = conf.replace(self.GATEWAY, self.gw)
        stripped = self.workdir / f"{self.iface}.conf"
        _write_private(stripped, strip_conf(conf))
        try:
            _run(self._in_ns([awg, "setconf", self.iface, str(stripped)]))
        finally:
            stripped.unlink(missing_ok=True)
        self._ip("addr", "add", self.address, "dev", self.iface, ns=self.mode == "netns")
        self._ip("link", "set", self.iface, "mtu", self.mtu, "up", ns=self.mode == "netns")
        if self.mode == "netns":
            self._ip("route", "add", "default", "dev", self.iface, ns=True)
        else:
            self._ip("route", "add", "default", "dev", self.iface, "table", str(self.table), check=False)
            # без правила тоже работает (сокет с oif на TUN считается on-link), правило — для ясности
            self._ip("rule", "add", "oif", self.iface, "lookup", str(self.table), "pref",
                     str(self.table), check=False)
            self._made_rule = True
            # обратный путь ответов из туннеля не должен резаться rp_filter
            try:
                Path(f"/proc/sys/net/ipv4/conf/{self.iface}/rp_filter").write_text("0")
            except OSError:
                pass

    def _setup_netns(self) -> None:
        host, _ = split_hostport(self.probe.get("endpoint") or "")
        self._ip("netns", "add", self.ns)
        self._made_ns = True
        self.gw, self.ns_ip = self._pick_pair()
        self._ip("link", "add", self.veth_host, "type", "veth", "peer", "name", self.veth_ns, "netns", self.ns)
        self._made_veth = True
        self._ip("addr", "add", f"{self.gw}/30", "dev", self.veth_host)
        self._ip("link", "set", self.veth_host, "up")
        self._ip("link", "set", "lo", "up", ns=True)
        self._ip("addr", "add", f"{self.ns_ip}/30", "dev", self.veth_ns, ns=True)
        self._ip("link", "set", self.veth_ns, "up", ns=True)
        if host != self.GATEWAY:
            # до сервера — через хост, остальное — в туннель
            self._ip("route", "add", f"{host}/32", "via", self.gw, ns=True)

    @staticmethod
    def _pick_pair() -> tuple[str, str]:
        """Свободная /30 в 169.254.0.0/16 для veth: (адрес хоста, адрес неймспейса)."""
        used = _run(["ip", "-4", "-o", "addr", "show"], check=False)
        for _ in range(50):
            prefix = f"169.254.{100 + secrets.randbelow(150)}."
            if prefix not in used:
                base = 4 * secrets.randbelow(60)
                return f"{prefix}{base + 1}", f"{prefix}{base + 2}"
        raise ClientError("не нашёл свободную подсеть 169.254.x.x/30 для veth")

    def _create_iface(self) -> None:
        # модуль ядра, если есть; иначе userspace amneziawg-go
        out = subprocess.run(self._in_ns(["ip", "link", "add", self.iface, "type", "amneziawg"]),
                             capture_output=True, text=True)
        if out.returncode == 0:
            self._made_iface = True
            return
        go = find_binary("amneziawg-go")
        if not go:
            raise ClientMissing("нет amneziawg-go и модуля ядра amneziawg")
        if not os.path.exists("/dev/net/tun"):
            raise ClientError("нет /dev/net/tun (docker run --device /dev/net/tun --cap-add NET_ADMIN)")
        self.go = _Process(self._in_ns([go, "-f", self.iface]), self.log_path,
                           {"WG_PROCESS_FOREGROUND": "1", "LOG_LEVEL": "error"})
        sock = Path(AWG_SOCK_DIR) / f"{self.iface}.sock"
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if sock.exists():
                self._made_iface = True
                return
            if not self.go.alive():
                break
            time.sleep(0.1)
        raise ClientError(f"amneziawg-go не поднял {self.iface}: {_tail(self.log_path) or 'нет вывода'}")

    # ---------- проба ----------

    def handshake(self, timeout: float) -> tuple[str, str, float | None]:
        awg = find_binary("awg") or "awg"
        t0 = time.monotonic()
        end = t0 + timeout
        kicked = 0.0
        while time.monotonic() < end:
            if time.monotonic() - kicked > 2:
                self._kick()
                kicked = time.monotonic()
            out = _run(self._in_ns([awg, "show", self.iface, "latest-handshakes"]), check=False) or ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 0:
                    return "ok", "", round((time.monotonic() - t0) * 1000, 1)
            if self.go and not self.go.alive():
                return "fail", f"amneziawg-go завершился: {_tail(self.log_path)}", None
            time.sleep(0.3)
        return "timeout", f"AmneziaWG: нет рукопожатия за {timeout:.0f} с", None

    def _kick(self) -> None:
        """Пакет в туннель, чтобы клиент начал рукопожатие (DNS-запрос, ответ не важен)."""
        if self.mode == "netns":
            self.run_jobs([], kick=True)
            return
        try:
            fetch_mod.resolve("example.com", self.dns, self.iface, timeout=0.3, tries=1)
        except fetch_mod.FetchError:
            pass

    def run_jobs(self, jobs: list[dict[str, Any]], kick: bool = False) -> list[dict[str, Any]]:
        if self.mode == "netns":
            spec = {"jobs": [dict(job, dns=self.dns) for job in jobs]}
            if kick:
                spec = {"jobs": [{"url": "http://example.com/", "dns": self.dns, "connect_timeout": 0.5,
                                  "stall": 0.5, "max_time": 1}]}
            fetch_py = str(Path(fetch_mod.__file__).resolve())
            timeout = sum(float(j.get("max_time", 60)) + float(j.get("connect_timeout", 10)) for j in spec["jobs"]) + 10
            try:
                cp = subprocess.run(self._in_ns([sys.executable, fetch_py]), input=json.dumps(spec),
                                    capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ClientError("замер в netns: таймаут процесса") from None
            if kick:
                return []
            try:
                return json.loads(cp.stdout)
            except json.JSONDecodeError:
                raise ClientError(f"замер в netns: {cp.stderr.strip()[-300:]}") from None
        return [fetch_mod.fetch(bind_dev=self.iface, dns=self.dns, **job) for job in jobs]

    def log_tail(self, n: int = 6) -> str:
        return _tail(self.log_path, n)

    def stop(self) -> None:
        if self._made_rule:
            self._ip("rule", "del", "pref", str(self.table), check=False)
            self._ip("route", "flush", "table", str(self.table), check=False)
        if self._made_iface:
            self._ip("link", "del", self.iface, ns=self.mode == "netns", check=False)
        if self.go:
            self.go.stop()
        if self._made_ns:
            self._ip("netns", "del", self.ns, check=False)
        if self._made_veth:
            self._ip("link", "del", self.veth_host, check=False)
        try:
            (Path(AWG_SOCK_DIR) / f"{self.iface}.sock").unlink()
        except OSError:
            pass

    def __enter__(self) -> "AwgTunnel":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class MtprotoClient:
    """MTProxy: стороннего клиента нет — рукопожатие Fake-TLS с секретом пользователя (probe/mtproto.py). Запросов
    через прокси нет: Telegram говорит с ним своим протоколом, движок после рукопожатия останавливается."""
    handshake_only = True
    NOTE = "проверено рукопожатие Fake-TLS с секретом; сам Telegram через прокси не проверялся"

    def __init__(self, probe: dict[str, Any]) -> None:
        self.probe = probe

    def start(self, timeout: float) -> None:
        try:
            mtproto.parse_secret(str(self.probe.get("secret") or ""))
        except ValueError as e:
            raise ClientError(f"probe mtproto: {e}") from None

    def handshake(self, timeout: float) -> tuple[str, str, float | None]:
        ep = endpoint_of(self.probe)
        return mtproto.handshake(ep.host, ep.port, str(self.probe["secret"]), timeout)

    def run_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise ClientError("MTProxy: запросов через прокси пробник не делает")

    def log_tail(self, n: int = 6) -> str:
        return ""

    def stop(self) -> None:
        pass


def make_client(probe: dict[str, Any], workdir: Path, mode: str, stall: float = 8.0):
    kind = probe.get("kind")
    if kind == "xray":
        return XrayClient(probe, workdir, stall)
    if kind == "hysteria":
        return HysteriaClient(probe, workdir, stall)
    if kind == "sing-box":
        return SingBoxClient(probe, workdir, stall)
    if kind == "awg":
        return AwgTunnel(probe, workdir, "netns" if mode == "local" else "bind", stall)
    if kind == "mtproto":
        return MtprotoClient(probe)
    raise ClientError(f"неизвестный probe.kind: {kind!r}")
