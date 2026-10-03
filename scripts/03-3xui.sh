#!/usr/bin/env bash
# 03-3xui.sh — 3x-ui закреплённой версии (versions.env) с проверкой sha256.
# Панель только на 127.0.0.1 (D6), случайные порт и путь, API-токен в config.env,
# публичная подписка выключена (D8). Доступ к панели: ssh -L.
#
# Пароль панели не попадает в argv: на свежей БД (admin/admin) он меняется через
# POST /panel/api/setting/updateUser телом запроса. Если API недоступно — фолбэк
# на `x-ui setting -password` (argv) с предупреждением.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
config_load
versions_load
detect_arch >/dev/null

XUI_SERVICE=/etc/systemd/system/x-ui.service
XUI_ETC=/etc/x-ui
WANT_VER="${XUI_VERSION#v}"
XRAY_BIN="$(xui_xray_bin)"

# ------------------------------------------------------------
# 1. Параметры панели (генерируются один раз)
# ------------------------------------------------------------

if [ -z "${PANEL_PORT:-}" ] || is_banned_port "$PANEL_PORT"; then
    [ -n "${PANEL_PORT:-}" ] && log_info "порт панели $PANEL_PORT из запрещённого списка — меняю"
    config_set PANEL_PORT "$(rand_port)"
fi
[ -n "${PANEL_PATH:-}" ] || config_set PANEL_PATH "$(gen_random_alnum 18)"
PANEL_PATH="${PANEL_PATH#/}"; PANEL_PATH="${PANEL_PATH%/}"
[ -n "${PANEL_USER:-}" ] || config_set PANEL_USER "$(gen_random_alnum 10)"
[ -n "${PANEL_PASS:-}" ] || config_set PANEL_PASS "$(gen_random_alnum 24)"

# ------------------------------------------------------------
# 2. Чужая установка?
# ------------------------------------------------------------

# Установка первой версии этого инсталлера — тоже наша
if ! is_owned x-ui && [ "$(state_get 03-3xui)" = "done" ] && [ -f "$XUI_ETC/x-ui.db" ]; then
    mark_owned x-ui
    config_set PANEL_CREDS_SET 1
fi
guard_foreign_install x-ui "$XUI_ETC/x-ui.db" /usr/local/x-ui/x-ui /etc/default/x-ui

# ------------------------------------------------------------
# 3. Установка/обновление бинарей
# ------------------------------------------------------------

installed_ver="$(xui_cli_version || true)"
fresh_bin=0
if [ "$installed_ver" = "$WANT_VER" ] && [ -x "$XRAY_BIN" ]; then
    log_info "3x-ui $installed_ver уже установлен"
else
    [ -n "$installed_ver" ] && log_info "3x-ui $installed_ver → $WANT_VER"
    sum="$(version_for XUI_SHA256 "$ZOO_ARCH")"
    tarball="/var/cache/vpn-zoo/x-ui-${XUI_VERSION}-linux-${ZOO_ARCH}.tar.gz"
    download_verified "$XUI_URL_BASE/x-ui-linux-${ZOO_ARCH}.tar.gz" "$sum" "$tarball"

    if systemctl is-active --quiet x-ui 2>/dev/null; then
        systemctl stop x-ui
    fi
    if [ -d "$XUI_DIR" ] || [ -d "$XUI_ETC" ]; then
        bdir="$(backup_path "$XUI_ETC" "$XUI_SERVICE" | tail -1)"
        # старые бинари — переносом, без копирования 300 МБ
        if [ -d "$XUI_DIR" ]; then
            mv "$XUI_DIR" "$bdir/x-ui.prev"
            log_info "прежние бинари: $bdir/x-ui.prev"
        fi
    fi

    tmpd="$(mktemp -d /usr/local/.x-ui-new.XXXXXX)"
    tar -xzf "$tarball" -C "$tmpd"
    [ -x "$tmpd/x-ui/x-ui" ] || die "в архиве нет x-ui/x-ui"
    mv "$tmpd/x-ui" "$XUI_DIR"
    rmdir "$tmpd"
    chmod 755 "$XUI_DIR/x-ui" "$XRAY_BIN"
    [ -f "$XUI_DIR/bin/mtg-linux-$ZOO_ARCH" ] && chmod 755 "$XUI_DIR/bin/mtg-linux-$ZOO_ARCH"
    install -m 755 "$XUI_DIR/x-ui.sh" /usr/bin/x-ui
    # в архиве нет общего x-ui.service, только .debian/.arch/.rhel
    install -m 644 "$XUI_DIR/x-ui.service.debian" "$XUI_SERVICE"
    systemctl daemon-reload
    fresh_bin=1
    log_ok "3x-ui $WANT_VER распакован в $XUI_DIR"
