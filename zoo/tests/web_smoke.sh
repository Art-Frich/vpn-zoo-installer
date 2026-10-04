#!/usr/bin/env bash
# web_smoke.sh — проверка веб-админки curl'ом на сервере (стенд docker/ или VPS).
#
#   bash zoo/tests/web_smoke.sh [--users]    # порт и токен — из zoo web --info
#   ZOO_WEB_URL=http://127.0.0.1:PORT ZOO_WEB_TOKEN=... bash web_smoke.sh
#
# Проверяет: слушает только loopback, без входа — редирект, неверный Host и токен
# отвергаются, все страницы отдаются с CSP и без inline-стилей, POST без CSRF — 403.
# --users: ещё и жизненный цикл пользователя zoo-websmoke через формы (добавить,
# отключить, включить, скачать файл, удалить) — меняет протоколы на сервере.

set -euo pipefail

if [ -z "${ZOO_WEB_URL:-}" ] || [ -z "${ZOO_WEB_TOKEN:-}" ]; then
    info="$(zoo web --info --json)"
    ZOO_WEB_URL="${ZOO_WEB_URL:-$(jq -r .url <<< "$info")}"
    ZOO_WEB_TOKEN="${ZOO_WEB_TOKEN:-$(jq -r .token <<< "$info")}"
fi
BASE="${ZOO_WEB_URL%/}"
PORT="${BASE##*:}"
WITH_USERS=0
[ "${1:-}" = "--users" ] && WITH_USERS=1

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
JAR="$TMP/jar"
PASS=0 FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }

# code PATH [curl-аргументы] — HTTP-код; тело в $TMP/body, заголовки в $TMP/head
code() {
    local path="$1"; shift
    curl -sS -o "$TMP/body" -D "$TMP/head" -w '%{http_code}' -b "$JAR" -c "$JAR" "$@" "$BASE$path"
}
csrf() { grep -o 'name="csrf" value="[^"]*"' "$TMP/body" | head -1 | sed 's/.*value="//; s/"$//'; }

echo "== $BASE"
echo "-- сеть"
listen="$(ss -Htlnp "sport = :$PORT" | awk '{print $4}')"
check "слушает только 127.0.0.1 ($listen)" test "$listen" = "127.0.0.1:$PORT"
check "на 0.0.0.0/[::] этот порт не открыт" bash -c "! ss -Htln | awk '{print \$4}' | grep -Eq '^(0\.0\.0\.0|\*|\[::\]):$PORT\$'"

echo "-- без входа"
check "GET / → 303 на /login" test "$(code /)" = 303
check "Location → /login?next=" grep -qi '^location: /login?next=' "$TMP/head"
check "/users без входа → 303" test "$(code /users)" = 303
check "POST /users без входа → 401" test "$(code /users -d name=x)" = 401
check "чужой Host → 421" test "$(code /login -H 'Host: evil.example')" = 421
check "/healthz → 200" test "$(code /healthz)" = 200

echo "-- вход"
code /login >/dev/null
lc="$(awk '$6 == "zoo_login" {print $7}' "$JAR")"
check "форма входа ставит zoo_login" test -n "$lc"
check "неверный токен → 401" test "$(code /login --data-urlencode token=wrong -d "lc=$lc")" = 401
code /login >/dev/null
lc="$(awk '$6 == "zoo_login" {print $7}' "$JAR")"
check "вход без double-submit → 400" test "$(code /login --data-urlencode "token=$ZOO_WEB_TOKEN" -d lc=forged)" = 400
code /login >/dev/null
lc="$(awk '$6 == "zoo_login" {print $7}' "$JAR")"
check "верный токен → 303" test "$(code /login --data-urlencode "token=$ZOO_WEB_TOKEN" -d "lc=$lc")" = 303
check "cookie сессии HttpOnly; SameSite=Strict" grep -qi '^set-cookie: zoo_sid=.*HttpOnly.*SameSite=Strict' "$TMP/head"

echo "-- страницы"
for p in / /users "/users?verify=1" /traffic "/traffic?period=1h" "/traffic?period=7d" "/traffic?period=30d" \
         /probe /logs /settings /users/owner "/users/owner?period=24h"; do
    c="$(code "$p")"
    if [ "$c" != 200 ]; then bad "$p → $c"; continue; fi
    if grep -q 'style=' "$TMP/body"; then bad "$p: inline style (нарушит CSP)"; continue; fi
    if ! grep -qi "^content-security-policy: default-src 'none'" "$TMP/head"; then bad "$p: нет CSP"; continue; fi
    if ! grep -qi '^cache-control: no-store' "$TMP/head"; then bad "$p: нет no-store"; continue; fi
    ok "$p (200, CSP, без inline-стилей, $(wc -c < "$TMP/body") байт)"
