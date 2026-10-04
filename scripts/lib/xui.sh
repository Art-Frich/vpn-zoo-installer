#!/usr/bin/env bash
# lib/xui.sh — единственный адаптер к 3x-ui (CLI x-ui и REST /panel/api).
# Сверено с исходниками тега v3.9.0 (internal/web/controller/*.go, main.go)
# и живыми вызовами на стенде. source'ится после lib.sh:
#   . "$SCRIPTS_DIR/lib/xui.sh"
#
# Ответ API всегда {success, msg, obj}. Функции xui_* печатают .obj (JSON),
# при success=false или HTTP != 200 пишут .msg в stderr и возвращают 1.
# Без заголовка Authorization API отвечает 404 (маскировка), с неверным Bearer — 401.

XUI_DIR="${XUI_DIR:-/usr/local/x-ui}"
XUI_BIN="${XUI_BIN:-$XUI_DIR/x-ui}"
XUI_DB="${XUI_DB:-/etc/x-ui/x-ui.db}"
XUI_HDR_FILE="${XUI_HDR_FILE:-$VPN_ETC/xui-auth.hdr}"
XUI_TOKEN_NAME="${XUI_TOKEN_NAME:-vpn-zoo}"

# Бинарь Xray внутри 3x-ui для текущей архитектуры
xui_xray_bin() {
    local arch
    arch="${ZOO_ARCH:-$(detect_arch)}"
    printf '%s/bin/xray-linux-%s\n' "$XUI_DIR" "$arch"
}

# ---------- CLI (работает без запущенной панели, пишет прямо в БД) ----------

# Версия установленной панели без «v»: 3.9.0
xui_cli_version() { [ -x "$XUI_BIN" ] && "$XUI_BIN" -v 2>/dev/null | tr -d '[:space:]'; }

# x-ui setting не возвращает ненулевой код при ошибке — проверяем вывод
_xui_cli_setting() {
    local out
    out="$("$XUI_BIN" setting "$@" 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    if grep -qiE 'failed|error|invalid|warning: ignored' <<< "$out"; then
        printf '%s\n' "$out" >&2
        return 1
    fi
    printf '%s\n' "$out"
}

# xui_cli_set_web PORT BASEPATH LISTEN — порт, путь и адрес панели (БД; нужен рестарт x-ui)
xui_cli_set_web() {
    _xui_cli_setting -port "$1" -webBasePath "$2" -listenIP "$3" >/dev/null
}

# xui_cli_set_credentials USER PASS — только когда API не может (неизвестен старый
# пароль: чужая установка под --force). Пароль кратко виден в списке процессов (D13)
xui_cli_set_credentials() {
    _xui_cli_setting -username "$1" -password "$2" >/dev/null
}

# Ключ таблицы settings до первого старта панели (CLI x-ui таких флагов не имеет).
# Нужен, чтобы подписка не открылась на 0.0.0.0:2096 даже на секунды. Только SQLite;
# дальше значения всё равно проверяются и выставляются через API (xui_settings_update)
xui_db_preseed() {
    local key="$1" value="$2"
    [ -f "$XUI_DB" ] && command -v sqlite3 >/dev/null || return 1
    [[ "$key" =~ ^[A-Za-z0-9]+$ ]] || return 1
    sqlite3 "$XUI_DB" >/dev/null 2>&1 <<SQL
UPDATE settings SET value = '${value//\'/\'\'}' WHERE key = '$key';
INSERT INTO settings(key, value) SELECT '$key', '${value//\'/\'\'}'
    WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = '$key');
SQL
}

# Текущие port/webBasePath/listenIP из БД: печатает три строки key=value
xui_cli_show() {
    "$XUI_BIN" setting -show -getListen 2>/dev/null | awk -F': ' '
        $1 == "port" {print "port=" $2}
        $1 == "webBasePath" {print "webBasePath=" $2}
        $1 == "listenIP" {print "listenIP=" $2}
        $1 == "hasDefaultCredential" {print "hasDefaultCredential=" $2}'
}

# Перевыпуск токена с именем $XUI_TOKEN_NAME (scope admin). Старый токен с этим
# именем перестаёт действовать. Сам токен печатается только в stdout этой функции.
xui_cli_issue_token() {
    local out tok
    out="$("$XUI_BIN" setting -getApiToken -tokenName "$XUI_TOKEN_NAME" -tokenScope admin 2>&1)" || true
    tok="$(awk '/^apiToken:/ {print $2}' <<< "$out" | tail -1)"
    [ -n "$tok" ] || { printf '%s\n' "$out" >&2; return 1; }
    printf '%s\n' "$tok"
}

