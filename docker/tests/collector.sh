#!/usr/bin/env bash
# docker/tests/collector.sh — коллектор трафика под песочницей systemd (zoo-collector.service:
# ProtectSystem=strict, PrivateDevices, RestrictAddressFamilies) и служебный пользователь пробника.
#
#   docker/tests/collector.sh SERVER
#
# Запускать после тестов протоколов (они дают трафик). Проверки:
#   - zoo-collector.service отрабатывает без ошибок источников (3x-ui, Hysteria, AmneziaWG);
#   - у каждого включённого протокола есть серия, у AmneziaWG — счётчики peer'ов (`awg show`
#     из песочницы: для amneziawg-go это UAPI-сокет в /run/amneziawg, для модуля — netlink);
#   - приращение AWG после трафика через туннель (самопроверка кредами zoo-probe);
#   - zoo-probe скрыт из `zoo traffic` и `zoo user list`, но виден с --all.
# Код возврата 0 — всё прошло.

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: collector.sh SERVER}"
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac

fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
info() { echo "....  $*"; }
srv() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }

# SQL к базе трафика на сервере (python3 там есть: его ставит фаза 09)
sql() {
    srv python3 -c 'import sqlite3, sys
con = sqlite3.connect("/var/lib/vpn-zoo/traffic.sqlite")
for row in con.execute(sys.argv[1]):
    print("\t".join("" if v is None else str(v) for v in row))' "$1"
}

collect() {
    srv systemctl start zoo-collector.service
    sql "SELECT ok, errors FROM runs ORDER BY ts DESC LIMIT 1"
}

docker container inspect "$SRV" >/dev/null || { echo "нет контейнера $SRV" >&2; exit 2; }
srv test -x /usr/local/bin/zoo || { echo "на $SRV нет zoo (фаза 09)" >&2; exit 2; }

# ---------- 1. снятие под песочницей ----------
# Свежий трафик по всем протоколам: счётчики Hysteria обнуляются при её рестарте (routing,
# security), и без этого серия зависела бы от того, успел ли таймер сработать раньше
info "трафик по всем протоколам кредами zoo-probe (zoo probe --local)"
srv zoo probe --local --quiet >/dev/null 2>&1 || true
res="$(collect)"
if [ "${res%%$'\t'*}" = "1" ]; then pass "zoo-collector.service: без ошибок источников"; else fail "zoo-collector.service: $res"; fi
srv systemctl show zoo-collector.service -p ProtectSystem -p PrivateDevices | tr '\n' ' ' | sed 's/^/....  юнит: /'; echo

# shellcheck disable=SC2016 # glob раскрывается на сервере
protos="$(srv sh -c 'cat /etc/vpn-setup/protocols.d/*.json' \
    | srv jq -r 'select(.enabled != false and .users_backend != "none") | .id' | tr -d '\r')"
for p in $protos; do
    n="$(sql "SELECT COUNT(*) FROM counters WHERE proto = '$p'")"
    if [ "${n:-0}" -gt 0 ]; then pass "серия $p есть ($n)"; else fail "нет серии $p в counters"; fi
done

# ---------- 2. AmneziaWG: приращение под песочницей ----------
if grep -qx amneziawg <<< "$protos"; then
    before="$(sql "SELECT COALESCE(SUM(up + down), 0) FROM counters WHERE proto = 'amneziawg' AND user = 'zoo-probe'")"
    info "трафик AWG кредами zoo-probe (zoo probe --local --proto amneziawg)"
    srv zoo probe --local --proto amneziawg --quiet >/dev/null 2>&1 || true
    res="$(collect)"
    after="$(sql "SELECT COALESCE(SUM(up + down), 0) FROM counters WHERE proto = 'amneziawg' AND user = 'zoo-probe'")"
    if [ "${res%%$'\t'*}" = "1" ] && [ "${after:-0}" -gt "${before:-0}" ]; then
        pass "AWG: счётчик zoo-probe вырос под песочницей ($before → $after байт)"
    else
        fail "AWG: счётчик zoo-probe $before → $after, снятие: $res"
    fi
    engine="$(srv jq -r .engine /etc/vpn-setup/protocols.d/amneziawg.json)"
    info "движок AWG: $engine"
fi

# ---------- 3. служебный пользователь скрыт ----------
if srv zoo user list --json | srv jq -e '[.users[].name] | index("zoo-probe") == null' >/dev/null; then
    pass "zoo user list: zoo-probe скрыт"
else
    fail "zoo user list показывает zoo-probe"
fi
if srv zoo user list --all --json | srv jq -e '.users[] | select(.name == "zoo-probe" and .system == true)' >/dev/null; then
    pass "zoo user list --all: zoo-probe (system)"
else
    fail "zoo user list --all: нет zoo-probe с system=true"
fi
if srv zoo traffic --period 24h --json | srv jq -e '[.rows[].key] | index("zoo-probe") == null' >/dev/null \
        && srv zoo traffic --period 24h --all --json | srv jq -e '[.rows[].key] | index("zoo-probe") != null' >/dev/null; then
    pass "zoo traffic: zoo-probe только с --all"
else
    fail "zoo traffic: zoo-probe виден без --all или не виден с --all"
fi
if srv zoo user del zoo-probe >/dev/null 2>&1; then
    fail "zoo user del zoo-probe прошёл без --force"
else
    pass "zoo user del zoo-probe без --force — отказ"
fi
if srv zoo status --json | srv jq -e '[.problems[] | select(test("коллектор|трафик,"))] | length == 0' >/dev/null; then
    pass "zoo status: коллектор без проблем"
else
    fail "zoo status: $(srv zoo status --json | srv jq -c '[.problems[] | select(test("коллектор|трафик,"))]')"
fi

echo
[ "$fails" -eq 0 ] && echo "collector: всё прошло" || echo "collector: провалов $fails"
[ "$fails" -eq 0 ]
