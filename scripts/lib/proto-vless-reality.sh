#!/usr/bin/env bash
# lib/proto-vless-reality.sh — VLESS RAW+REALITY+Vision в 3x-ui: пользователи, ссылки,
# probe, манифест. source'ится после lib.sh и lib/xui.sh (и config_load).
#
# Пользователь = клиент 3x-ui с email=<имя>, общий для всех xray-inbound зоопарка
# (XHTTP, SS, TUIC): один uuid и subId. Здесь клиент только привязывается к нашему
# inbound; удаляется целиком, лишь когда не остаётся других привязок.
# Источник правды для порта, SNI, ключей и shortId — сам inbound в панели.

PVR_ID="vless-reality"
PVR_REMARK="zoo-vless-reality"
PVR_FLOW="xtls-rprx-vision"
PVR_FP="chrome"

PVR_NOTES="Клиенты на ядре Xray, совместимые с сервером Xray 26.9.30 (с 26.9.8 REALITY отклоняет ClientHello без X25519MLKEM768): v2rayN ≥7.25, v2rayNG ≥2.2.6, Happ, INCY, Streisand, Throne (ядро Xray), mihomo ≥1.19.31 (Clash Verge Rev, FlClash — только fp=chrome и support-x25519mlkem768=true, импорт через подписку/YAML). НЕ работают: sing-box и всё на нём (SFA/SFI, Hiddify, NekoBox, Karing), Shadowrocket — «подключено, но трафика нет». Отпечаток chrome (допустимы firefox/safari), не randomized/ios/edge."

_pvr_name_ok() { zoo_user_valid "$1"; }

_pvr_client_dir() { printf '%s/%s\n' "$ZOO_CLIENTS_DIR" "$1"; }

# Наш inbound: JSON в PVR_INB, id в PVR_INB_ID. Ищем по remark
_pvr_load_inbound() {
    local id
    id="$(xui_inbound_find_by_remark "$PVR_REMARK")" || return 1
    [ -n "$id" ] || { xui_inbound_missing vless-reality "inbound «$PVR_REMARK» не найден (фаза 04 не выполнена?)"; return 1; }
    PVR_INB="$(xui_inbound_get "$id")" || return 1
    PVR_INB_ID="$id"
}

proto_vless_reality_inbound_id() { _pvr_load_inbound && printf '%s\n' "$PVR_INB_ID"; }

# Адрес для ссылок: IPv6 в []
_pvr_host() {
    local h="${SERVER_IP:-$(config_get SERVER_IP)}"
    [[ "$h" == *:* ]] && h="[$h]"
    printf '%s\n' "$h"
}

# spiderX клиента: свой у каждого, хранится в clients/<имя>/vless-reality.json
_pvr_user_spx() {
    local name="$1" f spx=""
    f="$(_pvr_client_dir "$name")/vless-reality.json"
    [ -f "$f" ] && spx="$(jq -r '.spx // empty' "$f" 2>/dev/null || true)"
    [ -n "$spx" ] || spx="/$(gen_random_hex 8)"
    printf '%s\n' "$spx"
}

