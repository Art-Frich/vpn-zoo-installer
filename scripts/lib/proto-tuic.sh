#!/usr/bin/env bash
# lib/proto-tuic.sh — TUIC v5 нативным сервером 3x-ui v3.9.0 (protocol "tuic").
# Функции для фазы 04d, zoo и 99. source'ится после lib.sh и lib/xui.sh.
#
# Как устроено в 3x-ui v3.9.0: QUIC-сервер TUIC живёт в процессе x-ui (не в Xray), слушает
# TUIC_PORT/udp и отдаёт расшифрованный трафик в Xray через SOCKS-inbound на
# 127.0.0.1:(64001 + (id-1) % N) с тем же тегом, что у inbound (маршрутизация Xray по
# inboundTag работает). Сертификат — самоподписанный, клиенту нужен пин или allow_insecure.
# Учёт трафика по клиентам — задачей панели (tuic_job), с известными багами (#6714).

# Пользователи: xui_user_attach/detach/... из lib/xui.sh, каталоги — zoo_client_* из lib.sh

TUIC_ID="tuic"
TUIC_REMARK="zoo-tuic"
TUIC_DIR="${TUIC_DIR:-$VPN_ETC/tuic}"
TUIC_CERT="$TUIC_DIR/cert.pem"
TUIC_KEY="$TUIC_DIR/key.pem"
TUIC_SOCKS_BASE=64000

proto_tuic_inbound_id() {
    local id
    id="$(xui_inbound_find_by_remark "$TUIC_REMARK")"
    [ -n "$id" ] || id="$(xui_inbound_find_by_port "${TUIC_PORT:-0}" tuic)"
    printf '%s\n' "$id"
}

_tuic_ib() {
    local id
    id="$(proto_tuic_inbound_id)"
    [ -n "$id" ] || { xui_inbound_missing tuic "inbound $TUIC_REMARK не найден (фаза 04d не выполнена?)"; return 1; }
    printf '%s\n' "$id"
}

# Порт SOCKS-моста к Xray. Слотов в v3.9.0 1000 (64001..65000); при id > 1000 формула
# может разойтись с панелью — тогда проверка в фазе просто предупредит
proto_tuic_socks_port() { printf '%s\n' $(( TUIC_SOCKS_BASE + 1 + ($1 - 1) % 1000 )); }

# Имя в сертификате и в SNI: TUIC_SNI, если задан, иначе IP (в ClientHello SNI тогда нет)
_tuic_server_name() { printf '%s\n' "${TUIC_SNI:-${SERVER_IP:?}}"; }

# Нужен ли новый сертификат: нет файлов, не тот SAN или истекает в течение 30 дней
proto_tuic_cert_stale() {
    [ -s "$TUIC_CERT" ] && [ -s "$TUIC_KEY" ] || return 0
    openssl x509 -in "$TUIC_CERT" -noout -checkend $((30*86400)) >/dev/null 2>&1 || return 0
    local san
    san="$(openssl x509 -in "$TUIC_CERT" -noout -ext subjectAltName 2>/dev/null | tail -n +2)"
    [[ "$san" == *"IP Address:${SERVER_IP:?}"* ]] || return 0
    if [ -n "${TUIC_SNI:-}" ]; then
        [[ "$san" == *"DNS:${TUIC_SNI}"* ]] || return 0
    fi
    return 1
}

# Самоподписанный ECDSA P-256 на 10 лет: SAN = IP сервера (+ TUIC_SNI)
proto_tuic_cert_gen() {
    local san="IP:${SERVER_IP:?}" tmpc tmpk
    [ -n "${TUIC_SNI:-}" ] && san="DNS:${TUIC_SNI},$san"
    ( umask 077; mkdir -p "$TUIC_DIR" )
    chmod 700 "$TUIC_DIR"
    tmpc="$(mktemp "$TUIC_DIR/.cert.XXXXXX")"; tmpk="$(mktemp "$TUIC_DIR/.key.XXXXXX")"
    if ! openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
            -subj "/CN=$(_tuic_server_name)" -addext "subjectAltName=$san" \
            -keyout "$tmpk" -out "$tmpc" >/dev/null 2>&1; then
        rm -f "$tmpc" "$tmpk"
        log_err "tuic: openssl не создал сертификат"
        return 1
    fi
    chmod 600 "$tmpc" "$tmpk"
    mv -f "$tmpk" "$TUIC_KEY"
    mv -f "$tmpc" "$TUIC_CERT"
}

# sha256 сертификата (DER, hex) — для пина в mihomo (fingerprint) и сверки руками
proto_tuic_cert_sha256() {
    openssl x509 -in "$TUIC_CERT" -outform der 2>/dev/null | sha256sum | awk '{print $1}'
}

