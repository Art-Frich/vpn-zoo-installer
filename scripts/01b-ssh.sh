#!/usr/bin/env bash
# 01b-ssh.sh — закрыть SSH (opt-in, SSH_HARDEN=1): sshd на новом порту, вход только по ключу,
# root — только по ключу. Два шага, чтобы не потерять доступ (DECISIONS D30):
#
#   1. SSH_HARDEN=1: sshd слушает старый И новый порт, пароли выключены, взведён таймер
#      отката (SSH_REVERT_MIN минут, по умолчанию 10; таймер переживает reboot).
#   2. SSH_CONFIRM=1 --phase 01b из НОВОЙ SSH-сессии на новом порту: старый порт закрыт
#      (sshd, ufw, fail2ban), таймер снят.
#   Без подтверждения таймер сам возвращает прежнее состояние sshd, ufw и fail2ban.
#   SSH_HARDEN=0 на закрытом сервере возвращает исходные порт и настройки sshd.
#
#   SSH_PORT=N|random|keep    новый порт: число, случайный высокий (по умолчанию) или текущий
#                             (keep — порт не меняется, только вход по ключу)
#   SSH_CONFIRM_FORCE=1       подтвердить не из SSH-сессии на новом порту (консоль хостера)
#
# Служебные режимы: --revert (таймер отката), --remind (конец install.sh: инструкция ещё
# раз, отсчёт таймера заново).

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_root
config_load

MODE="${1:-apply}"

SSHD_MAIN=/etc/ssh/sshd_config
SSHD_DIR=/etc/ssh/sshd_config.d
# Имя 00-…: раньше 50-cloud-init.conf и прочих — в sshd действует первое значение параметра
SSHD_DROPIN="$SSHD_DIR/00-vpn-zoo.conf"
SOCKET_DROPIN=/etc/systemd/system/ssh.socket.d/vpn-zoo.conf
SOCKET_GENERATORS="/usr/lib/systemd/system-generators/sshd-socket-generator /lib/systemd/system-generators/sshd-socket-generator"
F2B_JAIL=/etc/fail2ban/jail.d/vpn-zoo.local
PORT_MARK='#vpn-zoo# '
REVERT_UNIT=vpn-zoo-ssh-revert
ORIG_ENV="$SSH_STATE_DIR/orig.env"       # исходное состояние (с первого шага 1 до SSH_HARDEN=0)
MANAGED_ENV="$SSH_STATE_DIR/managed.env" # подтверждённый порт
PENDING_ENV="$SSH_STATE_DIR/pending.env" # шаг 1 применён, ждёт подтверждения
UNWIND_ENV="$SSH_STATE_DIR/unwind.env"   # SSH_HARDEN=0: вход исходный, порт фазы ещё открыт
PREV_SNAP="$SSH_STATE_DIR/prev"          # /etc/ssh и drop-in сокета до шага 1
BIN_COPY="$SSH_STATE_DIR/bin"            # копия скрипта для таймера (git pull его не изменит)

# ============================================================
# Состояние
# ============================================================

# env_write FILE KEY=VALUE... — значения через %q, файл читается source
env_write() {
    local f="$1" tmp kv
    shift
    ( umask 077; mkdir -p "$SSH_STATE_DIR" )
    tmp="$(mktemp "$f.XXXXXX")"
    for kv in "$@"; do printf '%s=%q\n' "${kv%%=*}" "${kv#*=}"; done > "$tmp"
    mv -f "$tmp" "$f"
}

# shellcheck source=/dev/null
env_read() { . "$1"; }

# Ключи файлов состояния (env_read): orig.env, managed.env, pending.env
ORIG_PORTS=""; ORIG_LOGIN_PORT=""; ROOT_POLICY=""; PORT=""; SPEC=""
NEW_PORT=""; CUR_PORTS=""; PREV_PORTS=""; PREV_LOGIN_PORT=""; PREV_MANAGED=""; PREV_SPEC=""
APPLIED_AT=0; DEADLINE=0; REVERT_MIN=""; LOGIN_USER=""; OWNER_IP=""

# Шаг 2 был: SSH закрыт фазой и подтверждён
is_managed() { [ -f "$MANAGED_ENV" ]; }

revert_min() {
    local m="${SSH_REVERT_MIN:-${REVERT_MIN:-10}}"
    [[ "$m" =~ ^[0-9]+$ ]] && [ "$m" -ge 1 ] && [ "$m" -le 120 ] || die "SSH_REVERT_MIN=$m: число минут от 1 до 120"
    printf '%s\n' "$m"
}

# Список портов «a,b,c» без повторов, по возрастанию
csv_ports() { tr -s ', ' '\n' <<< "$*" | awk '/^[0-9]+$/' | sort -un | paste -sd, -; }

# ============================================================
# sshd: модель запуска, эффективные значения, перезапуск
# ============================================================

# socket — Ubuntu 24.04 (ssh.socket держит порт, Port из sshd_config читает генератор);
# service — классический ssh.service (22.04)
ssh_model() {
    if systemctl is-active --quiet ssh.socket 2>/dev/null \
       || [ "$(systemctl is-enabled ssh.socket 2>/dev/null || true)" = "enabled" ]; then
        echo socket
    else
        echo service
    fi
}

has_generator() {
    local g
    for g in $SOCKET_GENERATORS; do [ -x "$g" ] && return 0; done
    return 1
}

# Значение параметра из sshd -T (несколько строк для Port)
sshd_eff() {
    local out
    out="$(sshd -T 2>/dev/null)" || return 0
    awk -v k="$1" '$1 == k {$1 = ""; sub(/^ /, ""); print}' <<< "$out"
}

