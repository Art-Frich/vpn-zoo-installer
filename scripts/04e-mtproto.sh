#!/usr/bin/env bash
# 04e-mtproto.sh — MTProxy для Telegram (MTProto Fake-TLS) нативным inbound 3x-ui v3.9.0 (D63).
# Флаг ENABLE_MTPROTO, по умолчанию 0: включается переключателем в админке или ENABLE_MTPROTO=1.
# Режим «Только Telegram»: человеку не нужно VPN-приложение — ссылка tg://proxy открывается в
# Telegram, остальное на телефоне идёт напрямую. Случайный высокий TCP-порт (MTPROTO_PORT: 443
# занят REALITY), домен Fake-TLS (MTPROTO_DOMAIN) — тем же валидатором, что REALITY target (D10),
# но не тот, что у VLESS и XHTTP. Секрет у каждого пользователя свой.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/reality-target.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-mtproto.sh"
config_load
detect_arch >/dev/null

[ -n "${SERVER_IP:-}" ] || die "SERVER_IP не задан (config.env)"
xui_wait_api 60 || die "API 3x-ui не отвечает — сначала фаза 03-3xui"

if [ "${ENABLE_MTPROTO:-0}" != "1" ]; then
    MTPROTO_PORT="$(config_get MTPROTO_PORT)"
    proto_mtproto_disable
    log_ok "ENABLE_MTPROTO=${ENABLE_MTPROTO:-0}: MTProxy выключен, порт закрыт"
    exit 0
fi

panel_ver="$(xui_panel_version)"
case "$panel_ver" in
    3.9.*) ;;
    *) die "MTProxy проверен только на inbound mtproto 3x-ui 3.9.x, а установлена $panel_ver" ;;
esac
[ -x "$(proto_mtproto_bin)" ] || die "нет $(proto_mtproto_bin): панель 3x-ui поставлена без mtg — переустановите фазу 03-3xui"

# ------------------------------------------------------------
# 1. Порт и домен Fake-TLS
# ------------------------------------------------------------

if config_has MTPROTO_PORT; then
    MTPROTO_PORT="$(config_get MTPROTO_PORT)"
fi
if [ -n "${MTPROTO_PORT:-}" ] && { ! [[ "$MTPROTO_PORT" =~ ^[0-9]+$ ]] || [ "$MTPROTO_PORT" -lt 1024 ] || [ "$MTPROTO_PORT" -gt 65535 ] || is_banned_port "$MTPROTO_PORT"; }; then
    die "MTPROTO_PORT=$MTPROTO_PORT недопустим (1024..65535, не из $BANNED_PORTS)"
fi
if ! config_has MTPROTO_PORT; then
    port="${MTPROTO_PORT:-}"
    [ -n "$port" ] || port="$(rand_port)" || die "не удалось подобрать порт для MTProxy"
    config_set MTPROTO_PORT "$port"
fi

# Домен — как REALITY target (TLS 1.3, h2, без редиректа, не .ru и не apple/microsoft): mtg
# отдаёт сканеру настоящий TLS этого сайта. Свой, не VLESS_SNI и не XHTTP_SNI — как у XHTTP
if config_has MTPROTO_DOMAIN && [ -n "$(config_get MTPROTO_DOMAIN)" ]; then
    MTPROTO_DOMAIN="$(config_get MTPROTO_DOMAIN)"
    reality_target_check "$MTPROTO_DOMAIN" \
        || log_warn "домен Fake-TLS $MTPROTO_DOMAIN сейчас не проходит проверку ($REALITY_REASON) — сменить: MTPROTO_DOMAIN=... --phase 04e"
elif [ -n "${MTPROTO_DOMAIN:-}" ]; then
    if reality_target_check "$MTPROTO_DOMAIN"; then
        config_set MTPROTO_DOMAIN "$MTPROTO_DOMAIN"
    elif [ "${ZOO_FORCE:-0}" = "1" ]; then
        log_warn "MTPROTO_DOMAIN=$MTPROTO_DOMAIN не прошёл проверку ($REALITY_REASON) — --force, оставляю"
        config_set MTPROTO_DOMAIN "$MTPROTO_DOMAIN"
    else
        die "MTPROTO_DOMAIN=$MTPROTO_DOMAIN не годится для Fake-TLS: $REALITY_REASON. Оставить всё равно: --force"
    fi
else
    picked="$(REALITY_EXCLUDE="$(config_get VLESS_SNI) $(config_get XHTTP_SNI)" reality_target_select)" \
        || die "ни один кандидат домена Fake-TLS не прошёл проверку (нет выхода в интернет?). Задайте свой: MTPROTO_DOMAIN=домен"
    config_set MTPROTO_DOMAIN "$picked"
    log_ok "домен Fake-TLS: $picked"
fi
MTPROTO_DOMAIN="$(config_get MTPROTO_DOMAIN)"

# ------------------------------------------------------------
# 2. Inbound
# ------------------------------------------------------------

