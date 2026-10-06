#!/usr/bin/env bash
# lib/proto-vless-xhttp.sh — VLESS + XHTTP + REALITY поверх 3x-ui (фаза 04b, zoo, 99).
# source'ится после lib.sh (и lib/xui.sh) с загруженным config.env. Прямой запуск:
#   bash scripts/lib/proto-vless-xhttp.sh user_add NAME | user_del NAME | user_enable NAME true|false
#        | user_list | links NAME | probe NAME | manifest_refresh | traffic
#
# Размещение (XHTTP_PLACEMENT, обоснование — в шапке scripts/04b-vless-xhttp.sh):
#   port     — отдельный inbound XHTTP+REALITY на XHTTP_PORT, свои ключи и SNI (по умолчанию);
#   fallback — child XHTTP без TLS на 127.0.0.1:XHTTP_PORT за VLESS-inbound фазы 04
#              (RAW+REALITY+Vision, 443): ссылка идёт через порт и REALITY master.
# Пользователи — клиенты 3x-ui (email = имя), общие с остальными Xray-протоколами.
# flow на XHTTP-inbound панель обнуляет сама (Vision поверх XHTTP только с VLESS Encryption).

# Прямой запуск: сначала lib.sh и config.env
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -euo pipefail
    # shellcheck source=../lib.sh
    . "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
    config_load
fi

PROTO_XHTTP_ID="vless-xhttp"

if ! declare -F xui_api >/dev/null; then
    # shellcheck source=xui.sh
    . "$SCRIPTS_DIR/lib/xui.sh"
fi
# shellcheck source=reality-target.sh
. "$(dirname "${BASH_SOURCE[0]}")/reality-target.sh"

# Кандидаты REALITY target — общий список lib/reality-target.sh (фаза 04 берёт из него же,
# но SNI у транспортов разные: 04b исключает VLESS_SNI, 04 — XHTTP_SNI)
XHTTP_SNI_CANDIDATES="${XHTTP_SNI_CANDIDATES:-$REALITY_CANDIDATES}"

_xhttp_valid_name() { zoo_user_valid "$1"; }

# Годится ли HOST в REALITY target: проверки reality_target_check (TLS 1.3, цепочка
# ≤16000 Б, h2, SAN, без редиректа на чужой хост) и обязательно X25519MLKEM768
proto_vless_xhttp_target_ok() {
    reality_target_check "$1" && [ "$REALITY_PQ" = "1" ]
}

# id нашего inbound: vless + network xhttp на XHTTP_PORT; пусто, если нет
# id нашего inbound: vless + network xhttp на XHTTP_PORT, иначе по remark <LABEL>-xhttp
# (XHTTP_PORT сменили — inbound тот же, фаза переносит его на новый порт); пусто, если нет
proto_vless_xhttp_inbound_id() {
    xui_inbound_list | jq -r --arg p "${XHTTP_PORT:-}" --arg r "${LABEL:-vpn}-xhttp" '
        [.[] | select(.protocol == "vless"
                      and ((.streamSettings | if type == "string" then fromjson else . end).network == "xhttp"))] as $x
        | ([$x[] | select((.port | tostring) == $p)][0] // [$x[] | select(.remark == $r)][0]).id // empty'
}

# VLESS RAW+REALITY master фазы 04 (для fallback): id по порту VLESS_PORT (443)
proto_vless_xhttp_master_id() {
    local port="${VLESS_PORT:-443}"
    xui_inbound_list | jq -r --argjson p "$port" '
        [.[] | select(.port == $p and .protocol == "vless")
             | select((.streamSettings | if type == "string" then fromjson else . end)
                      | .network == "tcp" and .security == "reality")][0].id // empty'
}

# Публичный порт: свой (port) или master (fallback)
proto_vless_xhttp_public_port() {
    if [ "${XHTTP_PLACEMENT:-port}" = "fallback" ]; then
        local mid
        mid="$(proto_vless_xhttp_master_id)"
        [ -n "$mid" ] || return 1
        xui_inbound_get "$mid" | jq -r '.port'
    else
        printf '%s\n' "$XHTTP_PORT"
    fi
}

_xhttp_need_inbound() {
    local id
    id="$(proto_vless_xhttp_inbound_id)"
    [ -n "$id" ] || { xui_inbound_missing vless-xhttp "inbound на порту ${XHTTP_PORT:-?} не найден (фаза 04b не выполнена?)"; return 1; }
    printf '%s\n' "$id"
}

# ---------- пользователи ----------

# Манифест после изменений пользователей; XHTTP_NO_REFRESH=1 — пакетные операции
_xhttp_refresh() { [ "${XHTTP_NO_REFRESH:-0}" = "1" ] || proto_vless_xhttp_manifest_refresh; }

# Пользователь — общий клиент 3x-ui (xui_user_attach: uuid, subId, ключ SS-2022 в password,
# flow Vision, который панель на XHTTP сама обнуляет)
proto_vless_xhttp_user_add() {
    local name="$1" id
    _xhttp_valid_name "$name" || { log_err "vless-xhttp: недопустимое имя пользователя: $name"; return 1; }
    id="$(_xhttp_need_inbound)" || return 1
    if xui_user_attached "$name" "$id"; then
        log_info "vless-xhttp: $name уже подключён к inbound $id"
    else
        xui_user_attach "$name" "$id" || return 1
        log_ok "vless-xhttp: $name подключён к inbound $id"
    fi
    _xhttp_store_user "$name" || return 1
    _xhttp_refresh
}

# Ссылка пользователя в /etc/vpn-setup/clients/<name>/vless-xhttp.uri (0600)
_xhttp_store_user() {
    local name="$1" uri
    uri="$(proto_vless_xhttp_links "$name" | head -1)" || return 1
    [ -n "$uri" ] || { log_err "vless-xhttp: панель не вернула ссылку для $name"; return 1; }
    printf '%s\n' "$uri" | zoo_client_file_write "$name" vless-xhttp.uri
}

# Отвязать от XHTTP; если других inbound у клиента нет — удалить клиента целиком
proto_vless_xhttp_user_del() {
    local name="$1" id
    _xhttp_valid_name "$name" || { log_err "vless-xhttp: недопустимое имя пользователя: $name"; return 1; }
    id="$(_xhttp_need_inbound)" || return 1
    xui_user_detach "$name" "$id" || return 1
    zoo_client_file_del "$name" vless-xhttp.uri
    _xhttp_refresh
}

# В 3x-ui enable — свойство клиента, а не пары клиент+inbound: выключает
# пользователя во всех Xray-протоколах сразу
proto_vless_xhttp_user_enable() {
    local name="$1" en="${2:-true}"
    case "$en" in true|false) ;; *) log_err "vless-xhttp: user_enable: ожидаю true|false"; return 1 ;; esac
    xui_client_exists "$name" || { log_err "vless-xhttp: нет клиента $name"; return 1; }
    xui_user_set_enable "$name" "$en" || return 1
    _xhttp_refresh
}