sshd_files() {
    local f
    printf '%s\n' "$SSHD_MAIN"
    for f in "$SSHD_DIR"/*.conf; do
        [ -f "$f" ] && [ "$f" != "$SSHD_DROPIN" ] && printf '%s\n' "$f"
    done
    return 0
}

# Строки Port в чужих файлах комментируются меткой: Port в sshd накапливается из всех
# файлов, и старый порт иначе не закрыть. SSH_HARDEN=0 снимает метку
ports_comment() {
    local f
    while IFS= read -r f; do
        grep -qiE '^[[:space:]]*port[[:space:]]' "$f" || continue
        backup_path "$f" >/dev/null
        sed -i -E "s/^([[:space:]]*[Pp][Oo][Rr][Tt][[:space:]].*)$/${PORT_MARK}\\1/" "$f"
        log_info "$f: строки Port закомментированы (порты задаёт $SSHD_DROPIN)"
    done < <(sshd_files)
}

ports_uncomment() {
    local f
    while IFS= read -r f; do
        grep -q "^${PORT_MARK}" "$f" || continue
        sed -i "s/^${PORT_MARK}//" "$f"
        log_info "$f: строки Port возвращены"
    done < <(sshd_files)
}

dropin_content() {
    local p
    echo "# vpn-zoo, фаза 01b (SSH_HARDEN=1): порт SSH и вход только по ключу."
    echo "# Файл перезаписывается фазой. Вернуть как было: SSH_HARDEN=0 install.sh --phase 01b"
    echo "# Имя 00-…: раньше 50-cloud-init.conf, в sshd действует первое значение параметра"
    for p in "$@"; do echo "Port $p"; done
    echo "PasswordAuthentication no"
    echo "KbdInteractiveAuthentication no"
    echo "PermitRootLogin $ROOT_POLICY"
    echo "MaxAuthTries 3"
    echo "LoginGraceTime 20"
    echo "X11Forwarding no"
}

# Ubuntu 24.04: sshd-socket-generator сам строит ListenStream из Port в sshd_config (и
# учитывает ListenAddress) — свой drop-in только там, где генератора нет
socket_dropin_write() {
    local p v6only
    if [ "$MODEL" != "socket" ] || has_generator; then
        rm -f "$SOCKET_DROPIN"
        rmdir "${SOCKET_DROPIN%/*}" 2>/dev/null || true
        return 0
    fi
    v6only="$(systemctl show ssh.socket -p BindIPv6Only --value 2>/dev/null || true)"
    mkdir -p "${SOCKET_DROPIN%/*}"
    {
        echo "# vpn-zoo, фаза 01b: порты SSH для ssh.socket (sshd-socket-generator нет)"
        echo "[Socket]"
        echo "ListenStream="
        for p in "$@"; do
            if [ "$v6only" = "ipv6-only" ]; then
                echo "ListenStream=0.0.0.0:$p"
                echo "ListenStream=[::]:$p"
            else
                echo "ListenStream=$p"
            fi
        done
    } > "$SOCKET_DROPIN"
}

ssh_restart() {
    local pid
    mkdir -p /run/sshd && chmod 0755 /run/sshd
    systemctl daemon-reload
    if [ "$MODEL" = "socket" ]; then
        pid="$(systemctl show ssh.service -p MainPID --value 2>/dev/null || true)"
        systemctl restart ssh.socket || return 1
        # ssh.service (Requires=ssh.socket) обычно перезапускается вместе с сокетом; если нет —
        # он работает со старым конфигом
        if systemctl is-active --quiet ssh.service \
           && [ "$(systemctl show ssh.service -p MainPID --value 2>/dev/null)" = "$pid" ]; then
            systemctl restart ssh.service || return 1
        fi
    else
        # KillMode=process: открытые сессии (в том числе эта) переживают перезапуск
        systemctl restart ssh.service || return 1
    fi
}

# Отвечает ли порт баннером SSH (loopback ufw не трогает)
ssh_banner() {
    local a
    for a in 127.0.0.1 ::1 "${SERVER_IP:-}"; do
        [ -n "$a" ] || continue
        # shellcheck disable=SC2016 # $1/$2 раскрывает вложенный bash
        timeout 6 bash -c 'exec 3<>"/dev/tcp/$1/$2" || exit 1
            IFS= read -r -t 5 l <&3 || exit 1
            printf "SSH-2.0-vpnzoo-check\r\n" >&3 2>/dev/null
            [[ "$l" == SSH-* ]]' _ "$a" "$1" 2>/dev/null && return 0
    done
    return 1
}

wait_banner() {
    for _ in $(seq 1 15); do
        ssh_banner "$1" && return 0
        sleep 1
    done
    return 1
}

# Итоговые значения sshd совпадают с задуманными (Include стоит в начале sshd_config,
# никто не перебил наш drop-in)
verify_effective() {
    local got want
    got="$(sshd_eff passwordauthentication)"
    [ "$got" = "no" ] || { log_err "sshd: PasswordAuthentication=$got, ожидалось no (Include $SSHD_DIR не в начале $SSHD_MAIN?)"; return 1; }
    got="$(sshd_eff kbdinteractiveauthentication)"
    [ "$got" = "no" ] || { log_err "sshd: KbdInteractiveAuthentication=$got, ожидалось no"; return 1; }
    got="$(sshd_eff permitrootlogin)"
    [ "${got/without-password/prohibit-password}" = "$ROOT_POLICY" ] || { log_err "sshd: PermitRootLogin=$got, ожидалось $ROOT_POLICY"; return 1; }
    got="$(sshd_eff port | sort -n | paste -sd, -)"
    want="$(csv_ports "$*")"
    [ "$got" = "$want" ] || { log_err "sshd: порты $got, ожидались $want"; return 1; }
}

# Блоки Match могут вернуть пароль отдельным пользователям или адресам: sshd -T без -C их
# не учитывает. Проверка — для каждого пользователя с shell и каждого порта с внешнего
# адреса; найденное — предупреждение (Match — осознанная настройка владельца)
match_check() {
    local f has=0 user shell uid p out bad=""
    while IFS= read -r f; do
        grep -qiE '^[[:space:]]*Match[[:space:]]' "$f" && has=1
    done < <(sshd_files)
    [ "$has" = 1 ] || return 0
    while IFS=: read -r user _ uid _ _ _ shell; do
        case "$shell" in */nologin|*/false|"") continue ;; esac
        [ "$uid" = "0" ] || [ "$uid" -ge 1000 ] 2>/dev/null || continue
        for p in "$@"; do
            out="$(sshd -T -C "user=$user,host=scan.example,addr=203.0.113.7,laddr=${SERVER_IP:-0.0.0.0},lport=$p" 2>/dev/null)" || continue
            if grep -qxE '(passwordauthentication|kbdinteractiveauthentication) yes' <<< "$out" \
               || { [ "$uid" = "0" ] && grep -qx 'permitrootlogin yes' <<< "$out"; }; then
                bad="$bad $user:$p"
            fi
        done
    done < <(getent passwd)
    [ -z "$bad" ] || log_warn "блоки Match в конфиге sshd оставляют вход по паролю (пользователь:порт):$bad — уберите PasswordAuthentication/KbdInteractiveAuthentication/PermitRootLogin yes из Match, если это не задумано"
}

# render_managed ПОРТ... — наш drop-in, чужие Port закомментированы, sshd перезапущен,
# каждый порт отвечает баннером. Ошибка — rc 1 (откат делает вызывающий)
render_managed() {
    local p tmp
    tmp="$(mktemp)"
    dropin_content "$@" > "$tmp"
    mkdir -p "$SSHD_DIR"
    install -m 644 "$tmp" "$SSHD_DROPIN"
    rm -f "$tmp"
    ports_comment
    socket_dropin_write "$@"
    mkdir -p /run/sshd && chmod 0755 /run/sshd
    sshd -t || { log_err "sshd -t: конфиг не прошёл проверку"; return 1; }
    verify_effective "$@" || return 1
    match_check "$@"
    ssh_restart || { log_err "перезапуск sshd не удался"; return 1; }
    for p in "$@"; do
        wait_banner "$p" || { log_err "порт $p не отвечает баннером SSH"; return 1; }
    done
}

# ============================================================
# Снимки /etc/ssh и drop-in сокета
# ============================================================

snap_take() {
    local d="$1"
    rm -rf "$d"
    ( umask 077; mkdir -p "$d/sshd_config.d" )
    cp -a "$SSHD_MAIN" "$d/sshd_config"
    [ ! -d "$SSHD_DIR" ] || cp -a "$SSHD_DIR/." "$d/sshd_config.d/"
    [ ! -f "$SOCKET_DROPIN" ] || cp -a "$SOCKET_DROPIN" "$d/socket.conf"
}

