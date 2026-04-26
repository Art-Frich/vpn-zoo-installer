#!/usr/bin/env bash
# 03-3xui.sh — установка 3x-ui панели (без интерактивного installer'а)
#
# Ставит из релиза напрямую, чтобы:
#   1. Не отвечать на вопросы install.sh
#   2. Сразу применить наши $PANEL_USER/$PANEL_PASS/$PANEL_PORT/$PANEL_PATH

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
config_load

XUI_VERSION="${XUI_VERSION:-latest}"
XUI_DIR="/usr/local/x-ui"

# ------------------------------------------------------------
# 1. Архитектура
# ------------------------------------------------------------

case "$(uname -m)" in
    x86_64)  ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
    armv7l)  ARCH="armv7" ;;
    *) die "неподдерживаемая архитектура: $(uname -m)" ;;
esac

# ------------------------------------------------------------
# 2. Скачиваем релиз
# ------------------------------------------------------------

if [ -x "$XUI_DIR/x-ui" ]; then
    log_info "3x-ui уже установлен в $XUI_DIR — пропускаю скачивание"
else
    if [ "$XUI_VERSION" = "latest" ]; then
        log_info "определяю latest-версию 3x-ui"
        XUI_VERSION="$(curl -fsSL https://api.github.com/repos/MHSanaei/3x-ui/releases/latest | jq -r .tag_name)"
        [ -n "$XUI_VERSION" ] && [ "$XUI_VERSION" != "null" ] || die "не получилось узнать latest-версию"
    fi
    log_info "качаю 3x-ui $XUI_VERSION (arch=$ARCH)"

    URL="https://github.com/MHSanaei/3x-ui/releases/download/${XUI_VERSION}/x-ui-linux-${ARCH}.tar.gz"
    tmpfile="$(mktemp)"
    if ! curl -fsSL --max-time 120 -o "$tmpfile" "$URL"; then
        rm -f "$tmpfile"
        die "не удалось скачать $URL"
    fi

    log_info "распаковываю в $XUI_DIR"
    rm -rf "$XUI_DIR"
    tar xzf "$tmpfile" -C /usr/local/
    rm -f "$tmpfile"

    # Бинарники + права
    chmod +x "$XUI_DIR/x-ui"
    chmod +x "$XUI_DIR/bin/xray-linux-${ARCH}" 2>/dev/null || true
    if [ -f "$XUI_DIR/x-ui.sh" ]; then
        cp "$XUI_DIR/x-ui.sh" /usr/bin/x-ui
        chmod +x /usr/bin/x-ui
    fi
    # systemd unit (3x-ui v2.6+ имеет варианты для разных дистров)
    if   [ -f "$XUI_DIR/x-ui.service" ];        then cp "$XUI_DIR/x-ui.service" /etc/systemd/system/x-ui.service
    elif [ -f "$XUI_DIR/x-ui.service.debian" ]; then cp "$XUI_DIR/x-ui.service.debian" /etc/systemd/system/x-ui.service
    elif [ -f "$XUI_DIR/x-ui.service.arch" ];   then cp "$XUI_DIR/x-ui.service.arch" /etc/systemd/system/x-ui.service
    else die "не найден x-ui.service unit-файл"
    fi
fi

# ------------------------------------------------------------
# 3. Применяем настройки (username/password/port/webBasePath)
# ------------------------------------------------------------

log_info "применяю настройки панели: port=$PANEL_PORT, path=$PANEL_PATH"

# x-ui setting записывает в БД /etc/x-ui/x-ui.db. Работает офлайн.
mkdir -p /etc/x-ui

"$XUI_DIR/x-ui" setting \
    -username "$PANEL_USER" \
    -password "$PANEL_PASS" \
    -port "$PANEL_PORT" \
    -webBasePath "$PANEL_PATH" 2>&1 | tail -5 || true

# ------------------------------------------------------------
# 4. systemd
# ------------------------------------------------------------

if is_container; then
    log_warn "контейнер — systemd unit записан, но не активируется (нет init)"
else
    systemctl daemon-reload
    systemctl enable x-ui >/dev/null 2>&1 || true
    systemctl restart x-ui

    # Wait for panel
    log_info "жду пока панель поднимется"
    for i in $(seq 1 15); do
        if ss -tlnp 2>/dev/null | grep -q ":${PANEL_PORT} "; then
            log_ok "x-ui слушает на :${PANEL_PORT}"
            break
        fi
        sleep 1
    done

    if ! systemctl is-active --quiet x-ui; then
        log_err "x-ui не стартовал. Логи:"
        journalctl -u x-ui -n 20 --no-pager
        die "x-ui failed"
    fi
fi

log_ok "3x-ui установлен и настроен"
log_info "панель: http://${SERVER_IP}:${PANEL_PORT}/${PANEL_PATH}/"
log_info "  user: $PANEL_USER"
log_info "  pass: $PANEL_PASS"