done
check "/static/app.css кешируется" bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $BASE/static/app.css)\" = 200 ]"
check "в журнале нет токена админки" bash -c "! curl -s -b '$JAR' '$BASE/logs' | grep -qF '$ZOO_WEB_TOKEN'"

echo "-- CSRF"
code /users >/dev/null
TOKEN_CSRF="$(csrf)"
check "CSRF-токен на странице" test -n "$TOKEN_CSRF"
check "POST без csrf → 403" test "$(code /users -d name=zoo-websmoke)" = 403
check "POST с чужим csrf → 403" test "$(code /users -d name=zoo-websmoke -d csrf=forged)" = 403
check "POST с Origin другого сайта → 403" \
    test "$(code /settings/action -d action=collect -d "csrf=$TOKEN_CSRF" -H 'Origin: http://evil.example')" = 403
check "снятие трафика из настроек → задача" \
    test "$(code /settings/action -d action=collect -d "csrf=$TOKEN_CSRF")" = 303
job="$(grep -i '^location:' "$TMP/head" | tr -d '\r' | awk '{print $2}')"
for _ in $(seq 1 20); do code "$job" >/dev/null; grep -q 'идёт' "$TMP/body" || break; sleep 1; done
check "задача $job завершилась успешно" grep -q 'готово' "$TMP/body"

if [ "$WITH_USERS" = 1 ]; then
    echo "-- пользователь через формы"
    U=zoo-websmoke
    check "добавить $U → 303 /users/$U" test "$(code /users -d "name=$U" --data-urlencode 'note=проверка веба' -d "csrf=$TOKEN_CSRF")" = 303
    check "redirect на страницу пользователя" grep -qi "^location: /users/$U" "$TMP/head"
    check "zoo user list видит $U" bash -c "zoo user list --json | jq -e '.users[] | select(.name == \"$U\")' >/dev/null"
    check "страница $U: ссылки и QR" bash -c "[ \"\$(curl -s -b '$JAR' '$BASE/users/$U' | grep -c 'class=\"link\"')\" -ge 1 ]"
    check "QR встроен как SVG" bash -c "curl -s -b '$JAR' '$BASE/users/$U' | grep -q '<div class=\"qr\"><svg'"
    check "отключить" test "$(code "/users/$U/disable" -d "csrf=$TOKEN_CSRF")" = 303
    check "в реестре отключён" bash -c "zoo user list --json | jq -e '.users[] | select(.name == \"$U\") | .enabled == false' >/dev/null"
    check "включить" test "$(code "/users/$U/enable" -d "csrf=$TOKEN_CSRF")" = 303
    check "страница подтверждения удаления" test "$(code "/users/$U/delete")" = 200
    check "удалить" test "$(code "/users/$U/delete" -d "csrf=$TOKEN_CSRF")" = 303
    check "$U удалён отовсюду" bash -c "! zoo user list --json | jq -e '.users[] | select(.name == \"$U\")' >/dev/null"
    code /users >/dev/null
    check "сообщение «пользователь удалён»" grep -q 'пользователь удалён' "$TMP/body"

    P=zoo-probe
    if zoo user list --all --json | jq -e ".users[] | select(.name == \"$P\" and .system == true)" >/dev/null; then
        echo "-- служебный пользователь $P"
        check "в списке пользователей его нет" bash -c "! curl -s -b '$JAR' '$BASE/users' | grep -q '<strong>$P</strong>'"
        check "страница $P открывается" test "$(code "/users/$P")" = 200
        check "отключить $P нельзя" test "$(code "/users/$P/disable" -d "csrf=$TOKEN_CSRF")" = 303
        check "$P остался включён" bash -c "zoo user list --all --json | jq -e '.users[] | select(.name == \"$P\") | .enabled' >/dev/null"
        check "удалить $P нельзя" test "$(code "/users/$P/delete" -d "csrf=$TOKEN_CSRF")" = 303
        check "$P на месте" bash -c "zoo user list --all --json | jq -e '.users[] | select(.name == \"$P\")' >/dev/null"
    fi
fi

echo "-- выход"
check "POST /logout → 303" test "$(code /logout -d "csrf=$TOKEN_CSRF")" = 303
check "после выхода / → 303" test "$(code /)" = 303

echo "== итог: ok $PASS, FAIL $FAIL"
[ "$FAIL" -eq 0 ]
