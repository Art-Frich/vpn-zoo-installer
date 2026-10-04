#!/usr/bin/env bash
# 04d-tuic.sh — TUIC v5 нативным сервером 3x-ui v3.9.0 (флаг ENABLE_TUIC, по умолчанию 0, D5).
# Случайный высокий UDP-порт (TUIC_PORT), самоподписанный сертификат в /etc/vpn-setup/tuic/
# (SAN = IP сервера, опционально TUIC_SNI), пользователь owner.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-tuic.sh"
config_load

[ -n "${SERVER_IP:-}" ] || die "SERVER_IP не задан (config.env)"
xui_wait_api 60 || die "API 3x-ui не отвечает — сначала фаза 03-3xui"

if [ "${ENABLE_TUIC:-0}" != "1" ]; then
    TUIC_PORT="$(config_get TUIC_PORT)"
    proto_tuic_disable
    log_ok "ENABLE_TUIC=$ENABLE_TUIC: TUIC выключен, порт закрыт"
    exit 0
fi

panel_ver="$(xui_panel_version)"
case "$panel_ver" in
    3.9.*) ;;
    *) die "TUIC проверен только на нативном сервере 3x-ui 3.9.x, а установлена $panel_ver" ;;
esac

# ------------------------------------------------------------
# 1. Порт и SNI
# ------------------------------------------------------------

if config_has TUIC_PORT; then
    TUIC_PORT="$(config_get TUIC_PORT)"
fi
if [ -n "${TUIC_PORT:-}" ] && { ! [[ "$TUIC_PORT" =~ ^[0-9]+$ ]] || [ "$TUIC_PORT" -lt 1024 ] || [ "$TUIC_PORT" -gt 65535 ] || is_banned_port "$TUIC_PORT"; }; then
    die "TUIC_PORT=$TUIC_PORT недопустим (1024..65535, не из $BANNED_PORTS)"
fi
if ! config_has TUIC_PORT; then
    port="${TUIC_PORT:-}"
    if [ -z "$port" ]; then
        port="$(rand_port)" || die "не удалось подобрать порт для TUIC"
    fi
    config_set TUIC_PORT "$port"
fi
config_default TUIC_SNI "${TUIC_SNI:-}"
if [ -n "$TUIC_SNI" ] && ! [[ "$TUIC_SNI" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; then
    die "TUIC_SNI=$TUIC_SNI — не похоже на доменное имя"
fi

# ------------------------------------------------------------
# 2. Сертификат
# ------------------------------------------------------------

guard_foreign_install tuic "$TUIC_DIR"
if proto_tuic_cert_stale; then
    [ -e "$TUIC_CERT" ] && backup_path "$TUIC_DIR" >/dev/null
    proto_tuic_cert_gen || die "не удалось создать сертификат TUIC"
    log_ok "сертификат TUIC создан: $TUIC_CERT (sha256 $(proto_tuic_cert_sha256))"
else
    log_info "сертификат TUIC актуален: $TUIC_CERT"
fi
# каталог теперь наш: повторный запуск после сбоя ниже не должен упираться в guard
mark_owned tuic

# ------------------------------------------------------------
# 3. Inbound
# ------------------------------------------------------------

want="$(proto_tuic_inbound_body)"
id="$(proto_tuic_inbound_id)"
cmp='{port, enable, remark, s: (.settings.server | {certificate, private_key, congestion_control, alpn, udp_relay_mode, zero_rtt_handshake, sni})}'
# Новый порт свободен (занятый чужим процессом не даст подняться TUIC-серверу панели)
tuic_port_free() {
    local other busy
    other="$(xui_inbound_find_by_port "$TUIC_PORT")"
    [ -z "$other" ] || die "порт $TUIC_PORT уже занят inbound id=$other в 3x-ui — задайте другой TUIC_PORT в config.env"
    busy="$(ss -Hlunp "sport = :$TUIC_PORT" 2>/dev/null | head -1)"
    [ -z "$busy" ] || die "порт $TUIC_PORT/udp уже занят другим процессом: $busy"
}
if [ -z "$id" ]; then
    tuic_port_free
    id="$(xui_inbound_add "$want")" || die "3x-ui не создал inbound TUIC"
    [[ "$id" =~ ^[0-9]+$ ]] || die "3x-ui вернул странный id inbound: $id"
    log_ok "inbound $TUIC_REMARK создан (id=$id, порт $TUIC_PORT/udp)"
else
    cur="$(xui_inbound_get "$id")"
    if [ "$(jq -c "$cmp" <<< "$cur")" != "$(jq -c "$cmp" <<< "$want")" ]; then
        old_port="$(jq -r '.port' <<< "$cur")"
        [ "$old_port" = "$TUIC_PORT" ] || tuic_port_free
        xui_inbound_update "$id" "$want" || die "3x-ui не обновил inbound TUIC (id=$id)"
        [ "$old_port" = "$TUIC_PORT" ] || fw_revoke "$old_port/udp"
        log_ok "inbound $TUIC_REMARK обновлён (id=$id)"
    else
        log_info "inbound $TUIC_REMARK уже настроен (id=$id)"
    fi
fi

# inbounds/update поле enable не меняет (v3.9.0) — включаем отдельно
if [ "$(xui_inbound_get "$id" | jq -r '.enable')" != "true" ]; then
    xui_inbound_set_enable "$id" true || die "не удалось включить inbound id=$id"
    log_ok "inbound $TUIC_REMARK включён"
fi

fw_allow "$TUIC_PORT/udp" "vpn-zoo tuic"

proto_tuic_user_add owner || die "не удалось добавить owner в TUIC"

# SOCKS-мост TUIC→Xray панель добавляет в конфиг Xray только при его генерации:
# перезапускаем Xray, если моста ещё нет
socks="$(proto_tuic_socks_port "$id")"
if [ -z "$(ss -Hltn "sport = :$socks" 2>/dev/null)" ]; then
    xui_xray_restart || die "не удалось перезапустить Xray"
fi

# ------------------------------------------------------------
# 4. Самопроверка
# ------------------------------------------------------------

wait_port "$TUIC_PORT" udp 40 || die "TUIC не слушает $TUIC_PORT/udp: $(journalctl -u x-ui -n 200 --no-pager 2>/dev/null | grep -i tuic | tail -3)"
listener="$(ss -Hlunp "sport = :$TUIC_PORT" | head -1)"
[[ "$listener" == *'"x-ui"'* ]] || die "$TUIC_PORT/udp слушает не x-ui: $listener"
if ! wait_port "$socks" tcp 30; then
    die "нет SOCKS-моста TUIC→Xray на 127.0.0.1:$socks — трафик TUIC никуда не уйдёт"
fi
[[ "$(ss -Hltn "sport = :$socks")" == *"127.0.0.1:$socks"* ]] || die "SOCKS-мост TUIC слушает не только 127.0.0.1"
[ "$(xui_xray_state)" = "running" ] || die "Xray не запущен: $(xui_server_status | jq -r '.xray.errorMsg')"
[ -n "$(proto_tuic_links owner)" ] || die "не удалось собрать ссылку tuic:// для owner"

proto_tuic_manifest_refresh || die "не удалось записать манифест $TUIC_ID"

log_ok "TUIC v5 готов: $SERVER_IP:$TUIC_PORT/udp, ссылка owner — $ZOO_CLIENTS_DIR/owner/tuic.txt"
log_info "сертификат самоподписанный: в ссылке allow_insecure=1, пин sha256 — в манифесте $(manifest_path "$TUIC_ID")"
