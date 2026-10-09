#!/usr/bin/env bash
# docker/tests/mtproto.sh SERVER [--keep] — сквозной тест MTProxy (D63). Сервер прошёл фазу 04e
# (ENABLE_MTPROTO=1). Клиента Telegram на стенде нет, поэтому:
#   1. манифест, ссылка tg://proxy owner, порт слушает mtg, порт в ports.tsv и UFW;
#   2. из отдельного клиентского контейнера (zoo-net): соединение без секрета с SNI домена Fake-TLS
#      получает TLS настоящего сайта (openssl s_client, сертификат проверяется);
#   3. рукопожатие Fake-TLS с секретом (zoolib/probe/mtproto.py) из клиентского контейнера: подпись
#      ответа mtg сходится; zoo probe --local --proto mtproto → OK;
#   4. пользователь: add → секрет принят, disable → не принят, enable → принят, del → не принят;
#      у двух пользователей разные секреты.
# Вывод: строки PASS/FAIL, код 0 — всё прошло.

# pass/fail всегда возвращают 0, поэтому «A && pass || fail» безопасно
# shellcheck disable=SC2015

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SERVER="${1:?контейнер сервера (zoo-...)}"
KEEP="${2:-}"
case "$SERVER" in zoo-*) ;; *) SERVER="zoo-$SERVER" ;; esac
TEST_USER="zt-mt-$$"
CLIENT="${SERVER}-cl-mtproto"
FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
info() { echo "      $*"; }
cleanup() {
    [ "$KEEP" = "--keep" ] && { info "клиент оставлен: $CLIENT"; return 0; }
    docker rm -f "$CLIENT" >/dev/null 2>&1 || true
}
die() { echo "FAIL  $*"; cleanup; exit 1; }

srv_env() { docker exec -i -e ZOO_TEST_ENV=docker "$SERVER" "$@"; }
srv() {
    docker exec -i -e ZOO_TEST_ENV=docker "$SERVER" bash -c '
        set -euo pipefail
        cd /repo
        . scripts/lib.sh; . scripts/lib/xui.sh; . scripts/lib/proto-mtproto.sh
        config_load
        "$@"' _ "$@"
}
cl() { docker exec -i "$CLIENT" "$@"; }
jq() { docker exec -i "$SERVER" jq "$@"; }

docker container inspect "$SERVER" >/dev/null 2>&1 || { echo "нет контейнера $SERVER"; exit 2; }

manifest="$(srv_env cat /etc/vpn-setup/protocols.d/mtproto.json)" || die "нет манифеста mtproto (ENABLE_MTPROTO=1?)"
[ "$(jq -r '.enabled' <<< "$manifest")" = "true" ] && pass "манифест: enabled" || fail "манифест: enabled=$(jq -r '.enabled' <<< "$manifest")"
port="$(jq -r '.port' <<< "$manifest")"
host="$(jq -r '.probe.server' <<< "$manifest")"
domain="$(jq -r '.fake_tls.domain' <<< "$manifest")"
info "MTProxy: $host:$port/tcp, Fake-TLS $domain"
owner_link="$(jq -r '.links[] | select(.user == "owner") | .uri' <<< "$manifest")"
[[ "$owner_link" == "tg://proxy?server="*"&port=$port&secret=ee"* ]] && pass "ссылка owner: tg://proxy?…&port=$port&secret=ee…" \
    || fail "ссылка owner: «${owner_link:0:40}»"
[ "$domain" != "$(srv_env bash -c '. /etc/vpn-setup/config.env; echo "$VLESS_SNI"')" ] \
    && pass "домен Fake-TLS не совпадает с SNI VLESS" || fail "домен Fake-TLS = VLESS_SNI"

listener="$(srv_env ss -Hltnp "sport = :$port" | head -1)"
[[ "$listener" == *'"mtg-'* ]] && pass "порт $port/tcp слушает mtg" || fail "порт $port/tcp: «$listener»"
srv_env grep -q "^$port/tcp"$'\t' /etc/vpn-setup/ports.tsv && pass "порт в ports.tsv" || fail "порта нет в ports.tsv"
if srv_env ufw status 2>/dev/null | grep -q "^Status: active"; then
    srv_env ufw status | grep -q "^$port/tcp .*ALLOW" && pass "UFW пропускает $port/tcp" || fail "в UFW нет правила $port/tcp"
else
    info "UFW на стенде не активен — правило не проверяю"
fi

