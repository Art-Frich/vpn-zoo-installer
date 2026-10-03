#!/usr/bin/env bash
# 02-kernel.sh — можно ли собрать DKMS-модуль amneziawg на этом ядре.
# Итог — подсказка для 06: AWG_ENGINE_HINT=kernel|userspace (+ AWG_ENGINE_REASON).
# HWE ставится только когда без него нельзя собрать модуль (jammy, ядро < 6.7),
# после установки — state=rebooting, install.sh просит перезагрузку.
# Миф «нужно ядро ≥ 6.2» снят: DKMS amneziawg 3.1 собирается по одним заголовкам.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
config_load
versions_load
require_ubuntu

PHASE="${ZOO_PHASE:-02-kernel}"
kernel="$(uname -r)"
kmaj="$(cut -d. -f1 <<< "$kernel")"
kmin="$(cut -d. -f2 <<< "$kernel")"
engine="${AWG_ENGINE:-auto}"

log_info "ядро: $kernel, AWG_ENGINE=$engine, ENABLE_AWG=${ENABLE_AWG:-1}"

set_hint() {
    config_set AWG_ENGINE_HINT "$1"
    config_set AWG_ENGINE_REASON "$2"
    log_ok "AmneziaWG: движок $1 — $2"
}

# Ядро, которое загрузится после reboot (самое новое из установленных)
newest_installed_kernel() {
    local k
    if command -v linux-version >/dev/null; then
        k="$(linux-version list 2>/dev/null | linux-version sort 2>/dev/null | tail -1 || true)"
    fi
    [ -n "${k:-}" ] || k="$(find /boot -maxdepth 1 -name 'vmlinuz-*' 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -1)"
    printf '%s\n' "${k:-$kernel}"
}

kernel_lt() { # kernel_lt MAJOR MINOR — текущее ядро меньше?
    [ "$kmaj" -lt "$1" ] || { [ "$kmaj" -eq "$1" ] && [ "$kmin" -lt "$2" ]; }
}

if [ "${ENABLE_AWG:-1}" != "1" ]; then
    set_hint userspace "AWG выключен (ENABLE_AWG=0)"
    mark_done "$PHASE"; exit 0
fi

if [ "$engine" = "userspace" ]; then
    set_hint userspace "задано AWG_ENGINE=userspace"
    mark_done "$PHASE"; exit 0
fi

if kernel_is_foreign; then
    [ "$engine" = "kernel" ] && die "AWG_ENGINE=kernel невозможен в контейнере/стенде: модуль ядра не загрузить"
    set_hint userspace "контейнер/стенд: ядро хоста, модули не грузим"
    mark_done "$PHASE"; exit 0
fi

# ------------------------------------------------------------
# Ядра, на которых kmod не собирается
# ------------------------------------------------------------

broken=""
if [[ "$kernel" =~ $AWG_KMOD_BROKEN_KERNELS ]]; then
    broken="ядро $kernel в списке несобираемых (amneziawg-linux-kernel-module #259)"
elif kernel_lt 5 5; then
    broken="ядро $kernel < 5.5 — header protection и strscpy не собираются"
fi
if [ -z "$broken" ] && dpkg -l 'linux-generic-hwe-24.04' 2>/dev/null | grep -q '^ii'; then
    broken="стоит linux-generic-hwe-24.04 (ветка 7.0): следующие ядра ломают DKMS amneziawg"
fi

if [ -n "$broken" ]; then
    [ "$engine" = "kernel" ] && die "AWG_ENGINE=kernel: $broken. Варианты: AWG_ENGINE=userspace или ядро 6.8 (linux-generic)"
    log_warn "$broken"
    set_hint userspace "$broken"
    mark_done "$PHASE"; exit 0
fi

# ------------------------------------------------------------
# Ubuntu 22.04: на GA 5.15 сборка не проверена — предпочитаем HWE 6.8
# ------------------------------------------------------------

if [ "$OS_VERSION_ID" = "22.04" ] && kernel_lt 6 7; then
    if [ "$(state_get "$PHASE")" = "rebooting" ]; then
        log_warn "HWE-ядро установлено, но загружено всё ещё $kernel — нужна перезагрузка: sudo reboot"
        exit 0
    fi
    if [ "${AWG_NO_HWE:-0}" = "1" ]; then
        set_hint userspace "jammy $kernel без HWE (AWG_NO_HWE=1)"
        mark_done "$PHASE"; exit 0
    fi
    log_info "jammy на ядре $kernel: ставлю linux-generic-hwe-22.04 (6.8, самый проверенный путь для DKMS amneziawg)"
    wait_for_apt
    apt_update
    apt_install linux-generic-hwe-22.04
    config_set AWG_ENGINE_HINT kernel
    config_set AWG_ENGINE_REASON "HWE 6.8 установлено, ждёт перезагрузки"
    state_set "$PHASE" rebooting
    log_warn "HWE-ядро установлено. Нужен reboot; после него запусти install.sh снова"
    exit 0
fi

# ------------------------------------------------------------
# Заголовки для текущего ядра (их поставит 06; здесь — только доступность)
# ------------------------------------------------------------

if [ ! -d "/lib/modules/$kernel/build" ] && ! apt-cache show "linux-headers-$kernel" >/dev/null 2>&1; then
    msg="в apt нет linux-headers-$kernel (нестандартное ядро хостера?)"
    [ "$engine" = "kernel" ] && die "AWG_ENGINE=kernel: $msg"
    log_warn "$msg"
    set_hint userspace "$msg"
    mark_done "$PHASE"; exit 0
fi

# Установлено ядро новее загруженного: DKMS соберётся под оба, но модуль загрузится
# только после reboot — перезагружаемся до сборки (06)
newest="$(newest_installed_kernel)"
if [ "$newest" != "$kernel" ] && [ "$(printf '%s\n%s\n' "$kernel" "$newest" | sort -V | tail -1)" = "$newest" ]; then
    if [[ "$newest" =~ $AWG_KMOD_BROKEN_KERNELS ]]; then
        log_warn "после reboot загрузится $newest — на нём DKMS amneziawg не собирается"
        [ "$engine" = "kernel" ] && die "AWG_ENGINE=kernel: следующее ядро $newest несовместимо"
        set_hint userspace "следующее ядро $newest несовместимо с DKMS amneziawg"
        mark_done "$PHASE"; exit 0
    fi
    if [ "${ZOO_NO_REBOOT:-0}" != "1" ]; then
        config_set AWG_ENGINE_HINT kernel
        config_set AWG_ENGINE_REASON "ждёт перезагрузки в $newest"
        state_set "$PHASE" rebooting
        log_warn "установлено ядро $newest, загружено $kernel — перезагрузись до сборки модуля"
        exit 0
    fi
    log_warn "ZOO_NO_REBOOT=1: модуль соберётся под $kernel, после reboot в $newest DKMS пересоберёт"
fi

set_hint kernel "ядро $kernel, заголовки доступны"
mark_done "$PHASE"
