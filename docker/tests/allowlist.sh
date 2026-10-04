#!/usr/bin/env bash
# docker/tests/allowlist.sh — приложения через VPN (D31): реестр zoo allow, Android-вариант
# конфига AmneziaWG и правила v2rayN.
#
#   docker/tests/allowlist.sh SERVER
#
# Проверки: реестр создан (0600) с пресетом; у owner amneziawg-android.conf с
# IncludedApplications в [Interface], а в общем amneziawg.conf ключа нет; манифест и zoo links
# отдают оба файла и правила v2rayN; zoo allow add/del пересобирает файлы; свой список
# пользователя и его забывание при удалении; общий .conf по-прежнему понимает awg-quick.
# Затем путь обновления (zoo setup без реестра и файлов). В конце общий список — пресет. Код возврата 0 — всё прошло.

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: allowlist.sh SERVER}"
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac

fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
info() { echo "....  $*"; }
srv() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }

C=/etc/vpn-setup/clients
REG=/etc/vpn-setup/allowlist.json
U="zal$RANDOM"

docker container inspect "$SRV" >/dev/null || { echo "нет контейнера $SRV" >&2; exit 2; }
srv test -x /usr/local/bin/zoo || { echo "на $SRV нет zoo (фаза 09)" >&2; exit 2; }
srv test -f /etc/vpn-setup/protocols.d/amneziawg.json || { echo "на $SRV нет AmneziaWG" >&2; exit 2; }

# iface FILE — секция [Interface] конфига; has_apps FILE «a, b» — строка IncludedApplications в ней
iface() { srv awk '/^\[Peer\]/{exit} {print}' "$1"; }
apps_of() { iface "$1" | sed -n 's/^IncludedApplications = //p' | tr -d '\r'; }
no_key() { ! srv grep -q IncludedApplications "$1"; }
jqe() { srv jq -e "$@" >/dev/null; }
v2rayn_procs() { srv jq -r '.[] | select(.outboundTag == "proxy") | .process | join(", ")' "$1" | tr -d '\r'; }

trap 'srv zoo allow reset >/dev/null 2>&1 || true; srv zoo user del "$U" --force >/dev/null 2>&1 || true' EXIT

# ---------- 1. после установки ----------
check "реестр $REG есть, 0600" test "$(srv stat -c %a "$REG" 2>/dev/null | tr -d '\r')" = 600
check "пресет: Android = Brave, Telegram" \
    test "$(srv jq -r '.android | join(", ")' "$REG" | tr -d '\r')" = "com.brave.browser, org.telegram.messenger"
check "owner: amneziawg-android.conf со списком в [Interface]" \
    test "$(apps_of "$C/owner/amneziawg-android.conf")" = "com.brave.browser, org.telegram.messenger"
check "owner: QR Android-варианта (.png)" srv test -s "$C/owner/amneziawg-android.png"
check "owner: в общем amneziawg.conf ключа нет" no_key "$C/owner/amneziawg.conf"
check "Android-вариант отличается от общего только строкой IncludedApplications" \
    srv bash -c "diff <(grep -v '^IncludedApplications' $C/owner/amneziawg-android.conf) $C/owner/amneziawg.conf >/dev/null"
check "манифест: файл Android-варианта с меткой" \
    jqe '[.files[] | select(.user == "owner" and .platform == "android" and (.path | endswith("amneziawg-android.conf")))] | length == 1' \
    /etc/vpn-setup/protocols.d/amneziawg.json
check "probe манифеста — общий .conf (без IncludedApplications)" \
    bash -c "! docker exec $SRV jq -r .probe.conf /etc/vpn-setup/protocols.d/amneziawg.json | grep -q IncludedApplications"
check "правила v2rayN owner: brave.exe, Telegram.exe → proxy" \
    test "$(v2rayn_procs "$C/owner/v2rayn-routing.json")" = "brave.exe, Telegram.exe"
check "правила v2rayN: последнее правило — всё остальное напрямую" \
    jqe '.[-1] == {"remarks": "Всё остальное напрямую", "outboundTag": "direct", "port": "0-65535"}' "$C/owner/v2rayn-routing.json"
links="$(srv zoo links owner --json)"
check "zoo links owner: оба .conf AWG и правила v2rayN" jqe \
    '([.links[] | select(.kind == "file") | .uri | split("/") | last] | sort)
     == ["amneziawg-android.conf", "amneziawg.conf", "v2rayn-routing.json"]' <<< "$links"

# ---------- 2. общий список ----------
check "zoo allow add youtube" srv zoo allow add youtube
check "owner: YouTube в Android-варианте" \
    test "$(apps_of "$C/owner/amneziawg-android.conf")" = "com.brave.browser, org.telegram.messenger, com.google.android.youtube"
