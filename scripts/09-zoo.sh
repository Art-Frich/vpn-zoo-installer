#!/usr/bin/env bash
# 09-zoo.sh — установка инструмента zoo (ARCHITECTURE §6)
#
#   /opt/vpn-zoo/zoo, /opt/vpn-zoo/scripts   копия zoo/ и scripts/ репо: zoo вызывает
#                                            функции lib/proto-<id>.sh из этой копии
#   /opt/vpn-zoo/INSTALL.json                когда и из какого коммита поставлено
#   /usr/local/bin/zoo                       симлинк на /opt/vpn-zoo/zoo/zoo
#   /etc/vpn-setup/users.json                реестр пользователей с owner (zoo setup)
#   /var/lib/vpn-zoo                         данные zoo: traffic.sqlite (история трафика),
#                                            probe-history.sqlite (история проб)
#   /usr/local/lib/vpn-zoo/bin/age           закреплённый age (AGE_* в versions.env): шифрует сырые
#                                            отчёты в `zoo history export`; не в PATH, сбой — не провал фазы
#   config.env: ZOO_WEB_PORT, ZOO_WEB_TOKEN  порт (127.0.0.1) и токен веб-админки (zoo setup)
#
# Юниты: все zoo/systemd/*.service|*.timer|*.path копируются в /etc/systemd/system, включаются
# и запускаются те, что перечислены в zoo/systemd/enable.list (zoo-collector.timer —
# трафик каждые 5 минут, zoo-live.timer/.path — метрики протоколов, zoo-job.path — вкл/выкл
# протокола из админки, zoo-web.service — админка, zoo-clients.timer — версии клиентских
# приложений из GitHub раз в сутки). Повторный запуск (--phase 09)
# обновляет копию и юниты и перезапускает админку.
#   /var/lib/vpn-zoo/jobs, live-req            заявки админки (файлы); исполняют zoo-job / zoo-live-req

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"

require_root
config_load
versions_load
detect_arch >/dev/null

ZOO_HOME="${ZOO_HOME:-/opt/vpn-zoo}"
ZOO_BIN_LINK="/usr/local/bin/zoo"
ZOO_STATE_DIR="${ZOO_STATE_DIR:-/var/lib/vpn-zoo}"
ZOO_UNIT_DIR="/etc/systemd/system"
ZOO_MIN_PY="3.10"
ZOO_AGE_BIN="${ZOO_AGE_BIN:-/usr/local/lib/vpn-zoo/bin/age}"

