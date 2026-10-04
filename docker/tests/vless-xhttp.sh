#!/usr/bin/env bash
# tests/vless-xhttp.sh — e2e VLESS+XHTTP+REALITY: настоящий Xray-клиент в отдельном контейнере.
#
#   docker/tests/vless-xhttp.sh SERVER [--keep]
#
# SERVER — контейнер стенда (zoo-NAME или NAME) после фаз 00-03 + 04b, репо в /repo.
# Клиент: контейнер SERVER-xcli в zoo-net, образ как у сервера, Xray — бинарь из 3x-ui
# сервера. Исходящий трафик клиента закрыт iptables везде, кроме IP сервера: любой
# успешный запрос прошёл через туннель. Outbound берётся из манифеста (probe.outbound)
# и из proto_vless_xhttp_probe — так проверяется ровно ссылка, которую выдаёт панель.
# Проверки: generate_204, 2 МБ, IP выхода = IP выхода сервера; новый пользователь
# работает, выключенный и удалённый — нет. Код возврата 0 — все проверки PASS.

# pass/fail не падают: A && pass || fail безопасно
# shellcheck disable=SC2015
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:-}"; KEEP=0
[ -n "$SRV" ] || { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ "${2:-}" = "--keep" ] && KEEP=1
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
CLI="${SRV}-xcli"
SOCKS="socks5h://127.0.0.1:10808"
SMALL_URL="https://www.gstatic.com/generate_204"
BIG_URL="https://speed.cloudflare.com/__down?bytes=2000000"
BIG_BYTES=2000000
# IP выхода — по cdn-cgi/trace (строка ip=): api.ipify.org после фазы 07 блокируется или уходит в WARP (D9)
IP_URL="https://www.cloudflare.com/cdn-cgi/trace"

fails=0
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; fails=$((fails + 1)); }
note() { echo "[i] $*"; }

srv()  { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
cli()  { docker exec -i "$CLI" "$@"; }
lib()  { srv bash /repo/scripts/lib/proto-vless-xhttp.sh "$@"; }

cleanup() {
    if [ "$KEEP" = "0" ]; then docker rm -f "$CLI" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null 2>&1 || { echo "нет контейнера $SRV"; exit 2; }
srv test -f /etc/vpn-setup/protocols.d/vless-xhttp.json || { echo "на $SRV нет манифеста vless-xhttp (фаза 04b не выполнена)"; exit 2; }

image="$(docker container inspect -f '{{.Config.Image}}' "$SRV")"
srv_ip="$(docker container inspect -f '{{(index .NetworkSettings.Networks "zoo-net").IPAddress}}' "$SRV")"
case "$(srv uname -m)" in aarch64|arm64) arch=arm64 ;; *) arch=amd64 ;; esac

# ---------- клиентский контейнер ----------
if docker container inspect "$CLI" >/dev/null 2>&1; then
    [ "$(docker container inspect -f '{{index .Config.Labels "zoo.harness"}}' "$CLI")" = "1" ] \
        || { echo "$CLI существует и создан не стендом — не трогаю"; exit 2; }
    docker rm -f "$CLI" >/dev/null
fi
docker run -d --name "$CLI" --hostname "$CLI" --label zoo.harness=1 --label zoo.role=client \
    --network zoo-net --cap-add NET_ADMIN --entrypoint sleep "$image" infinity >/dev/null
srv cat "/usr/local/x-ui/bin/xray-linux-$arch" | cli sh -c 'cat > /usr/local/bin/xray && chmod 755 /usr/local/bin/xray'
note "клиент $CLI: $(cli xray version | head -1)"

cli sh -c "iptables -A OUTPUT -o lo -j ACCEPT && iptables -A OUTPUT -d $srv_ip -j ACCEPT && iptables -A OUTPUT -j REJECT && (ip6tables -A OUTPUT -o lo -j ACCEPT; ip6tables -A OUTPUT -j REJECT) 2>/dev/null; true"
if cli curl -sS -o /dev/null --max-time 8 "$IP_URL" 2>/dev/null; then
    fail "у клиента остался прямой выход в интернет — проверка IP бессмысленна"
