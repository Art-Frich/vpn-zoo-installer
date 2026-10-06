#!/usr/bin/env bash
# docker/tests/live.sh — живые метрики карточек (zoo-live.*) и вкл/выкл протокола с карточки
# (zoo-job.*) под настоящим systemd и через настоящую админку (её песочница).
#
#   docker/tests/live.sh SERVER
#
# Запускать после установки с фазой 09 (лучше после тестов протоколов). Проверки:
#   - юниты zoo-live.timer, zoo-live.path, zoo-job.path включены и ждут; у zoo-live.service Nice=10, CPUQuota=30%;
#   - `systemctl start zoo-live.service`: у каждого включённого протокола строка в таблице live — задержка,
#     джиттер и (в первый раз) скорость; повторный запуск скорость не меряет; цифры нагрузки (время, CPU,
#     байты на eth0) печатаются как есть;
#   - ↻: POST /live/<протокол> → live-req → zoo-live.path → zoo-live-req.service → новая строка, заявка забрана;
#     повторный POST в первую минуту отклонён;
#   - вкл/выкл: POST /protocols/<id>/disable|enable → jobs → zoo-job.path → zoo-job.service: ENABLE_* в config.env,
#     манифест enabled=false/true, правило ufw снято/возвращено, ссылки и пользователи на месте; защита
#     «последний включённый протокол» (на копии набора).
# LIVE_TOGGLE_PROTO (по умолчанию tuic) — какой протокол выключать и включать. Код возврата 0 — всё прошло.

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: live.sh SERVER}"
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
TOGGLE="${LIVE_TOGGLE_PROTO:-tuic}"

fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
info() { echo "....  $*"; }
srv() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }

HIST=/var/lib/vpn-zoo/probe-history.sqlite
sql() {
    srv python3 -c 'import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for row in con.execute(sys.argv[2]):
    print("\t".join("" if v is None else str(v) for v in row))
con.commit()' "$HIST" "$1"
}
# до $2 секунд ждём, пока выражение $1 (выполняется здесь, умеет sql и srv) станет истинным
wait_for() {
    local i=0
    while [ "$i" -lt "${2:-90}" ]; do
        if eval "$1" >/dev/null 2>&1; then return 0; fi
        sleep 2; i=$((i + 2))
    done
    return 1
}
netb() { srv bash -c 'cat /sys/class/net/eth0/statistics/rx_bytes /sys/class/net/eth0/statistics/tx_bytes | tr "\n" " "' | tr -d '\r'; }
now_ms() { date +%s%3N; }

docker container inspect "$SRV" >/dev/null || { echo "нет контейнера $SRV" >&2; exit 2; }
srv test -x /usr/local/bin/zoo || { echo "на $SRV нет zoo (фаза 09)" >&2; exit 2; }

protos="$(srv bash -c 'cat /etc/vpn-setup/protocols.d/*.json' \
    | srv jq -r 'select(.enabled != false and .users_backend != "none") | .id' | tr -d '\r')"

# ---------- 1. юниты ----------
for u in zoo-live.timer zoo-live.path zoo-job.path; do
    en="$(srv systemctl is-enabled "$u" 2>&1 | tr -d '\r')"; ac="$(srv systemctl is-active "$u" 2>&1 | tr -d '\r')"
    if [ "$en" = enabled ] && [ "$ac" = active ]; then pass "$u включён и ждёт"; else fail "$u: $en / $ac"; fi
done
lim="$(srv systemctl show zoo-live.service -p Nice -p CPUQuotaPerSecUSec | tr '\n' ' ' | tr -d '\r')"
case "$lim" in
    *Nice=10*CPUQuotaPerSecUSec=300ms*|*CPUQuotaPerSecUSec=300ms*Nice=10*) pass "zoo-live.service: $lim" ;;
    *) fail "zoo-live.service: нет Nice=10 / CPUQuota=30% ($lim)" ;;
esac

# ---------- 2. замер по таймеру ----------
sql "DELETE FROM live; DELETE FROM meta WHERE key LIKE 'live_speed:%'" >/dev/null 2>&1 \
    || { sql "DELETE FROM live" >/dev/null; sql "DELETE FROM meta WHERE key LIKE 'live_speed:%'" >/dev/null; }
read -r -a n0 <<< "$(netb)"; t0="$(now_ms)"
srv systemctl start zoo-live.service
t1="$(now_ms)"; read -r -a n1 <<< "$(netb)"
cpu="$(srv systemctl show zoo-live.service -p CPUUsageNSec --value | tr -d '\r')"
info "прогон со скоростью: $((t1 - t0)) мс, CPU $((cpu / 1000000)) мс, eth0 rx +$((n1[0] - n0[0])) tx +$((n1[1] - n0[1])) Б, протоколов: $(wc -w <<< "$protos")"
for p in $protos; do
    row="$(sql "SELECT ok, COALESCE(rtt_ms, ''), COALESCE(jitter_ms, ''), COALESCE(mbps, ''), bytes, verdict FROM live WHERE proto = '$p' ORDER BY ts DESC LIMIT 1")"
    if [ -z "$row" ]; then fail "$p: нет строки в live"; continue; fi
    IFS=$'\t' read -r ok rtt jit mbps nbytes verdict <<< "$row"
    if [ "$ok" = 1 ] && [ -n "$rtt" ] && [ -n "$mbps" ] && [ "$nbytes" -gt 0 ]; then
        pass "$p: $verdict, задержка $rtt мс, джиттер ${jit:-—} мс, скорость $mbps Мбит/с, скачано $nbytes Б"
    else
        fail "$p: ожидалась задержка и скорость в первом прогоне: $row"
    fi
