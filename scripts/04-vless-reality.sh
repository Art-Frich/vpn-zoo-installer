#!/usr/bin/env bash
# 04-vless-reality.sh — VLESS+Reality+Vision inbound напрямую в БД 3x-ui
#
# Генерирует Reality keypair, UUID, shortId (если ещё нет в config.env)
# и вставляет inbound в /etc/x-ui/x-ui.db.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
config_load

XUI_DIR="/usr/local/x-ui"
XRAY_BIN="$XUI_DIR/bin/xray-linux-amd64"
DB="/etc/x-ui/x-ui.db"

# ------------------------------------------------------------
# 1. Генерация Reality keys / UUID / shortId
# ------------------------------------------------------------

[ -x "$XRAY_BIN" ] || die "xray binary не найден: $XRAY_BIN (03-3xui выполнен?)"

# Reality keypair
if [ -z "${VLESS_PRIV:-}" ] || [ -z "${VLESS_PUB:-}" ]; then
    log_info "генерирую Reality keypair"
    keypair="$("$XRAY_BIN" x25519)"
    VLESS_PRIV="$(echo "$keypair" | awk '/PrivateKey/ {print $NF}')"
    VLESS_PUB="$(echo "$keypair"  | awk '/Password/   {print $NF}')"
    [ -n "$VLESS_PRIV" ] && [ -n "$VLESS_PUB" ] || die "x25519 не вернул keypair: $keypair"
    log_ok "Reality keys сгенерированы"
else
    log_info "Reality keys уже есть в config.env — пропуск"
fi

# UUID
if [ -z "${VLESS_UUID:-}" ]; then
    VLESS_UUID="$("$XRAY_BIN" uuid)"
    log_info "UUID: $VLESS_UUID"
fi

# shortId (8 байт hex)
if [ -z "${VLESS_SID:-}" ]; then
    VLESS_SID="$(openssl rand -hex 8)"
    log_info "shortId: $VLESS_SID"
fi

# Сохраняем обратно в config.env
config_save

# ------------------------------------------------------------
# 2. Проверяем что БД существует и inbound ещё не создан
# ------------------------------------------------------------

[ -f "$DB" ] || die "БД 3x-ui не найдена: $DB (стартовал ли x-ui хотя бы раз?)"

existing="$(sqlite3 "$DB" "SELECT id FROM inbounds WHERE port=$VLESS_PORT;" 2>/dev/null || echo "")"
if [ -n "$existing" ]; then
    log_info "inbound на порту $VLESS_PORT уже существует (id=$existing) — пропуск"
    return 0 2>/dev/null || exit 0
fi

# ------------------------------------------------------------
# 3. Формируем JSON для settings + stream_settings + sniffing
# ------------------------------------------------------------

SETTINGS=$(jq -cn \
  --arg id "$VLESS_UUID" \
  --arg email "$LABEL" \
  '{
    clients:[{
      id:$id, flow:"xtls-rprx-vision", email:$email,
      limitIp:0, totalGB:0, expiryTime:0, enable:true,
      tgId:"", subId:$email, reset:0
    }],
    decryption:"none",
    fallbacks:[]
  }')

STREAM=$(jq -cn \
  --arg priv "$VLESS_PRIV" \
  --arg pub "$VLESS_PUB" \
  --arg sid "$VLESS_SID" \
  --arg dest "$VLESS_SNI" \
  '{
    network:"tcp",
    security:"reality",
    externalProxy:[],
    realitySettings:{
      show:false, xver:0,
      dest:($dest+":443"),
      serverNames:[$dest],
      privateKey:$priv,
      minClient:"", maxClient:"", maxTimediff:0,
      shortIds:[$sid],
      settings:{ publicKey:$pub, fingerprint:"chrome", serverName:"", spiderX:"/" }
    },
    tcpSettings:{ acceptProxyProtocol:false, header:{type:"none"} }
  }')

SNIFF='{"enabled":true,"destOverride":["http","tls","quic","fakedns"],"metadataOnly":false,"routeOnly":false}'

# ------------------------------------------------------------
# 4. INSERT в БД (через bind parameters во избежание SQL injection)
# ------------------------------------------------------------

log_info "вставляю inbound в БД (port=$VLESS_PORT, sni=$VLESS_SNI)"
sqlite3 "$DB" <<SQL
INSERT INTO inbounds (
    user_id, up, down, total, all_time, remark, enable, expiry_time,
    traffic_reset, last_traffic_reset_time, listen, port, protocol,
    settings, stream_settings, tag, sniffing
) VALUES (
    1, 0, 0, 0, 0,
    'reality-vision-${VLESS_PORT}',
    1, 0,
    'never', 0,
    '', ${VLESS_PORT}, 'vless',
    '$(printf '%s' "$SETTINGS" | sed "s/'/''/g")',
    '$(printf '%s' "$STREAM"   | sed "s/'/''/g")',
    'inbound-${VLESS_PORT}',
    '$(printf '%s' "$SNIFF"    | sed "s/'/''/g")'
);
SQL

log_ok "inbound создан"
sqlite3 "$DB" "SELECT id, port, protocol, remark FROM inbounds;" | head -5

# ------------------------------------------------------------
# 5. Restart x-ui чтобы Xray перегенерил config.json из БД
# ------------------------------------------------------------

if ! is_container; then
    systemctl restart x-ui
    sleep 3
    if ss -tlnp 2>/dev/null | grep -q ":${VLESS_PORT} "; then
        log_ok "Xray слушает на :${VLESS_PORT}"
    else
        log_warn "не слышу Xray на :${VLESS_PORT} — проверь логи: journalctl -u x-ui -n 50"
    fi
else
    log_warn "контейнер — restart x-ui пропущен"
fi

log_ok "VLESS+Reality+Vision inbound установлен"