fi

got_ver="$(xui_cli_version)"
[ "$got_ver" = "$WANT_VER" ] || die "x-ui -v = $got_ver, ожидалась $WANT_VER"
xray_ver="$("$XRAY_BIN" version 2>/dev/null | awk 'NR==1 {print $2}')"
if [ "$xray_ver" != "$XUI_XRAY_VERSION" ]; then
    log_warn "Xray в архиве: $xray_ver, в versions.env: $XUI_XRAY_VERSION — обновите XUI_XRAY_VERSION"
else
    log_ok "Xray $xray_ver (встроен в 3x-ui $WANT_VER)"
fi

# ------------------------------------------------------------
# 4. Настройки в БД (офлайн): порт, путь, только 127.0.0.1
# ------------------------------------------------------------

( umask 077; mkdir -p "$XUI_ETC" )
chmod 700 "$XUI_ETC"

declare -A cur=()
while IFS='=' read -r k v; do [ -n "$k" ] && cur[$k]="$v"; done < <(xui_cli_show)
web_changed=0
if [ "${cur[port]:-}" != "$PANEL_PORT" ] || [ "${cur[webBasePath]:-}" != "/$PANEL_PATH/" ] || [ "${cur[listenIP]:-}" != "127.0.0.1" ]; then
    xui_cli_set_web "$PANEL_PORT" "$PANEL_PATH" 127.0.0.1 || die "x-ui setting: не удалось задать порт/путь/listenIP"
    web_changed=1
    log_info "панель: 127.0.0.1:$PANEL_PORT/$PANEL_PATH/"
fi

# Схема БД под новую версию (так делает и официальный install.sh); сервис сейчас остановлен
if [ "$fresh_bin" = "1" ]; then
    "$XUI_BIN" migrate >/dev/null 2>&1 || die "x-ui migrate завершился с ошибкой"
fi
chmod 600 "$XUI_ETC"/x-ui.db* 2>/dev/null || true

# Токен: выпускаем, если его нет. Проверка на живой панели — ниже (xui_wait_api)
if [ -z "${XUI_API_TOKEN:-}" ]; then
    xui_token_refresh
    log_ok "API-токен «$XUI_TOKEN_NAME» выпущен (config.env: XUI_API_TOKEN)"
fi

# ------------------------------------------------------------
# 5. systemd
# ------------------------------------------------------------

systemctl enable x-ui >/dev/null 2>&1
if [ "$web_changed" = "1" ] || ! systemctl is-active --quiet x-ui; then
    systemctl restart x-ui
fi
xui_wait_api 60 || { journalctl -u x-ui -n 40 --no-pager || true; die "API 3x-ui не поднялся"; }

# ------------------------------------------------------------
# 6. Логин/пароль панели (без argv)
# ------------------------------------------------------------

declare -A cur2=()
while IFS='=' read -r k v; do [ -n "$k" ] && cur2[$k]="$v"; done < <(xui_cli_show)
if [ "${cur2[hasDefaultCredential]:-}" = "true" ]; then
    if xui_set_credentials admin admin "$PANEL_USER" "$PANEL_PASS"; then
        log_ok "логин/пароль панели заданы через API"
    else
        log_warn "API не принял смену пароля — фолбэк: x-ui setting (пароль кратко виден в списке процессов)"
        _xui_cli_setting -username "$PANEL_USER" -password "$PANEL_PASS" >/dev/null || die "не удалось задать логин/пароль панели"
    fi
    config_set PANEL_CREDS_SET 1
