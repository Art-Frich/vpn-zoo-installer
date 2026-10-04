#!/usr/bin/env bash
# xui-smoke.sh — смоук адаптера scripts/lib/xui.sh на живой панели (после фазы 03).
# Запуск внутри сервера стенда:
#   docker/run-server.sh exec NAME bash /repo/docker/xui-smoke.sh
# Создаёт временный inbound и клиентов с префиксом zoo-smoke и удаляет их за собой.

# A && ok || bad: ok/bad не падают; пути /repo существуют только внутри стенда
# shellcheck disable=SC2015,SC1091

set -euo pipefail
. /repo/scripts/lib.sh
. /repo/scripts/lib/xui.sh
config_load
versions_load

fail=0
ok()  { echo "[PASS] $*"; }
bad() { echo "[FAIL] $*"; fail=1; }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }

XRAY="$(xui_xray_bin)"
PORT="$(rand_port)"

check "wait_api" xui_wait_api 30
st="$(xui_server_status)"
[ "$(jq -r '.xray.state' <<< "$st")" = "running" ] && ok "xray running $(jq -r '.xray.version' <<< "$st")" || bad "xray state"
[ "$(xui_panel_version)" = "${XUI_VERSION#v}" ] && ok "panelVersion=$(xui_panel_version)" || bad "panelVersion"

before="$(xui_inbound_list | jq 'length')"
ok "inbounds/list: $before шт."

keys="$("$XRAY" x25519)"
priv="$(awk -F': ' '/^PrivateKey/ {print $2}' <<< "$keys")"
pub="$(awk -F': ' '/^Password/ {print $2}' <<< "$keys")"
uuid="$("$XRAY" uuid)"
body="$(jq -cn --arg uuid "$uuid" --arg priv "$priv" --arg pub "$pub" --argjson port "$PORT" '{
  remark:"zoo-smoke", enable:true, listen:"", port:$port, protocol:"vless", expiryTime:0, total:0,
  settings:{clients:[{id:$uuid, email:"zoo-smoke-a", flow:"xtls-rprx-vision", enable:true,
    limitIp:0, totalGB:0, expiryTime:0, tgId:0, subId:"zoosmokesub00001", reset:0}],
    decryption:"none", fallbacks:[]},
  streamSettings:{network:"tcp", security:"reality", tcpSettings:{header:{type:"none"}},
    realitySettings:{show:false, xver:0, target:"www.example.com:443", serverNames:["www.example.com"],
      privateKey:$priv, shortIds:["0123abcd"], minClientVer:"", maxClientVer:"",
      settings:{publicKey:$pub, fingerprint:"chrome", spiderX:"/"}}},
  sniffing:{enabled:true, destOverride:["http","tls","quic"]}}')"

id="$(xui_inbound_add "$body")" && [ -n "$id" ] && ok "inbounds/add → id=$id port=$PORT" || bad "inbounds/add"
[ "$(xui_inbound_find_by_port "$PORT" vless)" = "$id" ] && ok "find_by_port" || bad "find_by_port"
[ "$(xui_inbound_find_by_remark zoo-smoke)" = "$id" ] && ok "find_by_remark" || bad "find_by_remark"
wait_port "$PORT" tcp 35 && ok "xray слушает :$PORT" || bad "xray не слушает :$PORT"

check "clients/add" xui_client_add "$(jq -cn --arg u "$("$XRAY" uuid)" '{id:$u, email:"zoo-smoke-b", flow:"xtls-rprx-vision", enable:true, tgId:0, subId:"zoosmokesub00002"}')" "$id"
xui_client_exists zoo-smoke-b && ok "clients/list содержит zoo-smoke-b" || bad "clients/list"
check "clients/update (enable=false)" xui_client_update zoo-smoke-b '{"enable":false}'
c="$(xui_client_get zoo-smoke-b | jq -c '.client')"
[ "$(jq -r '.enable' <<< "$c")" = "false" ] && [ "$(jq -r '.flow' <<< "$c")" = "xtls-rprx-vision" ] \
    && ok "update сохранил flow и выключил клиента" || bad "update испортил клиента: $c"
