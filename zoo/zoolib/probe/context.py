"""Контекст пробы: метки владельца (--tag, --device) и то, что видно о прямом подключении
машины, с которой идёт проба: страна, ASN и провайдер, тип сети.

Откуда ASN и провайдер. Офлайн-базы ASN в stdlib нет, а ставить сторонние пакеты и качать
сотни мегабайт ради подписи нельзя. Берём `https://speed.cloudflare.com/meta` (JSON с asn,
asOrganization, country): с этого же хоста пробник и так качает тестовые файлы, поэтому новых
третьих сторон нет, а адрес в запрос не входит (Cloudflare видит его сам, как при любом
запросе). Страна — ещё и из `cdn-cgi/trace` (`loc=`), если meta недоступен. Конечная точка
не документирована и требует Referer: может измениться — тогда провайдер просто не определится.
`--no-lookup` отключает поиск совсем (метки и тип сети остаются).

В историю (history/, jsonl) уходят только страна, номер ASN и название провайдера: ни IP, ни город.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .fetch import fetch

META_URL = "https://speed.cloudflare.com/meta"
META_HEADERS = {"Referer": "https://speed.cloudflare.com/", "Origin": "https://speed.cloudflare.com"}
NET_TYPES = ("wifi", "ethernet", "cellular")

# свободный текст: буквы (в т. ч. русские), цифры, пробел и . _ + - / @ ( ); без «:» — значит, без IPv6
LABEL_RE = re.compile(r"^\w[\w .+\-/@()]{0,39}$")
IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_label(value: str, what: str = "метка") -> str:
    """Проверка метки (--tag, --device): короткий текст без IP-адресов. ValueError — с сообщением по-русски."""
    v = " ".join(str(value).split())
    if not v:
        raise ValueError(f"{what}: пусто")
    if not LABEL_RE.match(v):
        raise ValueError(f"{what}: до 40 символов — буквы, цифры, пробел и . _ + - / @ ( ); без «:» и спецсимволов")
    if IPV4_RE.search(v) or IPV6_RE.search(v):
        raise ValueError(f"{what}: IP-адрес в метке не допускается (метки попадают в историю)")
    return v


def label_arg(what: str):
    """Тип аргумента argparse для метки."""
    import argparse

    def conv(value: str) -> str:
        try:
            return clean_label(value, what)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None
    return conv


def clean_text(value: Any, limit: int = 60) -> str | None:
    """Строка из внешнего источника (имя провайдера): без управляющих символов, обрезана."""
    if not isinstance(value, str):
        return None
    v = " ".join(CTRL_RE.sub(" ", value).split())[:limit]
    return v or None


# ---------- тип сети ----------

def _default_iface() -> str | None:
    try:
        for line in Path("/proc/net/route").read_text(encoding="ascii", errors="replace").splitlines()[1:]:
            f = line.split()
            if len(f) > 3 and f[1] == "00000000" and int(f[3], 16) & 2:
                return f[0]
    except (OSError, ValueError):
        pass
    return None


def net_hint(env: dict[str, str] | None = None) -> str | None:
    """wifi | ethernet | cellular — по интерфейсу маршрута по умолчанию; None — не определить.
    В контейнере Docker виден только виртуальный eth0, тип сети хоста недоступен: тогда ZOO_PROBE_NET
    или --tag. ZOO_PROBE_NET=wifi|ethernet|cellular задаёт значение вручную."""
    env = os.environ if env is None else env
    forced = (env.get("ZOO_PROBE_NET") or "").strip().lower()
    if forced in NET_TYPES:
        return forced
    iface = _default_iface()
    if not iface:
        return None
    base = Path("/sys/class/net") / iface
    if (base / "wireless").exists() or (base / "phy80211").exists():
        return "wifi"
    if iface.startswith(("wwan", "rmnet", "ccmni", "ppp", "usb")):
        return "cellular"
    if Path("/.dockerenv").exists():
        return None
    if iface.startswith(("eth", "en")):
        return "ethernet"
    return None


# ---------- прямое подключение ----------

def _trace_loc(body: str) -> str | None:
    m = re.search(r"(?m)^loc=([A-Z]{2})\s*$", body or "")
    return m.group(1) if m else None


def parse_meta(body: str) -> dict[str, Any]:
    """Ответ speed.cloudflare.com/meta → {country, asn, isp}; мусор → пустой словарь."""
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict[str, Any] = {}
    asn = d.get("asn")
    if isinstance(asn, int) and 0 < asn < 4_294_967_296:
        out["asn"] = asn
    isp = clean_text(d.get("asOrganization"))
    if isp:
        out["isp"] = isp
    c = d.get("country")
    if isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c):
        out["country"] = c
    return out


def direct_context(ip_urls: tuple[str, ...], lookup: bool = True) -> dict[str, Any]:
    """Прямое подключение (без туннеля): {ip, country, asn, isp}. Любой сбой — None в поле."""
    out: dict[str, Any] = {"ip": None, "country": None, "asn": None, "isp": None}
    for url in ip_urls:
        r = fetch(url, connect_timeout=5, stall=5, max_time=8, keep_body=2048)
        body = r.get("body") or ""
        m = re.search(r"(?m)^ip=([0-9a-fA-F:.]+)\s*$", body)
        if m:
            out["ip"], out["country"] = m.group(1), _trace_loc(body)
            break
    if lookup:
        r = fetch(META_URL, connect_timeout=5, stall=5, max_time=8, keep_body=4096, extra_headers=META_HEADERS)
        meta = parse_meta(r.get("body") or "") if r.get("ok") else {}
        for k in ("asn", "isp", "country"):
            if meta.get(k) is not None:
                out[k] = meta[k]
    return out


def build(tag: str | None, device: str | None, direct: dict[str, Any] | None = None,
          net: str | None = None) -> dict[str, Any]:
    """Блок `context` отчёта (без IP)."""
    d = direct or {}
    return {"tag": tag or None, "device": device or None, "net": net,
            "country": d.get("country"), "asn": d.get("asn"), "isp": d.get("isp")}