snap_restore() {
    local d="$1"
    [ -f "$d/sshd_config" ] || return 1
    cp -a "$d/sshd_config" "$SSHD_MAIN"
    mkdir -p "$SSHD_DIR"
    find "$SSHD_DIR" -mindepth 1 -maxdepth 1 -delete
    cp -a "$d/sshd_config.d/." "$SSHD_DIR/"
    if [ -f "$d/socket.conf" ]; then
        mkdir -p "${SOCKET_DROPIN%/*}"
        cp -a "$d/socket.conf" "$SOCKET_DROPIN"
    else
        rm -f "$SOCKET_DROPIN"
        rmdir "${SOCKET_DROPIN%/*}" 2>/dev/null || true
    fi
}

# rollback_to СНИМОК ПОРТ... — вернуть sshd к снимку и дождаться портов
rollback_to() {
    local d="$1" p
    shift
    log_warn "возвращаю sshd к состоянию до изменений"
    snap_restore "$d" || { log_err "нет снимка $d"; return 1; }
    ssh_restart || log_err "sshd не перезапустился после отката — проверь: systemctl status ssh"
    for p in "$@"; do
        wait_banner "$p" || log_err "после отката порт $p не отвечает — проверь: systemctl status ssh ssh.socket"
    done
}

# ============================================================
# ufw, fail2ban, config.env, маршрутизация, CREDENTIALS.md
# ============================================================

f2b_ports() {
    [ -f "$F2B_JAIL" ] || { log_warn "нет $F2B_JAIL — fail2ban не настроен фазой 01"; return 0; }
    sed -i -E "s/^port([[:space:]]*)=.*/port\\1= $1/" "$F2B_JAIL"
    if systemctl is-active --quiet fail2ban; then
        fail2ban-client reload >/dev/null 2>&1 || log_warn "fail2ban-client reload не удался (jail sshd: порты $1)"
    fi
}

# f2b_owner add|del IP — пока шаг 1 ждёт подтверждения, адрес владельца fail2ban не банит:
# пробы ключей на новом порту (MaxAuthTries 3, «Too many authentication failures») иначе
# закрыли бы ему SSH на час. Только в памяти fail2ban: reload и перезапуск его сбрасывают
f2b_owner() {
    local j
    [ -n "$2" ] && systemctl is-active --quiet fail2ban || return 0
    ! peer_via_server "$2" || return 0
    for j in sshd recidive; do
        fail2ban-client set "$j" "${1}ignoreip" "$2" >/dev/null 2>&1 || true
    done
    [ "$1" = del ] || log_info "fail2ban: адрес $2 (эта сессия) не банится, пока перенос ждёт подтверждения"
}

# ssh_fw_sync «ПОРТ,ПОРТ» ПОРТ_ВХОДА — ufw allow на нужные порты (сначала открыть, потом
# закрыть лишние), jail sshd, SSH_PORTS и SSH_LOGIN_PORT в config.env
ssh_fw_sync() {
    local list="$1" login="$2" p spec
    for p in ${list//,/ }; do
        # владелец SSH-правил — 01: иначе её fw_allow при следующем запуске упрётся в «чужой» порт
        ZOO_PHASE=01-firewall fw_allow "$p/tcp" "SSH"
    done
    if [ -f "$PORTS_FILE" ]; then
        while IFS= read -r spec; do
            p="${spec%/tcp}"
            case ",$list," in *",$p,"*) continue ;; esac
            fw_revoke "$spec"
            ufw --force delete allow "$spec" >/dev/null 2>&1 || true
            log_info "ufw: SSH-порт $spec закрыт"
        done < <(awk -F'\t' '$2 == "SSH" && $1 ~ /^[0-9]+\/tcp$/ {print $1}' "$PORTS_FILE")
    fi
    case ",$list," in
        *",22,"*) ;;
        *)
            if grep -qE '^OpenSSH[[:space:]]' <<< "$(ufw status 2>/dev/null || true)"; then
                log_warn "в ufw есть чужое правило OpenSSH (22/tcp) — не трогаю; убрать: sudo ufw delete allow OpenSSH"
            fi ;;
    esac
    f2b_ports "$list"
    config_set SSH_PORTS "$list"
    config_set SSH_LOGIN_PORT "$login"
}

# Из туннеля на адрес сервера разрешён только SSH-порт (D25): 07 пересобирает правило
routing_refresh() {
    local log
    [ "$(state_get 07-routing)" = "done" ] && [ -f "$SCRIPTS_DIR/07-routing.sh" ] || return 0
    log="$LOG_DIR/ssh-routing-$(date +%Y%m%d-%H%M%S).log"
    ( umask 077; mkdir -p "$LOG_DIR" )
    if ZOO_PHASE=07-routing bash "$SCRIPTS_DIR/07-routing.sh" > "$log" 2>&1 8>&-; then
        log_ok "маршрутизация (07): из туннеля разрешены SSH-порты $SSH_PORTS"
    else
        log_warn "07 не переприменилась (лог $log): ssh из-под VPN на новый порт может не пускать — sudo bash $SCRIPTS_DIR/install.sh --phase 07"
    fi
}

# CREDENTIALS.md с новым портом; вывод 99 (секреты) — не в терминал и не в журнал
creds_refresh() {
    [ "$(state_get 99-print-creds)" = "done" ] && [ -f "$SCRIPTS_DIR/99-print-creds.sh" ] || return 0
    if ZOO_CREDS_NO_QR=1 ZOO_PHASE=99-print-creds bash "$SCRIPTS_DIR/99-print-creds.sh" >/dev/null 2>&1 8>&-; then
        log_ok "CREDENTIALS.md обновлён: SSH-порт $SSH_LOGIN_PORT"
    else
        log_warn "CREDENTIALS.md не обновился: sudo bash scripts/install.sh --phase 99"
    fi
}

# ============================================================
# Таймер отката: юнит в /etc (переживает reboot — после загрузки отсчёт заново)
# ============================================================

timer_arm() {
    local min="$1" d on="${1}min"
    # только стенд: таймер в секундах, чтобы тест не ждал минутами
    if is_test_env && [[ "${ZOO_TEST_REVERT_SEC:-}" =~ ^[0-9]+$ ]]; then on="${ZOO_TEST_REVERT_SEC}s"; fi
    ( umask 077; mkdir -p "$BIN_COPY" )
    d="$(dirname "${BASH_SOURCE[0]}")"
    if ! [ "$d" -ef "$BIN_COPY" ]; then
        cp -f "$d/lib.sh" "$BIN_COPY/lib.sh"
        cp -f "${BASH_SOURCE[0]}" "$BIN_COPY/01b-ssh.sh"
    fi
    cat > "/etc/systemd/system/$REVERT_UNIT.service" <<EOF
[Unit]
Description=vpn-zoo: откат SSH_HARDEN без подтверждения (фаза 01b)

[Service]
Type=oneshot
Environment="REPO_ROOT=$REPO_ROOT" "SCRIPTS_DIR=$SCRIPTS_DIR"
ExecStart=/bin/bash $BIN_COPY/01b-ssh.sh --revert
EOF
    cat > "/etc/systemd/system/$REVERT_UNIT.timer" <<EOF
[Unit]
Description=vpn-zoo: таймер отката SSH_HARDEN (фаза 01b)

[Timer]
OnActiveSec=${on}
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable "$REVERT_UNIT.timer" >/dev/null 2>&1
    systemctl restart "$REVERT_UNIT.timer"
    systemctl is-active --quiet "$REVERT_UNIT.timer" || return 1
}

