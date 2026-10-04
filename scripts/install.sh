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
# После итога (фаза 99) — самопроверка `zoo probe --local`: работает ли каждый протокол
# в принципе, и что делать для проверки с машины пользователя. Провал протокола установку
# не прерывает. ZOO_SELFTEST=0 — без самопроверки.
#
# Флаги протоколов и параметры — через окружение, сохраняются в /etc/vpn-setup/config.env:
#   ENABLE_XHTTP=0 ENABLE_TUIC=1 AWG_ENGINE=userspace SERVER_IP=1.2.3.4 sudo -E bash scripts/install.sh
# Изменённый параметр перезапускает фазу-владельца (HY2_HOP=1 → 05, RU_EGRESS=block → 07).
# Протокол, выключенный флагом после установки (ENABLE_HY2=0), фаза выключает сама:
# сервис/inbound остановлен, порт закрыт, манифест enabled=false (state=disabled).
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

# Фазы, которые умеют выключать уже установленный протокол (запуск с флагом = 0)
phase_can_disable() {
    case "$1" in 04-*|04b-*|04c-*|04d-*|05-*|06-*|08-*) return 0 ;; esac
    return 1
}

# Фаза, которой принадлежит ключ config.env: его смена через окружение перезапускает её
phase_owns_key() {
    local phase="$1" key="$2"
    case "$phase:$key" in
        03-*:PANEL_PORT|03-*:PANEL_PATH|03-*:PANEL_USER|03-*:PANEL_2FA|03-*:SUB_PUBLIC|03-*:DOMAIN) return 0 ;;
        04-*:VLESS_*|04b-*:XHTTP_*|04c-*:SS_*|04d-*:TUIC_*) return 0 ;;
        05-*:HY2_RU_EGRESS|06-*:AWG_RU_EGRESS) return 1 ;;
        05-*:HY2_*|05-*:ENABLE_HY2_OBFS|06-*:AWG_*) return 0 ;;
        0[456]*:SERVER_IP|0[456]*:LABEL) return 0 ;;
        07-*:RU_EGRESS|07-*:HY2_RU_EGRESS|07-*:AWG_RU_EGRESS|07-*:ENABLE_BITTORRENT|07-*:ROUTING_ECHO_EXTRA|07-*:ENABLE_WARP) return 0 ;;
        08-*:ENABLE_WARP) return 0 ;;
    esac
    return 1
}

