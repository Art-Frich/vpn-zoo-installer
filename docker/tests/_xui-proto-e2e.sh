#!/usr/bin/env bash
# _xui-proto-e2e.sh PROTO SERVER [--keep] — сквозной тест протокола 3x-ui (ss2022 | tuic).
# Вызывается обёртками docker/tests/ss2022.sh и docker/tests/tuic.sh.
#
# Сервер (контейнер стенда) уже прошёл фазы 00-03 и фазу протокола. Тест:
#   1. берёт probe из манифеста /etc/vpn-setup/protocols.d/<id>.json;
#   2. поднимает отдельный клиентский контейнер в zoo-net, где наружу разрешён только
#      адрес сервера (iptables), и запускает в нём настоящий клиент (xray | sing-box);
#   3. через туннель: маленький запрос, большой (2 МБ), IP выхода = IP выхода сервера;
#   4. пользователь через proto_<id>_user_*: add → работает, disable → нет, enable → да,
#      del → нет.
# Код возврата 0 — всё прошло. Вывод: строки PASS/FAIL.

# pass/fail всегда возвращают 0, поэтому «A && pass || fail» безопасно
# shellcheck disable=SC2015

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

PROTO="${1:?протокол: ss2022|tuic}"
SERVER="${2:?контейнер сервера (zoo-...)}"
KEEP="${3:-}"
case "$SERVER" in zoo-*) ;; *) SERVER="zoo-$SERVER" ;; esac

# sing-box для TUIC-клиента: пины из scripts/versions.env (значения ниже — запасные)
# shellcheck source=../../scripts/versions.env
. "$(dirname "${BASH_SOURCE[0]}")/../../scripts/versions.env"
SINGBOX_VERSION="${SINGBOX_VERSION:-1.14.2}"
SINGBOX_SHA256_amd64="${SINGBOX_SHA256_amd64:-a684484d7477d1437282ee411f4d131d0340aaad60a7868841ebd5d87dd8a0c6}"
SINGBOX_SHA256_arm64="${SINGBOX_SHA256_arm64:-b43a1fb1bda131c6653576741ce527eb2bdeab7c9308ca90ee8b972abb7e4a7f}"

