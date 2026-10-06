"""Журнал атак «Кто нас щупал» (ARCHITECTURE §6, D32): кто и чем пробовал сервер снаружи.

Коллектор (`zoo journal --collect`, его запускает zoo-collector.service после снятия трафика)
читает журнал systemd и лог fail2ban с прошлого раза, раскладывает события по часам и суткам
в SQLite (journal.sqlite рядом с traffic.sqlite). Отчёты (CLI и веб) читают только её.

Источники — только то, что уже пишет сервер, без новых исходящих соединений:
    ufw         блокировки пакетов на закрытые порты из журнала ядра («[UFW BLOCK]», только с внешнего
                интерфейса; ufw пишет их не чаще 3 в минуту на весь сервер — это выборка, а не итог)
                и отказы `ufw limit` на SSH («[UFW LIMIT BLOCK]»)
    ssh         неудачные входы и сканеры SSH из журнала sshd; успешный вход по ключу помечает адрес «своим»
    fail2ban    баны из /var/log/fail2ban.log
    hysteria    неверный ключ: строку «reject IP:порт» пишет помощник hy2-auth (сама Hysteria на уровне
                error молчит, на info пишет адреса и имена пользователей, на debug — их назначения)
    xray        «REALITY: processed invalid connection» — Xray пишет её на уровне Info, а на нём в журнал
                попадают и назначения пользователей, поэтому по умолчанию строки нет (D32); разбор
                оставлен на случай, если владелец поднимет уровень сам
    3x-ui, zoo-web   неудачные входы в панели (слушают только loopback)

Чего здесь нет и не будет: трафик и назначения пользователей VPN. Пишутся только адрес источника,
порт назначения, вид события и счётчик; пакеты с VPN-интерфейсов (awg0 и т. п.) отбрасываются
разбором ufw, строки журнала целиком не сохраняются, имена пользователей и пароли — тоже.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import config, geoip, output, paths, system, traffic
from .config import Config
from .fsutil import LockTimeout, file_lock

DB_NAME = "journal.sqlite"
SCHEMA = 1

RES_1H, RES_1D = traffic.RES_1H, traffic.RES_1D
KEEP_HOURLY_DAYS = 8
DEFAULT_KEEP_DAYS = 90
OWN_KEEP_DAYS = 30          # «свой» адрес забывается через месяц без входов: динамический IP достаётся другим
PERIODS = ("24h", "7d", "30d", "90d")
FIRST_RUN_DAYS = 3          # сколько журнала разобрать при первом запуске
MAX_LINES = 50_000          # строк одного источника за запуск; остаток — в следующий
MAX_LAG = 6 * 3600          # отставание больше — пропускаем накопившееся
PORT_CAP = 32               # разных портов у одного адреса за запуск; остальные — порт 0
F2B_LOG = Path("/var/log/fail2ban.log")
F2B_TAIL = 4 * 1024 * 1024
SPIKE_MIN = 100             # событий за час для тревоги на обзоре
SPIKE_FACTOR = 5            # и во столько раз больше обычного
EPHEMERAL_FROM = 32768      # начало ip_local_port_range Linux (и эфемерных портов клиентов за NAT AmneziaWG)

# вид события → (источник, подпись, группа)
KINDS: dict[str, tuple[str, str, str]] = {
    "port-scan": ("ufw", "закрытый порт", "ports"),
    "ssh-limit": ("ufw", "лимит подключений SSH", "ssh"),
    "ssh-auth": ("ssh", "перебор SSH", "ssh"),
    "ssh-scan": ("ssh", "сканер SSH", "ssh"),
    "ssh-ban": ("fail2ban", "бан fail2ban", "ssh"),
    "hy2-auth": ("hysteria", "Hysteria2: неверный ключ", "proxy"),
    "reality-probe": ("xray", "REALITY: чужой клиент", "proxy"),
    "panel-login": ("3x-ui", "вход в 3x-ui", "login"),
    "web-login": ("zoo-web", "вход в админку zoo", "login"),
}
# справка по видам событий (одно место правды для веба): что это / опасно ли / что сделала защита / что делать.
# Факты сверены с кодом: 01-firewall.sh (ufw allow без limit — D37, fail2ban sshd 5 за 10 мин → 1 ч, recidive → 1 нед; banaction = ufw: бан адреса целиком, не порта),
# 01b-ssh.sh (пароли выключает только SSH_HARDEN=1, по умолчанию 0), 03-3xui.sh (панель на 127.0.0.1 или die),
# web/server.py (админка только loopback), proto-hysteria2.sh (хук пишет «reject АДРЕС»), PROBE-SURFACE (REALITY).
HELP_LABELS = ("Что это", "Опасно ли", "Что сделала защита", "Что делать")
KIND_HELP: dict[str, tuple[str, str, str, str]] = {
    "port-scan": (
        "Кто-то стучался в порт, который у вас закрыт: сканеры обходят адреса и порты в поиске сервисов.",
        "Нет. Это фон интернета: на любом сервере таких сотни в сутки.",
        "ufw закрывает всё, кроме нужных портов, и молча отбрасывает пакеты.",
        "Ничего."),
    "ssh-limit": (
        "Слишком частые подключения к SSH с одного адреса: сработало правило ufw limit (6 подключений за 30 секунд).",
        "Само по себе нет: похоже на перебор, но так же выглядят частые ssh и scp подряд.",
        "ufw отбросил лишние подключения. Установщик такое правило больше не ставит (D37): запись осталась "
        "от старой установки или от вашего правила.",
        "Ваш адрес — подождать полминуты. Чужой — ничего."),
    "ssh-auth": (
        "Подбор входа по SSH: неверный пароль или ключ, несуществующий пользователь.",
        "Если вход по паролю выключен — подобрать нельзя. Установщик выключает его только с SSH_HARDEN=1 "
        "(тогда вход лишь по ключу); иначе зависит от настроек хостера.",
        "fail2ban банит адрес на час после 5 неудач за 10 минут, повторных нарушителей — на неделю.",
        "Пароль включён — поставьте SSH_HARDEN=1 (README). Выключен — ничего."),
    "ssh-scan": (
        "Подключились к SSH и ушли без попытки войти: проверили версию или не договорились о шифрах.",
        "Нет.",
        "Входа не было, блокировать нечего.",
        "Ничего."),
    "ssh-ban": (
        "fail2ban заблокировал адрес целиком: jail sshd — все порты на час, recidive — все порты на неделю.",
        "Нет: это реакция защиты, а не атака (в «попытках» не считается).",
        "Правило ufw закрывает адресу все порты сервера (action ufw банит адрес, а не порт SSH); снимается само по сроку.",
        "Забанили себя — с другого адреса или из консоли хостера: sudo fail2ban-client unban АДРЕС."),
    "hy2-auth": (
        "К порту Hysteria2 пришли с ключом, которого у вас нет: чужой зонд или клиент со старым ключом.",
        "Само по себе нет: без верного ключа к VPN не подключиться. Частые попытки — активное зондирование.",
        "Помощник авторизации отказал; в журнал записан только адрес, ключ и имя не пишутся.",
        "Адрес ваш — обновите ссылку подключения у пользователя. Чужой — ничего."),
    "reality-probe": (
        "К порту VLESS пришёл клиент без верного ключа REALITY. Так цензор проверяет, прокси ли это.",
        "Нет: без ключа REALITY отвечает как настоящий сайт-прикрытие.",
        "Соединение ушло на сайт-прикрытие: сертификат и ответы его, а не ваши.",
        "Единичные — норма. Лавина с одного адреса — активная проверка: попробуйте другой SNI (VLESS_SNI, REFERENCE)."),
    "panel-login": (
        "Неудачный вход в панель 3x-ui.",
        "Панель слушает только 127.0.0.1 (установщик это проверяет): снаружи на неё не зайти.",
        "Вход отклонён.",
        "Адрес 127.0.0.1 — обычно ssh-туннель: вы или опечатка. Внешний — кто-то открыл панель наружу, закройте."),
    "web-login": (
        "Неверный токен при входе в админку zoo (или слишком частые попытки).",
        "Админка слушает только 127.0.0.1: зайти можно лишь через ssh-туннель.",
        "Вход отклонён, при частых попытках сервер отвечает 429.",
        "В записи всегда 127.0.0.1 (так пишется). Не вы — у кого-то есть доступ к серверу: смените токен админки."),
}
GROUPS: dict[str, str] = {
    "ports": "Стучались в закрытые порты",
    "ssh": "Перебор SSH",
    "proxy": "Проверяли, прокси ли это",
    "login": "Входы в панели",
}
# как группа называется в строке «…— попыток не было»; REALITY добавляется, только если он отслеживается
GROUP_QUIET: dict[str, str] = {"ports": "закрытые порты", "ssh": "SSH", "proxy": "Hysteria2", "login": "панели"}
REALITY_UNTRACKED = "не отслеживается (так задумано: иначе в логах были бы сайты пользователей)"
# реакция защиты, а не атака: в «событий» не считается
DEFENCE_KINDS = {"ssh-ban"}
OWN_LOGIN = "own-login"     # не событие журнала: адрес успешного входа по ключу

SCOPE_TITLES = {"own": "свой", "local": "служебный"}

BLIND = [
    ("REALITY", "Чужие клиенты REALITY не видны: Xray пишет их только на уровне Info, а на нём в журнал попадают "
                "и назначения пользователей. Следы ищите косвенно: адрес, стучавшийся в закрытые порты, "
                "скорее всего щупал и 443."),
    ("AmneziaWG", "Неверные рукопожатия AmneziaWG не логируются ни в ядре, ни в amneziawg-go: пакеты без "
                  "верной обфускации молча отбрасываются."),
    ("Hysteria2 + Salamander", "Пакеты с неверным паролем Salamander отбрасываются до разбора запроса: "
                               "Hysteria не знает, что это были за пакеты."),
]


@dataclass
class Event:
    ts: int
    kind: str
    ip: str
    port: int = 0

    @property
    def source(self) -> str:
        return KINDS[self.kind][0] if self.kind in KINDS else self.kind


def db_path() -> Path:
    return paths.state_dir() / DB_NAME


def keep_days() -> int:
    try:
        d = int(os.environ.get("ZOO_JOURNAL_KEEP_DAYS", DEFAULT_KEEP_DAYS))
    except ValueError:
        d = DEFAULT_KEEP_DAYS
    return max(7, min(365, d))


def ignore_file() -> Path:
    return paths.etc() / "journal-ignore.txt"


def geo_index_file() -> Path:
    return paths.state_dir() / "geoip-cc.bin"


# ---------- разбор строк ----------

def norm_ip(raw: str) -> str | None:
    """Адрес из лога → канонический вид; мусор и ::ffff:1.2.3.4 обрабатываются."""
    s = raw.strip().strip("[]")
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return None
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return str(a)


def _addr_port(raw: str) -> tuple[str | None, int]:
    """«1.2.3.4:5678», «[::1]:5678» → (адрес, порт)."""
    host, _, port = raw.rpartition(":")
    return norm_ip(host), int(port) if port.isdigit() else 0


_SSH_AUTH = re.compile(
    r"^(?:Invalid user \S* from (?P<a>\S+) port (?P<ap>\d+)"
    r"|Failed (?:password|publickey|keyboard-interactive/pam|none) for (?:invalid user )?\S* from (?P<b>\S+) port (?P<bp>\d+)"
    r"|(?:Connection closed by|Disconnected from) (?:authenticating|invalid) user \S* (?P<c>\S+) port (?P<cp>\d+)"
    r"|error: maximum authentication attempts exceeded for (?:invalid user )?\S* from (?P<d>\S+) port (?P<dp>\d+))")
_SSH_SCAN = re.compile(
    r"^(?:Unable to negotiate with (?P<a>\S+) port (?P<ap>\d+):"
    r"|Did not receive identification string from (?P<b>\S+) port (?P<bp>\d+)"
    r"|banner exchange: Connection from (?P<c>\S+) port (?P<cp>\d+):"
    r"|Bad protocol version identification .* from (?P<d>\S+) port (?P<dp>\d+)"
    r"|Connection (?:closed|reset) by (?P<e>[0-9a-fA-F:.]+) port (?P<ep>\d+)(?: \[preauth\])?$"
    r"|ssh_dispatch_run_fatal: Connection from (?P<f>\S+) port (?P<fp>\d+):)")
_SSH_OK = re.compile(r"^Accepted publickey for \S+ from (\S+) port \d+")


def parse_ssh(msg: str, ts: int) -> Event | None:
    """Строка sshd → событие. Одно подключение с неудачной аутентификацией — одно событие
    (в разборе по строкам их было бы несколько: Invalid user, Failed password, Connection closed)."""
    m = _SSH_OK.match(msg)
    if m:
        ip = norm_ip(m.group(1))
        return Event(ts, OWN_LOGIN, ip) if ip else None
    for rx, kind in ((_SSH_AUTH, "ssh-auth"), (_SSH_SCAN, "ssh-scan")):
        m = rx.match(msg)
        if not m:
            continue
        d = m.groupdict()
        for key in "abcdef":
            if d.get(key):
                ip = norm_ip(d[key])
                return Event(ts, kind, ip, 0) if ip else None
    return None


def ssh_conn_key(msg: str) -> tuple[str, str] | None:
    """(адрес, порт клиента) — ключ подключения для склейки строк одной попытки."""
    for rx in (_SSH_AUTH, _SSH_SCAN):
        m = rx.match(msg)
        if m:
            d = m.groupdict()
            for key in "abcdef":
                if d.get(key):
                    return d[key], d[key + "p"]
    return None


_KV = re.compile(r"\b([A-Z]+)=(\S*)")


def parse_ufw(msg: str, ts: int, wan: set[str]) -> Event | None:
    """Строка ядра «[UFW BLOCK] IN=eth0 … SRC=… DPT=…». Берём только то, что пришло снаружи на сам сервер:
    внешний интерфейс, без пересылки, TCP SYN или UDP на порт ниже EPHEMERAL_FROM. Пакеты с VPN-интерфейсов — это трафик
    пользователей, его не пишем."""
    if "[UFW BLOCK]" in msg:
        kind = "port-scan"
    elif "[UFW LIMIT BLOCK]" in msg:
        kind = "ssh-limit"
    else:
        return None
    f = dict(_KV.findall(msg))
    if f.get("IN", "") not in wan or f.get("OUT"):
        return None
    proto = f.get("PROTO", "")
    flags = set(re.findall(r"\b(SYN|ACK|RST|FIN|PSH|URG)\b", msg.split("URGP=")[0].split("WINDOW=")[-1]))
    if proto == "TCP":
        if "SYN" not in flags or "ACK" in flags:
            return None  # ответы и «хвосты» чужих соединений — не попытка подключения
    elif proto != "UDP":
        return None
    ip = norm_ip(f.get("SRC", ""))
    if not ip or not f.get("DPT", "").isdigit():
        return None
    if proto == "UDP" and int(f["DPT"]) >= EPHEMERAL_FROM:
        # запоздалый ответ на исходящий UDP пользователей (QUIC, DNS, звонки) после истечения conntrack:
        # SRC здесь — сайт, куда ходил пользователь, а не сканер
        return None
    try:
        if ipaddress.ip_address(f.get("DST", "0.0.0.0")).is_multicast:
            return None
    except ValueError:
        pass
    return Event(ts, kind, ip, int(f["DPT"]))


_F2B = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)? fail2ban\.actions\s+\[\d+\]: NOTICE\s+\[(\S+)\] Ban (\S+)\s*$")


def parse_f2b(line: str) -> Event | None:
    m = _F2B.match(line)
    if not m:
        return None
    ip = norm_ip(m.group(3))
    if not ip:
        return None
    try:
        ts = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
    except (ValueError, OverflowError):
        return None
    return Event(ts, "ssh-ban", ip, 0)


_HY2 = re.compile(r"^reject (\S+)$")
_XRAY = re.compile(r"REALITY: processed invalid connection from (\S+?): ")
_XUI = re.compile(r'failed login: .*\bIP="([^"]+)"')
_WEB = re.compile(r"^zoo-web: POST /login (?:401|429)$")


def parse_hy2(msg: str, ts: int) -> Event | None:
    m = _HY2.match(msg)
    if not m:
        return None
    ip, _ = _addr_port(m.group(1))
    return Event(ts, "hy2-auth", ip) if ip else None


def parse_xray(msg: str, ts: int) -> Event | None:
    m = _XRAY.search(msg)
    if not m:
        return None
    ip, _ = _addr_port(m.group(1))
    return Event(ts, "reality-probe", ip) if ip else None


def parse_xui(msg: str, ts: int) -> Event | None:
    m = _XUI.search(msg)
    if m:
        ip = norm_ip(m.group(1))
        return Event(ts, "panel-login", ip) if ip else None
    return parse_xray(msg, ts)


def parse_web(msg: str, ts: int) -> Event | None:
    return Event(ts, "web-login", "127.0.0.1") if _WEB.match(msg.strip()) else None


# ---------- адреса: свои, локальные, внешние ----------

def load_ignore(path: Path | None = None) -> list[Any]:
    """Сети из /etc/vpn-setup/journal-ignore.txt: адрес или CIDR в строке, # — комментарий."""
    nets = []
    try:
        text = (path or ignore_file()).read_text(encoding="utf-8")
    except OSError:
        return nets
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    return nets


