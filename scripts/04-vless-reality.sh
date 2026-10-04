#!/usr/bin/env bash
# 04-vless-reality.sh — VLESS RAW+REALITY+Vision через API 3x-ui (lib/xui.sh).
#
# Порт VLESS_PORT (по умолчанию 443/tcp; старый 8443 из v1 переносится), target выбирает
# валидатор lib/reality-target.sh (или VLESS_SNI, проверка отключается VLESS_SNI_CHECK=0;
# свой target, например self-steal, — VLESS_TARGET=host:port). Ключи — встроенный xray x25519.
# Пользователь owner создаётся здесь; манифест — /etc/vpn-setup/protocols.d/vless-reality.json.
# Разовые переключатели: ZOO_VLESS_REPICK=1 — выбрать target заново.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/reality-target.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-vless-reality.sh"
config_load
versions_load
detect_arch >/dev/null

XRAY_BIN="$(xui_xray_bin)"
[ -x "$XRAY_BIN" ] || die "нет $XRAY_BIN — сначала фаза 03-3xui"
is_owned x-ui || die "3x-ui поставлен не этим инсталлером — сначала install.sh --phase 03 (--force для чужой установки)"
xui_wait_api 60 || die "API 3x-ui не отвечает — проверь: systemctl status x-ui"

LEGACY_RE='^reality-vision-'

# ------------------------------------------------------------
# 0. Выключено флагом (--phase запускает и выключенную фазу)
# ------------------------------------------------------------

if [ "${ENABLE_VLESS:-1}" != "1" ]; then
    id="$(xui_inbound_find_by_remark "$PVR_REMARK")"
    if [ -n "$id" ]; then
        xui_inbound_set_enable "$id" false
        fw_revoke "$(xui_inbound_get "$id" | jq -r '.port')/tcp"
        proto_vless_reality_manifest_refresh
        log_ok "ENABLE_VLESS=$ENABLE_VLESS: inbound выключен, порт закрыт"
    else
        log_info "ENABLE_VLESS=$ENABLE_VLESS: VLESS не ставится"
    fi
    exit 0
fi

# ------------------------------------------------------------
# 1. Inbound: наш, от v1 или чужой на порту
# ------------------------------------------------------------

inbounds="$(xui_inbound_list)" || die "не удалось получить список inbound"
inb_id="$(jq -r --arg r "$PVR_REMARK" '[.[] | select(.remark == $r)][0].id // empty' <<< "$inbounds")"
legacy_id="$(jq -r --arg re "$LEGACY_RE" '[.[] | select(.protocol == "vless" and (.remark | test($re)))][0].id // empty' <<< "$inbounds")"
if [ -z "$inb_id" ] && [ -n "$legacy_id" ]; then
    inb_id="$legacy_id"
    log_warn "найден inbound первой версии (id=$legacy_id) — переношу на новые параметры, его клиенты сохраняются"
fi
cur_inb=""
[ -z "$inb_id" ] || cur_inb="$(jq -c --argjson id "$inb_id" '.[] | select(.id == $id)' <<< "$inbounds")"

# ------------------------------------------------------------
# 2. Порт: 443, перенос 8443 от v1
# ------------------------------------------------------------

old_port=""
if [ -n "$cur_inb" ]; then
    old_port="$(jq -r '.port' <<< "$cur_inb")"
fi
# Первый проход v2 по установке v1: inbound v1 на месте или state.v1 без отметки о переносе
v1_now=0
if [ -n "$legacy_id" ] || { [ -f "${STATE_FILE}.v1" ] && [ "$(config_get VLESS_PORT_MIGRATED)" != "1" ]; }; then
    v1_now=1
fi
port="$(config_get VLESS_PORT)"
if [ "$port" = "8443" ] && [ "$v1_now" = "1" ]; then
    log_warn "VLESS на 8443 от первой версии → 443 (Xray ≥26.3.27 предупреждает о REALITY не на 443). Ссылки изменятся"
    port=443
