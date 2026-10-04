#!/usr/bin/env bash
# 03-3xui.sh — 3x-ui закреплённой версии (versions.env) с проверкой sha256.
# Панель только на 127.0.0.1 (D6), случайные порт и путь, API-токен в config.env,
# публичная подписка выключена (D8). Доступ к панели: ssh -L.
#
# Пароль панели не попадает в argv: на свежей БД (admin/admin) он меняется через
# POST /panel/api/setting/updateUser телом запроса. Если API недоступно — фолбэк
# на `x-ui setting -password` (argv) с предупреждением.

# shellcheck disable=SC2153 # PANEL_* приходят из config.env (config_load)
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

# Значение — через переменную: ошибка внутри $(...) в аргументе команды set -e не ловит,
# и в config.env записалась бы пустая строка
gen_once() {
    local key="$1" val; shift
    [ -n "${!key:-}" ] && return 0
    val="$("$@")" || die "не удалось сгенерировать $key"
    [ -n "$val" ] || die "пустое значение $key"
    config_set "$key" "$val"
}

if [ -n "${PANEL_PORT:-}" ] && { ! [[ "$PANEL_PORT" =~ ^[0-9]+$ ]] || is_banned_port "$PANEL_PORT"; }; then
    log_info "порт панели $PANEL_PORT недопустим (не число или из запрещённого списка) — меняю"
    PANEL_PORT=""
fi
gen_once PANEL_PORT rand_port
gen_once PANEL_PATH gen_random_alnum 18
PANEL_PATH="${PANEL_PATH#/}"; PANEL_PATH="${PANEL_PATH%/}"
[[ "$PANEL_PATH" =~ ^[A-Za-z0-9_-]+$ ]] || die "PANEL_PATH: допустимы только A-Z a-z 0-9 _ - (получено: $PANEL_PATH)"
gen_once PANEL_USER gen_random_alnum 10
gen_once PANEL_PASS gen_random_alnum 24

# ------------------------------------------------------------
# 2. Чужая установка?
# ------------------------------------------------------------

# Установки первой версии инсталлера помечает нашими state_migrate (install.sh)
# каталог целиком: рядом с x-ui.db лежат -wal/-shm, без них копия БД неполная
foreign_takeover=0
if ! is_owned x-ui && { [ -e "$XUI_ETC" ] || [ -e /etc/default/x-ui ] || [ -e "$XUI_SERVICE" ]; }; then
    foreign_takeover=1
fi
guard_foreign_install x-ui "$XUI_ETC" /etc/default/x-ui "$XUI_SERVICE"

# ------------------------------------------------------------
# 3. Установка/обновление бинарей
# ------------------------------------------------------------

installed_ver="$(xui_cli_version || true)"
fresh_bin=0
# Бинари чужой установки не проверены по sha256 — при перехвате (--force) ставим свои
if [ "$installed_ver" = "$WANT_VER" ] && [ -x "$XRAY_BIN" ] && [ "$foreign_takeover" = "0" ]; then
    log_info "3x-ui $installed_ver уже установлен"
else
    [ "$foreign_takeover" = "1" ] && log_info "чужая установка: бинари 3x-ui заменяются проверенными (sha256)"
    [ -n "$installed_ver" ] && log_info "3x-ui $installed_ver → $WANT_VER"
    sum="$(version_for XUI_SHA256 "$ZOO_ARCH")"
    tarball="/var/cache/vpn-zoo/x-ui-${XUI_VERSION}-linux-${ZOO_ARCH}.tar.gz"
    download_verified "$XUI_URL_BASE/x-ui-linux-${ZOO_ARCH}.tar.gz" "$sum" "$tarball"

    # Распаковка и проверка — до остановки работающей панели: при битом архиве
    # старая установка остаётся нетронутой
    tmpd="$(mktemp -d /usr/local/.x-ui-new.XXXXXX)"
    trap 'rm -rf "$tmpd"' EXIT
    # --no-same-owner: в архиве владелец uid 1001 (сборщик GitHub) — на VPS это может быть
    # обычный пользователь, который тогда подменит бинари, запускаемые от root
    tar --no-same-owner -xzf "$tarball" -C "$tmpd" || die "не удалось распаковать $tarball"
    [ -x "$tmpd/x-ui/x-ui" ] || die "в архиве нет x-ui/x-ui"
    [ -f "$tmpd/x-ui/bin/xray-linux-$ZOO_ARCH" ] || die "в архиве нет bin/xray-linux-$ZOO_ARCH"
    [ -f "$tmpd/x-ui/x-ui.service.debian" ] || die "в архиве нет x-ui.service.debian"

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

    mv "$tmpd/x-ui" "$XUI_DIR"
    rm -rf "$tmpd"
    trap - EXIT
    chmod 755 "$XUI_DIR/x-ui" "$XRAY_BIN"
    [ -f "$XUI_DIR/bin/mtg-linux-$ZOO_ARCH" ] && chmod 755 "$XUI_DIR/bin/mtg-linux-$ZOO_ARCH"
    install -m 755 "$XUI_DIR/x-ui.sh" /usr/bin/x-ui
    # в архиве нет общего x-ui.service, только .debian/.arch/.rhel
    install -m 644 "$XUI_DIR/x-ui.service.debian" "$XUI_SERVICE"
    systemctl daemon-reload
    fresh_bin=1
    log_ok "3x-ui $WANT_VER распакован в $XUI_DIR"