done
read -r -a n0 <<< "$(netb)"; t0="$(now_ms)"
srv systemctl start zoo-live.service
t1="$(now_ms)"; read -r -a n1 <<< "$(netb)"
cpu="$(srv systemctl show zoo-live.service -p CPUUsageNSec --value | tr -d '\r')"
info "лёгкий прогон: $((t1 - t0)) мс, CPU $((cpu / 1000000)) мс, eth0 rx +$((n1[0] - n0[0])) tx +$((n1[1] - n0[1])) Б"
count="$(wc -w <<< "$protos")"
light="$(sql "SELECT COUNT(*) FROM (SELECT proto FROM live GROUP BY proto HAVING COUNT(*) = 2 AND SUM(mbps IS NULL) = 1)")"
if [ "$light" = "$count" ]; then
    pass "повторный запуск: у каждого протокола второй замер без скорости (раз в 55 мин)"
else
    fail "повторный запуск: без скорости только у $light из $count"
fi

# ---------- 3. админка: ↻ ----------
info_json="$(srv zoo web --info --json | tr -d '\r')"
URL="$(srv jq -r .url <<< "$info_json")"; URL="${URL%/}"; TOKEN="$(srv jq -r .token <<< "$info_json")"
PORT="${URL##*:}"
curlw() { srv curl -sS -o /tmp/lv.body -D /tmp/lv.head -w '%{http_code}' -b /tmp/lv.jar -c /tmp/lv.jar "$@"; }
csrf() { srv bash -c "grep -o 'name=\"csrf\" value=\"[^\"]*\"' /tmp/lv.body | head -1 | sed 's/.*value=\"//; s/\"\$//'" | tr -d '\r'; }
srv rm -f /tmp/lv.jar
curlw "$URL/login" >/dev/null
lc="$(srv awk -v n="zoo_login_$PORT" '$6 == n {print $7}' /tmp/lv.jar | tr -d '\r')"
c="$(curlw "$URL/login" --data-urlencode "token=$TOKEN" -d "lc=$lc")"
if [ "$c" = 303 ]; then pass "вход в админку"; else fail "вход в админку: $c"; fi
curlw "$URL/" >/dev/null
CSRF="$(csrf)"
if srv grep -q "metrics" /tmp/lv.body && srv grep -q "/live/$TOGGLE" /tmp/lv.body; then
    pass "обзор: у карточек строка метрик и ↻"
else
    fail "обзор: нет строки метрик или ↻ у $TOGGLE"
fi

if ! grep -qx "$TOGGLE" <<< "$protos"; then
    info "протокол $TOGGLE не включён на стенде — остальное пропущено"
