#!/usr/bin/env bash
# lib/proto-amneziawg.sh — AmneziaWG: пользователи (peer'ы), конфиги клиентов, манифест.
# source'ится после lib.sh (если lib.sh не загружен — загружается здесь).
#
# Источник правды о пользователях — /etc/vpn-setup/clients/<имя>/amneziawg.{key,pub,psk,ip}
# (+ amneziawg.disabled). Серверный awg0.conf целиком генерируется из них и из AWG_* в
# config.env, затем применяется вживую: awg syncconf awg0 <(awg-quick strip awg0).
#
# Функции для zoo/99: proto_amneziawg_user_add|user_del|user_enable|user_list|links|probe|
# manifest_refresh|traffic. PROTO_AWG_NO_REFRESH=1 — не переписывать манифест после операции.

# shellcheck disable=SC2153 # AWG_* приходят из config.env
if ! declare -F config_set >/dev/null; then
    # shellcheck source=../lib.sh
    . "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
fi
# shellcheck source=awg-params.sh
. "$(dirname "${BASH_SOURCE[0]}")/awg-params.sh"

AWG_IFACE="${AWG_IFACE:-awg0}"
AWG_CONF_DIR="${AWG_CONF_DIR:-/etc/amnezia/amneziawg}"
AWG_CONF="$AWG_CONF_DIR/$AWG_IFACE.conf"
AWG_SERVICE="awg-quick@$AWG_IFACE"
AWG_NAT_HELPER="${AWG_NAT_HELPER:-/usr/local/lib/vpn-zoo/awg-nat.sh}"
AWG_MANIFEST_ID="amneziawg"
ZOO_CLIENTS_DIR="${ZOO_CLIENTS_DIR:-$VPN_ETC/clients}"

# Userspace-сборка лежит в /usr/local/bin, пакеты PPA — в /usr/bin
case ":$PATH:" in *:/usr/local/bin:*) ;; *) PATH="/usr/local/bin:$PATH" ;; esac

_awg_name_ok() { zoo_user_valid "${1:-}"; }
_awg_udir() { printf '%s/%s\n' "$ZOO_CLIENTS_DIR" "$1"; }
_awg_has_user() { [ -s "$(_awg_udir "$1")/amneziawg.pub" ]; }
_awg_user_on() { [ ! -e "$(_awg_udir "$1")/amneziawg.disabled" ]; }
_awg_iface_up() { ip link show "$AWG_IFACE" >/dev/null 2>&1; }

_awg_ctx() {
    config_load
    command -v awg >/dev/null || { log_err "amneziawg: нет команды awg (фаза 06 не выполнена?)"; return 1; }
    [ -n "${AWG_SERVER_PUB:-}" ] && [ -n "${AWG_PORT:-}" ] && [ -n "${AWG_NETWORK:-}" ] \
        || { log_err "amneziawg: в config.env нет AWG_SERVER_PUB/AWG_PORT/AWG_NETWORK"; return 1; }
}

# Все пользователи AWG (имена каталогов с amneziawg.pub)
_awg_users() {
    local f
    for f in "$ZOO_CLIENTS_DIR"/*/amneziawg.pub; do
        [ -s "$f" ] && basename "$(dirname "$f")"
    done
    return 0
}

# Файл 0600 в каталоге пользователя из stdin (атомарно)
_awg_ufile() { zoo_client_file_write "$1" "$2"; }

# Операции над peer'ами сериализуются; вложенный вызов замок не берёт
_awg_locked() {
    if [ "${_AWG_LOCKED:-0}" = "1" ]; then "$@"; return; fi
    (
        exec 7>/run/lock/vpn-zoo-awg.lock
        flock -w 60 7 || { log_err "amneziawg: не дождался блокировки"; exit 1; }
        _AWG_LOCKED=1 "$@"
    )
}

# ---------- адреса ----------

_awg_ip2int() { local a b c d; IFS=. read -r a b c d <<< "$1"; printf '%s\n' $(( (a << 24) | (b << 16) | (c << 8) | d )); }
_awg_int2ip() { printf '%s.%s.%s.%s\n' $(( ($1 >> 24) & 255 )) $(( ($1 >> 16) & 255 )) $(( ($1 >> 8) & 255 )) $(( $1 & 255 )); }

# AWG_NETWORK → «база префикс»
_awg_net() {
    local net="${AWG_NETWORK%/*}" pfx="${AWG_NETWORK#*/}" base
    [[ "$net" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ && "$pfx" =~ ^[0-9]+$ && "$pfx" -ge 16 && "$pfx" -le 29 ]] \
        || { log_err "amneziawg: AWG_NETWORK=$AWG_NETWORK — нужна IPv4-сеть /16../29"; return 1; }
    base=$(( $(_awg_ip2int "$net") & ~((1 << (32 - pfx)) - 1) & 0xFFFFFFFF ))
    printf '%s %s\n' "$base" "$pfx"
}