fi
[ -n "$port" ] || port=443
[[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "VLESS_PORT: неверный порт «$port»"
is_banned_port "$port" && die "VLESS_PORT=$port из запрещённого списка ($BANNED_PORTS)"
config_set VLESS_PORT "$port"
config_set VLESS_PORT_MIGRATED 1

# Порт занят другим inbound панели?
other="$(jq -r --argjson p "$port" --arg id "${inb_id:-0}" \
    '[.[] | select(.port == $p and (.id|tostring) != $id)][0] | if . then "\(.id) \(.remark) \(.protocol)" else empty end' <<< "$inbounds")"
[ -z "$other" ] || die "порт $port/tcp уже занят inbound 3x-ui (id remark protocol: $other). Освободи его в панели или задай VLESS_PORT"

# Порт слушает не xray (nginx, caddy...)?
listener="$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -oE 'users:\(\("[^"]+"' | head -1 | sed 's/users:(("//' || true)"
if [ -n "$listener" ] && [[ "$listener" != xray* ]]; then
    die "порт $port/tcp занят процессом «$listener». Освободи порт или задай VLESS_PORT"
fi

# ------------------------------------------------------------
# 3. Ключи REALITY и shortId
# ------------------------------------------------------------

# Публичный ключ из приватного (формат вывода менялся: берём строку Password/Public key)
pub_from_priv() {
    "$XRAY_BIN" x25519 -i "$1" 2>/dev/null | awk -F': ' '/^(Password|Public key)/ {print $2; exit}'
}

priv="$(config_get VLESS_PRIV)"
sid="$(config_get VLESS_SID)"
if [ -z "$priv" ] && [ -n "$cur_inb" ]; then
    priv="$(jq -r '.streamSettings.realitySettings.privateKey // empty' <<< "$cur_inb")"
    [ -z "$priv" ] || log_info "ключ REALITY взят из существующего inbound"
fi
if [ -z "$sid" ] && [ -n "$cur_inb" ]; then
    sid="$(jq -r '.streamSettings.realitySettings.shortIds[0] // empty' <<< "$cur_inb")"
fi
pub=""
[ -z "$priv" ] || pub="$(pub_from_priv "$priv")"
if [ -z "$priv" ] || [ "${#pub}" -ne 43 ]; then
    [ -z "$priv" ] || log_warn "VLESS_PRIV не даёт публичный ключ — генерирую новую пару (ссылки изменятся)"
    keys="$("$XRAY_BIN" x25519)" || die "xray x25519 не сработал"
    priv="$(awk -F': ' '/^PrivateKey/ {print $2; exit}' <<< "$keys")"
    pub="$(awk -F': ' '/^(Password|Public key)/ {print $2; exit}' <<< "$keys")"
    [ "${#priv}" -eq 43 ] && [ "${#pub}" -eq 43 ] || die "не удалось разобрать вывод xray x25519: $keys"
    log_ok "сгенерирована пара ключей REALITY"
fi
if ! [[ "$sid" =~ ^([0-9a-f]{2}){1,8}$ ]]; then
    sid="$(gen_random_hex 8)"
fi
config_set VLESS_PRIV "$priv"
config_set VLESS_PUB "$pub"
config_set VLESS_SID "$sid"

# ------------------------------------------------------------
# 4. REALITY target (D10)
# ------------------------------------------------------------

sni="$(config_get VLESS_SNI)"
picked="$(config_get VLESS_SNI_PICKED)"
target_cfg="$(config_get VLESS_TARGET)"
need_pick=0

if [ -z "$sni" ] || [ "${ZOO_VLESS_REPICK:-0}" = "1" ]; then
    need_pick=1
elif [ "$sni" = "$picked" ]; then
    # выбран нами раньше: перепроверяем, но ссылки сами не меняем
    if ! reality_target_check "$sni" "${target_cfg:-}"; then
        sleep 5
        reality_target_check "$sni" "${target_cfg:-}" \
            || log_warn "REALITY target $sni больше не проходит проверку ($REALITY_REASON). Выбрать новый: ZOO_VLESS_REPICK=1 install.sh --phase 04 (ссылки изменятся)"
    fi
elif [ "$sni" = "www.cbr.ru" ] && [ "$v1_now" = "1" ] && [ -z "$target_cfg" ]; then
    log_warn "SNI www.cbr.ru от первой версии непригоден (.ru на зарубежном IP, нет ML-KEM) — выбираю новый target, ссылки изменятся"
    need_pick=1
elif [ "${VLESS_SNI_CHECK:-1}" = "0" ]; then
    log_warn "VLESS_SNI=$sni: проверка target отключена (VLESS_SNI_CHECK=0)"
elif reality_target_check "$sni" "${target_cfg:-}"; then
    log_ok "VLESS_SNI=$sni прошёл проверку (ML-KEM: $REALITY_PQ, цепочка $REALITY_CHAIN Б)"
else
    die "VLESS_SNI=$sni не годится как REALITY target: $REALITY_REASON. Выбери другой или отключи проверку VLESS_SNI_CHECK=0"
fi

if [ "$need_pick" = "1" ]; then
    log_info "выбираю REALITY target (xray tls ping: TLS 1.3, X25519MLKEM768/X25519, h2, цепочка ≤${REALITY_MAX_CHAIN} Б)"
    new_sni="$(REALITY_EXCLUDE="$(config_get XHTTP_SNI)" reality_target_select)" || die "ни один кандидат REALITY target не прошёл проверку (нет выхода в интернет?). Задай свой: VLESS_SNI=домен"
    sni="$new_sni"
    target_cfg=""
    config_set VLESS_SNI "$sni"
    config_set VLESS_SNI_PICKED "$sni"
    config_set VLESS_TARGET ""
    log_ok "REALITY target: $sni"
fi
target="${target_cfg:-$sni:443}"

# ------------------------------------------------------------
# 5. Inbound: создать или привести к нужному виду
# ------------------------------------------------------------

stream="$(jq -cn --arg target "$target" --arg sni "$sni" --arg priv "$priv" --arg pub "$pub" \
    --arg sid "$sid" --arg fp "$PVR_FP" '{
    network:"tcp", security:"reality",
    tcpSettings:{acceptProxyProtocol:false, header:{type:"none"}},
    realitySettings:{show:false, xver:0, target:$target, serverNames:[$sni], privateKey:$priv,
        minClientVer:"", maxClientVer:"", maxTimediff:0, shortIds:[$sid],
        settings:{publicKey:$pub, fingerprint:$fp, serverName:"", spiderX:"/"}}}')"
# routeOnly: домен из sniffing только для маршрутизации (фаза 07), назначение не переписывается
sniff='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":true}'

if [ -z "$inb_id" ]; then
    owner_uuid="$("$XRAY_BIN" uuid)"
    xui_client_exists owner && owner_uuid="$(xui_client_get owner | jq -r '.client.uuid')"
    body="$(jq -cn --arg remark "$PVR_REMARK" --argjson port "$port" --argjson stream "$stream" \
        --argjson sniff "$sniff" '{
        remark:$remark, enable:true, listen:"", port:$port, protocol:"vless", expiryTime:0, total:0,
        settings:{clients:[], decryption:"none", fallbacks:[]},
        streamSettings:$stream, sniffing:$sniff}')"
    inb_id="$(xui_inbound_add "$body")" || die "inbounds/add не прошёл"
    [[ "$inb_id" =~ ^[0-9]+$ ]] || die "inbounds/add вернул странный id: $inb_id"
    log_ok "inbound VLESS+REALITY создан (id=$inb_id, порт $port/tcp)"
else
    # read-modify-write: settings (клиенты, fallbacks от 04b) не трогаем
    cur_full="$(xui_inbound_get "$inb_id")" || die "inbounds/get/$inb_id не прошёл"
    want="$(jq -c --arg remark "$PVR_REMARK" --argjson port "$port" --argjson stream "$stream" --argjson sniff "$sniff" '
        .remark = $remark | .port = $port | .enable = true | .protocol = "vless"
        | .streamSettings = ((.streamSettings // {}) * $stream
            | .realitySettings.serverNames = $stream.realitySettings.serverNames
            | .realitySettings.shortIds = $stream.realitySettings.shortIds
            | del(.realitySettings.dest, .realitySettings.minClient, .realitySettings.maxClient))
        | .sniffing = $sniff
        | .settings.decryption = "none"' <<< "$cur_full")"
    norm='{remark, port, s: .streamSettings, n: .sniffing, d: .settings.decryption}'
    if [ "$(jq -cS "$norm" <<< "$cur_full")" != "$(jq -cS "$norm" <<< "$want")" ]; then
        xui_inbound_update "$inb_id" "$want" || die "inbounds/update/$inb_id не прошёл"
        log_ok "inbound id=$inb_id обновлён (порт $port, target $target)"
    else
        log_info "inbound id=$inb_id уже в нужном состоянии"
    fi
    # inbounds/update поле enable игнорирует (v3.9.0) — только setEnable
    if [ "$(jq -r '.enable' <<< "$cur_full")" != "true" ]; then
        xui_inbound_set_enable "$inb_id" true || die "не удалось включить inbound id=$inb_id"
        log_ok "inbound id=$inb_id включён"
    fi
    owner_uuid=""
fi

# ------------------------------------------------------------
# 6. Пользователь owner
# ------------------------------------------------------------

PVR_NO_REFRESH=1 proto_vless_reality_user_add owner ${owner_uuid:+"$owner_uuid"} \
    || die "не удалось завести пользователя owner"
owner_uuid="$(xui_client_get owner | jq -r '.client.uuid')"
# совместимость: старый 99 печатает ссылку из VLESS_UUID
config_set VLESS_UUID "$owner_uuid"

# ------------------------------------------------------------
# 7. Firewall
# ------------------------------------------------------------

fw_allow "$port/tcp" "vless-reality"
for p in $(printf '%s\n' "$old_port" 8443 | sort -u); do
    [ "$p" != "$port" ] || continue
    # 8443 от v1 в реестре не записан: fw_revoke всё равно удаляет правило ufw
    if [ "$p" = "$old_port" ] || [ "$v1_now" = "1" ]; then
        fw_revoke "$p/tcp"
        log_info "старый порт $p/tcp закрыт"
    fi
done

# ------------------------------------------------------------
# 8. Самопроверка
# ------------------------------------------------------------

wait_port "$port" tcp 40 || die "Xray не слушает $port/tcp — journalctl -u x-ui -n 50"
listener="$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -oE 'users:\(\("[^"]+"' | head -1 | sed 's/users:(("//' || true)"
[[ "$listener" == xray* ]] || die "$port/tcp слушает «$listener», а не xray"
[ "$(xui_xray_state)" = "running" ] || die "Xray не запущен: $(xui_server_status | jq -r '.xray.errorMsg')"
inb_now="$(xui_inbound_get "$inb_id")"
[ "$(jq -r '.enable' <<< "$inb_now")" = "true" ] || die "inbound id=$inb_id выключен"

# Не-REALITY соединение сервер отдаёт target: через наш порт должен прийти его сертификат.
# Сразу после старта Xray ~30 с изучает target (мимикрия post-handshake), поэтому с повторами
selfcheck_ok=0
for _ in $(seq 1 8); do
    out="$(timeout 20 "$XRAY_BIN" tls ping -ip 127.0.0.1 "$sni:$port" 2>&1 || true)"
    if sed -n '/Pinging with SNI/,$p' <<< "$out" | grep -q 'Handshake succeeded'; then
        selfcheck_ok=1; break
    fi
    sleep 5
done
[ "$selfcheck_ok" = "1" ] || die "REALITY на $port/tcp не проксирует к target $target (сервер не достаёт до target?): $(tail -3 <<< "$out")"
log_ok "REALITY отвечает сертификатом $sni через 127.0.0.1:$port"

# ------------------------------------------------------------
# 9. Манифест
# ------------------------------------------------------------

proto_vless_reality_manifest_refresh || die "не удалось записать манифест"
link="$(proto_vless_reality_links owner)"
[[ "$link" == *"support-x25519mlkem768=true"* ]] || die "ссылка owner без support-x25519mlkem768: $link"

log_ok "VLESS+REALITY+Vision: ${SERVER_IP}:$port/tcp, SNI $sni"
log_info "ссылка owner: $(manifest_path "$PVR_ID") (links[]), клиенты — notes в манифесте"
