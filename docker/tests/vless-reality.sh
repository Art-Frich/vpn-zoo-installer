#!/usr/bin/env bash
# docker/tests/vless-reality.sh — e2e VLESS+REALITY+Vision через настоящий клиент Xray.
#
#   docker/tests/vless-reality.sh SERVER     (SERVER — контейнер стенда после фаз 00-04)
#
# Клиент — отдельный контейнер в zoo-net из того же образа, что и сервер, с бинарём
# xray из 3x-ui сервера. Прямой выход клиента в интернет закрыт iptables (разрешён только
# сервер), поэтому ответ через туннель доказывает, что трафик шёл через VLESS.
# Проверки: маленький запрос, большой (2 МБ), IP выхода = IP выхода сервера;
# пользователи через lib/proto-vless-reality.sh: новый подключается, выключенный и
# удалённый — нет; клиент, заведённый другим потоком (flow ""), привязывается с Vision.
# Код выхода 0 — всё PASS.

# A && ok || bad: ok/bad не падают; jq-выражения в одинарных кавычках намеренно
# shellcheck disable=SC2015,SC2016

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:-}"
[ -n "$SRV" ] || { echo "использование: $0 SERVER" >&2; exit 2; }
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
CLI="${SRV}-vrcli"
NET="zoo-net"
SMALL_URL="${SMALL_URL:-https://www.gstatic.com/generate_204}"
BIG_URL="${BIG_URL:-https://speed.cloudflare.com/__down?bytes=2000000}"
BIG_MIN="${BIG_MIN:-2000000}"
# IP выхода — по cdn-cgi/trace (строка ip=): api.ipify.org после фазы 07 блокируется или уходит в WARP (D9)
IP_URL="${IP_URL:-https://www.cloudflare.com/cdn-cgi/trace}"
TU="zvr$$"                         # временный пользователь (уникален и при параллельных тестах)
SU="${TU}s"                          # «чужой» клиент с flow ""

fail=0
ok()   { echo "[PASS] $*"; }
bad()  { echo "[FAIL] $*"; fail=1; }
warn() { echo "[WARN] $*"; }

sx() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
cx() { docker exec -i "$CLI" "$@"; }

# Вызов функций lib/*.sh на сервере: srvlib proto_vless_reality_probe owner
srvlib() {
    sx bash -c '. /repo/scripts/lib.sh; . /repo/scripts/lib/xui.sh; . /repo/scripts/lib/proto-vless-reality.sh
        config_load; "$@"' _ "$@"
}

DUMMY_ID=""
# shellcheck disable=SC2329 # вызывается из trap
cleanup() {
    docker rm -f "$CLI" >/dev/null 2>&1 || true
    srvlib proto_vless_reality_user_del "$TU" >/dev/null 2>&1 || true
    srvlib proto_vless_reality_user_del "$SU" >/dev/null 2>&1 || true
    if [ -n "$DUMMY_ID" ]; then
        srvlib xui_inbound_del "$DUMMY_ID" >/dev/null 2>&1 || true
        srvlib xui_client_del "$SU" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null 2>&1 || { echo "нет контейнера $SRV" >&2; exit 2; }
MAN="$(sx cat /etc/vpn-setup/protocols.d/vless-reality.json)" || { bad "нет манифеста vless-reality"; exit 1; }
SRV_IP="$(docker container inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$SRV")"
PORT="$(sx jq -r '.port' <<< "$MAN")"
EGRESS="$(sx curl -4 -sS --max-time 15 "$IP_URL" | sed -n 's/^ip=//p' || true)"
echo "[i] сервер $SRV ($SRV_IP:$PORT), выход сервера: ${EGRESS:-?}"

# ---------- клиентский контейнер ----------
IMAGE="$(docker container inspect -f '{{.Config.Image}}' "$SRV")"
docker rm -f "$CLI" >/dev/null 2>&1 || true
docker run -d --name "$CLI" --label zoo.harness=1 --label zoo.role=client --network "$NET" \
    --cap-add NET_ADMIN --entrypoint sleep "$IMAGE" infinity >/dev/null
XRAY_SRV="$(srvlib xui_xray_bin)"
sx cat "$XRAY_SRV" | cx sh -c 'cat > /usr/local/bin/xray && chmod 755 /usr/local/bin/xray'
if [ "$(sx sha256sum "$XRAY_SRV" | awk '{print $1}')" = "$(cx sha256sum /usr/local/bin/xray | awk '{print $1}')" ]; then
    ok "клиент: $(cx xray version | head -1 | cut -c1-40)"
else
    bad "xray не скопировался в клиент"; exit 1
