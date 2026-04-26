#!/usr/bin/env bash
# install.sh — orchestrator для установки VPN-зоопарка
# Запускать на чистой Ubuntu 22.04/24.04 от root
#
# Usage:
#   ./install.sh                      # обычный запуск (читает state, продолжает с места остановки)
#   ./install.sh --phase 03-3xui      # запустить только конкретную фазу
#   ./install.sh --reset              # очистить state (но не uninstall) и начать заново
#   ./install.sh --dry-run            # показать что будет делать, не выполнять
#   CONTAINER_MODE=1 ./install.sh     # режим тестирования в Docker (пропускать kernel-зависимые шаги)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"

# shellcheck source=lib.sh
. "$SCRIPTS_DIR/lib.sh"

# ============================================================
# Парсинг аргументов
# ============================================================

DRY_RUN=0
SINGLE_PHASE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1; shift ;;
        --phase)      SINGLE_PHASE="$2"; shift 2 ;;
        --reset)      rm -f "$STATE_FILE"; log_ok "state cleared"; exit 0 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) die "unknown arg: $1" ;;
    esac
done

# ============================================================
# Pre-flight
# ============================================================

require_root
require_ubuntu
state_init
config_load
config_init_defaults
config_save

if is_container; then
    log_warn "запущено в контейнере — kernel-зависимые шаги (HWE, AmneziaWG) будут пропущены"
fi

log_info "config файл: $CONFIG_FILE"
log_info "state файл: $STATE_FILE"
log_info "SERVER_IP: $SERVER_IP"

# ============================================================
# Фазы
# ============================================================

PHASES=(
    "00-bootstrap"
    "01-firewall"
    "02-kernel-hwe"
    "03-3xui"
    "04-vless-reality"
    "05-hysteria2"
    "06-amneziawg"
    "99-print-creds"
)

run_phase() {
    local phase="$1"
    local script="$SCRIPTS_DIR/${phase}.sh"

    if [ ! -x "$script" ]; then
        log_warn "$phase: скрипт не найден или не executable: $script — пропуск"
        return 0
    fi

    if is_done "$phase"; then
        log_info "$phase: уже выполнено (state=done) — пропуск"
        return 0
    fi

    local current_state
    current_state="$(state_get "$phase")"
    if [ "$current_state" = "rebooting" ]; then
        # Эта фаза попросила reboot, и мы только что вернулись
        log_info "$phase: возврат после reboot, проверяю..."
    fi

    log_step "running $phase"

    if [ "$DRY_RUN" = "1" ]; then
        log_info "(dry-run) $script"
        return 0
    fi

    # Запуск саб-скрипта в дочернем shell, наследуя env и lib.sh
    if bash -c "set -e; . '$SCRIPTS_DIR/lib.sh'; . '$script'"; then
        # Если скрипт сам не пометил состояние — ставим done
        local final_state
        final_state="$(state_get "$phase")"
        if [ -z "$final_state" ] || [ "$final_state" = "rebooting" ] && [ "$final_state" != "done" ]; then
            : # rebooting сохраняется, скрипт сам разберётся
        fi
        if [ -z "$(state_get "$phase")" ]; then
            mark_done "$phase"
        fi
        log_ok "$phase: done"
    else
        log_err "$phase: failed"
        exit 1
    fi

    # Если фаза попросила reboot — останавливаем pipeline
    if [ "$(state_get "$phase")" = "rebooting" ]; then
        echo
        log_warn "Фаза $phase обновила ядро. Нужен reboot."
        log_warn "Выполни: sudo reboot"
        log_warn "После reboot снова запусти: $0"
        exit 0
    fi
}

# ============================================================
# Главная логика
# ============================================================

if [ -n "$SINGLE_PHASE" ]; then
    # Принудительный запуск конкретной фазы (state очищается)
    state_set "$SINGLE_PHASE" ""
    run_phase "$SINGLE_PHASE"
else
    for phase in "${PHASES[@]}"; do
        run_phase "$phase"
    done
fi

log_step "ВСЁ ГОТОВО"
log_ok "Установка завершена. Клиентские ссылки выше / в $CONFIG_FILE"