# sha256 публичного ключа (SPKI DER, base64) — tls.certificate_public_key_sha256 в sing-box ≥1.13
proto_tuic_pubkey_sha256() {
    openssl x509 -in "$TUIC_CERT" -noout -pubkey 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null \
        | openssl dgst -sha256 -binary | base64
}

proto_tuic_inbound_body() {
    jq -cn --arg r "$TUIC_REMARK" --argjson port "$TUIC_PORT" --arg c "$TUIC_CERT" --arg k "$TUIC_KEY" \
        --arg sni "${TUIC_SNI:-}" '{
        remark: $r, enable: true, listen: "", port: $port, protocol: "tuic",
        expiryTime: 0, total: 0,
        settings: {server: {certificate: $c, private_key: $k, congestion_control: "bbr", alpn: ["h3"],
                            udp_relay_mode: "native", zero_rtt_handshake: false, log_level: "warn",
                            max_idle_time: 15, authentication_timeout: 3, max_udp_relay_packet_size: 1500,
                            sni: $sni},
                   clients: []},
        streamSettings: {},
        sniffing: {enabled: true, destOverride: ["http", "tls", "quic"], routeOnly: true}
    }'
}

# {uuid, password, enable} пользователя в inbound или пусто
_tuic_user() {
    xui_inbound_get "$1" | jq -c --arg u "$2" \
        '[(.settings.clients // [])[] | select(.email == $u) | {uuid: (.uuid // .id), password, enable}][0] // empty'
}

# Манифест после операций с пользователями (если фаза его уже писала); TUIC_NO_REFRESH=1 — нет
_tuic_refresh() {
    [ "${TUIC_NO_REFRESH:-0}" != "1" ] && [ -f "$(manifest_path "$TUIC_ID")" ] || return 0
    proto_tuic_manifest_refresh
}

proto_tuic_user_add() {
    local name="${1:-}" id
    zoo_user_require "$name"
    id="$(_tuic_ib)" || return 1
    xui_user_attach "$name" "$id" || { log_err "tuic: не удалось добавить $name"; return 1; }
    [ -n "$(_tuic_user "$id" "$name" | jq -r '.uuid // empty')" ] \
        || { log_err "tuic: у $name нет uuid/пароля в inbound"; return 1; }
    _tuic_user_files "$name"
    _tuic_refresh
}

proto_tuic_user_del() {
    local name="${1:-}" id
    zoo_user_require "$name"
    id="$(_tuic_ib)" || return 1
    xui_user_detach "$name" "$id" || { log_err "tuic: не удалось удалить $name"; return 1; }
    zoo_client_file_del "$name" tuic.txt
    _tuic_refresh
}

# Флаг общий для всех протоколов Xray этого пользователя (клиент 3x-ui один)
proto_tuic_user_enable() {
    zoo_user_require "${1:-}"
    xui_user_set_enable "$1" "${2:-true}" || return 1
    _tuic_refresh
}

# «имя<TAB>true|false» по строке
proto_tuic_user_list() {
    local id
    id="$(_tuic_ib)" || return 1
    xui_inbound_user_list "$id"
}

_tuic_remark() { printf '%s-tuic-%s' "${LABEL:-vpn}" "$1"; }

# tuic://uuid:password@host:port?... Сертификат самоподписанный, поэтому allow_insecure=1
# (так понимают sing-box/Hiddify/Karing/NekoBox/v2rayN). Ссылка из панели (allow_insecure=0)
# с ним не заработает
proto_tuic_links() {
    local name="${1:-owner}" id u port
    zoo_user_require "$name"
    id="$(_tuic_ib)" || return 1
    u="$(_tuic_user "$id" "$name")"
    [ -n "$u" ] || return 0
    port="$(xui_inbound_get "$id" | jq -r '.port')"
    jq -rn --argjson u "$u" --arg host "${SERVER_IP:?}" --arg port "$port" --arg sni "${TUIC_SNI:-}" \
        --arg rem "$(_tuic_remark "$name")" '
        "tuic://\($u.uuid|@uri):\($u.password|@uri)@"
        + (if ($host|test(":")) then "[\($host)]" else $host end) + ":\($port)"
        + "?congestion_control=bbr&udp_relay_mode=native&alpn=h3&allow_insecure=1"
        + (if $sni != "" then "&sni=\($sni|@uri)" else "" end)
        + "#\($rem|@uri)"'
}

