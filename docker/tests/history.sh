#!/usr/bin/env bash
# docker/tests/history.sh — история проб: метрики задержки и скорости, контекст, SQLite на сервере,
# рейтинг, анонимный экспорт без IP и шифрование age на ключ SSH, scripts/history.sh, админка.
#
#   docker/tests/history.sh SERVER
#   ZOO_PROBE_IMAGE=zoo-hist-probe docker/tests/history.sh hist-a    # другой образ пробника
#
# Нужны: установленный сервер (zoo, age от фазы 09, хотя бы один протокол) и собранный образ zoo-probe.
# Что проверяется:
#   1. zoo probe --local --tag … --upload-mb 1: в отчёте context и metrics (медиана/p90/джиттер, загрузка,
#      отдача), прогон записан в /var/lib/vpn-zoo/probe-history.sqlite, повтор отчёта не дублируется;
#   2. клиентский пробник (контейнер в zoo-net, прямой выход) с --tag/--device и каталогом /history:
#      анонимный jsonl без IP, сырой отчёт .age расшифровывается ключом SSH, чужим — нет;
#   3. zoo history add (stdin) и zoo probe --rank: контекст ci-mobile, протоколы и числа сходятся с отчётом;
#   4. zoo history export --tar: jsonl без IP, по .age на каждый отчёт, расшифровка на сервере;
#   5. scripts/history.sh pull, push и decrypt через подменённый ssh (docker exec): слияние без дублей;
#   6. страница «Проверка» админки: «Лучшие протоколы», тренды, журнал.
# Код возврата 0 — всё прошло.

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=../run-server.sh
. "$HERE/../run-server.sh"

SRV="$(zoo_name "${1:?usage: history.sh SERVER}")"
IMG="${ZOO_PROBE_IMAGE:-zoo-probe}"
CLI="$SRV-hcli"
AGE=/usr/local/lib/vpn-zoo/bin/age
IP_RE='[0-9]{1,3}(\.[0-9]{1,3}){3}'
HIST_SH="$HERE/../../scripts/history.sh"

fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
info() { echo "....  $*"; }
# check ЧТО команда… — PASS при коде 0
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
srv() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }

TMP="$(mktemp -d)"
cleanup() {
    docker rm -f "$CLI" >/dev/null 2>&1 || true
    srv rm -f /tmp/hist-key /tmp/hist-key.pub /tmp/hist-key2 /tmp/hist-key2.pub >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null 2>&1 || { echo "нет контейнера $SRV" >&2; exit 2; }
srv test -x /usr/local/bin/zoo || { echo "на $SRV нет zoo (фаза 09)" >&2; exit 2; }
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "нет образа $IMG (docker build -f docker/probe.Dockerfile)" >&2; exit 2; }
if ! srv test -x "$AGE"; then
    echo "на $SRV нет $AGE: фаза 09 ставит age (docker/run-server.sh sync … && install … -- --phase 09)" >&2
    exit 2
fi

# sql ЗАПРОС — база истории на сервере
sql() {
    srv python3 -c 'import sqlite3, sys
con = sqlite3.connect("/var/lib/vpn-zoo/probe-history.sqlite")
for row in con.execute(sys.argv[1]):
    print("\t".join("" if v is None else str(v) for v in row))' "$1" | tr -d '\r'
}
# jqf ФАЙЛ ВЫРАЖЕНИЕ — код 0, если выражение истинно; jqv — значение
jqf() { srv jq -e "$2" < "$1" >/dev/null; }
jqv() { srv jq -r "$2" < "$1" | tr -d '\r'; }
# no_ip ФАЙЛ… — в файлах нет IPv4-подобного
no_ip() { ! cat "$@" | grep -Eq "$IP_RE"; }
# has_text ТЕКСТ ОБРАЗЕЦ — образец найден в тексте (без канала: pipefail + grep -q даёт SIGPIPE)
has_text() { [[ "$1" == *"$2"* ]]; }
no_style() { [[ "$1" != *"style="* ]]; }
# has_all ТЕКСТ ОБРАЗЕЦ… — все образцы найдены в тексте
has_all() { local t="$1" s; shift; for s in "$@"; do [[ "$t" == *"$s"* ]] || return 1; done; }
# jq_text ТЕКСТ ВЫРАЖЕНИЕ — JSON из текста удовлетворяет выражению jq
jq_text() { printf '%s' "$1" | srv jq -e "$2" >/dev/null; }
# no_ip_text ТЕКСТ — в тексте нет IPv4-подобного
no_ip_text() { ! grep -Eq "$IP_RE" <<< "$1"; }
# absent ФАЙЛ СТРОКА… — ни одной из строк в файле нет; present — все есть
absent() { local f="$1" s; shift; for s in "$@"; do ! grep -qF -- "$s" "$f" || return 1; done; }
present() { local f="$1" s; shift; for s in "$@"; do grep -qF -- "$s" "$f" || return 1; done; }
# files_sorted ФАЙЛ… — строки каждого файла по возрастанию (ts первым ключом = по времени)
files_sorted() { local f; for f in "$@"; do LC_ALL=C sort -c "$f" || return 1; done; }
# age_dec КЛЮЧ ФАЙЛ — расшифровка в контейнере пробника (age из образа)
age_dec() {
    docker run --rm -i --entrypoint age -v "$(zoo_host_path "$TMP/keys"):/key:ro" "$IMG" -d -i "/key/$1" < "$2"
}

