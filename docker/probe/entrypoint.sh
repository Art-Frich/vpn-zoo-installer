#!/usr/bin/env bash
# zoo-probe — точка входа образа пробника (CMD).
# Читает пакет $ZOO_PROBE_DATA/probe-export.json (zoo export-probe с сервера), проверяет каждый
# протокол через провайдера этой машины, пишет probe-report.json и probe-report.md рядом.
# Дополнительные аргументы уходят в `zoo probe --remote` (например, --proto vless-reality).
#
# Окружение:
#   ZOO_PROBE_DATA    каталог с пакетом и отчётом (по умолчанию /data)
#   ZOO_PROBE_VIA     стенд: IP маршрутизатора-цензора — маршрут к серверу пойдёт через него

set -euo pipefail

DATA="${ZOO_PROBE_DATA:-/data}"
EXPORT="$DATA/probe-export.json"

if [ ! -f "$EXPORT" ]; then
    echo "[x] нет $EXPORT" >&2
    echo "    на сервере: sudo zoo export-probe --out probe-export.json, скопируйте файл в каталог," >&2
    echo "    подключённый как /data (docker run -v <каталог>:/data ...)" >&2
    exit 2
fi

if [ -n "${ZOO_PROBE_VIA:-}" ]; then
    server="$(jq -r '.server_ip // empty' "$EXPORT")"
    [ -n "$server" ] || { echo "[x] в пакете нет server_ip" >&2; exit 2; }
    ip route replace "$server/32" via "$ZOO_PROBE_VIA"
fi

[ -e /dev/net/tun ] || echo "[!] нет /dev/net/tun — AmneziaWG не проверить (docker run --device /dev/net/tun --cap-add NET_ADMIN)" >&2

set +e
zoo probe --remote "$EXPORT" --out "$DATA/probe-report.json" --md "$DATA/probe-report.md" "$@"
rc=$?
set -e
echo
echo "[i] отчёт: $DATA/probe-report.json, $DATA/probe-report.md (в пакете probe-export.json — ключи, удалите его)" >&2
exit "$rc"
