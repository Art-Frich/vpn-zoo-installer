#!/usr/bin/env bash
# lib/proto-ss2022.sh — Shadowsocks-2022 (2022-blake3-aes-128-gcm, tcp+udp) в 3x-ui.
# Функции для фазы 04c, zoo и 99. source'ится после lib.sh и lib/xui.sh:
#   . "$SCRIPTS_DIR/lib/proto-ss2022.sh"
# Нужны config_load (SERVER_IP, SS_PORT, SS_PSK, PANEL_*) и работающий API 3x-ui.
#
# Многопользовательский SS-2022: у inbound ключ сервера (SS_PSK), у каждого клиента
# свой ключ (поле password клиента 3x-ui, панель генерирует его при attach).
# Клиент подключается паролем «ключ_сервера:ключ_клиента».

# Пользователи: xui_user_attach/detach/... из lib/xui.sh, каталоги — zoo_client_* из lib.sh

SS2022_ID="ss2022"
SS2022_REMARK="zoo-ss2022"
SS2022_METHOD="2022-blake3-aes-128-gcm"

# id inbound: по remark, затем по порту (inbound мог переименовать человек в панели)
proto_ss2022_inbound_id() {
    local id
    id="$(xui_inbound_find_by_remark "$SS2022_REMARK")"
    [ -n "$id" ] || id="$(xui_inbound_find_by_port "${SS_PORT:-0}" shadowsocks)"
    printf '%s\n' "$id"
}

_ss2022_ib() {
    local id
    id="$(proto_ss2022_inbound_id)"
    [ -n "$id" ] || { xui_inbound_missing ss2022 "inbound $SS2022_REMARK не найден (фаза 04c не выполнена?)"; return 1; }
    printf '%s\n' "$id"
}

# Тело inbounds/add|update. Клиентов в теле нет: ими управляют clients/*
proto_ss2022_inbound_body() {
    jq -cn --arg r "$SS2022_REMARK" --argjson port "$SS_PORT" --arg m "$SS2022_METHOD" --arg p "$SS_PSK" '{
        remark: $r, enable: true, listen: "", port: $port, protocol: "shadowsocks",
        expiryTime: 0, total: 0,
        settings: {method: $m, password: $p, network: "tcp,udp", clients: []},
        streamSettings: {network: "tcp", security: "none", tcpSettings: {header: {type: "none"}}},
        sniffing: {enabled: true, destOverride: ["http", "tls", "quic"], routeOnly: true}
    }'
}

# Ключ клиента (16 байт base64) или пусто
_ss2022_user_key() {
    xui_client_get "$1" 2>/dev/null | jq -r '.client.password // empty'
}

# Манифест после операций с пользователями (если фаза его уже писала); SS2022_NO_REFRESH=1 — нет
_ss2022_refresh() {
    [ "${SS2022_NO_REFRESH:-0}" != "1" ] && [ -f "$(manifest_path "$SS2022_ID")" ] || return 0
    proto_ss2022_manifest_refresh
}

proto_ss2022_user_add() {
    local name="${1:-}" id
    zoo_user_require "$name"
    id="$(_ss2022_ib)" || return 1
    xui_user_attach "$name" "$id" || { log_err "ss2022: не удалось добавить $name"; return 1; }
    [ -n "$(_ss2022_user_key "$name")" ] || { log_err "ss2022: панель не выдала ключ пользователю $name"; return 1; }
    _ss2022_user_files "$name"
    _ss2022_refresh
}

proto_ss2022_user_del() {
    local name="${1:-}" id
    zoo_user_require "$name"
    id="$(_ss2022_ib)" || return 1
    xui_user_detach "$name" "$id" || { log_err "ss2022: не удалось удалить $name"; return 1; }
    zoo_client_file_del "$name" ss2022.txt
    _ss2022_refresh
}

# Выключение действует на клиента 3x-ui целиком (все протоколы Xray этого пользователя)
proto_ss2022_user_enable() {
    zoo_user_require "${1:-}"
    xui_user_set_enable "$1" "${2:-true}" || return 1
    _ss2022_refresh
}

# «имя<TAB>true|false» по строке
proto_ss2022_user_list() {
    local id
    id="$(_ss2022_ib)" || return 1
    xui_inbound_user_list "$id"
}

# Remark в ссылке: <LABEL>-ss2022-<имя>
_ss2022_remark() { printf '%s-ss2022-%s' "${LABEL:-vpn}" "$1"; }