SMALL_URLS=(https://www.gstatic.com/generate_204 http://cp.cloudflare.com/)
BIG_URL="https://speed.cloudflare.com/__down?bytes=2000000"
BIG_BYTES=2000000
TEST_USER="zt-$PROTO-$$"

CLIENT="${SERVER}-cl-${PROTO}"
FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
info() { echo "      $*"; }
die()  { echo "FAIL  $*"; cleanup; exit 1; }

cleanup() {
    [ "$KEEP" = "--keep" ] && { info "клиент оставлен: $CLIENT"; return 0; }
    docker rm -f "$CLIENT" >/dev/null 2>&1 || true
}

# Команда на сервере с загруженными lib.sh, lib/xui.sh и lib/proto-<id>.sh
srv_env() { docker exec -i -e ZP="$PROTO" -e ZOO_TEST_ENV=docker "$SERVER" "$@"; }
srv() {
    docker exec -i -e ZP="$PROTO" -e ZOO_TEST_ENV=docker "$SERVER" bash -c '
        set -euo pipefail
        cd /repo
        . scripts/lib.sh; . scripts/lib/xui.sh; . "scripts/lib/proto-$ZP.sh"
        config_load
        "$@"' _ "$@"
}
cl() { docker exec -i "$CLIENT" "$@"; }
# jq на хосте (Git Bash) может не быть — JSON разбираем на сервере, там jq ставит фаза 00
jq() { docker exec -i "$SERVER" jq "$@"; }

docker container inspect "$SERVER" >/dev/null 2>&1 || { echo "нет контейнера $SERVER"; exit 2; }

manifest="$(srv_env cat "/etc/vpn-setup/protocols.d/$PROTO.json")" || die "нет манифеста $PROTO на $SERVER"
kind="$(jq -r '.probe.kind // empty' <<< "$manifest")"
[ -n "$kind" ] || die "в манифесте нет probe"
server_ip="$(jq -r '.probe.outbound.settings.servers[0].address // .probe.outbound.server' <<< "$manifest")"
info "манифест $PROTO: kind=$kind, сервер $server_ip:$(jq -r '.port' <<< "$manifest"), ссылок: $(jq '.links | length' <<< "$manifest")"
jq -e '.links[] | select(.user == "owner") | .uri' <<< "$manifest" >/dev/null && pass "в манифесте есть ссылка owner" || fail "нет ссылки owner в манифесте"

# IP выхода — по cdn-cgi/trace (строка ip=): api.ipify.org после фазы 07 блокируется или уходит в WARP (D9)
egress="$(srv_env curl -4 -fsS --max-time 15 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p')" || die "сервер не видит интернет"
info "IP выхода сервера: $egress"

# ---------- клиентский контейнер ----------
distro="$(docker container inspect -f '{{index .Config.Labels "zoo.ubuntu"}}' "$SERVER" 2>/dev/null || true)"
image="zoo-test-server:${distro:-24.04}"
docker rm -f "$CLIENT" >/dev/null 2>&1 || true
docker run -d --name "$CLIENT" --hostname "$CLIENT" --label zoo.harness=1 --label zoo.role=client \
    --network zoo-net --cap-add NET_ADMIN --entrypoint sleep "$image" infinity >/dev/null \
    || die "не стартовал клиентский контейнер ($image)"
trap cleanup EXIT

arch="$(cl uname -m)"
case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac

if [ "$kind" = "xray" ]; then
    # Xray из архива 3x-ui (проверен по sha256 при установке фазой 03)
    srv_env cat "/usr/local/x-ui/bin/xray-linux-$arch" | cl sh -c 'cat > /usr/local/bin/xray && chmod 755 /usr/local/bin/xray'
    info "клиент: $(cl xray version | head -1)"
elif [ "$kind" = "sing-box" ]; then
    sum_var="SINGBOX_SHA256_$arch"
    cl sh -c "set -e; cd /tmp; curl -fsSL --max-time 120 -o sb.tgz \
        https://github.com/SagerNet/sing-box/releases/download/v$SINGBOX_VERSION/sing-box-$SINGBOX_VERSION-linux-$arch.tar.gz
        echo '${!sum_var}  sb.tgz' | sha256sum -c - >/dev/null
        tar -xzf sb.tgz; install -m 755 sing-box-$SINGBOX_VERSION-linux-$arch/sing-box /usr/local/bin/sing-box" \
        || die "не удалось поставить sing-box $SINGBOX_VERSION (sha256)"
    info "клиент: $(cl sing-box version | head -1)"
else
    die "probe.kind=$kind этим тестом не поддерживается"
fi

# Наружу из клиента — только к серверу: успех через туннель доказывает, что трафик идёт через него
cl sh -c "iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -d $server_ip -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -j REJECT" || die "iptables в клиенте"
if cl curl -s -o /dev/null --max-time 5 https://api.ipify.org; then
    fail "клиент ходит в интернет напрямую — изоляция не работает"
else
    pass "прямой выход из клиента закрыт"
fi

# [r]un: шаблон pkill не совпадает с командной строкой самого sh -c
client_stop() {
    cl sh -c "pkill -f '[r]un -c /tmp/$1.json'; for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '[r]un -c /tmp/$1.json' >/dev/null || exit 0; sleep 0.3; done" || true
}

# client_start NAME PORT PROBE_JSON — клиент с локальным SOCKS/mixed на 127.0.0.1:PORT
client_start() {
    local name="$1" port="$2" probe="$3" cfg
    if [ "$kind" = "xray" ]; then
        cfg="$(jq -c --argjson p "$port" '{log: {loglevel: "warning"},
            inbounds: [{listen: "127.0.0.1", port: $p, protocol: "socks", settings: {udp: true}},
                       {listen: "127.0.0.1", port: ($p + 100), protocol: "dokodemo-door",
                        settings: {address: "1.1.1.1", port: 53, network: "udp"}}],
            outbounds: [.outbound]}' <<< "$probe")"
        printf '%s' "$cfg" | cl sh -c "cat > /tmp/$name.json"
        client_stop "$name"
        docker exec -d "$CLIENT" sh -c "exec xray run -c /tmp/$name.json > /tmp/$name.log 2>&1"
    else
        cfg="$(jq -c --argjson p "$port" '{log: {level: "warn"},
            inbounds: [{type: "mixed", listen: "127.0.0.1", listen_port: $p},
                       {type: "direct", listen: "127.0.0.1", listen_port: ($p + 100), network: "udp",
                        override_address: "1.1.1.1", override_port: 53}],
            outbounds: [.outbound]}' <<< "$probe")"
        printf '%s' "$cfg" | cl sh -c "cat > /tmp/$name.json"
        client_stop "$name"
        docker exec -d "$CLIENT" sh -c "exec sing-box run -c /tmp/$name.json > /tmp/$name.log 2>&1"
    fi
    for _ in $(seq 1 20); do
        cl sh -c "ss -Hltn 'sport = :$port' | grep -q ." 2>/dev/null && return 0
        sleep 0.5
    done
    echo "клиент $name не открыл порт $port:"; cl cat "/tmp/$name.log" | tail -5
    return 1
}

# fetch PORT URL → http_code size
fetch() {
    cl curl -sS -o /dev/null -w '%{http_code} %{size_download}' --max-time "${3:-20}" \
        -x "socks5h://127.0.0.1:$1" "$2" 2>/dev/null || echo "000 0"
}

# Может ли клиент через туннель (несколько попыток: соединение QUIC/Xray поднимается не мгновенно)
tunnel_ok() {
    local port="$1" r
    for _ in 1 2 3; do
        r="$(fetch "$port" "${SMALL_URLS[0]}" 10)"
        [ "${r%% *}" = "204" ] && return 0
        sleep 2
    done
    return 1
}

