#!/usr/bin/env bash
# 05-hysteria2.sh — Hysteria2 (HyNetworks/hysteria, закреплённая версия + sha256, D2).
# UDP/HY2_PORT (443), self-signed ECDSA P-256 + pinSHA256 в ссылках, пользователи через
# auth.type=command (users.tsv, без рестарта), trafficStats на 127.0.0.1, masquerade proxy.
# Опции: ENABLE_HY2_OBFS=1 — второй инстанс с Salamander на HY2_OBFS_PORT;
#        HY2_HOP=1 — port hopping: nft-redirect HY2_HOP_RANGE → HY2_PORT (vpn-zoo-hy2-hop.service).
# Ссылка владельца из v1 (hysteria2://HY2_PASSWORD@…) продолжает работать: HY2_PASSWORD
# становится токеном пользователя owner.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-hysteria2.sh"
require_root
config_load
versions_load
detect_arch >/dev/null

# Выключено флагом после установки: сервисы остановлены, порты закрыты, манифест enabled=false
if [ "${ENABLE_HY2:-1}" != "1" ]; then
    proto_hysteria2_disable
    log_ok "ENABLE_HY2=$ENABLE_HY2: Hysteria2 остановлена, порты закрыты"
    exit 0
fi

# ------------------------------------------------------------
# 1. Чужая установка?
# ------------------------------------------------------------

# Установку первой версии инсталлера state_migrate уже пометил нашей
guard_foreign_install hysteria "$HY_ETC" "$HY_BIN" \
    "$HY_UNIT_DIR/$HY_UNIT" "$HY_UNIT_DIR/hysteria-server@.service"

# ------------------------------------------------------------
# 2. Параметры (config.env)
# ------------------------------------------------------------

gen_once() {
    local key="$1" val; shift
    [ -n "$(config_get "$key")" ] && { config_default "$key" ""; return 0; }
    val="$("$@")" || die "не удалось сгенерировать $key"
    [ -n "$val" ] || die "пустое значение $key"
    config_set "$key" "$val"
}

valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ] && ! is_banned_port "$1"; }

