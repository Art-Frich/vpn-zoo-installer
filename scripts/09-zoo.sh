#!/usr/bin/env bash
# 09-zoo.sh — установка инструмента zoo (ARCHITECTURE §6)
#
#   /opt/vpn-zoo/zoo, /opt/vpn-zoo/scripts   копия zoo/ и scripts/ репо: zoo вызывает
#                                            функции lib/proto-<id>.sh из этой копии
#   /opt/vpn-zoo/INSTALL.json                когда и из какого коммита поставлено
#   /usr/local/bin/zoo                       симлинк на /opt/vpn-zoo/zoo/zoo
#   /etc/vpn-setup/users.json                реестр пользователей с owner (zoo setup)
#   /var/lib/vpn-zoo                         данные zoo: traffic.sqlite (история трафика)
#   config.env: ZOO_WEB_PORT, ZOO_WEB_TOKEN  порт (127.0.0.1) и токен веб-админки (zoo setup)
#
# Юниты: все zoo/systemd/*.service|*.timer копируются в /etc/systemd/system, включаются
# и запускаются те, что перечислены в zoo/systemd/enable.list (zoo-collector.timer —
# трафик каждые 5 минут, zoo-web.service — админка). Повторный запуск (--phase 09)
# обновляет копию и юниты и перезапускает админку.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"

require_root
config_load

ZOO_HOME="${ZOO_HOME:-/opt/vpn-zoo}"
ZOO_BIN_LINK="/usr/local/bin/zoo"
ZOO_STATE_DIR="${ZOO_STATE_DIR:-/var/lib/vpn-zoo}"
ZOO_UNIT_DIR="/etc/systemd/system"
ZOO_MIN_PY="3.10"

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
    for f in "$d"/*.service "$d"/*.timer; do
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
( umask 077; mkdir -p "$ZOO_STATE_DIR" )
chmod 700 "$ZOO_STATE_DIR"
[ "$(config_get ZOO_HOME)" = "$ZOO_HOME" ] || config_set ZOO_HOME "$ZOO_HOME"
"$ZOO_BIN_LINK" version >/dev/null || die "zoo: не запускается ($ZOO_BIN_LINK version)"
mark_owned zoo
log_ok "zoo $("$ZOO_BIN_LINK" version --json | jq -r '.zoo') → $ZOO_BIN_LINK"

log_step "zoo: пользователи и модули"
# owner заводят фазы протоколов; setup только записывает его в реестр users.json
# setup: реестр users.json, схема БД трафика, ZOO_WEB_PORT и ZOO_WEB_TOKEN в config.env
ZOO_HOME="$ZOO_HOME" ZOO_STATE_DIR="$ZOO_STATE_DIR" "$ZOO_BIN_LINK" setup \
    || die "zoo setup завершился с ошибкой"
config_load

log_step "zoo: коллектор трафика и админка"
zoo_install_units
# копия кода обновилась: работающая админка должна её перечитать
systemctl try-restart zoo-web.service >/dev/null 2>&1 || true
if systemctl is-enabled --quiet zoo-web.service 2>/dev/null; then
    wait_port "$ZOO_WEB_PORT" tcp 15 || die "zoo-web не слушает 127.0.0.1:$ZOO_WEB_PORT (journalctl -u zoo-web)"
    log_ok "админка на 127.0.0.1:$ZOO_WEB_PORT — вход: ssh -N -L $ZOO_WEB_PORT:127.0.0.1:$ZOO_WEB_PORT root@${SERVER_IP:-СЕРВЕР}, токен: sudo zoo web --info"
fi

log_ok "zoo готов: zoo status, zoo user add <имя>, zoo links <имя> --qr, zoo traffic, zoo web --info"
