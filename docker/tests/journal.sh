#!/usr/bin/env bash
# docker/tests/journal.sh SERVER [--keep] — журнал атак «Кто нас щупал» (zoo journal) на живом сервере стенда.
#
# Из отдельных клиентских контейнеров (у каждого свой адрес в zoo-net) делаются настоящие вещи:
#   scan — сканирование закрытых портов, мусор вместо баннера SSH, неверный ключ Hysteria2, простой TLS на 443;
#   ssh  — перебор SSH (неверные пароли до бана fail2ban);
#   own  — вход по ключу (адрес становится «своим»), затем стук в закрытые порты.
# Потом `systemctl start zoo-collector.service` (журнал атак — второй ExecStart под песочницей юнита) и проверки:
# адреса источников, виды событий, порты, бан, «свой»/«локальный» (контейнеры стенда — приватные адреса),
# страница админки, и то, чего в журнале быть не должно: паролей, имён, строк журнала, трафика VPN-интерфейсов,
# назначений пользователей в логе Xray.
#
# Ограничение стенда: LOG из netfilter сети контейнера в журнал ядра не попадает (nf_log_all_netns, замер
# 05.10.2026 на WSL2 6.6), поэтому настоящие строки «[UFW BLOCK]» стенд не отдаёт. Тест это проверяет; если
# ядро строк не дало, строки в формате ufw вбрасываются через /dev/kmsg с адресом сканера — проверяется
# тракт «журнал ядра → коллектор → база», но не само появление строки ядром (в выводе помечено «вброшено»).
# Код возврата 0 — всё прошло.

# pass/fail возвращают 0: «A && pass || fail» безопасно; $ в одинарных кавычках — для сервера
# shellcheck disable=SC2015,SC2016

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: journal.sh SERVER [--keep]}"
KEEP="${2:-}"
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
SCAN="${SRV}-scan"
SSHC="${SRV}-ssh"
OWN="${SRV}-own"
BAD_USER="jrbaduser$$"
BAD_PASS="jr-wrong-pass-$$"
BAD_TOKEN="jr-wrong-token-$$"

FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
info() { echo "      $*"; }

sx() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
ip_of() { docker inspect -f '{{(index .NetworkSettings.Networks "zoo-net").IPAddress}}' "$1"; }