@lru_cache(maxsize=1 << 16)
def _addr(ip: str) -> Any:
    """Разбор адреса (None — не адрес): на странице за 90 дней их десятки тысяч, разбор дорогой."""
    try:
        return ipaddress.ip_address(ip)
    except ValueError:
        return None


# не-интернет явно, не полагаясь на is_global разных версий Python: документационные (RFC 5737, 3849),
# общий адрес операторов (CGNAT), тестовые стенды (RFC 2544)
RESERVED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "240.0.0.0/4", "2001:db8::/32"))
IFACE_TTL = 300
_ifaces: tuple[float, list[str]] | None = None


@lru_cache(maxsize=1 << 16)
def _is_global(ip: str) -> bool:
    a = _addr(ip)
    return a is not None and a.is_global and not any(a in n for n in RESERVED_NETS if n.version == a.version)


def _iface_addrs(now: float | None = None) -> list[str]:
    """Адреса интерфейсов сервера (ip -o addr): реже раза в IFACE_TTL не спрашиваем."""
    global _ifaces
    now = time.monotonic() if now is None else now
    if _ifaces is not None and now - _ifaces[0] < IFACE_TTL:
        return _ifaces[1]
    rc, out, _ = system.run(["ip", "-o", "addr", "show"], timeout=5)
    found: list[str] = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            for i, w in enumerate(parts[:-1]):
                if w in ("inet", "inet6"):
                    found.append(parts[i + 1].split("/", 1)[0])
    _ifaces = (now, found)
    return found


