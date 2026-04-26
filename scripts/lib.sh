#!/usr/bin/env bash
# lib.sh — общие функции для всех саб-скриптов
# source'ится из install.sh и каждого ##-*.sh

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
# Окружение и определение режима
# ============================================================

# Корень репозитория (откуда запущен install.sh)
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPTS_DIR="${SCRIPTS_DIR:-$REPO_ROOT/scripts}"

# Конфиг и state
CONFIG_FILE="${CONFIG_FILE:-/etc/vpn-setup/config.env}"
STATE_FILE="${STATE_FILE:-/var/lib/vpn-setup/state}"

# Детект контейнера (docker/lxc) — там пропускаем kernel-зависимые шаги
is_container() {
    [ -f /.dockerenv ] && return 0
    grep -qaE '(lxc|docker|kubepods|containerd)' /proc/1/cgroup 2>/dev/null && return 0
    [ "${CONTAINER_MODE:-0}" = "1" ] && return 0
    return 1
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Этот скрипт нужно запускать от root"
}

require_ubuntu() {
    [ -f /etc/os-release ] || die "Не Linux?"
    . /etc/os-release
    case "$ID" in
        ubuntu|debian) ;;
        *) die "Поддерживается только Ubuntu/Debian, найден: $ID" ;;
    esac
}

# ============================================================
# State management — что уже выполнено
# ============================================================

state_init() {
    mkdir -p "$(dirname "$STATE_FILE")"
    [ -f "$STATE_FILE" ] || : > "$STATE_FILE"
}

state_get() {
    local phase="$1"
    grep -E "^${phase}=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2 || echo ""
}

state_set() {
    local phase="$1" value="$2"
    state_init
    # Удаляем старую запись и пишем новую
    grep -vE "^${phase}=" "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null || true
    echo "${phase}=${value}" >> "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
}

is_done() { [ "$(state_get "$1")" = "done" ]; }

mark_done() { state_set "$1" "done"; }

# ============================================================
# Конфиг — читаем + auto-generate недостающее
# ============================================================

config_load() {
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        set -a; . "$CONFIG_FILE"; set +a
    fi
}

config_save() {
    mkdir -p "$(dirname "$CONFIG_FILE")"
    cat > "$CONFIG_FILE" <<EOF
# vpn-setup config — generated $(date -Iseconds)
# Регенерация секретов = удалить файл и перезапустить install.sh

SERVER_IP="$SERVER_IP"
LABEL="$LABEL"

# Панель 3x-ui
PANEL_PORT=$PANEL_PORT
PANEL_PATH="$PANEL_PATH"
PANEL_USER="$PANEL_USER"
PANEL_PASS="$PANEL_PASS"

# VLESS+Reality+Vision
VLESS_PORT=$VLESS_PORT
VLESS_SNI="$VLESS_SNI"
VLESS_UUID="$VLESS_UUID"
VLESS_PRIV="$VLESS_PRIV"
VLESS_PUB="$VLESS_PUB"
VLESS_SID="$VLESS_SID"

# Hysteria2
HY2_PORT=$HY2_PORT
HY2_SNI="$HY2_SNI"
HY2_PASSWORD="$HY2_PASSWORD"

# AmneziaWG v2
AWG_PORT=$AWG_PORT
AWG_NETWORK="$AWG_NETWORK"
AWG_SERVER_KEY="$AWG_SERVER_KEY"
AWG_SERVER_PUB="$AWG_SERVER_PUB"
AWG_CLIENT_KEY="$AWG_CLIENT_KEY"
AWG_CLIENT_PUB="$AWG_CLIENT_PUB"
AWG_CLIENT_PSK="$AWG_CLIENT_PSK"
EOF
    chmod 600 "$CONFIG_FILE"
}

config_init_defaults() {
    : "${SERVER_IP:=$(detect_public_ip)}"
    : "${LABEL:=vpn}"

    : "${PANEL_PORT:=2053}"
    : "${PANEL_PATH:=$(gen_random_hex 8)}"
    : "${PANEL_USER:=$(gen_random_alnum 10)}"
    : "${PANEL_PASS:=$(gen_random_alnum 12)}"

    : "${VLESS_PORT:=8443}"
    : "${VLESS_SNI:=www.cbr.ru}"
    : "${VLESS_UUID:=}"   # генерируется в 04-vless-reality.sh через xray uuid
    : "${VLESS_PRIV:=}"
    : "${VLESS_PUB:=}"
    : "${VLESS_SID:=}"

    : "${HY2_PORT:=443}"
    : "${HY2_SNI:=bing.com}"
    : "${HY2_PASSWORD:=$(gen_random_alnum 24)}"

    : "${AWG_PORT:=51822}"
    : "${AWG_NETWORK:=10.66.66.0/24}"
    : "${AWG_SERVER_KEY:=}"
    : "${AWG_SERVER_PUB:=}"
    : "${AWG_CLIENT_KEY:=}"
    : "${AWG_CLIENT_PUB:=}"
    : "${AWG_CLIENT_PSK:=}"
}

# ============================================================
# Утилиты
# ============================================================

gen_random_hex() {
    local n="${1:-8}"
    openssl rand -hex "$n"
}

gen_random_alnum() {
    local n="${1:-12}"
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$n"
}

detect_public_ip() {
    curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
        || curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
        || ip -o -4 route show default | awk '{print $5; exit}' \
            | xargs -I{} ip -o -4 addr show {} | awk '{print $4}' | cut -d/ -f1 | head -1
}

detect_default_iface() {
    ip -o -4 route show default | awk '{print $5; exit}'
}

apt_install() {
    DEBIAN_FRONTEND=noninteractive apt-get install -yq \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        "$@"
}

apt_update() {
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
}

# Ждём освобождения apt-lock'а (до 60 сек)
wait_for_apt() {
    local i=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
       || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
        [ $i -ge 60 ] && die "apt locked для длинной паузы; проверь unattended-upgrades"
        sleep 1; i=$((i+1))
    done
}

# Простой retry для нестабильных команд
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