AK_SAVED=0
# адреса контейнеров стенда переиспользуются: следы прошлых прогонов («свой» адрес, события) убираем до и после
forget_ips() {
    [ -n "${S1:-}" ] || return 0
    sx sqlite3 /var/lib/vpn-zoo/journal.sqlite "DELETE FROM own WHERE ip IN ('$S1','$S2','$S3'); DELETE FROM hits WHERE ip IN ('$S1','$S2','$S3'); DELETE FROM ips WHERE ip IN ('$S1','$S2','$S3')" >/dev/null 2>&1 || true
}
cleanup() {
    forget_ips
    sx fail2ban-client unban --all >/dev/null 2>&1 || true
    if [ "$AK_SAVED" = "1" ]; then
        sx bash -c 'rm -f /root/.ssh/authorized_keys; [ -f /root/.ssh/authorized_keys.zoo-jr ] && mv -f /root/.ssh/authorized_keys.zoo-jr /root/.ssh/authorized_keys || true' >/dev/null 2>&1 || true
    else
        sx bash -c 'sed -i "/zoo-jr-own/d" /root/.ssh/authorized_keys 2>/dev/null || true' >/dev/null 2>&1 || true
    fi
    [ "$KEEP" = "--keep" ] && { info "клиенты оставлены: $SCAN $SSHC $OWN"; return 0; }
    docker rm -f "$SCAN" "$SSHC" "$OWN" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null || { echo "нет контейнера $SRV" >&2; exit 2; }
sx test -x /usr/local/bin/zoo || { echo "на $SRV нет zoo (фаза 09)" >&2; exit 2; }
SIP="$(ip_of "$SRV")"
IMG="$(docker inspect -f '{{.Config.Image}}' "$SRV")"
WAN="$(sx bash -c "ip route show default | awk '{print \$5; exit}'" | tr -d '\r')"
SSH_PORT="$(sx bash -c '. /etc/vpn-setup/config.env; p="${SSH_LOGIN_PORT:-${SSH_PORTS%%,*}}"; echo "${p:-22}"' | tr -d '\r')"
echo "сервер $SRV ($SIP), внешний интерфейс $WAN, SSH-порт $SSH_PORT"

# ---------- 0. подготовка ----------
if sx bash -c 'ufw status verbose | grep -q "^Logging: on"'; then pass "ufw: лог блокировок включён"; else fail "ufw: лог блокировок выключен"; fi
if [ "$(sx bash -c 'ufw status verbose | grep -c "^Status: active"')" = "1" ]; then pass "ufw активен"; else fail "ufw не активен"; fi
sx test -f /usr/local/lib/vpn-zoo/hy2-auth && HY=1 || HY=0
if [ "$HY" = "1" ]; then
    sx grep -q 'zoo-hy2-auth' /usr/local/lib/vpn-zoo/hy2-auth && pass "помощник hy2-auth пишет отказы в журнал" \
        || fail "hy2-auth без записи отказов (--phase 05 обновит помощника)"
    HY_PORT="$(sx jq -r '.port' /etc/vpn-setup/protocols.d/hysteria2.json | tr -d '\r')"
fi

# исходные ключи root откладываем: заводим свой для клиента «own»; возвращаем в cleanup
sx bash -c 'install -d -m 700 /root/.ssh; if [ -f /root/.ssh/authorized_keys ]; then mv -f /root/.ssh/authorized_keys /root/.ssh/authorized_keys.zoo-jr; fi; : > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys'
AK_SAVED=1

mkc() {
    docker rm -f "$1" >/dev/null 2>&1 || true
    docker run -d --name "$1" --hostname "$1" --label zoo.harness=1 --label zoo.role=journal-test \
        --network zoo-net --entrypoint sleep "$IMG" infinity >/dev/null
}
mkc "$SCAN"; mkc "$SSHC"; mkc "$OWN"
S1="$(ip_of "$SCAN")"; S2="$(ip_of "$SSHC")"; S3="$(ip_of "$OWN")"
info "клиенты: scan=$S1 ssh=$S2 own=$S3"
# базовый разбор: база и курсоры есть, накопленное до теста не мешает
sx zoo journal --collect >/dev/null || fail "zoo journal --collect (базовый) не удался"
forget_ips
# fail2ban не должен прятать тестовые адреса: ignoreip от других тестов (ssh-harden) к ним не относится
sx bash -c 'grep -l "^ignoreip" /etc/fail2ban/jail.d/*.local 2>/dev/null | xargs -r grep -H "^ignoreip" || true' | sed 's/^/      /'

if [ "$HY" = "1" ]; then
    sx cat /usr/local/bin/hysteria | docker exec -i "$SCAN" bash -c 'cat > /usr/local/bin/hysteria; chmod +x /usr/local/bin/hysteria'
fi

# ---------- 1. «свой» адрес: вход по ключу ----------
docker exec "$OWN" bash -c 'ssh-keygen -q -t ed25519 -N "" -C zoo-jr-own -f /root/.ssh/id_own 2>/dev/null || { mkdir -p /root/.ssh; ssh-keygen -q -t ed25519 -N "" -C zoo-jr-own -f /root/.ssh/id_own; }'
docker exec "$OWN" cat /root/.ssh/id_own.pub | sx bash -c 'cat >> /root/.ssh/authorized_keys'
if docker exec "$OWN" ssh -p "$SSH_PORT" -i /root/.ssh/id_own -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "root@$SIP" 'echo ok' 2>/dev/null | grep -q '^ok'; then
    pass "вход по ключу с own ($S3)"
else
    fail "вход по ключу с own не удался — проверка «свой адрес» невозможна"
fi

# ---------- 2. сканирование: scan ----------
T0="$(sx date +%s | tr -d '\r')"
info "сканирование закрытых портов с $S1"
PORTS="81 82 83 84 85 3389 5900 8081 8888 9999 23 25"
docker exec "$SCAN" bash -c "for p in $PORTS; do (timeout 0.4 bash -c \"echo > /dev/tcp/$SIP/\$p\" 2>/dev/null &) ; done; sleep 1; for p in 53 161 5060; do echo x > /dev/udp/$SIP/\$p; done" || true
docker exec "$OWN" bash -c "for p in 7001 7002 7003; do (timeout 0.4 bash -c \"echo > /dev/tcp/$SIP/\$p\" 2>/dev/null &) ; done; sleep 1" || true
sleep 1
# настоящие строки ядра с адресом сканера?
real="$(sx bash -c "journalctl _TRANSPORT=kernel --since @$T0 --no-pager -o cat | grep -c 'SRC=$S1 ' || true" | tr -d '\r')"
MARK=""
if [ "${real:-0}" -gt 0 ]; then
    pass "ядро записало блокировки ufw с адреса сканера ($real строк)"
else
    MARK=" (вброшено)"
    info "ядро стенда не отдало строк «[UFW BLOCK]» (LOG из сети контейнера) — вбрасываю в формате ufw через /dev/kmsg"
    for p in 3389 5900 8081 8888 9999; do
        sx bash -c "echo '<4>[UFW BLOCK] IN=$WAN OUT= MAC=02:42:ac:16:00:02:02:42:ac:16:00:04:08:00 SRC=$S1 DST=$SIP LEN=44 TOS=0x00 PREC=0x00 TTL=64 ID=1 PROTO=TCP SPT=40000 DPT=$p WINDOW=1024 RES=0x00 SYN URGP=0' > /dev/kmsg"
    done
    sx bash -c "echo '<4>[UFW BLOCK] IN=$WAN OUT= MAC=02:42:ac:16:00:02:02:42:ac:16:00:04:08:00 SRC=$S3 DST=$SIP LEN=44 TOS=0x00 PREC=0x00 TTL=64 ID=1 PROTO=TCP SPT=40000 DPT=7001 WINDOW=1024 RES=0x00 SYN URGP=0' > /dev/kmsg"
fi
# трафик VPN-интерфейса не должен попасть в журнал при любом источнике: вбрасываем такую строку всегда
sx bash -c "echo '<4>[UFW BLOCK] IN=awg0 OUT= MAC= SRC=10.66.66.77 DST=$SIP LEN=44 TOS=0x00 PREC=0x00 TTL=64 ID=1 PROTO=TCP SPT=40000 DPT=4444 WINDOW=1024 RES=0x00 SYN URGP=0' > /dev/kmsg"

# мусор вместо баннера SSH и простой TLS на 443 (REALITY)
docker exec "$SCAN" bash -c "printf 'GET / HTTP/1.0\r\n\r\n' | timeout 4 bash -c 'cat > /dev/tcp/$SIP/$SSH_PORT'" 2>/dev/null || true
sleep 7
docker exec "$SCAN" bash -c "curl -sk -m 6 -o /dev/null https://$SIP:443/; echo hi | timeout 3 bash -c 'cat > /dev/tcp/$SIP/443'" 2>/dev/null || true

# неверный ключ Hysteria2
if [ "$HY" = "1" ]; then
    docker exec "$SCAN" bash -c "cat > /tmp/hy-bad.json <<EOF
{\"server\":\"$SIP:$HY_PORT\",\"auth\":\"$BAD_TOKEN\",\"tls\":{\"sni\":\"bing.com\",\"insecure\":true},\"socks5\":{\"listen\":\"127.0.0.1:1081\"}}
EOF
HYSTERIA_DISABLE_UPDATE_CHECK=1 timeout 8 hysteria client -c /tmp/hy-bad.json >/tmp/hy-bad.log 2>&1 || true"
    if docker exec "$SCAN" grep -q 'authentication error' /tmp/hy-bad.log; then pass "Hysteria отвергла неверный ключ"; else info "клиент Hysteria: $(docker exec "$SCAN" tail -1 /tmp/hy-bad.log | cut -c1-160)"; fi
fi

# ---------- 3. перебор SSH: ssh ----------
info "перебор SSH с $S2 (до бана fail2ban)"
docker exec "$SSHC" bash -c "printf '#!/bin/sh\necho $BAD_PASS\n' > /tmp/ap; chmod +x /tmp/ap"
for i in 1 2 3 4 5 6; do
    u=root; [ $((i % 2)) = 0 ] && u="$BAD_USER"
    docker exec "$SSHC" bash -c "SSH_ASKPASS=/tmp/ap SSH_ASKPASS_REQUIRE=force setsid ssh -p $SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive -o NumberOfPasswordPrompts=1 -o ConnectTimeout=5 $u@$SIP true </dev/null >/dev/null 2>&1" || true
done
for _ in $(seq 1 15); do
    sx bash -c "fail2ban-client status sshd 2>/dev/null | grep -q '$S2'" && break
    sleep 1
done
if sx bash -c "fail2ban-client status sshd | grep -q '$S2'"; then pass "fail2ban забанил $S2"; else fail "fail2ban не забанил $S2 (проверка бана в журнале невозможна)"; fi

# ---------- 4. разбор коллектором под песочницей ----------
sleep 2
info "systemctl start zoo-collector.service (трафик + журнал атак под песочницей юнита)"
sx systemctl start zoo-collector.service || fail "zoo-collector.service завершился с ошибкой"
sx systemctl show zoo-collector.service -p ExecStart | grep -q 'journal --collect' && pass "юнит запускает zoo journal --collect" || fail "в юните нет ExecStart=…zoo journal --collect"
run="$(sx sqlite3 /var/lib/vpn-zoo/journal.sqlite 'SELECT ok, events, lines, duration, errors FROM runs ORDER BY ts DESC LIMIT 1' | tr -d '\r')"
info "последний разбор (ok|событий|строк|с|ошибки): $run"
[ "${run%%|*}" = "1" ] && pass "разбор без ошибок источников" || fail "разбор с ошибками: $run"

J="$(sx zoo journal --all --json --period 24h --top 50)"
jq_() { printf '%s' "$J" | sx jq "$@"; }
kinds_of() { jq_ -r --arg ip "$1" '[.top_ips[] | select(.ip == $ip) | .kinds | keys[]] | sort | join(",")' | tr -d '\r'; }

# ---------- 5. проверки ----------
k1="$(kinds_of "$S1")"
info "scan ($S1): $k1"
case ",$k1," in *,port-scan,*) pass "закрытые порты: $S1 в журнале$MARK" ;; *) fail "закрытые порты: $S1 нет в журнале" ;; esac
case ",$k1," in *,ssh-scan,*) pass "мусор вместо баннера SSH: $S1 в журнале" ;; *) fail "сканер SSH: $S1 нет в журнале ($k1)" ;; esac
if [ "$HY" = "1" ]; then
    case ",$k1," in *,hy2-auth,*) pass "неверный ключ Hysteria2: $S1 в журнале" ;; *) fail "Hysteria2: $S1 нет в журнале ($k1)" ;; esac