config_default HY2_PORT 443
valid_port "$HY2_PORT" || die "HY2_PORT=$HY2_PORT: нужен порт 1..65535 не из списка запрещённых ($BANNED_PORTS)"
config_default HY2_SNI bing.com
[[ "$HY2_SNI" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "HY2_SNI=$HY2_SNI: нужно доменное имя"
# Маскировка отдаёт тот же хост, что в SNI (в v1 было https://www.${HY2_SNI}/ → www.www.*)
config_default HY2_MASQ_URL "https://${HY2_SNI}/"
[[ "$HY2_MASQ_URL" =~ ^https?://[^[:space:]\"]+$ ]] || die "HY2_MASQ_URL=$HY2_MASQ_URL: нужен http(s)-URL"
config_default HY2_CERT_DAYS 3650
config_default HY2_LOG_LEVEL error
config_default HY2_HOP 0
config_default HY2_HOP_RANGE 40000-49999
if [ "$HY2_HOP" = "1" ]; then
    iface="$(detect_default_iface || true)"
    config_default HY2_HOP_IFACE "$iface"
fi
config_default ENABLE_HY2_OBFS 1
gen_once HY2_STATS_PORT rand_port
gen_once HY2_STATS_SECRET gen_random_alnum 32

# Токен владельца: пароль v1 (config.env), иначе пароль из прежнего config.yaml с
# auth.type=password (установка get.hy2.sh, перехват с --force) — старые ссылки живут
legacy_yaml_password() {
    [ -f "$HY_ETC/config.yaml" ] || return 0
    awk '/^auth:/ {a=1; next} a && /^[^[:space:]#]/ {a=0}
         a && $1 == "password:" {v=$2; gsub(/^["'\'']|["'\'']$/, "", v); print v; exit}' "$HY_ETC/config.yaml"
}
owner_tok="$(config_get HY2_PASSWORD)"
if [ -z "$owner_tok" ]; then
    owner_tok="$(legacy_yaml_password)"
    [ -z "$owner_tok" ] || log_info "пароль owner взят из прежнего $HY_ETC/config.yaml (старые ссылки продолжат работать)"
fi
if [ -n "$owner_tok" ] && ! _hy2_token_valid "$owner_tok"; then
    log_warn "HY2_PASSWORD содержит символы, недопустимые в ссылке (нужно A-Z a-z 0-9 . _ ~ -, 8..128) — у owner будет новый пароль, старую ссылку Hy2 придётся заменить"
    owner_tok=""
fi
if [ -z "$owner_tok" ]; then
    cur="$(_hy2_users_get owner 2>/dev/null || true)"
    owner_tok="${cur%%$'\t'*}"
    [ -n "$owner_tok" ] || owner_tok="$(gen_random_alnum 24)"
    [ -n "$owner_tok" ] || die "не удалось сгенерировать пароль Hysteria2"
fi
[ "$(config_get HY2_PASSWORD)" = "$owner_tok" ] || config_set HY2_PASSWORD "$owner_tok"

if hy2_obfs_enabled; then
    gen_once HY2_OBFS_PORT rand_port
    valid_port "$HY2_OBFS_PORT" && [ "$HY2_OBFS_PORT" != "$HY2_PORT" ] \
        || die "HY2_OBFS_PORT=$HY2_OBFS_PORT: недопустимый порт"
    gen_once HY2_OBFS_PASSWORD gen_random_alnum 24
    _hy2_token_valid "$HY2_OBFS_PASSWORD" || die "HY2_OBFS_PASSWORD: допустимы A-Z a-z 0-9 . _ ~ -, 8..128 символов"
    gen_once HY2_OBFS_STATS_PORT rand_port
fi

# Диапазон hopping не должен накрывать UDP-порты других протоколов: redirect перехватит их
if [ "$HY2_HOP" = "1" ]; then
    [[ "$HY2_HOP_RANGE" =~ ^([0-9]+)-([0-9]+)$ ]] || die "HY2_HOP_RANGE=$HY2_HOP_RANGE: нужен вид A-B"
    hop_a="${BASH_REMATCH[1]}"; hop_b="${BASH_REMATCH[2]}"
    { [ "$hop_a" -lt "$hop_b" ] && [ "$hop_b" -le 65535 ]; } || die "HY2_HOP_RANGE=$HY2_HOP_RANGE: нужно A<B<=65535"
    [[ "${HY2_HOP_IFACE:-}" =~ ^[A-Za-z0-9_.@-]+$ ]] \
        || die "HY2_HOP_IFACE: не определён внешний интерфейс — задайте HY2_HOP_IFACE=..."
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        if [ "$p" -ge "$hop_a" ] && [ "$p" -le "$hop_b" ]; then
            die "HY2_HOP_RANGE=$HY2_HOP_RANGE пересекается с занятым портом $p (config.env/ports.tsv) — задайте другой диапазон"
        fi
    done < <({ grep -oE "^[A-Z0-9_]*PORT='?[0-9]+" "$CONFIG_FILE" | grep -oE '[0-9]+$'
               awk -F'\t' '{split($1,a,"/"); print a[1]}' "$PORTS_FILE" 2>/dev/null | grep -oE '^[0-9]+'; } | sort -un)
fi

# ------------------------------------------------------------
# 3. Порт свободен (или его держит наша же Hysteria)
# ------------------------------------------------------------

check_udp_port() {
    local port="$1" who
    who="$(ss -Hulnp "sport = :$port" 2>/dev/null || true)"
    [ -z "$who" ] && return 0
    [[ "$who" == *'"hysteria"'* ]] && return 0
    die "UDP-порт $port занят другим процессом: $who"
}
check_udp_port "$HY2_PORT"
hy2_obfs_enabled && check_udp_port "$HY2_OBFS_PORT"

# ------------------------------------------------------------
# 4. Бинарь, пользователь, сертификат, auth-помощник
# ------------------------------------------------------------

log_step "Hysteria $HY2_VERSION"
restart_all=0
hy2_install_binary && restart_all=1
hy2_ensure_user
( umask 022; mkdir -p "$HY_ETC" )
chown root:"$HY_SVC_USER" "$HY_ETC"; chmod 0750 "$HY_ETC"
hy2_install_auth_helper
hy2_ensure_cert && restart_all=1
pin="$(hy2_pin)"
[[ "$pin" =~ ^[0-9a-f]{64}$ ]] || die "не удалось посчитать pinSHA256 сертификата"
[ "$(config_get HY2_PIN)" = "$pin" ] || config_set HY2_PIN "$pin"
hy2_stats_hdr_write

if [ "$HY2_HOP" = "1" ] && ! command -v nft >/dev/null; then
    log_info "port hopping: ставлю nftables"
    apt_install nftables >/dev/null
fi

# Конфиг v1 (auth.type=password) — в бэкап делает _hy2_put при первой перезаписи

# ------------------------------------------------------------
# 5. Пользователь owner (до старта: без него никто не авторизуется)
# ------------------------------------------------------------

cur="$(_hy2_users_get owner 2>/dev/null || true)"
if [ -z "$cur" ]; then
    proto_hysteria2_user_add owner "$owner_tok"
elif [ "${cur%%$'\t'*}" != "$owner_tok" ]; then
    _hy2_users_set owner "$owner_tok" "${cur#*$'\t'}"
    _hy2_pass_write owner "$owner_tok"
else
    _hy2_pass_write owner "$owner_tok"
fi

# ------------------------------------------------------------
# 6. systemd + конфиги
# ------------------------------------------------------------

hy2_write_units && restart_all=1
if [ "$restart_all" = "1" ]; then
    proto_hysteria2_apply force || die "Hysteria2 не запустилась (journalctl -u $HY_UNIT)"
else
    proto_hysteria2_apply || die "Hysteria2 не запустилась (journalctl -u $HY_UNIT)"
fi

hy2_hop_apply || die "port hopping: nft-redirect не применился (journalctl -u $HY_HOP_UNIT)"

fw_allow "${HY2_PORT}/udp" "hysteria2"
want_ports=" ${HY2_PORT}/udp "
if hy2_obfs_enabled; then
    fw_allow "${HY2_OBFS_PORT}/udp" "hysteria2-obfs"
    want_ports+="${HY2_OBFS_PORT}/udp "
fi
# порты, которые фаза открывала раньше (сменился HY2_PORT, выключен obfs), закрываем
if [ -f "$PORTS_FILE" ]; then
    while IFS=$'\t' read -r spec _ owner _; do
        [ "$owner" = "${ZOO_PHASE:-05-hysteria2}" ] || continue
        case "$want_ports" in *" $spec "*) continue ;; esac
        log_info "закрываю прежний порт Hysteria2 $spec"
        fw_revoke "$spec"
    done < <(cat "$PORTS_FILE")
fi
# Диапазон hopping в ufw не открываем: redirect в nat/prerouting срабатывает раньше
# фильтра, и ufw видит уже HY2_PORT

# ------------------------------------------------------------
# 7. Самопроверка: помощник авторизации и настоящее рукопожатие
# ------------------------------------------------------------

log_step "самопроверка Hysteria2"
who="$(runuser -u "$HY_SVC_USER" -- "$HY_AUTH_BIN" 127.0.0.1:1 "$owner_tok" 0 2>/dev/null || true)"
[ "$who" = "owner" ] || die "помощник авторизации $HY_AUTH_BIN не узнал owner (ответ: «$who»)"

# Клиент на самом сервере: TLS с пином + авторизация. Трафик наружу не нужен
selfcheck() {
    local inst="$1" tmpd sport rc=1
    tmpd="$(mktemp -d)"
    sport="$(rand_port)"
    proto_hysteria2_probe owner "$inst" \
        | jq --arg l "127.0.0.1:$sport" '.client | .server = ("127.0.0.1:" + (.server | sub("^.*:"; ""))) | . + {socks5: {listen: $l}}' \
        > "$tmpd/client.json"
    HYSTERIA_DISABLE_UPDATE_CHECK=1 "$HY_BIN" client -c "$tmpd/client.json" > "$tmpd/log" 2>&1 &
    local cpid=$!
    for _ in $(seq 1 10); do
        if grep -q 'connected to server' "$tmpd/log"; then rc=0; break; fi
        kill -0 "$cpid" 2>/dev/null || break
        sleep 1
    done
    kill "$cpid" 2>/dev/null || true
    wait "$cpid" 2>/dev/null || true
    [ "$rc" = 0 ] || { log_err "клиент ($inst):"; tail -5 "$tmpd/log" >&2; }
    rm -rf "$tmpd"
    return "$rc"
}
selfcheck main || die "Hysteria2: рукопожатие с 127.0.0.1:$HY2_PORT не прошло"
log_ok "Hysteria2: рукопожатие owner прошло (UDP/$HY2_PORT)"
if hy2_obfs_enabled; then
    selfcheck obfs || die "Hysteria2 Salamander: рукопожатие с 127.0.0.1:$HY2_OBFS_PORT не прошло"
    log_ok "Hysteria2 Salamander: рукопожатие owner прошло (UDP/$HY2_OBFS_PORT)"
fi
if [ "$HY2_HOP" = "1" ]; then
    log_ok "port hopping: ${HY2_HOP_IFACE} UDP ${HY2_HOP_RANGE} → ${HY2_PORT}"
fi

# ------------------------------------------------------------
# 8. Итог
# ------------------------------------------------------------

mark_owned hysteria
config_set HY2_INSTALLED_VERSION "$(hy2_installed_version)"
proto_hysteria2_manifest_refresh
log_ok "Hysteria2 $(hy2_installed_version): UDP/${HY2_PORT}, SNI ${HY2_SNI}, pinSHA256 ${pin}"
log_info "ссылка owner: proto_hysteria2_links owner (или фаза 99)"
