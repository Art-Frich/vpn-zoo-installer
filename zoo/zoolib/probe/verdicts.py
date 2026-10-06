"""Классификация результата пробы (ARCHITECTURE §7). Чистые функции, без сети.

Наблюдение (Obs) собирает движок; classify() превращает его в вердикт и причину.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OK = "OK"
SLOW = "SLOW"
FREEZE_16K = "FREEZE_16K"
HANDSHAKE_FAIL = "HANDSHAKE_FAIL"
IP_BLOCKED = "IP_BLOCKED"
UDP_BLOCKED = "UDP_BLOCKED"
SERVER_DOWN = "SERVER_DOWN"
CLIENT_ERROR = "CLIENT_ERROR"
SKIPPED = "SKIPPED"

VERDICTS = (OK, SLOW, FREEZE_16K, HANDSHAKE_FAIL, IP_BLOCKED, UDP_BLOCKED, SERVER_DOWN, CLIENT_ERROR)
WORKING = {OK, SLOW}
BLOCKS = {FREEZE_16K, HANDSHAKE_FAIL, IP_BLOCKED, UDP_BLOCKED}
# итог «не проверялось»: нет клиента на этой машине или нет probe в манифесте
NOT_TESTED = {SKIPPED, CLIENT_ERROR}

def describe(verdict: str, intercepted: bool = False) -> str:
    """Расшифровка вердикта. Через локальный VPN/TUN путь к серверу идёт не через провайдера:
    «блокировка» тогда — свойство этого пути или хостера, а не ТСПУ."""
    text = DESCRIPTIONS.get(verdict, "")
    if intercepted and verdict in BLOCKS:
        text = text.replace(" (типичная «заморозка» ТСПУ)", "")
        text += " — путь шёл через локальный VPN, провайдер и ТСПУ тут ни при чём"
    return text


DESCRIPTIONS = {
    OK: "работает",
    SLOW: "работает, но медленно или с обрывами",
    FREEZE_16K: "соединение замирает после ~16–20 КБ (типичная «заморозка» ТСПУ)",
    HANDSHAKE_FAIL: "порт доступен, но рукопожатие/туннель не устанавливается",
    IP_BLOCKED: "IP сервера недоступен (пакеты не доходят)",
    UDP_BLOCKED: "UDP до сервера не проходит, TCP проходит",
    SERVER_DOWN: "протокол не работает на самом сервере (порт закрыт или сервис лежит)",
    CLIENT_ERROR: "ошибка клиента пробника (не сеть)",
    SKIPPED: "не проверялось",
}

# l4: ok | timeout | refused | unreachable | error | unknown
# tunnel (явное рукопожатие hysteria/awg): ok | timeout | rejected | fail | unknown
L4_SILENT = {"timeout", "unreachable", "error"}
# застой/обрыв уже поднятого туннеля; *_timeout — бюджет 16 КБ съели рукопожатия (REALITY +
# TLS внутри), и замер встал ещё до заголовков ответа
STALL_KINDS = {"stall", "reset", "eof", "header_timeout", "tls_timeout"}
TARGET_KINDS = {"http", "dns"}


@dataclass
class Thresholds:
    min_mbps: float = 2.0           # ниже — SLOW
    max_latency_ms: float = 1500.0  # выше — SLOW
    freeze_max_bytes: int = 64 * 1024  # застой раньше этого объёма — FREEZE_16K


@dataclass
class Obs:
    layer: str = "tcp"
    client_error: str = ""
    skipped: str = ""
    l4: str = "unknown"
    tunnel: str = "unknown"
    tunnel_error: str = ""
    small_ok: bool | None = None
    small_error: str = ""
    small_kind: str = ""
    large_bytes: int = 0
    large_expected: int | None = None
    large_ok: bool | None = None
    large_kind: str = ""
    large_error: str = ""
    speed_mbps: float | None = None
    latency_ms: float | None = None
    notes: list[str] = field(default_factory=list)


def classify(obs: Obs, mode: str = "remote", tcp_reachable: bool | None = None,
             selftest: str | None = None, th: Thresholds | None = None) -> tuple[str, str]:
    """→ (вердикт, причина по-русски).

    mode=local — проба с самого сервера: «блокировок» там не бывает, недоступность = SERVER_DOWN.
    tcp_reachable — доступен ли хоть один TCP-порт этого сервера (для UDP: IP_BLOCKED или
    UDP_BLOCKED). selftest — вердикт `zoo probe --local` по этому протоколу, если известен.
    """
    th = th or Thresholds()
    if obs.skipped:
        return SKIPPED, obs.skipped
    if obs.client_error:
        return CLIENT_ERROR, obs.client_error
    local = mode == "local"
    server_broken = selftest is not None and selftest not in WORKING and selftest not in NOT_TESTED

    # 1. доступность порта (для TCP — connect)
    if obs.l4 == "refused":
        return SERVER_DOWN, ("порт закрыт" if local or obs.layer == "udp" else
                             "порт отвечает отказом (RST): сервис не слушает или отказ подделан")
    if obs.layer == "tcp" and obs.l4 in L4_SILENT:
        if local or server_broken:
            return SERVER_DOWN, f"TCP-порт недоступен ({obs.l4})"
        if tcp_reachable:
            # другой TCP-порт того же адреса отвечает: режут не весь IP, а этот порт
            return IP_BLOCKED, (f"этот TCP-порт не отвечает ({obs.l4}), другие порты сервера доступны — "
                                "похоже на блокировку порта (IP:порт)")
        return IP_BLOCKED, f"TCP-соединение с сервером не устанавливается ({obs.l4})"

    # 2. туннель: явное рукопожатие или первый малый запрос
    tunnel_failed = obs.tunnel in ("timeout", "rejected", "fail") or obs.small_ok is False
    if tunnel_failed:
        why = obs.tunnel_error or obs.small_error or "туннель не поднялся"
        if server_broken:
            return SERVER_DOWN, f"{why}; на сервере протокол тоже не работает ({selftest})"
        # UDP без ответа (у TUIC явного рукопожатия нет — unknown); rejected = сервер ответил
        if obs.layer == "udp" and obs.tunnel in ("timeout", "unknown"):
            if local:
                if obs.l4 == "ok":
                    return HANDSHAKE_FAIL, f"порт слушает, но рукопожатие не прошло: {why}"
                return SERVER_DOWN, f"UDP-порт не слушает: {why}"
            if tcp_reachable is False:
                return IP_BLOCKED, f"нет ответа по UDP, TCP-порты сервера тоже недоступны: {why}"
            return UDP_BLOCKED, f"нет ответа по UDP: {why}"
        return HANDSHAKE_FAIL, why

    # 3. большой запрос
    notes = []
    if obs.large_ok is False:
        if obs.large_kind in TARGET_KINDS and obs.large_bytes == 0:
            notes.append(f"большой запрос не выполнен ({obs.large_error}): тестовый сервер недоступен")
        elif obs.large_kind in STALL_KINDS and obs.large_bytes < th.freeze_max_bytes:
            if obs.layer == "tcp":
                return FREEZE_16K, (f"малый запрос прошёл, большой встал на {obs.large_bytes // 1024} КБ "
                                    f"({obs.large_error})")
            return SLOW, f"большой запрос оборвался на {obs.large_bytes // 1024} КБ ({obs.large_error})"
        else:
            return SLOW, f"большой запрос не завершён: {obs.large_bytes // 1024} КБ ({obs.large_error})"

    # 4. скорость и задержка
    if obs.speed_mbps is not None and obs.speed_mbps < th.min_mbps:
        return SLOW, f"скорость {obs.speed_mbps:.2f} Мбит/с (порог {th.min_mbps:g})"
    if obs.latency_ms is not None and obs.latency_ms > th.max_latency_ms:
        return SLOW, f"задержка {obs.latency_ms:.0f} мс (порог {th.max_latency_ms:.0f})"
    return OK, "; ".join(notes)


# ---------- сравнение сервер ↔ клиент ----------

def compare_one(local: str | None, remote: str | None) -> tuple[str, str]:
    """→ (категория, вывод по-русски). Категории: works | blocked | server | unknown."""
    if remote is None and local is None:
        return "unknown", "нет данных"
    if remote is None:
        return "unknown", "у вас не проверялось"
    if remote in WORKING:
        return "works", "работает у вас" + (" (медленно)" if remote == SLOW else "")
    if remote in NOT_TESTED:
        return "unknown", f"у вас не проверено ({remote})"
    if local is None or local in NOT_TESTED:
        if remote in BLOCKS:
            return "blocked", f"не работает у вас ({remote}); серверной самопроверки нет"
        return "unknown", f"не работает у вас ({remote}); серверной самопроверки нет"
    if local not in WORKING:
        return "server", f"не работает на самом сервере ({local}) — дело не в блокировке"
    if remote == SERVER_DOWN:
        return "blocked", "работает в принципе, у вас порт отвечает отказом (RST) — похоже на блокировку"
    return "blocked", f"работает в принципе, блокируется у вас ({remote})"
