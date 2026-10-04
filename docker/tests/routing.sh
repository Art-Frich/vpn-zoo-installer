#!/usr/bin/env bash
# docker/tests/routing.sh — e2e анти-утечек (фаза 07, опционально 08) через настоящие клиенты.
#
#   docker/tests/routing.sh SERVER [--no-ru-switch] [--no-awg] [--no-hy2] [--keep-client]
#
# SERVER — контейнер стенда (zoo-NAME или NAME) с выполненными 00-03 и 07 (08 — если
# ENABLE_WARP=1, 05 — если нужна проверка Hysteria). Тест сам создаёт временный
# VLESS-inbound (TCP без TLS, sniffing выключен — routing_sniffing_fix должен его включить),
# поднимает отдельный клиентский контейнер с Xray (и Hysteria) из сервера и через туннель
# проверяет:
#   - малый и большой (2 МБ) запрос, IP выхода = egress сервера (по cdn-cgi/trace:
#     api.ipify.org после 07 намеренно не отвечает адресом сервера);
#   - echo (api.ipify.org, ip.mail.ru) — заблокирован или выходит через WARP (не IP сервера),
#     по домену (socks5h) и по IP со sniffing (socks5);
#   - RU_EGRESS: direct → ya.ru/vk.com открываются; block → закрыты (домен, sniffing,
#     IP-литерал), ru-blocked и обычные сайты работают; warp (если WARP готов) → открываются;
#   - Hysteria (если есть манифест hysteria2): то же для echo и HY2_RU_EGRESS=block;
#   - AWG_RU_EGRESS=block на L3: netns с veth «awgtest0» → RU-сеть REJECT, остальное работает.
# В конце всё временное удаляется, *_RU_EGRESS возвращаются как были.
# Код выхода 0 — все проверки прошли.

# Проверки записаны как «условие; check $? PASS FAIL»
# shellcheck disable=SC2319
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SERVER="${1:?usage: routing.sh SERVER [--no-ru-switch] [--no-awg] [--no-hy2] [--keep-client]}"; shift
case "$SERVER" in zoo-*) ;; *) SERVER="zoo-$SERVER" ;; esac
RU_SWITCH=1; AWG_TEST=1; HY2_TEST=1; KEEP_CLIENT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-ru-switch) RU_SWITCH=0 ;;
        --no-awg) AWG_TEST=0 ;;
        --no-hy2) HY2_TEST=0 ;;
        --keep-client) KEEP_CLIENT=1 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
    shift
done
CLIENT="${SERVER}-rtclient"
NET=zoo-net
FAILS=0
PASSES=0

ok()   { echo "  [PASS] $*"; PASSES=$((PASSES + 1)); }
bad()  { echo "  [FAIL] $*"; FAILS=$((FAILS + 1)); }
# check COND_RC PASS_MSG FAIL_MSG — ok/bad по коду предыдущей проверки
check() { if [ "$1" = 0 ]; then ok "$2"; else bad "$3"; fi; }
info() { echo "[rt] $*"; }

srv() { docker exec -i -e ZOO_TEST_ENV=docker "$SERVER" bash -s; }
cli() { docker exec -i "$CLIENT" bash -c "$1"; }

# Пролог для серверных скриптов: библиотеки инсталлера
SRV_LIB='set -euo pipefail; cd /repo; . scripts/lib.sh; . scripts/lib/xui.sh; . scripts/lib/routing.sh; config_load; versions_load'

# srv_set KEY VALUE APPLY_FN — поменять ключ config.env и применить
srv_set() {
    srv <<EOF
$SRV_LIB
config_set $1 '$2'
$3 >/dev/null
EOF
}

docker container inspect "$SERVER" >/dev/null 2>&1 || { echo "нет контейнера $SERVER" >&2; exit 2; }
SERVER_IP="$(docker container inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$SERVER")"

# ------------------------------------------------------------
# 1. Сервер: временный VLESS-inbound без sniffing
# ------------------------------------------------------------

