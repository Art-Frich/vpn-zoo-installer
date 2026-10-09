"""Прогон пробы по списку протоколов: L4 → клиент → рукопожатие → малый запрос → большой
запрос → IP выхода; затем классификация (verdicts.classify) с учётом соседних протоколов.

Запись протокола (entry): {"id", "name", "layer", "port", "probe": {...} | None}.
"""

from __future__ import annotations

import errno
import re
import shutil
import socket
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import output, system
from . import clients, verdicts
from . import metrics as metrics_mod
from .endpoints import Endpoint, EndpointError, endpoint_of, with_host
from .fetch import fetch
from .verdicts import Obs, Thresholds

SMALL_URLS = ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204")
LARGE_URLS = ("https://speed.cloudflare.com/__down?bytes={bytes}", "https://proof.ovh.net/files/10Mb.dat")
# IP выхода: cloudflare.com не входит в geosite:category-ip-geo-detect (07-routing блокирует
# echo-сервисы, ipify и т. п. через туннель не ответят); запасной — IP-литерал без SNI
IP_URLS = ("https://cloudflare.com/cdn-cgi/trace", "https://1.1.1.1/cdn-cgi/trace")
# отдача: POST на speed.cloudflare.com/__up принимает тело любого размера и отвечает 200 (проверено
# 05.10.2026); документированного API у неё нет, поэтому замер отдачи — по запросу (--upload-mb)
UPLOAD_URL = "https://speed.cloudflare.com/__up"
IP_RE = re.compile(r"(?m)^ip=([0-9a-fA-F:.]+)\s*$")
RETRY_KINDS = verdicts.TARGET_KINDS | {"connect_timeout", "refused"}


@dataclass
class Settings:
    mode: str = "remote"            # local | remote
    timeout: float = 10.0           # рукопожатие и малый запрос
    connect_timeout: float = 5.0    # TCP-connect к порту сервера
    stall: float = 8.0              # нет данных дольше — застой
    large_bytes: int = 5_000_000    # объём большого запроса: по нему же скорость (--speed-mb); 0 — не качать
    large_max_time: float = 60.0
    small_urls: tuple[str, ...] = SMALL_URLS
    large_urls: tuple[str, ...] = LARGE_URLS
    ip_urls: tuple[str, ...] = IP_URLS
    thresholds: Thresholds = field(default_factory=Thresholds)
    latency_samples: int = 5        # замеров задержки на каждую из двух целей (0 — не мерить)
    latency_urls: tuple[str, ...] = SMALL_URLS
    upload_bytes: int = 0           # 0 — отдачу не мерить (--upload-mb)
    upload_url: str = UPLOAD_URL
    tag: str | None = None          # метки владельца (--tag, --device) и поиск провайдера (--no-lookup)
    device: str | None = None
    lookup: bool = True
    fallback: bool = True           # local: при неудаче по публичному IP — повтор через loopback
    loopback: str = "127.0.0.1"
    progress: Callable[[str], None] | None = output.info


# ---------- L4 ----------

def tcp_check(host: str, port: int, timeout: float, tries: int = 3) -> tuple[str, float | None]:
    """→ (ok | timeout | refused | unreachable | error, медиана RTT connect в мс)."""
    rtts: list[float] = []
    for _ in range(tries):
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                rtts.append((time.monotonic() - t0) * 1000)
            continue
        except ConnectionRefusedError:
            status = "refused"
        except socket.timeout:
            status = "timeout"
        except OSError as e:
            status = "unreachable" if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH) else "error"
        if not rtts:
            return status, None
    return "ok", round(statistics.median(rtts), 1)


