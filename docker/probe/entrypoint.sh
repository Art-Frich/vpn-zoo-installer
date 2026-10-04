#!/usr/bin/env bash
# zoo-probe — точка входа образа пробника (ENTRYPOINT).
# Читает пакет $ZOO_PROBE_DATA/probe-export.json (zoo export-probe с сервера), проверяет каждый
# протокол через провайдера этой машины, пишет probe-report.json и probe-report.md рядом.
# Аргументы с «-» уходят в `zoo probe --remote`: docker run ... zoo-probe --proto vless-reality.
# Первый аргумент без «-» — команда вместо пробника (стенд: sleep infinity, python3 …).
#
# Окружение:
#   ZOO_PROBE_DATA    каталог с пакетом и отчётом (по умолчанию /data)
#   ZOO_PROBE_VIA     стенд: IP маршрутизатора-цензора — маршрут к серверу пойдёт через него

set -euo pipefail

case "${1:-}" in
    -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    ""|-*) ;;
    *) exec "$@" ;;
esac

DATA="${ZOO_PROBE_DATA:-/data}"
EXPORT="$DATA/probe-export.json"

if [ ! -f "$EXPORT" ]; then
    echo "[x] нет $EXPORT" >&2
    echo "    скопируйте пакет с сервера в каталог probe/ и запускайте из каталога над ним:" >&2
    echo "    scp root@СЕРВЕР:/etc/vpn-setup/probe-export.json probe/probe-export.json" >&2
    # shellcheck disable=SC2016 # команды для пользователя, $PWD раскрывает его shell
    echo '    docker run ... -v "$PWD/probe:/data" zoo-probe   (PowerShell: -v "${PWD}\probe:/data")' >&2
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
echo "[i] отчёт: probe-report.md и probe-report.json в подключённом каталоге (у вас — probe/)." >&2
echo "[i] в probe-export.json — ключи доступа к серверу: удалите его после проверки." >&2
exit "$rc"