fi
if jq_ -e --arg ip "$S1" '.top_ips[] | select(.ip == $ip) | (.ports | index(3389)) != null and (.ports | index(5900)) != null' >/dev/null; then
    pass "порты назначения сохранены (3389, 5900)"
else
    fail "порты назначения не сохранены: $(jq_ -c --arg ip "$S1" '.top_ips[] | select(.ip == $ip) | .ports')"
fi

k2="$(kinds_of "$S2")"
info "ssh ($S2): $k2"
case ",$k2," in *,ssh-auth,*) pass "перебор SSH: $S2 в журнале" ;; *) fail "перебор SSH: $S2 нет в журнале" ;; esac
n2="$(jq_ -r --arg ip "$S2" '.top_ips[] | select(.ip == $ip) | .kinds["ssh-auth"] // 0' | tr -d '\r')"
[ "${n2:-0}" -ge 4 ] && pass "попыток SSH учтено $n2 (по подключению, а не по строке журнала)" || fail "попыток SSH учтено ${n2:-0}, ожидалось 4 и больше"
[ "${n2:-0}" -le 6 ] && pass "одно подключение — одно событие (не больше 6 при 6 попытках)" || fail "попыток учтено $n2 при 6 подключениях — строки одной попытки не склеены"
if jq_ -e --arg ip "$S2" '.top_ips[] | select(.ip == $ip) | .bans >= 1' >/dev/null; then pass "бан fail2ban привязан к $S2"; else fail "бана $S2 нет в журнале"; fi