def server_nets() -> list[Any]:
    """Адреса самого сервера: SERVER_IP из config.env и адреса его интерфейсов. Обращения с них — свои."""
    raw = list(_iface_addrs())
    try:
        raw += [config.load().get(k) or "" for k in ("SERVER_IP", "SERVER_IPV6")]
    except (config.ConfigError, OSError):
        pass
    nets = []
    for a in dict.fromkeys(x.strip().split("%", 1)[0] for x in raw if x and x.strip()):
        if _is_global(a):   # локальные и так «служебные»: своими их делать незачем
            nets.append(ipaddress.ip_network(a, strict=False))
    return nets


def ignore_nets() -> list[Any]:
    """Свои сети: journal-ignore.txt и адреса самого сервера."""
    return load_ignore() + server_nets()


def scope_of(ip: str, own: set[str], nets: list[Any]) -> str:
    """own — адрес успешного входа по ключу или из journal-ignore.txt; local — не из публичного
    интернета (loopback, частные сети, контейнеры и тесты); public — остальное."""
    if ip in own:
        return "own"
    a = _addr(ip)
    if a is None:
        return "local"
    if nets and any(a in n for n in nets if n.version == a.version):
        return "own"
    return "public" if _is_global(ip) else "local"


# ---------- база ----------

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hits (
    res INTEGER NOT NULL, ts INTEGER NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
    ip TEXT NOT NULL, port INTEGER NOT NULL DEFAULT 0, n INTEGER NOT NULL,
    PRIMARY KEY (res, ts, kind, ip, port)
);
-- покрывающий: суммы по адресам считаются обходом индекса, без прыжков в таблицу (замер: 1100 → 50 мс на 90 сутках)
CREATE INDEX IF NOT EXISTS hits_ip2 ON hits (res, ip, ts, kind, port, n);
DROP INDEX IF EXISTS hits_ip;
CREATE TABLE IF NOT EXISTS ips (
    ip TEXT PRIMARY KEY, first INTEGER NOT NULL, last INTEGER NOT NULL, n INTEGER NOT NULL DEFAULT 0, cc TEXT
);
CREATE TABLE IF NOT EXISTS own (ip TEXT PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    ts INTEGER PRIMARY KEY, ok INTEGER NOT NULL, events INTEGER NOT NULL, lines INTEGER NOT NULL,
    duration REAL NOT NULL, errors TEXT NOT NULL
);
"""


def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection | None:
    path = Path(path) if path else db_path()
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 10000")
    if create:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(_DDL)
        con.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
        con.commit()
    return con


def setup(cfg: Config) -> None:
    """Из `zoo setup` (фаза 09): схема БД."""
    con = connect()
    if con is not None:
        con.close()


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    r = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _meta_set(con: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        con.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


# ---------- чтение источников ----------

def wan_ifaces() -> set[str]:
    """Внешние интерфейсы: маршрут по умолчанию (VPN-интерфейсы awg0 и т. п. сюда не входят)."""
    iface = traffic.default_iface()
    return {iface} if iface else set()


def journalctl(args: list[str]) -> tuple[int, str, str]:
    # -n ограничивает вывод, даже если за трое суток в журнале миллион строк перебора SSH. Замер 05.10.2026:
    # с --after-cursor это первые N записей (systemd 249 и 255), с --since — первые (249) или последние (255)
    return system.run(["journalctl", "--no-pager", "-q", "-o", "json", "--output-fields=MESSAGE",
                       "-n", str(MAX_LINES + 1), *args], timeout=120)


@dataclass
class Source:
    name: str
    match: list[str]
    parse: Callable[[str, int], Event | None]


def sources(wan: set[str]) -> list[Source]:
    return [
        Source("ufw", ["_TRANSPORT=kernel", "-p", "warning"], lambda m, ts: parse_ufw(m, ts, wan)),
        Source("ssh", ["-u", "ssh.service", "-u", "sshd.service"], parse_ssh),
        Source("hysteria", ["SYSLOG_IDENTIFIER=zoo-hy2-auth"], parse_hy2),
        Source("xui", ["-u", "x-ui.service"], parse_xui),
        Source("web", ["-u", "zoo-web.service"], parse_web),
    ]


def _entries(rc: int, out: str) -> list[dict[str, Any]]:
    rows = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            rows.append(e)
    return rows


def read_source(con: sqlite3.Connection, src: Source, now: int) -> tuple[list[Event], int, str, bool]:
    """Новые события источника. (события, прочитано строк, ошибка, упёрлись ли в MAX_LINES).
    Курсор сохраняется здесь же: вызывающий обязан закоммитить транзакцию вместе с событиями."""
    cur = _meta_get(con, f"cursor:{src.name}")
    skip = _meta_get(con, f"since:{src.name}")
    since = f"@{skip}" if skip else f"-{FIRST_RUN_DAYS}d"
    rc, out, err = journalctl([*(["--after-cursor", cur] if cur else ["--since", since]), *src.match])
    if rc != 0 and cur:
        # курсор пропал (журнал ротировался): берём последний час, повтор события не страшен
        rc, out, err = journalctl(["--since", "-1h", *src.match])
    if rc == 127:
        return [], 0, "нет journalctl", False
    if rc != 0:
        return [], 0, (err.strip().splitlines() or [f"код {rc}"])[-1][:200], False
    rows = _entries(rc, out)
    capped = len(rows) > MAX_LINES
    rows = rows[:MAX_LINES]
    events: list[Event] = []
    seen_conn: set[tuple[str, str]] = set()
    last_cursor = None
    last_ts = now
    for e in rows:
        last_cursor = e.get("__CURSOR") or last_cursor
        msg = e.get("MESSAGE")
        if not isinstance(msg, str):
            continue
        try:
            ts = int(e.get("__REALTIME_TIMESTAMP", "0")) // 1_000_000 or now
        except ValueError:
            ts = now
        last_ts = ts
        ev = src.parse(msg, ts)
        if ev is None:
            continue
        if src.name == "ssh" and ev.kind != OWN_LOGIN:
            key = ssh_conn_key(msg)
            if key:
                if key in seen_conn:
                    continue
                seen_conn.add(key)
        events.append(ev)
    if last_cursor:
        # отстали на часы (поток записей > MAX_LINES за запуск) — пропускаем накопленное
        # следующий запуск начнёт с последнего часа: без курсора он взял бы первые сутки и перечитал то же
        lag_skip = capped and now - last_ts > MAX_LAG
        _meta_set(con, f"cursor:{src.name}", None if lag_skip else last_cursor)
        _meta_set(con, f"since:{src.name}", str(now - 3600) if lag_skip else None)
    err_text = ""
    if capped:
        err_text = f"больше {MAX_LINES} строк за запуск" + ("; накопленное пропущено" if now - last_ts > MAX_LAG else "")
    return events, len(rows), err_text, capped


def read_f2b(con: sqlite3.Connection, path: Path | None = None) -> tuple[list[Event], int, str]:
    """Баны из лога fail2ban с прошлого смещения; ротацию (другой inode, файл стал короче) переживаем."""
    path = path or F2B_LOG
    try:
        st = path.stat()
    except OSError:
        return [], 0, ""  # fail2ban не стоит — не ошибка
    ino, off = _meta_get(con, "f2b:ino"), _meta_get(con, "f2b:off")
    if ino != str(st.st_ino) or off is None or int(off) > st.st_size:
        start = max(0, st.st_size - F2B_TAIL) if off is None else 0
    else:
        start = int(off)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(F2B_TAIL)
    except OSError as e:
        return [], 0, f"fail2ban.log: {e.strerror or e}"
    cut = data.rfind(b"\n") + 1  # неполную последнюю строку оставляем на следующий раз
    data = data[:cut]
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and off is None and lines:
        lines = lines[1:]  # первая строка хвоста могла быть обрезана
    _meta_set(con, "f2b:ino", str(st.st_ino))
    _meta_set(con, "f2b:off", str(start + cut))
    return [ev for ev in map(parse_f2b, lines) if ev], len(lines), ""


# ---------- запись ----------

def aggregate(events: list[Event]) -> dict[tuple[int, int, str, str, str, int], int]:
    """События → {(res, ts, source, kind, ip, port): n} по часам и суткам. Портов у одного
    адреса за запуск — не больше PORT_CAP: сканер всех 65535 портов не раздувает базу."""
    out: dict[tuple[int, int, str, str, str, int], int] = {}
    ports: dict[tuple[int, str, str], set[int]] = {}
    for ev in events:
        if ev.kind not in KINDS:
            continue
        hour = traffic.align(ev.ts, RES_1H)
        seen = ports.setdefault((hour, ev.kind, ev.ip), set())
        port = ev.port
        if port and port not in seen:
            if len(seen) >= PORT_CAP:
                port = 0
            else:
                seen.add(port)
        for res in (RES_1H, RES_1D):
            key = (res, traffic.align(ev.ts, res), ev.source, ev.kind, ev.ip, port)
            out[key] = out.get(key, 0) + 1
    return out


def default_port(events: list[Event], cfg: Config | None) -> None:
    """У событий без порта назначения (sshd и помощник Hysteria его не пишут) — порт службы из конфига."""
    if cfg is None:
        return
    ports = {"ssh-auth": cfg.ssh_login_port(), "ssh-scan": cfg.ssh_login_port(), "hy2-auth": cfg.get("HY2_PORT")}
    for ev in events:
        p = ports.get(ev.kind, "")
        if not ev.port and str(p).isdigit():
            ev.port = int(p)


def store(con: sqlite3.Connection, events: list[Event], now: int) -> None:
    for ev in events:
        if ev.kind == OWN_LOGIN:
            con.execute("INSERT INTO own VALUES (?, ?) ON CONFLICT(ip) DO UPDATE SET ts = max(ts, excluded.ts)",
                        (ev.ip, ev.ts))
    attacks = [e for e in events if e.kind != OWN_LOGIN]
    rows = aggregate(attacks)
    con.executemany(
        "INSERT INTO hits (res, ts, source, kind, ip, port, n) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (res, ts, kind, ip, port) DO UPDATE SET n = n + excluded.n",
        [(*k, n) for k, n in rows.items()])
    per_ip: dict[str, list[int]] = {}
    for ev in attacks:
        p = per_ip.setdefault(ev.ip, [ev.ts, ev.ts, 0])
        p[0], p[1], p[2] = min(p[0], ev.ts), max(p[1], ev.ts), p[2] + 1
    con.executemany(
        "INSERT INTO ips (ip, first, last, n) VALUES (?, ?, ?, ?) ON CONFLICT (ip) DO UPDATE SET "
        "first = min(first, excluded.first), last = max(last, excluded.last), n = n + excluded.n",
        [(ip, a, b, n) for ip, (a, b, n) in per_ip.items()])


def enrich(con: sqlite3.Connection) -> str:
    """Страна для адресов, которых ещё не искали. Нет geoip.dat — страны не будет, это не ошибка."""
    todo = [r[0] for r in con.execute("SELECT ip FROM ips WHERE cc IS NULL LIMIT 5000")]
    if not todo:
        return ""
    src = geoip.find_dat()
    idx = geoip.open_index(src, geo_index_file()) if src else None
    if idx is None:
        return "" if src is None else "geoip.dat не разобран"
    con.executemany("UPDATE ips SET cc = ? WHERE ip = ?", [(idx.country(ip), ip) for ip in todo])
    return ""


def prune(con: sqlite3.Connection, now: int) -> None:
    days = keep_days()
    con.execute("DELETE FROM hits WHERE res = ? AND ts < ?", (RES_1H, now - KEEP_HOURLY_DAYS * 86400))
    con.execute("DELETE FROM hits WHERE res = ? AND ts < ?", (RES_1D, now - days * 86400))
    con.execute("DELETE FROM ips WHERE last < ?", (now - days * 86400,))
    con.execute("DELETE FROM own WHERE ts < ?", (now - OWN_KEEP_DAYS * 86400,))
    con.execute("DELETE FROM runs WHERE ts < ?", (now - 7 * 86400,))


def collect(cfg: Config | None = None, now: int | None = None) -> dict[str, Any]:
    """Прочитать новые записи всех источников и сложить в базу. {events, lines, errors, duration}."""
    started = time.monotonic()
    now = int(time.time()) if now is None else now
    with file_lock(paths.state_dir() / "journal.lock", timeout=60):
        con = connect()
        assert con is not None
        errors: dict[str, str] = {}
        events: list[Event] = []
        lines = 0
        try:
            with con:
                wan = wan_ifaces()
                if not wan:
                    errors["ufw"] = "не найден внешний интерфейс (маршрут по умолчанию)"
                for src in sources(wan):
                    if src.name == "ufw" and not wan:
                        continue
                    ev, n, err, _ = read_source(con, src, now)
                    events += ev
                    lines += n
                    if err:
                        errors[src.name] = err
                ev, n, err = read_f2b(con)
                events += ev
                lines += n
                if err:
                    errors["fail2ban"] = err
                default_port(events, cfg)
                store(con, events, now)
                err = enrich(con)
                if err:
                    errors["geoip"] = err
                prune(con, now)
                dur = round(time.monotonic() - started, 2)
                con.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                            (now, int(not errors), len([e for e in events if e.kind != OWN_LOGIN]), lines, dur,
                             json.dumps(errors, ensure_ascii=False)))
        finally:
            con.close()
    return {"ts": now, "events": len([e for e in events if e.kind != OWN_LOGIN]), "lines": lines,
            "errors": errors, "duration": dur}


# ---------- отчёты ----------

def _con() -> sqlite3.Connection | None:
    try:
        return connect(create=False)
    except sqlite3.Error:
        return None


def resolve_period(period: str) -> str:
    p = traffic.resolve_period(period)
    if p not in PERIODS:
        raise ValueError(f"период: одно из {', '.join(PERIODS)}")
    return p


def _scope_split(rows: Any, own: set[str], include_local: bool) -> tuple[set[str], dict[str, int]]:
    """По строкам (адрес, событий): адреса, которые скрываем (свои и локальные), и сколько событий они дали."""
    nets = ignore_nets()
    hidden: set[str] = set()
    counts = {"own": 0, "local": 0}
    for ip, n in rows:
        sc = scope_of(ip, own, nets)
        if sc != "public":
            counts[sc] += int(n)
            if not include_local:
                hidden.add(ip)
    return hidden, counts


def _hidden_ips(con: sqlite3.Connection, since: int, res: int, include_local: bool) -> tuple[set[str], dict[str, int]]:
    """Адреса, которые скрываем (свои и локальные), и сколько событий они дали."""
    own = {r[0] for r in con.execute("SELECT ip FROM own")}
    return _scope_split(con.execute("SELECT ip, SUM(n) FROM hits WHERE res = ? AND ts >= ? AND kind NOT IN "
                                    "(%s) GROUP BY ip" % ",".join("?" * len(DEFENCE_KINDS)),
                                    (res, since, *DEFENCE_KINDS)), own, include_local)


def report(period: str = "24h", now: float | None = None, include_local: bool = False,
           top: int = 15) -> dict[str, Any]:
    """Сводка за период. include_local — показать и свои/локальные адреса (контейнеры, тесты).

    Суммы считает SQLite (десятки тысяч адресов за 90 суток в Python-цикле — сотни миллисекунд),
    подробности (виды, порты, страна) берутся только для адресов из верхушки списка."""
    period = resolve_period(period)
    since, step, n, res = traffic.window(period, now)
    out: dict[str, Any] = {
        "period": period, "title": traffic.PERIOD_TITLES[period], "since": since, "step": step,
        "include_local": include_local, "db": str(db_path()), "blind": [],
        "totals": {"events": 0, "ips": 0, "bans": 0, "by_kind": {}}, "groups": [], "top_ips": [], "top_ports": [],
        "countries": [], "timeline": {"buckets": [since + i * step for i in range(n)], "step": step, "series": []},
        "hidden": {"own": 0, "local": 0}, "own_ips": [], "last_run": None, "external": {},
    }
    tracked = reality_tracked()
    out["reality_tracked"] = tracked
    out["blind"] = [{"what": a, "why": b} for a, b in BLIND if not (tracked and a == "REALITY")]
    con = _con()
    if con is None:
        out["empty"] = True
        return out
    kinds = tuple(KINDS)
    attack = tuple(k for k in KINDS if k not in DEFENCE_KINDS)
    marks, amarks = ",".join("?" * len(kinds)), ",".join("?" * len(attack))
    try:
        own = {r["ip"]: r["ts"] for r in con.execute("SELECT ip, ts FROM own ORDER BY ts DESC")}
        out["last_run"] = _last_run(con)
        # события периода — во временную таблицу один раз, суммы — SQL-запросами к ней
        con.execute("PRAGMA temp_store = MEMORY")
        con.execute(f"CREATE TEMP TABLE _v AS SELECT ts, kind, ip, port, n FROM hits WHERE res = ? AND ts + 0 >= ? "
                    f"AND kind IN ({marks})", (res, since, *kinds))
        con.execute(f"CREATE TEMP TABLE _ip AS SELECT ip, SUM(n) AS n FROM _v WHERE kind IN ({amarks}) GROUP BY ip",
                    attack)
        ip_rows = con.execute("SELECT ip, n FROM _ip").fetchall()
        hidden, out["hidden"] = _scope_split(ip_rows, set(own), include_local)
        nonpublic = _scope_split(ip_rows, set(own), False)[0] if include_local else hidden
        if hidden:
            con.execute("CREATE TEMP TABLE _hide (ip TEXT PRIMARY KEY) WITHOUT ROWID")
            con.executemany("INSERT INTO _hide VALUES (?)", ((ip,) for ip in hidden))
            con.execute("DELETE FROM _v WHERE ip IN (SELECT ip FROM _hide)")
            con.execute("DELETE FROM _ip WHERE ip IN (SELECT ip FROM _hide)")
        # по видам и времени: итоги, группы, график, баны
        by_kind: dict[str, int] = {}
        by_group: dict[str, dict[str, Any]] = {g: {"n": 0, "ips": 0, "kinds": {}} for g in GROUPS}
        series: dict[str, list[int]] = {g: [0] * n for g in GROUPS}
        for ts, kind, cnt in con.execute("SELECT ts, kind, SUM(n) FROM _v GROUP BY ts, kind"):
            by_kind[kind] = by_kind.get(kind, 0) + int(cnt)
            if kind in DEFENCE_KINDS:
                continue
            group = KINDS[kind][2]
            g = by_group[group]
            g["n"] += int(cnt)
            g["kinds"][kind] = g["kinds"].get(kind, 0) + int(cnt)
            i = (ts - since) // step
            if 0 <= i < n:
                series[group][i] += int(cnt)
        out["totals"]["bans"] = sum(by_kind.get(k, 0) for k in DEFENCE_KINDS)
        if by_kind.get("reality-probe") and not tracked:  # записи есть — значит, строку кто-то включил
            out["reality_tracked"] = True
            out["blind"] = [b for b in out["blind"] if b["what"] != "REALITY"]
        case = "CASE kind " + " ".join(f"WHEN '{k}' THEN '{KINDS[k][2]}'" for k in attack) + " END"
        for grp, cnt in con.execute(f"SELECT {case}, COUNT(DISTINCT ip) FROM _v WHERE kind IN ({amarks}) GROUP BY 1",
                                    attack):
            by_group[grp]["ips"] = int(cnt)
        # «внешние» счёты для вывода на странице: свои и служебные адреса в них не входят, даже когда показаны
        external = {g: {"n": by_group[g]["n"], "ips": by_group[g]["ips"]} for g in GROUPS}
        if include_local and nonpublic:
            external = {g: {"n": 0, "ips": 0} for g in GROUPS}
            con.execute("CREATE TEMP TABLE _np (ip TEXT PRIMARY KEY) WITHOUT ROWID")
            con.executemany("INSERT INTO _np VALUES (?)", ((ip,) for ip in nonpublic))
            for grp, cnt, ips in con.execute(f"SELECT {case}, SUM(n), COUNT(DISTINCT ip) FROM _v WHERE kind IN "
                                             f"({amarks}) AND ip NOT IN (SELECT ip FROM _np) GROUP BY 1", attack):
                external[grp] = {"n": int(cnt), "ips": int(ips)}
        out["external"] = external
        # верхушка: адреса, порты, страны
        tops = con.execute("SELECT ip, n FROM _ip ORDER BY n DESC, ip LIMIT ?", (top,)).fetchall()
        top_ports = con.execute(f"SELECT port, SUM(n) AS n, COUNT(DISTINCT ip) AS ips FROM _v WHERE kind IN ({amarks}) "
                                "AND port != 0 GROUP BY port ORDER BY n DESC, port LIMIT ?", (*attack, top)).fetchall()
        countries = con.execute("SELECT COALESCE(NULLIF(i.cc, ''), '?') AS c, SUM(v.n) AS n, COUNT(*) AS ips "
                                "FROM _ip v LEFT JOIN ips i ON i.ip = v.ip GROUP BY c ORDER BY n DESC, c").fetchall()
        total_ips = sum(int(r["ips"]) for r in countries)
        detail: dict[str, dict[str, Any]] = {}
        for r in tops:
            d: dict[str, Any] = {"ip": r["ip"], "n": int(r["n"]), "kinds": {}, "ports": {}, "bans": 0}
            for kind, port, cnt in con.execute("SELECT kind, port, SUM(n) FROM hits WHERE res = ? AND ts >= ? AND ip = ? "
                                               f"AND kind IN ({marks}) GROUP BY kind, port",
                                               (res, since, r["ip"], *kinds)):
                if kind in DEFENCE_KINDS:
                    d["bans"] += int(cnt)
                    continue
                d["kinds"][kind] = d["kinds"].get(kind, 0) + int(cnt)
                if port:
                    d["ports"][port] = d["ports"].get(port, 0) + int(cnt)
            row = con.execute("SELECT cc, first, last FROM ips WHERE ip = ?", (r["ip"],)).fetchone()
            d["cc"], d["first"], d["last"] = (row["cc"] or "", row["first"], row["last"]) if row else ("", None, None)
            detail[r["ip"]] = d
    finally:
        con.close()
    nets = ignore_nets()
    own_set = set(own)
    out["own_ips"] = [{"ip": ip, "ts": ts} for ip, ts in own.items()]
    out["totals"]["events"] = sum(g["n"] for g in by_group.values())
    out["totals"]["ips"] = int(total_ips)
    out["totals"]["by_kind"] = by_kind
    out["groups"] = [{"key": k, "title": GROUPS[k], "n": g["n"], "ips": g["ips"],
                      "kinds": [{"kind": kd, "title": KINDS[kd][1], "n": cn}
                                for kd, cn in sorted(g["kinds"].items(), key=lambda kv: -kv[1])]}
                     for k, g in by_group.items()]
    out["timeline"]["series"] = [{"key": k, "title": GROUPS[k], "values": v} for k, v in series.items() if any(v)]
    for r in tops:
        d = detail[r["ip"]]
        d["scope"] = scope_of(d["ip"], own_set, nets)
        d["ports"] = [p for p, _ in sorted(d["ports"].items(), key=lambda kv: -kv[1])[:6]]
        out["top_ips"].append(d)
    out["top_ports"] = [{"port": r["port"], "n": int(r["n"]), "ips": int(r["ips"])} for r in top_ports]
    out["countries"] = [{"cc": r["c"], "n": int(r["n"]), "ips": int(r["ips"])} for r in countries[:top]]
    return out



def reality_tracked() -> bool:
    """Видит ли журнал чужие клиенты REALITY: Xray пишет их только на уровне info/debug (D32), по умолчанию
    уровень выше — владелец поднимает его сам в шаблоне Xray 3x-ui."""
    cfg = Path(os.environ.get("XUI_DIR", "/usr/local/x-ui")) / "bin" / "config.json"
    try:
        level = json.loads(cfg.read_text(encoding="utf-8")).get("log", {}).get("loglevel", "")
    except (OSError, ValueError, AttributeError):
        return False
    return str(level).lower() in ("info", "debug")


# ---------- поиск и карточка адреса ----------

ROWS = (15, 50, 100)
SORTS = ("n", "last")           # по числу попыток / по давности (ключ keyset — (значение, адрес))
SERVICES = tuple(dict.fromkeys(v[0] for v in KINDS.values()))
SERVICE_TITLES = {"ufw": "ufw", "ssh": "SSH", "fail2ban": "fail2ban", "hysteria": "Hysteria2", "xray": "REALITY",
                  "3x-ui": "3x-ui", "zoo-web": "админка"}
SERVICE_ALIASES = {"sshd": "ssh", "f2b": "fail2ban", "hy2": "hysteria", "hysteria2": "hysteria", "reality": "xray",
                   "xui": "3x-ui", "x-ui": "3x-ui", "web": "zoo-web", "zoo": "zoo-web", "firewall": "ufw"}
QUERY_MAX = 200
TOKENS_MAX = 10
SEARCH_BATCH = 200              # строк за обращение к базе, пока отсеиваем свои и локальные адреса
SEARCH_BATCHES = 20             # и не больше стольких обращений на один запрос
_PREFIX = re.compile(r"^[0-9a-f:.]{1,45}$")


@dataclass
class Query:
    """Разобранная строка поиска. Внутри одного вида условий — «или», между видами — «и»."""
    exact: list[str] = field(default_factory=list)
    prefixes: list[str] = field(default_factory=list)
    ccs: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    bad: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.exact or self.prefixes or self.ccs or self.ports or self.services or self.kinds)


def parse_query(text: str) -> Query:
    """IP или префикс (`185.220.`), страна (`cc:NL`), порт (`:22`, `port:22`), сервис (`ssh`, `hy2`) или вид
    события (`ssh-auth`). Непонятное — в bad, а не в SQL: условия строятся только из разобранных значений."""
    q = Query()
    for tok in text[:QUERY_MAX].lower().split()[:TOKENS_MAX]:
        m = re.fullmatch(r"(?:cc|country):([a-z]{2})", tok)
        if m:
            q.ccs.append(m.group(1).upper())
            continue
        m = re.fullmatch(r"(?:port)?:(\d{1,5})", tok)
        if m:
            if 0 < int(m.group(1)) <= 65535:
                q.ports.append(int(m.group(1)))
            else:
                q.bad.append(tok)
            continue
        svc = SERVICE_ALIASES.get(tok, tok)
        addr = norm_ip(tok)
        if svc in SERVICES:
            q.services.append(svc)
        elif tok in KINDS:
            q.kinds.append(tok)
        elif addr:
            q.exact.append(addr)
        elif _PREFIX.match(tok) and ("." in tok or ":" in tok or tok.isdigit()):
            q.prefixes.append(tok)
        else:
            q.bad.append(tok[:40])
    return q


def _prefix_range(prefix: str) -> tuple[str, str]:
    """[prefix, верхняя граница): диапазон по индексу, а не LIKE (он без индекса и нечувствителен к регистру)."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _clauses(q: Query, svc: str, kind: str) -> tuple[list[str], list[Any]]:
    """Условия WHERE по таблице hits (алиас h). Страна — отдельно, по адресу после группировки. Значения — только параметрами."""
    where: list[str] = []
    args: list[Any] = []

    def one_of(col: str, vals: list[Any]) -> None:
        where.append(f"{col} IN ({','.join('?' * len(vals))})")
        args.extend(vals)

    ip_or: list[str] = []
    for ip in q.exact:
        ip_or.append("h.ip = ?")
        args.append(ip)
    for pf in q.prefixes:
        ip_or.append("(h.ip >= ? AND h.ip < ?)")
        args.extend(_prefix_range(pf))
    if ip_or:
        where.append("(" + " OR ".join(ip_or) + ")")
    if q.ports:
        one_of("h.port", q.ports)
    if q.services:  # вид однозначно задаёт источник: так читается только покрывающий индекс
        one_of("h.kind", [k for k, v in KINDS.items() if v[0] in q.services])
    if q.kinds:
        one_of("h.kind", q.kinds)
    if svc in SERVICES:
        one_of("h.kind", [k for k, v in KINDS.items() if v[0] == svc])
    if kind in KINDS:
        where.append("h.kind = ?")
        args.append(kind)
    return where, args