zoo_python_ok() {
    command -v python3 >/dev/null \
        && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

# Копия zoo/ и scripts/ во временный каталог рядом с целевым, затем замена целиком:
# zoo, запущенный во время установки, не увидит наполовину скопированные файлы
zoo_install_tree() {
    local stage old="" rev=""
    stage="$(mktemp -d "${ZOO_HOME%/}.new.XXXXXX")"
    tar -C "$REPO_ROOT" --exclude='zoo/tests' --exclude='__pycache__' --exclude='*.pyc' \
        -cf - zoo scripts | tar -C "$stage" --no-same-owner -xf -
    if command -v git >/dev/null && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        rev="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
        [ -z "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | head -1)" ] || rev="${rev:+$rev+}dirty"
    fi
    jq -n --arg ts "$(date -Iseconds)" --arg src "$REPO_ROOT" --arg git "$rev" \
        '{installed: $ts, source: $src, git: $git}' > "$stage/INSTALL.json"
    chown -R root:root "$stage"
    chmod -R u=rwX,go=rX "$stage"
    chmod 755 "$stage/zoo/zoo"
    if [ -e "$ZOO_HOME" ]; then
        old="${ZOO_HOME%/}.old.$$"
        mv "$ZOO_HOME" "$old"
    fi
    mv "$stage" "$ZOO_HOME"
    [ -z "$old" ] || rm -rf "$old"
}

# Юниты из zoo/systemd: установить изменившиеся, включить перечисленные в enable.list
zoo_install_units() {
    local d="$ZOO_HOME/zoo/systemd" f unit changed=()
    [ -d "$d" ] || return 0
    for f in "$d"/*.service "$d"/*.timer "$d"/*.path; do
        [ -f "$f" ] || continue
        unit="$(basename "$f")"
        cmp -s "$f" "$ZOO_UNIT_DIR/$unit" && continue
        install -m 0644 "$f" "$ZOO_UNIT_DIR/$unit"
        changed+=("$unit")
    done
    [ "${#changed[@]}" -eq 0 ] || systemctl daemon-reload
    [ -f "$d/enable.list" ] || return 0
    while read -r unit _; do
        case "$unit" in ''|'#'*) continue ;; esac
        if [ ! -f "$ZOO_UNIT_DIR/$unit" ]; then
            log_warn "zoo: юнит $unit указан в enable.list, но файла нет"
            continue
        fi
        systemctl enable --now "$unit" >/dev/null 2>&1 || die "zoo: не удалось включить $unit"
        case " ${changed[*]:-} " in *" $unit "*) systemctl try-restart "$unit" ;; esac
        log_ok "zoo: $unit включён"
    done < "$d/enable.list"
}

# age закреплённой версии для шифрования истории проб. Без него zoo history export отдаёт только
# анонимный jsonl, поэтому сбой — предупреждение, а не провал фазы
zoo_age_install() {
    local sum tarball tmpd
    if [ -x "$ZOO_AGE_BIN" ] && [ "$("$ZOO_AGE_BIN" --version 2>/dev/null)" = "$AGE_VERSION" ]; then
        log_info "age $AGE_VERSION уже стоит: $ZOO_AGE_BIN"
        return 0
    fi
    sum="$(version_for AGE_SHA256 "$ZOO_ARCH")"
    tarball="/var/cache/vpn-zoo/age-$AGE_VERSION-linux-$ZOO_ARCH.tar.gz"
    # download_verified при сбое делает die — в подоболочке это только код возврата
    ( download_verified "$AGE_URL_BASE/age-$AGE_VERSION-linux-$ZOO_ARCH.tar.gz" "$sum" "$tarball" ) || return 1
    tmpd="$(mktemp -d)"
    if ! tar -xzf "$tarball" -C "$tmpd" age/age; then
        rm -rf "$tmpd"
        return 1
    fi
    mkdir -p "$(dirname "$ZOO_AGE_BIN")"
    install -m 0755 "$tmpd/age/age" "$ZOO_AGE_BIN"
    rm -rf "$tmpd" "$tarball"
    log_ok "age $AGE_VERSION: $ZOO_AGE_BIN"
}

log_step "zoo: Python"
wait_for_apt
command -v python3 >/dev/null || apt_install python3
zoo_python_ok || die "zoo: нужен Python >= $ZOO_MIN_PY, установлен $(python3 -V 2>&1)"
command -v qrencode >/dev/null || apt_install qrencode
log_ok "$(python3 -V 2>&1)"

log_step "zoo: установка в $ZOO_HOME"
guard_foreign_install zoo "$ZOO_HOME" "$ZOO_BIN_LINK"
[ -f "$REPO_ROOT/zoo/zoo" ] || die "zoo: нет $REPO_ROOT/zoo/zoo — неполная копия репо"
zoo_install_tree
ln -sfn "$ZOO_HOME/zoo/zoo" "$ZOO_BIN_LINK"
( umask 077; mkdir -p "$ZOO_STATE_DIR" "$ZOO_STATE_DIR/jobs" "$ZOO_STATE_DIR/live-req" )
chmod 700 "$ZOO_STATE_DIR" "$ZOO_STATE_DIR/jobs" "$ZOO_STATE_DIR/live-req"
[ "$(config_get ZOO_HOME)" = "$ZOO_HOME" ] || config_set ZOO_HOME "$ZOO_HOME"
"$ZOO_BIN_LINK" version >/dev/null || die "zoo: не запускается ($ZOO_BIN_LINK version)"
mark_owned zoo
log_ok "zoo $("$ZOO_BIN_LINK" version --json | jq -r '.zoo') → $ZOO_BIN_LINK"

log_step "zoo: age для истории проб"
zoo_age_install || log_warn "zoo: age не установлен — zoo history export выгрузит только анонимный jsonl (повтор: --phase 09)"

log_step "zoo: пользователи и модули"
# owner заводят фазы протоколов; setup только записывает его в реестр users.json
# setup: реестр users.json, схема БД трафика, ZOO_WEB_PORT и ZOO_WEB_TOKEN в config.env
ZOO_HOME="$ZOO_HOME" ZOO_STATE_DIR="$ZOO_STATE_DIR" "$ZOO_BIN_LINK" setup \
    || die "zoo setup завершился с ошибкой"
config_load

log_step "zoo: коллектор трафика и админка"
journald_limit
zoo_install_units
# копия кода обновилась: работающая админка должна её перечитать
systemctl try-restart zoo-web.service >/dev/null 2>&1 || true
if systemctl is-enabled --quiet zoo-web.service 2>/dev/null; then
    wait_port "$ZOO_WEB_PORT" tcp 15 || die "zoo-web не слушает 127.0.0.1:$ZOO_WEB_PORT (journalctl -u zoo-web)"
    log_ok "админка на 127.0.0.1:$ZOO_WEB_PORT — вход: ssh -N -L $ZOO_WEB_PORT:127.0.0.1:$ZOO_WEB_PORT ${SUDO_USER:-root}@${SERVER_IP:-СЕРВЕР}, токен: sudo zoo web --info"
fi

log_ok "zoo готов: zoo status, zoo user add <имя>, zoo links <имя> --qr, zoo traffic, zoo web --info"