# ss:// по SIP002/SIP022: userinfo без base64, «метод:ключ_сервера:ключ_клиента»
# с percent-encoding ключей (так же формирует 3x-ui v3.9.0)
proto_ss2022_links() {
    local name="${1:-owner}" id st
    zoo_user_require "$name"
    id="$(_ss2022_ib)" || return 1
    st="$(xui_inbound_get "$id")" || return 1
    jq -r --arg u "$name" --arg host "${SERVER_IP:?}" --arg rem "$(_ss2022_remark "$name")" '
        ((.settings.clients // [])[] | select(.email == $u) | .password) as $k
        | "ss://\(.settings.method):\(.settings.password|@uri):\($k|@uri)@"
          + (if ($host|test(":")) then "[\($host)]" else $host end)
          + ":\(.port)#\($rem|@uri)"' <<< "$st"
}

# probe для манифеста: готовый outbound Xray
proto_ss2022_probe() {
    local name="${1:-owner}" id
    zoo_user_require "$name"
    id="$(_ss2022_ib)" || return 1
    xui_inbound_get "$id" | jq -c --arg u "$name" --arg host "${SERVER_IP:?}" '
        ((.settings.clients // [])[] | select(.email == $u) | .password) as $k
        | {kind: "xray", user: $u, outbound: {
            tag: "proxy", protocol: "shadowsocks",
            settings: {servers: [{address: $host, port: .port, method: .settings.method,
                                  password: "\(.settings.password):\($k)"}]},
            streamSettings: {network: "tcp"}}}'
}

_ss2022_user_files() {
    local link
    link="$(proto_ss2022_links "$1")" || return 1
    [ -n "$link" ] || return 0
    printf '%s\n' "$link" | zoo_client_file_write "$1" ss2022.txt
}

proto_ss2022_traffic() {
    local id
    id="$(_ss2022_ib)" || return 1
    xui_inbound_traffic "$id"
}

# Манифест из текущего состояния панели (ARCHITECTURE §4)
proto_ss2022_manifest_refresh() {
    local id ib users states en u links="[]" link probe="{}" enabled
    id="$(_ss2022_ib)" || return 1
    ib="$(xui_inbound_get "$id")" || return 1
    # «имя<TAB>true|false»: enable клиента общий для всех Xray-протоколов (D18)
    states="$(xui_inbound_user_list "$id")"
    users="$(cut -f1 <<< "$states")"
    for u in $users; do
        zoo_user_valid "$u" || continue
        link="$(proto_ss2022_links "$u")" || continue
        [ -n "$link" ] || continue
        en="$(awk -F'\t' -v u="$u" '$1 == u {print $2; exit}' <<< "$states")"
        links="$(jq -c --arg u "$u" --arg l "$link" --arg en "$en" \
            '. + [{user: $u, uri: $l, enabled: ($en != "false")}]' <<< "$links")"
        printf '%s\n' "$link" | zoo_client_file_write "$u" ss2022.txt
    done
    if grep -qx owner <<< "$users"; then
        probe="$(proto_ss2022_probe owner)" || return 1
    fi
    enabled="$(jq -r '.enable' <<< "$ib")"
    manifest_write "$SS2022_ID" "$(jq -cn \
        --argjson port "$(jq '.port' <<< "$ib")" --argjson en "$enabled" \
        --argjson links "$links" --argjson probe "$probe" \
        --argjson ibid "$id" --arg tag "$(jq -r '.tag' <<< "$ib")" '{
        id: "ss2022",
        name: "Shadowsocks-2022 (2022-blake3-aes-128-gcm)",
        short: "Shadowsocks-2022",
        layer: "tcp",
        port: $port,
        transports: ["tcp", "udp"],
        engine: "xray",
        service: "x-ui",
        enabled: $en,
        users_backend: "xui",
        xui: {inbound_id: $ibid, tag: $tag},
        links: $links,
        files: [],
        probe: $probe,
        notes: "Запасной протокол без TLS. Голый SS в РФ режут по отпечатку TLS внутри туннеля, держать как резерв. Клиенты: v2rayN/v2rayNG, Happ, Hiddify, sing-box, mihomo (нужна поддержка SS-2022 multi-user, пароль ключ_сервера:ключ_клиента). Порт tcp+udp."
    }')"
}

# Выключить протокол (ENABLE_SS=0 после установки): inbound выключен, порты закрыты
proto_ss2022_disable() {
    local id
    id="$(proto_ss2022_inbound_id)"
    [ -n "$id" ] && xui_inbound_set_enable "$id" false
    if [ -n "${SS_PORT:-}" ]; then
        fw_revoke "$SS_PORT/tcp"
        fw_revoke "$SS_PORT/udp"
    fi
    if [ -f "$(manifest_path "$SS2022_ID")" ]; then
        manifest_write "$SS2022_ID" "$(manifest_get "$SS2022_ID" | jq -c '.enabled = false')"
    fi
}
