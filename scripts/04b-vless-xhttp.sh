#!/usr/bin/env bash
# 04b-vless-xhttp.sh — VLESS + XHTTP + REALITY: запасной TCP-транспорт (ENABLE_XHTTP).
#
# XHTTP_PLACEMENT=port (по умолчанию): отдельный inbound на случайном высоком порту
#   XHTTP_PORT, свои ключи REALITY (XHTTP_PRIV/XHTTP_PUB/XHTTP_SID) и свой target (XHTTP_SNI).
# XHTTP_PLACEMENT=fallback: child без TLS на 127.0.0.1:XHTTP_PORT за VLESS-inbound фазы 04
#   (443, RAW+REALITY+Vision) через POST inbounds/:id/fallbacks; ключи и SNI — master.
# Path и mode: XHTTP_PATH (случайный), XHTTP_MODE=auto (сервер принимает все режимы,
# клиент с REALITY выбирает stream-one; packet-up — XHTTP_MODE=packet-up).
# Пользователь owner подключается к inbound (существующий клиент 3x-ui — через attach).
#
# Почему по умолчанию отдельный порт (оба варианта проверены e2e на 3x-ui v3.9.0):
# - запасной транспорт не падает вместе с основным: другой порт (по отчётам из РФ
#   смена порта с 443 помогала чаще всего), свой SNI, свои ключи;
# - не зависит от фазы 04 (ENABLE_VLESS=0, пересоздание её inbound стирает fallback);
# - схема из официальных Xray-examples, ссылку панель строит штатно.
# Цена: REALITY не на 443 — Xray пишет предупреждение, target отвечает на 443, а не
# на нашем порту. Кому это важнее — XHTTP_PLACEMENT=fallback (один 443 на всё).

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-vless-xhttp.sh"
config_load
detect_arch >/dev/null

XRAY_BIN="$(xui_xray_bin)"
[ -x "$XRAY_BIN" ] || die "нет $XRAY_BIN — сначала фаза 03-3xui"
xui_wait_api 60 || die "API 3x-ui не отвечает — проверь: systemctl status x-ui"

# Выключено флагом (--phase запускает и выключенную фазу): inbound выключить, порт закрыть
if [ "${ENABLE_XHTTP:-1}" != "1" ]; then
    id=""
    if config_has XHTTP_PORT; then
        config_default XHTTP_PORT ""; config_default XHTTP_PLACEMENT port
        id="$(proto_vless_xhttp_inbound_id)"
    fi
    if [ -n "$id" ]; then
        xui_inbound_set_enable "$id" false || die "3x-ui не выключил inbound $id"
        [ "$XHTTP_PLACEMENT" != "port" ] || fw_revoke "$XHTTP_PORT/tcp"
        proto_vless_xhttp_manifest_refresh || log_warn "манифест vless-xhttp не обновлён"
        log_ok "ENABLE_XHTTP=$ENABLE_XHTTP: XHTTP-inbound выключен, порт закрыт"
    else
        log_info "ENABLE_XHTTP=$ENABLE_XHTTP: XHTTP не ставится"
    fi
    exit 0
fi

# ------------------------------------------------------------
# 1. Параметры (генерируются один раз, хранятся в config.env)
# Ключи XHTTP_* можно задать окружением при первом запуске
# ------------------------------------------------------------

# Значение через переменную: ошибка внутри $(...) в аргументе config_set не ловится set -e
set_once() {
    local key="$1" val; shift
    if config_has "$key"; then config_default "$key" ""; return 0; fi
    if [ -n "${!key:-}" ]; then config_set "$key" "${!key}"; return 0; fi
    val="$("$@")" || die "не удалось сгенерировать $key"
    [ -n "$val" ] || die "пустое значение $key"
    config_set "$key" "$val"
}

set_once XHTTP_PLACEMENT echo port
case "$XHTTP_PLACEMENT" in
    port|fallback) ;;
    *) die "XHTTP_PLACEMENT=$XHTTP_PLACEMENT: допустимо port или fallback" ;;
esac
set_once XHTTP_MODE echo auto
case "$XHTTP_MODE" in
    auto|packet-up|stream-up|stream-one) ;;
    *) die "XHTTP_MODE=$XHTTP_MODE: допустимо auto, packet-up, stream-up, stream-one" ;;
esac
gen_path() { printf '/%s\n' "$(gen_random_alnum 16 | tr '[:upper:]' '[:lower:]')"; }
set_once XHTTP_PATH gen_path
[[ "$XHTTP_PATH" =~ ^/[A-Za-z0-9_-]{4,64}$ ]] || die "XHTTP_PATH=$XHTTP_PATH: нужен вид /abc123 (A-Z a-z 0-9 _ -)"

