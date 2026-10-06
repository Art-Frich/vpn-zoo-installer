"""Трафик по пользователям и протоколам (ARCHITECTURE §6): коллектор и отчёты.

Коллектор (`zoo traffic --collect`, его запускает zoo-collector.timer раз в 5 минут) снимает
накопительные счётчики, считает приращения с прошлого снятия и раскладывает их по корзинам
5 мин / 1 ч / 1 сут в SQLite (db_path()). Отчёты (CLI и веб) читают только корзины.

Источники счётчиков (серия = протокол + пользователь):
    3x-ui API       clients/list → серия «xray»: счётчик клиента общий для всех его Xray-inbound
                    (VLESS, XHTTP, SS, TUIC — по протоколам его не разделить);
                    inbounds/list → итог каждого Xray-протокола (пользователь «»)
    Hysteria        trafficStats API 127.0.0.1:HY2_STATS_PORT (и HY2_OBFS_STATS_PORT),
                    секрет HY2_STATS_SECRET; запасной путь — proto_hysteria2_traffic
    остальные       proto_<id>_traffic (AmneziaWG: `awg show dump`)
    host            байты интерфейса маршрута по умолчанию (принято/отправлено сервером)

Служебный пользователь пробника (users.PROBE_USER) собирается как все, но в отчётах по
пользователям его нет (include_hidden=True — показать); в итогах протоколов он учтён.

Сброс счётчика (рестарт сервиса, сброс в панели, новый peer) распознаётся по уменьшению
значения или смене «эпохи» источника (PID сервиса, ifindex, boot_id): приращением тогда
считается текущее значение. Первое снятие нового протокола только запоминает базу.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import manifests, output, paths, protolib, system, users
from .config import Config
from .fsutil import LockTimeout, file_lock
from .xui import XuiClient, XuiError

DB_NAME = "traffic.sqlite"
SCHEMA = 1

RES_5M, RES_1H, RES_1D = 300, 3600, 86400
RESOLUTIONS = (RES_5M, RES_1H, RES_1D)
RETENTION = {RES_5M: 3 * 86400, RES_1H: 90 * 86400, RES_1D: 1100 * 86400}

# период → (шаг графика, число шагов, разрешение хранения, из которого он собирается)
PERIODS: dict[str, tuple[int, int, int]] = {
    "1h": (RES_5M, 12, RES_5M),
    "24h": (RES_1H, 24, RES_1H),
    "7d": (6 * RES_1H, 28, RES_1H),
    "30d": (RES_1D, 30, RES_1D),
    "90d": (RES_1D, 90, RES_1D),
}
PERIOD_ALIASES = {"hour": "1h", "day": "24h", "week": "7d", "month": "30d"}
PERIOD_TITLES = {"1h": "за час", "24h": "за сутки", "7d": "за неделю", "30d": "за 30 дней",
                 "90d": "за 90 дней"}

XRAY = "xray"     # общий счётчик клиента 3x-ui
HOST = "host"     # интерфейс сервера
SPECIAL_TITLES = {XRAY: "Xray (общий счётчик)", HOST: "сервер"}

STALE_AFTER = 20 * 60  # коллектор молчит дольше — предупреждение в status/web


def db_path() -> Path:
    return paths.state_dir() / DB_NAME


def resolve_period(period: str) -> str:
    p = PERIOD_ALIASES.get(period, period)
    if p not in PERIODS:
        raise ValueError(f"период: одно из {', '.join(PERIODS)}")
    return p


# ---------- время и корзины ----------

def _utcoffset(ts: float) -> int:
    return time.localtime(ts).tm_gmtoff or 0


def align(ts: float, step: int) -> int:
    """Начало корзины step, содержащей ts. Шаги от часа и больше — по местному времени
    (сутки начинаются в местную полночь)."""
    ts = int(ts)
    off = _utcoffset(ts) if step >= RES_1H else 0
    return (ts + off) // step * step - off


def window(period: str, now: float | None = None) -> tuple[int, int, int, int]:
    """(начало, шаг, число шагов, разрешение хранения) окна периода, последний шаг — текущий."""
    step, n, res = PERIODS[resolve_period(period)]
    now = time.time() if now is None else now
    return align(now, step) - (n - 1) * step, step, n, res


# ---------- база ----------

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (
    proto TEXT NOT NULL, user TEXT NOT NULL,
    up INTEGER NOT NULL, down INTEGER NOT NULL,
    epoch TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL, seen INTEGER,
    PRIMARY KEY (proto, user)
);
CREATE TABLE IF NOT EXISTS traffic (
    res INTEGER NOT NULL, ts INTEGER NOT NULL, proto TEXT NOT NULL, user TEXT NOT NULL,
    up INTEGER NOT NULL DEFAULT 0, down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (res, ts, proto, user)
);
CREATE INDEX IF NOT EXISTS traffic_user ON traffic (res, user, ts);
CREATE TABLE IF NOT EXISTS runs (
    ts INTEGER PRIMARY KEY, ok INTEGER NOT NULL, series INTEGER NOT NULL,
    up INTEGER NOT NULL, down INTEGER NOT NULL, duration REAL NOT NULL, errors TEXT NOT NULL
);
"""