# Параметры ссылки/probe из inbound и клиента → JSON {uuid,host,port,sni,pbk,sid,spx,flow,fp,name}
_pvr_params() {
    local name="$1" client uuid spx
    client="$(xui_client_get "$name")" || return 1
    uuid="$(jq -r '.client.uuid' <<< "$client")"
    [ -n "$uuid" ] && [ "$uuid" != "null" ] || { log_err "vless-reality: у клиента $name нет uuid"; return 1; }
    spx="$(_pvr_user_spx "$name")"
    jq -c --arg uuid "$uuid" --arg host "$(_pvr_host)" --arg spx "$spx" --arg name "$name" \
        --arg flow "$PVR_FLOW" --arg fp "$PVR_FP" --arg lbl "${LABEL:-vpn}" '
        .streamSettings.realitySettings as $r
        | {uuid:$uuid, host:$host, port:.port, sni:$r.serverNames[0], pbk:$r.settings.publicKey,
           sid:($r.shortIds[0] // ""), spx:$spx, flow:$flow, fp:$fp, name:$name,
           tag:($lbl + "-vless-" + $name)}' <<< "$PVR_INB"
}

# Каноническая ссылка (стандарт VLESS #716 + подсказка ML-KEM для mihomo)
_pvr_link_from() {
    jq -r '"vless://\(.uuid)@\(.host):\(.port)?encryption=none&type=tcp&headerType=none"
        + "&security=reality&pbk=\(.pbk|@uri)&fp=\(.fp)&sni=\(.sni|@uri)&sid=\(.sid)"
        + "&spx=\(.spx|@uri)&flow=\(.flow)&support-x25519mlkem768=true#\(.tag|@uri)"' <<< "$1"
}

# Xray outbound для пробника и клиентов-конфигов
_pvr_outbound_from() {
    jq -c '{tag:"proxy", protocol:"vless",
        settings:{vnext:[{address:(.host|ltrimstr("[")|rtrimstr("]")), port:.port,
            users:[{id:.uuid, encryption:"none", flow:.flow}]}]},
        streamSettings:{network:"tcp", security:"reality",
            realitySettings:{serverName:.sni, fingerprint:.fp, publicKey:.pbk, shortId:.sid, spiderX:.spx}}}' <<< "$1"
}

# Сохранить креды пользователя (0700/0600): spx и готовая ссылка
_pvr_save_user() {
    local name="$1" params="$2" link
    link="$(_pvr_link_from "$params")"
    jq -n --argjson p "$params" --arg link "$link" '{uuid:$p.uuid, spx:$p.spx, link:$link}' \
        | zoo_client_file_write "$name" vless-reality.json
}

# Привязан ли клиент к нашему inbound
_pvr_attached() { xui_user_attached "$1" "$PVR_INB_ID"; }

# proto_vless_reality_user_add NAME [UUID] — создать или привязать клиента, flow Vision
proto_vless_reality_user_add() {
    local name="$1" uuid="${2:-}" flow params
    _pvr_name_ok "$name" || { log_err "vless-reality: недопустимое имя пользователя: $name"; return 1; }
    _pvr_load_inbound || return 1
    xui_user_attach "$name" "$PVR_INB_ID" "$uuid" || return 1
    # клиента мог создать другой протокол (SS, TUIC, XHTTP) — там панель убирает flow.
    # flow хранится на клиента; в inbound без Vision панель его не применяет (v3.9.0)
    flow="$(xui_client_get "$name" | jq -r '.client.flow // ""')" || return 1
    if [ "$flow" != "$PVR_FLOW" ]; then
        xui_client_update "$name" "{\"flow\":\"$PVR_FLOW\"}" || return 1
    fi
    params="$(_pvr_params "$name")" || return 1
    _pvr_save_user "$name" "$params"
    [ "${PVR_NO_REFRESH:-0}" = "1" ] || proto_vless_reality_manifest_refresh
}

# proto_vless_reality_user_del NAME — отвязать; клиент без других inbound удаляется
proto_vless_reality_user_del() {
    local name="$1"
    _pvr_name_ok "$name" || { log_err "vless-reality: недопустимое имя пользователя: $name"; return 1; }
    _pvr_load_inbound || return 1
    xui_user_detach "$name" "$PVR_INB_ID" || return 1
    zoo_client_file_del "$name" vless-reality.json
    [ "${PVR_NO_REFRESH:-0}" = "1" ] || proto_vless_reality_manifest_refresh
}

# proto_vless_reality_user_enable NAME true|false — действует на все xray-протоколы клиента
proto_vless_reality_user_enable() {
    local name="$1" en="${2:-true}"
    case "$en" in true|false) ;; *) log_err "vless-reality: ожидаю true|false, получено: $en"; return 1 ;; esac
    _pvr_load_inbound || return 1
    _pvr_attached "$name" || { log_err "vless-reality: $name не привязан к inbound"; return 1; }
    xui_user_set_enable "$name" "$en" || return 1
    [ "${PVR_NO_REFRESH:-0}" = "1" ] || proto_vless_reality_manifest_refresh
}

