#!/usr/bin/env bash
# docker/tests/security.sh SERVER [--keep] — проверки безопасности установленного сервера.
#
# Сервер (без клиента):
#   - публичные сокеты (не loopback) = только порты из ports.tsv; ufw active, default deny,
#     каждое правило allow/limit есть в ports.tsv; запрещённых портов нет;
#   - права файлов с секретами (0600/0700, Hysteria — без «other»);
#   - секреты config.env не читаются непривилегированным пользователем (grep от nobody);
#   - 3x-ui: панель и подписка на 127.0.0.1, подписка и JSON-подписка выключены, API без
#     токена — 404, access-лог Xray выключен; панель не видна из сети.
# Туннели (клиенты — отдельный контейнер в zoo-net, наружу только сервер):
#   xray (VLESS, XHTTP, SS-2022), hysteria (Hy2, Salamander), sing-box (TUIC), awg —
#   туннель работает; echo (api.ipify.org) не видит IP сервера; сервисы сервера на
#   127.0.0.1 и на его адресах в обход ufw недоступны; SSH на адрес сервера доступен.
#   Адрес сервера в стенде частный (его закрывает geoip:private), поэтому на lo сервера на
#   время теста вешается публичный ALIAS_IP и маршрутизация переприменяется (Hy2 рестартует).
# Код выхода 0 — всё PASS.

# pass/fail возвращают 0: «A && pass || fail» безопасно; $ в одинарных кавычках — для сервера
# shellcheck disable=SC2015,SC2016

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: security.sh SERVER [--keep]}"
KEEP="${2:-}"
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
CLI="${SRV}-seccli"
ALIAS_IP="${ALIAS_IP:-9.9.9.77}"
C_LO=5591      # канарейка на 127.0.0.1
C_ANY=5592     # канарейка на 0.0.0.0, порт закрыт ufw
SMALL_URL="https://www.gstatic.com/generate_204"
ECHO_URL="https://api.ipify.org"

# shellcheck source=../../scripts/versions.env
. "$(dirname "${BASH_SOURCE[0]}")/../../scripts/versions.env"

FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
warn() { echo "WARN  $*"; }
info() { echo "      $*"; }

sx() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
cx() { docker exec -i "$CLI" "$@"; }
# маршрутизация Xray и ACL Hysteria заново (адреса сервера берутся в момент применения)
routing_reapply() {
    sx bash -c 'set -euo pipefail; cd /repo; . scripts/lib.sh; . scripts/lib/xui.sh; . scripts/lib/routing.sh
        config_load; routing_xray_apply >/dev/null; routing_hy2_apply >/dev/null' \
        || fail "переприменение маршрутизации не удалось"
}

cleanup() {
    sx bash -c "pkill -f '[z]oo-canary' || true; ip addr del $ALIAS_IP/32 dev lo 2>/dev/null || true" || true
    if [ "${ALIAS_ADDED:-0}" = "1" ]; then routing_reapply >/dev/null 2>&1 || true; fi
    [ "$KEEP" = "--keep" ] && { info "клиент оставлен: $CLI"; return 0; }
    docker rm -f "$CLI" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null 2>&1 || { echo "нет контейнера $SRV" >&2; exit 2; }

# ============================================================
# 1. Сервер
# ============================================================

echo "== сервер"
audit="$(sx bash -s <<'EOS'
set -uo pipefail
. /repo/scripts/lib.sh
config_load
P() { echo "PASS  $*"; }
F() { echo "FAIL  $*"; }
W() { echo "WARN  $*"; }

reg="$(cut -f1 "$PORTS_FILE" 2>/dev/null)"
eph="$(awk '{print $1}' /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || echo 32768)"

# публичные сокеты
bad=0
while read -r proto local proc; do
    addr="${local%:*}"; port="${local##*:}"
    case "$addr" in 127.*|"[::1]"|"[::ffff:127."*) continue ;; esac
    if grep -qx "$port/$proto" <<< "$reg"; then continue; fi
    if [ "$proto" = "udp" ] && [ "$port" -ge "$eph" ]; then
        W "UDP $local ($proc) — эфемерный порт не из ports.tsv (исходящий сокет?)"; continue
    fi
    F "слушает наружу, но нет в ports.tsv: $proto $local $proc"; bad=1