# Изменённый через окружение ключ, из-за которого фазу надо перезапустить (или пусто)
changed_key_for() {
    local k
    for k in "${ZOO_ENV_CHANGED[@]:-}"; do
        [ -n "$k" ] && phase_owns_key "$1" "$k" && { printf '%s\n' "$k"; return 0; }
    done
    return 0
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
    resolved="$(resolve_phase "$SINGLE_PHASE")" || die "--phase: нет такой фазы: $SINGLE_PHASE (см. PHASES в install.sh)"
    SINGLE_PHASE="$resolved"
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
ZOO_ENV_CHANGED=()

if [ "$DRY_RUN" = "1" ]; then
    # dry-run ничего не пишет: читаем то, что есть, и показываем план
    config_load
    log_step "dry-run: план"
else
    ( umask 077; mkdir -p "$VPN_ETC" "$LOG_DIR" )
    chmod 700 "$VPN_ETC" "$LOG_DIR"
    LOG_FILE="$LOG_DIR/install-$ZOO_RUN_TS.log"
    ( umask 077; : > "$LOG_FILE" )
    exec > >(tee -a "$LOG_FILE" 8>&-) 2>&1
    # в журналах и бэкапах ключи: держим ограниченно (ZOO_KEEP_LOGS, ZOO_KEEP_BACKUPS)
    prune_keep_newest "$LOG_DIR" 'install-*.log' "${ZOO_KEEP_LOGS:-10}" 7
    prune_keep_newest "$BACKUP_ROOT" '*' "${ZOO_KEEP_BACKUPS:-10}" 30
    config_load
    state_migrate
    config_apply_env
    config_init_defaults
    config_load
    # Фазы-владельцы изменённых ключей — pending до запуска: если прогон упадёт раньше
    # (на другой фазе), следующий запуск всё равно их пройдёт, а не оставит новое
    # значение в config.env неприменённым
    for p in "${PHASES[@]}"; do
        if is_done "$p" && [ -n "$(changed_key_for "$p")" ]; then state_set "$p" pending; fi
    done
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

# Причина пропуска фазы или пусто, если фазу надо запускать. «@disable» — фаза выключена
# флагом, но была установлена: запустить, чтобы она выключила протокол
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
    if [ -n "$flag" ] && [ "${!flag:-1}" != "1" ]; then
        if phase_can_disable "$phase" && [[ "$(state_get "$phase")" =~ ^(done|failed|pending)$ ]]; then
            echo "@disable"; return
        fi
        echo "выключена ($flag=${!flag})"; return
    fi
    [ "$RERUN" = "0" ] && is_done "$phase" || return 0
    [ -z "$(changed_key_for "$phase")" ] || return 0
    # zoo вызывает lib/proto-*.sh из своей копии в /opt/vpn-zoo: после обновления репо
    # (git pull) копия отстаёт от фаз, и 09 нужно пройти снова
    case "$phase" in
        09-*) if zoo_copy_stale; then log_info "$phase: копия zoo/scripts в ${ZOO_HOME:-/opt/vpn-zoo} устарела — обновляю" >&2; return 0; fi ;;
        # установка до появления самопроверки TUIC или новый SINGBOX_VERSION
        04d-*) if tuic_probe_client_stale; then log_info "$phase: нет sing-box ${SINGBOX_VERSION:-} для самопроверки TUIC — прохожу фазу" >&2; return 0; fi ;;
    esac
    # полный прогон: маршрутизация переприменяется после любой фазы протокола (новые
    # inbound, ACL Hysteria), итог печатается, если что-то менялось
    if [ -z "$ONLY_LIST" ]; then
        case "$phase" in
            07-*) [ "$RAN_PROTO" = "0" ] || return 0 ;;
            99-*) [ "$RAN" = "0" ] || return 0 ;;
        esac
    fi
    echo "уже выполнена (state=done)"
}

# Копия zoo/ и scripts/ (фаза 09) отличается от репозитория
zoo_copy_stale() {
    local home="${ZOO_HOME:-/opt/vpn-zoo}" d
    [ -d "$home/scripts" ] || return 1
    for d in scripts zoo; do
        diff -rq -x __pycache__ -x '*.pyc' -x tests "$REPO_ROOT/$d" "$home/$d" >/dev/null 2>&1 || return 0
    done
    return 1
}

# sing-box для самопроверки TUIC (ставит 04d) отсутствует или не той версии
tuic_probe_client_stale() {
    local bin="/usr/local/lib/vpn-zoo/bin/sing-box"
    [ "$("$bin" version 2>/dev/null | awk 'NR == 1 {print $3}')" != "${SINGBOX_VERSION#v}" ]
}

declare -A RESULT=()
RAN=0
RAN_PROTO=0