# ---------- токен и базовый URL ----------

xui_base_url() {
    local path="${PANEL_PATH:-}"
    path="${path#/}"; path="${path%/}"
    [ -n "${PANEL_PORT:-}" ] || die "xui: PANEL_PORT не задан (фаза 03 не выполнена?)"
    if [ -n "$path" ]; then
        printf 'http://127.0.0.1:%s/%s\n' "$PANEL_PORT" "$path"
    else
        printf 'http://127.0.0.1:%s\n' "$PANEL_PORT"
    fi
}

# Заголовок с токеном в файле 0600: токен не попадает в argv curl
_xui_write_hdr() {
    ( umask 077; mkdir -p "$(dirname "$XUI_HDR_FILE")"; printf 'Authorization: Bearer %s\n' "$1" > "$XUI_HDR_FILE" )
}

xui_hdr() {
    if [ ! -s "$XUI_HDR_FILE" ]; then
        [ -n "${XUI_API_TOKEN:-}" ] || XUI_API_TOKEN="$(config_get XUI_API_TOKEN)"
        [ -n "${XUI_API_TOKEN:-}" ] || die "xui: нет API-токена (XUI_API_TOKEN) — запусти фазу 03-3xui"
        _xui_write_hdr "$XUI_API_TOKEN"
    fi
    printf '%s\n' "$XUI_HDR_FILE"
}

# Выпустить новый токен через CLI и сохранить в config.env + файл заголовка
xui_token_refresh() {
    local tok
    tok="$(xui_cli_issue_token)" || die "xui: x-ui setting -getApiToken не вернул токен"
    config_set XUI_API_TOKEN "$tok"
    _xui_write_hdr "$tok"
}

# ---------- HTTP ----------

# Сегмент пути URL (в email клиента бывают @, пробел, /): gin сам декодирует %XX
_xui_uri() { jq -rn --arg s "$1" '$s|@uri'; }

# xui_api METHOD PATH [BODY_JSON|-] — PATH относительно /panel/api (например inbounds/list).
# Тело JSON идёт через stdin curl. Печатает полный ответ {success,msg,obj}.
xui_api() {
    local method="$1" path="${2#/}" body="${3-}" url resp code hdr
    url="$(xui_base_url)/panel/api/$path"
    hdr="$(xui_hdr)"
    resp="$(mktemp)"
    if [ "$method" = "GET" ]; then
        code="$(curl -sS -o "$resp" -w '%{http_code}' --max-time 60 -H @"$hdr" "$url")" || code="000"
    else
        [ "$body" = "-" ] && body="$(cat)"
        code="$(printf '%s' "${body:-}" | curl -sS -o "$resp" -w '%{http_code}' --max-time 120 \
            -X "$method" -H @"$hdr" -H 'Content-Type: application/json' --data-binary @- "$url")" || code="000"
    fi
    if [ "$code" != "200" ]; then
        log_err "xui: $method $path → HTTP $code $(head -c 300 "$resp" 2>/dev/null)"
        rm -f "$resp"
        return 1
    fi
    if ! jq -e '.success == true' "$resp" >/dev/null 2>&1; then
        log_err "xui: $method $path → $(jq -r '.msg // "ответ не JSON"' "$resp" 2>/dev/null | head -c 500)"
        rm -f "$resp"
        return 1
    fi
    cat "$resp"
    rm -f "$resp"
}

# POST с form-urlencoded (xray/update читает PostForm): xui_api_form PATH key=@file|key=value...
xui_api_form() {
    local path="${1#/}" url resp code hdr arg
    shift
    url="$(xui_base_url)/panel/api/$path"
    hdr="$(xui_hdr)"
    local args=()
    for arg in "$@"; do args+=(--data-urlencode "$arg"); done
    resp="$(mktemp)"
    code="$(curl -sS -o "$resp" -w '%{http_code}' --max-time 120 -H @"$hdr" "${args[@]}" "$url")" || code="000"
    if [ "$code" != "200" ] || ! jq -e '.success == true' "$resp" >/dev/null 2>&1; then
        log_err "xui: POST $path → HTTP $code $(jq -r '.msg // empty' "$resp" 2>/dev/null | head -c 500)"
        rm -f "$resp"
        return 1
    fi
    cat "$resp"
    rm -f "$resp"
}

