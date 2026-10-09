#!/usr/bin/env bash
# test.sh — e2e-прогон инсталлера в systemd-контейнере
#
#   docker/test.sh [--distro 24.04|22.04] [--phases all|00-bootstrap,03-3xui,...]
#                  [--mode phases|full] [--modes git|fs] [--env VAR=val]...
#                  [--name NAME] [--timeout СЕК] [--stop-on-fail] [--keep]
#                  [--tests all|vless-reality,hysteria2,...]
#
#   --mode phases  каждая фаза отдельным `install.sh --phase X` (по умолчанию)
#   --mode full    один `install.sh`, как у пользователя; итог по фазам — из state
#   --modes git    права файлов как после git clone (по умолчанию), fs — все shebang-файлы +x
#   --keep         не удалять контейнер после прогона
#   --tests        после установки запустить docker/tests/<id>.sh против сервера
#                  (all — тесты протоколов, чьи манифесты есть на сервере, routing, links,
#                  security, а при установленном zoo — allowlist (если есть AmneziaWG),
#                  web (zoo/tests/web_smoke.sh --users), collector, journal, live и history (если есть
#                  образ zoo-probe); последним — ssh-harden)
#   --probe-profiles  профили клиентского пробника (docker/probe/run.sh), через запятую:
#                  direct,clean,drop-udp,ip-block,freeze-16k,rst-tls (по умолчанию все); none — без него
#   --serial       тесты и профили пробника по очереди (по умолчанию тесты протоколов и links
#                  идут параллельно, профили пробника — параллельно; для отладки флаппинга)
#
# Итог: таблица в stdout и docker/out/<ts>/ (summary.md, summary.tsv, phases/*.log,
# diag.txt, files/). Код выхода 0 — фазы PASS (или выключены флагом), тесты PASS,
# самопроверка и пробники не FAIL.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=run-server.sh
. "$(dirname "${BASH_SOURCE[0]}")/run-server.sh"

DISTRO="$ZOO_DEFAULT_DISTRO"
PHASES_ARG="all"
MODE="phases"
MODES="git"
NAME=""
TIMEOUT=1800
STOP_ON_FAIL=0
KEEP=0
EXTRA_ENV=()
TESTS_ARG=""
PROBE_PROFILES=""
SERIAL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --distro)       DISTRO="$2"; shift 2 ;;
        --phases)       PHASES_ARG="$2"; shift 2 ;;
        --mode)         MODE="$2"; shift 2 ;;
        --modes)        MODES="$2"; shift 2 ;;
        --env)          EXTRA_ENV+=("$2"); shift 2 ;;
        --name)         NAME="$2"; shift 2 ;;
        --timeout)      TIMEOUT="$2"; shift 2 ;;
        --stop-on-fail) STOP_ON_FAIL=1; shift ;;
        --keep)         KEEP=1; shift ;;
        --serial)       SERIAL=1; shift ;;
        --tests)        TESTS_ARG="$2"; shift 2 ;;
        --probe-profiles) PROBE_PROFILES="${2//,/ }"; shift 2 ;;
        -h|--help)      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) zoo_die "неизвестный аргумент: $1" ;;
    esac
done