want="$(proto_mtproto_inbound_body)"
id="$(proto_mtproto_inbound_id)"
cmp='{port, enable, remark, d: .settings.fakeTlsDomain}'
# Новый порт свободен: занятый чужим процессом не даст подняться mtg
mtproto_port_free() {
    local other busy
    other="$(xui_inbound_find_by_port "$MTPROTO_PORT")"
    [ -z "$other" ] || die "порт $MTPROTO_PORT уже занят inbound id=$other в 3x-ui — задайте другой MTPROTO_PORT в config.env"
    busy="$(ss -Hltnp "sport = :$MTPROTO_PORT" 2>/dev/null | head -1)"
    [ -z "$busy" ] || die "порт $MTPROTO_PORT/tcp уже занят другим процессом: $busy"
}
if [ -z "$id" ]; then
    mtproto_port_free
    id="$(xui_inbound_add "$want")" || die "3x-ui не создал inbound MTProxy"
    [[ "$id" =~ ^[0-9]+$ ]] || die "3x-ui вернул странный id inbound: $id"
    log_ok "inbound $MTPROTO_REMARK создан (id=$id, порт $MTPROTO_PORT/tcp)"
else
    cur="$(xui_inbound_get "$id")"
    if [ "$(jq -c "$cmp" <<< "$cur")" != "$(jq -c "$cmp" <<< "$want")" ]; then
        old_port="$(jq -r '.port' <<< "$cur")"
        [ "$old_port" = "$MTPROTO_PORT" ] || mtproto_port_free
        # update меняет поля inbound, клиентов (и их секреты) не трогает
        xui_inbound_update "$id" "$(jq -c --argjson c "$(jq -c '.settings.clients // []' <<< "$cur")" \
            '.settings.clients = $c' <<< "$want")" || die "3x-ui не обновил inbound MTProxy (id=$id)"
        [ "$old_port" = "$MTPROTO_PORT" ] || fw_revoke "$old_port/tcp"
        log_ok "inbound $MTPROTO_REMARK обновлён (id=$id)"
    else
        log_info "inbound $MTPROTO_REMARK уже настроен (id=$id)"
    fi
fi

# inbounds/update поле enable не меняет (v3.9.0) — включаем отдельно
if [ "$(xui_inbound_get "$id" | jq -r '.enable')" != "true" ]; then
    xui_inbound_set_enable "$id" true || die "не удалось включить inbound id=$id"
    log_ok "inbound $MTPROTO_REMARK включён"
fi

fw_allow "$MTPROTO_PORT/tcp" "vpn-zoo mtproto"

proto_mtproto_user_add owner || die "не удалось добавить owner в MTProxy"

# Домен сменили (MTPROTO_DOMAIN=…): он зашит в секрет каждого клиента — переписываем, середина
# секрета та же. Ссылки у людей всё равно меняются: их нужно разослать заново
while IFS=$'\t' read -r who sec; do
    [ -n "$who" ] && [ -n "$sec" ] || continue
    [ "$(proto_mtproto_secret_domain "$sec")" != "$MTPROTO_DOMAIN" ] || continue
    xui_client_update "$who" "$(jq -cn --arg s "$(proto_mtproto_secret_with_domain "$sec" "$MTPROTO_DOMAIN")" '{secret: $s}')" \
        || die "не удалось сменить домен в секрете $who"
    log_warn "MTProxy: у $who новый домен в секрете — ссылку tg://proxy нужно отправить заново"
done < <(xui_inbound_get "$id" | jq -r '(.settings.clients // [])[] | "\(.email)\t\(.secret // "")"')

# ------------------------------------------------------------
# 3. Самопроверка: слушает mtg, чужому соединению отвечает настоящий сайт
# ------------------------------------------------------------

# mtg запускает задача панели (раз в 10 с), когда у inbound есть включённый клиент
wait_port "$MTPROTO_PORT" tcp 40 || die "MTProxy не слушает $MTPROTO_PORT/tcp: $(journalctl -u x-ui -n 200 --no-pager 2>/dev/null | grep -i mtproto | tail -3)"
listener="$(ss -Hltnp "sport = :$MTPROTO_PORT" | head -1)"
[[ "$listener" == *'"mtg-'* ]] || die "$MTPROTO_PORT/tcp слушает не mtg: $listener"
# соединение без нашего секрета mtg пересылает на сайт домена: TLS с его сертификатом
if timeout 20 openssl s_client -connect "127.0.0.1:$MTPROTO_PORT" -servername "$MTPROTO_DOMAIN" \
        -verify_hostname "$MTPROTO_DOMAIN" -verify_return_error </dev/null >/dev/null 2>&1; then
    log_ok "Fake-TLS: на $MTPROTO_PORT/tcp с SNI $MTPROTO_DOMAIN отвечает настоящий сайт (сертификат верен)"
else
    log_warn "Fake-TLS: на $MTPROTO_PORT/tcp с SNI $MTPROTO_DOMAIN нет TLS настоящего сайта — сканер увидит не сайт. Telegram при этом работает; домен сменить: MTPROTO_DOMAIN=... --phase 04e"
fi
[ -n "$(proto_mtproto_links owner)" ] || die "не удалось собрать ссылку tg://proxy для owner"

proto_mtproto_manifest_refresh || die "не удалось записать манифест $MTPROTO_ID"
mark_owned mtproto

log_ok "MTProxy готов: $SERVER_IP:$MTPROTO_PORT/tcp, Fake-TLS $MTPROTO_DOMAIN, ссылка owner — $ZOO_CLIENTS_DIR/owner/mtproto.txt"
log_info "людям — группа с протоколом MTProxy (мастер «Подключить», вариант «Только Telegram»)"