links="$(xui_client_links zoo-smoke-a "${SERVER_IP:-127.0.0.1}")"
grep -q "@${SERVER_IP:-127.0.0.1}:$PORT" <<< "$links" && ok "clients/links: $(jq -r '.[0]' <<< "$links" | cut -c1-60)…" || bad "clients/links: $links"
check "clients/detach" xui_client_detach zoo-smoke-b "$id"
check "clients/del" xui_client_del zoo-smoke-b
xui_client_exists zoo-smoke-b && bad "zoo-smoke-b не удалился" || ok "zoo-smoke-b удалён"

check "inbounds/setEnable false" xui_inbound_set_enable "$id" false
[ "$(xui_inbound_get "$id" | jq -r '.enable')" = "false" ] && ok "inbound выключен" || bad "setEnable"

tpl="$(xui_xray_template_get)"
jq -e '.routing and .outbounds' <<< "$tpl" >/dev/null && ok "xray/ (шаблон получен)" || bad "xray/ get"
tpl2="$(jq -c '.routing.rules += [{type:"field", outboundTag:"blocked", domain:["domain:zoo-smoke.invalid"]}]' <<< "$tpl")"
check "xray/update" xui_xray_template_set "$tpl2"
xui_xray_template_get | grep -q 'zoo-smoke.invalid' && ok "правило в шаблоне" || bad "правило не сохранилось"
check "xray/update (откат)" xui_xray_template_set "$tpl"
check "server/restartXrayService" xui_xray_restart
check "setting/all" xui_settings_get
[ "$(xui_settings_get | jq -r '.subEnable')" = "false" ] && ok "подписка выключена" || bad "подписка включена"

check "inbounds/del" xui_inbound_del "$id"
[ -z "$(xui_inbound_find_by_port "$PORT")" ] && ok "inbound удалён" || bad "inbound не удалился"
# v3.9.0: клиенты удалённого inbound остаются «сиротами» (inboundIds=[])
[ "$(xui_client_get zoo-smoke-a | jq -c '.inboundIds')" = "[]" ] && ok "zoo-smoke-a осиротел (ожидаемо для v3.9.0)" || bad "zoo-smoke-a: неожиданная привязка"
check "clients/del осиротевшего" xui_client_del zoo-smoke-a
xui_client_exists zoo-smoke-a && bad "zoo-smoke-a не удалился" || ok "zoo-smoke-a удалён"
[ "$(xui_client_del_orphans)" -ge 0 ] && ok "clients/delOrphans" || bad "clients/delOrphans"
[ "$(xui_inbound_list | jq 'length')" = "$before" ] && ok "число inbounds вернулось к $before" || bad "inbounds не вернулись к исходному"

# 401 → перевыпуск токена
saved="$(cat "$XUI_HDR_FILE")"
printf 'Authorization: Bearer %s\n' "zoo-smoke-wrong-token" > "$XUI_HDR_FILE"
[ "$(xui_http_code)" = "401" ] && ok "неверный токен → 401" || bad "неверный токен не дал 401"
xui_wait_api 20 >/dev/null 2>&1 && [ "$(xui_http_code)" = "200" ] && ok "wait_api перевыпустил токен" || { bad "перевыпуск токена"; printf '%s\n' "$saved" > "$XUI_HDR_FILE"; }
[ "$(config_get XUI_API_TOKEN)" = "$(awk '{print $3}' "$XUI_HDR_FILE")" ] && ok "config.env и xui-auth.hdr согласованы" || bad "токен в config.env и hdr расходится"

echo
[ "$fail" = "0" ] && echo "XUI SMOKE: OK" || { echo "XUI SMOKE: FAIL"; exit 1; }