info "сервер $SERVER ($SERVER_IP): временный VLESS-inbound"
setup="$(srv <<EOF
$SRV_LIB
port="\$(rand_port)"
uuid="\$(cat /proc/sys/kernel/random/uuid)"
email="rt-test-\$(gen_random_hex 3)"
body="\$(jq -cn --argjson port "\$port" --arg id "\$uuid" --arg e "\$email" '{
  remark: "zoo-routing-test", enable: true, listen: "", port: \$port, protocol: "vless",
  expiryTime: 0, total: 0,
  settings: {clients: [{id: \$id, email: \$e, flow: "", enable: true, limitIp: 0, totalGB: 0,
                         expiryTime: 0, tgId: 0, subId: "", reset: 0}], decryption: "none", fallbacks: []},
  streamSettings: {network: "tcp", security: "none", tcpSettings: {header: {type: "none"}}},
  sniffing: {enabled: false, destOverride: []}}')"
id="\$(xui_inbound_add "\$body")"
ZOO_PHASE=rt-test fw_allow "\$port/tcp" "routing e2e (временный)" >/dev/null
fixed="\$(routing_sniffing_fix 2>/dev/null)"
echo "PORT=\$port"; echo "UUID=\$uuid"; echo "EMAIL=\$email"; echo "IBID=\$id"; echo "SNIFF_FIXED=\$fixed"
echo "RU_EGRESS_ORIG=\${RU_EGRESS:-direct}"; echo "AWG_RU_EGRESS_ORIG=\${AWG_RU_EGRESS:-direct}"
echo "HY2_RU_EGRESS_ORIG=\${HY2_RU_EGRESS:-direct}"
echo "WARP_READY=\$(routing_warp_ready && echo 1 || echo 0)"
echo "EGRESS=\$(curl -fsS --max-time 15 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p')"
f="\$MANIFEST_DIR/hysteria2.json"
if [ -f "\$f" ] && systemctl is-active --quiet hysteria-server; then
    echo "HY_CLIENT=\$(jq -c '.probe.client // empty | . + {socks5: {listen: "127.0.0.1:1080"}}' "\$f" | base64 -w0)"
fi
EOF
)" || { echo "$setup"; echo "не удалось подготовить сервер" >&2; exit 1; }
HY_CLIENT=""
eval "$(grep -E '^[A-Z_0-9]+=' <<< "$setup")"
info "inbound id=$IBID порт $PORT, egress сервера $EGRESS, WARP_READY=$WARP_READY"
[ "$SNIFF_FIXED" -ge 1 ] 2>/dev/null; check $? "sniffing на новом inbound включён routing_sniffing_fix ($SNIFF_FIXED)" \
    "routing_sniffing_fix не исправил inbound без sniffing ($SNIFF_FIXED)"

cleanup() {
    info "уборка"
    srv <<EOF >/dev/null 2>&1
$SRV_LIB
xui_inbound_del "$IBID" || true
xui_client_del "$EMAIL" 2>/dev/null || true
fw_revoke "$PORT/tcp" || true
[ "\$(config_get RU_EGRESS)" = "$RU_EGRESS_ORIG" ] || { config_set RU_EGRESS "$RU_EGRESS_ORIG"; routing_xray_apply; }
[ "\$(config_get HY2_RU_EGRESS)" = "$HY2_RU_EGRESS_ORIG" ] || { config_set HY2_RU_EGRESS "$HY2_RU_EGRESS_ORIG"; routing_hy2_apply; }
if [ "\$(config_get AWG_RU_EGRESS)" != "$AWG_RU_EGRESS_ORIG" ]; then config_set AWG_RU_EGRESS "$AWG_RU_EGRESS_ORIG"; routing_awg_apply; fi
ip netns del rt-awg 2>/dev/null || true
ip link del awgtest0 2>/dev/null || true
iptables -D FORWARD -i awgtest0 -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -o awgtest0 -j ACCEPT 2>/dev/null || true
iptables -t nat -D POSTROUTING -s 10.199.0.0/24 -j MASQUERADE 2>/dev/null || true
EOF
    [ "$KEEP_CLIENT" = 1 ] || docker rm -f "$CLIENT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ------------------------------------------------------------
# 2. Клиентский контейнер: Xray (и Hysteria) с сервера
# ------------------------------------------------------------

info "клиент $CLIENT"
docker rm -f "$CLIENT" >/dev/null 2>&1 || true
image="$(docker container inspect -f '{{.Config.Image}}' "$SERVER")"
docker run -d --name "$CLIENT" --hostname "$CLIENT" --label zoo.harness=1 --label zoo.role=client \
    --network "$NET" --entrypoint sleep "$image" infinity >/dev/null || { echo "клиент не стартовал" >&2; exit 1; }
# shellcheck disable=SC2016 # $(...) раскрывается внутри контейнера
docker exec "$SERVER" bash -c 'cat "$(ls /usr/local/x-ui/bin/xray-linux-* | head -1)"' \
    | docker exec -i "$CLIENT" bash -c 'cat > /usr/local/bin/xray && chmod 755 /usr/local/bin/xray'
cli "cat > /etc/xray-client.json" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{"tag": "socks", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
                "settings": {"auth": "noauth", "udp": true}}],
  "outbounds": [{"tag": "proxy", "protocol": "vless",
    "settings": {"vnext": [{"address": "$SERVER_IP", "port": $PORT,
                 "users": [{"id": "$UUID", "encryption": "none", "flow": ""}]}]},
    "streamSettings": {"network": "tcp", "security": "none"}}]
}
EOF
cli 'nohup xray run -c /etc/xray-client.json >/var/log/xray-client.log 2>&1 &'