# Адрес сервера в туннеле: первый в сети
_awg_server_tip() { local base pfx; read -r base pfx < <(_awg_net) || return 1; _awg_int2ip $(( base + 1 )); }

# Первый свободный адрес клиента
_awg_alloc_ip() {
    local base pfx used="" u ip i
    read -r base pfx < <(_awg_net) || return 1
    for u in $(_awg_users); do used+=" $(cat "$(_awg_udir "$u")/amneziawg.ip" 2>/dev/null) "; done
    for (( i = base + 2; i < base + (1 << (32 - pfx)) - 1; i++ )); do
        ip="$(_awg_int2ip "$i")"
        case "$used" in *" $ip "*) continue ;; esac
        printf '%s\n' "$ip"; return 0
    done
    log_err "amneziawg: в $AWG_NETWORK не осталось свободных адресов"
    return 1
}

# ---------- конфиги ----------

# Параметры обфускации — одинаковые строки для сервера и клиентов
_awg_obf_lines() {
    printf 'Jc = %s\nJmin = %s\nJmax = %s\n' "$AWG_JC" "$AWG_JMIN" "$AWG_JMAX"
    printf 'S1 = %s\nS2 = %s\nS3 = %s\nS4 = %s\n' "$AWG_S1" "$AWG_S2" "$AWG_S3" "$AWG_S4"
    printf 'H1 = %s\nH2 = %s\nH3 = %s\nH4 = %s\n' "$AWG_H1" "$AWG_H2" "$AWG_H3" "$AWG_H4"
    [ -z "${AWG_I1:-}" ] || printf 'I1 = %s\n' "$AWG_I1"
    if [ "${AWG_PROFILE:-v2}" = "v3" ]; then
        printf 'HeaderProtectionKey = %s\n' "$AWG_HPK"
        [ "${AWG_RT:-0}" != "1" ] || printf 'RandomTrailers = on\n'
    fi
}

# [Interface] сервера без peer'ов: по его хешу 06 решает, нужен ли рестарт интерфейса
_awg_server_iface() {
    local tip pfx
    tip="$(_awg_server_tip)" || return 1
    pfx="${AWG_NETWORK#*/}"
    printf '[Interface]\nPrivateKey = %s\nAddress = %s/%s\nListenPort = %s\nMTU = %s\n' \
        "$AWG_SERVER_KEY" "$tip" "$pfx" "$AWG_PORT" "${AWG_MTU:-1280}"
    _awg_obf_lines
    printf 'PostUp = %s up %%i %s\nPostDown = %s down %%i %s\n' \
        "$AWG_NAT_HELPER" "$AWG_NETWORK" "$AWG_NAT_HELPER" "$AWG_NETWORK"
}

# Переписать awg0.conf из config.env и каталогов пользователей
_awg_render_server() {
    local u d tmp iface
    [ -n "${AWG_SERVER_KEY:-}" ] || { log_err "amneziawg: нет AWG_SERVER_KEY"; return 1; }
    iface="$(_awg_server_iface)" || return 1
    ( umask 077; mkdir -p "$AWG_CONF_DIR" )
    chmod 700 "$AWG_CONF_DIR"
    tmp="$(mktemp "$AWG_CONF_DIR/.${AWG_IFACE}.XXXXXX")"
    {
        echo "# vpn-zoo: генерируется 06-amneziawg / lib/proto-amneziawg.sh из config.env и"
        echo "# $ZOO_CLIENTS_DIR/*/amneziawg.*; ручные правки будут затёрты"
        printf '%s\n' "$iface"
        for u in $(_awg_users); do
            _awg_user_on "$u" || continue
            d="$(_awg_udir "$u")"
            printf '\n# user: %s\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n' \
                "$u" "$(cat "$d/amneziawg.pub")" "$(cat "$d/amneziawg.psk")" "$(cat "$d/amneziawg.ip")"
        done
    } > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$AWG_CONF"
}

