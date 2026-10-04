#!/usr/bin/env bash
# docker/tests/hysteria2.sh SERVER — e2e Hysteria2 через настоящий клиент Hysteria.
#
#   docker/tests/hysteria2.sh hy2-s1        # сервер после фаз 00-03 + 05 (zoo-hy2-s1)
#
# Клиент — отдельный контейнер в zoo-net (образ zoo-probe, если есть, иначе ubuntu:24.04),
# бинарь hysteria той же версии и sha256, что в scripts/versions.env. Конфиг клиента
# строится только из манифеста (probe.client) и ссылок манифеста.
# Проверки: малый запрос, большой (2 МБ), IP выхода = IP выхода сервера, счётчик
# trafficStats вырос, неверный пин отвергается, ссылка из манифеста, user add/del/enable
# через lib без рестарта сервиса и без обрыва чужой сессии; obfs и hopping — если включены.
# Код выхода 0 — все проверки PASS.

# «A && ok || bad»: ok всегда возвращает 0
# shellcheck disable=SC2015
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=docker/run-server.sh
. "$HERE/../run-server.sh"
REPO="$ZOO_REPO_ROOT"

[ $# -ge 1 ] || { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
SRV="$(zoo_name "$1")"
CLI="zoo-hy2t-$$"
SMALL_URL="https://www.gstatic.com/generate_204"
BIG_URL="https://speed.cloudflare.com/__down?bytes=2000000"
BIG_SIZE=2000000
# IP выхода — по cdn-cgi/trace (строка ip=): api.ipify.org после фазы 07 блокируется или уходит в WARP (D9)
IP_URL="https://www.cloudflare.com/cdn-cgi/trace"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  [PASS] $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $*"; }
step() { echo; echo "== $*"; }

cleanup() { docker rm -f "$CLI" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Команда на сервере с lib.sh + proto-hysteria2.sh
srv() {
    docker exec -i -e ZOO_TEST_ENV=docker "$SRV" bash -c \
        'set -euo pipefail; cd /repo; . scripts/lib.sh; . scripts/lib/proto-hysteria2.sh; config_load; versions_load; '"$1"
}
cli() { docker exec -i "$CLI" bash -c "$1"; }

zoo_exists "$SRV" || zoo_die "нет контейнера $SRV"
MANIFEST="$(docker exec "$SRV" cat /etc/vpn-setup/protocols.d/hysteria2.json)" || zoo_die "нет манифеста hysteria2 на $SRV"
SRV_IP="$(zoo_ip "$SRV")"
SRV_EGRESS="$(docker exec "$SRV" curl -4 -fsS --max-time 10 "$IP_URL" | sed -n 's/^ip=//p' || true)"
echo "сервер $SRV ($SRV_IP), egress ${SRV_EGRESS:-?}"

# ------------------------------------------------------------
step "клиентский контейнер $CLI"
img="zoo-probe"
docker image inspect "$img" >/dev/null 2>&1 || img="ubuntu:24.04"
docker run -d --name "$CLI" --label zoo.harness=1 --label zoo.role=hy2-test \
    --network "$ZOO_NET" "$img" sleep infinity >/dev/null
cli 'command -v curl >/dev/null && command -v jq >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl jq ca-certificates >/dev/null; }'
# shellcheck source=scripts/versions.env
. "$REPO/scripts/versions.env"
arch="$(cli 'uname -m')"; case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
sumvar="HY2_SHA256_$arch"
cli "curl -fsSL --retry 3 -o /usr/local/bin/hysteria '$HY2_URL_BASE/hysteria-linux-$arch' \
     && echo '${!sumvar}  /usr/local/bin/hysteria' | sha256sum -c --quiet && chmod +x /usr/local/bin/hysteria" \
    || zoo_die "не удалось поставить hysteria в клиент"
echo "  клиент: $(cli "hysteria version | awk '/^Version:/ {print \$2}'")"

# jq на хосте (Git Bash) может не быть — берём из клиентского контейнера
jq() { docker exec -i "$CLI" jq "$@"; }

# start_client NAME JSON — клиент с socks5 на 127.0.0.1:PORT; печатает порт; 1 при отказе.
# Порт выводится из имени: функция работает в $(...), счётчик там не сохранился бы
start_client() {
    local name="$1" json="$2" port log
    port=$(( 11000 + $(printf '%s' "$name" | cksum | cut -d' ' -f1) % 20000 ))
    jq --arg l "127.0.0.1:$port" '. + {socks5: {listen: $l}}' <<< "$json" \
        | docker exec -i "$CLI" bash -c "cat > /tmp/$name.json"
    docker exec -d -e HYSTERIA_DISABLE_UPDATE_CHECK=1 "$CLI" bash -c \
        "exec hysteria client -c /tmp/$name.json > /tmp/$name.log 2>&1"
    for _ in $(seq 1 15); do
        log="$(cli "cat /tmp/$name.log 2>/dev/null" || true)"
        [[ "$log" == *"SOCKS5 server listening"* ]] && { echo "$port"; return 0; }
        [[ "$log" == *FATAL* ]] && break
        sleep 1
    done
    echo "$log" | tail -3 >&2
    echo "$port"
    return 1
}
# [h] — чтобы pkill не нашёл собственный bash -c с этим шаблоном
stop_client() { cli "pkill -f '[h]ysteria client -c /tmp/$1.json' || true"; }

# fetch PORT URL → код HTTP; fetch_size PORT URL → байт скачано
fetch()      { cli "curl -s -o /dev/null -w '%{http_code}' --max-time ${3:-20} -x socks5h://127.0.0.1:$1 '$2'" || true; }
fetch_size() { cli "curl -s -o /dev/null -w '%{size_download}' --max-time ${3:-60} -x socks5h://127.0.0.1:$1 '$2'" || true; }
fetch_body() { cli "curl -s --max-time 20 -x socks5h://127.0.0.1:$1 '$2'" || true; }

owner_down() { srv 'proto_hysteria2_traffic' | jq -r --arg u "${1:-owner}" 'select(.user == $u) | .down' | tail -1; }

# check_tunnel TAG PORT — малый, большой, IP выхода
check_tunnel() {
    local tag="$1" port="$2" code size ip
    code="$(fetch "$port" "$SMALL_URL")"
    [ "$code" = "204" ] && ok "$tag: малый запрос ($SMALL_URL → 204)" || bad "$tag: малый запрос: код $code"
    size="$(fetch_size "$port" "$BIG_URL")"
    [ "${size:-0}" -ge "$BIG_SIZE" ] && ok "$tag: большой запрос ($size байт)" || bad "$tag: большой запрос: $size байт"
    ip="$(fetch_body "$port" "$IP_URL" | sed -n 's/^ip=//p' | tr -d '[:space:]')"
    if [ -n "$ip" ] && [ "$ip" = "$SRV_EGRESS" ]; then ok "$tag: IP выхода $ip = egress сервера"
    else bad "$tag: IP выхода «$ip», ожидался «$SRV_EGRESS»"; fi
}

# ------------------------------------------------------------
step "owner из probe.client манифеста"
probe_client="$(jq -c '.probe.client' <<< "$MANIFEST")"
[ "$(jq -r '.probe.kind' <<< "$MANIFEST")" = "hysteria" ] && ok "probe.kind=hysteria" || bad "probe.kind"
before="$(owner_down)"
if P_OWNER="$(start_client owner "$probe_client")"; then
    ok "owner: рукопожатие (TLS insecure + pinSHA256, auth)"
    check_tunnel owner "$P_OWNER"
    after="$(owner_down)"
    if [ $(( ${after:-0} - ${before:-0} )) -ge "$BIG_SIZE" ]; then
        ok "trafficStats: down owner вырос на $(( after - ${before:-0} )) байт (трафик шёл через туннель)"
    else bad "trafficStats: down owner ${before:-0} → ${after:-0}"; fi
else
    bad "owner: клиент не подключился"
fi

step "ссылка из манифеста (официальный клиент принимает URI в server)"
link="$(jq -r '.links[] | select(.user == "owner") | .uri' <<< "$MANIFEST" | head -1)"
echo "  $link" | sed -E 's#//[^@]+@#//***@#'
if P_LINK="$(start_client link "$(jq -cn --arg s "$link" '{server: $s}')")"; then
    code="$(fetch "$P_LINK" "$SMALL_URL")"
    [ "$code" = "204" ] && ok "ссылка owner: работает" || bad "ссылка owner: код $code"
else bad "ссылка owner: клиент не подключился"; fi
stop_client link

step "вид «имя:токен» (как у userpass)"
tok="$(jq -r '.auth' <<< "$probe_client")"
if P_UP="$(start_client userpass "$(jq -c --arg a "owner:$tok" '.auth = $a' <<< "$probe_client")")"; then
    [ "$(fetch "$P_UP" "$SMALL_URL")" = "204" ] && ok "auth owner:токен работает" || bad "auth owner:токен: нет ответа"
else bad "auth owner:токен отвергнут"; fi
stop_client userpass

step "неверный pinSHA256 отвергается"
wrong="$(jq -c '.tls.pinSHA256 = ("0" * 64)' <<< "$probe_client")"
if start_client badpin "$wrong" >/dev/null 2>&1; then bad "клиент с чужим пином подключился"
else ok "клиент с чужим пином не подключился"; fi
stop_client badpin

# ------------------------------------------------------------
step "user add/del/enable без рестарта и без обрыва сессии owner"
U="zt$RANDOM"
pid0="$(docker exec "$SRV" systemctl show -p MainPID --value hysteria-server)"
# долгая закачка owner идёт всё время операций с пользователями
cli "curl -s -o /dev/null -w '%{size_download}' --limit-rate 400k --max-time 120 -x socks5h://127.0.0.1:$P_OWNER 'https://speed.cloudflare.com/__down?bytes=6000000' > /tmp/long.out 2>&1; echo \$? > /tmp/long.rc" &
LONG_PID=$!
sleep 2
srv "proto_hysteria2_user_add $U >/dev/null" && ok "user_add $U" || bad "user_add $U"
users="$(srv 'proto_hysteria2_user_list')"
grep -q "^$U"$'\t' <<< "$users" && ok "user_list содержит $U" || bad "user_list: $users"
pu="$(srv "proto_hysteria2_probe $U")"
if P_U="$(start_client u1 "$(jq -c '.client' <<< "$pu")")"; then
    [ "$(fetch "$P_U" "$SMALL_URL")" = "204" ] && ok "$U: подключился и ходит" || bad "$U: нет ответа"
else bad "$U: клиент не подключился"; fi
links_n="$(srv "proto_hysteria2_links $U" | grep -c '^hysteria2://' || true)"
[ "$links_n" -ge 1 ] && ok "proto_hysteria2_links $U: $links_n ссылок" || bad "proto_hysteria2_links $U пусто"

srv "proto_hysteria2_user_enable $U false" && ok "user_enable $U false" || bad "user_enable false"
code="$(fetch "$P_U" "$SMALL_URL" 8)"; [ "$code" = "204" ] && code="$(fetch "$P_U" "$SMALL_URL" 8)"
[ "$code" != "204" ] && ok "$U отключён: живая сессия оборвана (kick)" || bad "$U отключён, но ходит"
stop_client u1
if start_client u2 "$(jq -c '.client' <<< "$pu")" >/dev/null 2>&1; then bad "$U отключён, но новое подключение прошло"
else ok "$U отключён: новое подключение отвергнуто"; fi
stop_client u2
srv "proto_hysteria2_user_enable $U true" && ok "user_enable $U true" || bad "user_enable true"
if P_U3="$(start_client u3 "$(jq -c '.client' <<< "$pu")")"; then
    [ "$(fetch "$P_U3" "$SMALL_URL")" = "204" ] && ok "$U снова включён и ходит" || bad "$U включён, но нет ответа"
else bad "$U включён, но не подключился"; fi

srv "proto_hysteria2_user_del $U" && ok "user_del $U" || bad "user_del $U"
code="$(fetch "$P_U3" "$SMALL_URL" 8)"; [ "$code" = "204" ] && code="$(fetch "$P_U3" "$SMALL_URL" 8)"
[ "$code" != "204" ] && ok "$U удалён: живая сессия оборвана" || bad "$U удалён, но ходит"
stop_client u3
if start_client u4 "$(jq -c '.client' <<< "$pu")" >/dev/null 2>&1; then bad "$U удалён, но подключился"
else ok "$U удалён: подключение отвергнуто"; fi
stop_client u4
srv "[ ! -e /etc/vpn-setup/clients/$U/hysteria2.pass ]" && ok "файл пароля $U удалён" || bad "clients/$U/hysteria2.pass остался"

wait "$LONG_PID" || true
long_size="$(cli 'cat /tmp/long.out')"; long_rc="$(cli 'cat /tmp/long.rc')"
[ "$long_rc" = "0" ] && [ "${long_size:-0}" -ge 6000000 ] \
    && ok "сессия owner не прервалась: докачано $long_size байт за время add/disable/enable/del" \
    || bad "закачка owner прервалась (rc=$long_rc, $long_size байт)"
pid1="$(docker exec "$SRV" systemctl show -p MainPID --value hysteria-server)"
[ "$pid0" = "$pid1" ] && ok "hysteria-server не перезапускался (MainPID $pid0)" || bad "MainPID $pid0 → $pid1"
[ "$(fetch "$P_OWNER" "$SMALL_URL")" = "204" ] && ok "owner после операций работает" || bad "owner после операций не работает"
stop_client owner

# ------------------------------------------------------------
if jq -e '.probe.hop' <<< "$MANIFEST" >/dev/null; then
    step "port hopping (только диапазон, без основного порта)"
    range="$(jq -r '.hop_ports' <<< "$MANIFEST")"
    hopc="$(jq -c --arg s "$SRV_IP:$range" '.server = $s | . + {transport: {udp: {hopInterval: "5s"}}}' <<< "$probe_client")"
    if P_HOP="$(start_client hop "$hopc")"; then
        check_tunnel hop "$P_HOP"
        sleep 6
        [ "$(fetch "$P_HOP" "$SMALL_URL")" = "204" ] && ok "hop: работает после смены порта" || bad "hop: после смены порта нет ответа"
    else bad "hop: клиент не подключился к $range"; fi
    stop_client hop
    hlink="$(jq -r '.links[] | select(.user == "owner") | .uri | select(test("-hy2hop-"))' <<< "$MANIFEST" | head -1)"
    if [ -n "$hlink" ] && P_HL="$(start_client hoplink "$(jq -cn --arg s "$hlink" '{server: $s}')")"; then
        [ "$(fetch "$P_HL" "$SMALL_URL")" = "204" ] && ok "hop: ссылка (мульти-порт в authority) работает" || bad "hop: ссылка не ходит"
    else bad "hop: ссылка hy2hop не подключилась"; fi
    stop_client hoplink
fi

if obfs="$(docker exec "$SRV" cat /etc/vpn-setup/protocols.d/hysteria2-obfs.json 2>/dev/null)"; then
    step "Salamander (манифест hysteria2-obfs)"
    if P_OB="$(start_client obfs "$(jq -c '.probe.client' <<< "$obfs")")"; then
        check_tunnel obfs "$P_OB"
    else bad "obfs: клиент не подключился"; fi
    stop_client obfs
    olink="$(jq -r '.links[] | select(.user == "owner") | .uri' <<< "$obfs" | head -1)"
    if P_OL="$(start_client obfslink "$(jq -cn --arg s "$olink" '{server: $s}')")"; then
        [ "$(fetch "$P_OL" "$SMALL_URL")" = "204" ] && ok "obfs: ссылка работает" || bad "obfs: ссылка не ходит"
    else bad "obfs: ссылка не подключилась"; fi
    stop_client obfslink
    noobfs="$(jq -c '.probe.client | del(.obfs)' <<< "$obfs")"
    if start_client obfsplain "$noobfs" >/dev/null 2>&1; then bad "obfs-порт принял клиента без Salamander"
    else ok "obfs-порт не принимает клиента без Salamander"; fi
    stop_client obfsplain
fi

echo
echo "итог: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