xui_get()  { xui_api GET "$1" | jq -c '.obj'; }
xui_post() { xui_api POST "$1" "${2-}" | jq -c '.obj'; }

# HTTP-код без проверки success (диагностика: 404 = нет токена/неверный путь, 401 = неверный токен)
xui_http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H @"$(xui_hdr)" "$(xui_base_url)/panel/api/${1:-server/status}" || true
}

# Ждать готовности API до N секунд. При 401 один раз перевыпускает токен.
xui_wait_api() {
    local timeout="${1:-60}" code refreshed=0
    for _ in $(seq 1 "$timeout"); do
        code="$(xui_http_code server/status)"
        case "$code" in
            200) return 0 ;;
            401)
                if [ "$refreshed" = "0" ]; then
                    log_warn "xui: токен отвергнут (401) — перевыпускаю"
                    xui_token_refresh; refreshed=1
                fi ;;
        esac
        sleep 1
    done
    log_err "xui: API не ответил за ${timeout} с (последний HTTP-код: ${code:-нет})"
    return 1
}

# ---------- сервер / Xray ----------

xui_server_status() { xui_get server/status; }

# Версия Xray, которую запустила панель (из server/status → .xray.version)
xui_xray_version() { xui_get server/status | jq -r '.xray.version'; }
xui_xray_state()   { xui_get server/status | jq -r '.xray.state'; }
xui_panel_version() { xui_get server/status | jq -r '.panelVersion'; }

# Перезапуск Xray (применить изменения сразу; иначе панель применит в течение 30 с)
xui_xray_restart() { xui_api POST server/restartXrayService >/dev/null; }

# ---------- inbounds ----------
# Тело inbounds/add: settings/streamSettings/sniffing можно передавать объектами.
# В ответе list/get они тоже объекты. Клиенты из settings.clients попадают в таблицы
# clients/client_inbounds сами; email обязателен, tgId — число.

xui_inbound_list() { xui_get inbounds/list; }
xui_inbound_get()  { xui_get "inbounds/get/$1"; }

# id inbound по порту (и протоколу), пусто если нет
xui_inbound_find_by_port() {
    local port="$1" proto="${2:-}"
    xui_inbound_list | jq -r --argjson p "$port" --arg pr "$proto" \
        '[.[] | select(.port == $p and ($pr == "" or .protocol == $pr))][0].id // empty'
}

xui_inbound_find_by_remark() {
    xui_inbound_list | jq -r --arg r "$1" '[.[] | select(.remark == $r)][0].id // empty'
}

# xui_inbound_add JSON → печатает id нового inbound
xui_inbound_add() { xui_post inbounds/add "$1" | jq -r '.id'; }

# Полная замена полей inbound (в v3.9.0 клиентов не трогает — для них clients/*)
xui_inbound_update() { xui_post "inbounds/update/$1" "$2" >/dev/null; }

xui_inbound_set_enable() {
    local en="${2:-true}"
    xui_post "inbounds/setEnable/$1" "{\"enable\":$en}" >/dev/null
}

# Внимание (v3.9.0): del несуществующего id тоже отвечает success; клиенты удалённого
# inbound остаются в таблице clients без привязки (inboundIds=[]) — см. xui_client_del_orphans
xui_inbound_del() { xui_api POST "inbounds/del/$1" >/dev/null; }

# ---------- клиенты ----------

xui_client_list() { xui_get clients/list; }

# {client:{...,uuid,...}, inboundIds:[...], externalLinks, tunnelAllowedIPs, usedTraffic}
xui_client_get() { xui_get "clients/get/$(_xui_uri "$1")"; }

xui_client_exists() {
    xui_client_list | jq -e --arg e "$1" 'any(.[]; .email == $e)' >/dev/null
}

# xui_client_add CLIENT_JSON IDS — IDS через запятую: 1,2,3
# CLIENT_JSON — model.Client: {id(uuid для vless), email, flow, enable, subId, tgId(число), ...}
xui_client_add() {
    local ids="[${2}]"
    jq -cn --argjson c "$1" --argjson ids "$ids" '{client:$c, inboundIds:$ids}' \
        | xui_api POST clients/add - >/dev/null
}