# Строки «имя<TAB>true|false» для клиентов, подключённых к XHTTP-inbound
proto_vless_xhttp_user_list() {
    local id
    id="$(_xhttp_need_inbound)" || return 1
    xui_client_list | jq -r --argjson i "$id" \
        '.[] | select(.inboundIds | index($i) != null) | "\(.email)\t\(.enable)"'
}

# ---------- ссылки и probe ----------

# Ссылки пользователя на XHTTP-inbound (панель сама подставляет порт и REALITY
# master при fallback). Отбор по type=xhttp и нашему path
proto_vless_xhttp_links() {
    local name="$1" path
    path="${XHTTP_PATH#/}"
    xui_client_links "$name" "${SERVER_IP:-}" | jq -r --arg p "path=%2F$path" '
        .[] | select(test("^vless://") and test("[?&]type=xhttp(&|#|$)") and (split("#")[0] | split("?")[1] | split("&") | index($p) != null))'
}

# Значение параметра query из URI (с %XX-декодированием)
_xhttp_uri_param() {
    local uri="$1" key="$2" q kv v
    q="${uri#*\?}"; q="${q%%#*}"
    local IFS='&'
    for kv in $q; do
        if [ "${kv%%=*}" = "$key" ]; then
            v="${kv#*=}"; v="${v//+/ }"
            printf '%b\n' "${v//%/\\x}"
            return 0
        fi
    done
    return 0
}

# Xray outbound из ссылки панели: probe проверяет ровно то, что получит пользователь.
# xmux = клиентские дефолты Xray ≥ v26.7.28, заданные явно для старых ядер:
# задать часть полей нельзя — остальные дефолты тогда пропадают
_xhttp_outbound_from_uri() {
    local uri="$1" body userinfo host port
    body="${uri#vless://}"; body="${body%%\?*}"
    userinfo="${body%%@*}"; host="${body#*@}"
    port="${host##*:}"; host="${host%:*}"; host="${host#[}"; host="${host%]}"
    jq -cn --arg id "$userinfo" --arg host "$host" --argjson port "$port" \
        --arg path "$(_xhttp_uri_param "$uri" path)" --arg mode "$(_xhttp_uri_param "$uri" mode)" \
        --arg sec "$(_xhttp_uri_param "$uri" security)" --arg sni "$(_xhttp_uri_param "$uri" sni)" \
        --arg fp "$(_xhttp_uri_param "$uri" fp)" --arg pbk "$(_xhttp_uri_param "$uri" pbk)" \
        --arg sid "$(_xhttp_uri_param "$uri" sid)" --arg spx "$(_xhttp_uri_param "$uri" spx)" '
        {tag:"proxy", protocol:"vless",
         settings:{vnext:[{address:$host, port:$port, users:[{id:$id, encryption:"none", flow:""}]}]},
         streamSettings:{network:"xhttp", security:$sec,
           realitySettings:{serverName:$sni, fingerprint:(if $fp == "" then "chrome" else $fp end),
                            publicKey:$pbk, shortId:$sid, spiderX:(if $spx == "" then "/" else $spx end)},
           xhttpSettings:{path:$path, mode:(if $mode == "" then "auto" else $mode end),
             xmux:{maxConnections:3, hMaxRequestTimes:"600-900", hMaxReusableSecs:"1800-3000"}}}}'
}

