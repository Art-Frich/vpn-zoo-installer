#!/usr/bin/env bash
# install.sh — оркестратор установки VPN-зоопарка (Ubuntu 22.04/24.04, root)
#
# Запуск:
#   sudo bash scripts/install.sh                 # все фазы; выполненные (state=done) пропускаются
#   sudo bash scripts/install.sh --only 00,01,03 # только эти фазы (номер или полное имя)
#   sudo bash scripts/install.sh --skip 06       # все, кроме этих
#   sudo bash scripts/install.sh --phase 03-3xui # одна фаза принудительно (state игнорируется)
#   sudo bash scripts/install.sh --rerun         # перезапустить и выполненные фазы
#   sudo bash scripts/install.sh --dry-run       # показать план, ничего не менять
#   sudo bash scripts/install.sh --reset         # очистить state (не удаляет установленное)
#   sudo bash scripts/install.sh --force         # разрешить перезапись чужой установки (с бэкапом)
#
# Флаги протоколов и параметры — через окружение, сохраняются в /etc/vpn-setup/config.env:
#   ENABLE_XHTTP=0 ENABLE_TUIC=1 AWG_ENGINE=userspace SERVER_IP=1.2.3.4 sudo -E bash scripts/install.sh
# Тестовый стенд: ZOO_TEST_ENV=docker (см. docker/README.md).

set -euo pipefail
umask 022

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"
export REPO_ROOT SCRIPTS_DIR

# shellcheck source=lib.sh
. "$SCRIPTS_DIR/lib.sh"

# Порядок фаз (ARCHITECTURE §2). Фазы, которой нет на диске, пропускается.
PHASES=(
    "00-bootstrap"
    "01-firewall"
    "02-kernel"
    "03-3xui"
    "04-vless-reality"
    "04b-vless-xhttp"
    "04c-ss2022"
    "04d-tuic"
    "05-hysteria2"
    "06-amneziawg"
    "07-routing"
    "08-warp"
    "09-zoo"
    "99-print-creds"
)

# Фаза → флаг включения в config.env (фазы без флага включены всегда)
phase_flag() {
    case "$1" in
        04-*) echo ENABLE_VLESS ;;
        04b-*) echo ENABLE_XHTTP ;;
        04c-*) echo ENABLE_SS ;;
        04d-*) echo ENABLE_TUIC ;;
        05-*) echo ENABLE_HY2 ;;
        06-*) echo ENABLE_AWG ;;
        08-*) echo ENABLE_WARP ;;
        09-*) echo ENABLE_ZOO ;;
        *) echo "" ;;
    esac
}

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

# ============================================================
# Аргументы
# ============================================================

DRY_RUN=0
RERUN=0
SINGLE_PHASE=""
ONLY_LIST=""
SKIP_LIST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY_RUN=1; shift ;;
        --rerun)    RERUN=1; shift ;;
        --force)    export ZOO_FORCE=1; shift ;;
        --phase)    [ $# -ge 2 ] || die "--phase: нужно имя фазы"; SINGLE_PHASE="$2"; shift 2 ;;
        --only)     [ $# -ge 2 ] || die "--only: нужен список"; ONLY_LIST="$2"; shift 2 ;;
        --skip)     [ $# -ge 2 ] || die "--skip: нужен список"; SKIP_LIST="$2"; shift 2 ;;
        --reset)
            require_root
            rm -f "$STATE_FILE"
            log_ok "state очищен ($STATE_FILE). Установленное не удалялось."
            exit 0 ;;
        -h|--help)  usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
done

# Номер фазы — часть имени до первого «-»: 04, 04b, 99
phase_id() { printf '%s\n' "${1%%-*}"; }

# Есть ли фаза в списке через запятую (по номеру или полному имени)
in_list() {
    local phase="$1" list=",${2// /},"
    case "$list" in
        *",$phase,"*|*",$(phase_id "$phase"),"*) return 0 ;;
    esac
    return 1
}

resolve_phase() {
    local want="$1" p
    for p in "${PHASES[@]}"; do
        if [ "$p" = "$want" ] || [ "$(phase_id "$p")" = "$want" ]; then
            printf '%s\n' "$p"; return 0
        fi
    done
    # старое имя
    [ "$want" = "02-kernel-hwe" ] && { echo "02-kernel"; return 0; }
    return 1
}

if [ -n "$SINGLE_PHASE" ]; then
    SINGLE_PHASE="$(resolve_phase "$SINGLE_PHASE")" || die "--phase: нет такой фазы: $SINGLE_PHASE (см. PHASES в install.sh)"
fi

# ============================================================
# Pre-flight
# ============================================================

require_root
require_ubuntu
detect_arch >/dev/null
versions_load
export ZOO_ARCH
export ZOO_RUN_TS="${ZOO_RUN_TS:-$(date +%Y%m%d-%H%M%S)}"

# Один инсталлер за раз
exec 8>/run/vpn-setup.lock
flock -n 8 || die "install.sh уже запущен (блокировка /run/vpn-setup.lock)"

config_capture_env

if [ "$DRY_RUN" = "1" ]; then
    # dry-run ничего не пишет: читаем то, что есть, и показываем план
    config_load
    log_step "dry-run: план"