def parse_cursor(text: str) -> tuple[int, str] | None:
    """«ключ:адрес» (адрес может содержать «:», ключ — число); мусор — как «с начала»."""
    key, _, ip = text.partition(":")
    ip = norm_ip(ip) or ""
    return (int(key), ip) if key.isdigit() and ip else None


def search(period: str = "24h", q: str = "", svc: str = "", kind: str = "", sort: str = "n", after: str = "",
           limit: int = ROWS[0], include_local: bool = False, now: float | None = None) -> dict[str, Any]:
    """Адреса периода по фильтрам, keyset-страницами: ORDER BY (ключ, адрес) и условие «после курсора» вместо
    OFFSET — стоимость страницы не растёт с глубиной. Свои и локальные адреса отсеиваются после запроса
    (их единицы), поэтому читаем пачками, пока не наберём страницу. {rows, next, query, empty}."""
    period = resolve_period(period)
    sort = sort if sort in SORTS else "n"
    limit = limit if limit in ROWS else ROWS[0]
    query = parse_query(q)
    since, _, _, res = traffic.window(period, now)
    out: dict[str, Any] = {"period": period, "query": query, "rows": [], "next": None, "empty": False, "sort": sort}
    con = _con()
    if con is None:
        out["empty"] = True
        return out
    dm = ",".join("?" * len(DEFENCE_KINDS))
    want_def = bool(DEFENCE_KINDS & ({kind} | set(query.kinds)) or "fail2ban" in (svc, *query.services))
    where, wargs = _clauses(query, svc, kind)
    # «+ 0»: иначе при фильтрах планировщик берёт индекс по ts с прыжками в таблицу (замер: 500 против 120 мс)
    where = ["h.res = ?", "h.ts + 0 >= ?", f"h.kind IN ({','.join('?' * len(KINDS))})", *where]
    # att — попытки (без банов fail2ban), k — когда адрес замечен в последний раз; страну и «замечен» берём у уже
    # сгруппированных адресов, а не у каждой строки hits
    inner = (f"SELECT h.ip AS ip, SUM(CASE WHEN h.kind IN ({dm}) THEN 0 ELSE h.n END) AS att, "
             f"SUM(CASE WHEN h.kind IN ({dm}) THEN h.n ELSE 0 END) AS bans, MAX(h.ts) AS mts "
             f"FROM hits h WHERE {' AND '.join(where)} GROUP BY h.ip "
             f"HAVING {'(att > 0 OR bans > 0)' if want_def else 'att > 0'}")
    joined = sort == "last" or bool(query.ccs)  # к ips присоединяем все адреса, только если по ним сортируем или ищем
    outer = (f"SELECT g.ip AS ip, g.att AS att, g.bans AS bans, {'COALESCE(i.last, g.mts)' if joined else 'g.mts'} AS k "
             f"FROM ({inner}) g" + (" LEFT JOIN ips i ON i.ip = g.ip" if joined else ""))
    base = [*sorted(DEFENCE_KINDS), *sorted(DEFENCE_KINDS), res, since, *KINDS, *wargs]
    key = "att" if sort == "n" else "k"
    conds = []
    if query.ccs:
        conds.append(f"i.cc IN ({','.join('?' * len(query.ccs))})")
        base += query.ccs
    if sort == "n":
        order, cmp_ = " ORDER BY g.att DESC, g.ip ASC", "(g.att < ? OR (g.att = ? AND g.ip > ?))"
    else:
        order, cmp_ = " ORDER BY k DESC, g.ip DESC", "(COALESCE(i.last, g.mts) < ? OR (COALESCE(i.last, g.mts) = ? AND g.ip < ?))"
    pos = parse_cursor(after)
    try:
        own = {r["ip"] for r in con.execute("SELECT ip FROM own")}
        nets = ignore_nets()
        picked: list[Any] = []
        capped = False
        for i in range(SEARCH_BATCHES):
            use = [*conds, *([cmp_] if pos else [])]
            stmt = outer + (" WHERE " + " AND ".join(use) if use else "") + order + " LIMIT ?"
            args = [*base, *([pos[0], pos[0], pos[1]] if pos else []), SEARCH_BATCH]
            batch = con.execute(stmt, args).fetchall()
            for r in batch:
                pos = (int(r[key]), r["ip"])
                if include_local or scope_of(r["ip"], own, nets) == "public":
                    picked.append(r)
                    if len(picked) > limit:
                        break
            if len(picked) > limit or len(batch) < SEARCH_BATCH:
                break
            capped = i == SEARCH_BATCHES - 1
        page = picked[:limit]
        if len(picked) > limit:
            out["next"] = f"{int(page[-1][key])}:{page[-1]['ip']}"
        elif capped and pos:
            out["next"] = f"{pos[0]}:{pos[1]}"
        out["rows"] = _row_details(con, page, res, since, own, nets)
    finally:
        con.close()
    return out