wait_listen() { for _ in $(seq 1 30); do cli "ss -Hltn 'sport = :$1' | grep -q ':$1'" && return 0; sleep 0.5; done; return 1; }
wait_listen 10808 || bad "клиент Xray не поднял socks 10808: $(cli 'tail -3 /var/log/xray-client.log')"

if [ "$HY2_TEST" = 1 ] && [ -n "$HY_CLIENT" ]; then
    docker exec "$SERVER" cat /usr/local/bin/hysteria \
        | docker exec -i "$CLIENT" bash -c 'cat > /usr/local/bin/hysteria && chmod 755 /usr/local/bin/hysteria'
    base64 -d <<< "$HY_CLIENT" | docker exec -i "$CLIENT" bash -c 'cat > /etc/hy-client.json'
    cli 'nohup hysteria client -c /etc/hy-client.json >/var/log/hy-client.log 2>&1 &'
    wait_listen 1080 || bad "клиент Hysteria не поднял socks 1080: $(cli 'tail -3 /var/log/hy-client.log')"
fi

# curl через туннель: MODE=h — домен уходит на сервер (socks5h), MODE=ip — клиент сам
# резолвит, сервер видит только IP и узнаёт домен через sniffing. Порт — $SOCKS
SOCKS=10808
tcurl() {
    local mode="$1"; shift
    local proxy="socks5h://127.0.0.1:$SOCKS"
    [ "$mode" = "ip" ] && proxy="socks5://127.0.0.1:$SOCKS"
    cli "curl -sS --max-time 15 -x $proxy $*"
}

ru_check() {  # ru_check EXPECT(open|closed) LABEL MODE URL
    local expect="$1" label="$2" mode="$3" url="$4" code
    code="$(tcurl "$mode" -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)"
    if [ "$expect" = open ]; then
        [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]; check $? "$label открывается (HTTP $code)" "$label не открывается (код «$code»)"
    else
        ! [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]; check $? "$label закрыт" "$label открылся при block (HTTP $code)"
    fi
}