# probe: outbound sing-box (Xray не умеет TUIC-клиент). Сертификат закреплён целиком
# (tls.certificate), без insecure
proto_tuic_probe() {
    local name="${1:-owner}" id u port
    zoo_user_require "$name"
    id="$(_tuic_ib)" || return 1
    u="$(_tuic_user "$id" "$name")"
    [ -n "$u" ] || { log_err "tuic: $name не привязан к inbound"; return 1; }
    port="$(xui_inbound_get "$id" | jq -r '.port')"
    jq -cn --argjson u "$u" --arg user "$name" --arg host "${SERVER_IP:?}" --argjson port "$port" \
        --arg sn "$(_tuic_server_name)" --rawfile pem "$TUIC_CERT" --arg pk "$(proto_tuic_pubkey_sha256)" '{
        kind: "sing-box", user: $user,
        outbound: {type: "tuic", tag: "proxy", server: $host, server_port: $port,
                   uuid: $u.uuid, password: $u.password,
                   congestion_control: "bbr", udp_relay_mode: "native", zero_rtt_handshake: false,
                   tls: {enabled: true, server_name: $sn, alpn: ["h3"], certificate: $pem}},
        certificate_public_key_sha256: $pk}'
}

_tuic_user_files() {
    local link
    link="$(proto_tuic_links "$1")" || return 1
    [ -n "$link" ] || return 0
    printf '%s\n' "$link" | zoo_client_file_write "$1" tuic.txt
}

proto_tuic_traffic() {
    local id
    id="$(_tuic_ib)" || return 1
    xui_inbound_traffic "$id"
}

proto_tuic_manifest_refresh() {
    local id ib users states en u links="[]" link probe="{}"
    id="$(_tuic_ib)" || return 1
    ib="$(xui_inbound_get "$id")" || return 1
    # «имя<TAB>true|false»: enable клиента общий для всех Xray-протоколов (D18)
    states="$(xui_inbound_user_list "$id")"
    users="$(cut -f1 <<< "$states")"
    for u in $users; do
        zoo_user_valid "$u" || continue
        link="$(proto_tuic_links "$u")" || continue
        [ -n "$link" ] || continue
        en="$(awk -F'\t' -v u="$u" '$1 == u {print $2; exit}' <<< "$states")"
        links="$(jq -c --arg u "$u" --arg l "$link" --arg en "$en" \
            '. + [{user: $u, uri: $l, enabled: ($en != "false")}]' <<< "$links")"
        printf '%s\n' "$link" | zoo_client_file_write "$u" tuic.txt
    done
    if grep -qx owner <<< "$users"; then
        probe="$(proto_tuic_probe owner)" || return 1
    fi
    manifest_write "$TUIC_ID" "$(jq -cn \
        --argjson port "$(jq '.port' <<< "$ib")" --argjson en "$(jq '.enable' <<< "$ib")" \
        --argjson links "$links" --argjson probe "$probe" --argjson ibid "$id" \
        --arg tag "$(jq -r '.tag' <<< "$ib")" --arg sha "$(proto_tuic_cert_sha256)" \
        --arg pk "$(proto_tuic_pubkey_sha256)" --arg cert "$TUIC_CERT" \
        --argjson socks "$(proto_tuic_socks_port "$id")" '{
        id: "tuic",
        phase: "04d-tuic",
        enable_var: "ENABLE_TUIC",
        name: "TUIC v5 (3x-ui native)",
        layer: "udp",
        port: $port,
        engine: "x-ui",
        service: "x-ui",
        enabled: $en,
        users_backend: "xui",
        xui: {inbound_id: $ibid, tag: $tag, socks_relay: $socks},
        tls: {self_signed: true, cert_path: $cert, cert_sha256: $sha, pubkey_sha256: $pk},
        links: $links,
        files: [],
        probe: $probe,
        notes: "QUIC (UDP), самоподписанный сертификат: в ссылке allow_insecure=1. Пин для ручной настройки: sha256 сертификата \($sha) (mihomo: fingerprint), sha256 ключа \($pk) (sing-box: certificate_public_key_sha256). Клиенты: sing-box/SFA/SFI, Hiddify, Karing, NekoBox, v2rayN (ядро sing-box), mihomo. v2rayNG и Xray-клиенты TUIC не умеют. Против ТСПУ ничего не добавляет к Hy2."
    }')"
}

# Выключить протокол после установки: inbound выключен, порт закрыт
proto_tuic_disable() {
    local id
    id="$(proto_tuic_inbound_id)"
    [ -n "$id" ] && xui_inbound_set_enable "$id" false
    [ -z "${TUIC_PORT:-}" ] || fw_revoke "$TUIC_PORT/udp"
    if [ -f "$(manifest_path "$TUIC_ID")" ]; then
        manifest_write "$TUIC_ID" "$(manifest_get "$TUIC_ID" | jq -c '.enabled = false')"
    fi
}