# Применить awg0.conf к работающему интерфейсу без разрыва сессий
_awg_apply() {
    _awg_iface_up || return 0
    awg syncconf "$AWG_IFACE" <(awg-quick strip "$AWG_IFACE") \
        || { log_err "amneziawg: awg syncconf не применился"; return 1; }
}

_awg_endpoint() {
    case "$SERVER_IP" in *:*) printf '[%s]:%s\n' "$SERVER_IP" "$AWG_PORT" ;; *) printf '%s:%s\n' "$SERVER_IP" "$AWG_PORT" ;; esac
}

# Клиентский .conf пользователя
_awg_client_conf() {
    local d
    d="$(_awg_udir "$1")"
    printf '[Interface]\nPrivateKey = %s\nAddress = %s/32\nDNS = %s\nMTU = %s\n' \
        "$(cat "$d/amneziawg.key")" "$(cat "$d/amneziawg.ip")" "${AWG_DNS:-1.1.1.1, 1.0.0.1}" "${AWG_MTU:-1280}"
    _awg_obf_lines
    printf '\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nEndpoint = %s\nAllowedIPs = 0.0.0.0/0, ::/0\nPersistentKeepalive = %s\n' \
        "$AWG_SERVER_PUB" "$(cat "$d/amneziawg.psk")" "$(_awg_endpoint)" "${AWG_KEEPALIVE:-25}"
}

# Ключ AmneziaVPN: vpn:// + base64url(uint32_be(len) || zlib(json)) — это qCompress.
# Без psk_key клиент теряет PSK и не проходит рукопожатие. Секреты — через env, не argv
_awg_vpn_uri() {
    local d conf
    command -v python3 >/dev/null || return 1
    d="$(_awg_udir "$1")"
    conf="$(_awg_client_conf "$1")" || return 1
    AWG_U_CONF="$conf" AWG_U_KEY="$(cat "$d/amneziawg.key")" AWG_U_PSK="$(cat "$d/amneziawg.psk")" \
    AWG_U_IP="$(cat "$d/amneziawg.ip")" AWG_U_DESC="${LABEL:-vpn}-$1" \
    AWG_U_HPK="$([ "${AWG_PROFILE:-v2}" = "v3" ] && printf '%s' "${AWG_HPK:-}")" \
    AWG_SERVER_IP="$SERVER_IP" AWG_PORT="$AWG_PORT" AWG_SERVER_PUB="$AWG_SERVER_PUB" \
    AWG_MTU="${AWG_MTU:-1280}" AWG_KEEPALIVE="${AWG_KEEPALIVE:-25}" AWG_DNS="${AWG_DNS:-1.1.1.1, 1.0.0.1}" \
    AWG_JC="$AWG_JC" AWG_JMIN="$AWG_JMIN" AWG_JMAX="$AWG_JMAX" \
    AWG_S1="$AWG_S1" AWG_S2="$AWG_S2" AWG_S3="$AWG_S3" AWG_S4="$AWG_S4" \
    AWG_H1="$AWG_H1" AWG_H2="$AWG_H2" AWG_H3="$AWG_H3" AWG_H4="$AWG_H4" AWG_I1="${AWG_I1:-}" \
    python3 - <<'PY'
import base64, json, os, struct, zlib
e = os.environ
dns = [x.strip() for x in e["AWG_DNS"].split(",") if x.strip()] or ["1.1.1.1"]
inner = {k: e["AWG_" + k.upper()] for k in ("H1", "H2", "H3", "H4", "S1", "S2", "S3", "S4")}
inner.update(Jc=e["AWG_JC"], Jmin=e["AWG_JMIN"], Jmax=e["AWG_JMAX"],
             I1=e["AWG_I1"], I2="", I3="", I4="", I5="")
if e.get("AWG_U_HPK"):
    inner["HeaderProtectionKey"] = e["AWG_U_HPK"]
inner.update({
    "allowed_ips": ["0.0.0.0/0", "::/0"],
    "client_ip": e["AWG_U_IP"],
    "client_priv_key": e["AWG_U_KEY"],
    "psk_key": e["AWG_U_PSK"],
    "config": e["AWG_U_CONF"],
    "hostName": e["AWG_SERVER_IP"],
    "mtu": e["AWG_MTU"],
    "persistent_keep_alive": e["AWG_KEEPALIVE"],
    "port": int(e["AWG_PORT"]),
    "server_pub_key": e["AWG_SERVER_PUB"],
})
outer = {
    "containers": [{"awg": {"isThirdPartyConfig": True,
                            "last_config": json.dumps(inner, ensure_ascii=False),
                            "port": e["AWG_PORT"], "protocol_version": "2",
                            "transport_proto": "udp"},
                    "container": "amnezia-awg"}],
    "defaultContainer": "amnezia-awg",
    "description": e["AWG_U_DESC"],
    "dns1": dns[0], "dns2": dns[1] if len(dns) > 1 else dns[0],
    "hostName": e["AWG_SERVER_IP"],
}
raw = json.dumps(outer, ensure_ascii=False).encode()
blob = struct.pack(">I", len(raw)) + zlib.compress(raw)
print("vpn://" + base64.urlsafe_b64encode(blob).decode().rstrip("="))
PY
}