run_phase() {
    local phase="$1" script="$SCRIPTS_DIR/$1.sh" rc st flag
    if [ "$DRY_RUN" = "1" ]; then
        log_info "(dry-run) запустил бы: bash $script"
        RESULT[$phase]="dry-run"
        return 0
    fi
    [ -n "$SINGLE_PHASE" ] && state_set "$phase" ""
    # failed/disabled/pending от прошлого запуска сбрасываем: иначе успешный проход не отметится done
    # (и повторное ENABLE_*=0 потом не выключит протокол). rebooting фаза 02 читает сама
    case "$(state_get "$phase")" in failed|disabled|pending) state_set "$phase" "" ;; esac
    [ "$(state_get "$phase")" = "rebooting" ] && log_info "$phase: возврат после reboot, проверяю..."

    log_step "фаза $phase"
    RAN=$((RAN + 1))
    set +e
    # bash явно: после git clone на Windows бит +x может потеряться.
    # 8>&- — фаза не наследует fd блокировки: запущенный ею фоновый процесс иначе
    # держал бы /run/vpn-setup.lock и следующий install.sh считал бы себя занятым
    ZOO_PHASE="$phase" bash "$script" 8>&-
    rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        RESULT[$phase]="FAIL (rc=$rc)"
        # не done: следующий запуск пройдёт фазу снова, а не пропустит её с неприменённым
        # (или неверным) значением из окружения, которое уже записано в config.env
        state_set "$phase" failed
        log_err "$phase: ошибка (rc=$rc). Лог: ${LOG_FILE:-stdout}"
        log_err "после исправления причины запусти install.sh снова — фаза $phase пройдёт заново"
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
    flag="$(phase_flag "$phase")"
    if [ -n "$flag" ] && [ "${!flag:-1}" != "1" ]; then
        # фаза выключила свой протокол: при ENABLE_*=1 она снова пройдёт целиком
        state_set "$phase" disabled
        RESULT[$phase]="выключена ($flag=0)"
        log_ok "$phase: протокол выключен"
    else
        [ -n "$st" ] || mark_done "$phase"
        RESULT[$phase]="OK"
        log_ok "$phase: готово"
    fi
    case "$phase" in 04*|05-*|06-*|08-*) RAN_PROTO=1 ;; esac
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
    if [ "$reason" = "@disable" ]; then
        log_info "$phase: $(phase_flag "$phase")=0 — выключаю установленный протокол"
        run_phase "$phase"
        continue
    fi
    k="$(changed_key_for "$phase")"
    if [ -z "$reason" ] && [ "$(state_get "$phase")" = pending ]; then
        log_info "$phase: изменены её параметры${k:+ ($k)} — перезапуск фазы"
    fi
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

# Самопроверка (ARCHITECTURE §7): клиент каждого протокола поднимается на самом сервере.
# Итог — в /var/lib/vpn-zoo/probe-local.json (админка, «Проверка») и в пакете для
# клиентского пробника. Провал протокола подсвечивается, но установку не валит
selftest() {
    [ "${RESULT[99-print-creds]:-}" = "OK" ] && [ -z "$SINGLE_PHASE" ] || return 0
    if [ "${ZOO_SELFTEST:-1}" != "1" ]; then
        log_info "самопроверка пропущена (ZOO_SELFTEST=${ZOO_SELFTEST}); вручную: sudo zoo probe --local --summary"
        return 0
    fi
    if ! command -v zoo >/dev/null; then
        log_warn "zoo не установлен (ENABLE_ZOO=0?) — самопроверки протоколов не будет"
        return 0
    fi
    local color=0 rc=0
    [ -z "$C_RED" ] || color=1
    log_step "самопроверка: работает ли протокол в принципе"
    log_info "клиент каждого протокола поднимается на самом сервере (1–3 минуты)"
    ZOO_COLOR="$color" timeout 900 zoo probe --local --summary --export "$PROBE_EXPORT_FILE" 8>&- || rc=$?
    echo
    case "$rc" in
        0) log_ok "самопроверка: все протоколы работают в принципе" ;;
        1) log_warn "самопроверка: часть протоколов НЕ работает на самом сервере (выше). Установка при этом завершена" ;;
        *) log_warn "самопроверка не завершилась (код $rc): sudo zoo probe --local --summary" ;;
    esac
    return 0
}
PROBE_EXPORT_FILE="${PROBE_EXPORT:-$VPN_ETC/probe-export.json}"
selftest

log_step "ГОТОВО"
log_ok "Установка завершена. Конфиг и секреты: $CONFIG_FILE"