if jq_ -e --arg ip "$S3" '.top_ips[] | select(.ip == $ip) | .scope == "own"' >/dev/null; then
    pass "адрес со входом по ключу ($S3) помечен «свой»"
else
    fail "адрес $S3 не помечен «свой»: $(jq_ -c --arg ip "$S3" '[.top_ips[] | select(.ip == $ip)]')"
fi
for ip in "$S1" "$S2"; do
    jq_ -e --arg ip "$ip" '.top_ips[] | select(.ip == $ip) | .scope == "local"' >/dev/null \
        && pass "контейнер стенда $ip помечен «локальный»" || fail "$ip не помечен «локальный»"
done

D="$(sx zoo journal --json --period 24h --top 50)"
if printf '%s' "$D" | sx jq -e --arg a "$S1" --arg b "$S2" --arg c "$S3" '([.top_ips[].ip] | index($a) == null and index($b) == null and index($c) == null) and .hidden.local > 0 and .hidden.own > 0' >/dev/null; then
    pass "по умолчанию локальные и свои скрыты (скрыто: $(printf '%s' "$D" | sx jq -c .hidden | tr -d '\r'))"
else
    fail "по умолчанию тестовые адреса не скрыты: $(printf '%s' "$D" | sx jq -c '{hidden, ips: [.top_ips[].ip]}')"
