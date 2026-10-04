#!/usr/bin/env bash
# docker/tests/amneziawg.sh — e2e AmneziaWG: реальный клиент (amneziawg-go + awg-quick) в
# отдельном контейнере сети zoo-net, конфиг — из probe манифеста сервера.
#
#   docker/tests/amneziawg.sh SERVER [--keep] [--no-users]
#
# SERVER — контейнер стенда (zoo-NAME или NAME) с выполненной фазой 06. Проверки:
#   малый запрос, большой (2 МБ), IP выхода = IP выхода сервера, маршрут через awg0,
#   счётчики peer'а на сервере; клиент закрыт kill-switch'ем (выход только в туннель).
#   Пользователи (без --no-users): add → подключается, disable → нет, enable → да, del → нет.
# Код возврата 0 — всё прошло. Клиентские бинарники копируются с сервера (userspace-движок).

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: amneziawg.sh SERVER [--keep] [--no-users]}"; shift
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
KEEP=0; USERS=1
for a in "$@"; do
    case "$a" in --keep) KEEP=1 ;; --no-users) USERS=0 ;; *) echo "неизвестный аргумент $a" >&2; exit 2 ;; esac
done
CLI="${SRV}-awgcli"
CLIENT_IMAGE="${CLIENT_IMAGE:-zoo-test-server:24.04}"
REPO_IN_SRV="${ZOO_REPO_DIR:-/repo}"
SMALL_URL="https://www.gstatic.com/generate_204"
BIG_URL="https://speed.cloudflare.com/__down?bytes=2000000"
IP_URL="https://api.ipify.org"

fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
info() { echo "....  $*"; }

srv() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
# jq с сервера: на хосте (Git Bash) его может не быть
hjq() { srv jq "$@"; }
cli() { docker exec -i "$CLI" "$@"; }
# вызов функции lib/proto-amneziawg.sh на сервере
awglib() { srv bash -c ". $REPO_IN_SRV/scripts/lib.sh; . $REPO_IN_SRV/scripts/lib/proto-amneziawg.sh; config_load; $*"; }

