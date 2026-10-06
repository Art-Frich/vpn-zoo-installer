#!/usr/bin/env bash
# 04c-ss2022.sh — Shadowsocks-2022 (2022-blake3-aes-128-gcm, tcp+udp) через API 3x-ui.
# Запасной протокол без TLS (D5). Случайный высокий порт, ключ сервера в config.env
# (SS_PORT, SS_PSK), ключи пользователей генерирует панель. Пользователь owner.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-ss2022.sh"
config_load

[ -n "${SERVER_IP:-}" ] || die "SERVER_IP не задан (config.env)"
xui_wait_api 60 || die "API 3x-ui не отвечает — сначала фаза 03-3xui"

# Выключено флагом после установки: inbound выключить, порты закрыть, манифест enabled=false
if [ "${ENABLE_SS:-0}" != "1" ]; then
    SS_PORT="$(config_get SS_PORT)"
    proto_ss2022_disable
    log_ok "ENABLE_SS=$ENABLE_SS: SS-2022 выключен, порт закрыт"
    exit 0
fi

# ------------------------------------------------------------
# 1. Порт и ключ сервера (один раз, дальше из config.env)
# ------------------------------------------------------------

if config_has SS_PORT; then
    SS_PORT="$(config_get SS_PORT)"
fi
if [ -n "${SS_PORT:-}" ] && { ! [[ "$SS_PORT" =~ ^[0-9]+$ ]] || [ "$SS_PORT" -lt 1024 ] || [ "$SS_PORT" -gt 65535 ] || is_banned_port "$SS_PORT"; }; then
    die "SS_PORT=$SS_PORT недопустим (1024..65535, не из $BANNED_PORTS)"
fi
if ! config_has SS_PORT; then
    port="${SS_PORT:-}"
    if [ -z "$port" ]; then
        port="$(rand_port)" || die "не удалось подобрать порт для SS-2022"
    fi
    config_set SS_PORT "$port"
fi

if ! config_has SS_PSK; then
    psk="$(openssl rand -base64 16)" || die "openssl rand не сработал"
    config_set SS_PSK "$psk"
fi
SS_PSK="$(config_get SS_PSK)"
[ "$(printf '%s' "$SS_PSK" | base64 -d 2>/dev/null | wc -c)" = "16" ] \
    || die "SS_PSK в config.env — не 16 байт base64 (нужно для $SS2022_METHOD)"

# ------------------------------------------------------------
# 2. Inbound (создать или привести к нужному виду)
# ------------------------------------------------------------

# Новый порт свободен: занятый чужим процессом порт не даст Xray стартовать — лягут
# все Xray-протоколы сразу (VLESS, XHTTP, TUIC)
ss_port_free() {
    local other pr flag busy
    other="$(xui_inbound_find_by_port "$SS_PORT")"
    [ -z "$other" ] || die "порт $SS_PORT уже занят inbound id=$other в 3x-ui — задайте другой SS_PORT в config.env"
    for pr in tcp udp; do
        flag=-Hltnp; [ "$pr" = udp ] && flag=-Hlunp
        busy="$(ss "$flag" "sport = :$SS_PORT" 2>/dev/null | head -1)"
        [ -z "$busy" ] || die "порт $SS_PORT/$pr уже занят другим процессом: $busy — задайте другой SS_PORT в config.env"
    done
}

want="$(proto_ss2022_inbound_body)"
id="$(proto_ss2022_inbound_id)"
if [ -z "$id" ]; then
    ss_port_free
    id="$(xui_inbound_add "$want")" || die "3x-ui не создал inbound SS-2022"
    [[ "$id" =~ ^[0-9]+$ ]] || die "3x-ui вернул странный id inbound: $id"
    log_ok "inbound $SS2022_REMARK создан (id=$id, порт $SS_PORT tcp+udp)"
else
    cur="$(xui_inbound_get "$id")"
    if [ "$(jq -c '{port, enable, remark, m: .settings.method, p: .settings.password, n: .settings.network}' <<< "$cur")" \
         != "$(jq -c '{port, enable, remark, m: .settings.method, p: .settings.password, n: .settings.network}' <<< "$want")" ]; then
        old_port="$(jq -r '.port' <<< "$cur")"
        [ "$old_port" = "$SS_PORT" ] || ss_port_free
        xui_inbound_update "$id" "$want" || die "3x-ui не обновил inbound SS-2022 (id=$id)"
        if [ "$old_port" != "$SS_PORT" ]; then
            fw_revoke "$old_port/tcp"; fw_revoke "$old_port/udp"
        fi
        log_ok "inbound $SS2022_REMARK обновлён (id=$id)"
    else
        log_info "inbound $SS2022_REMARK уже настроен (id=$id)"
    fi
fi

# inbounds/update поле enable не меняет (v3.9.0) — включаем отдельно
if [ "$(xui_inbound_get "$id" | jq -r '.enable')" != "true" ]; then
    xui_inbound_set_enable "$id" true || die "не удалось включить inbound id=$id"
    log_ok "inbound $SS2022_REMARK включён"
fi

fw_allow "$SS_PORT/tcp" "vpn-zoo ss2022"
fw_allow "$SS_PORT/udp" "vpn-zoo ss2022"

# ------------------------------------------------------------
# 3. Пользователь owner
# ------------------------------------------------------------

proto_ss2022_user_add owner || die "не удалось добавить owner в SS-2022"

# ------------------------------------------------------------
# 4. Самопроверка: Xray жив, порт слушает tcp и udp
# ------------------------------------------------------------

# Xray, упавший на прошлом конфиге (порт был занят), панель сама не поднимает
xui_xray_ensure_running 30 || die "Xray не запущен — SS-2022 не заработает"
wait_port "$SS_PORT" tcp 40 || die "SS-2022 не слушает $SS_PORT/tcp (Xray: $(xui_xray_state), $(xui_server_status | jq -r '.xray.errorMsg'))"
wait_port "$SS_PORT" udp 20 || die "SS-2022 не слушает $SS_PORT/udp"
[ "$(xui_xray_state)" = "running" ] || die "Xray не запущен: $(xui_server_status | jq -r '.xray.errorMsg')"
[ -n "$(proto_ss2022_links owner)" ] || die "не удалось собрать ссылку ss:// для owner"

proto_ss2022_manifest_refresh || die "не удалось записать манифест $SS2022_ID"
mark_owned ss2022

log_ok "Shadowsocks-2022 готов: $SERVER_IP:$SS_PORT (tcp+udp), ссылка owner — $ZOO_CLIENTS_DIR/owner/ss2022.txt"