# Пользователи нашего inbound: «имя<TAB>true|false» по строке
proto_vless_reality_user_list() {
    _pvr_load_inbound || return 1
    xui_client_list | jq -r --argjson id "$PVR_INB_ID" \
        '.[] | select(.inboundIds | index($id) != null) | "\(.email)\t\(.enable)"'
}

proto_vless_reality_links() {
    local params
    _pvr_load_inbound || return 1
    _pvr_attached "$1" || { log_err "vless-reality: $1 не привязан к inbound"; return 1; }
    params="$(_pvr_params "$1")" || return 1
    _pvr_link_from "$params"
}

# probe для манифеста: {kind:"xray", user, outbound:{...}, link}
proto_vless_reality_probe() {
    local params
    _pvr_load_inbound || return 1
    _pvr_attached "$1" || { log_err "vless-reality: $1 не привязан к inbound"; return 1; }
    params="$(_pvr_params "$1")" || return 1
    jq -cn --arg user "$1" --argjson ob "$(_pvr_outbound_from "$params")" --arg link "$(_pvr_link_from "$params")" \
        '{kind:"xray", user:$user, outbound:$ob, link:$link}'
}

# Трафик: счётчики 3x-ui ведутся на клиента, а не на пару клиент×inbound, поэтому
# up/down — сумма по всем xray-протоколам пользователя (shared:true)
proto_vless_reality_traffic() {
    _pvr_load_inbound || return 1
    xui_client_list | jq -c --argjson id "$PVR_INB_ID" \
        '.[] | select(.inboundIds | index($id) != null)
         | {user:.email, up:(.traffic.up // 0), down:(.traffic.down // 0), shared:true}'
}

# Трафик inbound целиком: {up, down}
proto_vless_reality_traffic_inbound() {
    _pvr_load_inbound || return 1
    jq -c '{up:(.up // 0), down:(.down // 0)}' <<< "$PVR_INB"
}

# Переписать манифест из текущего состояния панели
proto_vless_reality_manifest_refresh() {
    local users name en params links="[]" probe="null" probe_user="" sni
    _pvr_load_inbound || return 1
    users="$(xui_client_list | jq -c --argjson id "$PVR_INB_ID" \
        '[.[] | select(.inboundIds | index($id) != null) | {email, enable}]')"
    while IFS=$'\t' read -r name en; do
        [ -n "$name" ] || continue
        params="$(_pvr_params "$name")" || continue
        _pvr_save_user "$name" "$params"
        links="$(jq -c --arg u "$name" --arg uri "$(_pvr_link_from "$params")" --argjson en "$en" \
            '. + [{user:$u, uri:$uri, enabled:$en}]' <<< "$links")"
    done < <(jq -r '.[] | "\(.email)\t\(.enable)"' <<< "$users")
    # probe — для owner, иначе для первого включённого
    probe_user="$(jq -r 'if any(.[]; .email == "owner") then "owner"
        else ([.[] | select(.enable)][0].email // "") end' <<< "$users")"
    if [ -n "$probe_user" ]; then
        probe="$(proto_vless_reality_probe "$probe_user")" || probe="null"
    fi
    sni="$(jq -r '.streamSettings.realitySettings.serverNames[0]' <<< "$PVR_INB")"
    manifest_write "$PVR_ID" "$(jq -cn --argjson inb "$PVR_INB" --argjson links "$links" --argjson probe "$probe" \
        --arg notes "$PVR_NOTES" --arg sni "$sni" '{
        id:"vless-reality", name:"VLESS + REALITY + Vision", layer:"tcp", port:$inb.port,
        phase:"04-vless-reality", enable_var:"ENABLE_VLESS",
        engine:"xray", service:"x-ui", enabled:$inb.enable, users_backend:"xui",
        xui_inbound_id:$inb.id, xui_tag:$inb.tag, sni:$sni,
        target:$inb.streamSettings.realitySettings.target,
        links:$links, files:[], probe:$probe, notes:$notes}')"
}