fi
sx zoo journal --all --period 24h | grep -q "$S1" && pass "zoo journal --all (текст) показывает $S1" || fail "zoo journal --all без $S1"

# ---------- 6. чего быть не должно ----------
dump="$(sx python3 -c 'import sqlite3; print("\n".join(sqlite3.connect("/var/lib/vpn-zoo/journal.sqlite").iterdump()))')"
leaks=""
for s in "$BAD_USER" "$BAD_PASS" "$BAD_TOKEN" "Invalid user" "Failed password"; do
    case "$dump" in *"$s"*) leaks="$leaks [$s]" ;; esac
done
[ -z "$leaks" ] && pass "в базе журнала нет паролей, имён и строк журнала" || fail "в базе журнала найдено:$leaks"
if sx bash -c "journalctl -t zoo-hy2-auth --no-pager -o cat | grep -q '$BAD_TOKEN'"; then fail "неверный ключ Hysteria попал в журнал"; else pass "ключ из отказа Hysteria в журнал не пишется (только адрес)"; fi
vpn="$(sx sqlite3 /var/lib/vpn-zoo/journal.sqlite "SELECT COUNT(*) FROM hits WHERE ip = '10.66.66.77' OR port = 4444" | tr -d '\r')"
[ "${vpn:-1}" = "0" ] && pass "пакет с VPN-интерфейса awg0 в журнал не попал (трафик пользователей не пишется)" || fail "в журнале есть пакет с VPN-интерфейса ($vpn)"
lvl="$(sx jq -r '.log.loglevel + "/" + .log.access' /usr/local/x-ui/bin/config.json | tr -d '\r')"
dest="$(sx bash -c "journalctl -u x-ui --no-pager -o cat | grep -cE 'default route for|dialing to|taking detour|accepted tcp' || true" | tr -d '\r')"
[ "$lvl" = "warning/none" ] && [ "${dest:-1}" = "0" ] \
    && pass "Xray: loglevel/access = $lvl, назначений пользователей в журнале нет (REALITY-пробы не пишем, D32)" \
    || fail "Xray: $lvl, строк с назначениями: $dest"
if sx sqlite3 /var/lib/vpn-zoo/journal.sqlite "SELECT 1 FROM hits WHERE kind = 'reality-probe' AND ip = '$S1'" | grep -q 1; then
    info "REALITY-проба $S1 всё же записана (уровень Xray выше warning — проверьте приватность)"
else
    info "REALITY-проба $S1 не видна на уровне warning — так и задумано (D32)"
fi
exp="$(sx bash -c "journalctl _TRANSPORT=kernel --since @$T0 --no-pager -g '\[UFW ' -o export > /tmp/jr-export; grep -a -c -F 'MESSAGE=[UFW' /tmp/jr-export || true; wc -c < /tmp/jr-export; rm -f /tmp/jr-export" | tr -d '\r' | paste -sd' ')"
info "журнал ядра за тест (строк «[UFW …]», байт в формате export): $exp"

# ---------- 7. страница админки ----------
page="$(sx bash -c '. /etc/vpn-setup/config.env
B=http://127.0.0.1:$ZOO_WEB_PORT
cj=$(mktemp)
curl -s -m 10 -c "$cj" "$B/login" -o /tmp/jr-login.html
lc=$(grep -o "name=\"lc\" value=\"[^\"]*\"" /tmp/jr-login.html | sed "s/.*value=\"//;s/\"//")
curl -s -m 10 -b "$cj" -c "$cj" -d "lc=$lc&token=$ZOO_WEB_TOKEN&next=/" "$B/login" -o /dev/null
curl -s -m 10 -b "$cj" "$B/journal?period=24h"
rm -f "$cj" /tmp/jr-login.html')"
if grep -q 'не учтены: ваши входы и служебные адреса' <<< "$page" && grep -q 'Стучались в закрытые порты' <<< "$page" && grep -q 'Перебор SSH' <<< "$page"; then
    pass "страница «Атаки»: группы и строка «не учтены» на месте"
else
    fail "страница «Атаки» без ожидаемого содержимого"
fi
if grep -qE '(src|href|action)="https?://' <<< "$page"; then fail "страница «Атаки» ссылается наружу"; else pass "страница «Атаки» ничего внешнего не подгружает"; fi

echo
[ "$FAILS" -eq 0 ] && echo "journal: всё прошло" || echo "journal: провалов $FAILS"
[ "$FAILS" -eq 0 ]
