#!/usr/bin/env bash
# lib/proto-mtproto.sh — MTProxy для Telegram (MTProto Fake-TLS) нативным inbound 3x-ui v3.9.0.
# Функции для фазы 04e, zoo и 99. source'ится после lib.sh и lib/xui.sh.
#
# Как устроено в 3x-ui v3.9.0: Xray MTProto не умеет — inbound "mtproto" обслуживает отдельный
# процесс mtg-multi (bin/mtg-linux-<arch>, ставится вместе с панелью), один на inbound. Секрет
# Fake-TLS у каждого клиента свой («ee» + 16 байт + домен в hex): панель генерирует его при
# attach из settings.fakeTlsDomain, mtg держит секреты всех включённых клиентов и меняет их
# без рестарта. Отключённый клиент пропадает из mtg — человека можно отозвать одного.
# Трафик mtg отдаёт по клиентам, панель складывает его в общий счётчик клиента (D18).
# Чужое соединение (не наш секрет) mtg пересылает на настоящий сайт домена — сканер видит его TLS.

# Пользователи: xui_user_attach/detach/... из lib/xui.sh, каталоги — zoo_client_* из lib.sh

MTPROTO_ID="mtproto"
MTPROTO_REMARK="zoo-mtproto"

proto_mtproto_inbound_id() {
    local id
    id="$(xui_inbound_find_by_remark "$MTPROTO_REMARK")"
    [ -n "$id" ] || id="$(xui_inbound_find_by_port "${MTPROTO_PORT:-0}" mtproto)"
    printf '%s\n' "$id"
}

_mtproto_ib() {
    local id
    id="$(proto_mtproto_inbound_id)"
    [ -n "$id" ] || { xui_inbound_missing mtproto "inbound $MTPROTO_REMARK не найден (фаза 04e не выполнена?)"; return 1; }
    printf '%s\n' "$id"
}

# Бинарь mtg, который запускает панель
proto_mtproto_bin() {
    local arch
    arch="${ZOO_ARCH:-$(detect_arch)}"
    printf '%s/bin/mtg-linux-%s\n' "$XUI_DIR" "$arch"
}

# Тело inbounds/add|update. Клиентов в теле нет: ими управляют clients/*
proto_mtproto_inbound_body() {
    jq -cn --arg r "$MTPROTO_REMARK" --argjson port "$MTPROTO_PORT" --arg d "$MTPROTO_DOMAIN" '{
        remark: $r, enable: true, listen: "", port: $port, protocol: "mtproto",
        expiryTime: 0, total: 0,
        settings: {fakeTlsDomain: $d, clients: []},
        streamSettings: {},
        sniffing: {enabled: false, destOverride: []}
    }'
}

# Домен Fake-TLS, зашитый в секрет (хвост после «ee» и 16 байт), или пусто
proto_mtproto_secret_domain() {
    local s="${1#ee}" hex
    [ "${#s}" -gt 32 ] || return 0
    hex="${s:32}"
    [[ "$hex" =~ ^([0-9a-fA-F]{2})+$ ]] || return 0
    # shellcheck disable=SC2059 # формат — сами \xHH из проверенного hex
    printf "$(sed 's/../\\x&/g' <<< "$hex")"
}

# Тот же секрет с другим доменом: случайная середина сохраняется (так же лечит секреты панель)
proto_mtproto_secret_with_domain() {
    local s="${1#ee}" mid
    mid="${s:0:32}"
    [[ "$mid" =~ ^[0-9a-fA-F]{32}$ ]] || mid="$(openssl rand -hex 16)"
    printf 'ee%s%s\n' "$mid" "$(printf '%s' "$2" | od -An -tx1 | tr -d ' \n')"
}

# {secret, enable} пользователя в inbound или пусто
_mtproto_user() {
    xui_inbound_get "$1" | jq -c --arg u "$2" \
        '[(.settings.clients // [])[] | select(.email == $u) | {secret, enable}][0] // empty'
}

# Манифест после операций с пользователями (если фаза его уже писала); MTPROTO_NO_REFRESH=1 — нет
_mtproto_refresh() {
    [ "${MTPROTO_NO_REFRESH:-0}" != "1" ] && [ -f "$(manifest_path "$MTPROTO_ID")" ] || return 0
    proto_mtproto_manifest_refresh
}

proto_mtproto_user_add() {
    local name="${1:-}" id
    zoo_user_require "$name"
    id="$(_mtproto_ib)" || return 1
    xui_user_attach "$name" "$id" || { log_err "mtproto: не удалось добавить $name"; return 1; }
    [ -n "$(_mtproto_user "$id" "$name" | jq -r '.secret // empty')" ] \
        || { log_err "mtproto: панель не выдала секрет пользователю $name"; return 1; }
    _mtproto_user_files "$name"
    _mtproto_refresh
}

proto_mtproto_user_del() {
    local name="${1:-}" id
    zoo_user_require "$name"
    id="$(_mtproto_ib)" || return 1
    xui_user_detach "$name" "$id" || { log_err "mtproto: не удалось удалить $name"; return 1; }
    zoo_client_file_del "$name" mtproto.txt
    _mtproto_refresh
}