# Общие проверки для одного туннеля: трафик, IP выхода, echo, RU при текущем режиме
suite() {
    local tag="$1" ru_mode="$2" mode out rc code size exit_ip
    info "[$tag] трафик"
    code="$(tcurl h -o /dev/null -w '%{http_code}' https://www.gstatic.com/generate_204 2>&1)"
    [ "$code" = "204" ]; check $? "[$tag] малый запрос (generate_204)" "[$tag] малый запрос: $code"
    size="$(tcurl h -o /dev/null -w '%{size_download}' "'https://speed.cloudflare.com/__down?bytes=2000000'" 2>&1)"
    [ "$size" = "2000000" ]; check $? "[$tag] большой запрос 2 МБ" "[$tag] большой запрос: $size"
    exit_ip="$(tcurl h https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p')"
    [ -n "$exit_ip" ] && [ "$exit_ip" = "$EGRESS" ]; check $? "[$tag] IP выхода = egress сервера ($exit_ip)" \
        "[$tag] IP выхода «$exit_ip», egress сервера «$EGRESS»"

    info "[$tag] echo-сервисы (WARP_READY=$WARP_READY)"
    for mode in h ip; do
        out="$(tcurl "$mode" https://api.ipify.org 2>&1)"; rc=$?
        # бесплатный WARP изредка подвисает на первом запросе — один повтор
        if [ "$WARP_READY" = "1" ] && [ "$rc" != 0 ]; then out="$(tcurl "$mode" https://api.ipify.org 2>&1)"; rc=$?; fi
        if [ "$WARP_READY" = "1" ]; then
            [ "$rc" = 0 ] && [[ "$out" =~ ^[0-9.:a-f]+$ ]] && [ "$out" != "$EGRESS" ]
            check $? "[$tag] api.ipify.org ($mode) → WARP, видит $out, а не $EGRESS" "[$tag] api.ipify.org ($mode) при WARP: rc=$rc «$out»"
        else
            [ "$rc" != 0 ] && ! grep -q "$EGRESS" <<< "$out"
            check $? "[$tag] api.ipify.org ($mode) заблокирован (curl rc=$rc)" "[$tag] api.ipify.org ($mode) НЕ заблокирован: «$out»"
        fi
    done
    out="$(tcurl h https://ip.mail.ru 2>&1)"; rc=$?
    if [ "$WARP_READY" = "1" ] || [ "$rc" != 0 ]; then
        ! grep -q "$EGRESS" <<< "$out"; check $? "[$tag] ip.mail.ru (RU echo) не видит адрес сервера" "[$tag] ip.mail.ru видит $EGRESS"
    else
        bad "[$tag] ip.mail.ru открылся без WARP: «$(head -c 120 <<< "$out")»"
    fi

    info "[$tag] RU-назначения при режиме $ru_mode"
    case "$ru_mode" in
        direct|warp)
            ru_check open "[$tag] ya.ru" h https://ya.ru/
            ru_check open "[$tag] vk.com" h https://vk.com/ ;;
        block)
            ru_check closed "[$tag] ya.ru (домен)" h https://ya.ru/
            ru_check closed "[$tag] ya.ru (sniffing по IP)" ip https://ya.ru/
            ru_check closed "[$tag] vk.com (category-ru, не .ru)" h https://vk.com/
            ru_check closed "[$tag] RU IP-литерал 77.88.55.242 (geoip:ru)" h http://77.88.55.242/
            ru_check open "[$tag] rutracker.org (ru-blocked → direct)" h https://rutracker.org/
            ru_check open "[$tag] example.com" h https://example.com/ ;;
    esac
}

# ------------------------------------------------------------
# 3. Xray (VLESS)
# ------------------------------------------------------------

SOCKS=10808
suite xray "$RU_EGRESS_ORIG"
if [ "$RU_SWITCH" = 1 ]; then
    info "[xray] RU_EGRESS=block"
    srv_set RU_EGRESS block routing_xray_apply || bad "не удалось применить RU_EGRESS=block"
    sleep 2
    suite xray block
    if [ "$WARP_READY" = 1 ]; then
        info "[xray] RU_EGRESS=warp"
        srv_set RU_EGRESS warp routing_xray_apply || bad "не удалось применить RU_EGRESS=warp"
        sleep 2
        ru_check open "[xray] ya.ru через WARP" h https://ya.ru/
        got="$(srv <<EOF
$SRV_LIB
routing_route_test domain=gosuslugi.ru
EOF
)"
        [ "$got" = "warp" ]; check $? "[xray] routeTest gosuslugi.ru → warp" "[xray] routeTest gosuslugi.ru → «$got»"
    fi
    srv_set RU_EGRESS "$RU_EGRESS_ORIG" routing_xray_apply || bad "не удалось вернуть RU_EGRESS"
fi

# ------------------------------------------------------------
# 4. Hysteria
# ------------------------------------------------------------