timer_disarm() {
    systemctl disable --now "$REVERT_UNIT.timer" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$REVERT_UNIT.timer" "/etc/systemd/system/$REVERT_UNIT.service"
    systemctl daemon-reload
    rm -rf "$BIN_COPY"
}

# ============================================================
# Проверки до изменений
# ============================================================

user_can_sudo() {
    if command -v sudo >/dev/null; then
        sudo -n -l -U "$1" 2>/dev/null | grep -q 'may run the following' && return 0
    fi
    id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qxE 'sudo|admin|wheel'
}

# StrictModes: файл ключей и каталоги до домашнего — владелец пользователь или root,
# без записи для группы и остальных. Иначе sshd ключ молча не примет
ak_strict_ok() {
    local p="$1" user="$2" home="$3" owner mode
    [ "$(sshd_eff strictmodes)" = "no" ] && return 0
    while :; do
        read -r owner mode < <(stat -c '%U %a' "$p" 2>/dev/null) || return 1
        [ "$owner" = "$user" ] || [ "$owner" = "root" ] || return 1
        [ $(( 8#$mode & 8#022 )) -eq 0 ] || return 1
        [ "$p" = "$home" ] || [ "$p" = "/" ] && break
        p="$(dirname "$p")"
    done
}

# Пригодные ключи: root (если ему разрешён вход по ключу) и пользователи с sudo.
# Печатает найденное; rc 1 — ни одного
keys_report() {
    local akf user uid home shell tmpl path n rc=1
    akf="$(sshd_eff authorizedkeysfile)"
    while IFS=: read -r user _ uid _ _ home shell; do
        [[ "$uid" =~ ^[0-9]+$ ]] || continue
        case "$shell" in */nologin|*/false|"") continue ;; esac
        if [ "$uid" = "0" ]; then
            [ "$user" = "root" ] || continue
            case "$ROOT_POLICY" in no|forced-commands-only) continue ;; esac
        else
            [ "$uid" -ge 1000 ] && [ "$user" != "nobody" ] || continue
            user_can_sudo "$user" || continue
        fi
        for tmpl in $akf; do
            [ "$tmpl" != "none" ] || continue
            path="${tmpl//%%/$'\x01'}"
            path="${path//%h/$home}"
            path="${path//%u/$user}"
            path="${path//$'\x01'/%}"
            [ "${path#/}" != "$path" ] || path="$home/$path"
            [ -f "$path" ] || continue
            n="$(ssh-keygen -l -f "$path" 2>/dev/null | grep -c . || true)"
            [ "${n:-0}" -gt 0 ] || continue
            if ! ak_strict_ok "$path" "$user" "$home"; then
                log_warn "$path: sshd не примет эти ключи (StrictModes: владелец или права на запись группе/всем)"
                continue
            fi
            echo "$user — $path, ключей: $n"
            rc=0
        done
    done < <(getent passwd)
    return "$rc"
}

# Текущая SSH-сессия: процесс sshd среди предков (под sudo SSH_CONNECTION нет), его
# сокет — порт сервера и адрес клиента, время старта и способ входа из журнала
SESS_PORT=""; SESS_PEER=""; SESS_START=0; SESS_AUTH=""; SESS_USER=""
session_detect() {
    local p="$$" comm est priv="" line st btime hz
    est="$(ss -Htnp state established 2>/dev/null || true)"
    while [[ "$p" =~ ^[0-9]+$ ]] && [ "$p" -gt 1 ]; do
        comm="$(cat "/proc/$p/comm" 2>/dev/null || true)"
        case "$comm" in
            sshd|sshd-session) grep -q "pid=$p," <<< "$est" && priv="$p" ;;
        esac
        p="$(sed -E 's/^.*\) //' "/proc/$p/stat" 2>/dev/null | awk '{print $2}' || true)"
    done
    if [ -n "$priv" ]; then
        line="$(awk -v p="pid=$priv," 'index($0, p) {print; exit}' <<< "$est")"
        SESS_PORT="$(awk '{n = split($3, a, ":"); print a[n]}' <<< "$line")"
        SESS_PEER="$(awk '{sub(/:[0-9]+$/, "", $4); gsub(/[][]/, "", $4); sub(/^::ffff:/, "", $4); print $4}' <<< "$line")"
        st="$(sed -E 's/^.*\) //' "/proc/$priv/stat" | awk '{print $20}')"
        btime="$(awk '$1 == "btime" {print $2}' /proc/stat)"
        hz="$(getconf CLK_TCK)"
        SESS_START=$(( btime + st / hz ))
        line="$(journalctl -q --no-pager -o cat "_PID=$priv" 2>/dev/null | grep -E '^Accepted ' | tail -n 1 || true)"
        SESS_AUTH="$(awk '{print $2}' <<< "$line")"
        SESS_USER="$(awk '{print $4}' <<< "$line")"
    elif [ -n "${SSH_CONNECTION:-}" ]; then
        SESS_PORT="$(awk '{print $4}' <<< "$SSH_CONNECTION")"
        SESS_PEER="$(awk '{sub(/^::ffff:/, "", $1); print $1}' <<< "$SSH_CONNECTION")"
    fi
    return 0
}

# Клиент сессии — сам сервер (loopback, свой адрес: ssh -J/ProxyJump, VLESS/Hysteria этого
# же сервера) или туннель (интерфейс без L2: AmneziaWG, WireGuard, tun). Такой вход файрвол
# хостера не проходил и не доказывает, что порт открыт снаружи
peer_via_server() {
    local ip="$1" dev
    [ -n "$ip" ] || return 1
    case "$ip" in 127.*|::1) return 0 ;; esac
    ip -o addr show 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4}' | grep -qxF "$ip" && return 0
    dev="$(ip route get "$ip" 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
    [ -n "$dev" ] && ip -o link show dev "$dev" 2>/dev/null | grep -q 'link/none'
}

# Сессия доказывает, что порт PORT открыт снаружи: пришла на него и не с сервера/из туннеля.
# Пустой вывод — доказано; иначе причина
port_proof() {
    local port="$1" hint="$2"
    if [ -z "$SESS_PORT" ]; then
        echo "запустите это из SSH-сессии на порту $port: $hint (это и есть проверка, что вход работает). Из консоли хостера — SSH_CONFIRM_FORCE=1"
    elif [ "$SESS_PORT" != "$port" ]; then
        echo "эта сессия на порту $SESS_PORT, а не на $port. Откройте новый терминал: $hint — и запустите оттуда"
    elif peer_via_server "$SESS_PEER"; then
        echo "сессия пришла с адреса самого сервера или через туннель ($SESS_PEER: VPN этого сервера, ssh -J). Файрвол хостера она не проходила — что порт $port открыт снаружи, не доказано. Выключите VPN и войдите напрямую: $hint. Если SSH у вас всегда только через VPN — SSH_CONFIRM_FORCE=1"
    fi
}