existing_id=""
old_port=""
config_has XHTTP_PORT && config_default XHTTP_PORT ""
existing_id="$(proto_vless_xhttp_inbound_id)"
[ -z "$existing_id" ] || old_port="$(xui_inbound_get "$existing_id" | jq -r '.port')"
if [ -z "$existing_id" ] || [ "$old_port" != "${XHTTP_PORT:-}" ]; then
    # новый inbound или новый порт: порт не должен быть занят (env-значение проверяем так же)
    if [ -n "${XHTTP_PORT:-}" ]; then
        [[ "$XHTTP_PORT" =~ ^[0-9]+$ ]] && [ "$XHTTP_PORT" -ge 1 ] && [ "$XHTTP_PORT" -le 65535 ] \
            || die "XHTTP_PORT=$XHTTP_PORT: не номер порта"
        is_banned_port "$XHTTP_PORT" && die "XHTTP_PORT=$XHTTP_PORT из запрещённого списка ($BANNED_PORTS)"
        [ -n "$(ss -Hltn "sport = :$XHTTP_PORT" 2>/dev/null)" ] && die "XHTTP_PORT=$XHTTP_PORT уже слушает другой процесс: $(ss -Hltnp "sport = :$XHTTP_PORT" | head -1)"
        [ -n "$(xui_inbound_find_by_port "$XHTTP_PORT")" ] && die "XHTTP_PORT=$XHTTP_PORT занят другим inbound 3x-ui"
        config_set XHTTP_PORT "$XHTTP_PORT"
    else
        port="$(rand_port)" || die "не нашёл свободный порт"
        config_set XHTTP_PORT "$port"
    fi
fi

master_id=""
if [ "$XHTTP_PLACEMENT" = "fallback" ]; then
    master_id="$(proto_vless_xhttp_master_id)"
    [ -n "$master_id" ] || die "XHTTP_PLACEMENT=fallback: нет VLESS RAW+REALITY inbound на порту ${VLESS_PORT:-443} — нужна фаза 04 (ENABLE_VLESS=1) или XHTTP_PLACEMENT=port"
    master_sec="$(xui_inbound_get "$master_id" | jq -r '.settings.decryption // "none"')"
    [ "$master_sec" = "none" ] || die "XHTTP_PLACEMENT=fallback: у master включено VLESS Encryption (decryption=$master_sec) — Xray не совмещает его с fallbacks"
    # до любых изменений: чужой fallback «на всё» у master занял бы наше место
    if xui_get "inbounds/$master_id/fallbacks" | jq -e --argjson c "${existing_id:-0}" \
        'any(.[]; .childId != $c and .name == "" and .alpn == "" and .path == "")' >/dev/null; then
        die "у master-inbound $master_id уже есть fallback без name/alpn/path (не наш) — XHTTP-child некуда поставить. Убери его или XHTTP_PLACEMENT=port"
    fi
else
    gen_x25519() { "$XRAY_BIN" x25519; }
    if ! config_has XHTTP_PRIV || ! config_has XHTTP_PUB; then
        keys="$(gen_x25519)" || die "xray x25519 не сработал"
        priv="$(awk -F': ' '/^PrivateKey/ {print $2}' <<< "$keys")"
        pub="$(awk -F': ' '/^Password/ {print $2}' <<< "$keys")"
        [ "${#priv}" -eq 43 ] && [ "${#pub}" -eq 43 ] || die "не разобрал вывод xray x25519: $keys"
        config_set XHTTP_PRIV "$priv"
        config_set XHTTP_PUB "$pub"
        log_ok "ключи REALITY для XHTTP сгенерированы (свои, не общие с 04)"
    else
        config_default XHTTP_PRIV ""; config_default XHTTP_PUB ""
    fi
    set_once XHTTP_SID gen_random_hex 8

    # target: свой, не совпадающий с SNI фазы 04
    if config_has XHTTP_SNI; then
        config_default XHTTP_SNI ""
        proto_vless_xhttp_target_ok "$XHTTP_SNI" \
            || log_warn "REALITY target $XHTTP_SNI сейчас не проходит проверку (TLS 1.3 + X25519MLKEM768 + h2) — сменить: XHTTP_SNI=... --phase 04b"
    elif [ -n "${XHTTP_SNI:-}" ]; then
        if proto_vless_xhttp_target_ok "$XHTTP_SNI"; then
            config_set XHTTP_SNI "$XHTTP_SNI"
        elif [ "${ZOO_FORCE:-0}" = "1" ]; then
            log_warn "XHTTP_SNI=$XHTTP_SNI не прошёл проверку — --force, оставляю"
            config_set XHTTP_SNI "$XHTTP_SNI"
        else
            die "XHTTP_SNI=$XHTTP_SNI не годится в REALITY target: ${REALITY_REASON:-нет X25519MLKEM768}. Оставить всё равно: --force"
        fi
    else
        busy=" $(config_get VLESS_SNI) "
        vid="$(proto_vless_xhttp_master_id)"
        [ -n "$vid" ] && busy+="$(xui_inbound_get "$vid" | jq -r '.streamSettings.realitySettings.serverNames // [] | join(" ")') "
        picked=""
        for h in $(tr ' ' '\n' <<< "$XHTTP_SNI_CANDIDATES" | shuf); do
            [[ "$busy" == *" $h "* ]] && continue
            log_info "проверяю REALITY target $h"
            if proto_vless_xhttp_target_ok "$h"; then picked="$h"; break; fi
        done
        [ -n "$picked" ] || die "ни один кандидат REALITY target не прошёл проверку ($XHTTP_SNI_CANDIDATES). Задай свой: XHTTP_SNI=host"
        config_set XHTTP_SNI "$picked"
        log_ok "REALITY target для XHTTP: $picked"
    fi