done < <(ss -Htulnp | awk '{p = ($1 == "tcp") ? "tcp" : "udp"; print p, $5, $7}')
[ "$bad" = 0 ] && P "наружу слушают только порты из ports.tsv: $(tr '\n' ' ' <<< "$reg")"

# запрещённые порты
banned=""
for p in $BANNED_PORTS; do
    grep -qE "^$p/" <<< "$reg" && banned="$banned $p"
    [ -n "$(ss -Htuln "sport = :$p" | awk '$5 !~ /^(127\.|\[::1\])/')" ] && banned="$banned $p(listen)"
done
[ -z "$banned" ] && P "запрещённые порты ($BANNED_PORTS) не используются" || F "запрещённые порты:$banned"

# ufw
st="$(ufw status verbose 2>/dev/null)"
[[ "$st" == "Status: active"* ]] && P "ufw активен" || F "ufw не активен"
grep -q 'Default: deny (incoming)' <<< "$st" && P "ufw: default deny incoming" || F "ufw: не default deny incoming"
extra=""
while read -r spec; do
    grep -qx "$spec" <<< "$reg" || extra="$extra $spec"
done < <(ufw show added 2>/dev/null | awk '$1 == "ufw" && ($2 == "allow" || $2 == "limit") {print $3}' | grep -E '^[0-9:]+/(tcp|udp)$' | sort -u)
[ -z "$extra" ] && P "каждое правило ufw allow/limit есть в ports.tsv" || F "правила ufw вне ports.tsv:$extra"