preflight() {
    local am item ok=0 f
    command -v sshd >/dev/null || die "sshd не установлен — закрывать нечего"
    systemctl cat ssh.service >/dev/null 2>&1 || die "нет ssh.service (sshd не из пакета Ubuntu) — фаза 01b его не поддерживает"
    command -v ufw >/dev/null && [ -f "$F2B_JAIL" ] || die "сначала фаза 01 (ufw, fail2ban): sudo bash scripts/install.sh --phase 01"
    command -v ssh-keygen >/dev/null || die "нет ssh-keygen (пакет openssh-client)"
    mkdir -p /run/sshd && chmod 0755 /run/sshd
    sshd -t || die "текущий конфиг sshd с ошибкой (sshd -t) — сначала исправьте его"
    while IFS= read -r f; do
        if grep -qiE '^[[:space:]]*ListenAddress[[:space:]]+(\[[^]]+\]|[^:[:space:]]+):[0-9]+[[:space:]]*$' "$f"; then
            die "$f: ListenAddress с портом — порт задан там, фаза 01b такое не переносит. Уберите порт из ListenAddress или перенесите SSH вручную"
        fi
    done < <(sshd_files)
    [ "$(sshd_eff pubkeyauthentication)" = "yes" ] || die "в sshd выключен вход по ключу (PubkeyAuthentication no) — после запрета паролей войти будет нечем"
    am="$(sshd_eff authenticationmethods)"
    if [ -n "$am" ] && [ "$am" != "any" ]; then
        for item in $am; do [ "$item" = "publickey" ] && ok=1; done
        [ "$ok" = 1 ] || die "AuthenticationMethods $am: без паролей вход станет невозможен — уберите его или добавьте вариант publickey"
    fi
}

# ============================================================
# Порт назначения
# ============================================================

resolve_target() {
    local spec="${SSH_PORT:-random}" owner
    case "$spec" in
        ""|random)
            if [ -f "$PENDING_ENV" ] && [ "${PEND_SPEC:-}" = "random" ]; then echo "$NEW_PORT"; return 0; fi
            if is_managed && [ "${MANAGED_SPEC:-}" = "random" ]; then echo "$MANAGED_PORT"; return 0; fi
            rand_port ;;
        keep)
            if is_managed; then echo "$MANAGED_PORT"; else ssh_login_port; fi ;;
        *)
            [[ "$spec" =~ ^[0-9]{1,5}$ ]] && [ "$((10#$spec))" -ge 1 ] && [ "$((10#$spec))" -le 65535 ] \
                || die "SSH_PORT=$spec: число 1–65535, random или keep"
            spec="$((10#$spec))"
            is_banned_port "$spec" && die "SSH_PORT=$spec: этот порт ищут сканеры (запрещены: $BANNED_PORTS)"
            # порт, уже назначенный другому компоненту (в том числе ещё не установленному:
            # 01b идёт раньше фаз протоколов), иначе та фаза потом упадёт на занятом порту
            owner="$(ssh_port_taken_by "$spec")"
            [ -z "$owner" ] || die "SSH_PORT=$spec: порт назначен $owner — выберите другой"
            if ! case ",${SSH_PORTS:-}," in *",$spec,"*) true ;; *) false ;; esac; then
                _port_listening "$spec" tcp && die "SSH_PORT=$spec: порт уже занят другим сервисом (ss -tlnp)"
                owner="$(awk -F'\t' -v s="$spec/tcp" '$1 == s {print $3}' "$PORTS_FILE" 2>/dev/null | tail -n 1)"
                [ -z "$owner" ] || [ "$owner" = "01-firewall" ] || die "SSH_PORT=$spec: порт открыт фазой $owner"
            fi
            if [ "$spec" -ge "$(ephemeral_lo)" ]; then
                log_warn "SSH_PORT=$spec из диапазона исходящих портов (ip_local_port_range): его может занять исходящее соединение, и sshd на нём не поднимется" >&2
            fi
            echo "$spec" ;;
    esac
}

# Кому в config.env назначен порт (ключ *_PORT, кроме SSH-ключей); VLESS без VLESS_PORT — 443
ssh_port_taken_by() {
    local key
    key="$(awk -v p="$1" '
        /^[A-Z0-9_]*PORT=/ {
            k = $0; sub(/=.*/, "", k); v = $0; sub(/^[^=]*=/, "", v); gsub(/'\''/, "", v)
            if (k !~ /^SSH_/ && v == p) { print k; exit }
        }' "$CONFIG_FILE" 2>/dev/null || true)"
    if [ -n "$key" ]; then printf '%s\n' "$key"; return 0; fi
    if [ "$1" = "443" ] && [ "${ENABLE_VLESS:-1}" != "0" ] && [ -z "${VLESS_PORT:-}" ]; then
        echo "VLESS (443 по умолчанию)"
    fi
}

ephemeral_lo() {
    local lo=32768
    read -r lo _ < /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || true
    [[ "$lo" =~ ^[0-9]+$ ]] || lo=32768
    printf '%s\n' "$lo"
}

spec_name() {
    case "${SSH_PORT:-random}" in
        ""|random) echo random ;;
        keep) echo keep ;;
        *) if [[ "$SSH_PORT" =~ ^[0-9]{1,5}$ ]]; then echo "$((10#$SSH_PORT))"; else echo "$SSH_PORT"; fi ;;
    esac
}

load_state() {
    MANAGED_PORT=""; MANAGED_SPEC=""; NEW_PORT=""; PEND_SPEC=""
    if [ -f "$ORIG_ENV" ]; then env_read "$ORIG_ENV"; fi
    if [ -f "$MANAGED_ENV" ]; then
        env_read "$MANAGED_ENV"
        MANAGED_PORT="$PORT"; MANAGED_SPEC="$SPEC"
    fi
    if [ -f "$PENDING_ENV" ]; then
        env_read "$PENDING_ENV"
        PEND_SPEC="$SPEC"
    fi
}

# ============================================================
# Инструкция после шага 1
# ============================================================

print_instructions() {
    local who host when
    who="${LOGIN_USER:-ПОЛЬЗОВАТЕЛЬ}"
    host="${SERVER_IP:-СЕРВЕР}"; [[ "$host" == *:* ]] && host="[$host]"
    when="$(date -d "@$DEADLINE" +%H:%M)"
    log_step "SSH: подтвердите вход до $when (время сервера), иначе всё вернётся само"
    if [ "$NEW_PORT" = "$PREV_LOGIN_PORT" ]; then
        echo "  Порт SSH прежний ($NEW_PORT), пароли и вход root по паролю уже выключены."
    else
        echo "  Сейчас SSH принимает порты ${CUR_PORTS//,/ и } — вход только по ключу, пароли выключены."
    fi
    echo "  1. НЕ закрывая это окно, откройте НОВЫЙ терминал на своём компьютере и войдите напрямую"
    echo "     (VPN этого сервера выключен — через него файрвол хостера не проверяется):"
    echo "       ssh -p $NEW_PORT $who@$host"
    echo "     Если ключей в ssh-agent больше трёх: ssh -p $NEW_PORT -o IdentitiesOnly=yes -i ~/.ssh/ВАШ_КЛЮЧ $who@$host"
    echo "  2. В НОВОЙ сессии подтвердите:"
    # без cd: пользователь с sudo не войдёт в /root, если репозиторий там
    echo "       SSH_CONFIRM=1 sudo -E bash $REPO_ROOT/scripts/install.sh --phase 01b"
    if [ "$NEW_PORT" != "$PREV_LOGIN_PORT" ]; then
        echo "     После подтверждения порт $PREV_LOGIN_PORT закроется."
    fi
    echo "  Не получилось войти — ничего не делайте: в $when порт и настройки SSH вернутся как были."
    echo "  Файрвол у хостера (security group, «firewall» в панели)? Откройте в нём ${NEW_PORT}/tcp."
}