check "zoo allow del telegram (оба: пакет и Telegram.exe)" srv zoo allow del telegram
check "owner: Telegram убран из Android-варианта" \
    test "$(apps_of "$C/owner/amneziawg-android.conf")" = "com.brave.browser, com.google.android.youtube"
check "owner: Telegram.exe убран из правил v2rayN" test "$(v2rayn_procs "$C/owner/v2rayn-routing.json")" = "brave.exe"
check "zoo allow add Discord.exe → правила v2rayN" srv zoo allow add Discord.exe
check "owner: Discord.exe в правилах v2rayN" test "$(v2rayn_procs "$C/owner/v2rayn-routing.json")" = "brave.exe, Discord.exe"
check "общий .conf по-прежнему без ключа" no_key "$C/owner/amneziawg.conf"
check "пустой список не принимается" bash -c "! docker exec $SRV zoo allow del brave youtube --android >/dev/null 2>&1"
check "мусор не принимается" bash -c "! docker exec $SRV zoo allow add 'evil; rm' >/dev/null 2>&1"

# ---------- 3. свой список пользователя ----------
if srv zoo user add "$U" --note "allowlist e2e" >/dev/null 2>&1; then pass "zoo user add $U"; else fail "zoo user add $U"; fi
check "$U: правила v2rayN созданы при добавлении" srv test -s "$C/$U/v2rayn-routing.json"
check "$U: Android-вариант с общим списком" \
    test "$(apps_of "$C/$U/amneziawg-android.conf")" = "com.brave.browser, com.google.android.youtube"
check "zoo allow del youtube --user $U" srv zoo allow del youtube --user "$U"
check "$U: свой список без YouTube" test "$(apps_of "$C/$U/amneziawg-android.conf")" = "com.brave.browser"
check "owner: общий список не тронут" \
    test "$(apps_of "$C/owner/amneziawg-android.conf")" = "com.brave.browser, com.google.android.youtube"
# общий .conf — то, что получает awg-quick (Linux, пробник): ключа Android там нет
srv sh -c "umask 077; mkdir -p /tmp/zal && cp $C/$U/amneziawg.conf /tmp/zal/zal0.conf"
check "awg-quick strip общего .conf: без IncludedApplications" srv bash -c \
    'PATH=/usr/local/bin:$PATH; s="$(awg-quick strip /tmp/zal/zal0.conf)" && grep -q "^PrivateKey" <<< "$s" && ! grep -q IncludedApplications <<< "$s"'
srv rm -rf /tmp/zal
check "zoo user del $U" srv zoo user del "$U"
check "свой список $U забыт" bash -c "! docker exec $SRV jq -e --arg u $U '.users | has(\$u)' $REG >/dev/null"
check "каталог $U удалён" bash -c "! docker exec $SRV test -e $C/$U"

# ---------- 4. сброс ----------
check "zoo allow reset" srv zoo allow reset
check "owner: снова пресет" test "$(apps_of "$C/owner/amneziawg-android.conf")" = "com.brave.browser, org.telegram.messenger"
check "zoo allow apply" srv zoo allow apply

# ---------- 5. обновление со старой версии ----------
# zoo upgrade перезапускает только фазу 09: zoo setup сам создаёт реестр и файлы старым пользователям
srv rm -f "$REG" "$C/owner/v2rayn-routing.json" "$C/owner/amneziawg-android.conf" "$C/owner/amneziawg-android.png"
check "zoo setup (как при zoo upgrade)" bash -c "docker exec $SRV zoo setup >/dev/null 2>&1"
check "после setup: реестр 0600" test "$(srv stat -c %a "$REG" 2>/dev/null | tr -d '\r')" = 600
check "после setup: правила v2rayN owner" test "$(v2rayn_procs "$C/owner/v2rayn-routing.json")" = "brave.exe, Telegram.exe"
check "после setup: Android-вариант owner" \
    test "$(apps_of "$C/owner/amneziawg-android.conf")" = "com.brave.browser, org.telegram.messenger"
check "после setup: манифест с Android-вариантом" \
    jqe '[.files[] | select(.user == "owner" and .platform == "android")] | length == 1' \
    /etc/vpn-setup/protocols.d/amneziawg.json
check "zoo-probe: свой список не принимается" \
    bash -c "! docker exec $SRV zoo allow del telegram --user zoo-probe >/dev/null 2>&1"

echo
if [ "$fails" -eq 0 ]; then echo "ИТОГ: allowlist e2e — OK"; else echo "ИТОГ: allowlist e2e — $fails провал(ов)"; fi
[ "$fails" -eq 0 ]