fi

# ------------------------------------------------------------
# 2. Inbound
# ------------------------------------------------------------

REMARK="${LABEL:-vpn}-xhttp"
: "${XHTTP_SID:=}"
if [ "$XHTTP_PLACEMENT" = "port" ]; then
    body="$(jq -cn --arg r "$REMARK" --argjson port "$XHTTP_PORT" --arg path "$XHTTP_PATH" --arg mode "$XHTTP_MODE" \
        --arg sni "$XHTTP_SNI" --arg priv "$XHTTP_PRIV" --arg pub "$XHTTP_PUB" --arg sid "$XHTTP_SID" '{
        remark:$r, enable:true, listen:"", port:$port, protocol:"vless", expiryTime:0, total:0,
        settings:{clients:[], decryption:"none", fallbacks:[]},
        streamSettings:{network:"xhttp", security:"reality",
          xhttpSettings:{path:$path, host:"", mode:$mode, headers:{}},
          realitySettings:{show:false, xver:0, target:($sni + ":443"), serverNames:[$sni],
            privateKey:$priv, shortIds:[$sid], minClientVer:"", maxClientVer:"",
            settings:{publicKey:$pub, fingerprint:"chrome", spiderX:"/"}}},
        sniffing:{enabled:true, destOverride:["http","tls","quic"], routeOnly:true}}')"
else
    body="$(jq -cn --arg r "$REMARK" --argjson port "$XHTTP_PORT" --arg path "$XHTTP_PATH" --arg mode "$XHTTP_MODE" '{
        remark:$r, enable:true, listen:"127.0.0.1", port:$port, protocol:"vless", expiryTime:0, total:0,
        settings:{clients:[], decryption:"none", fallbacks:[]},
        streamSettings:{network:"xhttp", security:"none",
          xhttpSettings:{path:$path, host:"", mode:$mode, headers:{}}},
        sniffing:{enabled:true, destOverride:["http","tls","quic"], routeOnly:true}}')"
fi