# ============================================================
# Режимы
# ============================================================

# Откат шага 1 к снимку (вызывающий держит блокировку)
do_revert() {
    local reason="$1" p
    load_state
    MODEL="$(ssh_model)"
    log_warn "SSH: откат шага 1 ($reason): порты $PREV_PORTS, прежние настройки sshd"
    if ! snap_restore "$PREV_SNAP"; then
        [ "$PREV_MANAGED" = "0" ] || die "нет снимка $PREV_SNAP — вернуть исходное: SSH_HARDEN=0 sudo -E bash scripts/install.sh --phase 01b"
        log_warn "нет снимка $PREV_SNAP — возвращаю исходное: убираю drop-in фазы, строки Port"
        rm -f "$SSHD_DROPIN" "$SOCKET_DROPIN"
        ports_uncomment
    fi
    ssh_restart || log_err "sshd не перезапустился после отката — systemctl status ssh"
    for p in ${PREV_PORTS//,/ }; do
        wait_banner "$p" || log_err "после отката порт $p не отвечает — systemctl status ssh ssh.socket"
    done
    timer_disarm
    ssh_fw_sync "$PREV_PORTS" "$PREV_LOGIN_PORT"
    f2b_owner del "$OWNER_IP"
    if [ "$PREV_MANAGED" = "0" ]; then rm -f "$ORIG_ENV" "$MANAGED_ENV"; fi
    rm -rf "$PENDING_ENV" "$PREV_SNAP"
    routing_refresh
    log_ok "SSH: откат выполнен, порты $PREV_PORTS"
}

mode_apply() {
    local target spec want prev_ports prev_login prev_managed applied min keys login_user orig_new=0
    preflight
    MODEL="$(ssh_model)"
    load_state
    spec="$(spec_name)"
    if [ -f "$PENDING_ENV" ]; then
        target="$(resolve_target)"
        if [ "$target" = "$NEW_PORT" ]; then
            log_info "шаг 1 уже применён (порт $NEW_PORT), жду подтверждения — отсчёт таймера заново"
            mode_remind
            return 0
        fi
        do_revert "SSH_PORT изменён до подтверждения"
        load_state
    fi

    if [ -f "$ORIG_ENV" ]; then
        env_read "$ORIG_ENV"
    else
        # не ослабляем: PermitRootLogin no остаётся no
        case "$(sshd_eff permitrootlogin)" in
            no) ROOT_POLICY=no ;;
            forced-commands-only) ROOT_POLICY=forced-commands-only ;;
            *) ROOT_POLICY=prohibit-password ;;
        esac
    fi
    target="$(resolve_target)"

    if is_managed && [ "$target" = "$MANAGED_PORT" ] && [ "$(dropin_content "$target")" = "$(cat "$SSHD_DROPIN" 2>/dev/null)" ]; then
        log_ok "SSH уже закрыт: порт $MANAGED_PORT, вход только по ключу"
        [ "${SSH_LOGIN_PORT:-}" = "$MANAGED_PORT" ] || config_set SSH_LOGIN_PORT "$MANAGED_PORT"
        return 0
    fi

    log_info "ключи для входа после запрета паролей:"
    if keys="$(keys_report)"; then
        awk '{print "    " $0}' <<< "$keys"
    else
        [ -z "$keys" ] || awk '{print "    " $0}' <<< "$keys"
        die "нет ни одного ключа, с которым можно войти после запрета паролей (authorized_keys root$( [ "$ROOT_POLICY" = "prohibit-password" ] || echo ' — root по ключу запрещён') или пользователя с sudo). Ничего не изменено. Добавьте ключ: на своём компьютере ssh-copy-id -p $(ssh_login_port) ПОЛЬЗОВАТЕЛЬ@${SERVER_IP:-СЕРВЕР}, затем повторите. Не закрывать SSH: SSH_HARDEN=0"
    fi
    if [ -n "$(sshd_eff authorizedkeyscommand | grep -v '^none$' || true)" ]; then
        log_info "задан AuthorizedKeysCommand — его ключи не проверялись"
    fi
    session_detect
    case "$SESS_AUTH" in
        password|keyboard-interactive)
            log_warn "ЭТА сессия вошла ПО ПАРОЛЮ ($SESS_USER, порт $SESS_PORT). После шага 1 пароль не пустит: нужен приватный ключ к одному из ключей выше" ;;
        publickey) log_ok "эта сессия вошла по ключу ($SESS_USER, порт $SESS_PORT)" ;;
        *) log_info "как вошла эта сессия, определить не удалось — для входа после шага 1 нужен приватный ключ к одному из ключей выше" ;;
    esac
    # кого подставить в «ssh -p НОВЫЙ …»: кто запустил sudo, кем вошла сессия, иначе
    # первый пользователь с ключом (root — только если ему разрешён вход по ключу)
    login_user="${SUDO_USER:-${SESS_USER:-}}"
    if [ -z "$login_user" ] || ! awk -F' — /' -v u="$login_user" '$1 == u {f = 1} END {exit !f}' <<< "$keys"; then
        login_user="$(awk -F' — /' 'NF > 1 {u = $1; sub(/^[[:space:]]+/, "", u); if (u != "root") {print u; exit}}' <<< "$keys")"
        grep -q '^root — /' <<< "$keys" && login_user=root
    fi

    if is_managed; then
        prev_ports="$MANAGED_PORT"; prev_login="$MANAGED_PORT"; prev_managed=1
        # после первого SSH_HARDEN=0 слушаются и исходные порты
        [ ! -f "$UNWIND_ENV" ] || prev_ports="$(csv_ports "$ORIG_PORTS,$MANAGED_PORT")"
    else
        prev_ports="$(csv_ports "$(detect_ssh_ports)")"
        prev_login="$(ssh_login_port)"
        prev_managed=0
        # остался от прерванного прогона — он записан до любых изменений, оставляем
        if [ ! -f "$ORIG_ENV" ]; then
            env_write "$ORIG_ENV" "ORIG_PORTS=$prev_ports" "ORIG_LOGIN_PORT=$prev_login" "ROOT_POLICY=$ROOT_POLICY"
            orig_new=1
        fi
    fi
    want="$(csv_ports "$prev_ports,$target")"
    min="$(revert_min)"
    log_info "шаг 1: SSH на портах $want, вход только по ключу (модель запуска sshd: $MODEL)"

    snap_take "$PREV_SNAP"
    applied="$(date +%s)"
    DEADLINE=$(( applied + min * 60 ))
    env_write "$PENDING_ENV" "NEW_PORT=$target" "SPEC=$spec" "CUR_PORTS=$want" "PREV_PORTS=$prev_ports" \
        "PREV_LOGIN_PORT=$prev_login" "PREV_MANAGED=$prev_managed" "PREV_SPEC=${MANAGED_SPEC:-}" \
        "APPLIED_AT=$applied" "DEADLINE=$DEADLINE" "REVERT_MIN=$min" "LOGIN_USER=$login_user" "OWNER_IP=$SESS_PEER"
    # Таймер — до первого изменения: если шаг 1 оборвётся на середине (обрыв SSH, SIGHUP,
    # ошибка), откат всё равно случится
    if ! timer_arm "$min"; then
        timer_disarm
        rm -f "$PENDING_ENV"
        rm -rf "$PREV_SNAP"
        [ "$orig_new" = "0" ] || rm -f "$ORIG_ENV"
        die "таймер отката не запустился — ничего не изменено (systemctl status $REVERT_UNIT.timer)"
    fi
    ssh_fw_sync "$want" "$prev_login"
    f2b_owner add "$SESS_PEER"
    # shellcheck disable=SC2046 # список портов — отдельные аргументы
    if ! render_managed $(tr ',' ' ' <<< "$want"); then
        # shellcheck disable=SC2046
        rollback_to "$PREV_SNAP" $(tr ',' ' ' <<< "$prev_ports")
        ssh_fw_sync "$prev_ports" "$prev_login"
        timer_disarm
        rm -f "$PENDING_ENV"
        [ "$orig_new" = "0" ] || rm -f "$ORIG_ENV"
        rm -rf "$PREV_SNAP"
        die "шаг 1 не применён, SSH возвращён как был"
    fi
    rm -f "$UNWIND_ENV"
    # отсчёт — с момента, когда новый порт заработал
    DEADLINE=$(( $(date +%s) + min * 60 ))
    sed -i -E "s/^DEADLINE=.*/DEADLINE=$DEADLINE/" "$PENDING_ENV"
    systemctl restart "$REVERT_UNIT.timer"
    routing_refresh
    load_state
    print_instructions
}