fi

# Прямой выход клиента закрыт: только сервер и loopback
if cx sh -c "iptables -A OUTPUT -o lo -j ACCEPT && iptables -A OUTPUT -d $SRV_IP -j ACCEPT \
        && iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT && iptables -A OUTPUT -j REJECT" 2>/dev/null; then
    if cx curl -sS -o /dev/null --max-time 8 "$SMALL_URL" 2>/dev/null; then
        bad "прямой выход клиента не закрылся — проверка IP теряет смысл"
    else
        ok "прямой выход клиента закрыт (только $SRV_IP)"
    fi
else
    warn "iptables в клиенте недоступен — прямой выход не закрыт, проверка IP слабее"
fi

# ---------- помощники ----------
# client_up NAME PROBE_JSON SOCKS_PORT — xray-клиент с socks-входом на 127.0.0.1:PORT
client_up() {
    local name="$1" probe="$2" sport="$3"
    sx jq -c --argjson p "$sport" '{log:{loglevel:"warning"},
        inbounds:[{tag:"socks", listen:"127.0.0.1", port:$p, protocol:"socks", settings:{udp:false}}],
        outbounds:[.outbound]}' <<< "$probe" | cx sh -c "cat > /tmp/$name.json"
    cx xray run -test -c "/tmp/$name.json" >/dev/null 2>&1 || { bad "$name: xray -test отверг конфиг клиента"; return 1; }
    docker exec -d "$CLI" sh -c "exec xray run -c /tmp/$name.json > /tmp/$name.log 2>&1"
    for _ in $(seq 1 20); do
        cx bash -c "exec 3<>/dev/tcp/127.0.0.1/$sport" 2>/dev/null && return 0
        sleep 0.5
    done
    bad "$name: socks 127.0.0.1:$sport не поднялся: $(cx tail -5 "/tmp/$name.log")"
    return 1
}

client_down() { cx pkill -f "xray run -c /tmp/$1.json" >/dev/null 2>&1 || true; }

# fetch SOCKS_PORT URL [curl-формат] — через туннель
fetch() { cx curl -sS --max-time "${4:-30}" -x "socks5h://127.0.0.1:$1" -o "${3:-/dev/null}" -w '%{http_code} %{size_download} %{speed_download}' "$2" 2>/dev/null; }

# tunnel_ok SOCKS_PORT — маленький запрос с повторами (после старта Xray ~30 с изучает target)
tunnel_ok() {
    local r
    for _ in $(seq 1 "${2:-8}"); do
        r="$(fetch "$1" "$SMALL_URL" /dev/null 15 || true)"
        [ "${r%% *}" = "204" ] && return 0
        sleep 5
    done
    return 1
}

# full_check NAME SOCKS_PORT — малый, большой, IP выхода
full_check() {
    local name="$1" sp="$2" r size ip
    if tunnel_ok "$sp"; then ok "$name: малый запрос через туннель → 204"; else bad "$name: малый запрос не прошёл ($(cx tail -3 "/tmp/$name.log"))"; return; fi
    r="$(fetch "$sp" "$BIG_URL" /dev/null 120 || true)"
    size="$(awk '{print $2}' <<< "$r")"
    if [ "${size:-0}" -ge "$BIG_MIN" ]; then ok "$name: большой запрос ${size} Б ($(awk '{printf "%.1f", $3/1048576}' <<< "$r") МБ/с)"; else bad "$name: большой запрос: $r"; fi
    ip="$(cx curl -sS --max-time 20 -x "socks5h://127.0.0.1:$sp" "$IP_URL" 2>/dev/null | sed -n 's/^ip=//p' || true)"
    if [ -n "$ip" ] && [ "$ip" = "$EGRESS" ]; then ok "$name: IP выхода $ip = IP сервера"; else bad "$name: IP выхода «$ip», у сервера «$EGRESS»"; fi
}

# ---------- 1. owner из манифеста ----------
PROBE="$(sx jq -c '.probe' <<< "$MAN")"
LINK="$(sx jq -r '.links[] | select(.user == "owner") | .uri' <<< "$MAN")"
[[ "$LINK" == *"support-x25519mlkem768=true"* && "$LINK" == *"fp=chrome"* && "$LINK" == *"flow=xtls-rprx-vision"* ]] \
    && ok "ссылка owner: ML-KEM-подсказка, fp=chrome, Vision" || bad "ссылка owner: $LINK"
[ "$(sx jq -r '.kind' <<< "$PROBE")" = "xray" ] && ok "probe.kind=xray" || bad "probe: $PROBE"
client_up owner "$PROBE" 10801 && full_check owner 10801
client_down owner