SERVER_IP="$(zoo_ip "$SRV")"
srv rm -f /var/lib/vpn-zoo/probe-history.sqlite /var/lib/vpn-zoo/probe-history.sqlite-wal /var/lib/vpn-zoo/probe-history.sqlite-shm

# ---------- ключи age (ssh-ed25519) ----------
for k in hist-key hist-key2; do
    srv rm -f "/tmp/$k" "/tmp/$k.pub"
    srv ssh-keygen -q -t ed25519 -N '' -C "$k" -f "/tmp/$k" >/dev/null
done
mkdir -p "$TMP/hist" "$TMP/data" "$TMP/keys"
{ echo "# получатели теста"; srv cat /tmp/hist-key.pub; } | tr -d '\r' > "$TMP/hist/recipients.txt"
srv cat /tmp/hist-key | tr -d '\r' > "$TMP/keys/key"
srv cat /tmp/hist-key2 | tr -d '\r' > "$TMP/keys/key2"
check "recipients.txt: публичный ssh-ed25519" grep -q '^ssh-ed25519 ' "$TMP/hist/recipients.txt"

# ---------- 1. прогон с сервера ----------
info "zoo probe --local --tag ci-server --upload-mb 1"
srv zoo probe --local --quiet --json --tag ci-server --upload-mb 1 > "$TMP/local.json" 2> "$TMP/local.err" || true
check "отчёт сервера: context.tag и mode" jqf "$TMP/local.json" '.mode == "local" and .context.tag == "ci-server"'
check "отчёт сервера: settings (5 замеров, отдача 1 МБ, загрузка 5 МБ)" \
    jqf "$TMP/local.json" '.settings.latency_samples == 5 and .settings.upload_bytes == 1000000 and .settings.large_bytes == 5000000'
check "у каждого работающего протокола: задержка (10 замеров, p90 ≥ медианы) и загрузка" jqf "$TMP/local.json" '
    [.results[] | select(.verdict == "OK" or .verdict == "SLOW")] as $w
    | ($w | length) > 0 and all($w[];
        .metrics.latency.sent == 10 and .metrics.latency.median_ms > 0
        and .metrics.latency.p90_ms >= .metrics.latency.median_ms
        and (.metrics.latency.per_target | length) == 2
        and .metrics.download.mbps > 0 and .speed_mbps > 0 and .latency_ms > 0)'
check "отдача измерена хотя бы через один протокол" \
    jqf "$TMP/local.json" '[.results[].metrics.upload.mbps | select(. != null and . > 0)] | length > 0'