# Пересобрать файлы пользователя для выдачи: .conf, vpn://, QR (.png) текста .conf
_awg_user_files() {
    local d uri
    d="$(_awg_udir "$1")"
    _awg_client_conf "$1" | _awg_ufile "$1" amneziawg.conf
    if uri="$(_awg_vpn_uri "$1")" && [ -n "$uri" ]; then
        printf '%s\n' "$uri" | _awg_ufile "$1" amneziawg.vpnuri
    else
        log_warn "amneziawg: vpn:// для $1 не собран (нет python3?)"
        rm -f "$d/amneziawg.vpnuri"
    fi
    if command -v qrencode >/dev/null; then
        ( umask 077; qrencode -t png -o "$d/amneziawg.png" -r "$d/amneziawg.conf" ) \
            || log_warn "amneziawg: QR для $1 не создан"
    fi
}

# ---------- пользователи ----------

_awg_user_add() {
    local name="$1" d key ip
    _awg_ctx || return 1
    d="$(_awg_udir "$name")"
    if ! _awg_has_user "$name"; then
        ip="$(_awg_alloc_ip)" || return 1
        key="$(awg genkey)"
        printf '%s\n' "$key" | _awg_ufile "$name" amneziawg.key
        printf '%s\n' "$key" | awg pubkey | _awg_ufile "$name" amneziawg.pub
        awg genpsk | _awg_ufile "$name" amneziawg.psk
        printf '%s\n' "$ip" | _awg_ufile "$name" amneziawg.ip
        log_ok "amneziawg: пользователь $name, адрес $ip"
    fi
    _awg_render_server || return 1
    _awg_apply || return 1
    _awg_user_files "$name"
    [ "${PROTO_AWG_NO_REFRESH:-0}" = "1" ] || _awg_manifest_refresh
}

_awg_user_del() {
    local name="$1" d
    _awg_ctx || return 1
    _awg_has_user "$name" || { log_err "amneziawg: пользователя $name нет"; return 1; }
    d="$(_awg_udir "$name")"
    rm -f "$d"/amneziawg.*
    rmdir "$d" 2>/dev/null || true
    _awg_render_server || return 1
    _awg_apply || return 1
    log_ok "amneziawg: пользователь $name удалён"
    [ "${PROTO_AWG_NO_REFRESH:-0}" = "1" ] || _awg_manifest_refresh
}