else
    # rate-limit 60 с на протокол: дождаться, чтобы последний замер состарился
    wait_for "[ \"\$(sql \"SELECT CAST(strftime('%s','now') AS INTEGER) - MAX(ts) FROM live WHERE proto = '$TOGGLE'\")\" -gt 61 ]" 90 || true
    before="$(sql "SELECT COUNT(*) FROM live WHERE proto = '$TOGGLE'")"
    c="$(curlw "$URL/live/$TOGGLE" -d "csrf=$CSRF")"
    if [ "$c" = 303 ]; then pass "POST /live/$TOGGLE → 303 (заявка пишется из песочницы админки)"; else fail "POST /live/$TOGGLE → $c"; fi
    if wait_for "[ \"\$(sql \"SELECT COUNT(*) FROM live WHERE proto = '$TOGGLE'\")\" -gt $before ] && [ -z \"\$(srv ls /var/lib/vpn-zoo/live-req | tr -d '\r')\" ]" 120; then
        pass "заявка ↻ исполнена: новый замер $TOGGLE, файл заявки забран"
    else
        fail "заявка ↻ не исполнена за 120 с: [$(srv ls /var/lib/vpn-zoo/live-req | tr '\n' ' ')] zoo-live-req: $(srv systemctl is-active zoo-live-req.service | tr -d '\r')"
    fi
    curlw "$URL/live/$TOGGLE" -d "csrf=$CSRF" >/dev/null
    if [ -z "$(srv ls /var/lib/vpn-zoo/live-req | tr -d '\r')" ]; then pass "повторный ↻ в первую минуту отклонён"; else fail "повторный ↻ принят"; fi
    c="$(curlw "$URL/live/$TOGGLE" -d "csrf=bad")"
    if [ "$c" = 403 ]; then pass "POST без верного CSRF → 403"; else fail "POST /live без CSRF → $c"; fi

    # ---------- 4. вкл/выкл ----------
    mf="/etc/vpn-setup/protocols.d/$TOGGLE.json"
    port="$(srv jq -r .port "$mf" | tr -d '\r')"; layer="$(srv jq -r .layer "$mf" | tr -d '\r')"
    var="$(srv jq -r '.enable_var // empty' "$mf" | tr -d '\r')"
    [ -n "$var" ] && pass "манифест $TOGGLE несёт enable_var=$var, phase=$(srv jq -r .phase "$mf" | tr -d '\r')" || fail "в манифесте $TOGGLE нет enable_var"
    links_before="$(srv jq -r '[.links[] | select(.enabled != false)] | length' "$mf" | tr -d '\r')"
    users_before="$(srv zoo user list --json | srv jq -r '.users | length' | tr -d '\r')"
    ufw_rule() { srv bash -c "ufw status | grep -Eq '^$port/$layer '"; }
    ufw_rule && pass "до выключения: правило ufw $port/$layer есть" || fail "до выключения нет правила ufw $port/$layer"

    c="$(curlw "$URL/protocols/$TOGGLE/disable" -d "csrf=$CSRF")"
    if [ "$c" = 303 ]; then pass "POST /protocols/$TOGGLE/disable → 303"; else fail "disable → $c"; fi
    curlw "$URL/" >/dev/null
    if srv grep -q "выключается\|Выключение" /tmp/lv.body; then pass "обзор показывает, что выключение идёт"; else info "обзор уже без «выключается…» (задача успела)"; fi
    if wait_for "[ \"\$(srv jq -r .enabled $mf | tr -d '\r')\" = false ] && [ -z \"\$(srv bash -c 'ls /var/lib/vpn-zoo/jobs/*.json 2>/dev/null' | tr -d '\r')\" ]" 600; then
        pass "$TOGGLE выключен: манифест enabled=false, заявка выполнена"
    else
        fail "$TOGGLE не выключился за 600 с"
    fi
    st="$(srv bash -c 'cd /var/lib/vpn-zoo/jobs/state && for f in $(ls -t *.json); do jq -c "{proto,action,status,rc}" $f; done' | tr -d '\r')"
    if grep -q '"proto":"'"$TOGGLE"'","action":"disable","status":"ok"' <<< "$st"; then pass "задача выключения: ok"; else fail "задачи: $st"; fi
    if srv grep -q "^$var='0'" /etc/vpn-setup/config.env; then pass "config.env: $var=0"; else fail "config.env: нет $var=0"; fi
    if ufw_rule; then fail "правило ufw $port/$layer осталось после выключения"; else pass "правило ufw $port/$layer снято"; fi
    curlw "$URL/" >/dev/null
    if srv grep -q "protocols/$TOGGLE/enable" /tmp/lv.body; then pass "обзор: теперь предлагает «Включить»"; else fail "на обзоре нет кнопки «Включить» для $TOGGLE"; fi

    c="$(curlw "$URL/protocols/$TOGGLE/enable" -d "csrf=$CSRF")"
    if [ "$c" = 303 ]; then pass "POST /protocols/$TOGGLE/enable → 303"; else fail "enable → $c"; fi
    if wait_for "[ \"\$(srv jq -r .enabled $mf | tr -d '\r')\" = true ] && [ -z \"\$(srv bash -c 'ls /var/lib/vpn-zoo/jobs/*.json 2>/dev/null' | tr -d '\r')\" ]" 900; then
        pass "$TOGGLE включён: манифест enabled=true"
    else
        fail "$TOGGLE не включился за 900 с"
    fi
    links_after="$(srv jq -r '[.links[] | select(.enabled != false)] | length' "$mf" | tr -d '\r')"
    if [ "$links_after" = "$links_before" ]; then pass "ссылки вернулись: $links_after"; else fail "ссылок было $links_before, стало $links_after"; fi
    if ufw_rule; then pass "правило ufw $port/$layer вернулось"; else fail "правило ufw $port/$layer не вернулось"; fi
    if [ "$(srv zoo user list --json | srv jq -r '.users | length' | tr -d '\r')" = "$users_before" ]; then
        pass "пользователи на месте ($users_before)"
    else
        fail "пользователи изменились"
    fi
    if srv jq -e '.status == "ok"' "$(srv bash -c 'ls -t /var/lib/vpn-zoo/jobs/state/*.json | head -1' | tr -d '\r')" >/dev/null; then
        pass "задача включения: ok"
    else
        fail "задача включения не ok"
    fi
fi

# ---------- 5. последний включённый протокол ----------
if srv python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/vpn-zoo/zoo")
from zoolib import protoctl
c = protoctl.controls()
on = [k for k, v in c.items() if v.enabled]
for k in on[1:]:
    c[k].enabled = False
try:
    protoctl.check(on[0], "disable", c)
except protoctl.JobError:
    sys.exit(0)
sys.exit(1)
PY
then pass "защита: последний включённый протокол не выключается"; else fail "защита последнего протокола не сработала"; fi

echo
if [ "$fails" -eq 0 ]; then echo "live: всё прошло"; else echo "live: провалов $fails"; fi
[ "$fails" -eq 0 ]