if [ "$HY2_TEST" = 1 ] && [ -n "$HY_CLIENT" ]; then
    SOCKS=1080
    suite hy2 "$HY2_RU_EGRESS_ORIG"
    if [ "$RU_SWITCH" = 1 ]; then
        info "[hy2] HY2_RU_EGRESS=block"
        srv_set HY2_RU_EGRESS block routing_hy2_apply || bad "не удалось применить HY2_RU_EGRESS=block"
        # рестарт сервера рвёт сессию — клиент переподключится сам
        for _ in $(seq 1 20); do tcurl h -o /dev/null https://www.gstatic.com/generate_204 2>/dev/null && break; sleep 1; done
        ru_check closed "[hy2] ya.ru (домен)" h https://ya.ru/
        ru_check closed "[hy2] ya.ru (sniffing по IP)" ip https://ya.ru/
        ru_check closed "[hy2] RU IP-литерал 77.88.55.242 (geoip:ru)" h http://77.88.55.242/
        ru_check open "[hy2] rutracker.org (ru-blocked → direct)" h https://rutracker.org/
        ru_check open "[hy2] example.com" h https://example.com/
        srv_set HY2_RU_EGRESS "$HY2_RU_EGRESS_ORIG" routing_hy2_apply || bad "не удалось вернуть HY2_RU_EGRESS"
    fi
elif [ "$HY2_TEST" = 1 ]; then
    info "Hysteria на сервере нет (манифест hysteria2 / hysteria-server) — пропуск"
fi

# ------------------------------------------------------------
# 5. AWG L3: RU-сети REJECT для интерфейсов awg*
# ------------------------------------------------------------

if [ "$AWG_TEST" = 1 ]; then
    info "AWG_RU_EGRESS=block на L3 (netns + veth awgtest0)"
    awg_out="$(srv <<EOF 2>&1
$SRV_LIB
config_set AWG_RU_EGRESS block
routing_awg_apply >/dev/null
ip netns add rt-awg
ip link add awgtest0 type veth peer name rtpeer0
ip link set rtpeer0 netns rt-awg
ip addr add 10.199.0.1/24 dev awgtest0; ip link set awgtest0 up
ip -n rt-awg addr add 10.199.0.2/24 dev rtpeer0; ip -n rt-awg link set rtpeer0 up; ip -n rt-awg link set lo up
ip -n rt-awg route add default via 10.199.0.1
sysctl -qw net.ipv4.ip_forward=1
iptables -I FORWARD -i awgtest0 -j ACCEPT; iptables -I FORWARD -o awgtest0 -j ACCEPT
iptables -t nat -A POSTROUTING -s 10.199.0.0/24 -j MASQUERADE
echo "RU=\$(ip netns exec rt-awg curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve ya.ru:443:77.88.55.242 https://ya.ru/ || true)"
echo "OTHER=\$(ip netns exec rt-awg curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve www.cloudflare.com:443:104.16.124.96 https://www.cloudflare.com/cdn-cgi/trace || true)"
config_set AWG_RU_EGRESS direct
routing_awg_apply >/dev/null
echo "RU_OFF=\$(ip netns exec rt-awg curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve ya.ru:443:77.88.55.242 https://ya.ru/ || true)"
EOF
)"
    ru="$(sed -n 's/^RU=//p' <<< "$awg_out")"; other="$(sed -n 's/^OTHER=//p' <<< "$awg_out")"
    ru_off="$(sed -n 's/^RU_OFF=//p' <<< "$awg_out")"
    [ "$ru" = "000" ]; check $? "AWG: RU-сеть (77.88.55.242) отклонена" "AWG: RU-сеть ответила «$ru» ($awg_out)"
    [ "$other" = "200" ]; check $? "AWG: не-RU (cloudflare) работает" "AWG: не-RU не работает «$other»"
    [[ "$ru_off" =~ ^[1-5][0-9][0-9]$ ]]; check $? "AWG: при AWG_RU_EGRESS=direct RU-сеть снова доступна (HTTP $ru_off)" \
        "AWG: после снятия фильтра RU-сеть недоступна «$ru_off»"
fi

echo
echo "[rt] итог: PASS=$PASSES FAIL=$FAILS"
[ "$FAILS" = 0 ]