_awg_user_enable() {
    local name="$1" en="$2" d
    _awg_ctx || return 1
    _awg_has_user "$name" || { log_err "amneziawg: пользователя $name нет"; return 1; }
    d="$(_awg_udir "$name")"
    case "$en" in
        true)  rm -f "$d/amneziawg.disabled" ;;
        false) : | _awg_ufile "$name" amneziawg.disabled ;;
        *) log_err "amneziawg: user_enable NAME true|false"; return 1 ;;
    esac
    _awg_render_server || return 1
    _awg_apply || return 1
    [ "${PROTO_AWG_NO_REFRESH:-0}" = "1" ] || _awg_manifest_refresh
}

proto_amneziawg_user_add() {
    _awg_name_ok "${1:-}" || { log_err "amneziawg: недопустимое имя «${1:-}» (латиница, цифры, _ . -, до 32)"; return 1; }
    _awg_locked _awg_user_add "$1"
}

proto_amneziawg_user_del() {
    _awg_name_ok "${1:-}" || { log_err "amneziawg: недопустимое имя «${1:-}»"; return 1; }
    _awg_locked _awg_user_del "$1"
}

proto_amneziawg_user_enable() {
    _awg_name_ok "${1:-}" || { log_err "amneziawg: недопустимое имя «${1:-}»"; return 1; }
    _awg_locked _awg_user_enable "$1" "${2:-}"
}

# «имя<TAB>true|false» по строке
proto_amneziawg_user_list() {
    local u
    for u in $(_awg_users); do
        if _awg_user_on "$u"; then printf '%s\ttrue\n' "$u"; else printf '%s\tfalse\n' "$u"; fi
    done
}

# Ссылка vpn:// (AmneziaVPN) и путь к .conf (AmneziaWG, WG Tunnel, QR)
proto_amneziawg_links() {
    local d
    _awg_has_user "${1:-}" || { log_err "amneziawg: пользователя ${1:-} нет"; return 1; }
    d="$(_awg_udir "$1")"
    [ -s "$d/amneziawg.conf" ] || { _awg_ctx && _awg_user_files "$1"; } || return 1
    [ ! -s "$d/amneziawg.vpnuri" ] || cat "$d/amneziawg.vpnuri"
    printf '%s\n' "$d/amneziawg.conf"
}

# probe для манифеста: {kind:"awg", user, conf (полный клиентский .conf), endpoint, ...}
proto_amneziawg_probe() {
    local conf
    _awg_ctx || return 1
    _awg_has_user "${1:-}" || { log_err "amneziawg: пользователя ${1:-} нет"; return 1; }
    conf="$(_awg_client_conf "$1")" || return 1
    conf+=$'\n'
    jq -cn --arg user "$1" --arg conf "$conf" --arg ep "$(_awg_endpoint)" \
        --arg tip "$(cat "$(_awg_udir "$1")/amneziawg.ip")" --arg stip "$(_awg_server_tip)" \
        --arg profile "${AWG_PROFILE:-v2}" --argjson mtu "${AWG_MTU:-1280}" \
        '{kind:"awg", user:$user, conf:$conf, endpoint:$ep, address:$tip,
          server_tunnel_ip:$stip, mtu:$mtu, profile:$profile}'
}

# Трафик из awg show dump: {user, up, down, latest_handshake}. up — от клиента к серверу.
# Счётчики сбрасываются при рестарте интерфейса и удалении peer'а
proto_amneziawg_traffic() {
    local u map="{}" dump
    _awg_iface_up || return 0
    for u in $(_awg_users); do
        map="$(jq -c --arg k "$(cat "$(_awg_udir "$u")/amneziawg.pub")" --arg u "$u" '. + {($k): $u}' <<< "$map")"
    done
    dump="$(awg show "$AWG_IFACE" dump)" || return 1
    awk -F'\t' 'NR > 1 {print $1 "\t" $5 "\t" $6 "\t" $7}' <<< "$dump" \
    | while IFS=$'\t' read -r pub hs rx tx; do
        jq -cn --argjson m "$map" --arg p "$pub" --argjson hs "$hs" --argjson rx "$rx" --argjson tx "$tx" \
            '{user:($m[$p] // ("?" + $p)), up:$rx, down:$tx, latest_handshake:$hs}'
    done
}