mode_remind() {
    local min
    [ -f "$PENDING_ENV" ] || return 0
    load_state
    min="$(revert_min)"
    DEADLINE=$(( $(date +%s) + min * 60 ))
    sed -i -E "s/^DEADLINE=.*/DEADLINE=$DEADLINE/; s/^REVERT_MIN=.*/REVERT_MIN=$min/" "$PENDING_ENV"
    timer_arm "$min" || die "таймер отката не запустился (systemctl status $REVERT_UNIT.timer)"
    f2b_owner add "$OWNER_IP"
    print_instructions
}

# Шаг 2. Доказательство — сама сессия: если закрываются другие порты, она пришла на новый
# порт снаружи (не с сервера и не через его VPN: файрвол хостера пройден); если новый порт
# был и до шага 1, она вошла по ключу (по журналу sshd или открыта после шага 1, когда
# пароли уже выключены)
mode_confirm() {
    local cur p snap why closes=0 existed=0
    if [ ! -f "$PENDING_ENV" ]; then
        load_state
        if [ -n "${MANAGED_PORT:-}" ]; then log_ok "подтверждать нечего: SSH уже закрыт, порт $MANAGED_PORT"; return 0; fi
        die "подтверждать нечего: шаг 1 не применён или уже откатился по таймеру (журнал: journalctl -u $REVERT_UNIT). Заново: SSH_HARDEN=1 sudo -E bash scripts/install.sh --phase 01b"
    fi
    load_state
    MODEL="$(ssh_model)"
    for p in ${PREV_PORTS//,/ }; do
        if [ "$p" = "$NEW_PORT" ]; then existed=1; else closes=1; fi
    done
    session_detect
    if [ "${SSH_CONFIRM_FORCE:-0}" = "1" ]; then
        log_warn "SSH_CONFIRM_FORCE=1: подтверждаю без проверки сессии (вход по новому порту не доказан)"
    else
        if [ "$closes" = "1" ]; then
            why="$(port_proof "$NEW_PORT" "ssh -p $NEW_PORT …")"
            [ -z "$why" ] || die "подтверждение: $why"
            log_ok "сессия на новом порту $NEW_PORT с внешнего адреса $SESS_PEER — вход работает"
        fi
        if [ "$existed" = "1" ]; then
            case "$SESS_AUTH" in
                publickey) log_ok "сессия вошла по ключу ($SESS_USER)" ;;
                password|keyboard-interactive) die "эта сессия вошла по паролю — вход по ключу не доказан. Войдите заново по ключу и подтвердите оттуда" ;;
                *)
                    [ -n "$SESS_PORT" ] && [ "$SESS_START" -ge "$APPLIED_AT" ] \
                        || die "запустите подтверждение из НОВОЙ SSH-сессии (открытой после шага 1 — значит, по ключу). Из консоли хостера — SSH_CONFIRM_FORCE=1"
                    log_ok "сессия открыта после шага 1 — значит, по ключу" ;;
            esac
        fi
    fi

    env_read "$ORIG_ENV"
    snap="$(mktemp -d)"
    snap_take "$snap"
    if ! render_managed "$NEW_PORT"; then
        # shellcheck disable=SC2046
        rollback_to "$snap" $(tr ',' ' ' <<< "$CUR_PORTS")
        rm -rf "$snap"
        die "подтверждение не применено: SSH по-прежнему на портах $CUR_PORTS, таймер отката идёт"
    fi
    rm -rf "$snap"
    timer_disarm
    for p in ${PREV_PORTS//,/ }; do
        [ "$p" = "$NEW_PORT" ] && continue
        if _port_listening "$p" tcp; then log_warn "порт $p всё ещё слушается (ss -tlnp) — его держит не sshd из этого конфига"; fi
    done
    ssh_fw_sync "$NEW_PORT" "$NEW_PORT"
    f2b_owner del "$OWNER_IP"
    cur="${SPEC:-random}"
    env_write "$MANAGED_ENV" "PORT=$NEW_PORT" "SPEC=$cur"
    rm -rf "$PENDING_ENV" "$PREV_SNAP"
    routing_refresh
    creds_refresh
    log_ok "SSH закрыт: только порт $NEW_PORT, вход только по ключу, root — $ROOT_POLICY"
}

# Переходный drop-in SSH_HARDEN=0: только порты (исходные и порт фазы), вход — исходный
dropin_unwind() {
    local p
    echo "# vpn-zoo, фаза 01b: SSH_HARDEN=0, переходный шаг — настройки входа исходные, порт фазы ещё открыт."
    echo "# Убрать: SSH_HARDEN=0 install.sh --phase 01b из SSH-сессии на исходном порту"
    for p in "$@"; do echo "Port $p"; done
}