xui_client_attach() { xui_api POST "clients/$(_xui_uri "$1")/attach" "{\"inboundIds\":[${2}]}" >/dev/null; }
xui_client_detach() { xui_api POST "clients/$(_xui_uri "$1")/detach" "{\"inboundIds\":[${2}]}" >/dev/null; }

# xui_client_update EMAIL PATCH_JSON — read-modify-write: частичное тело обнуляет
# поля (проверено: без flow сбрасывается flow), поэтому берём текущую запись целиком
xui_client_update() {
    local email="$1" patch="$2"
    xui_client_get "$email" | jq -c --argjson p "$patch" '
        .client
        | {id: .uuid, email, subId, password, auth, flow, security, privateKey, publicKey,
           preSharedKey, secret, adTag, limitIp, limitHwid, totalGB, expiryTime, enable,
           tgId, group, comment, reset,
           allowedIPs: (if (.allowedIPs|type) == "string"
                        then (.allowedIPs | if . == "" then [] else split(",") end)
                        else .allowedIPs end)}
        | with_entries(select(.value != "" and .value != null))
        | . + $p' \
        | xui_api POST "clients/update/$(_xui_uri "$email")" - >/dev/null
}

# Несуществующий email → success=false («not found in any inbound or client record»)
xui_client_del() { xui_api POST "clients/del/$(_xui_uri "$1")" >/dev/null; }

# Удалить клиентов без единого inbound; печатает число удалённых
xui_client_del_orphans() { xui_post clients/delOrphans | jq -r '.deleted'; }

# Ссылки клиента по всем inbound. Хост в ссылке панель берёт из заголовка Host запроса,
# поэтому передаём адрес сервера
xui_client_links() {
    local email="$1" host="${2:-${SERVER_IP:-127.0.0.1}}"
    curl -sS --max-time 30 -H @"$(xui_hdr)" -H "Host: $host" \
        "$(xui_base_url)/panel/api/clients/links/$(_xui_uri "$email")" | jq -c '.obj // []'
}

# ---------- настройки панели ----------

# Все настройки (секреты вроде tgBotToken в ответе скрыты; пустое значение при
# сохранении означает «не менять»)
xui_settings_get() { xui_api POST setting/all | jq -c '.obj'; }

# xui_settings_update PATCH_JSON — merge поверх текущих и сохранить. Поля веб-сервера
# и подписки применяются только после рестарта панели (systemctl restart x-ui)
xui_settings_update() {
    local patch="$1"
    xui_settings_get | jq -c --argjson p "$patch" '. + $p' | xui_api POST setting/update - >/dev/null
}

# Смена логина/пароля через API: пароль идёт телом запроса, не в argv
xui_set_credentials() {
    local old_user="$1" old_pass="$2" new_user="$3" new_pass="$4"
    jq -cn --arg ou "$old_user" --arg op "$old_pass" --arg nu "$new_user" --arg np "$new_pass" \
        '{oldUsername:$ou, oldPassword:$op, newUsername:$nu, newPassword:$np}' \
        | xui_api POST setting/updateUser - >/dev/null
}

# ---------- шаблон конфигурации Xray ----------
# Хранится в БД панели; inbounds из таблицы панель подмешивает сама при генерации
# bin/config.json. Правило api панель всегда держит первым.

xui_xray_template_get() {
    xui_api POST xray/ | jq -r '.obj' | jq -c '.xraySetting'
}

# xui_xray_template_set JSON — сохраняет шаблон, перезапускает работающий Xray.
# outboundTestUrl передаём текущий, иначе панель сбросит его на google
xui_xray_template_set() {
    local tmp test_url
    tmp="$(mktemp)"
    printf '%s' "$1" | jq -e . > "$tmp" || { rm -f "$tmp"; log_err "xui: шаблон Xray — невалидный JSON"; return 1; }
    test_url="$(xui_api POST xray/ | jq -r '.obj' | jq -r '.outboundTestUrl // empty')"
    if xui_api_form xray/update "xraySetting@$tmp" "outboundTestUrl=${test_url:-https://www.google.com/generate_204}" >/dev/null; then
        rm -f "$tmp"
    else
        rm -f "$tmp"
        return 1
    fi
}