fi

# Установки до --no-same-owner (и v1) оставили файлы uid 1001 — бинари root обязаны быть root
if [ -n "$(find "$XUI_DIR" \( ! -user root -o ! -group root \) -print -quit 2>/dev/null)" ]; then
    chown -R root:root "$XUI_DIR"
    log_ok "$XUI_DIR: владелец исправлен на root (в архиве был uid 1001)"
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

# Подписка по умолчанию слушает 0.0.0.0:2096 — выключаем в БД до первого старта,
# чтобы порт не открывался даже на секунды. Окончательно проверяется через API (п. 7)
if [ "${SUB_PUBLIC:-0}" != "1" ] && ! systemctl is-active --quiet x-ui; then
    if xui_db_preseed subEnable false && xui_db_preseed subListen 127.0.0.1; then
        log_info "подписка выключена в БД до старта панели"
    else
        log_warn "не удалось записать subEnable в БД напрямую — выключу через API после старта"
    fi
fi

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
elif [ "$foreign_takeover" = "1" ] || [ "${PANEL_CREDS_SET:-0}" != "1" ]; then
    if [ "${ZOO_FORCE:-0}" = "1" ]; then
        # чужая установка под --force: старый пароль неизвестен, API без него не сменит
        log_warn "--force: задаю свои логин/пароль панели через x-ui setting (пароль кратко виден в списке процессов), чужая 2FA сбрасывается"
        xui_cli_set_credentials "$PANEL_USER" "$PANEL_PASS" || die "не удалось задать логин/пароль панели"
        _xui_cli_setting -resetTwoFactor >/dev/null || log_warn "сброс 2FA не удался"
        systemctl restart x-ui
        xui_wait_api 60 || die "API 3x-ui не поднялся после смены пароля"
        config_set PANEL_CREDS_SET 1
    else
        log_warn "у панели уже не admin/admin и пароль задавал не этот инсталлер — PANEL_USER/PANEL_PASS в config.env могут не подходить (задать свои: install.sh --phase 03 --force)"
    fi
fi

# ------------------------------------------------------------
# 7. Подписка и 2FA
# ------------------------------------------------------------

settings="$(xui_settings_get)"
sub_enable="$(jq -r '.subEnable' <<< "$settings")"
sub_listen="$(jq -r '.subListen' <<< "$settings")"
need_restart=0

# JSON-подписка не включается никогда: её шаблон кладёт клиенту SOCKS 127.0.0.1:10808
# без пароля — канал утечки IP сервера (RISK-REDUCTION §5)
if [ "$(jq -r '.subJsonEnable' <<< "$settings")" != "false" ]; then
    xui_settings_update '{"subJsonEnable":false}'
    need_restart=1
    log_ok "JSON-подписка 3x-ui выключена (локальный SOCKS без пароля у клиента)"
fi

if [ "${SUB_PUBLIC:-0}" = "1" ]; then
    # D8: с доменом и TLS подписку включают явно; настройку домена делает отдельная фаза
    if [ "$sub_enable" != "true" ]; then
        xui_settings_update '{"subEnable":true}'; need_restart=1
    fi
    # Happ по заголовкам подписки сам ставит пароль на свой локальный прокси (RISK-REDUCTION §5)
    if [ "$(jq -r '.subHappAutoDetect' <<< "$settings")" != "true" ] \
       || [ "$(jq -r '.subHappLocalProxyAuth' <<< "$settings")" != "auto" ]; then
        xui_settings_update '{"subHappAutoDetect":true,"subHappLocalProxyAuth":"auto"}'; need_restart=1
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
[[ "$(ss -Hltn "sport = :$PANEL_PORT")" == *"127.0.0.1:$PANEL_PORT"* ]] || die "панель не слушает 127.0.0.1:$PANEL_PORT"

mark_owned x-ui
config_set XUI_INSTALLED_VERSION "$XUI_VERSION"

log_ok "3x-ui $WANT_VER готов: панель 127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/"
log_info "доступ: ssh -N -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} root@${SERVER_IP} → http://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/"
log_info "логин: ${PANEL_USER}, пароль: PANEL_PASS в $CONFIG_FILE"
