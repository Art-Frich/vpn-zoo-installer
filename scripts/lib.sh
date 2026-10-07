#!/usr/bin/env bash
# lib.sh — общие функции для всех саб-скриптов
# source'ится из install.sh и каждого NN-*.sh. Повторный source безопасен.

set -euo pipefail

# ============================================================
# Логирование
# ============================================================

if [ -t 1 ]; then
    C_RED='\033[0;31m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'
    C_BLUE='\033[0;34m'; C_CYAN='\033[0;36m'; C_RESET='\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_CYAN=''; C_RESET=''
fi

log_info()  { echo -e "${C_CYAN}[i]${C_RESET} $*"; }
log_ok()    { echo -e "${C_GREEN}[+]${C_RESET} $*"; }
log_warn()  { echo -e "${C_YELLOW}[!]${C_RESET} $*"; }
log_err()   { echo -e "${C_RED}[x]${C_RESET} $*" >&2; }
log_step()  { echo -e "\n${C_BLUE}===${C_RESET} ${C_BLUE}$*${C_RESET} ${C_BLUE}===${C_RESET}"; }

die() { log_err "$*"; exit 1; }

# ============================================================
# Окружение и пути
# ============================================================

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPTS_DIR="${SCRIPTS_DIR:-$REPO_ROOT/scripts}"

VPN_ETC="${VPN_ETC:-/etc/vpn-setup}"
CONFIG_FILE="${CONFIG_FILE:-$VPN_ETC/config.env}"
STATE_FILE="${STATE_FILE:-/var/lib/vpn-setup/state}"
MANIFEST_DIR="${MANIFEST_DIR:-$VPN_ETC/protocols.d}"
PORTS_FILE="${PORTS_FILE:-$VPN_ETC/ports.tsv}"
OWNED_DIR="${OWNED_DIR:-$VPN_ETC/owned.d}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/vpn-setup}"
LOG_DIR="${LOG_DIR:-/var/log/vpn-zoo}"
VERSIONS_FILE="${VERSIONS_FILE:-$SCRIPTS_DIR/versions.env}"

# Порты, которые никогда не занимаем: их ищут сканеры и ТСПУ (ARCHITECTURE §3)
BANNED_PORTS="1080 3128 8080 9050 2053 54321"

# Docker-стенд (docker/README.md): systemd работает, пропускаются только модули ядра,
# swap, глобальные sysctl и reboot
is_test_docker() { [ "${ZOO_TEST_ENV:-}" = "docker" ]; }
is_test_env() { is_test_docker; }

# Контейнер без нашего стенда (docker/lxc): ядро чужое, kernel-зависимые шаги невозможны
is_container() {
    is_test_docker && return 1
    [ -f /.dockerenv ] && return 0
    grep -qaE '(lxc|docker|kubepods|containerd)' /proc/1/cgroup 2>/dev/null && return 0
    [ "${CONTAINER_MODE:-0}" = "1" ] && return 0
    return 1
}

# Нельзя грузить свои модули ядра и перезагружаться (стенд или чужой контейнер)
kernel_is_foreign() { is_test_env || is_container; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Этот скрипт нужно запускать от root"
}

# OS_ID/OS_VERSION_ID/OS_CODENAME для остальных фаз
require_ubuntu() {
    [ -f /etc/os-release ] || die "нет /etc/os-release — это не Linux?"
    # shellcheck source=/dev/null
    . /etc/os-release
    OS_ID="${ID:-}"; OS_VERSION_ID="${VERSION_ID:-}"
    export OS_CODENAME="${VERSION_CODENAME:-}"
    case "$OS_ID:$OS_VERSION_ID" in
        ubuntu:22.04|ubuntu:24.04) ;;
        ubuntu:26.04) log_warn "Ubuntu 26.04 не проверялась стендом: AWG kmod на ядре 7.0 не собирается, будет userspace" ;;
        *) die "поддерживаются Ubuntu 22.04/24.04 (26.04 — с предупреждением), найдено: ${OS_ID} ${OS_VERSION_ID}" ;;
    esac
}

# amd64|arm64 → ZOO_ARCH. Другие архитектуры не поддерживаем: под них нет пинов в versions.env
detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64)  ZOO_ARCH="amd64" ;;
        aarch64|arm64) ZOO_ARCH="arm64" ;;
        *) die "неподдерживаемая архитектура: $(uname -m) (есть amd64 и arm64)" ;;
    esac
    printf '%s\n' "$ZOO_ARCH"
}

versions_load() {
    [ -f "$VERSIONS_FILE" ] || die "нет $VERSIONS_FILE"
    # shellcheck source=versions.env
    . "$VERSIONS_FILE"
}

# Значение переменной по имени: version_for XUI_SHA256 amd64 → $XUI_SHA256_amd64
version_for() {
    local name="${1}_${2}"
    [ -n "${!name:-}" ] || die "в versions.env нет $name"
    printf '%s\n' "${!name}"
}