def _row_details(con: sqlite3.Connection, page: list[Any], res: int, since: int, own: set[str],
                 nets: list[Any]) -> list[dict[str, Any]]:
    """Виды и порты адресов страницы — одним запросом по индексу hits_ip."""
    rows: dict[str, dict[str, Any]] = {}
    for r in page:
        rows[r["ip"]] = {"ip": r["ip"], "n": int(r["att"]), "bans": int(r["bans"]), "cc": "", "first": None,
                         "last": None, "kinds": {}, "ports": {}, "scope": scope_of(r["ip"], own, nets)}
    if not rows:
        return []
    ips = list(rows)
    for r in con.execute(f"SELECT ip, cc, first, last FROM ips WHERE ip IN ({','.join('?' * len(ips))})", ips):
        rows[r["ip"]].update(cc=r["cc"] or "", first=r["first"], last=r["last"])
    for ip, kind, port, cnt in con.execute(
            f"SELECT ip, kind, port, SUM(n) FROM hits WHERE res = ? AND ts >= ? AND ip IN ({','.join('?' * len(ips))}) "
            f"AND kind IN ({','.join('?' * len(KINDS))}) GROUP BY ip, kind, port", (res, since, *ips, *KINDS)):
        d = rows[ip]
        if kind in DEFENCE_KINDS:
            continue
        d["kinds"][kind] = d["kinds"].get(kind, 0) + int(cnt)
        if port:
            d["ports"][port] = d["ports"].get(port, 0) + int(cnt)
    for d in rows.values():
        d["ports"] = [p for p, _ in sorted(d["ports"].items(), key=lambda kv: -kv[1])[:6]]
    return list(rows.values())