_awg_manifest_refresh() {
    local u d links="[]" files="[]" probe="null" probe_user="" en name notes engine active
    _awg_ctx || return 1
    for u in $(_awg_users); do
        d="$(_awg_udir "$u")"
        _awg_user_files "$u"
        if _awg_user_on "$u"; then en=true; else en=false; fi
        [ ! -s "$d/amneziawg.vpnuri" ] || links="$(jq -c --arg u "$u" --arg uri "$(cat "$d/amneziawg.vpnuri")" \
            --argjson en "$en" '. + [{user:$u, uri:$uri, enabled:$en}]' <<< "$links")"
        files="$(jq -c --arg u "$u" --arg p "$d/amneziawg.conf" --argjson en "$en" \
            '. + [{user:$u, path:$p, enabled:$en}]' <<< "$files")"
        [ -n "$probe_user" ] || ! _awg_user_on "$u" || probe_user="$u"
    done
    if _awg_has_user owner && _awg_user_on owner; then probe_user=owner; fi
    [ -z "$probe_user" ] || probe="$(proto_amneziawg_probe "$probe_user")" || probe="null"
    if [ "${AWG_PROFILE:-v2}" = "v3" ]; then
        name="AmneziaWG 3.1 (HeaderProtection)"
        [ "${AWG_RT:-0}" != "1" ] || name="AmneziaWG 3.1 (HeaderProtection + RandomTrailers)"
        notes="Профиль 3.x: нужен клиент 3.1 — AmneziaVPN ≥5.0.1.5, AmneziaWG 3.1, WG Tunnel ≥5.6.0, mihomo ≥1.19.30. Клиенты 2.0 не подключатся."
    else
        name="AmneziaWG 2.0"
        notes="Клиенты AWG 2.0+: AmneziaVPN (vpn:// или .conf с 5.0.1.5), AmneziaWG ≥2.0, WG Tunnel ≥4.2.0, mihomo ≥1.19.14. UDP: там, где режут UDP, не работает — нужен TCP-протокол."
    fi
    # фактический движок: при kernel без модуля (DKMS сломался) awg-quick берёт amneziawg-go
    active="${AWG_ENGINE_ACTIVE:-userspace}"
    if pgrep -f "amneziawg-go $AWG_IFACE\$" >/dev/null 2>&1; then active=userspace; fi
    if [ "$active" = "kernel" ]; then engine="amneziawg-kmod"; else engine="amneziawg-go"; fi
    manifest_write "$AWG_MANIFEST_ID" "$(jq -cn --arg name "$name" --argjson port "$AWG_PORT" \
        --arg engine "$engine" --arg service "$AWG_SERVICE" --argjson links "$links" --argjson files "$files" \
        --argjson probe "$probe" --arg notes "$notes" --arg profile "${AWG_PROFILE:-v2}" \
        --arg net "$AWG_NETWORK" --arg iface "$AWG_IFACE" --argjson en "$([ "${ENABLE_AWG:-1}" = 1 ] && echo true || echo false)" '{
        id:"amneziawg", name:$name, layer:"udp", port:$port, engine:$engine, service:$service,
        enabled:$en, users_backend:"awg", interface:$iface, network:$net, profile:$profile,
        links:$links, files:$files, probe:$probe, notes:$notes}')"
}

proto_amneziawg_manifest_refresh() { _awg_locked _awg_manifest_refresh; }

# Выключить AWG (ENABLE_AWG=0 после установки): интерфейс остановлен, порт закрыт,
# манифест enabled=false. Ключи и peer'ы остаются — ENABLE_AWG=1 вернёт всё
proto_amneziawg_disable() {
    systemctl disable --now "$AWG_SERVICE" >/dev/null 2>&1 || true
    [ -z "${AWG_PORT:-}" ] || fw_revoke "$AWG_PORT/udp"
    if [ -f "$(manifest_path "$AWG_MANIFEST_ID")" ]; then
        manifest_write "$AWG_MANIFEST_ID" "$(manifest_get "$AWG_MANIFEST_ID" | jq -c '.enabled = false')"
    fi
}
