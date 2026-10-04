#!/usr/bin/env python3
"""expect.py PROFILE REPORT.json — сверка вердиктов пробника с профилем цензора (стенд).

Ожидания (docker/censor/README.md):
    clean       все — OK или SLOW
    drop-udp    UDP — UDP_BLOCKED, TCP — OK/SLOW
    ip-block    все — IP_BLOCKED
    freeze-16k  TCP — FREEZE_16K, UDP — OK/SLOW
    rst-tls     TCP с TLS/REALITY — HANDSHAKE_FAIL, остальные — OK/SLOW
    port-block  TCP на порту 443 — IP_BLOCKED (причина — блокировка порта), остальные — OK/SLOW
SKIPPED (нет клиента) не считается провалом. Вывод: TSV «протокол, вердикт, ожидалось, итог»;
код 1 — есть несовпадения.
"""

from __future__ import annotations

import json
import sys

WORKING = {"OK", "SLOW"}


def expected(profile: str, r: dict) -> set[str]:
    udp = r.get("layer") == "udp"
    if profile == "clean":
        return WORKING
    if profile == "drop-udp":
        return {"UDP_BLOCKED"} if udp else WORKING
    if profile == "ip-block":
        return {"IP_BLOCKED"}
    if profile == "freeze-16k":
        return WORKING if udp else {"FREEZE_16K"}
    if profile == "rst-tls":
        return {"HANDSHAKE_FAIL"} if (not udp and r.get("tls")) else WORKING
    if profile == "port-block":
        return {"IP_BLOCKED"} if (not udp and r.get("port") == 443) else WORKING
    raise SystemExit(f"неизвестный профиль: {profile}")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    profile, path = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    bad = 0
    for r in report.get("results", []):
        want = expected(profile, r)
        if r["verdict"] == "SKIPPED":
            status = "SKIP"
        elif r["verdict"] in want:
            status = "PASS"
        else:
            status, bad = "FAIL", bad + 1
        print(f"{profile}\t{r['id']}\t{r['verdict']}\t{'|'.join(sorted(want))}\t{status}\t{r.get('reason', '')}")
    if not report.get("results"):
        print(f"{profile}\t-\t-\t-\tFAIL\tв отчёте нет протоколов")
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