# ---------- клиентский контейнер ----------
distro="$(docker container inspect -f '{{index .Config.Labels "zoo.ubuntu"}}' "$SERVER" 2>/dev/null || true)"
image="zoo-test-server:${distro:-24.04}"
docker rm -f "$CLIENT" >/dev/null 2>&1 || true
docker run -d --name "$CLIENT" --hostname "$CLIENT" --label zoo.harness=1 --label zoo.role=client \
    --network zoo-net --entrypoint sleep "$image" infinity >/dev/null || die "не стартовал клиентский контейнер ($image)"
trap cleanup EXIT
# пробник zoo — в клиент (только stdlib)
docker exec -i "$SERVER" tar -C /repo -cf - zoo/zoolib | cl tar -C /tmp -xf - || die "не скопировал zoolib в клиент"

if cl timeout 20 openssl s_client -connect "$host:$port" -servername "$domain" -verify_hostname "$domain" \
        -verify_return_error </dev/null >/dev/null 2>&1; then
    pass "без секрета: TLS настоящего $domain с верным сертификатом (fronting)"
else
    fail "без секрета: нет TLS настоящего $domain на $port"
fi

# hs SECRET → статус рукопожатия Fake-TLS с этим секретом (ok | rejected | fail | timeout)
hs() {
    cl python3 -c 'import sys; sys.path.insert(0, "/tmp/zoo")
from zoolib.probe import mtproto
print(mtproto.handshake(sys.argv[1], int(sys.argv[2]), sys.argv[3], 8)[0])' "$host" "$port" "$1" 2>/dev/null || echo error
}
# ждать статус до N попыток: секреты mtg применяет без рестарта, но не мгновенно
hs_wait() {
    local want="$1" sec="$2" r=""
    for _ in 1 2 3 4 5 6 7 8; do r="$(hs "$sec")"; [ "$r" = "$want" ] && break; sleep 2; done
    printf '%s' "$r"
}
owner_secret="$(jq -r '.probe.secret' <<< "$manifest")"
r="$(hs_wait ok "$owner_secret")"
[ "$r" = ok ] && pass "owner: рукопожатие Fake-TLS, подпись mtg сходится" || fail "owner: рукопожатие → $r"
r="$(hs "ee$(printf '0%.0s' $(seq 1 32))$(printf '%s' "$domain" | od -An -tx1 | tr -d ' \n')")"
[ "$r" = rejected ] && pass "чужой секрет: ответ настоящего сайта, не mtg" || fail "чужой секрет → $r"

v="$(srv_env zoo probe --local --proto mtproto --no-history --json 2>/dev/null \
    | jq -r '.results[] | select(.id == "mtproto") | .verdict' 2>/dev/null | head -1 || true)"
[ "$v" = "OK" ] && pass "zoo probe --local --proto mtproto → OK" || fail "zoo probe --local --proto mtproto → «$v»"

# ---------- пользователь ----------
srv proto_mtproto_user_add "$TEST_USER" >/dev/null || die "user_add $TEST_USER"
grep -q "^$TEST_USER"$'\t' <<< "$(srv proto_mtproto_user_list)" && pass "user_list видит $TEST_USER" || fail "user_list не видит $TEST_USER"
link="$(srv proto_mtproto_links "$TEST_USER")"
sec="${link##*secret=}"
[[ "$link" == tg://proxy* ]] && [ -n "$sec" ] && [ "$sec" != "$owner_secret" ] \
    && pass "у $TEST_USER свой секрет (не как у owner)" || fail "секрет $TEST_USER: «${sec:0:12}…»"
[ "$(hs_wait ok "$sec")" = ok ] && pass "$TEST_USER после add: секрет принят" || fail "$TEST_USER после add: секрет не принят"
srv proto_mtproto_user_enable "$TEST_USER" false >/dev/null || fail "user_enable false"
[ "$(hs_wait rejected "$sec")" = rejected ] && pass "$TEST_USER после disable: секрет не принят" || fail "$TEST_USER после disable: всё ещё принят"
[ "$(hs_wait ok "$owner_secret")" = ok ] && pass "owner не задет отключением $TEST_USER" || fail "owner перестал работать"
srv proto_mtproto_user_enable "$TEST_USER" true >/dev/null || fail "user_enable true"
[ "$(hs_wait ok "$sec")" = ok ] && pass "$TEST_USER после enable: секрет принят" || fail "$TEST_USER после enable: не принят"
srv proto_mtproto_user_del "$TEST_USER" >/dev/null || fail "user_del"
[ "$(hs_wait rejected "$sec")" = rejected ] && pass "$TEST_USER после del: секрет не принят" || fail "$TEST_USER после del: всё ещё принят"

echo
[ "$FAILS" -eq 0 ] && { echo "mtproto: все проверки прошли"; exit 0; }
echo "mtproto: провалов — $FAILS"
exit 1