else
    ( umask 077; mkdir -p "$VPN_ETC" "$LOG_DIR" )
    chmod 700 "$VPN_ETC" "$LOG_DIR"
    LOG_FILE="$LOG_DIR/install-$ZOO_RUN_TS.log"
    ( umask 077; : > "$LOG_FILE" )
    exec > >(tee -a "$LOG_FILE") 2>&1
    state_init
    config_load
    config_apply_env
    config_init_defaults
    config_load
fi

if is_test_env; then
    log_warn "тестовый стенд (ZOO_TEST_ENV=docker): без модулей ядра, swap, глобальных sysctl и reboot"
elif is_container; then
    log_warn "запущено в контейнере — kernel-зависимые шаги (HWE, AWG kmod) будут пропущены"
fi

if ! command -v systemctl >/dev/null || [ "$(ps -o comm= -p 1 2>/dev/null)" != "systemd" ]; then
    log_warn "PID 1 — не systemd: сервисы не запустятся. Нужен VPS с systemd"
fi

avail_mb="$(df -Pm /usr/local 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "$avail_mb" ] && [ "$avail_mb" -lt 1024 ]; then
    log_warn "на /usr/local свободно ${avail_mb} МБ — нужно около 1 ГБ (3x-ui ≈ 350 МБ с geo-файлами)"
fi

log_info "Ubuntu ${OS_VERSION_ID} ${ZOO_ARCH}, ядро $(uname -r)"
log_info "SERVER_IP: ${SERVER_IP:-?}   config: $CONFIG_FILE   state: $STATE_FILE"
[ "$DRY_RUN" = "1" ] || log_info "лог: $LOG_FILE"

# ============================================================
# Выбор фаз
# ============================================================

# Причина пропуска фазы или пусто, если фазу надо запускать
skip_reason() {
    local phase="$1" flag
    if [ -n "$SINGLE_PHASE" ]; then
        [ "$phase" = "$SINGLE_PHASE" ] || { echo "не выбрана (--phase)"; return; }
        [ -f "$SCRIPTS_DIR/$phase.sh" ] || { echo "нет файла"; return; }
        return
    fi
    [ -f "$SCRIPTS_DIR/$phase.sh" ] || { echo "нет файла"; return; }
    if [ -n "$ONLY_LIST" ] && ! in_list "$phase" "$ONLY_LIST"; then echo "не в --only"; return; fi
    if [ -n "$SKIP_LIST" ] && in_list "$phase" "$SKIP_LIST"; then echo "--skip"; return; fi
    flag="$(phase_flag "$phase")"
    if [ -n "$flag" ] && [ "${!flag:-1}" != "1" ]; then echo "выключена ($flag=${!flag})"; return; fi
    if [ "$RERUN" = "0" ] && is_done "$phase"; then echo "уже выполнена (state=done)"; return; fi
}

declare -A RESULT=()
RAN=0

run_phase() {
    local phase="$1" script="$SCRIPTS_DIR/$1.sh" rc st
    if [ "$DRY_RUN" = "1" ]; then
        log_info "(dry-run) запустил бы: bash $script"
        RESULT[$phase]="dry-run"
        return 0
    fi
    [ -n "$SINGLE_PHASE" ] && state_set "$phase" ""
    [ "$(state_get "$phase")" = "rebooting" ] && log_info "$phase: возврат после reboot, проверяю..."

    log_step "фаза $phase"
    RAN=$((RAN + 1))
    set +e
    # bash явно: после git clone на Windows бит +x может потеряться
    ZOO_PHASE="$phase" bash "$script"
    rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        RESULT[$phase]="FAIL (rc=$rc)"
        log_err "$phase: ошибка (rc=$rc). Лог: ${LOG_FILE:-stdout}"
        print_summary
        exit 1
    fi

    st="$(state_get "$phase")"
    if [ "$st" = "rebooting" ]; then
        RESULT[$phase]="нужен reboot"
        print_summary
        echo
        log_warn "Фаза $phase требует перезагрузки: sudo reboot"
        log_warn "После reboot снова запусти: sudo bash $0"
        exit 0
    fi
    [ -n "$st" ] || mark_done "$phase"
    RESULT[$phase]="OK"
    log_ok "$phase: готово"
}

print_summary() {
    local p
    echo
    log_step "итог"
    for p in "${PHASES[@]}"; do
        [ -n "${RESULT[$p]:-}" ] || continue
        printf '  %-18s %s\n' "$p" "${RESULT[$p]}"
    done
}

for phase in "${PHASES[@]}"; do
    reason="$(skip_reason "$phase")"
    if [ -n "$reason" ]; then
        RESULT[$phase]="пропуск: $reason"
        [ "$reason" = "не выбрана (--phase)" ] && unset 'RESULT[$phase]'
        continue
    fi
    run_phase "$phase"
done

print_summary

if [ -n "$SINGLE_PHASE" ] && [[ "${RESULT[$SINGLE_PHASE]:-}" == "пропуск: нет файла" ]]; then
    die "фаза $SINGLE_PHASE: нет файла $SCRIPTS_DIR/$SINGLE_PHASE.sh"
fi

if [ "$DRY_RUN" = "1" ]; then
    log_ok "dry-run: изменений не было"
    exit 0
fi

if [ "$RAN" -eq 0 ]; then
    log_warn "ни одна фаза не запускалась (все пропущены). Перезапуск выполненных: --rerun"
    exit 0
fi

log_step "ГОТОВО"
log_ok "Установка завершена. Конфиг и секреты: $CONFIG_FILE"