info "$(jqv "$TMP/local.json" '[.results[] | "\(.id)=\(.verdict) ответ \(.metrics.latency.median_ms // "-") мс, загрузка \(.metrics.download.mbps // "-"), отдача \(.metrics.upload.mbps // "-") Мбит/с"] | join("; ")')"
n_res="$(jqv "$TMP/local.json" '.results | length')"
check "БД: прогон записан (1 отчёт, $n_res строк)" \
    test "$(sql "SELECT COUNT(*) || ',' || (SELECT COUNT(*) FROM results) FROM reports WHERE mode = 'local' AND tag = 'ci-server'")" = "1,$n_res"
check "БД: файл 0600" test "$(srv stat -c %a /var/lib/vpn-zoo/probe-history.sqlite | tr -d '\r')" = 600
out="$(srv zoo history add - < "$TMP/local.json" 2>&1 || true)"
check "zoo history add: тот же отчёт не дублируется" has_text "$out" "уже есть"
first_ok="$(jqv "$TMP/local.json" '[.results[] | select(.verdict == "OK")][0].id // empty')"
if [ -n "$first_ok" ]; then
    srv zoo probe --local --quiet --json --proto "$first_ok" --latency-samples 2 > "$TMP/local2.json" 2>/dev/null || true
    check "частичный прогон ($first_ok, 2 замера на цель): записан вторым отчётом" \
        test "$(sql "SELECT COUNT(*) FROM reports WHERE mode = 'local'")" = 2
    check "latency-samples 2: 4 замера" jqf "$TMP/local2.json" '.results[0].metrics.latency.sent == 4'
    before="$(sql 'SELECT COUNT(*) FROM reports')"
    srv zoo probe --local --quiet --no-history --latency-samples 0 --proto "$first_ok" >/dev/null 2>&1 || true
    check "--no-history: прогон не записывается" test "$(sql 'SELECT COUNT(*) FROM reports')" = "$before"
fi

# ---------- 2. клиентский пробник: метки, /history, age ----------
info "клиентский пробник: контейнер $CLI в zoo-net, --tag ci-mobile --device ci-box --upload-mb 1"
srv zoo export-probe > "$TMP/data/probe-export.json" 2>/dev/null
docker run --rm --name "$CLI" --label zoo.harness=1 --label zoo.role=probe --network "$ZOO_NET" \
    --cap-add NET_ADMIN --device /dev/net/tun \
    -v "$(zoo_host_path "$TMP/data"):/data" -v "$(zoo_host_path "$TMP/hist"):/history" \
    "$IMG" --tag ci-mobile --device ci-box --upload-mb 1 > "$TMP/client.log" 2>&1 || true
rm -f "$TMP/data/probe-export.json"
REPORT="$TMP/data/probe-report.json"
[ -s "$REPORT" ] || { fail "пробник не оставил probe-report.json"; tail -20 "$TMP/client.log"; exit 1; }
check "клиентский отчёт: mode, tag, device, direct_ip" \
    jqf "$REPORT" '.mode == "remote" and .context.tag == "ci-mobile" and .context.device == "ci-box" and (.direct_ip | type) == "string"'
ctx_json="$(jqv "$REPORT" '.context | tojson')"
check "клиентский отчёт: в context нет IP" no_ip_text "$ctx_json"
info "контекст: $ctx_json"
check "клиентский отчёт: метрики у работающих протоколов" jqf "$REPORT" '
    [.results[] | select(.verdict == "OK" or .verdict == "SLOW")] as $w
    | ($w | length) > 0 and all($w[]; .metrics.latency.median_ms > 0 and .metrics.download.mbps > 0)'
n_client="$(jqv "$REPORT" '.results | length')"
jsonl=("$TMP"/hist/*.jsonl)
if [ -f "${jsonl[0]}" ]; then
    pass "/history: создан ${jsonl[0]##*/}"
    check "/history: строк jsonl = числу протоколов ($n_client)" test "$(wc -l < "${jsonl[0]}" | tr -d ' ')" = "$n_client"
    check "/history: в jsonl нет IP" no_ip "${jsonl[@]}"
    direct_ip="$(jqv "$REPORT" '.direct_ip')"
    check "/history: нет адреса сервера и прямого IP клиента" absent "${jsonl[0]}" "$SERVER_IP" "$direct_ip"
    check "/history: метки tag и device в строках" present "${jsonl[0]}" '"tag":"ci-mobile"' '"device":"ci-box"'
else
    fail "/history: jsonl не создан"
fi
raws=("$TMP"/hist/raw/*.age)
if [ -f "${raws[0]}" ]; then
    pass "/history/raw: сырой отчёт ${raws[0]##*/}"
    plain="$(age_dec key "${raws[0]}" || true)"
    check "age: ключ ssh-ed25519 расшифровывает отчёт с адресом сервера" \
        jq_text "$plain" ".type == \"zoo-probe-report\" and .server_ip == \"$SERVER_IP\""
    if age_dec key2 "${raws[0]}" >/dev/null 2>&1; then fail "age: чужой ключ расшифровал отчёт"; else pass "age: чужой ключ не расшифровывает"; fi
    check "age: в зашифрованном файле нет адреса сервера" absent "${raws[0]}" "$SERVER_IP"
else
    fail "/history/raw: .age не создан (age --version в образе пробника?)"
fi

# ---------- 3. отчёт клиента на сервер, рейтинг ----------
out="$(srv zoo history add - < "$REPORT" 2>&1 || true)"
check "zoo history add (stdin): клиентский отчёт записан" has_text "$out" "записан"
out="$(srv zoo history add - < "$REPORT" 2>&1 || true)"
check "повторная запись не дублирует" has_text "$out" "уже есть"
srv zoo probe --rank --json --period 30d > "$TMP/rank.json"
check "rank: контекст ci-mobile, один прогон, низкая уверенность" jqf "$TMP/rank.json" '
    [.rank[] | select(.context == "ci-mobile")] as $c
    | ($c | length) == 1 and $c[0].reports == 1 and all($c[0].protocols[]; .low_confidence == true)'
check "rank: прогоны сервера не попали в рейтинг клиента" jqf "$TMP/rank.json" '[.rank[].context] | index("ci-server") == null'
check "rank: оценки 0..100 по убыванию, топ только из работающих" jqf "$TMP/rank.json" '
    all(.rank[]; (.protocols | map(.score)) as $s
        | ($s | all(. >= 0 and . <= 100)) and ($s == ($s | sort | reverse)) and all(.top[]; .ok > 0))'
want="$(jqv "$REPORT" '[.results[] | select(.verdict == "OK" or .verdict == "SLOW") | .id] | sort | join(",")')"
got="$(jqv "$TMP/rank.json" '[.rank[] | select(.context == "ci-mobile") | .protocols[] | select(.ok == 1) | .proto] | sort | join(",")')"
check "rank: протоколы с успехом = OK/SLOW отчёта клиента ($want)" test -n "$want" -a "$got" = "$want"
check "rank: у топа есть задержка и скорость" jqf "$TMP/rank.json" '
    [.rank[] | select(.context == "ci-mobile") | .top[]] | length > 0 and all(.[]; .latency_ms > 0 and .down_mbps > 0)'
srv zoo probe --rank --tag ci-mobile > "$TMP/rank.txt"
rank_txt="$(cat "$TMP/rank.txt")"
check "rank (текст): метка, «мало данных», формула" has_all "$rank_txt" ci-mobile "мало данных" "оценка ="
sed -n 1,12p "$TMP/rank.txt" | sed 's/^/....  /'
srv zoo probe --rank --json --with-local --by tag > "$TMP/rank-local.json"
check "rank --with-local --by tag: контексты ci-server (прогоны сервера) и ci-mobile" \
    jqf "$TMP/rank-local.json" '[.rank[].context] | index("ci-server") != null and index("ci-mobile") != null'

# ---------- 4. экспорт с сервера ----------
srv zoo history export --tar --recipients - < "$TMP/hist/recipients.txt" > "$TMP/export.tar" 2> "$TMP/export.err"
mkdir -p "$TMP/exp"
check "export --tar: архив читается" tar -xf "$TMP/export.tar" -C "$TMP/exp"
n_rep="$(sql 'SELECT COUNT(*) FROM reports')"
check "export: по .age на каждый отчёт ($n_rep)" test "$(find "$TMP/exp/raw" -name '*.json.age' | wc -l | tr -d ' ')" = "$n_rep"
exp_jsonl=("$TMP"/exp/*.jsonl)
cat "${exp_jsonl[@]}" > "$TMP/exp.all"
check "export: jsonl без IP" no_ip "$TMP/exp.all"
check "export: в jsonl нет адреса сервера" absent "$TMP/exp.all" "$SERVER_IP"
check "export: строк jsonl = строк results в БД"     test "$(wc -l < "$TMP/exp.all" | tr -d ' ')" = "$(sql 'SELECT COUNT(*) FROM results')"
bad=0
for f in "$TMP"/exp/raw/*.age; do
    srv "$AGE" -d -i /tmp/hist-key < "$f" | srv jq -e '.type == "zoo-probe-report"' >/dev/null || bad=$((bad + 1))
done
check "export: все сырые отчёты расшифровываются ключом получателя" test "$bad" = 0
check "export: без --recipients и без --no-raw — отказ" bash -c "! docker exec -e ZOO_TEST_ENV=docker '$SRV' zoo history export --tar >/dev/null 2>&1"
check "export: приватный ключ вместо получателя — отказ" \
    bash -c "! docker exec -i -e ZOO_TEST_ENV=docker '$SRV' zoo history export --tar --recipients - < '$TMP/keys/key' >/dev/null 2>&1"

# ---------- 5. scripts/history.sh через подменённый ssh ----------
mkdir -p "$TMP/shim"
cat > "$TMP/shim/ssh" <<EOF
#!/usr/bin/env bash
# подмена ssh: последний аргумент — команда на сервере, она выполняется в контейнере
exec docker exec -i -e ZOO_TEST_ENV=docker $SRV bash -c "\${@: -1}"
EOF
chmod +x "$TMP/shim/ssh"
REPO_HIST="$TMP/repo-history"
mkdir -p "$REPO_HIST"
cp "$TMP/hist/recipients.txt" "$REPO_HIST/"
month_file="$(basename "${exp_jsonl[0]}")"
# старая строка в файле месяца: слияние должно её сохранить и не задвоить остальное
printf '%s\n' '{"ts":"2026-01-01T00Z","id":"0000000000000000","mode":"remote","server":"old","proto":"x","verdict":"OK"}' \
    > "$REPO_HIST/$month_file"
hs() { PATH="$TMP/shim:$PATH" ZOO_HISTORY_DIR="$REPO_HIST" ZOO_PROBE_IMAGE="$IMG" bash "$HIST_SH" "$@"; }
hs pull root@stand > "$TMP/pull1.log" 2>&1 || { fail "history.sh pull"; cat "$TMP/pull1.log"; }
pulled=("$REPO_HIST"/*.jsonl)
check "pull: старая строка осталась" grep -q '"server":"old"' "$REPO_HIST/$month_file"
check "pull: строк = выгрузка + старая"     test "$(cat "${pulled[@]}" | wc -l | tr -d ' ')" = "$(( $(wc -l < "$TMP/exp.all" | tr -d ' ') + 1 ))"
check "pull: строки отсортированы (ts первый ключ)" files_sorted "${pulled[@]}"
check "pull: сырых отчётов столько же, сколько в БД" test "$(find "$REPO_HIST/raw" -name '*.age' | wc -l | tr -d ' ')" = "$n_rep"
check "pull: в history/ нет IP" no_ip "${pulled[@]}"
sum1="$(cat "${pulled[@]}" | md5sum)"
hs pull root@stand > "$TMP/pull2.log" 2>&1 || fail "history.sh pull (повтор)"
check "pull повторно: файлы не меняются" test "$(cat "${pulled[@]}" | md5sum)" = "$sum1"
check "pull повторно: новых .age нет" present "$TMP/pull2.log" "новых сырых отчётов 0"
hs push root@stand --tag ci-push --device ci-pusher "$REPORT" > "$TMP/push.log" 2>&1 || { fail "history.sh push"; cat "$TMP/push.log"; }
check "push: отчёт записан на сервер с новой меткой" \
    test "$(sql "SELECT COUNT(*) FROM reports WHERE tag = 'ci-push' AND device = 'ci-pusher'")" = 1
dec="$(hs decrypt -i "$TMP/keys/key" "${raws[0]}" 2>/dev/null || true)"
check "history.sh decrypt: расшифровывает сырой отчёт" has_all "$dec" '"type":' "zoo-probe-report"

# ---------- 6. админка ----------
winfo="$(srv zoo web --info --json)"
url="$(srv jq -r .url <<< "$winfo" | tr -d '\r')"
token="$(srv jq -r .token <<< "$winfo" | tr -d '\r')"
page="$(srv bash -s "$url" "$token" <<'EOS' || true
set -e
base="${1%/}"; token="$2"; jar="$(mktemp)"; trap 'rm -f "$jar"' EXIT
curl -s -o /dev/null -c "$jar" "$base/login"
lc="$(awk -v n="zoo_login_${base##*:}" '$6 == n {print $7}' "$jar")"
curl -s -o /dev/null -b "$jar" -c "$jar" --data-urlencode "token=$token" -d "lc=$lc" "$base/login"
curl -s -b "$jar" "$base/probe?rp=all"
EOS
)"
check "админка /probe: «Лучшие протоколы» с контекстом ci-mobile" has_all "$page" "Лучшие протоколы" ci-mobile
check "админка /probe: тренды и журнал (ci-server, ci-push)"     has_all "$page" "Тренды по протоколам" "История прогонов" ci-server ci-push
check "админка /probe: без inline-стилей" no_style "$page"

echo
[ "$fails" -eq 0 ] && echo "history: всё прошло" || echo "history: провалов $fails"
[ "$fails" -eq 0 ]