elif [ "${PANEL_CREDS_SET:-0}" != "1" ]; then
    log_warn "у панели уже не admin/admin и пароль задавал не этот инсталлер — PANEL_USER/PANEL_PASS в config.env могут не подходить"
fi

# ------------------------------------------------------------
# 7. Подписка и 2FA
# ------------------------------------------------------------

settings="$(xui_settings_get)"
sub_enable="$(jq -r '.subEnable' <<< "$settings")"
sub_listen="$(jq -r '.subListen' <<< "$settings")"
need_restart=0

if [ "${SUB_PUBLIC:-0}" = "1" ]; then
    # D8: с доменом и TLS подписку включают явно; настройку домена делает отдельная фаза
    if [ "$sub_enable" != "true" ]; then
        xui_settings_update '{"subEnable":true}'; need_restart=1
    fi
    log_warn "SUB_PUBLIC=1: подписка 3x-ui включена ($(jq -r '.subPort' <<< "$settings")/tcp) — порт открывает фаза подписки"
elif [ "$sub_enable" != "false" ] || [ "$sub_listen" != "127.0.0.1" ]; then
    xui_settings_update '{"subEnable":false,"subListen":"127.0.0.1"}'
    need_restart=1
    log_ok "публичная подписка 3x-ui выключена (D8)"
fi

if [ "${PANEL_2FA:-0}" = "1" ]; then
    if [ "$(jq -r '.twoFactorEnable' <<< "$settings")" != "true" ]; then
        secret="$(python3 -c 'import base64,os; print(base64.b32encode(os.urandom(20)).decode().rstrip("="))')"
        xui_settings_update "$(jq -cn --arg s "$secret" '{twoFactorEnable:true, twoFactorToken:$s}')"
        config_set PANEL_2FA_SECRET "$secret"
        log_ok "2FA панели включена. Секрет TOTP — PANEL_2FA_SECRET в $CONFIG_FILE (добавь в приложение-аутентификатор)"
        log_info "сброс 2FA при потере: /usr/local/x-ui/x-ui setting -resetTwoFactor"
    fi
fi

if [ "$need_restart" = "1" ]; then
    systemctl restart x-ui
    xui_wait_api 60 || die "API 3x-ui не поднялся после перезапуска"
fi

# ------------------------------------------------------------
# 8. Проверки: версия, Xray, никаких лишних listen
# ------------------------------------------------------------

api_ver="$(xui_panel_version)"
[ "$api_ver" = "$WANT_VER" ] || die "API сообщает версию панели $api_ver, ожидалась $WANT_VER"

for _ in $(seq 1 15); do
    [ "$(xui_xray_state)" = "running" ] && break
    sleep 1
done
state_x="$(xui_xray_state)"
[ "$state_x" = "running" ] || die "Xray в панели не запущен (state=$state_x): $(xui_server_status | jq -r '.xray.errorMsg')"
log_ok "Xray $(xui_xray_version) запущен панелью"

# Панель обязана слушать только loopback
public_listen="$(ss -Hltnp 2>/dev/null | awk '/"x-ui"/ && $4 !~ /^(127\.0\.0\.1|\[::1\]):/ {print $4}')"
if [ -n "$public_listen" ]; then
    die "x-ui слушает не только 127.0.0.1: $public_listen"
fi
ss -Hltn "sport = :$PANEL_PORT" | grep -q '127.0.0.1' || die "панель не слушает 127.0.0.1:$PANEL_PORT"

mark_owned x-ui
config_set XUI_INSTALLED_VERSION "$XUI_VERSION"

log_ok "3x-ui $WANT_VER готов: панель 127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/"
log_info "доступ: ssh -N -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} root@${SERVER_IP} → http://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/"
log_info "логин: ${PANEL_USER}, пароль: PANEL_PASS в $CONFIG_FILE"