# ---------- owner ----------
probe_owner="$(jq -c '.probe' <<< "$manifest")"
client_start owner 10808 "$probe_owner" || die "клиент owner не запустился"
for u in "${SMALL_URLS[@]}"; do
    r=""; for _ in 1 2 3; do r="$(fetch 10808 "$u")"; [ "${r%% *}" = "204" ] && break; sleep 2; done
    [ "${r%% *}" = "204" ] && pass "owner: маленький запрос $u → 204" || fail "owner: $u → $r"
done
r="$(fetch 10808 "$BIG_URL" 60)"
[ "$r" = "200 $BIG_BYTES" ] && pass "owner: большой запрос 2 МБ → $r" || fail "owner: большой запрос → $r (ожидалось 200 $BIG_BYTES)"
exit_ip="$(cl curl -sS --max-time 15 -x socks5h://127.0.0.1:10808 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p' || true)"
[ "$exit_ip" = "$egress" ] && pass "owner: IP выхода через туннель = IP сервера ($exit_ip)" || fail "owner: IP выхода «$exit_ip», у сервера $egress"

# UDP через туннель: DNS-запрос A example.com на локальный UDP-форвард (порт+100) → 1.1.1.1:53.
# Напрямую UDP из клиента закрыт iptables, ответ может прийти только через туннель
udp_dns() {
    # shellcheck disable=SC2016 # $1 подставляется снаружи, остальное — для bash в клиенте
    cl bash -c 'exec 3<>/dev/udp/127.0.0.1/'"$1"'
        printf "\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01" >&3
        timeout 5 head -c 64 <&3 | od -An -tx1 | tr -d " \n" | head -c 8' 2>/dev/null || true
}
r=""; for _ in 1 2 3; do r="$(udp_dns 10908)"; [ "${r:0:4}" = "1234" ] && break; sleep 2; done
[ "${r:0:4}" = "1234" ] && pass "owner: UDP через туннель (DNS 1.1.1.1:53) → ответ" || fail "owner: UDP через туннель не прошёл (ответ «$r»)"

# ---------- пользователь: add / disable / enable / del ----------
settle=3; [ "$PROTO" = "tuic" ] && settle=12   # TUIC применяет пользователей задачей раз в 10 с
srv "proto_${PROTO}_user_add" "$TEST_USER" >/dev/null || die "user_add $TEST_USER"
grep -q "^$TEST_USER"$'\t' <<< "$(srv "proto_${PROTO}_user_list")" && pass "user_list видит $TEST_USER" || fail "user_list не видит $TEST_USER"
link="$(srv "proto_${PROTO}_links" "$TEST_USER")"
[[ "$link" == "${PROTO/ss2022/ss}://"* ]] && pass "ссылка $TEST_USER: ${link%%@*}@…" || fail "ссылка $TEST_USER: «$link»"
probe_u="$(srv "proto_${PROTO}_probe" "$TEST_USER")" || die "probe $TEST_USER"
sleep "$settle"
client_start tu 10809 "$probe_u" || die "клиент $TEST_USER не запустился"
tunnel_ok 10809 && pass "$TEST_USER после add: туннель работает" || fail "$TEST_USER после add: туннель не работает"

srv "proto_${PROTO}_user_enable" "$TEST_USER" false >/dev/null || fail "user_enable false"
sleep "$settle"; client_stop tu; client_start tu 10809 "$probe_u" || true
tunnel_ok 10809 && fail "$TEST_USER выключен, но туннель работает" || pass "$TEST_USER после disable: не пускает"

srv "proto_${PROTO}_user_enable" "$TEST_USER" true >/dev/null || fail "user_enable true"
sleep "$settle"; client_stop tu; client_start tu 10809 "$probe_u" || true
tunnel_ok 10809 && pass "$TEST_USER после enable: туннель работает" || fail "$TEST_USER после enable: туннель не работает"

srv "proto_${PROTO}_user_del" "$TEST_USER" >/dev/null || fail "user_del"
sleep "$settle"; client_stop tu; client_start tu 10809 "$probe_u" || true
tunnel_ok 10809 && fail "$TEST_USER удалён, но туннель работает" || pass "$TEST_USER после del: не пускает"
grep -q "^$TEST_USER"$'\t' <<< "$(srv "proto_${PROTO}_user_list")" && fail "user_list всё ещё видит $TEST_USER" || pass "user_list: $TEST_USER удалён"

# owner не задет операциями с другим пользователем
tunnel_ok 10808 && pass "owner после операций с $TEST_USER работает" || fail "owner сломался после операций с $TEST_USER"

tr="$(srv "proto_${PROTO}_traffic" 2>/dev/null || true)"
info "traffic: $(tr '\n' ' ' <<< "$tr")"

echo
if [ "$FAILS" -eq 0 ]; then echo "ИТОГ $PROTO: PASS"; else echo "ИТОГ $PROTO: FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
