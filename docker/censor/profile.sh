#!/usr/bin/env bash
# profile.sh — эмулятор ТСПУ на маршрутизаторе между пробником и сервером.
#
#   profile.sh nat <IP-сервера>        маскарад в сторону сервера (ответы идут через цензор)
#   profile.sh <профиль> <IP-сервера>  включить профиль (предыдущий снимается)
#   profile.sh show                    текущие правила
#
# Профили (ожидаемый вердикт пробника — в README.md):
#   clean       без правил
#   drop-udp    весь UDP к серверу выбрасывается
#   ip-block    весь трафик к серверу и от него выбрасывается
#   freeze-16k  после 16 КБ от сервера в одном TCP-соединении ответы выбрасываются
#   rst-tls     TLS ClientHello к серверу (начало TCP-нагрузки 16 03 0x, тип 01) → RST клиенту
#   port-block  TCP к одному порту сервера (ZOO_CENSOR_PORT, по умолчанию 443) выбрасывается:
#               блокировка IP:порт, остальные порты доступны

set -euo pipefail

CMD="${1:-clean}"
TARGET="${2:-${ZOO_CENSOR_TARGET:-}}"
CHAIN="ZOO_CENSOR"

need_target() { [ -n "$TARGET" ] || { echo "нужен IP сервера (аргумент 2 или ZOO_CENSOR_TARGET)" >&2; exit 2; }; }

chain_reset() {
    iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
    iptables -C FORWARD -j "$CHAIN" 2>/dev/null || iptables -I FORWARD 1 -j "$CHAIN"
}

# Начало TCP-нагрузки: смещение IP-заголовка + смещение TCP-заголовка
PAYLOAD='0>>22&0x3C@ 12>>26&0x3C@'

case "$CMD" in
    nat)
        need_target
        dev="$(ip -o route get "$TARGET" | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
        [ -n "$dev" ] || { echo "нет маршрута к $TARGET" >&2; exit 1; }
        iptables -t nat -C POSTROUTING -d "$TARGET" -o "$dev" -j MASQUERADE 2>/dev/null \
            || iptables -t nat -A POSTROUTING -d "$TARGET" -o "$dev" -j MASQUERADE
        echo "censor: NAT к $TARGET через $dev"
        exit 0 ;;
    show)
        iptables -S "$CHAIN" 2>/dev/null || true
        exit 0 ;;
esac

chain_reset
case "$CMD" in
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
        iptables -A "$CHAIN" -p tcp -s "$TARGET" -m connbytes \
            --connbytes 16384: --connbytes-dir reply --connbytes-mode bytes -j DROP ;;
    rst-tls)
        need_target
        # запись TLS Handshake (16 03 00..03) с ClientHello (01) в начале нагрузки сегмента
        iptables -A "$CHAIN" -p tcp -d "$TARGET" -m u32 \
            --u32 "$PAYLOAD 0>>8=0x160300:0x160303 && $PAYLOAD 5>>24=0x01" \
            -j REJECT --reject-with tcp-reset ;;
    port-block)
        need_target
        iptables -A "$CHAIN" -p tcp -d "$TARGET" --dport "${ZOO_CENSOR_PORT:-443}" -j DROP ;;
    *) echo "неизвестный профиль: $CMD" >&2; exit 2 ;;
esac

echo "censor: профиль $CMD${TARGET:+ (сервер $TARGET)}"
iptables -S "$CHAIN"