# Второй SSH_HARDEN=0 закрывает порт фазы, только если эта сессия пришла снаружи на исходный
# порт после первого шага (файрвол хостера его пропускает). rc 1 — не доказано (причина
# в журнале); это не ошибка фазы: переходное состояние рабочее, полный install.sh не встаёт
unwind_proof() {
    local p why=""
    if [ "${SSH_CONFIRM_FORCE:-0}" = "1" ]; then
        log_warn "SSH_CONFIRM_FORCE=1: закрываю порт $PORT без проверки входа на исходный порт"
        return 0
    fi
    session_detect
    for p in ${ORIG_PORTS//,/ }; do
        why="$(port_proof "$p" "ssh -p $p …")"
        [ -n "$why" ] || break
    done
    if [ -z "$why" ] && [ "$SESS_START" -lt "${UNWIND_AT:-0}" ]; then
        why="эта сессия открыта до первого SSH_HARDEN=0 — откройте новую (ssh -p $SESS_PORT …) и запустите оттуда"
    fi
    if [ -n "$why" ]; then
        log_warn "порт $PORT оставлен открытым: $why"
        log_info "остаться на порту $PORT с закрытым SSH: SSH_HARDEN=1 sudo -E bash $REPO_ROOT/scripts/install.sh --phase 01b"
        return 1
    fi
    log_ok "сессия на исходном порту $SESS_PORT с внешнего адреса $SESS_PEER — порт $PORT можно закрыть"
}

# SSH_HARDEN=0 — в два запуска, если порт фазы не из исходных: (1) вход как до SSH_HARDEN,
# sshd слушает исходные порты И порт фазы; (2) из сессии на исходном порту порт фазы
# закрывается. Иначе возврат на 22 при файрволе хостера, пропускающем только порт фазы,
# отрезал бы доступ. Переходное состояние — надмножество рабочего, таймер ему не нужен
mode_disable() {
    local snap p orig_ports orig_login cur_ports ok=1 keep_port="" want who host
    if [ -f "$PENDING_ENV" ]; then
        do_revert "SSH_HARDEN=0"
    fi
    if [ ! -f "$ORIG_ENV" ] && ! is_managed && [ ! -f "$SSHD_DROPIN" ]; then
        log_info "SSH фазой 01b не закрывался — менять нечего"
        return 0
    fi
    MODEL="$(ssh_model)"
    ORIG_PORTS=""; ORIG_LOGIN_PORT=""; PORT=""; UNWIND_AT=0
    if [ -f "$ORIG_ENV" ]; then env_read "$ORIG_ENV"; fi
    if is_managed; then env_read "$MANAGED_ENV"; fi
    if [ -n "$ORIG_PORTS" ] && [ -n "$PORT" ] && ! case ",$ORIG_PORTS," in *",$PORT,"*) true ;; *) false ;; esac; then
        if [ -f "$UNWIND_ENV" ]; then
            env_read "$UNWIND_ENV"
            unwind_proof || return 0
        else
            keep_port="$PORT"
        fi
    fi
    snap="$(mktemp -d)"
    snap_take "$snap"
    cur_ports="$(sshd_eff port | paste -sd' ' -)"
    if [ -n "$keep_port" ]; then
        orig_ports="$ORIG_PORTS"
        want="$(csv_ports "$orig_ports,$keep_port")"
        # shellcheck disable=SC2046 # список портов — отдельные аргументы
        dropin_unwind $(tr ',' ' ' <<< "$want") > "$SSHD_DROPIN"
        chmod 644 "$SSHD_DROPIN"
        ports_comment
        # shellcheck disable=SC2046
        socket_dropin_write $(tr ',' ' ' <<< "$want")
    else
        rm -f "$SSHD_DROPIN" "$SOCKET_DROPIN"
        rmdir "${SOCKET_DROPIN%/*}" 2>/dev/null || true
        ports_uncomment
        orig_ports="${ORIG_PORTS:-$(sshd_eff port | paste -sd, -)}"
        want="$orig_ports"
    fi
    mkdir -p /run/sshd && chmod 0755 /run/sshd
    orig_login="${ORIG_LOGIN_PORT:-${orig_ports%%,*}}"
    # ufw и fail2ban — до перезапуска: новые порты открыты к моменту, когда sshd их слушает
    ssh_fw_sync "$(csv_ports "$want,${cur_ports// /,}")" "$(ssh_login_port)"
    if ! sshd -t; then ok=0
    elif ! ssh_restart; then ok=0
    else
        for p in ${want//,/ }; do wait_banner "$p" || { log_err "порт $p не отвечает"; ok=0; }; done
    fi
    if [ "$ok" = "0" ]; then
        # shellcheck disable=SC2086 # порты — отдельные аргументы
        rollback_to "$snap" $cur_ports
        rm -rf "$snap"
        ssh_fw_sync "$(csv_ports "$cur_ports")" "$(ssh_login_port)"
        die "исходные настройки SSH не поднялись — оставлено закрытое состояние"
    fi
    rm -rf "$snap"
    if [ -n "$keep_port" ]; then
        ssh_fw_sync "$want" "$keep_port"
        env_write "$UNWIND_ENV" "UNWIND_AT=$(date +%s)"
        routing_refresh
        creds_refresh
        session_detect
        who="${SUDO_USER:-${SESS_USER:-ПОЛЬЗОВАТЕЛЬ}}"
        host="${SERVER_IP:-СЕРВЕР}"; [[ "$host" == *:* ]] && host="[$host]"
        log_step "SSH: вход как до SSH_HARDEN, порт $keep_port пока тоже открыт"
        echo "  Порт $keep_port закроется после проверки, что порт $orig_login доступен снаружи"
        echo "  (файрвол хостера мог пропускать только $keep_port):"
        echo "  1. НЕ закрывая это окно, откройте НОВЫЙ терминал и войдите напрямую (без VPN этого сервера):"
        echo "       ssh -p $orig_login $who@$host"
        echo "  2. В НОВОЙ сессии:"
        echo "       SSH_HARDEN=0 sudo -E bash $REPO_ROOT/scripts/install.sh --phase 01b"
        echo "  Не входит на $orig_login — откройте его в файрволе хостера или оставьте как есть: SSH работает на $keep_port."
        return 0
    fi
    ssh_fw_sync "$orig_ports" "$orig_login"
    rm -f "$ORIG_ENV" "$MANAGED_ENV" "$UNWIND_ENV"
    routing_refresh
    creds_refresh
    log_ok "SSH возвращён как до SSH_HARDEN: порт(ы) $orig_ports, настройки входа sshd исходные"
}

# Таймер: шаг 1 не подтверждён. Пока работает install.sh — отложить
mode_revert() {
    local min
    if [ ! -f "$PENDING_ENV" ]; then timer_disarm; return 0; fi
    exec 8>/run/vpn-setup.lock
    if ! flock -n 8; then
        min="$(revert_min)"
        log_warn "install.sh ещё работает — откат SSH отложен на $min мин"
        systemctl restart "$REVERT_UNIT.timer"
        return 0
    fi
    load_state
    local prev_managed="$PREV_MANAGED" prev_spec="${PREV_SPEC:-}"
    # до отката: оборвись он на середине, следующий install.sh не должен молча повторить шаг 1
    if [ "$prev_managed" = "0" ]; then
        config_set SSH_HARDEN 0
        state_set 01b-ssh disabled
    elif [ -n "$prev_spec" ]; then
        config_set SSH_PORT "$prev_spec"
    fi
    do_revert "нет подтверждения за отведённое время"
    if [ "$prev_managed" = "0" ]; then
        log_info "записано SSH_HARDEN=0; повторить перенос: SSH_HARDEN=1 sudo -E bash scripts/install.sh --phase 01b"
    fi
}

case "$MODE" in
    --revert) mode_revert ;;
    --remind) mode_remind ;;
    apply)
        # не из install.sh (он держит блокировку сам) — своя: иначе таймер отката может
        # сработать посреди подтверждения и оставить sshd и ufw на разных портах
        if [ -z "${ZOO_PHASE:-}" ]; then
            exec 8>/run/vpn-setup.lock
            flock -n 8 || die "занято: работает install.sh или откат SSH (/run/vpn-setup.lock) — повторите через минуту"
        fi
        if [ "${SSH_CONFIRM:-0}" = "1" ]; then
            mode_confirm
        elif [ "${SSH_HARDEN:-0}" = "1" ]; then
            mode_apply
        else
            mode_disable
        fi ;;
    *) die "неизвестный аргумент: $MODE (служебные: --revert, --remind)" ;;
esac