else
    pass "прямой выход клиента закрыт (только $srv_ip)"
fi

egress="$(srv curl -4 -fsS --max-time 15 "$IP_URL" | sed -n 's/^ip=//p' || true)"
[ -n "$egress" ] || { echo "сервер не узнал свой внешний IP ($IP_URL)"; exit 2; }
note "IP выхода сервера: $egress"

# ---------- запуск клиента ----------

# start_client OUTBOUND_JSON — конфиг собираем jq на сервере (в образе клиента jq нет)
start_client() {
    local conf
    conf="$(printf '%s' "$1" | srv jq -c '{log:{loglevel:"warning"},
        inbounds:[{listen:"127.0.0.1", port:10808, protocol:"socks", settings:{udp:true}}],
        outbounds:[.]}')"
    printf '%s' "$conf" | cli sh -c 'cat > /tmp/xh.json'
    cli sh -c 'pkill -x xray; sleep 0.3; xray run -test -c /tmp/xh.json >/tmp/xh.test 2>&1 || { cat /tmp/xh.test; exit 1; }
        nohup xray run -c /tmp/xh.json >/tmp/xh.log 2>&1 &
        for i in 1 2 3 4 5 6 7 8 9 10; do ss -Hltn "sport = :10808" | grep -q . && exit 0; sleep 0.3; done; exit 1'
}

small_ok() { [ "$(cli curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -x "$SOCKS" "$SMALL_URL" 2>/dev/null)" = "204" ]; }

# Полный набор: маленький запрос (с ожиданием до ~40 с: REALITY при старте Xray
# сначала опрашивает target), 2 МБ, IP выхода
check_tunnel() {
    local who="$1" size ip
    for _ in $(seq 1 8); do small_ok && break; sleep 5; done
    if small_ok; then pass "$who: $SMALL_URL → 204"; else
        fail "$who: $SMALL_URL через туннель не прошёл"; cli tail -5 /tmp/xh.log || true; return 0
    fi
    # вторая попытка: speed.cloudflare.com при частых прогонах изредка отвечает пустым телом
    for _ in 1 2; do
        size="$(cli curl -sS -o /dev/null -w '%{size_download}' --max-time 90 -x "$SOCKS" "$BIG_URL" 2>/dev/null || true)"
        [ "${size%.*}" = "$BIG_BYTES" ] && break
        sleep 3
    done
    if [ "${size%.*}" = "$BIG_BYTES" ]; then pass "$who: большой ответ $size байт"; else fail "$who: большой ответ ${size:-0} байт из $BIG_BYTES"; fi
    ip="$(cli curl -sS --max-time 20 -x "$SOCKS" "$IP_URL" 2>/dev/null | sed -n 's/^ip=//p' || true)"
    if [ "$ip" = "$egress" ]; then pass "$who: IP выхода $ip = IP сервера"; else fail "$who: IP выхода «$ip», ожидался $egress"; fi
}

# Пользователь должен перестать проходить (панель применяет изменения сразу или за ≤30 с)
check_denied() {
    local who="$1"
    for _ in $(seq 1 14); do small_ok || { pass "$who: доступ закрыт"; return 0; }; sleep 3; done
    fail "$who: через 40 с туннель всё ещё работает"
}

# ---------- owner из манифеста ----------
manifest="$(srv cat /etc/vpn-setup/protocols.d/vless-xhttp.json)"
kind="$(printf '%s' "$manifest" | srv jq -r '.probe.kind')"
[ "$kind" = "xray" ] && pass "manifest: probe.kind=xray" || fail "manifest: probe.kind=$kind"
placement="$(printf '%s' "$manifest" | srv jq -r '.params.placement')"
note "размещение: $placement, порт $(printf '%s' "$manifest" | srv jq -r '.port')"
printf '%s' "$manifest" | srv jq -e '.links | any(.user == "owner" and (.uri | startswith("vless://")) and (.uri | test("type=xhttp")))' >/dev/null \
    && pass "manifest: ссылка owner type=xhttp" || fail "manifest: нет ссылки owner"