def udp_check(host: str, port: int, timeout: float = 1.5) -> str:
    """Лучшее, что можно сделать без протокола: ICMP port unreachable → refused, иначе unknown."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        s.send(b"\0" * 8)
        s.recv(64)
        return "ok"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "unknown"
    except OSError:
        return "unknown"
    finally:
        s.close()


def udp_listening(port: int) -> str:
    """На сервере: слушает ли кто-нибудь UDP-порт (ss)."""
    socks = system.listening_sockets()
    if not socks:
        return "unknown"
    return "ok" if any(s.proto == "udp" and s.port == port for s in socks) else "refused"


# ---------- один протокол ----------

def _say(st: Settings, msg: str) -> None:
    if st.progress:
        st.progress(msg)


def _first_ok(client, urls: tuple[str, ...], **kw: Any) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for url in urls:
        last = client.run_jobs([dict(url=url, **kw)])[0]
        if last.get("ok"):
            return last
    return last


def measure(entry: dict[str, Any], probe: dict[str, Any], st: Settings, workdir: Path) -> dict[str, Any]:
    """Замеры без вердикта: {"obs": Obs, ...поля отчёта}."""
    res: dict[str, Any] = {"host": None, "port": entry.get("port"), "layer": entry.get("layer"),
                           "l4": {}, "handshake": {}, "small": {}, "large": {}, "egress_ip": None,
                           "latency_ms": None, "speed_mbps": None, "metrics": {}, "client_log": ""}
    try:
        ep: Endpoint = endpoint_of(probe)
    except (EndpointError, KeyError, TypeError, ValueError) as e:
        res["obs"] = Obs(layer=entry.get("layer") or "tcp", client_error=f"probe негоден: {e}")
        return res
    obs = Obs(layer=ep.layer)
    res.update(host=ep.host, port=ep.port, layer=ep.layer, obs=obs)

    if ep.layer == "tcp":
        obs.l4, rtt = tcp_check(ep.host, ep.port, st.connect_timeout)
        res["l4"] = {"status": obs.l4, "rtt_ms": rtt}
        if obs.l4 != "ok":
            return res
    else:
        obs.l4 = udp_listening(ep.port) if st.mode == "local" else udp_check(ep.host, ep.port)
        res["l4"] = {"status": obs.l4}
        if obs.l4 == "refused":
            return res

    try:
        client = clients.make_client(probe, workdir, st.mode, st.stall)
    except clients.ClientError as e:
        obs.client_error = str(e)
        return res
    try:
        try:
            client.start(st.timeout)
        except clients.ClientMissing as e:
            obs.skipped = str(e)
            return res
        except clients.ClientError as e:
            obs.client_error = str(e)
            return res
        hs, hs_err, hs_ms = client.handshake(st.timeout)
        obs.tunnel, obs.tunnel_error = hs, hs_err
        res["handshake"] = {"status": hs, "ms": hs_ms, "error": hs_err}
        if hs in ("timeout", "rejected", "fail"):
            res["client_log"] = client.log_tail()
            return res
        if getattr(client, "handshake_only", False):   # MTProxy: дальше Telegram, запросов через него нет
            obs.notes.append(client.NOTE)
            return res

        small = _first_ok(client, st.small_urls, connect_timeout=st.timeout, stall=st.timeout,
                          max_time=st.timeout * 1.5)
        obs.small_ok = bool(small.get("ok"))
        obs.small_error, obs.small_kind = small.get("error", ""), small.get("error_kind", "")
        res["small"] = {"ok": obs.small_ok, "ms": _ms(small.get("seconds")), "url": small.get("url"),
                        "error": obs.small_error}
        if not obs.small_ok:
            res["client_log"] = client.log_tail()
            return res
        metrics = res["metrics"]
        # повторный малый запрос (туннель уже поднят) — задержка
        again = client.run_jobs([dict(url=small["url"], connect_timeout=st.timeout, stall=st.timeout,
                                      max_time=st.timeout * 1.5)])[0]
        lat = again.get("seconds") if again.get("ok") else small.get("seconds")
        obs.latency_ms = res["latency_ms"] = _ms(lat)

        if st.large_bytes > 0:   # 0 — лёгкий замер без скачивания (live)
            large = {}
            for url in st.large_urls:
                large = client.run_jobs([dict(url=url.format(bytes=st.large_bytes), connect_timeout=st.timeout,
                                              stall=st.stall, max_time=st.large_max_time,
                                              limit=st.large_bytes)])[0]
                # на другой адрес — только если до тестового сервера не достучались (его вина или
                # разовый сбой), а не если встал уже идущий поток: застой повторять незачем
                if large.get("ok") or large.get("bytes") or large.get("error_kind") not in RETRY_KINDS:
                    break
            obs.large_ok = bool(large.get("ok"))
            obs.large_bytes = int(large.get("bytes") or 0)
            obs.large_expected = large.get("expected")
            obs.large_kind, obs.large_error = large.get("error_kind", ""), large.get("error", "")
            body_s = large.get("body_seconds") or large.get("seconds")
            if obs.large_ok and body_s:
                obs.speed_mbps = res["speed_mbps"] = round(obs.large_bytes * 8 / 1e6 / max(body_s, 1e-3), 2)
            metrics["download"] = metrics_mod.download_block(large)
            res["large"] = {"ok": obs.large_ok, "bytes": obs.large_bytes, "seconds": large.get("seconds"),
                            "url": large.get("url"), "stalled": bool(large.get("stalled")), "error": obs.large_error}
            if not obs.large_ok:
                res["client_log"] = client.log_tail()

        if st.ip_urls:
            ipr = _first_ok(client, st.ip_urls, connect_timeout=st.timeout, stall=st.timeout,
                            max_time=st.timeout * 1.5, keep_body=2048)
            m = IP_RE.search(ipr.get("body") or "")
            res["egress_ip"] = m.group(1) if m else None
            if not m:
                obs.notes.append(f"IP выхода не определён ({ipr.get('error') or 'нет ip= в ответе'})")
        # расширенные метрики — после всех замеров вердикта: они его не меняют
        if st.latency_samples > 0:
            metrics["latency"] = measure_latency(client, st)
        if st.upload_bytes and obs.large_ok:
            metrics["upload"] = measure_upload(client, st)
    except (clients.ClientError, OSError) as e:
        obs.client_error = str(e)
    finally:
        client.stop()
    return res


def measure_latency(client: Any, st: Settings) -> dict[str, Any]:
    """latency_samples замеров на каждую из двух целей (чередуются). Первый круг — один запрос
    на цель: если оба провалились, остальное не гоним (каждый провал стоит до таймаута)."""
    urls = st.latency_urls[:2]
    job = {"connect_timeout": st.timeout, "stall": st.timeout, "max_time": st.timeout * 1.5}
    done = client.run_jobs([dict(url=u, **job) for u in urls])
    if any(j.get("ok") for j in done) and st.latency_samples > 1:
        done += client.run_jobs([dict(url=u, **job) for _ in range(st.latency_samples - 1) for u in urls])
    return metrics_mod.latency_from_jobs(done)


def measure_upload(client: Any, st: Settings) -> dict[str, Any]:
    job = client.run_jobs([dict(url=st.upload_url, post_bytes=st.upload_bytes, connect_timeout=st.timeout,
                                stall=st.stall, max_time=st.large_max_time, keep_body=64)])[0]
    return metrics_mod.upload_block(job)


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(float(seconds) * 1000, 1)


def direct_ip(st: Settings) -> str | None:
    """Свой IP без туннеля (для сравнения с IP выхода)."""
    for url in st.ip_urls:
        r = fetch(url, connect_timeout=5, stall=5, max_time=8, keep_body=2048)
        m = IP_RE.search(r.get("body") or "")
        if m:
            return m.group(1)
    return None


# ---------- список протоколов ----------

def _loopback_for(probe: dict[str, Any], st: Settings) -> str:
    return clients.AwgTunnel.GATEWAY if probe.get("kind") == "awg" else st.loopback


def uses_tls(probe: dict[str, Any] | None) -> bool:
    """Начинается ли соединение с TLS ClientHello (REALITY/TLS поверх TCP, QUIC)."""
    if not probe:
        return False
    if probe.get("kind") == "xray":
        sec = ((probe.get("outbound") or {}).get("streamSettings") or {}).get("security")
        return sec in ("tls", "reality")
    if probe.get("kind") == "sing-box":
        return bool(((probe.get("outbound") or {}).get("tls") or {}).get("enabled"))
    return probe.get("kind") in ("hysteria", "mtproto")


def run_one(entry: dict[str, Any], st: Settings, workdir: Path) -> dict[str, Any]:
    probe = entry.get("probe") or None
    base = {"id": entry["id"], "name": entry.get("name") or entry["id"],
            "kind": (probe or {}).get("kind"), "user": (probe or {}).get("user"), "tls": uses_tls(probe),
            "target": "public"}
    t0 = time.monotonic()
    if not probe:
        why = entry.get("skip_reason") or "в манифесте нет probe (нет пользователя?)"
        res = {"obs": Obs(layer=entry.get("layer") or "tcp", skipped=why),
               "host": None, "port": entry.get("port"), "layer": entry.get("layer")}
        return {**base, **res, "duration_s": 0.0}
    res = measure(entry, probe, st, workdir)
    if st.mode == "local" and st.fallback:
        first, _ = verdicts.classify(res["obs"], "local", th=st.thresholds)
        if first not in verdicts.WORKING and first not in verdicts.NOT_TESTED:
            lo = _loopback_for(probe, st)
            _say(st, f"{entry['id']}: по публичному IP — {first}, повтор через loopback")
            try:
                res2 = measure(entry, with_host(probe, lo), st, workdir)
            except EndpointError:
                res2 = None
            if res2:
                second, _ = verdicts.classify(res2["obs"], "local", th=st.thresholds)
                if second in verdicts.WORKING:
                    res2["obs"].notes.append(f"по публичному IP не прошло ({first}): хостер не пропускает "
                                             "трафик сервера к самому себе (hairpin) — проверено через loopback")
                    res, base["target"] = res2, "loopback"
                else:
                    res["obs"].notes.append(f"через loopback тоже не работает ({second})")
    if entry.get("note"):
        res["obs"].notes.append(entry["note"])
    return {**base, **res, "duration_s": round(time.monotonic() - t0, 1)}


def _tcp_context(raw: list[dict[str, Any]], context: list[dict[str, Any]], st: Settings,
                 tcp_by_host: dict[str, bool]) -> None:
    """Молчащий UDP без TCP-соседей в прогоне (--proto): проверить TCP-порты непроверенных
    протоколов того же адреса — иначе IP_BLOCKED нельзя отличить от UDP_BLOCKED."""
    need = {r["host"] for r in raw if r.get("layer") == "udp" and r.get("host")
            and r["host"] not in tcp_by_host}
    probed = {r.get("id") for r in raw}
    for e in context:
        if not need or e.get("id") in probed or not e.get("probe"):
            continue
        try:
            ep = endpoint_of(e["probe"])
        except (EndpointError, KeyError, TypeError, ValueError):
            continue
        if ep.layer != "tcp" or ep.host not in need:
            continue
        status, _ = tcp_check(ep.host, ep.port, st.connect_timeout)
        if status != "unknown":
            tcp_by_host[ep.host] = tcp_by_host.get(ep.host, False) or status in ("ok", "refused")


INTERCEPT_PORT = 9   # discard: на сервере за UFW закрыт — настоящий путь даёт таймаут, а не соединение


def tcp_intercepted(server_ip: str | None, timeout: float = 2.0) -> bool:
    """Локальный VPN/TUN (sing-box, Hiddify…) сам принимает любое TCP-соединение: тогда закрытый
    порт сервера «открывается» мгновенно, и проверка доступности портов ничего не значит."""
    if not server_ip:
        return False
    status, _ = tcp_check(server_ip, INTERCEPT_PORT, timeout)
    return status == "ok"


def run(entries: list[dict[str, Any]], st: Settings, server_ip: str | None = None,
        selftest: dict[str, str] | None = None, my_ip: str | None = None,
        context: list[dict[str, Any]] | None = None,
        server_ips: list[str] | None = None) -> list[dict[str, Any]]:
    """Прогон по всем протоколам и классификация. selftest — {id: вердикт серверной самопроверки};
    context — все протоколы пакета (до фильтра --proto), для TCP-контекста молчащего UDP;
    server_ips — все адреса сервера (IPv4 и IPv6): выход с любого из них — «с сервера»."""
    own = {a for a in (server_ips or []) if a} | ({server_ip} if server_ip else set())
    workdir = Path(tempfile.mkdtemp(prefix="zoo-probe-"))
    try:
        raw = []
        for e in entries:
            _say(st, f"{e['id']}: проверка…")
            raw.append(run_one(e, st, workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    # TCP-порт хоть одного протокола на том же адресе ответил (в т. ч. отказом) → IP доступен
    tcp_by_host: dict[str, bool] = {}
    for r in raw:
        if r.get("layer") == "tcp" and r.get("host") and r["obs"].l4 != "unknown":
            reachable = r["obs"].l4 in ("ok", "refused")
            tcp_by_host[r["host"]] = tcp_by_host.get(r["host"], False) or reachable
    if context:
        _tcp_context(raw, context, st, tcp_by_host)
    results = []
    for r in raw:
        obs: Obs = r.pop("obs")
        verdict, reason = verdicts.classify(obs, st.mode, tcp_by_host.get(r.get("host") or ""),
                                            (selftest or {}).get(r["id"]), st.thresholds)
        notes = list(obs.notes)
        ip = r.get("egress_ip")
        if ip:
            if ip in own:
                r["egress"] = "server"
            elif my_ip and ip == my_ip:
                r["egress"] = "direct"
                notes.append("IP выхода совпадает с вашим прямым IP (сервер за тем же NAT или туннель не используется)")
            else:
                r["egress"] = "other"
                notes.append(f"выход не с IP сервера, а с {ip} (WARP или NAT хостера)")
        if verdict in verdicts.WORKING:
            r.pop("client_log", None)
        if not r.get("metrics"):
            r.pop("metrics", None)
        r.update(verdict=verdict, reason=reason, notes=notes)
        results.append(r)
        _say(st, f"{r['id']}: {verdict}" + (f" — {reason}" if reason else ""))
    return results