cleanup() {
    if [ "$KEEP" = "1" ]; then info "клиент оставлен: $CLI"; return; fi
    docker rm -f "$CLI" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null || { echo "нет контейнера $SRV" >&2; exit 2; }
manifest="$(srv cat /etc/vpn-setup/protocols.d/amneziawg.json)" || { echo "на $SRV нет манифеста amneziawg" >&2; exit 2; }
if [ "$(hjq -r .probe.kind <<< "$manifest")" = "awg" ]; then pass "манифест: probe.kind=awg"; else fail "манифест: probe.kind"; fi
ep="$(hjq -r .probe.endpoint <<< "$manifest")"
srv_ip="${ep%:*}"; port="${ep##*:}"
srv_egress="$(srv curl -4 -fsS --max-time 10 "$IP_URL" || true)"
info "сервер $SRV: endpoint $ep, IP выхода ${srv_egress:-?}"

# ---------- PostUp/PostDown идемпотентны: рестарты не копят правила ----------
# shellcheck disable=SC2016 # $ раскрывается на сервере
nat_rules() { srv sh -c 'iptables-save | grep -c "vpn-zoo-awg:awg0" || true'; }
srv systemctl restart awg-quick@awg0
srv systemctl restart awg-quick@awg0
n_up="$(nat_rules)"
srv systemctl stop awg-quick@awg0
n_down="$(nat_rules)"
srv systemctl start awg-quick@awg0
if [ "$n_up" = "11" ] && [ "$n_down" = "0" ]; then
    pass "правила NAT/forward: 11 после двух рестартов, 0 после stop"
else
    fail "правила NAT/forward: после рестартов $n_up (ожидалось 11), после stop $n_down (ожидалось 0)"
fi

# ---------- клиент ----------
docker rm -f "$CLI" >/dev/null 2>&1 || true
docker run -d --name "$CLI" --label zoo.harness=1 --label zoo.role=awg-client \
    --network zoo-net --cap-add NET_ADMIN --device /dev/net/tun \
    --sysctl net.ipv4.conf.all.src_valid_mark=1 \
    --entrypoint sleep "$CLIENT_IMAGE" infinity >/dev/null
for b in amneziawg-go awg awg-quick; do
    srv sh -c "cat \"\$(PATH=/usr/local/bin:\$PATH command -v $b)\"" | cli sh -c "cat > /usr/local/bin/$b && chmod 755 /usr/local/bin/$b"
done
cli mkdir -p /etc/amnezia/amneziawg

# kill-switch: наружу только UDP к серверу и туннель — успешный curl доказывает туннель
cli sh -c "iptables -P OUTPUT DROP && iptables -A OUTPUT -o lo -j ACCEPT && iptables -A OUTPUT -o awg0 -j ACCEPT \
    && iptables -A OUTPUT -p udp -d $srv_ip --dport $port -j ACCEPT"
if cli curl -4 -s -o /dev/null --max-time 5 "$SMALL_URL"; then fail "kill-switch: прямой выход не закрыт"; else pass "kill-switch: прямой выход закрыт"; fi

# client_up USER — поднять туннель с конфигом probe пользователя (DNS — через туннель)
client_up() {
    local conf dns
    conf="$(awglib "proto_amneziawg_probe $1" | hjq -r .conf)"
    [ -n "$conf" ] && [ "$conf" != "null" ] || { fail "probe для $1 пуст"; return 1; }
    dns="$(awk -F' *= *' '$1 == "DNS" {split($2, a, " *, *"); print a[1]}' <<< "$conf")"
    cli sh -c "awg-quick down awg0 >/dev/null 2>&1; true"
    grep -v '^DNS' <<< "$conf" | cli sh -c 'umask 077; cat > /etc/amnezia/amneziawg/awg0.conf'
    cli sh -c "printf 'nameserver %s\n' '${dns:-1.1.1.1}' > /etc/resolv.conf"
    cli env WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/local/bin/amneziawg-go awg-quick up awg0 >/dev/null 2>&1 \
        || { fail "awg-quick up ($1)"; cli awg-quick up awg0 || true; return 1; }
}

# small_ok [СЕК] — повторяет запрос до срока: после смены peer'а клиент делает новое
# рукопожатие только через ~15 с без ответов (таймеры WireGuard)
small_ok() {
    local end=$(( SECONDS + ${1:-15} ))
    while :; do
        [ "$(cli curl -4 -sS -o /dev/null -w '%{http_code}' --max-time 8 "$SMALL_URL" 2>/dev/null || true)" = "204" ] && return 0
        [ "$SECONDS" -lt "$end" ] || return 1
        sleep 1
    done
}

peer_counters() { # USER → «rx tx handshake»
    local pub
    pub="$(srv cat "/etc/vpn-setup/clients/$1/amneziawg.pub")"
    srv awg show awg0 dump | awk -F'\t' -v p="$pub" 'NR > 1 && $1 == p {print $6, $7, $5}'
}

# ---------- owner ----------
if client_up owner; then
    pass "awg-quick up (owner)"
    if small_ok 20; then pass "малый запрос $SMALL_URL → 204"; else fail "малый запрос"; fi
    big="$(cli curl -4 -sS -o /dev/null -w '%{size_download} %{speed_download}' --max-time 60 "$BIG_URL" 2>&1 || true)"
    read -r size speed <<< "$big"
    if [ "${size:-0}" = "2000000" ]; then
        pass "большой запрос 2 МБ (скорость $(( ${speed%.*} / 1024 )) КБ/с)"
    else
        fail "большой запрос: $big"
    fi
    exit_ip="$(cli curl -4 -fsS --max-time 15 "$IP_URL" 2>/dev/null || true)"
    if [ -n "$exit_ip" ] && [ "$exit_ip" = "$srv_egress" ]; then
        pass "IP выхода через туннель = IP выхода сервера ($exit_ip)"
    else
        fail "IP выхода: клиент «$exit_ip», сервер «$srv_egress»"
    fi
    if cli ip route get 1.1.1.1 | grep -q 'dev awg0'; then pass "маршрут в интернет через awg0"; else fail "маршрут не через awg0"; fi
    read -r rx tx hs <<< "$(peer_counters owner)"
    if [ "${hs:-0}" -gt 0 ] && [ "${tx:-0}" -ge 2000000 ]; then
        pass "сервер: рукопожатие есть, peer owner rx=$rx tx=$tx"
    else
        fail "сервер: счётчики owner rx=${rx:-?} tx=${tx:-?} hs=${hs:-?}"
    fi
    tr="$(awglib proto_amneziawg_traffic)"
    # -s: строк несколько (owner, zoo-probe…); jq 1.6 с -e берёт код по последней строке
    if hjq -s -e 'map(select(.user == "owner")) | .[0].down >= 2000000' <<< "$tr" >/dev/null; then pass "proto_amneziawg_traffic: $(hjq -c 'select(.user == "owner")' <<< "$tr")"; else fail "proto_amneziawg_traffic: $tr"; fi
fi

# ---------- пользователи ----------
if [ "$USERS" = "1" ]; then
    u="zt$RANDOM"
    if awglib "proto_amneziawg_user_add $u" >/dev/null; then pass "user_add $u"; else fail "user_add $u"; fi
    # shellcheck disable=SC2016 # $u — переменная jq
    if srv jq -e --arg u "$u" '.links | any(.user == $u)' /etc/vpn-setup/protocols.d/amneziawg.json >/dev/null; then
        pass "манифест содержит $u"; else fail "манифест без $u"; fi
    links="$(awglib "proto_amneziawg_links $u")"
    if grep -q '^vpn://' <<< "$links" && grep -q 'amneziawg.conf$' <<< "$links"; then pass "links: vpn:// и путь .conf"; else fail "links: $links"; fi
    if client_up "$u" && small_ok 20; then pass "новый пользователь $u подключается"; else fail "новый пользователь $u не подключается"; fi
    awglib "proto_amneziawg_user_enable $u false" >/dev/null
    if small_ok 1; then fail "отключённый $u всё ещё ходит"; else pass "отключённый $u не подключается"; fi
    awglib "proto_amneziawg_user_enable $u true" >/dev/null
    if small_ok 60; then pass "включённый снова $u подключается"; else fail "включённый снова $u не подключается"; fi
    if awglib "proto_amneziawg_user_del $u" >/dev/null; then pass "user_del $u"; else fail "user_del $u"; fi
    if small_ok 1; then fail "удалённый $u всё ещё ходит"; else pass "удалённый $u не подключается"; fi
    if srv test -e "/etc/vpn-setup/clients/$u/amneziawg.pub"; then fail "файлы $u остались"; else pass "файлы $u удалены"; fi
    if awglib proto_amneziawg_user_list | grep -q "^$u"$'\t'; then fail "user_list всё ещё содержит $u"; else pass "user_list без $u"; fi
    # owner после всех операций не пострадал
    if client_up owner && small_ok 20; then pass "owner работает после операций с пользователями"; else fail "owner сломался"; fi
fi

echo
if [ "$fails" -eq 0 ]; then echo "ИТОГ: amneziawg e2e — OK"; else echo "ИТОГ: amneziawg e2e — $fails провал(ов)"; fi
[ "$fails" -eq 0 ]