printf '%s' "$manifest" | srv jq -e '.probe.outbound.settings.vnext[0].users[0].flow == ""' >/dev/null \
    && pass "manifest: flow пустой" || fail "manifest: flow не пустой"

owner_down() { lib traffic 2>/dev/null | srv jq -rs '[.[] | select(.user == "owner") | .down][0] // 0' || echo 0; }
down0="$(owner_down)"

start_client "$(printf '%s' "$manifest" | srv jq -c '.probe.outbound')" || fail "owner: xray-клиент не стартовал"
check_tunnel owner

# панель собирает статистику Xray периодически (~10 с): ждём прирост ≥ большого ответа
delta=0
for _ in $(seq 1 12); do
    delta=$(( $(owner_down) - down0 ))
    [ "$delta" -ge "$BIG_BYTES" ] && break
    sleep 3
done
[ "$delta" -ge "$BIG_BYTES" ] && pass "traffic: owner down +$delta" || fail "traffic: owner down +$delta (ожидалось ≥ $BIG_BYTES)"

# ---------- новый пользователь ----------
U="zvx$$"   # уникален и при параллельных тестах
if lib user_add "$U" >/dev/null 2>&1; then pass "user_add $U"; else fail "user_add $U"; fi
link="$(lib links "$U" 2>/dev/null | head -1)"
[[ "$link" == vless://*type=xhttp* ]] && pass "links $U: ${link:0:60}…" || fail "links $U: «$link»"
lib user_list 2>/dev/null | grep -q "^$U	true" && pass "user_list содержит $U" || fail "user_list без $U"
probe="$(lib probe "$U" 2>/dev/null || true)"
if [ -n "$probe" ]; then
    start_client "$(printf '%s' "$probe" | srv jq -c '.outbound')" || fail "$U: xray-клиент не стартовал"
    check_tunnel "$U"

    lib user_enable "$U" false >/dev/null 2>&1 && pass "user_enable $U false" || fail "user_enable $U false"
    check_denied "$U (выключен)"
    lib user_enable "$U" true >/dev/null 2>&1 && pass "user_enable $U true" || fail "user_enable $U true"
    ok=0; for _ in $(seq 1 14); do small_ok && { ok=1; break; }; sleep 3; done
    [ "$ok" = "1" ] && pass "$U (включён снова): туннель работает" || fail "$U: после включения туннель не работает"

    lib user_del "$U" >/dev/null 2>&1 && pass "user_del $U" || fail "user_del $U"
    check_denied "$U (удалён)"
    srv test ! -e "/etc/vpn-setup/clients/$U/vless-xhttp.uri" && pass "файл ссылки $U удалён" || fail "файл ссылки $U остался"
else
    fail "probe $U пустой"
fi

# owner после манипуляций с другим пользователем жив
start_client "$(printf '%s' "$manifest" | srv jq -c '.probe.outbound')" >/dev/null 2>&1 || true
small_ok && pass "owner после удаления $U работает" || fail "owner после удаления $U не работает"

lib manifest_refresh >/dev/null 2>&1 && pass "manifest_refresh" || fail "manifest_refresh"
# shellcheck disable=SC2016 # jq-выражение
srv jq -e --arg u "$U" 'all(.links[]; .user != $u)' /etc/vpn-setup/protocols.d/vless-xhttp.json >/dev/null \
    && pass "манифест без удалённого $U" || fail "в манифесте остался $U"

echo
if [ "$fails" -eq 0 ]; then echo "vless-xhttp e2e: PASS ($placement)"; else echo "vless-xhttp e2e: FAIL ($fails)"; fi
[ "$fails" -eq 0 ]