# Сравниваем только то, что задаёт фаза: лишних обновлений (и перезапусков Xray) нет
# shellcheck disable=SC2016 # jq-выражение
proj='(.streamSettings | if type == "string" then fromjson else . end) as $s
    | (.sniffing | if type == "string" then fromjson else . end) as $n
    | {remark, listen: ((.listen // "") | if . == "0.0.0.0" then "" else . end), port, sec: $s.security,
       path: $s.xhttpSettings.path, mode: $s.xhttpSettings.mode,
       target: ($s.realitySettings.target // null), sn: ($s.realitySettings.serverNames // null),
       priv: ($s.realitySettings.privateKey // null), sids: ($s.realitySettings.shortIds // null),
       sniff: [$n.enabled, ($n.routeOnly // false)]}'

inbound_id="$(proto_vless_xhttp_inbound_id)"
if [ -z "$inbound_id" ]; then
    inbound_id="$(xui_inbound_add "$body")" || die "3x-ui не создал XHTTP-inbound"
    [ -n "$inbound_id" ] && [ "$inbound_id" != "null" ] || die "3x-ui не вернул id нового inbound"
    log_ok "XHTTP-inbound создан (id=$inbound_id, порт $XHTTP_PORT, $XHTTP_PLACEMENT)"
else
    cur="$(xui_inbound_get "$inbound_id" | jq -cS "$proj")"
    want="$(jq -cS "$proj" <<< "$body")"
    if [ "$cur" != "$want" ]; then
        xui_inbound_update "$inbound_id" "$body" || die "3x-ui не обновил XHTTP-inbound $inbound_id"
        log_ok "XHTTP-inbound $inbound_id обновлён под config.env"
    else
        log_info "XHTTP-inbound $inbound_id уже настроен"
    fi
fi
# inbounds/update поле enable не меняет (v3.9.0) — только setEnable
if [ "$(xui_inbound_get "$inbound_id" | jq -r '.enable')" != "true" ]; then
    xui_inbound_set_enable "$inbound_id" true || die "3x-ui не включил inbound $inbound_id"
    log_ok "XHTTP-inbound $inbound_id включён"
fi

# ------------------------------------------------------------
# 3. Fallback у master (только XHTTP_PLACEMENT=fallback; иначе убрать свой след)
# Список fallback'ов master заменяется целиком — читаем, правим свою запись, пишем
# ------------------------------------------------------------

sync_fallback() {
    local mid="$1" want_child="$2" rows new
    rows="$(xui_get "inbounds/$mid/fallbacks")" || die "не прочитал fallbacks inbound $mid"
    new="$(jq -c --argjson c "$inbound_id" '[.[] | select(.childId != $c)
        | {childId, name, alpn, path, dest, xver, sortOrder}]' <<< "$rows")"
    if [ "$want_child" = "1" ]; then
        new="$(jq -c --argjson c "$inbound_id" '. + [{childId:$c, name:"", alpn:"", path:"", dest:"", xver:0, sortOrder:(length)}]' <<< "$new")"
    fi
    if [ "$(jq -c '[.[] | {childId, name, alpn, path, dest, xver}]' <<< "$rows")" != "$(jq -c '[.[] | {childId, name, alpn, path, dest, xver}]' <<< "$new")" ]; then
        xui_api POST "inbounds/$mid/fallbacks" "$(jq -cn --argjson f "$new" '{fallbacks:$f}')" >/dev/null \
            || die "3x-ui не принял fallbacks для inbound $mid"
        log_ok "fallbacks inbound $mid обновлены"
    fi
}

if [ "$XHTTP_PLACEMENT" = "fallback" ]; then
    sync_fallback "$master_id" 1
else
    vid="$(proto_vless_xhttp_master_id)"
    [ -z "$vid" ] || sync_fallback "$vid" 0
fi

# ------------------------------------------------------------
# 4. Firewall
# ------------------------------------------------------------

if [ -n "$old_port" ] && [ "$old_port" != "$XHTTP_PORT" ]; then
    fw_revoke "$old_port/tcp"
    log_info "старый порт XHTTP $old_port/tcp закрыт"
fi
if [ "$XHTTP_PLACEMENT" = "port" ]; then
    fw_allow "$XHTTP_PORT/tcp" "vless-xhttp"
elif [ -f "$PORTS_FILE" ] && awk -F'\t' -v s="$XHTTP_PORT/tcp" '$1 == s {f=1} END {exit !f}' "$PORTS_FILE"; then
    fw_revoke "$XHTTP_PORT/tcp"
    log_info "порт $XHTTP_PORT/tcp закрыт: в режиме fallback XHTTP слушает только 127.0.0.1"
fi

# ------------------------------------------------------------
# 5. Пользователь owner
# ------------------------------------------------------------

proto_vless_xhttp_user_add owner || die "не удалось подключить owner к XHTTP-inbound"

# ------------------------------------------------------------
# 6. Самопроверка
# ------------------------------------------------------------

wait_port "$XHTTP_PORT" tcp 35 || die "Xray не слушает порт XHTTP $XHTTP_PORT за 35 с: $(xui_server_status | jq -r '.xray.errorMsg // empty')"
listen_addr="$(ss -Hltn "sport = :$XHTTP_PORT" | awk '{print $4}' | head -1)"
if [ "$XHTTP_PLACEMENT" = "fallback" ]; then
    [[ "$listen_addr" == 127.0.0.1:* ]] || die "XHTTP-child слушает $listen_addr, ожидался 127.0.0.1"
    pport="$(proto_vless_xhttp_public_port)"
    wait_port "$pport" tcp 35 || die "master-inbound не слушает $pport"
fi
st="$(xui_xray_state)"
[ "$st" = "running" ] || die "Xray не запущен (state=$st): $(xui_server_status | jq -r '.xray.errorMsg // empty')"

proto_vless_xhttp_manifest_refresh || die "не удалось записать манифест vless-xhttp"
uri="$(proto_vless_xhttp_links owner | head -1)"
[ -n "$uri" ] || die "панель не выдала ссылку XHTTP для owner"

log_ok "VLESS+XHTTP+REALITY готов: $XHTTP_PLACEMENT, порт $(proto_vless_xhttp_public_port), path $XHTTP_PATH, mode $XHTTP_MODE"
log_info "ссылка owner: $ZOO_CLIENTS_DIR/owner/vless-xhttp.uri"
