#!/usr/bin/env bash
# 02-kernel-hwe.sh — установка HWE kernel (для AmneziaWG v2 нужно ядро >= 6.2)
#
# Логика:
#   1. uname -r >= 6.x → already OK, mark done.
#   2. < 6.x → apt install linux-generic-hwe-22.04, state="rebooting", выход.
#   3. После reboot: install.sh снова запускает эту фазу,
#      uname -r >= 6.x, mark done, продолжаем дальше.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if is_container; then
    log_warn "контейнер — HWE kernel пропущен (kernel shared с хостом, DKMS не работает)"
    log_warn "AmneziaWG v2 нельзя установить в контейнере — нужен реальный VPS"
    mark_done "02-kernel-hwe"
    return 0 2>/dev/null || exit 0
fi

# ------------------------------------------------------------
# 1. Проверяем текущее ядро
# ------------------------------------------------------------

current_kernel="$(uname -r)"
log_info "текущее ядро: $current_kernel"

# Парсим major.minor
kernel_major="$(echo "$current_kernel" | cut -d. -f1)"
kernel_minor="$(echo "$current_kernel" | cut -d. -f2)"

if [ "$kernel_major" -gt 6 ] || { [ "$kernel_major" -eq 6 ] && [ "$kernel_minor" -ge 2 ]; }; then
    log_ok "ядро $current_kernel — устраивает (>=6.2), HWE не требуется"
    mark_done "02-kernel-hwe"
    return 0 2>/dev/null || exit 0
fi

log_info "ядро < 6.2 — нужен HWE"

# ------------------------------------------------------------
# 2. Если state уже "rebooting" значит мы уже устанавливали и юзер не ребутнул
# ------------------------------------------------------------

if [ "$(state_get "02-kernel-hwe")" = "rebooting" ]; then
    log_warn "HWE-пакет уже установлен, но ядро всё ещё $current_kernel"
    log_warn "Ты не сделал reboot? Выполни: sudo reboot"
    log_warn "После reboot снова запусти install.sh"
    return 0 2>/dev/null || exit 0
fi

# ------------------------------------------------------------
# 3. Устанавливаем HWE kernel
# ------------------------------------------------------------

. /etc/os-release
case "$VERSION_ID" in
    "22.04") HWE_PKG="linux-generic-hwe-22.04" ;;
    "24.04") log_ok "Ubuntu 24.04 уже идёт с ядром 6.x — HWE не нужен"; mark_done "02-kernel-hwe"; return 0 2>/dev/null || exit 0 ;;
    *) die "Поддерживается только Ubuntu 22.04/24.04, найден: $VERSION_ID" ;;
esac

log_info "устанавливаю $HWE_PKG"
wait_for_apt
apt_update
apt_install "$HWE_PKG"

# Помечаем "rebooting" — install.sh увидит и попросит reboot
state_set "02-kernel-hwe" "rebooting"

log_warn "HWE-ядро установлено. Нужен REBOOT для активации."
log_warn "После reboot ядро будет: 6.8.x"

return 0 2>/dev/null || exit 0
