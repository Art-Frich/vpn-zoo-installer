#!/usr/bin/env bash
# profile.sh — переключение профиля цензора: profile.sh <профиль> [IP-сервера]
# Профили: clean, drop-udp, ip-block, freeze-16k, rst-tls (см. README.md)

set -euo pipefail

PROFILE="${1:-clean}"
TARGET="${2:-${ZOO_CENSOR_TARGET:-}}"
CHAIN="ZOO_CENSOR"

iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
iptables -C FORWARD -j "$CHAIN" 2>/dev/null || iptables -I FORWARD 1 -j "$CHAIN"

need_target() { [ -n "$TARGET" ] || { echo "нужен IP сервера (аргумент 2 или ZOO_CENSOR_TARGET)" >&2; exit 2; }; }

case "$PROFILE" in
    clean) ;;
    drop-udp)
        need_target
        iptables -A "$CHAIN" -p udp -d "$TARGET" -j DROP ;;
    ip-block)
        need_target
        iptables -A "$CHAIN" -d "$TARGET" -j DROP
        iptables -A "$CHAIN" -s "$TARGET" -j DROP ;;
    freeze-16k)
        need_target
        # ответный поток сервера дропается после ~16 КБ в одном TCP-соединении
        iptables -A "$CHAIN" -p tcp -s "$TARGET" -m connbytes \
            --connbytes 16384: --connbytes-dir reply --connbytes-mode bytes -j DROP ;;
    rst-tls)
        need_target
        # TLS handshake (0x16 0x03) от клиента к серверу — RST
        iptables -A "$CHAIN" -p tcp -d "$TARGET" -m string --algo bm --hex-string '|1603|' \
            -j REJECT --reject-with tcp-reset ;;
    *) echo "неизвестный профиль: $PROFILE" >&2; exit 2 ;;
esac

echo "censor: профиль $PROFILE${TARGET:+ (сервер $TARGET)}"
iptables -S "$CHAIN"