def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection | None:
    """Соединение с БД; нет файла и create=False → None."""
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
    """Вызывается из `zoo setup` (фаза 09): каталог данных и схема БД."""
    con = connect()
    if con is not None:
        con.close()


# ---------- снятие счётчиков ----------

@dataclass
class Sample:
    """Накопительный счётчик серии. user="" — итог протокола."""
    proto: str
    user: str
    up: int
    down: int
    epoch: str = ""
    seen: int | None = None


@dataclass
class Counter:
    up: int
    down: int
    epoch: str = ""
    ts: int = 0
    seen: int | None = None


@dataclass
class Gathered:
    samples: list[Sample] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # протоколы, итог которых считается как сумма приращений пользователей
    sum_protos: set[str] = field(default_factory=set)
    # протоколы, опрошенные без ошибки (даже если серий пока нет)
    polled: set[str] = field(default_factory=set)


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def xui_inbound_id(m: manifests.Manifest, inbounds: list[dict[str, Any]]) -> int | None:
    """id inbound 3x-ui протокола: поле манифеста (у модулей оно называется по-разному),
    иначе — inbound на порту манифеста."""
    raw = m.raw
    for v in (raw.get("xui_inbound_id"), (raw.get("xui") or {}).get("inbound_id"),
              (raw.get("params") or {}).get("inbound_id"), raw.get("inbound_id")):
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    ports = {m.port, (raw.get("params") or {}).get("inbound_port")}
    for ib in inbounds:
        if ib.get("port") in ports:
            return ib.get("id")
    return None


def _from_xui(cfg: Config, protos: list[manifests.Manifest], g: Gathered) -> None:
    try:
        api = XuiClient.from_config(cfg, timeout=10)
        clients = api.clients()
        inbounds = api.inbounds()
    except XuiError as e:
        g.errors["3x-ui"] = str(e)
        return
    for c in clients:
        t = c.get("traffic")
        if not isinstance(t, dict) or not c.get("email"):
            continue
        last = _int(t.get("lastOnline")) // 1000 or None
        g.samples.append(Sample(XRAY, str(c["email"]), _int(t.get("up")), _int(t.get("down")), seen=last))
    g.polled.add(XRAY)
    by_id = {ib.get("id"): ib for ib in inbounds}
    for m in protos:
        ib = by_id.get(xui_inbound_id(m, inbounds))
        if ib is None:
            g.errors[m.id] = "inbound 3x-ui не найден"
            continue
        g.samples.append(Sample(m.id, "", _int(ib.get("up")), _int(ib.get("down")), epoch=f"ib{ib.get('id')}"))
        g.polled.add(m.id)