# {"kind":"xray","user":NAME,"uri":...,"outbound":{...}}
proto_vless_xhttp_probe() {
    local name="${1:-owner}" uri
    uri="$(proto_vless_xhttp_links "$name" | head -1)"
    [ -n "$uri" ] || { log_err "vless-xhttp: нет ссылки для $name"; return 1; }
    jq -cn --arg u "$name" --arg uri "$uri" --argjson ob "$(_xhttp_outbound_from_uri "$uri")" \
        '{kind:"xray", user:$u, uri:$uri, outbound:$ob}'
}

# ---------- манифест и трафик ----------

proto_vless_xhttp_manifest_refresh() {
    local id ib users states en name links="[]" probe="null" uri pport notes
    id="$(_xhttp_need_inbound)" || return 1
    ib="$(xui_inbound_get "$id")"
    pport="$(proto_vless_xhttp_public_port)" || { log_err "vless-xhttp: не найден master-inbound для fallback"; return 1; }
    # «имя<TAB>true|false»: enable клиента общий для всех Xray-протоколов (D18)
    states="$(xui_inbound_user_list "$id")"
    users="$(cut -f1 <<< "$states")"
    while IFS=$'\t' read -r name en; do
        [ -n "$name" ] || continue
        while IFS= read -r uri; do
            [ -n "$uri" ] || continue
            links="$(jq -c --arg u "$name" --arg l "$uri" --arg en "$en" \
                '. + [{user:$u, uri:$l, enabled:($en != "false")}]' <<< "$links")"
        done < <(proto_vless_xhttp_links "$name")
    done <<< "$states"
    if grep -qx owner <<< "$users"; then
        probe="$(proto_vless_xhttp_probe owner)" || probe="null"
    fi
    notes="Запасной TCP-транспорт к VLESS+REALITY+Vision. Клиенты на ядре Xray: v2rayN, v2rayNG, Happ, INCY, Throne; mihomo ≥1.19.22 через подписку. sing-box/Hiddify/NekoBox/Shadowrocket XHTTP не поддерживают. flow пустой. xmux не задавать вручную частично: клиентские дефолты (maxConnections=3) действуют, только если блок xmux пуст. В ядрах Xray v26.9.8+ на Go 1.27 есть открытый баг xmux на стороне клиента (XTLS/Xray-core#6797)."
    manifest_write "$PROTO_XHTTP_ID" "$(jq -cn --argjson ib "$ib" --argjson links "$links" --argjson probe "$probe" \
        --argjson port "$pport" --arg placement "${XHTTP_PLACEMENT:-port}" --arg notes "$notes" '
        ($ib.streamSettings | if type == "string" then fromjson else . end) as $st |
        {id:"vless-xhttp", name:"VLESS + XHTTP + REALITY", short:"VLESS XHTTP", layer:"tcp", port:$port, engine:"xray",
         phase:"04b-vless-xhttp", enable_var:"ENABLE_XHTTP",
         service:"x-ui", enabled:($ib.enable == true), users_backend:"xui",
         links:$links, files:[], probe:$probe,
         params:{placement:$placement, inbound_id:$ib.id, inbound_port:$ib.port, listen:$ib.listen,
                 path:$st.xhttpSettings.path, mode:$st.xhttpSettings.mode},
         notes:$notes}')"
}

# JSON-строки {user, up, down, scope}. 3x-ui считает трафик клиента суммарно по всем его
# Xray-inbound (scope "xui-client"); строка scope "inbound" — только XHTTP-inbound
proto_vless_xhttp_traffic() {
    local id
    id="$(_xhttp_need_inbound)" || return 1
    xui_inbound_get "$id" | jq -c '
        (.clientStats // [])[] | {user:.email, up, down, scope:"xui-client"}'
    xui_inbound_get "$id" | jq -c '{user:null, up, down, scope:"inbound"}'
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    act="${1:-}"; shift || true
    declare -F "proto_vless_xhttp_$act" >/dev/null || die "использование: $0 user_add|user_del|user_enable|user_list|links|probe|manifest_refresh|traffic [аргументы]"
    "proto_vless_xhttp_$act" "$@"
fi