# Флаг общий для всех протоколов 3x-ui этого пользователя (клиент один, D18)
proto_mtproto_user_enable() {
    zoo_user_require "${1:-}"
    xui_user_set_enable "$1" "${2:-true}" || return 1
    _mtproto_refresh
}

# «имя<TAB>true|false» по строке
proto_mtproto_user_list() {
    local id
    id="$(_mtproto_ib)" || return 1
    xui_inbound_user_list "$id"
}

# tg://proxy?server=…&port=…&secret=… — как в подписке 3x-ui (без #метки: её дописывают в последний параметр)
proto_mtproto_links() {
    local name="${1:-owner}" id u port
    zoo_user_require "$name"
    id="$(_mtproto_ib)" || return 1
    u="$(_mtproto_user "$id" "$name")"
    [ -n "$u" ] || return 0
    port="$(xui_inbound_get "$id" | jq -r '.port')"
    jq -rn --argjson u "$u" --arg host "${SERVER_IP:?}" --arg port "$port" \
        '"tg://proxy?server=\($host|@uri)&port=\($port)&secret=\($u.secret)"'
}

# probe: рукопожатие Fake-TLS с секретом пользователя (zoo/zoolib/probe/mtproto.py), без клиента Telegram
proto_mtproto_probe() {
    local name="${1:-owner}" id u port
    zoo_user_require "$name"
    id="$(_mtproto_ib)" || return 1
    u="$(_mtproto_user "$id" "$name")"
    [ -n "$u" ] || { log_err "mtproto: $name не привязан к inbound"; return 1; }
    port="$(xui_inbound_get "$id" | jq -r '.port')"
    jq -cn --argjson u "$u" --arg user "$name" --arg host "${SERVER_IP:?}" --argjson port "$port" \
        '{kind: "mtproto", user: $user, server: $host, server_port: $port, secret: $u.secret}'
}

_mtproto_user_files() {
    local link
    link="$(proto_mtproto_links "$1")" || return 1
    [ -n "$link" ] || return 0
    printf '%s\n' "$link" | zoo_client_file_write "$1" mtproto.txt
}

proto_mtproto_traffic() {
    local id
    id="$(_mtproto_ib)" || return 1
    xui_inbound_traffic "$id"
}

proto_mtproto_manifest_refresh() {
    local id ib users states en u links="[]" link probe="{}"
    id="$(_mtproto_ib)" || return 1
    ib="$(xui_inbound_get "$id")" || return 1
    # «имя<TAB>true|false»: enable клиента общий для всех протоколов 3x-ui (D18)
    states="$(xui_inbound_user_list "$id")"
    users="$(cut -f1 <<< "$states")"
    for u in $users; do
        zoo_user_valid "$u" || continue
        link="$(proto_mtproto_links "$u")" || continue
        [ -n "$link" ] || continue
        en="$(awk -F'\t' -v u="$u" '$1 == u {print $2; exit}' <<< "$states")"
        links="$(jq -c --arg u "$u" --arg l "$link" --arg en "$en" \
            '. + [{user: $u, uri: $l, enabled: ($en != "false")}]' <<< "$links")"
        printf '%s\n' "$link" | zoo_client_file_write "$u" mtproto.txt
    done
    if grep -qx owner <<< "$users"; then
        probe="$(proto_mtproto_probe owner)" || return 1
    fi
    manifest_write "$MTPROTO_ID" "$(jq -cn \
        --argjson port "$(jq '.port' <<< "$ib")" --argjson en "$(jq '.enable' <<< "$ib")" \
        --argjson links "$links" --argjson probe "$probe" --argjson ibid "$id" \
        --arg tag "$(jq -r '.tag' <<< "$ib")" --arg dom "$(jq -r '.settings.fakeTlsDomain // empty' <<< "$ib")" '{
        id: "mtproto",
        phase: "04e-mtproto",
        enable_var: "ENABLE_MTPROTO",
        name: "MTProxy для Telegram (Fake-TLS)",
        short: "MTProxy",
        layer: "tcp",
        port: $port,
        engine: "x-ui",
        service: "x-ui",
        enabled: $en,
        users_backend: "xui",
        xui: {inbound_id: $ibid, tag: $tag},
        fake_tls: {domain: $dom},
        links: $links,
        files: [],
        probe: $probe,
        notes: "Только Telegram: ссылка tg://proxy открывается в Telegram → «Подключить прокси», VPN-приложение не нужно, остальное на телефоне идёт напрямую. Fake-TLS под \($dom): чужое соединение уходит на настоящий сайт. Секрет у каждого человека свой. Дополнение к VPN, не замена."
    }')"
}

# Выключить протокол после установки: inbound выключен (mtg остановлен), порт закрыт
proto_mtproto_disable() {
    local id
    id="$(proto_mtproto_inbound_id)"
    [ -n "$id" ] && xui_inbound_set_enable "$id" false
    [ -z "${MTPROTO_PORT:-}" ] || fw_revoke "$MTPROTO_PORT/tcp"
    if [ -f "$(manifest_path "$MTPROTO_ID")" ]; then
        manifest_write "$MTPROTO_ID" "$(manifest_get "$MTPROTO_ID" | jq -c '.enabled = false')"
    fi
}