def _unit_epoch(unit: str) -> str:
    st = system.unit_states([unit]).get(unit) or {}
    return f"{st.get('pid', '')}@{st.get('since', '')}" if st.get("active") == "active" else ""


def _hy_secret(cfg: Config) -> str:
    if cfg.get("HY2_STATS_SECRET"):
        return cfg.get("HY2_STATS_SECRET")
    try:
        for line in (paths.etc() / "hy2-stats.hdr").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "authorization":
                return value.strip()
    except OSError:
        pass
    return ""


def hysteria_stats(port: int, secret: str, timeout: float = 5.0) -> dict[str, dict[str, int]]:
    """GET /traffic trafficStats API Hysteria: {user: {tx, rx}} (tx — от клиента)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/traffic", headers={"Authorization": secret})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        data = json.loads(resp.read() or b"{}")
    return data if isinstance(data, dict) else {}


STATS_LISTEN_RE = re.compile(r"^trafficStats:[ \t]*\n(?:[ \t]+\S.*\n)*?[ \t]+listen:[ \t]*\S*:(\d+)", re.M)


def _hy_stats_port(cfg: Config, m: manifests.Manifest) -> int | None:
    """Порт trafficStats инстанса: манифест → config.env → конфиг самого инстанса. Последнее
    нужно, когда второй инстанс (Salamander) включили поверх старой установки и ключа в
    config.env нет: без этого он молча оставался бы с нулями."""
    obfs = m.id.endswith("-obfs")
    port = m.raw.get("stats_port") or cfg.int("HY2_OBFS_STATS_PORT" if obfs else "HY2_STATS_PORT")
    if port:
        return int(port)
    conf = Path(os.environ.get("HY_ETC", "/etc/hysteria")) / ("obfs.yaml" if obfs else "config.yaml")
    try:
        found = STATS_LISTEN_RE.search(conf.read_text(encoding="utf-8"))
    except OSError:
        return None
    return int(found.group(1)) if found else None


def _from_hysteria(cfg: Config, protos: list[manifests.Manifest], g: Gathered) -> None:
    secret = _hy_secret(cfg)
    for m in protos:
        port = _hy_stats_port(cfg, m)
        if not port or not secret:
            # ключей нет — через модуль (сумма по инстансам, без разбивки); у инстанса без своего
            # модуля (Salamander) так нельзя — и тогда это ошибка, а не тихий ноль
            if m.id in protolib.list_libs():
                _from_module(m, g)
            else:
                g.errors[m.id] = "нет порта trafficStats или секрета HY2_STATS_SECRET"
            continue
        try:
            stats = hysteria_stats(int(port), secret)
        except (OSError, ValueError, urllib.error.URLError) as e:
            g.errors[m.id] = f"trafficStats 127.0.0.1:{port}: {getattr(e, 'reason', e)}"
            continue
        epoch = _unit_epoch(m.services[0]) if m.services else ""
        for user, v in stats.items():
            if isinstance(v, dict):
                g.samples.append(Sample(m.id, str(user), _int(v.get("tx")), _int(v.get("rx")), epoch=epoch))
        g.sum_protos.add(m.id)
        g.polled.add(m.id)


def _iface_epoch(iface: str) -> str:
    try:
        return "if" + (Path("/sys/class/net") / iface / "ifindex").read_text().strip()
    except OSError:
        return ""


def _from_module(m: manifests.Manifest, g: Gathered) -> None:
    try:
        rows = protolib.traffic(m.id)
    except protolib.ProtoError as e:
        g.errors[m.id] = e.short()
        return
    iface = m.raw.get("interface")
    epoch = _iface_epoch(iface) if isinstance(iface, str) and iface else ""
    has_total = False
    for r in rows:
        if r.user == "":
            if r.extra.get("scope") == "inbound" or not has_total:
                g.samples.append(Sample(m.id, "", r.up, r.down, epoch=epoch))
                has_total = True
            continue
        if r.extra.get("shared") or r.extra.get("scope") == "xui-client":
            continue  # общий счётчик Xray снимается напрямую из 3x-ui
        hs = _int(r.extra.get("latest_handshake")) or None
        g.samples.append(Sample(m.id, r.user, r.up, r.down, epoch=epoch, seen=hs))
    if not has_total:
        g.sum_protos.add(m.id)
    g.polled.add(m.id)


def default_iface(proc: Path | None = None) -> str | None:
    """Интерфейс маршрута по умолчанию из /proc/net/route."""
    try:
        with open((proc or system.PROC) / "net" / "route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        pass
    return None


def _host_sample() -> Sample | None:
    iface = default_iface()
    try:
        net = system.net_counters()
    except (OSError, ValueError):
        return None
    if not iface or iface not in net:
        return None
    try:
        boot = (system.PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        boot = ""
    c = net[iface]
    return Sample(HOST, "", c["rx_bytes"], c["tx_bytes"], epoch=f"{boot}/{iface}")


def gather(cfg: Config) -> Gathered:
    """Снять счётчики всех включённых протоколов."""
    g = Gathered()
    good, _ = manifests.load_all()
    libs = set(protolib.list_libs())
    xui, hy, rest = [], [], []
    for m in good:
        if not m.enabled or not m.has_users:
            continue
        if m.users_backend == "xui":
            xui.append(m)
        elif m.engine == "hysteria":
            hy.append(m)
        elif m.id in libs:
            rest.append(m)
    if xui:
        _from_xui(cfg, xui, g)
    if hy:
        _from_hysteria(cfg, hy, g)
    for m in rest:
        _from_module(m, g)
    host = _host_sample()
    if host:
        g.samples.append(host)
        g.polled.add(HOST)
    return g


# ---------- приращения ----------

@dataclass
class Delta:
    proto: str
    user: str
    up: int
    down: int
    reset: bool = False


def compute_deltas(prev: dict[tuple[str, str], Counter], samples: Iterable[Sample], now: int,
                   known: set[str] | None = None) -> tuple[list[Delta], dict[tuple[str, str], Counter]]:
    """Приращения с прошлого снятия и новые значения счётчиков.

    Новая серия у протокола, который уже снимался (новый пользователь, первый трафик после
    рестарта Hysteria), считается с нуля; у протокола, которого раньше не было (первый
    запуск), — только база. known — протоколы, опрошенные раньше, даже без серий.
    """
    known_protos = {p for p, _ in prev} | (known or set())
    deltas: list[Delta] = []
    new: dict[tuple[str, str], Counter] = {}
    merged: dict[tuple[str, str], Sample] = {}
    for s in samples:
        # повтор серии в одном снятии (например, «?»-peer'ы AWG) — складываем
        m = merged.get((s.proto, s.user))
        if m is None:
            merged[(s.proto, s.user)] = Sample(s.proto, s.user, s.up, s.down, s.epoch, s.seen)
        else:
            m.up += s.up
            m.down += s.down
            m.seen = max(filter(None, (m.seen, s.seen)), default=None)
    for key, s in merged.items():
        old = prev.get(key)
        seen = s.seen
        if old is None:
            if s.proto in known_protos:
                d = Delta(s.proto, s.user, s.up, s.down)
            else:
                d = Delta(s.proto, s.user, 0, 0)
        elif s.up < old.up or s.down < old.down or (old.epoch and s.epoch and old.epoch != s.epoch):
            d = Delta(s.proto, s.user, s.up, s.down, reset=True)
        else:
            d = Delta(s.proto, s.user, s.up - old.up, s.down - old.down)
        if seen is None:
            seen = now if (d.up or d.down) else (old.seen if old else None)
        elif old and old.seen and seen < old.seen:
            seen = old.seen
        new[key] = Counter(s.up, s.down, s.epoch, now, seen)
        deltas.append(d)
    return deltas, new


def with_proto_totals(deltas: list[Delta], sum_protos: set[str]) -> list[Delta]:
    """Добавить итог протокола (user="") как сумму приращений его пользователей."""
    totals: dict[str, list[int]] = {}
    for d in deltas:
        if d.proto in sum_protos and d.user:
            t = totals.setdefault(d.proto, [0, 0])
            t[0] += d.up
            t[1] += d.down
    return deltas + [Delta(p, "", up, down) for p, (up, down) in totals.items()]


def store(con: sqlite3.Connection, deltas: list[Delta], counters: dict[tuple[str, str], Counter],
          now: int) -> None:
    rows = []
    for d in deltas:
        if d.up <= 0 and d.down <= 0:
            continue
        for res in RESOLUTIONS:
            rows.append((res, align(now, res), d.proto, d.user, d.up, d.down))
    con.executemany(
        "INSERT INTO traffic (res, ts, proto, user, up, down) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (res, ts, proto, user) DO UPDATE SET up = up + excluded.up, down = down + excluded.down",
        rows)
    con.executemany(
        "INSERT OR REPLACE INTO counters (proto, user, up, down, epoch, ts, seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(p, u, c.up, c.down, c.epoch, c.ts, c.seen) for (p, u), c in counters.items()])


def prune(con: sqlite3.Connection, now: int) -> None:
    for res, keep in RETENTION.items():
        con.execute("DELETE FROM traffic WHERE res = ? AND ts < ?", (res, now - keep))
    con.execute("DELETE FROM runs WHERE ts < ?", (now - 7 * 86400,))


def polled_before(con: sqlite3.Connection) -> set[str]:
    r = con.execute("SELECT value FROM meta WHERE key = 'polled'").fetchone()
    try:
        return set(json.loads(r[0])) if r else set()
    except (ValueError, TypeError):
        return set()


def load_counters(con: sqlite3.Connection) -> dict[tuple[str, str], Counter]:
    return {(r["proto"], r["user"]): Counter(r["up"], r["down"], r["epoch"], r["ts"], r["seen"])
            for r in con.execute("SELECT * FROM counters")}


def collect(cfg: Config, now: int | None = None) -> dict[str, Any]:
    """Снять счётчики и записать приращения. {series, up, down, errors, duration}."""
    started = time.monotonic()
    lock = paths.state_dir() / "collector.lock"
    with file_lock(lock, timeout=60):
        g = gather(cfg)
        now = int(time.time()) if now is None else now
        con = connect()
        assert con is not None
        try:
            with con:
                prev = load_counters(con)
                known = polled_before(con)
                deltas, new = compute_deltas(prev, g.samples, now, known)
                deltas = with_proto_totals(deltas, g.sum_protos)
                store(con, deltas, new, now)
                user_d = [d for d in deltas if d.user and d.proto != HOST]
                up = sum(d.up for d in user_d)
                down = sum(d.down for d in user_d)
                dur = round(time.monotonic() - started, 2)
                con.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (now, int(not g.errors), len(g.samples), up, down, dur,
                             json.dumps(g.errors, ensure_ascii=False)))
                con.execute("INSERT OR REPLACE INTO meta VALUES ('polled', ?)",
                            (json.dumps(sorted(known | g.polled)),))
                prune(con, now)
        finally:
            con.close()
    return {"ts": now, "series": len(g.samples), "up": up, "down": down,
            "resets": [f"{d.proto}/{d.user or '*'}" for d in deltas if d.reset],
            "errors": g.errors, "duration": dur}


# ---------- отчёты ----------

def _con() -> sqlite3.Connection | None:
    try:
        return connect(create=False)
    except sqlite3.Error:
        return None


def _hidden_sql(include_hidden: bool) -> tuple[str, list[str]]:
    """Условие «без служебных пользователей» для запросов по пользователям."""
    names = [] if include_hidden else sorted(users.hidden_names())
    if not names:
        return "", []
    return f"user NOT IN ({', '.join('?' * len(names))})", names


def proto_title(proto: str) -> str:
    return SPECIAL_TITLES.get(proto, proto)


def report(cfg: Config | None = None, user: str | None = None, period: str = "24h",
           proto: str | None = None, by: str | None = None, now: float | None = None,
           include_hidden: bool = False) -> dict[str, Any]:
    """Сводка за период.

    by="user" — по пользователям (сумма по их протоколам); by="protocol" — по протоколам:
    без user это итоги протоколов (для Xray — счётчики inbound), с user — его серии
    (Xray-протоколы у пользователя — одна строка «xray»).
    Возвращает {period, since, step, by, user, rows: [{key, title, up, down, total}], total}.
    """
    period = resolve_period(period)
    by = by or ("protocol" if user else "user")
    since, step, n, res = window(period, now)
    out: dict[str, Any] = {"period": period, "title": PERIOD_TITLES[period], "since": since, "step": step,
                           "by": by, "user": user, "proto": proto, "rows": [],
                           "total": {"up": 0, "down": 0, "total": 0}, "db": str(db_path())}
    con = _con()
    if con is None:
        out["empty"] = True
        return out
    where, args = ["res = ?", "ts >= ?", "proto != ?"], [res, since, HOST]
    if user:
        where.append("user = ?")
        args.append(user)
    elif by == "protocol":
        where.append("user = ''")
    else:
        where.append("user != ''")
        cond, names = _hidden_sql(include_hidden)
        if cond:
            where.append(cond)
            args += names
    if proto:
        where.append("proto = ?")
        args.append(proto)
    col = "user" if by == "user" else "proto"
    try:
        rows = con.execute(f"SELECT {col} AS key, SUM(up) AS up, SUM(down) AS down FROM traffic "
                           f"WHERE {' AND '.join(where)} GROUP BY {col} ORDER BY SUM(up + down) DESC",
                           args).fetchall()
    finally:
        con.close()
    for r in rows:
        up, down = int(r["up"] or 0), int(r["down"] or 0)
        title = proto_title(r["key"]) if by == "protocol" else r["key"]
        out["rows"].append({"key": r["key"], "title": title, "up": up, "down": down, "total": up + down})
        out["total"]["up"] += up
        out["total"]["down"] += down
    out["total"]["total"] = out["total"]["up"] + out["total"]["down"]
    return out


def timeseries(period: str = "24h", group: str = "protocol", user: str | None = None,
               now: float | None = None, top: int = 7) -> dict[str, Any]:
    """Ряды для графика: {buckets: [ts...], step, series: [{key, title, values: [байт...]}]}.

    group="protocol" — итоги протоколов (или серии пользователя user); group="user" —
    по пользователям. Ряды сверх top сворачиваются в «прочие»; HOST — отдельный ряд в
    group="host".
    """
    since, step, n, res = window(period, now)
    buckets = [since + i * step for i in range(n)]
    out: dict[str, Any] = {"period": resolve_period(period), "step": step, "buckets": buckets, "series": []}
    con = _con()
    if con is None:
        return out
    where, args = ["res = ?", "ts >= ?"], [res, since]
    if group == "host":
        where.append("proto = ?")
        args.append(HOST)
    else:
        where.append("proto != ?")
        args.append(HOST)
        if user:
            where.append("user = ?")
            args.append(user)
        elif group == "protocol":
            where.append("user = ''")
        else:
            where.append("user != ''")
            cond, names = _hidden_sql(False)
            if cond:
                where.append(cond)
                args += names
    col = "user" if group == "user" else "proto"
    try:
        rows = con.execute(f"SELECT ts, {col} AS key, up, down FROM traffic WHERE {' AND '.join(where)}",
                           args).fetchall()
    finally:
        con.close()
    series: dict[str, list[list[int]]] = {}
    for r in rows:
        i = (r["ts"] - since) // step
        if 0 <= i < n:
            v = series.setdefault(r["key"], [[0, 0] for _ in range(n)])
            v[i][0] += r["up"]
            v[i][1] += r["down"]
    ordered = sorted(series.items(), key=lambda kv: -sum(a + b for a, b in kv[1]))
    if len(ordered) > top:
        rest = [[0, 0] for _ in range(n)]
        for _, vals in ordered[top - 1:]:
            for i, (a, b) in enumerate(vals):
                rest[i][0] += a
                rest[i][1] += b
        ordered = ordered[:top - 1] + [("…", rest)]
    for key, vals in ordered:
        title = "прочие" if key == "…" else (proto_title(key) if col == "proto" else key)
        out["series"].append({"key": key, "title": title, "up": [a for a, _ in vals],
                              "down": [b for _, b in vals], "values": [a + b for a, b in vals]})
    return out


def today(group: str = "protocol", now: float | None = None, include_hidden: bool = False) -> dict[str, int]:
    """Трафик за текущие сутки: {протокол | пользователь: байты}. По пользователям служебные
    (zoo-probe) скрыты, include_hidden=True — с ними."""
    now = time.time() if now is None else now
    con = _con()
    if con is None:
        return {}
    col = "user" if group == "user" else "proto"
    cond = "user != ''" if group == "user" else "user = ''"
    args: list[Any] = [RES_1D, align(now, RES_1D)]
    hidden, names = _hidden_sql(include_hidden)
    if group == "user" and hidden:
        cond += f" AND {hidden}"
        args += names
    try:
        rows = con.execute(f"SELECT {col} AS key, SUM(up + down) AS t FROM traffic WHERE res = ? AND ts = ? "
                           f"AND {cond} GROUP BY {col}", args).fetchall()
    finally:
        con.close()
    return {r["key"]: int(r["t"] or 0) for r in rows}


def first_run_since(ts: float) -> int | None:
    """Время первого снятия счётчиков не раньше ts (его приращение — база, не трафик) или None."""
    con = _con()
    if con is None:
        return None
    try:
        r = con.execute("SELECT MIN(ts) FROM runs WHERE ts >= ?", (int(ts),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return int(r[0]) if r and r[0] is not None else None


def today_split(own_down: dict[str, int] | None = None, now: float | None = None,
                own_weight: dict[str, int] | None = None) -> dict[str, tuple[int, int]]:
    """Трафик протоколов за текущие сутки без служебного zoo-probe: {протокол: (↑ от клиента, ↓ к клиенту)}.

    Где у протокола есть серия служебного пользователя (Hysteria, AmneziaWG), она вычитается из итога
    точно. У Xray-протоколов 3x-ui считает клиента одним счётчиком на все inbound, поэтому служебный
    трафик известен точно только суммой по всем Xray-протоколам (серия zoo-probe в «xray»): она делится
    между ними по весам own_weight (замеры live) — итог по каждому приблизителен, сумма точна. Нет серии
    zoo-probe (замеры шли от owner) — из ↓ вычитаются байты загрузок own_down, цифра чуть выше настоящей.
    """
    now = time.time() if now is None else now
    con = _con()
    if con is None:
        return {}
    try:
        rows = con.execute("SELECT proto, user, up, down FROM traffic WHERE res = ? AND ts = ? AND proto != ?",
                           (RES_1D, align(now, RES_1D), HOST)).fetchall()
    finally:
        con.close()
    hidden = users.hidden_names()
    totals = {r["proto"]: [int(r["up"]), int(r["down"])] for r in rows if r["user"] == ""}
    sub: dict[str, list[int]] = {}
    shared = [0, 0]
    for r in rows:
        if r["user"] in hidden:
            acc = shared if r["proto"] == XRAY else sub.setdefault(r["proto"], [0, 0])
            acc[0] += int(r["up"])
            acc[1] += int(r["down"])
    xray = sorted(m.id for m in manifests.load_all()[0]
                  if m.enabled and m.users_backend == "xui" and m.id in totals and m.id not in sub)
    weights = {p: max((own_weight or {}).get(p, 0), 0) for p in xray}
    wsum = sum(weights.values())
    out: dict[str, tuple[int, int]] = {}
    for proto, (up, down) in totals.items():
        if proto in sub:
            up, down = up - sub[proto][0], down - sub[proto][1]
        elif proto in xray and any(shared):
            share = weights[proto] / wsum if wsum else 1 / len(xray)
            up, down = up - round(shared[0] * share), down - round(shared[1] * share)
        else:
            down -= (own_down or {}).get(proto, 0)
        out[proto] = (max(up, 0), max(down, 0))
    return out


def last_seen() -> dict[str, int]:
    """{пользователь: unix-время последней активности} по всем его сериям."""
    con = _con()
    if con is None:
        return {}
    try:
        hidden, names = _hidden_sql(False)
        rows = con.execute("SELECT user, MAX(seen) AS seen FROM counters WHERE user != '' AND seen IS NOT NULL "
                           + (f"AND {hidden} " if hidden else "") + "GROUP BY user", names).fetchall()
    finally:
        con.close()
    return {r["user"]: int(r["seen"]) for r in rows}


def last_run() -> dict[str, Any] | None:
    con = _con()
    if con is None:
        return None
    try:
        r = con.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        r = None
    finally:
        con.close()
    if r is None:
        return None
    d = dict(r)
    d["errors"] = json.loads(d["errors"] or "{}")
    d["age"] = int(time.time()) - d["ts"]
    d["stale"] = d["age"] > STALE_AFTER
    return d


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("user", nargs="?", help="пользователь (по умолчанию все)")
    p.add_argument("--period", default="24h", help="период: " + ", ".join(PERIODS) + " (по умолчанию 24h)")
    p.add_argument("--by", choices=("user", "protocol"), help="группировка (по умолчанию: user, "
                                                               "а для одного пользователя — protocol)")
    p.add_argument("--proto", help="только этот протокол")
    p.add_argument("--collect", action="store_true", help="снять счётчики (запускает zoo-collector.timer)")
    p.add_argument("--all", action="store_true", help=f"со служебным пользователем пробника ({users.PROBE_USER})")


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def cmd_traffic(args: argparse.Namespace, cfg: Config) -> int:
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
            print(f"снято серий: {res['series']}, ↑ {output.human_bytes(res['up'])} ↓ "
                  f"{output.human_bytes(res['down'])} за {res['duration']} с" + (f"; ошибки: {errs}" if errs else ""))
        # ошибка одного источника не валит таймер: остальные серии записаны
        return 0
    try:
        data = report(cfg, args.user, args.period, args.proto, args.by, include_hidden=args.all)
    except ValueError as e:
        output.error(str(e))
        return 2
    data["last_run"] = last_run()
    if args.json:
        output.print_json(data)
        return 0
    if data.get("empty"):
        output.warn(f"истории трафика нет ({data['db']}): коллектор ещё не запускался "
                    "(systemctl status zoo-collector.timer; вручную: zoo traffic --collect)")
        return 0
    head = "пользователь" if data["by"] == "user" else "протокол"
    who = f" — {data['user']}" if data["user"] else ""
    print(f"Трафик {data['title']} (с {_fmt_ts(data['since'])}){who}")
    rows = [[r["title"], output.human_bytes(r["up"]), output.human_bytes(r["down"]),
             output.human_bytes(r["total"])] for r in data["rows"]]
    t = data["total"]
    rows.append(["ИТОГО", output.human_bytes(t["up"]), output.human_bytes(t["down"]), output.human_bytes(t["total"])])
    print(output.table(rows, [head, "↑ от клиента", "↓ к клиенту", "всего"], right=(1, 2, 3)))
    if data["by"] == "protocol" and not data["user"]:
        print(output.color("Xray-протоколы — по счётчикам inbound; у пользователей их трафик общий (xray).", "dim"))
    run = data["last_run"]
    if run and run["stale"]:
        output.warn(f"последнее снятие {output.human_duration(run['age'])} назад — проверь zoo-collector.timer")
    return 0