# ---------- 2. новый пользователь ----------
if srvlib proto_vless_reality_user_add "$TU" >/dev/null; then ok "user_add $TU"; else bad "user_add $TU"; fi
srvlib proto_vless_reality_user_list | grep -q "^$TU	true$" && ok "user_list содержит $TU" || bad "user_list без $TU"
sx jq -e --arg u "$TU" 'any(.links[]; .user == $u)' /etc/vpn-setup/protocols.d/vless-reality.json >/dev/null \
    && ok "манифест обновлён ($TU в links)" || bad "манифест без $TU"
sx test -f "/etc/vpn-setup/clients/$TU/vless-reality.json" && [ "$(sx stat -c %a "/etc/vpn-setup/clients/$TU")" = "700" ] \
    && ok "креды $TU в clients/$TU (0700)" || bad "нет clients/$TU/vless-reality.json"
P2="$(srvlib proto_vless_reality_probe "$TU")"
client_up "$TU" "$P2" 10802 && full_check "$TU" 10802

# выключение и включение
srvlib proto_vless_reality_user_enable "$TU" false >/dev/null
sleep 3
if tunnel_ok 10802 1; then bad "$TU выключен, но подключается"; else ok "$TU выключен — не подключается"; fi
srvlib proto_vless_reality_user_enable "$TU" true >/dev/null
if tunnel_ok 10802 4; then ok "$TU снова включён — подключается"; else bad "$TU после включения не подключается"; fi

# удаление
srvlib proto_vless_reality_user_del "$TU" >/dev/null && ok "user_del $TU" || bad "user_del $TU"
sleep 3
if tunnel_ok 10802 1; then bad "$TU удалён, но подключается"; else ok "$TU удалён — не подключается"; fi
srvlib xui_client_exists "$TU" && bad "$TU остался в 3x-ui" || ok "$TU удалён из 3x-ui (других привязок не было)"
client_down "$TU"

# ---------- 3. клиент другого потока (flow "") → attach ----------
DPORT=$((30000 + RANDOM % 2000))
DUMMY_ID="$(srvlib xui_inbound_add "$(sx jq -cn --arg e "$SU" --arg u "$(sx "$XRAY_SRV" uuid)" --argjson p "$DPORT" '{
    remark:"zoo-test-dummy", enable:true, listen:"127.0.0.1", port:$p, protocol:"vless", expiryTime:0, total:0,
    settings:{clients:[{id:$u, email:$e, flow:"", enable:true, tgId:0, subId:"zoodummysub00001"}], decryption:"none", fallbacks:[]},
    streamSettings:{network:"xhttp", security:"none", xhttpSettings:{path:"/zt", mode:"auto"}},
    sniffing:{enabled:false, destOverride:[]}}')")" || true
if [ -n "$DUMMY_ID" ]; then
    srvlib proto_vless_reality_user_add "$SU" >/dev/null && ok "user_add $SU (уже был в другом inbound)" || bad "user_add $SU"
    [ "$(srvlib xui_client_get "$SU" | sx jq -r '.inboundIds | length')" = "2" ] && ok "$SU привязан к двум inbound" || bad "$SU: привязки $(srvlib xui_client_get "$SU" | sx jq -c '.inboundIds')"
    client_up "$SU" "$(srvlib proto_vless_reality_probe "$SU")" 10803 && \
        { tunnel_ok 10803 && ok "$SU подключается с Vision" || bad "$SU не подключается"; }
    srvlib proto_vless_reality_user_del "$SU" >/dev/null
    srvlib xui_client_exists "$SU" && ok "user_del $SU: клиент остался для другого inbound" || bad "user_del $SU удалил клиента целиком"
    client_down "$SU"
else
    bad "не удалось создать тестовый inbound для проверки attach"
fi

# ---------- 4. трафик ----------
tr=""; down=0
for _ in $(seq 1 8); do
    tr="$(srvlib proto_vless_reality_traffic | sx jq -c 'select(.user == "owner")' || true)"
    down="$(sx jq -r '.down // 0' <<< "${tr:-null}" 2>/dev/null || echo 0)"
    [ "${down:-0}" -ge "$BIG_MIN" ] && break
    sleep 5
done
if [ "${down:-0}" -ge "$BIG_MIN" ]; then ok "traffic owner: $tr"; else warn "traffic owner пока не дошёл до $BIG_MIN: ${tr:-нет}"; fi

echo
[ "$fail" = "0" ] && echo "ИТОГ: PASS" || echo "ИТОГ: FAIL"
exit "$fail"