def parse_feed_cursor(text: str) -> tuple[int, str, int] | None:
    parts = text.split(":")
    if len(parts) == 3 and parts[0].isdigit() and parts[1] in KINDS and parts[2].isdigit():
        return int(parts[0]), parts[1], int(parts[2])
    return None


def ip_card(ip: str, period: str = "24h", after: str = "", limit: int = ROWS[1],
            now: float | None = None) -> dict[str, Any] | None:
    """Всё об одном адресе: первый/последний раз, страна, разбивка по сервисам, видам и портам, лента событий
    (keyset по (время, вид, порт), индекс hits_ip). None — адрес не записан или не адрес."""
    ip = norm_ip(ip) or ""
    period = resolve_period(period)
    limit = limit if limit in ROWS else ROWS[1]
    since, step, _, res = traffic.window(period, now)
    con = _con() if ip else None
    if con is None:
        return None
    marks = ",".join("?" * len(KINDS))
    try:
        row = con.execute("SELECT first, last, n, cc FROM ips WHERE ip = ?", (ip,)).fetchone()
        if row is None:
            return None
        own = {r["ip"] for r in con.execute("SELECT ip FROM own")}
        by_kind: dict[str, int] = {}
        by_port: dict[int, int] = {}
        for kind, port, cnt in con.execute("SELECT kind, port, SUM(n) FROM hits WHERE res = ? AND ip = ? AND ts >= ? "
                                           f"AND kind IN ({marks}) GROUP BY kind, port", (res, ip, since, *KINDS)):
            by_kind[kind] = by_kind.get(kind, 0) + int(cnt)
            if port and kind not in DEFENCE_KINDS:
                by_port[port] = by_port.get(port, 0) + int(cnt)
        cur = parse_feed_cursor(after)
        cond, args = "", [res, ip, since]
        if cur:
            cond = " AND (ts < ? OR (ts = ? AND (kind < ? OR (kind = ? AND port < ?))))"
            args += [cur[0], cur[0], cur[1], cur[1], cur[2]]
        feed = con.execute(f"SELECT ts, kind, port, n FROM hits WHERE res = ? AND ip = ? AND ts >= ?{cond} "
                           f"AND kind IN ({marks}) ORDER BY ts DESC, kind DESC, port DESC LIMIT ?",
                           (*args, *KINDS, limit + 1)).fetchall()
    finally:
        con.close()
    more = len(feed) > limit
    feed = feed[:limit]
    by_service: dict[str, int] = {}
    for kind, cnt in by_kind.items():
        if kind not in DEFENCE_KINDS:
            by_service[KINDS[kind][0]] = by_service.get(KINDS[kind][0], 0) + cnt
    return {
        "ip": ip, "period": period, "res": res, "step": step, "first": row["first"], "last": row["last"],
        "total": int(row["n"]), "cc": row["cc"] or "", "scope": scope_of(ip, own, ignore_nets()),
        "n": sum(by_service.values()), "bans": sum(by_kind.get(k, 0) for k in DEFENCE_KINDS),
        "by_service": sorted(by_service.items(), key=lambda kv: -kv[1]),
        "by_kind": sorted(((k, c) for k, c in by_kind.items() if k not in DEFENCE_KINDS), key=lambda kv: -kv[1]),
        "by_port": sorted(by_port.items(), key=lambda kv: -kv[1])[:20],
        "feed": [{"ts": r["ts"], "kind": r["kind"], "port": r["port"], "n": int(r["n"])} for r in feed],
        "next": f"{feed[-1]['ts']}:{feed[-1]['kind']}:{feed[-1]['port']}" if more else None,
    }