# ============================================================
# State — какие фазы выполнены
# ============================================================

state_init() {
    mkdir -p "$(dirname "$STATE_FILE")"
    [ -f "$STATE_FILE" ] || : > "$STATE_FILE"
    # 02-kernel-hwe переименована в 02-kernel: переносим запись, чтобы не гонять фазу заново
    if grep -q '^02-kernel-hwe=' "$STATE_FILE" && ! grep -q '^02-kernel=' "$STATE_FILE"; then
        echo "02-kernel=$(grep '^02-kernel-hwe=' "$STATE_FILE" | tail -1 | cut -d= -f2)" >> "$STATE_FILE"
    fi
}

state_get() {
    local phase="$1"
    { grep -E "^${phase}=" "$STATE_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2
}

state_set() {
    local phase="$1" value="$2"
    mkdir -p "$(dirname "$STATE_FILE")"
    [ -f "$STATE_FILE" ] || : > "$STATE_FILE"
    { grep -vE "^${phase}=" "$STATE_FILE" || true; } > "${STATE_FILE}.tmp"
    [ -z "$value" ] || echo "${phase}=${value}" >> "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
}

is_done() { [ "$(state_get "$1")" = "done" ]; }

# Версия формата state. Первая версия инсталлера писала «done» без версии: её фазы
# устарели (3x-ui latest, порт 8443, панель наружу), поэтому при переходе на v2 все
# «done» сбрасываются и фазы проходят заново (они идемпотентны). Компоненты, которые
# ставила v1, помечаются нашими, чтобы guard_foreign_install не отказал.
STATE_SCHEMA=2

state_migrate() {
    state_init
    [ "$(state_get __schema)" = "$STATE_SCHEMA" ] && return 0
    if grep -qE '^[0-9]{2}[a-z]?-[a-z0-9-]+=done$' "$STATE_FILE"; then
        log_warn "state от первой версии инсталлера — все фазы будут пройдены заново (бэкап state: ${STATE_FILE}.v1)"
        cp -a "$STATE_FILE" "${STATE_FILE}.v1"
        if is_done 03-3xui && [ -d /etc/x-ui ]; then
            mark_owned x-ui
            config_set PANEL_CREDS_SET 1
        fi
        is_done 05-hysteria2 && [ -d /etc/hysteria ] && mark_owned hysteria
        is_done 06-amneziawg && [ -d /etc/amnezia/amneziawg ] && mark_owned amneziawg
        local tmp
        tmp="$(mktemp "${STATE_FILE}.XXXXXX")"
        { grep -vE '=done$' "$STATE_FILE" || true; } > "$tmp"
        mv -f "$tmp" "$STATE_FILE"
    fi
    state_set __schema "$STATE_SCHEMA"
}

mark_done() { state_set "$1" "done"; }

# ============================================================
# Конфиг /etc/vpn-setup/config.env
#   формат: KEY='значение' (одинарные кавычки, ' внутри → '\''), читается bash и shlex
# ============================================================

config_load() {
    if [ -f "$CONFIG_FILE" ]; then
        set -a
        # shellcheck source=/dev/null
        . "$CONFIG_FILE"
        set +a
    fi
}

_config_quote() {
    local v="$1"
    printf "'%s'" "${v//\'/\'\\\'\'}"
}

_config_lock_path() {
    if [ -d /run/lock ] && [ -w /run/lock ]; then echo /run/lock/vpn-setup-config.lock; else echo "${CONFIG_FILE}.lock"; fi
}

# Атомарно записать один ключ, остальные строки файла не трогаем
config_set() {
    local key="$1" value="${2-}"
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || die "config_set: недопустимое имя ключа: $key"
    [[ "$value" != *$'\n'* ]] || die "config_set: перевод строки в значении $key"
    local dir tmp line
    dir="$(dirname "$CONFIG_FILE")"
    ( umask 077; mkdir -p "$dir" )
    chmod 700 "$dir"
    line="${key}=$(_config_quote "$value")"
    (
        umask 077
        exec 9>"$(_config_lock_path)"
        flock 9
        tmp="$(mktemp "${CONFIG_FILE}.XXXXXX")"
        if [ -f "$CONFIG_FILE" ]; then
            # через ENVIRON: awk -v раскрыл бы \t и \\ в значении
            ZOO_CFG_K="$key" ZOO_CFG_L="$line" awk '
                BEGIN { done = 0; k = ENVIRON["ZOO_CFG_K"]; l = ENVIRON["ZOO_CFG_L"] }
                index($0, k "=") == 1 { if (!done) { print l; done = 1 }; next }
                { print }
                END { if (!done) print l }' "$CONFIG_FILE" > "$tmp"
        else
            { echo "# vpn-zoo config — ключи пишет config_set, правка руками допустима"; echo "$line"; } > "$tmp"
        fi
        chmod 600 "$tmp"
        mv -f "$tmp" "$CONFIG_FILE"
    )
    printf -v "$key" '%s' "$value"
    export "${key?}"
}

# Значение ключа из файла (не из окружения)
config_get() {
    local key="$1"
    [ -f "$CONFIG_FILE" ] || return 0
    # shellcheck source=/dev/null
    ( set +u; unset "$key"; . "$CONFIG_FILE"; printf '%s' "${!key:-}" )
}

config_has() { [ -f "$CONFIG_FILE" ] && grep -q "^${1}=" "$CONFIG_FILE"; }

# Записать ключ, только если его ещё нет в файле (значение по умолчанию)
config_default() {
    local key="$1" value="$2"
    if config_has "$key"; then
        printf -v "$key" '%s' "$(config_get "$key")"
        export "${key?}"
    else
        config_set "$key" "$value"
    fi
}

# Ключи, которые пользователь может задать через окружение: install.sh сохраняет их
# в config.env до запуска фаз. Фазы читают только файл. ZOO_* (ZOO_FORCE, ZOO_SKIP_UPGRADE,
# ZOO_NO_REBOOT, ZOO_TEST_ENV) — разовые переключатели, в файл не попадают.
# WARP_REREGISTER и ZOO_VLESS_REPICK — разовые, поэтому WARP_* и ZOO_* сюда не входят.
# SSH_CONFIRM, SSH_CONFIRM_FORCE и SSH_REVERT_MIN (фаза 01b) — тоже разовые.
CONFIG_ENV_KEYS_RE='^(SERVER_IP|LABEL|DOMAIN|ENABLE_[A-Z0-9_]+|[A-Z0-9]+_ENGINE|RU_EGRESS|SUB_PUBLIC|PANEL_2FA|PANEL_PORT|PANEL_PATH|PANEL_USER|VLESS_[A-Z_]+|XHTTP_[A-Z_]+|SS_[A-Z_]+|TUIC_[A-Z_]+|HY2_[A-Z0-9_]+|AWG_[A-Z0-9_]+|ROUTING_ECHO_EXTRA|SSH_PORTS|SSH_HARDEN|SSH_PORT|AUTO_REBOOT|AUTO_REBOOT_TIME)$'

# Снимок «что задано в окружении» — делать ДО config_load
config_capture_env() {
    local name
    ZOO_ENV_OVERRIDES=()
    while IFS= read -r name; do
        [[ "$name" =~ $CONFIG_ENV_KEYS_RE ]] || continue
        ZOO_ENV_OVERRIDES+=("$name=${!name}")
    done < <(compgen -e)
}

# Изменённые ключи — в ZOO_ENV_CHANGED: install.sh по ним перезапускает фазы-владельцы
config_apply_env() {
    local kv
    ZOO_ENV_CHANGED=()
    for kv in "${ZOO_ENV_OVERRIDES[@]:-}"; do
        [ -n "$kv" ] || continue
        if [ "$(config_get "${kv%%=*}")" != "${kv#*=}" ] || ! config_has "${kv%%=*}"; then
            config_has "${kv%%=*}" && ZOO_ENV_CHANGED+=("${kv%%=*}")
            config_set "${kv%%=*}" "${kv#*=}"
            log_info "config: ${kv%%=*} взят из окружения"
        fi
    done
}

# Ядро config.env: адрес, флаги протоколов, движки. Порты и секреты протоколов
# заводит фаза, которая ими владеет.
config_init_defaults() {
    config_default LABEL "vpn"
    if ! config_has SERVER_IP; then
        local ip
        ip="$(detect_server_ip)"
        [ -n "$ip" ] || die "не удалось определить IP сервера — задайте SERVER_IP=..."
        config_set SERVER_IP "$ip"
    fi
    config_default ENABLE_VLESS 1
    config_default ENABLE_XHTTP 1
    config_default ENABLE_SS 0
    config_default ENABLE_TUIC 1
    config_default ENABLE_HY2 1
    config_default ENABLE_HY2_OBFS 1
    config_default ENABLE_AWG 1
    config_default ENABLE_WARP 0
    config_default ENABLE_ZOO 1
    config_default SSH_HARDEN 0
    config_default RU_EGRESS direct
    config_default SUB_PUBLIC 0
    config_default AWG_ENGINE auto
    config_default HY2_ENGINE apernet
    config_legacy_defaults
}

# Раньше подставляла в окружение умолчания VLESS_*/HY2_*/AWG_* для фаз первой версии.
# Фазы v2 сами заводят свои ключи (config_default) и читают их из файла: случайный
# HY2_PASSWORD в окружении был ловушкой для всех, кто читал $HY2_PASSWORD без config_get.
config_legacy_defaults() { :; }

# Совместимость со старыми фазами: раньше config_save переписывал файл целиком.
# Теперь — config_set для каждого известного ключа, который задан.
config_save() {
    local k
    for k in SERVER_IP LABEL PANEL_PORT PANEL_PATH PANEL_USER PANEL_PASS \
             VLESS_PORT VLESS_SNI VLESS_UUID VLESS_PRIV VLESS_PUB VLESS_SID \
             HY2_PORT HY2_SNI HY2_PASSWORD \
             AWG_PORT AWG_NETWORK AWG_SERVER_KEY AWG_SERVER_PUB AWG_CLIENT_KEY AWG_CLIENT_PUB AWG_CLIENT_PSK; do
        [ -n "${!k:-}" ] || continue
        [ "$(config_get "$k")" = "${!k}" ] || config_set "$k" "${!k}"
    done
}

# ============================================================
# Манифесты протоколов (ARCHITECTURE §4)
# ============================================================

manifest_path() { printf '%s/%s.json\n' "$MANIFEST_DIR" "$1"; }

# manifest_write ID JSON — JSON проверяется jq, id внутри должен совпасть с ID
manifest_write() {
    local id="$1" json="$2" tmp
    [[ "$id" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "manifest_write: недопустимый id: $id"
    command -v jq >/dev/null || die "manifest_write: нет jq (фаза 00 не выполнена?)"
    printf '%s' "$json" | jq -e --arg id "$id" '.id == $id' >/dev/null \
        || die "manifest_write: невалидный JSON или .id != $id"
    ( umask 077; mkdir -p "$MANIFEST_DIR" )
    chmod 700 "$MANIFEST_DIR"
    tmp="$(mktemp "$MANIFEST_DIR/.${id}.XXXXXX")"
    printf '%s' "$json" | jq . > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$(manifest_path "$id")"
}

manifest_list() {
    local f
    [ -d "$MANIFEST_DIR" ] || return 0
    for f in "$MANIFEST_DIR"/*.json; do
        [ -f "$f" ] && basename "$f" .json
    done
    return 0
}

manifest_get() { cat "$(manifest_path "$1")"; }

manifest_del() { rm -f "$(manifest_path "$1")"; }

# ============================================================
# Пользователи зоопарка: имя и каталог /etc/vpn-setup/clients/<имя>
# ============================================================

ZOO_CLIENTS_DIR="${ZOO_CLIENTS_DIR:-$VPN_ETC/clients}"

# Имя пользователя: латиница, цифры, _.- до 32 символов (email клиента 3x-ui, имя
# каталога, метка в ссылках — одно и то же во всех протоколах)
zoo_user_valid() { [[ "${1:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$ ]]; }

zoo_user_require() {
    zoo_user_valid "${1:-}" || die "недопустимое имя пользователя: «${1:-}» (латиница, цифры, _ . -, до 32 символов)"
}

# Каталог пользователя (0700), печатает путь
zoo_client_dir() {
    local d="$ZOO_CLIENTS_DIR/$1"
    ( umask 077; mkdir -p "$d" )
    chmod 700 "$ZOO_CLIENTS_DIR" "$d"
    printf '%s\n' "$d"
}

# zoo_client_file_write NAME FILE — stdin в файл 0600 каталога пользователя (атомарно)
zoo_client_file_write() {
    local d tmp
    d="$(zoo_client_dir "$1")"
    tmp="$(mktemp "$d/.${2}.XXXXXX")"
    cat > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$d/$2"
}

# Удалить файл пользователя и каталог, если он опустел
zoo_client_file_del() {
    local d="$ZOO_CLIENTS_DIR/$1"
    rm -f "${d:?}/$2"
    rmdir "$d" 2>/dev/null || true
}

# Приложения через VPN (D31): реестр zoo allow, без него — пресет из репо.
# Правит только zoo (zoolib/allowlist.py); здесь — чтение для конфигов клиентов
ALLOWLIST_FILE="${ALLOWLIST_FILE:-$VPN_ETC/allowlist.json}"
ALLOWLIST_DEFAULT="${ALLOWLIST_DEFAULT:-$SCRIPTS_DIR/allowlist-default.json}"

# zoo_allowlist android|windows [ИМЯ] — список через «, »: свой список пользователя, затем
# список его группы (зеркало groups/members в allowlist.json), затем общий. Пусто — список
# пуст (вариант конфига не строится). Невалидные id отбрасываются: строка уходит в .conf как
# есть. Якоря \A…\z: «$» в jq (Oniguruma) пропускает завершающий перевод строки. Разбор тот же, что Allowlist.load в zoolib/allowlist.py
zoo_allowlist() {
    local plat="${1:-}" user="${2:-}" src="$ALLOWLIST_FILE" re
    case "$plat" in
        android) re='\A[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+\z' ;;
        windows) re='\A[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,63}\.[eE][xX][eE]\z' ;;
        *) log_err "zoo_allowlist: платформа android или windows"; return 1 ;;
    esac
    [ -s "$src" ] || src="$ALLOWLIST_DEFAULT"
    [ -s "$src" ] || { log_err "нет ни $ALLOWLIST_FILE, ни $ALLOWLIST_DEFAULT"; return 1; }
    jq -r --arg p "$plat" --arg u "$user" --arg re "$re" '
        def obj: if type == "object" then . else {} end;
        def arr: if type == "array" then . else null end;
        (obj | .members | obj | .[$u] | if type == "string" then . else "" end) as $g
        | (obj | .users | obj | .[$u] | obj | .[$p] | arr)
          // (obj | .groups | obj | .[$g] | obj | .[$p] | arr)
          // (obj | .[$p] | arr) // []
        | map(select(type == "string" and test($re)))
        | reduce .[] as $x ([]; if index([$x]) then . else . + [$x] end) | join(", ")' "$src"
}

# ============================================================
# Firewall: реестр портов + ufw
#   ports.tsv: порт/proto <TAB> комментарий <TAB> кто открыл
# ============================================================

_fw_valid() { [[ "$1" =~ ^[0-9]{1,5}(:[0-9]{1,5})?/(tcp|udp)$ ]]; }

# Вывод сначала в переменную: `cmd | grep -q` при pipefail падает, если grep закрыл
# канал раньше (SIGPIPE у cmd), и проверка ложно отвечает «нет»
_fw_ufw_active() { command -v ufw >/dev/null && [[ "$(ufw status 2>/dev/null)" == "Status: active"* ]]; }

# fw_allow PORT/PROTO COMMENT [limit] — запись в реестр и правило ufw (идемпотентно)
fw_allow() {
    local spec="$1" comment="${2:-vpn-zoo}" mode="${3:-allow}" owner tmp prev
    _fw_valid "$spec" || die "fw_allow: ожидаю PORT/tcp|udp или A:B/proto, получено: $spec"
    owner="${ZOO_PHASE:-$(basename "${0:-?}" .sh)}"
    comment="${comment//[$'\t\n\'']/ }"
    mkdir -p "$(dirname "$PORTS_FILE")"
    [ -f "$PORTS_FILE" ] || : > "$PORTS_FILE"
    # Чужую запись не перехватываем: иначе смена порта у одной фазы потом закроет
    # (fw_revoke старого порта) порт другого протокола
    prev="$(awk -F'\t' -v s="$spec" '$1 == s {print $3}' "$PORTS_FILE" | tail -1)"
    [ -z "$prev" ] || [ "$prev" = "$owner" ] \
        || die "fw_allow: $spec уже открыт фазой $prev — порт занят другим протоколом, задайте другой"
    tmp="$(mktemp "${PORTS_FILE}.XXXXXX")"
    { awk -F'\t' -v s="$spec" '$1 != s' "$PORTS_FILE"; printf '%s\t%s\t%s\t%s\n' "$spec" "$comment" "$owner" "$mode"; } > "$tmp"
    mv -f "$tmp" "$PORTS_FILE"
    if command -v ufw >/dev/null; then
        ufw "$mode" "$spec" comment "$comment" >/dev/null || die "ufw $mode $spec не применилось"
    else
        log_warn "ufw не установлен — $spec записан в реестр, правило применит 01-firewall"
    fi
}

# fw_revoke PORT/PROTO — убрать из реестра и из ufw
fw_revoke() {
    local spec="$1" mode tmp
    _fw_valid "$spec" || die "fw_revoke: неверный формат: $spec"
    mode="$(awk -F'\t' -v s="$spec" '$1 == s {print $4}' "$PORTS_FILE" 2>/dev/null | tail -1)"
    if [ -f "$PORTS_FILE" ]; then
        tmp="$(mktemp "${PORTS_FILE}.XXXXXX")"
        awk -F'\t' -v s="$spec" '$1 != s' "$PORTS_FILE" > "$tmp"
        mv -f "$tmp" "$PORTS_FILE"
    fi
    if command -v ufw >/dev/null; then
        ufw --force delete "${mode:-allow}" "$spec" >/dev/null 2>&1 || true
    fi
}

# Применить весь реестр к ufw (01-firewall после установки ufw)
fw_apply_registry() {
    local spec comment owner mode
    [ -f "$PORTS_FILE" ] || return 0
    while IFS=$'\t' read -r spec comment owner mode; do
        [ -n "$spec" ] || continue
        _fw_valid "$spec" || { log_warn "ports.tsv: пропускаю строку «$spec»"; continue; }
        ufw "${mode:-allow}" "$spec" comment "$comment" >/dev/null || die "ufw ${mode:-allow} $spec не применилось"
    done < "$PORTS_FILE"
}

# Занят ли порт кем-то (listen) или реестром/конфигом
_port_listening() {
    local port="$1" proto="${2:-tcp}" flags=-Hltn
    [ "$proto" = "udp" ] && flags=-Hlun
    [ -n "$(ss "$flags" "sport = :$port" 2>/dev/null)" ]
}

port_in_use() {
    local port="$1" proto="${2:-tcp}"
    _port_listening "$port" "$proto" && return 0
    [ -f "$PORTS_FILE" ] && awk -F'\t' -v p="$port" '{split($1,a,"/"); if (a[1] == p) f=1} END {exit !f}' "$PORTS_FILE" && return 0
    [ -f "$CONFIG_FILE" ] && grep -qE "^[A-Z0-9_]*PORT='?${port}'?$" "$CONFIG_FILE" && return 0
    return 1
}

# Случайный свободный высокий порт, не из BANNED_PORTS. Диапазон — от 20000 до начала
# эфемерных портов (ip_local_port_range, обычно 32768): слушающий порт из эфемерного
# диапазона может оказаться занят исходящим соединением, и сервис не стартует
rand_port() {
    local p i lo=32768 hi span
    read -r lo _ < /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || true
    [[ "$lo" =~ ^[0-9]+$ ]] || lo=32768
    hi=$(( lo - 1 ))
    # эфемерный диапазон начинается слишком низко — берём порты выше него
    [ "$hi" -ge 25000 ] || hi=60999
    [ "$hi" -le 65535 ] || hi=65535
    span=$(( hi - 20000 + 1 ))
    for i in $(seq 1 200); do
        p=$(( 20000 + $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % span ))
        case " $BANNED_PORTS " in *" $p "*) continue ;; esac
        port_in_use "$p" tcp && continue
        port_in_use "$p" udp && continue
        printf '%s\n' "$p"
        return 0
    done
    die "rand_port: не нашёл свободный порт за 200 попыток"
}

is_banned_port() { case " $BANNED_PORTS " in *" $1 "*) return 0 ;; esac; return 1; }

# SSH-порты: SSH_PORTS (env/config) + текущая сессия, sshd -T, ssh.socket, слушающий sshd;
# fallback 22. Объединение, а не «SSH_PORTS или детект»: если порт sshd сменили после
# установки, сохранённый SSH_PORTS устарел, и без детекта новый порт не откроется
detect_ssh_ports() {
    local ports=() p
    # SSH закрыт фазой 01b: порты ведёт она (SSH_PORTS). Иначе старая сессия, ещё открытая
    # на закрытом порту (SSH_CONNECTION), снова открыла бы его в ufw
    if [ -f "$SSH_STATE_DIR/orig.env" ] && [ -n "${SSH_PORTS:-}" ]; then
        tr -s ', ' '\n' <<< "$SSH_PORTS" | awk '/^[0-9]+$/ && $1 >= 1 && $1 <= 65535' | sort -un
        return 0
    fi
    if [ -n "${SSH_PORTS:-}" ]; then
        while read -r p; do ports+=("$p"); done < <(tr -s ', ' '\n' <<< "$SSH_PORTS")
    fi
    if [ -n "${SSH_CONNECTION:-}" ]; then
        p="$(awk '{print $4}' <<< "$SSH_CONNECTION")"
        [[ "$p" =~ ^[0-9]+$ ]] && ports+=("$p")
    fi
    if command -v sshd >/dev/null; then
        while read -r p; do ports+=("$p"); done < <(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}')
    fi
    if systemctl cat ssh.socket >/dev/null 2>&1; then
        while read -r p; do ports+=("$p"); done < <(systemctl show ssh.socket -p Listen --value 2>/dev/null \
            | tr ' ' '\n' | grep -oE '[0-9]+$' || true)
    fi
    while read -r p; do ports+=("$p"); done < <(ss -Htlnp 2>/dev/null | awk '/"sshd"/ {n=split($4,a,":"); print a[n]}')
    [ "${#ports[@]}" -gt 0 ] || ports=(22)
    printf '%s\n' "${ports[@]}" | awk '/^[0-9]+$/ && $1 >= 1 && $1 <= 65535' | sort -un
}

# Порт для команд ssh/scp, которые печатают 99 и zoo. SSH_LOGIN_PORT пишет фаза 01b: до
# подтверждения переноса это старый порт (он работает и после отката). Без 01b — первый
# из SSH_PORTS
ssh_login_port() {
    local p="${SSH_LOGIN_PORT:-}"
    [[ "$p" =~ ^[0-9]+$ ]] || p="$(tr -s ', ' '\n' <<< "${SSH_PORTS:-}" | awk '/^[0-9]+$/ {print; exit}')"
    printf '%s\n' "${p:-22}"
}

# Состояние фазы 01b: снимки конфигурации, ожидание подтверждения
SSH_STATE_DIR="${SSH_STATE_DIR:-/var/lib/vpn-setup/ssh}"

# ============================================================
# Бэкапы и чужие установки
# ============================================================

# Каталог бэкапа текущего прогона: /var/backups/vpn-setup/<ts>/
backup_dir() {
    local d="$BACKUP_ROOT/${ZOO_RUN_TS:=$(date +%Y%m%d-%H%M%S)}"
    export ZOO_RUN_TS
    ( umask 077; mkdir -p "$d" )
    printf '%s\n' "$d"
}

# backup_path PATH... — копия с сохранением структуры пути; печатает каталог бэкапа
backup_path() {
    local d p
    d="$(backup_dir)"
    for p in "$@"; do
        [ -e "$p" ] || continue
        mkdir -p "$d$(dirname "$p")"
        cp -a "$p" "$d$p"
        log_info "бэкап: $p → $d$p" >&2
    done
    printf '%s\n' "$d"
}

# prune_keep_newest DIR GLOB KEEP DAYS — в журналах установки и бэкапах лежат ключи и пароли:
# оставить KEEP самых новых записей DIR/GLOB, из остальных удалить те, что старше DAYS дней
prune_keep_newest() {
    local dir="$1" glob="$2" keep="$3" days="$4" n=0 p
    [ -d "$dir" ] || return 0
    [[ "$keep" =~ ^[0-9]+$ && "$days" =~ ^[0-9]+$ ]] || { log_warn "prune: KEEP и DAYS — числа"; return 0; }
    while IFS= read -r p; do
        n=$((n + 1))
        [ "$n" -gt "$keep" ] || continue
        [ -n "$(find "$p" -maxdepth 0 -mtime +"$days" 2>/dev/null)" ] || continue
        rm -rf -- "$p"
        log_info "удалён старый $(basename "$p") ($dir: хранятся $keep последних, старше $days дн. удаляются)"
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -name "$glob" -printf '%T@\t%p\n' 2>/dev/null | sort -rn | cut -f2-)
}

# Пометить компонент как «наш» (после успешной установки фазой)
mark_owned() {
    ( umask 077; mkdir -p "$OWNED_DIR" )
    printf '%s\n' "$(date -Iseconds) ${ZOO_PHASE:-?}" > "$OWNED_DIR/$1"
}

is_owned() { [ -f "$OWNED_DIR/$1" ]; }

# guard_foreign_install NAME PATH... — если пути существуют, а компонент ставили не мы,
# отказ без --force (ZOO_FORCE=1). При --force — бэкап и продолжение.
guard_foreign_install() {
    local name="$1"; shift
    local found=() p
    is_owned "$name" && return 0
    for p in "$@"; do [ -e "$p" ] && found+=("$p"); done
    [ "${#found[@]}" -eq 0 ] && return 0
    if [ "${ZOO_FORCE:-0}" = "1" ]; then
        log_warn "$name: найдена чужая установка (${found[*]}) — --force, делаю бэкап и продолжаю"
        backup_path "${found[@]}" >/dev/null
        return 0
    fi
    die "$name: найдена установка, сделанная не этим инсталлером: ${found[*]}. Перезапишу только с --force (будет бэкап в $BACKUP_ROOT)"
}

# ============================================================
# Загрузки
# ============================================================

# download_verified URL SHA256 DEST — скачать во временный файл рядом с DEST,
# проверить sha256 и только потом переместить
download_verified() {
    local url="$1" sum="$2" dest="$3" tmp actual
    [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || die "download_verified: неверный sha256 для $url"
    if [ -f "$dest" ] && [ "$(sha256sum "$dest" | awk '{print $1}')" = "$sum" ]; then
        log_info "уже скачано и совпадает по sha256: $dest"
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    # Постоянный .part: докачка (-C -) и между попытками, и между запусками install.sh
    # (параллельный запуск исключён блокировкой). Вместо общего таймаута — обрыв, только
    # если скорость ниже 10 КБ/с дольше минуты: медленный CDN GitHub (~130 КБ/с) дотягивает.
    tmp="${dest}.part"
    if ! retry 5 curl -fsSL --connect-timeout 15 --speed-limit 10240 --speed-time 60 -C - -o "$tmp" "$url"; then
        die "не удалось скачать $url (докачано $(( $(stat -c %s "$tmp" 2>/dev/null || echo 0) / 1048576 )) МБ — повторный запуск продолжит).
    Можно скачать вручную на другой машине и положить в $dest (sha256 $sum)"
    fi
    actual="$(sha256sum "$tmp" | awk '{print $1}')"
    if [ "$actual" != "$sum" ]; then
        rm -f "$tmp"
        die "sha256 не совпал для $url: ожидался $sum, получен $actual"
    fi
    mv -f "$tmp" "$dest"
    log_ok "скачано и проверено (sha256): $(basename "$dest")"
}

# ============================================================
# Утилиты
# ============================================================

gen_random_hex() {
    local n="${1:-8}"
    openssl rand -hex "$n"
}

# Без tr|head: при pipefail head закрывает канал раньше и tr получает SIGPIPE (rc 141)
gen_random_alnum() {
    local n="${1:-12}" out="" chunk
    while [ "${#out}" -lt "$n" ]; do
        chunk="$(openssl rand -base64 48 | LC_ALL=C tr -dc 'A-Za-z0-9')"
        out="$out$chunk"
    done
    printf '%s' "${out:0:$n}"
}

detect_default_iface() {
    ip -o -4 route show default | awk '{print $5; exit}'
}

detect_iface_ip() {
    local iface
    iface="$(detect_default_iface)"
    [ -n "$iface" ] || return 0
    ip -o -4 addr show dev "$iface" | awk '{print $4}' | cut -d/ -f1 | head -1
}

detect_public_ip() {
    local ip u
    for u in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
        ip="$(curl -4 -fsS --max-time 5 "$u" 2>/dev/null | tr -d '[:space:]' || true)"
        [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { printf '%s\n' "$ip"; return 0; }
    done
    detect_iface_ip
}

# IP, который попадёт в ссылки: SERVER_IP из окружения > адрес интерфейса (стенд) > внешний
detect_server_ip() {
    if [ -n "${SERVER_IP:-}" ]; then printf '%s\n' "$SERVER_IP"; return 0; fi
    if is_test_env; then detect_iface_ip; return 0; fi
    detect_public_ip
}

APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT:-600}"

apt_install() {
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -yq \
        -o DPkg::Lock::Timeout="$APT_LOCK_TIMEOUT" \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        "$@"
}

apt_update() {
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get update -qq -o DPkg::Lock::Timeout="$APT_LOCK_TIMEOUT"
}

# Занят ли apt/dpkg. Не `pgrep -x unattended-upgr`: под этим comm постоянно живёт
# unattended-upgrade-shutdown --wait-for-signal (сервис unattended-upgrades), и ожидание
# висело бы до таймаута на любом Ubuntu. Сам апгрейд — /usr/bin/unattended-upgrade.
_apt_busy() {
    pgrep -x 'apt|apt-get|aptitude|dpkg' >/dev/null 2>&1 && return 0
    pgrep -f '/usr/bin/unattended-upgrade( |$)' >/dev/null 2>&1 && return 0
    command -v fuser >/dev/null \
        && fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 \
        && return 0
    return 1
}

# Ожидание apt/dpkg (cloud-init, unattended-upgrades на свежем VPS). Сами apt-вызовы ещё
# и ждут блокировку через DPkg::Lock::Timeout; здесь — понятное сообщение в лог.
wait_for_apt() {
    local i=0
    while _apt_busy; do
        [ "$i" -eq 0 ] && log_info "apt занят другим процессом (unattended-upgrades?) — жду до ${APT_LOCK_TIMEOUT} с"
        [ "$i" -ge "$APT_LOCK_TIMEOUT" ] && die "apt занят дольше ${APT_LOCK_TIMEOUT} с; проверь: ps aux | grep -E 'apt|dpkg'"
        sleep 2; i=$((i+2))
    done
}

retry() {
    local n="${1:-3}"; shift
    local i=1
    while ! "$@"; do
        if [ "$i" -ge "$n" ]; then return 1; fi
        log_warn "попытка $i/$n не удалась, повторяю..."
        sleep $((i*2))
        i=$((i+1))
    done
}

# Ждать, пока порт начнёт слушать (tcp|udp), до N секунд
wait_port() {
    local port="$1" proto="${2:-tcp}" timeout="${3:-30}" i
    for i in $(seq 1 "$timeout"); do
        _port_listening "$port" "$proto" && return 0
        sleep 1
    done
    return 1
}

# journald: потолок объёма системного журнала (фазы 00 и 09 — чтобы доехал и до старых установок)
journald_limit() {
    local journald_new cfg=""
    local JOURNALD_DROPIN=/etc/systemd/journald.conf.d/50-vpn-zoo.conf
    if command -v systemd-analyze >/dev/null 2>&1; then
        cfg="$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null || true)"
    fi
    if printf '%s\n' "$cfg" | awk '
            /^# \// { own = ($0 ~ /50-vpn-zoo\.conf$/); next }
            /^[[:space:]]*SystemMaxUse=/ && !own { found = 1 }
            END { exit !found }'; then
        # drop-in читается после journald.conf и перебил бы настройку владельца — свой убрать
        if printf '%s\n' "$cfg" | grep -q '^# /.*/50-vpn-zoo\.conf$'; then
            rm -f "$JOURNALD_DROPIN"
            systemctl restart systemd-journald >/dev/null 2>&1 || true
            log_info "journald: SystemMaxUse задан чужой настройкой — свой потолок убран"
        else
            log_info "journald: SystemMaxUse уже задан чужой настройкой — свой потолок не добавляю"
        fi
        return 0
    fi
    journald_new="$(mktemp)"
    printf '%s\n' '# vpn-zoo: потолок объёма системного журнала' '[Journal]' 'SystemMaxUse=500M' > "$journald_new"
    if cmp -s "$journald_new" "$JOURNALD_DROPIN" 2>/dev/null; then
        log_info "journald: SystemMaxUse=500M уже задан"
    else
        mkdir -p "$(dirname "$JOURNALD_DROPIN")"
        install -m 0644 "$journald_new" "$JOURNALD_DROPIN"
        if systemctl restart systemd-journald >/dev/null 2>&1; then
            log_ok "journald: SystemMaxUse=500M"
        else
            log_warn "journald: лимит записан в $JOURNALD_DROPIN, но systemd-journald не перезапустился (подействует после перезагрузки)"
        fi
    fi
    rm -f "$journald_new"
}