# права файлов: «нет доступа group/other» (077) или «нет other» (007)
chk() {
    local mask="$1" f m; shift
    for f in "$@"; do
        [ -e "$f" ] || continue
        m="$(stat -c '%a' "$f")"
        if (( 8#$m & 8#$mask )); then F "права $m у $f (маска $mask)"; return 1; fi
    done
    return 0
}
ok=0
chk 077 /etc/vpn-setup /etc/vpn-setup/config.env /etc/vpn-setup/xui-auth.hdr /etc/vpn-setup/hy2-stats.hdr \
    /etc/vpn-setup/probe-export.json /etc/vpn-setup/users.json /etc/vpn-setup/protocols.d /etc/vpn-setup/protocols.d/* \
    /etc/vpn-setup/clients /etc/vpn-setup/clients/* /etc/vpn-setup/clients/*/* /etc/vpn-setup/tuic /etc/vpn-setup/tuic/* \
    /etc/amnezia/amneziawg /etc/amnezia/amneziawg/*.conf /etc/x-ui /etc/x-ui/x-ui.db* /root/CREDENTIALS.md \
    /var/log/vpn-zoo /var/log/vpn-zoo/* /var/backups/vpn-setup || ok=1
chk 007 /etc/hysteria /etc/hysteria/config.yaml /etc/hysteria/obfs.yaml /etc/hysteria/key.pem \
    /etc/hysteria/users.tsv /etc/hysteria/outbounds.yaml /etc/hysteria/hop.nft || ok=1
[ "$ok" = 0 ] && P "права файлов с секретами: 0600/0700, Hysteria без доступа other"

# то, что запускает root (бинари, юниты, библиотеки фаз): владелец root, без записи для чужих
own="$(find /usr/local /opt/vpn-zoo /etc/systemd/system /etc/hysteria /etc/amnezia -mindepth 1 ! -type l \
    \( ! -user root -o -perm -0002 -o \( -perm -0020 ! -group root \) \) -print 2>/dev/null | head -5)"
[ -z "$own" ] && P "бинари/юниты/конфиги: владелец root, запись только root" || F "чужой владелец или запись не-root: $(tr '\n' ' ' <<< "$own")"

# секреты config.env глазами непривилегированного пользователя
pat="$(grep -E '^[A-Z0-9_]*(PASS|PASSWORD|PRIV|SECRET|TOKEN|PSK|_KEY|HPK)=' "$CONFIG_FILE" \
    | sed -E "s/^[^=]+=//; s/^'(.*)'$/\1/" | awk 'length($0) >= 12' | sort -u)"
n="$(grep -c . <<< "$pat")"
if [ "$n" -gt 0 ]; then
    leak="$(printf '%s\n' "$pat" | runuser -u nobody -- grep -rlsF -f - /etc /usr/local /opt /var/lib /var/log \
        /var/backups /var/cache /var/tmp /tmp /root /run /home /srv 2>/dev/null | head -5 || true)"
    [ -z "$leak" ] && P "nobody не читает ни одного из $n секретов config.env" || F "секреты читаются nobody: $(tr '\n' ' ' <<< "$leak")"
else
    W "в config.env не нашлось секретов для проверки"
fi

# 3x-ui
. /repo/scripts/lib/xui.sh
s="$(xui_settings_get)"
[ "$(jq -r .webListen <<< "$s")" = "127.0.0.1" ] && P "панель: webListen 127.0.0.1" || F "панель: webListen $(jq -r .webListen <<< "$s")"
if [ "${SUB_PUBLIC:-0}" = "1" ]; then
    W "SUB_PUBLIC=1 — подписка включена владельцем"
else
    [ "$(jq -r '"\(.subEnable) \(.subListen)"' <<< "$s")" = "false 127.0.0.1" ] && P "подписка выключена, subListen 127.0.0.1" \
        || F "подписка: $(jq -c '{subEnable, subListen}' <<< "$s")"
fi
[ "$(jq -r .subJsonEnable <<< "$s")" = "false" ] && P "JSON-подписка выключена" || F "JSON-подписка включена"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$(xui_base_url)/panel/api/server/status")"
[ "$code" = "404" ] && P "API без токена → 404" || F "API без токена → HTTP $code"
acc="$(jq -r '.log.access // "?"' /usr/local/x-ui/bin/config.json 2>/dev/null)"
[ "$acc" = "none" ] && P "access-лог Xray выключен" || F "access-лог Xray: $acc"
echo "PANEL_PORT=$PANEL_PORT"
echo "SSH_PORT=$(tr ',' '\n' <<< "${SSH_PORTS:-22}" | head -1)"
echo "EGRESS=$(curl -4 -fsS --max-time 15 https://www.cloudflare.com/cdn-cgi/trace | sed -n 's/^ip=//p')"
EOS
)" || true
grep -E '^(PASS|FAIL|WARN) ' <<< "$audit" || true
FAILS=$((FAILS + $(grep -c '^FAIL ' <<< "$audit" || true)))
PANEL_PORT="$(sed -n 's/^PANEL_PORT=//p' <<< "$audit")"
SSH_PORT="$(sed -n 's/^SSH_PORT=//p' <<< "$audit")"
EGRESS="$(sed -n 's/^EGRESS=//p' <<< "$audit")"
SRV_IP="$(docker container inspect -f '{{(index .NetworkSettings.Networks "zoo-net").IPAddress}}' "$SRV")"
info "сервер $SRV_IP, выход ${EGRESS:-?}, панель 127.0.0.1:${PANEL_PORT:-?}"

# ============================================================
# 2. Клиент
# ============================================================

IMAGE="$(docker container inspect -f '{{.Config.Image}}' "$SRV")"
docker rm -f "$CLI" >/dev/null 2>&1 || true
docker run -d --name "$CLI" --label zoo.harness=1 --label zoo.role=client --network zoo-net \
    --cap-add NET_ADMIN --device /dev/net/tun --sysctl net.ipv4.conf.all.src_valid_mark=1 \
    --entrypoint sleep "$IMAGE" infinity >/dev/null
arch="$(cx uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
sx cat "/usr/local/x-ui/bin/xray-linux-$arch" | cx sh -c 'cat > /usr/local/bin/xray && chmod 755 /usr/local/bin/xray'
if sx test -x /usr/local/bin/hysteria; then
    sx cat /usr/local/bin/hysteria | cx sh -c 'cat > /usr/local/bin/hysteria && chmod 755 /usr/local/bin/hysteria'
fi
if sx test -f /etc/vpn-setup/protocols.d/amneziawg.json; then
    for b in amneziawg-go awg awg-quick; do
        sx sh -c "cat \"\$(PATH=/usr/local/bin:\$PATH command -v $b)\"" | cx sh -c "cat > /usr/local/bin/$b && chmod 755 /usr/local/bin/$b"
    done
fi
if sx test -f /etc/vpn-setup/protocols.d/tuic.json; then
    sum_var="SINGBOX_SHA256_$arch"
    cx sh -c "set -e; cd /tmp; curl -fsSL --max-time 120 -o sb.tgz \
        $SINGBOX_URL_BASE/sing-box-$SINGBOX_VERSION-linux-$arch.tar.gz
        echo '${!sum_var}  sb.tgz' | sha256sum -c - >/dev/null
        tar -xzf sb.tgz; install -m 755 sing-box-$SINGBOX_VERSION-linux-$arch/sing-box /usr/local/bin/sing-box" \
        || fail "sing-box $SINGBOX_VERSION не поставился (sha256)"
fi

# панель не видна из сети (ни на IP сервера, ни через его loopback)
if cx bash -c "exec 3<>/dev/tcp/$SRV_IP/${PANEL_PORT:-1}" 2>/dev/null; then fail "панель доступна снаружи $SRV_IP:$PANEL_PORT"; else pass "панель недоступна снаружи ($SRV_IP:$PANEL_PORT)"; fi

# наружу из клиента — только сервер
cx sh -c "iptables -A OUTPUT -o lo -j ACCEPT && iptables -A OUTPUT -o awg0 -j ACCEPT \
    && iptables -A OUTPUT -d $SRV_IP -j ACCEPT && iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT \
    && iptables -A OUTPUT -j REJECT"
cx curl -s -o /dev/null --max-time 5 "$SMALL_URL" && fail "клиент выходит в интернет напрямую" || pass "прямой выход клиента закрыт"

# ---------- канарейки и публичный адрес сервера ----------
sx bash -c "ip addr add $ALIAS_IP/32 dev lo" && ALIAS_ADDED=1
canary() {
    sx bash -c "rm -f /tmp/zoo-canary.$2; nohup python3 -c '
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(64)
while True:
    c, a = s.accept(); open(\"/tmp/zoo-canary.\" + sys.argv[2], \"a\").write(repr(a) + \"\\n\"); c.sendall(b\"HTTP/1.0 200 OK\\r\\n\\r\\nHIT\"); c.close()
' $1 $2 zoo-canary >/dev/null 2>&1 &"
}
canary 127.0.0.1 "$C_LO"
canary 0.0.0.0 "$C_ANY"
sleep 1
hits() { sx sh -c "cat /tmp/zoo-canary.$C_LO /tmp/zoo-canary.$C_ANY 2>/dev/null | wc -l"; }
[ "$(sx curl -s --max-time 3 "http://127.0.0.1:$C_ANY/")" = "HIT" ] && pass "канарейки подняты ($C_LO loopback, $C_ANY за ufw)" || fail "канарейки не поднялись"
sx sh -c "rm -f /tmp/zoo-canary.$C_LO /tmp/zoo-canary.$C_ANY"
info "публичный адрес $ALIAS_IP на lo сервера, маршрутизация переприменяется"
routing_reapply

# check_tunnel NAME CURL_ARGS... — curl-аргументы прокси (пусто для AWG)
check_tunnel() {
    local name="$1" r t h0 out; shift
    r=""
    for _ in 1 2 3 4 5 6; do
        r="$(cx curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$SMALL_URL" 2>/dev/null || true)"
        [ "$r" = "204" ] && break; sleep 3
    done
    [ "$r" = "204" ] && pass "[$name] туннель работает" || { fail "[$name] туннель не работает ($r)"; return 0; }
    out="$(cx curl -sS --max-time 10 "$@" "$ECHO_URL" 2>&1 || true)"
    if [ -n "$EGRESS" ] && grep -qF "$EGRESS" <<< "$out"; then
        # AWG — L3, доменов нет: echo-правило для него невозможно (RISK-REDUCTION §5)
        if [ "$name" = "awg" ]; then warn "[awg] echo видит IP сервера (L3, ожидаемо: только сплит на клиенте)"; else fail "[$name] echo $ECHO_URL видит IP сервера"; fi
    else
        pass "[$name] echo $ECHO_URL не видит IP сервера"
    fi
    h0="$(hits)"
    for t in "127.0.0.1:$C_LO" "localhost:$C_LO" "[::1]:$C_LO" "$ALIAS_IP:$C_ANY" "$SRV_IP:$C_ANY" "127.0.0.1.nip.io:$C_LO"; do
        cx curl -s -o /dev/null --max-time 6 "$@" "http://$t/" >/dev/null 2>&1 || true
    done
    [ "$(hits)" = "$h0" ] && pass "[$name] сервисы сервера на loopback и за ufw недоступны" \
        || fail "[$name] через туннель дошли до сервиса сервера: $(sx cat /tmp/zoo-canary.$C_LO /tmp/zoo-canary.$C_ANY 2>/dev/null | tr '\n' ' ')"
    sx sh -c "rm -f /tmp/zoo-canary.$C_LO /tmp/zoo-canary.$C_ANY"
    if [ "$name" != "awg" ]; then
        out="$(cx curl --http0.9 -s --max-time 5 "$@" "http://$ALIAS_IP:${SSH_PORT:-22}/" 2>/dev/null || true)"
        [[ "$out" == SSH-* ]] && pass "[$name] SSH на адрес сервера через туннель доступен" || warn "[$name] SSH на $ALIAS_IP:${SSH_PORT:-22} через туннель не отвечает"
    fi
}

stop_clients() { cx sh -c "pkill -f '[r]un -c /tmp/sec-' ; pkill -f '[c]lient -c /tmp/sec-'; true"; sleep 1; }

port=10850
for f in $(sx sh -c 'ls /etc/vpn-setup/protocols.d/ 2>/dev/null' | tr -d '\r'); do
    id="${f%.json}"
    m="$(sx cat "/etc/vpn-setup/protocols.d/$f")"
    [ "$(sx jq -r '.enabled != false' <<< "$m")" = "true" ] || { info "$id выключен — пропуск"; continue; }
    kind="$(sx jq -r '.probe.kind // empty' <<< "$m")"
    port=$((port + 1))
    case "$kind" in
        xray)
            sx jq -c --argjson p "$port" '{log: {loglevel: "warning"},
                inbounds: [{listen: "127.0.0.1", port: $p, protocol: "socks", settings: {udp: true}}],
                outbounds: [.probe.outbound]}' <<< "$m" | cx sh -c "cat > /tmp/sec-$id.json"
            docker exec -d "$CLI" sh -c "exec xray run -c /tmp/sec-$id.json > /tmp/sec-$id.log 2>&1" ;;
        hysteria)
            sx jq -c --arg l "127.0.0.1:$port" '.probe.client + {socks5: {listen: $l}}' <<< "$m" | cx sh -c "cat > /tmp/sec-$id.json"
            docker exec -d "$CLI" sh -c "HYSTERIA_DISABLE_UPDATE_CHECK=1 exec hysteria client -c /tmp/sec-$id.json > /tmp/sec-$id.log 2>&1" ;;
        sing-box)
            sx jq -c --argjson p "$port" '{log: {level: "warn"}, inbounds: [{type: "mixed", listen: "127.0.0.1", listen_port: $p}],
                outbounds: [.probe.outbound]}' <<< "$m" | cx sh -c "cat > /tmp/sec-$id.json"
            docker exec -d "$CLI" sh -c "exec sing-box run -c /tmp/sec-$id.json > /tmp/sec-$id.log 2>&1" ;;
        *) continue ;;
    esac
    sleep 2
    check_tunnel "$id" -x "socks5h://127.0.0.1:$port"
    stop_clients
done

# AmneziaWG: L3, весь трафик клиента (и к адресу сервера) идёт в туннель
if sx test -f /etc/vpn-setup/protocols.d/amneziawg.json && [ "$(sx jq -r '.enabled != false' /etc/vpn-setup/protocols.d/amneziawg.json)" = "true" ]; then
    conf="$(sx jq -r '.probe.conf' /etc/vpn-setup/protocols.d/amneziawg.json)"
    cx mkdir -p /etc/amnezia/amneziawg
    grep -v '^DNS' <<< "$conf" | cx sh -c 'umask 077; cat > /etc/amnezia/amneziawg/awg0.conf'
    cx sh -c "printf 'nameserver 1.1.1.1\n' > /etc/resolv.conf"
    if cx env WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/local/bin/amneziawg-go awg-quick up awg0 >/dev/null 2>&1; then
        tip="$(sx jq -r '.probe.server_tunnel_ip // empty' /etc/vpn-setup/protocols.d/amneziawg.json)"
        check_tunnel awg
        if [ -n "$tip" ]; then
            h0="$(hits)"
            cx curl -s -o /dev/null --max-time 6 "http://$tip:$C_ANY/" >/dev/null 2>&1 || true
            [ "$(hits)" = "$h0" ] && pass "[awg] $tip:$C_ANY (адрес сервера в туннеле) закрыт ufw" || fail "[awg] $tip:$C_ANY доступен из туннеля"
        fi
        cx awg-quick down awg0 >/dev/null 2>&1 || true
    else
        fail "[awg] awg-quick up не удался"
    fi
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "ИТОГ: PASS"; else echo "ИТОГ: FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