def _last_run(con: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        r = con.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if r is None:
        return None
    d = dict(r)
    d["errors"] = json.loads(d["errors"] or "{}")
    d["age"] = int(time.time()) - d["ts"]
    return d


def last_run_ts() -> int | None:
    """Время последнего разбора журнала: по нему страница знает, что данные не менялись."""
    con = _con()
    if con is None:
        return None
    try:
        r = _last_run(con)
        return r["ts"] if r else None
    finally:
        con.close()


def alerts(now: float | None = None) -> list[tuple[str, str]]:
    """Тревоги для обзора: всплеск внешних попыток за последний час против обычного за неделю."""
    now = time.time() if now is None else now
    con = _con()
    if con is None:
        return []
    try:
        hour = traffic.align(now, RES_1H)
        since = hour - 7 * 86400
        hidden, _ = _hidden_ips(con, since, RES_1H, False)
        rows = con.execute("SELECT ts, ip, SUM(n) AS n FROM hits WHERE res = ? AND ts >= ? AND kind NOT IN (%s) "
                           "GROUP BY ts, ip" % ",".join("?" * len(DEFENCE_KINDS)),
                           (RES_1H, since, *sorted(DEFENCE_KINDS))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    cur = sum(int(r["n"]) for r in rows if r["ts"] >= hour and r["ip"] not in hidden)
    old = [r for r in rows if r["ts"] < hour and r["ip"] not in hidden]
    before = sum(int(r["n"]) for r in old)
    # делим на часы, за которые журнал есть: первый разбор берёт 3 суток, а не неделю
    hours = (hour - min((r["ts"] for r in old), default=hour)) // 3600
    base = before / max(24, hours)
    if cur >= SPIKE_MIN and cur >= SPIKE_FACTOR * max(1.0, base):
        return [("warn", f"Журнал атак: за этот час {cur} попыток (обычно около {base:.0f} в час) — "
                         "подробности на странице «Атаки» или в zoo journal")]
    return []


def health() -> list[str]:
    """Что мешает видеть атаки (для страницы и CLI): выключенный лог ufw и т. п."""
    out = []
    rc, text, _ = system.run(["ufw", "status", "verbose"], timeout=10)
    if rc == 0 and re.search(r"^Logging:\s*off", text, re.M):
        out.append("ufw logging выключен: блокировки закрытых портов не пишутся "
                   "(sudo ufw logging low или install.sh --phase 01)")
    rc, _, _ = system.run(["journalctl", "--version"], timeout=5)
    if rc == 127:
        out.append("нет journalctl: журнал атак пуст")
    return out


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--period", default="24h", help="период: " + ", ".join(PERIODS) + " (по умолчанию 24h)")
    p.add_argument("--all", action="store_true", help="и свои и служебные адреса (контейнеры, тесты, ваш SSH, сам сервер)")
    p.add_argument("--top", type=int, default=10, help="сколько адресов и портов показать (10)")
    p.add_argument("--collect", action="store_true", help="разобрать новые записи журнала (запускает zoo-collector)")


def _fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "—"


def _ago(ts: int | None) -> str:
    if not ts:
        return "—"
    d = time.time() - ts
    return "только что" if d < 90 else output.human_duration(d) + " назад"


def _render(d: dict[str, Any]) -> None:
    if d.get("empty"):
        output.warn(f"журнала атак нет ({d['db']}): коллектор ещё не запускался "
                    "(systemctl status zoo-collector.timer; вручную: zoo journal --collect)")
        return
    t = d["totals"]
    print(f"Кто нас щупал {d['title']} (с {_fmt_ts(d['since'])})")
    hid = d["hidden"]
    hint = []
    if hid["local"] and not d["include_local"]:
        hint.append(f"служебных {hid['local']}")
    if hid["own"] and not d["include_local"]:
        hint.append(f"со своих {hid['own']}")
    print(f"Попыток: {t['events']} с {t['ips']} адресов; банов fail2ban: {t['bans']}"
          + (f"  (скрыто: {', '.join(hint)}; все — zoo journal --all)" if hint else ""))
    print()
    print(output.table([[g["title"], g["n"], g["ips"], "; ".join(f"{k['title']} {k['n']}" for k in g["kinds"])]
                        for g in d["groups"]], ["что делали", "попыток", "адресов", "из чего"], right=(1, 2)))
    if d["top_ips"]:
        print("\nКто чаще всего")
        print(output.table([[i["ip"], i["cc"] or "—", SCOPE_TITLES.get(i["scope"], ""), i["n"],
                             ", ".join(map(str, i["ports"])) or "—",
                             ", ".join(KINDS[k][1] for k in i["kinds"]), _ago(i["last"])] for i in d["top_ips"]],
                           ["адрес", "страна", "метка", "попыток", "порты", "что", "последний раз"], right=(3,)))
    if d["top_ports"]:
        print("\nПорты: " + ", ".join(f"{p['port']} ({p['n']})" for p in d["top_ports"]))
    if d["countries"] and any(c["cc"] != "?" for c in d["countries"]):
        print("Страны: " + ", ".join(f"{c['cc'] if c['cc'] != '?' else 'не определена'} {c['n']}"
                                    for c in d["countries"]))
    run = d["last_run"]
    if run is None:
        output.warn("коллектор журнала ещё не отрабатывал")
    else:
        for src, err in run["errors"].items():
            output.warn(f"источник {src}: {err}")
        if run["age"] > traffic.STALE_AFTER:
            output.warn(f"последний разбор {output.human_duration(run['age'])} назад — проверь zoo-collector.timer")
    for h in d.get("health", []):
        output.warn(h)
    print()
    print(output.color("Не видим: " + "; ".join(b["what"] for b in d["blind"]) + " (подробности — админка, «Атаки»).",
                       "dim"))


def cmd_journal(args: argparse.Namespace, cfg: Config) -> int:
    if args.collect:
        try:
            res = collect(cfg)
        except LockTimeout as e:
            output.error(str(e))
            return 1
        if args.json:
            output.print_json(res)
        else:
            errs = "; ".join(f"{k}: {v}" for k, v in res["errors"].items())
            print(f"разобрано строк: {res['lines']}, событий: {res['events']} за {res['duration']} с"
                  + (f"; ошибки: {errs}" if errs else ""))
        return 0
    try:
        data = report(args.period, include_local=args.all, top=max(1, args.top))
    except ValueError as e:
        output.error(str(e))
        return 2
    data["health"] = health()
    output.emit(data, args.json, _render)
    return 0
