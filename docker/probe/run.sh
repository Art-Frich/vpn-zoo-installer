#!/usr/bin/env bash
# run.sh SERVER OUT — клиентский пробник на стенде: напрямую и через цензора по профилям.
#
#   docker/probe/run.sh e2e-2404 docker/out/<ts>      # так вызывает docker/test.sh
#
# 1. На сервере: `zoo probe --local` (если в OUT ещё нет probe-local.json) и `zoo export-probe`.
# 2. Контейнер zoo-probe в zoo-net — профиль direct (ожидания как у clean).
# 3. Цензор (docker/censor) между отдельной сетью пробника и zoo-net; у каждого профиля свой
#    цензор и своя сеть, профили идут параллельно; сверка вердиктов — docker/probe/expect.py.
#    AmneziaWG — вторым проходом по профилям последовательно: у пира один endpoint, параллельные
#    пробники с одним ключом сбивали бы друг друга.
# Итог: OUT/probe/expect.tsv, OUT/probe/<профиль>[-awg]/probe-report.{json,md},
# OUT/probe-client-<профиль>[-awg].json. Код 1 — хоть один вердикт не совпал с ожиданием.
#
# Окружение: ZOO_PROBE_PROFILES (по умолчанию «direct clean drop-udp ip-block freeze-16k rst-tls»),
# ZOO_PROBE_IMAGE / ZOO_CENSOR_IMAGE (zoo-probe / zoo-censor), ZOO_PROBE_BUILD=0 — не пересобирать
# образы, ZOO_PROBE_PREFIX — префикс имён контейнеров и сетей (по умолчанию zoo-probe-<pid>),
# ZOO_PROBE_SERIAL=1 — профили по очереди, все протоколы за один прогон (для отладки).

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
PDIR="$OUT/probe"
TSV="$PDIR/expect.tsv"

zoo_exists "$SRV" || zoo_die "нет контейнера $SRV"
mkdir -p "$PDIR"

cleanup() {
    local p
    for p in $PROFILES; do
        docker rm -f "$PFX-censor-$p" >/dev/null 2>&1 || true
        docker network rm "$PFX-cnet-$p" >/dev/null 2>&1 || true
    done
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

# run_probe КАТАЛОГ СЕТЬ IP-шлюза|"" [аргументы zoo probe --remote…]
run_probe() {
    local name="$1" net="$2" via="${3:-}" dir="$PDIR/$1" envs=()
    shift 3
    mkdir -p "$dir"
    cp "$PDIR/probe-export.json" "$dir/probe-export.json"
    rm -f "$dir/probe-report.json" "$dir/probe-report.md"
    [ -z "$via" ] || envs=(-e "ZOO_PROBE_VIA=$via")
    zoo_log "пробник: $name"
    docker run --rm --name "$PFX-$name" --label zoo.harness=1 --label zoo.role=probe \
        --network "$net" --cap-add NET_ADMIN --device /dev/net/tun "${envs[@]}" \
        -v "$(zoo_host_path "$dir"):/data" "$IMG" "$@" > "$dir/run.log" 2>&1 || true
    [ -s "$dir/probe-report.json" ] && cp "$dir/probe-report.json" "$OUT/probe-client-$name.json"
    return 0
}

# check КАТАЛОГ ПРОФИЛЬ [ПРОФИЛЬ-ОЖИДАНИЙ] — вердикты в PDIR/КАТАЛОГ.tsv, первая колонка — профиль
check() {
    local dir="$PDIR/$1" out="$PDIR/$1.tsv" exp="${3:-$2}"
    if [ ! -s "$dir/probe-report.json" ]; then
        printf '%s\t-\t-\t-\tFAIL\tнет отчёта (см. %s)\n' "$2" "$dir/run.log" > "$out"
        return 0
    fi
    docker run --rm -v "$(zoo_host_path "$dir"):/data" "$IMG" \
        python3 /opt/zoo-probe/expect.py "$exp" /data/probe-report.json \
        | sed "s/^$exp\t/$2\t/" > "$out" || true
}

# censor_up ПРОФИЛЬ — свой цензор и своя сеть на профиль; печатает IP цензора в сети пробника
censor_up() {
    local p="$1" c="$PFX-censor-$1" n="$PFX-cnet-$1"
    if ! docker container inspect "$c" >/dev/null 2>&1; then
        docker network create --label zoo.harness=1 "$n" >/dev/null
        docker run -d --name "$c" --label zoo.harness=1 --label zoo.role=censor \
            --network "$ZOO_NET" --cap-add NET_ADMIN --sysctl net.ipv4.ip_forward=1 "$CIMG" >/dev/null
        docker network connect "$n" "$c"
        docker exec "$c" zoo-censor-profile nat "$SERVER_IP" >/dev/null
        docker exec "$c" zoo-censor-profile "$p" "$SERVER_IP" > "$PDIR/censor-$p.log"
    fi
    docker container inspect -f "{{(index .NetworkSettings.Networks \"$n\").IPAddress}}" "$c"
}

# profile_run ПРОФИЛЬ КАТАЛОГ [аргументы пробника…] — один прогон пробника и сверка
profile_run() {
    local p="$1" dir="$2" ip
    shift 2
    if [ "$p" = "direct" ]; then
        run_probe "$dir" "$ZOO_NET" "" "$@"
        check "$dir" direct clean
    else
        ip="$(censor_up "$p")"
        run_probe "$dir" "$PFX-cnet-$p" "$ip" "$@"
        check "$dir" "$p"
    fi
}

mapfile -t ALL_IDS < <(docker exec "$SRV" bash -c 'ls /etc/vpn-setup/protocols.d/' | tr -d '\r' | sed -n 's/\.json$//p')
MAIN_ARGS=(); AWG_ARGS=()
for id in "${ALL_IDS[@]}"; do
    case "$id" in amneziawg*) AWG_ARGS+=(--proto "$id") ;; *) MAIN_ARGS+=(--proto "$id") ;; esac
done

if [ "${ZOO_PROBE_SERIAL:-0}" = "1" ]; then
    for p in $PROFILES; do profile_run "$p" "$p"; done
else
    if [ ${#MAIN_ARGS[@]} -gt 0 ]; then
        for p in $PROFILES; do profile_run "$p" "$p" "${MAIN_ARGS[@]}" & done
        wait
    fi
    if [ ${#AWG_ARGS[@]} -gt 0 ]; then
        for p in $PROFILES; do profile_run "$p" "$p-awg" "${AWG_ARGS[@]}"; done
    fi
fi

printf 'profile\tproto\tverdict\texpected\tresult\treason\n' > "$TSV"
for p in $PROFILES; do
    for f in "$PDIR/$p.tsv" "$PDIR/$p-awg.tsv"; do
        if [ -f "$f" ]; then cat "$f" >> "$TSV"; fi
    done
done

echo
column -t -s $'\t' "$TSV" 2>/dev/null || cat "$TSV"
! grep -q $'\tFAIL\t' "$TSV"