zoo_check_distro "$DISTRO"
case "$MODE" in phases|full) ;; *) zoo_die "--mode: phases или full" ;; esac
NAME="$(zoo_name "${NAME:-e2e-${DISTRO//./}}")"

TS="$(date +%Y%m%d-%H%M%S)"
OUT="$ZOO_DOCKER_DIR/out/$TS"
mkdir -p "$OUT/phases" "$OUT/files"

cleanup() {
    if [ "$KEEP" = "1" ]; then
        zoo_log "контейнер оставлен: $NAME (docker/run-server.sh shell $NAME; down $NAME)"
    else
        zoo_down "$NAME" || true
    fi
}
trap cleanup EXIT

{
    echo "ts=$TS"
    echo "distro=$DISTRO"
    echo "mode=$MODE"
    echo "modes=$MODES"
    echo "container=$NAME"
    echo "git_head=$(cd "$ZOO_REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo '-')"
    echo "git_dirty=$(cd "$ZOO_REPO_ROOT" && git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    printf 'env=%s\n' "${EXTRA_ENV[@]:-}"
} > "$OUT/meta.txt"

zoo_build "$DISTRO"
zoo_up "$NAME" --distro "$DISTRO" --modes "$MODES" 2>&1 | tee "$OUT/up.log"

# Список фаз берём из тестируемого install.sh, а не из своей копии
discover_phases() {
    docker exec -w /repo "$NAME" bash -c '
        if grep -q "^PHASES=(" scripts/install.sh; then
            eval "$(sed -n "/^PHASES=(/,/^)/p" scripts/install.sh)"
            printf "%s\n" "${PHASES[@]}"
        else
            for f in scripts/[0-9][0-9]*-*.sh; do basename "$f" .sh; done
        fi'
}

if [ "$PHASES_ARG" = "all" ]; then
    mapfile -t PHASES < <(discover_phases | tr -d '\r')
else
    IFS=',' read -r -a PHASES <<< "$PHASES_ARG"
fi
[ "${#PHASES[@]}" -gt 0 ] || zoo_die "не найдено ни одной фазы"
zoo_log "фазы: ${PHASES[*]}"

# Причина провала: последняя строка [x] самой фазы (не общая «X: failed» от install.sh),
# иначе последняя непустая строка перед ней
reason_of() {
    local log="$1" r
    r="$(grep -vE '^\[x\] [^ ]+: failed$' "$log" | grep -F '[x]' | tail -1 || true)"
    [ -n "$r" ] || r="$(grep -vE '^\[x\] [^ ]+: failed$|^[[:space:]]*$' "$log" | tail -1 || true)"
    printf '%s' "$r" | tr '\t' ' ' | cut -c1-200
}

declare -A STATUS=() RC=() DUR=() WARNS=() REASON=()

classify() {
    local phase="$1" rc="$2" log="$3"
    RC[$phase]="$rc"
    WARNS[$phase]="$(grep -c '\[!\]' "$log" || true)"
    if grep -q "${phase}: скрипт не найден или не executable" "$log"; then
        STATUS[$phase]="SKIPPED"; REASON[$phase]="install.sh пропустил фазу: нет файла или нет +x"
    elif grep -qE "фаза ${phase}: нет файла|^ +${phase} +пропуск: " "$log"; then
        STATUS[$phase]="SKIPPED"; REASON[$phase]="$(grep -oE "^ +${phase} +пропуск: .*|нет файла.*" "$log" | head -1 | sed 's/^ *//')"
    elif [ "$rc" = "0" ]; then
        STATUS[$phase]="PASS"; REASON[$phase]=""
    elif [ "$rc" = "124" ]; then
        STATUS[$phase]="TIMEOUT"; REASON[$phase]="дольше ${TIMEOUT} с"
    elif [ "$rc" = "126" ]; then
        STATUS[$phase]="NOEXEC"; REASON[$phase]="$(reason_of "$log")"
    else
        STATUS[$phase]="FAIL"; REASON[$phase]="$(reason_of "$log")"
    fi
}

run_install() {
    local envs=(-e ZOO_TEST_ENV=docker -e "SERVER_IP=$(zoo_ip "$NAME")") e
    for e in "${EXTRA_ENV[@]:-}"; do [ -n "$e" ] && envs+=(-e "$e"); done
    docker exec -w /repo "${envs[@]}" "$NAME" timeout "$TIMEOUT" ./scripts/install.sh "$@"
}

if [ "$MODE" = "phases" ]; then
    failed=0
    for phase in "${PHASES[@]}"; do
        log="$OUT/phases/$phase.log"
        if [ "$failed" = "1" ] && [ "$STOP_ON_FAIL" = "1" ]; then
            STATUS[$phase]="NOT_RUN"; RC[$phase]="-"; DUR[$phase]=0; WARNS[$phase]=0; REASON[$phase]="--stop-on-fail"
            continue
        fi
        zoo_log "фаза $phase"
        start=$SECONDS
        set +e
        run_install --phase "$phase" > "$log" 2>&1
        rc=$?
        set -e
        DUR[$phase]=$((SECONDS - start))
        classify "$phase" "$rc" "$log"
        zoo_log "  $phase: ${STATUS[$phase]} (rc=$rc, ${DUR[$phase]} с)"
        [ "${STATUS[$phase]}" = "PASS" ] || failed=1
    done
else
    log="$OUT/phases/full.log"
    start=$SECONDS
    set +e
    run_install > "$log" 2>&1
    rc=$?
    set -e
    total=$((SECONDS - start))
    state="$(docker exec "$NAME" cat /var/lib/vpn-setup/state 2>/dev/null || true)"
    blamed=0
    for phase in "${PHASES[@]}"; do
        DUR[$phase]="-"; WARNS[$phase]="-"; RC[$phase]="-"; REASON[$phase]=""
        st="$(printf '%s\n' "$state" | grep -E "^${phase}=" | tail -1 | cut -d= -f2 || true)"
        if [ "$st" = "done" ]; then
            STATUS[$phase]="PASS"
        elif [ "$st" = "rebooting" ]; then
            STATUS[$phase]="REBOOT"; REASON[$phase]="фаза попросила reboot"; blamed=1
        elif [ "$blamed" = "0" ] && [ "$rc" != "0" ]; then
            classify "$phase" "$rc" "$log"; blamed=1
        elif grep -q "${phase}: скрипт не найден или не executable" "$log"; then
            STATUS[$phase]="SKIPPED"; REASON[$phase]="install.sh пропустил фазу: нет файла или нет +x"
        elif grep -qE "^ +${phase} +пропуск: " "$log"; then
            STATUS[$phase]="SKIPPED"; REASON[$phase]="$(grep -oE "^ +${phase} +пропуск: .*" "$log" | head -1 | sed -E 's/^ +[^ ]+ +//')"
        else
            STATUS[$phase]="NOT_RUN"
        fi
    done
    zoo_log "install.sh: rc=$rc за $total с"
fi

# Самопроверка в конце установки (install.sh после 99): сводка «работает в принципе»,
# следующий шаг для пользователя и пакет пробника с её итогом
INSTALL_SELFTEST="SKIPPED"
if [ "$MODE" = "full" ] && [ "${STATUS[99-print-creds]:-}" = "PASS" ]; then
    if grep -q 'работает в принципе' "$log" && grep -q 'Дальше — проверка с вашей машины' "$log" \
            && docker exec "$NAME" jq -e '.selftest.verdicts | length > 0' /etc/vpn-setup/probe-export.json >/dev/null 2>&1; then
        INSTALL_SELFTEST="PASS"
    else
        INSTALL_SELFTEST="FAIL"
    fi
    zoo_log "самопроверка в конце install.sh: $INSTALL_SELFTEST"
fi

# ---------- диагностика ----------
zoo_log "сбор диагностики"
docker exec -i "$NAME" bash -s > "$OUT/diag.txt" 2>&1 <<'DIAG' || true
sec() { printf '\n===== %s =====\n' "$*"; }
sec uname; uname -a; grep PRETTY /etc/os-release
sec systemd; systemctl is-system-running; systemctl --failed --no-legend --plain
sec listen; ss -tulpn
sec ufw; ufw status verbose 2>&1 || true
sec iptables-save; iptables-save 2>&1 || true
sec nft; nft list ruleset 2>&1 || true
sec sysctl; sysctl net.ipv4.ip_forward net.ipv4.tcp_congestion_control net.core.default_qdisc 2>&1 || true
sec versions
[ -x /usr/local/x-ui/x-ui ] && /usr/local/x-ui/x-ui -v 2>&1
for x in /usr/local/x-ui/bin/xray-linux-*; do [ -x "$x" ] && "$x" version 2>&1 | head -1; done
command -v hysteria >/dev/null && hysteria version 2>&1 | grep -E '^Version' || true
dpkg -l 2>/dev/null | awk '/amneziawg|wireguard|ufw|fail2ban|python3 /{print $2, $3}'
sec state; cat /var/lib/vpn-setup/state 2>/dev/null
sec manifests; ls -la /etc/vpn-setup/protocols.d 2>/dev/null
for u in x-ui hysteria-server awg-quick@awg0 fail2ban zoo-web; do
    systemctl list-unit-files "$u.service" >/dev/null 2>&1 || continue
    sec "journal $u"; journalctl -u "$u" -n 80 --no-pager 2>&1
done
DIAG

# Файлы с сервера (секреты тестового стенда; docker/out/ в .gitignore)
docker exec "$NAME" bash -c '
    paths=()
    for p in /etc/vpn-setup /var/lib/vpn-setup /root/CREDENTIALS.md /etc/hysteria \
             /etc/amnezia/amneziawg /usr/local/x-ui/bin/config.json /etc/x-ui /var/log/vpn-zoo; do
        [ -e "$p" ] && paths+=("$p")
    done
    [ "${#paths[@]}" -eq 0 ] || tar -cf - "${paths[@]}" 2>/dev/null' \
    | tar -xf - -C "$OUT/files" 2>/dev/null || true

# ---------- пробники (подключаются по мере появления) ----------
PROBE_LOCAL="SKIPPED"
if docker exec "$NAME" bash -c 'command -v zoo' >/dev/null 2>&1; then
    if docker exec -e ZOO_TEST_ENV=docker "$NAME" zoo probe --local --json > "$OUT/probe-local.json" 2> "$OUT/probe-local.log"; then
        PROBE_LOCAL="PASS"
    else
        PROBE_LOCAL="FAIL"
    fi
fi
# Клиентский пробник: напрямую и через цензора; вердикты сверяются с профилем (OUT/probe/expect.tsv)
PROBE_CLIENT="SKIPPED"
if [ -f "$ZOO_DOCKER_DIR/probe/run.sh" ] && [ "$PROBE_PROFILES" != "none" ] \
        && docker exec "$NAME" bash -c 'ls /etc/vpn-setup/protocols.d/*.json' >/dev/null 2>&1; then
    zoo_log "клиентский пробник: ${PROBE_PROFILES:-все профили}"
    if ZOO_PROBE_PROFILES="${PROBE_PROFILES:-}" ZOO_PROBE_PREFIX="$NAME-probe" ZOO_PROBE_SERIAL="$SERIAL" \
            bash "$ZOO_DOCKER_DIR/probe/run.sh" "$NAME" "$OUT" > "$OUT/probe-client.log" 2>&1; then
        PROBE_CLIENT="PASS"
    else
        PROBE_CLIENT="FAIL"
    fi
    zoo_log "  пробник: $PROBE_CLIENT (probe/expect.tsv)"
fi

# ---------- сквозные тесты протоколов (docker/tests/) ----------
declare -A TSTATUS=()
TESTS=()
if [ -n "$TESTS_ARG" ]; then
    mkdir -p "$OUT/tests"
    if [ "$TESTS_ARG" = "all" ]; then
        have="$(docker exec "$NAME" bash -c 'ls /etc/vpn-setup/protocols.d/ 2>/dev/null' | tr -d '\r' || true)"
        for t in vless-reality vless-xhttp ss2022 tuic mtproto hysteria2 amneziawg; do
            grep -qx "$t.json" <<< "$have" && TESTS+=("$t")
        done
        docker exec "$NAME" test -f /var/lib/vpn-zoo/geo/state.json 2>/dev/null && TESTS+=(routing)
        # выданные ссылки owner (не probe) импортируются клиентами и пропускают трафик
        [ -z "$have" ] || TESTS+=(links)
        TESTS+=(security)
        # админка формами (с пользователем во всех протоколах) и коллектор трафика — после
        # тестов протоколов: им нужен накопленный трафик
        if docker exec "$NAME" test -x /usr/local/bin/zoo 2>/dev/null; then
            grep -qx amneziawg.json <<< "$have" && TESTS+=(allowlist)
            TESTS+=(web collector journal live)
            # история проб: нужен образ пробника (его собирает docker/probe/run.sh выше)
            if docker image inspect "${ZOO_PROBE_IMAGE:-zoo-probe}" >/dev/null 2>&1; then TESTS+=(history); fi
        fi
        # закрытие SSH (фаза 01b) — последним: меняет порт SSH, в конце возвращает исходный
        TESTS+=(ssh-harden)
    else
        IFS=',' read -r -a TESTS <<< "$TESTS_ARG"
    fi
    # run_test ТЕСТ — статус в OUT/tests/ТЕСТ.status: фоновые запуски не видят TSTATUS
    run_test() {
        local t="$1" st
        if [ "$t" = "web" ]; then
            zoo_log "тест web (zoo/tests/web_smoke.sh --users)"
            if docker exec -e ZOO_TEST_ENV=docker "$NAME" bash /repo/zoo/tests/web_smoke.sh --users \
                    > "$OUT/tests/web.log" 2>&1; then st=PASS; else st=FAIL; fi
        elif [ ! -f "$ZOO_DOCKER_DIR/tests/$t.sh" ]; then
            st=MISSING
        else
            zoo_log "тест $t"
            if bash "$ZOO_DOCKER_DIR/tests/$t.sh" "$NAME" > "$OUT/tests/$t.log" 2>&1; then st=PASS; else st=FAIL; fi
        fi
        echo "$st" > "$OUT/tests/$t.status"
        zoo_log "  $t: $st"
    }
    # Параллельно — тесты, которые трогают только своих пользователей и своих клиентов.
    # Остальные (routing, security, allowlist, web, collector, journal, live, history, ssh-harden) меняют общее
    # состояние сервера или ждут накопленного трафика — по очереди после них
    PARALLEL_OK=" vless-reality vless-xhttp ss2022 tuic mtproto hysteria2 amneziawg links "
    PAR=(); SEQ=()
    for t in "${TESTS[@]}"; do
        if [ "$SERIAL" = "0" ] && [[ "$PARALLEL_OK" == *" $t "* ]]; then PAR+=("$t"); else SEQ+=("$t"); fi
    done
    if [ ${#PAR[@]} -gt 0 ]; then
        zoo_log "параллельно: ${PAR[*]}"
        for t in "${PAR[@]}"; do run_test "$t" & done
        wait
    fi
    for t in "${SEQ[@]}"; do run_test "$t"; done
    for t in "${TESTS[@]}"; do TSTATUS[$t]="$(cat "$OUT/tests/$t.status" 2>/dev/null || echo FAIL)"; done
fi

# ---------- итог ----------
all_pass=1
{
    printf 'phase\tstatus\trc\tseconds\twarnings\treason\n'
    for phase in "${PHASES[@]}"; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$phase" "${STATUS[$phase]}" "${RC[$phase]}" \
            "${DUR[$phase]}" "${WARNS[$phase]}" "${REASON[$phase]}"
    done
    printf 'install-selftest\t%s\t-\t-\t-\t\n' "$INSTALL_SELFTEST"
    printf 'probe-local\t%s\t-\t-\t-\t\n' "$PROBE_LOCAL"
    printf 'probe-client\t%s\t-\t-\t-\t\n' "$PROBE_CLIENT"
    # по профилю цензора: PASS — все вердикты совпали с ожидаемыми
    if [ -s "$OUT/probe/expect.tsv" ]; then
        tail -n +2 "$OUT/probe/expect.tsv" | awk -F'\t' '
            !($1 in st) { order[++n] = $1; st[$1] = "PASS" }
            $5 == "FAIL" { st[$1] = "FAIL"; why[$1] = why[$1] $2 "=" $3 " " }
            END { for (i = 1; i <= n; i++) printf "probe:%s\t%s\t-\t-\t-\t%s\n", order[i], st[order[i]], why[order[i]] }'
    fi
    for t in "${TESTS[@]:-}"; do
        [ -n "$t" ] || continue
        printf 'test:%s\t%s\t-\t-\t-\t%s\n' "$t" "${TSTATUS[$t]}" "tests/$t.log"
    done
} > "$OUT/summary.tsv"

{
    echo "# e2e $TS — Ubuntu $DISTRO, mode=$MODE, modes=$MODES"
    echo
    echo '| Фаза | Итог | rc | с | [!] | Причина |'
    echo '|---|---|---|---|---|---|'
    tail -n +2 "$OUT/summary.tsv" | while IFS=$'\t' read -r p s r d w why; do
        echo "| $p | $s | $r | $d | $w | ${why//|/\\|} |"
    done
} > "$OUT/summary.md"

# фаза, выключенная флагом (ENABLE_TUIC=0 по умолчанию), — не провал
for phase in "${PHASES[@]}"; do
    case "${STATUS[$phase]}:${REASON[$phase]}" in
        PASS:*|SKIPPED:*"выключена ("*) ;;
        *) all_pass=0 ;;
    esac
done
for t in "${TESTS[@]:-}"; do [ -z "$t" ] || [ "${TSTATUS[$t]}" = "PASS" ] || all_pass=0; done
[ "$PROBE_LOCAL" != "FAIL" ] && [ "$PROBE_CLIENT" != "FAIL" ] && [ "$INSTALL_SELFTEST" != "FAIL" ] || all_pass=0

echo
column -t -s $'\t' "$OUT/summary.tsv" 2>/dev/null || cat "$OUT/summary.tsv"
echo
zoo_log "отчёт: $OUT"
[ "$all_pass" = "1" ]
