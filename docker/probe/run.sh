#!/usr/bin/env bash
# run.sh SERVER OUT — клиентский пробник на стенде: напрямую и через цензора по профилям.
#
#   docker/probe/run.sh e2e-2404 docker/out/<ts>      # так вызывает docker/test.sh
#
# 1. На сервере: `zoo probe --local` (если в OUT ещё нет probe-local.json) и `zoo export-probe`.
# 2. Контейнер zoo-probe в zoo-net — профиль direct (ожидания как у clean).
# 3. Цензор (docker/censor) между отдельной сетью пробника и zoo-net; для каждого профиля —
#    свой прогон пробника и сверка вердиктов (docker/probe/expect.py).
# Итог: OUT/probe/expect.tsv, OUT/probe/<профиль>/probe-report.{json,md},
# OUT/probe-client-<профиль>.json. Код 1 — хоть один вердикт не совпал с ожиданием.
#
# Окружение: ZOO_PROBE_PROFILES (по умолчанию «direct clean drop-udp ip-block freeze-16k rst-tls»),
# ZOO_PROBE_IMAGE / ZOO_CENSOR_IMAGE (zoo-probe / zoo-censor), ZOO_PROBE_BUILD=0 — не пересобирать
# образы, ZOO_PROBE_PREFIX — префикс имён контейнеров и сети (по умолчанию zoo-probe-<pid>).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=../run-server.sh
. "$HERE/../run-server.sh"

[ $# -eq 2 ] || { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
SRV="$(zoo_name "$1")"
mkdir -p "$2"
OUT="$(cd "$2" && pwd)"
PROFILES="${ZOO_PROBE_PROFILES:-direct clean drop-udp ip-block freeze-16k rst-tls}"
IMG="${ZOO_PROBE_IMAGE:-zoo-probe}"
CIMG="${ZOO_CENSOR_IMAGE:-zoo-censor}"
PFX="${ZOO_PROBE_PREFIX:-zoo-probe-$$}"
CENSOR="$PFX-censor"
CNET="$PFX-cnet"
PDIR="$OUT/probe"
TSV="$PDIR/expect.tsv"

zoo_exists "$SRV" || zoo_die "нет контейнера $SRV"
mkdir -p "$PDIR"

cleanup() {
    docker rm -f "$CENSOR" >/dev/null 2>&1 || true
    docker network rm "$CNET" >/dev/null 2>&1 || true
    # копии пакета с ключами в каталогах профилей больше не нужны
    rm -f "$PDIR"/*/probe-export.json
}
trap cleanup EXIT

build() {
    local img="$1" file="$2" ctx="$3"
    if [ "${ZOO_PROBE_BUILD:-1}" = "1" ] || ! docker image inspect "$img" >/dev/null 2>&1; then
        zoo_log "сборка $img"
        docker build -q -f "$(zoo_host_path "$file")" -t "$img" "$(zoo_host_path "$ctx")" >/dev/null
    fi
}
build "$IMG" "$ZOO_REPO_ROOT/docker/probe.Dockerfile" "$ZOO_REPO_ROOT"
build "$CIMG" "$ZOO_REPO_ROOT/docker/censor/censor.Dockerfile" "$ZOO_REPO_ROOT/docker/censor"

# zoo на сервере: установленный фазой 09 или прямо из репо
ZOO="$(docker exec "$SRV" sh -c 'command -v zoo || echo /repo/zoo/zoo')"
srv_zoo() { docker exec -e ZOO_TEST_ENV=docker "$SRV" python3 "$ZOO" "$@"; }

if [ ! -s "$OUT/probe-local.json" ]; then
    zoo_log "самопроверка сервера (zoo probe --local)"
    srv_zoo probe --local --json > "$OUT/probe-local.json" 2> "$PDIR/local.log" || true
fi
srv_zoo export-probe > "$PDIR/probe-export.json" 2> "$PDIR/export.log" \
    || zoo_die "zoo export-probe не сработал: $(tail -1 "$PDIR/export.log")"
SERVER_IP="$(zoo_ip "$SRV")"

# run_probe ИМЯ СЕТЬ [IP-шлюза к серверу]
run_probe() {
    local name="$1" net="$2" via="${3:-}" dir="$PDIR/$1" envs=()
    mkdir -p "$dir"
    cp "$PDIR/probe-export.json" "$dir/probe-export.json"
    rm -f "$dir/probe-report.json" "$dir/probe-report.md"
    [ -z "$via" ] || envs=(-e "ZOO_PROBE_VIA=$via")
    zoo_log "пробник: $name"
    docker run --rm --name "$PFX-$name" --label zoo.harness=1 --label zoo.role=probe \
        --network "$net" --cap-add NET_ADMIN --device /dev/net/tun "${envs[@]}" \
        -v "$(zoo_host_path "$dir"):/data" "$IMG" > "$dir/run.log" 2>&1 || true
    [ -s "$dir/probe-report.json" ] && cp "$dir/probe-report.json" "$OUT/probe-client-$name.json"
    return 0
}

# check ИМЯ ПРОФИЛЬ — сверка вердиктов, строки в expect.tsv
check() {
    local dir="$PDIR/$1"
    if [ ! -s "$dir/probe-report.json" ]; then
        printf '%s\t-\t-\t-\tFAIL\tнет отчёта (см. %s)\n' "$1" "$dir/run.log" >> "$TSV"
        return 0
    fi
    docker run --rm -v "$(zoo_host_path "$dir"):/data" "$IMG" \
        python3 /opt/zoo-probe/expect.py "$2" /data/probe-report.json | sed "s/^$2\t/$1\t/" >> "$TSV" || true
}

censor_up() {
    docker container inspect "$CENSOR" >/dev/null 2>&1 && return 0
    docker network create --label zoo.harness=1 "$CNET" >/dev/null
    docker run -d --name "$CENSOR" --label zoo.harness=1 --label zoo.role=censor \
        --network "$ZOO_NET" --cap-add NET_ADMIN --sysctl net.ipv4.ip_forward=1 "$CIMG" >/dev/null
    docker network connect "$CNET" "$CENSOR"
    docker exec "$CENSOR" zoo-censor-profile nat "$SERVER_IP" >/dev/null
    CENSOR_IP="$(docker container inspect -f "{{(index .NetworkSettings.Networks \"$CNET\").IPAddress}}" "$CENSOR")"
}

printf 'profile\tproto\tverdict\texpected\tresult\treason\n' > "$TSV"
CENSOR_IP=""
for p in $PROFILES; do
    if [ "$p" = "direct" ]; then
        run_probe direct "$ZOO_NET"
        check direct clean
    else
        censor_up
        docker exec "$CENSOR" zoo-censor-profile "$p" "$SERVER_IP" > "$PDIR/censor-$p.log"
        run_probe "$p" "$CNET" "$CENSOR_IP"
        check "$p" "$p"
    fi
done

echo
column -t -s $'\t' "$TSV" 2>/dev/null || cat "$TSV"
! grep -q $'\tFAIL\t' "$TSV"
