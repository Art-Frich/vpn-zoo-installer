#!/usr/bin/env bash
# 99-print-creds.sh — итог установки по манифестам /etc/vpn-setup/protocols.d/*.json:
# ссылки и QR пользователя owner по каждому протоколу, .conf AmneziaWG (QR — Android-вариант
# со списком приложений, D31), приложения через VPN, доступ к панели через ssh -L, какие
# клиенты с чем работают.
#
#   /root/CREDENTIALS.md (0600)              то же в Markdown (путь: VPN_CREDENTIALS_OUT)
#   /etc/vpn-setup/probe-export.json (0600)  пакет клиентского пробника (zoo export-probe:
#                                            креды zoo-probe, IP сервера, итог самопроверки)
# Секреты печатаются в терминал. ZOO_CREDS_NO_QR=1 — без QR.

# shellcheck disable=SC2153 # PANEL_* приходят из config.env (config_load)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_root
config_load
command -v jq >/dev/null || die "нет jq (фаза 00 не выполнена?)"

CRED_OUT="${VPN_CREDENTIALS_OUT:-/root/CREDENTIALS.md}"
PROBE_EXPORT="${PROBE_EXPORT:-$VPN_ETC/probe-export.json}"
WHO="owner"
# Порядок вывода: основной протокол первым, запасные ниже
ORDER="vless-reality vless-xhttp hysteria2 hysteria2-obfs amneziawg ss2022 tuic"

[ -n "${SERVER_IP:-}" ] || die "SERVER_IP не задан в $CONFIG_FILE"
if ! command -v qrencode >/dev/null; then
    apt_install qrencode >/dev/null 2>&1 || log_warn "qrencode не установился — QR не будет"
fi
# install.sh работает с umask 022: временные файлы с секретами не должны быть 0644 даже на миг
umask 077

# Пользователь zoo — во всех включённых протоколах (ARCHITECTURE §5): протокол, включённый
# после установки (ENABLE_SS=1, ENABLE_TUIC=1), получает уже заведённых пользователей
if command -v zoo >/dev/null && [ -f "$VPN_ETC/users.json" ]; then
    if zoo user sync --json >/dev/null 2>&1; then
        log_info "пользователи zoo сверены с включёнными протоколами (zoo user sync)"
    else
        log_warn "zoo user sync завершился с ошибками — подробности: zoo user sync"
    fi
    # приложения через VPN (D31): реестр, Android-варианты AWG и правила v2rayN всех пользователей
    if zoo allow apply --json >/dev/null 2>&1; then
        log_info "приложения через VPN: файлы пользователей пересобраны (zoo allow apply)"
    else
        log_warn "zoo allow apply завершился с ошибками — подробности: zoo allow apply"
    fi
fi

ids=()
for id in $ORDER; do
    [ -f "$(manifest_path "$id")" ] && ids+=("$id")
done
while IFS= read -r id; do
    case " $ORDER " in *" $id "*) ;; *) ids+=("$id") ;; esac
done < <(manifest_list)
[ "${#ids[@]}" -gt 0 ] || die "нет ни одного манифеста в $MANIFEST_DIR — фазы протоколов не выполнены?"

M=()
for id in "${ids[@]}"; do
    if m="$(manifest_get "$id" | jq -c .)"; then
        M+=("$m")
    else
        log_warn "манифест $id не читается — пропускаю"
    fi
done

qr() {
    [ "${ZOO_CREDS_NO_QR:-0}" != "1" ] && command -v qrencode >/dev/null || return 0
    qrencode -t ANSIUTF8 -m 1 "$@" 2>/dev/null || log_warn "QR не построился (слишком длинные данные?)"
}

# Ссылки owner и файлы owner из манифеста
m_links() { jq -r --arg u "$WHO" '[.links[]? | select(.user == $u and (.enabled != false))][] | .uri' <<< "$1"; }
m_files() { jq -r --arg u "$WHO" '[.files[]? | select(.user == $u and (.enabled != false))][] | .path' <<< "$1"; }

ssh_port="$(ssh_login_port)"
ssh_user="${SUDO_USER:-root}"
ssh_host="$SERVER_IP"; [[ "$ssh_host" == *:* ]] && ssh_host="[$ssh_host]"
ssh_p=""; [ "${ssh_port:-22}" = "22" ] || ssh_p=" -p $ssh_port"
panel_path="${PANEL_PATH#/}"; panel_path="${panel_path%/}"
fwd="-L 127.0.0.1:${PANEL_PORT:-?}:127.0.0.1:${PANEL_PORT:-?}"
[ -z "${ZOO_WEB_PORT:-}" ] || fwd="$fwd -L 127.0.0.1:$ZOO_WEB_PORT:127.0.0.1:$ZOO_WEB_PORT"
SSH_CMD="ssh -N$ssh_p $fwd $ssh_user@$ssh_host"
PANEL_URL="http://127.0.0.1:${PANEL_PORT:-?}/${panel_path:+$panel_path/}"

# Матрица клиентов (research/2026-10-04/vpn-news §6.1, сервер Xray 26.9.30): id|работают|не работают
CLIENTS='vless-reality|v2rayN ≥7.25, v2rayNG ≥2.2.6, Happ, INCY, Streisand, Throne (ядро Xray); mihomo ≥1.19.31 (Clash Verge Rev, FlClash) — через подписку/YAML|sing-box и всё на нём (SFA/SFI, Hiddify, NekoBox, Karing), Shadowrocket
vless-xhttp|v2rayN, v2rayNG, Happ, Throne; mihomo ≥1.19.22 (подписка/YAML)|sing-box, Hiddify, NekoBox, Shadowrocket
hysteria2|hysteria 2.12, v2rayN/v2rayNG (pinSHA256), Happ, mihomo, Throne|NekoBox и Hiddify игнорируют пин (только insecure) — не рекомендуются
hysteria2-obfs|hysteria, sing-box, mihomo, v2rayN/v2rayNG|клиенты без Salamander
amneziawg|AmneziaVPN ≥5.0.1.5 (vpn:// или .conf), AmneziaWG ≥2.0, WG Tunnel ≥4.2, DefaultVPN (iOS, vpn://), mihomo ≥1.19.14|обычный WireGuard
ss2022|v2rayN/v2rayNG, Happ, Hiddify, sing-box, mihomo (SS-2022 multi-user)|клиенты без SS-2022
tuic|sing-box/SFA/SFI, Hiddify, Karing, NekoBox, v2rayN (ядро sing-box), mihomo|v2rayNG и клиенты на ядре Xray'

clients_for() { awk -F'|' -v id="$1" -v col="$2" '$1 == id {print $col}' <<< "$CLIENTS"; }

ALLOW_ANDROID="$(zoo_allowlist android "$WHO" 2>/dev/null || true)"
ALLOW_WINDOWS="$(zoo_allowlist windows "$WHO" 2>/dev/null || true)"
V2RAYN_FILE="$ZOO_CLIENTS_DIR/$WHO/v2rayn-routing.json"

ADVICE=(
    "Через VPN идут только приложения из списка (zoo allow list): Android — QR amneziawg-android (AmneziaWG или WG Tunnel), Windows — правила v2rayN. Банки, Госуслуги, MAX и остальное работают мимо VPN: настраивать для них ничего не нужно."
    "Приложения из списка лучше поставить до сканирования QR. Пакет, которого нет на телефоне, Android по коду просто пропускает (на телефоне не проверено); поставленное позже попадёт в VPN после перезапуска туннеля."
    "Brave — из Google Play или с GitHub, не из RuStore; не делать браузером по умолчанию; поиск — не Яндекс. «Блокировать соединения без VPN» в Android не включать: RU-приложения останутся без сети."
    "Факт VPN приложения на телефоне видят всё равно. Список прячет адрес сервера: приложения вне списка ходят напрямую и его не узнают."
    "REALITY на сервере — Xray 26.9.30: клиенты на sing-box (SFA/SFI, Hiddify, NekoBox, Karing) показывают «подключено», но трафика нет. Для VLESS/XHTTP — клиенты на ядре Xray ≥26.x (v2rayN, v2rayNG, Happ, INCY)."
    "Если вместо AmneziaWG — Happ или v2rayNG: режим «только выбранные приложения», пароль на локальный прокси (Happ — auto) или его выключение (v2rayNG), «Разрешить LAN» не включать. У AmneziaWG локального прокси нет, у WG Tunnel — только в режиме «Локальный прокси» (оставить режим VPN, «Блокировку» тоже не выбирать)."
    "Пользователям: $REPO_ROOT/docs/USER-GUIDE.md (по платформам). Подробно о рисках: $REPO_ROOT/docs/RISK-REDUCTION.md, раздел 4."
)

# ------------------------------------------------------------
# 1. Терминал
# ------------------------------------------------------------

line() { printf '%s\n' "================================================================"; }

echo
printf '%b\n' "${C_GREEN}$(line)${C_RESET}"
printf '%b\n' "${C_GREEN} VPN-зоопарк: доступы (пользователь $WHO)${C_RESET}"
printf '%b\n' "${C_GREEN}$(line)${C_RESET}"
echo "  Сервер: $SERVER_IP   метка: ${LABEL:-vpn}"
echo "  Ссылки всех пользователей: $ZOO_CLIENTS_DIR/<имя>/, манифесты: $MANIFEST_DIR/"

for m in "${M[@]}"; do
    id="$(jq -r '.id' <<< "$m")"
    name="$(jq -r '.name // .id' <<< "$m")"
    port="$(jq -r '.port // "?"' <<< "$m")"
    layer="$(jq -r '.layer // "?"' <<< "$m")"
    echo
    if [ "$(jq -r '.enabled != false' <<< "$m")" != "true" ]; then
        printf '%b\n' "${C_BLUE}-- $name ($layer/$port) — выключен --${C_RESET}"
        continue
    fi
    printf '%b\n' "${C_BLUE}-- $name ($layer/$port) --${C_RESET}"
    if [ "$id" = "amneziawg" ]; then
        awg_android=""; awg_plain=""
        while IFS= read -r f; do
            case "$f" in "") ;; *-android.conf) awg_android="$f" ;; *) awg_plain="$f" ;; esac
        done < <(m_files "$m")
        if [ -n "$awg_android" ]; then
            echo "  Android — AmneziaWG или WG Tunnel, через VPN только: ${ALLOW_ANDROID:-?}"
            echo "  QR для телефона (сканировать в приложении):"
            if [ -s "$awg_android" ]; then qr -r "$awg_android"; fi
            echo "  файл: $awg_android"
            [ ! -s "${awg_android%.conf}.png" ] || echo "  QR картинкой: ${awg_android%.conf}.png"
        fi
        if [ -n "$awg_plain" ]; then
            echo "  компьютер, iPhone, AmneziaVPN ≥5.0.1.5 — весь трафик через VPN: $awg_plain"
            [ ! -s "${awg_plain%.conf}.png" ] || echo "  QR картинкой: ${awg_plain%.conf}.png"
            # без Android-варианта (пустой список) — QR общего, как раньше
            if [ -z "$awg_android" ] && [ -s "$awg_plain" ]; then qr -r "$awg_plain"; fi
        fi
        while IFS= read -r l; do
            [ -n "$l" ] || continue
            echo "  ключ AmneziaVPN / DefaultVPN:"
            printf '%b\n' "${C_CYAN}$l${C_RESET}"
        done < <(m_links "$m")
    else
        n=0
        while IFS= read -r l; do
            [ -n "$l" ] || continue
            n=$((n + 1))
            printf '%b\n' "${C_CYAN}$l${C_RESET}"
            qr "$l"
        done < <(m_links "$m")
        [ "$n" -gt 0 ] || log_warn "у $WHO нет ссылки $id (zoo user add $WHO / install.sh --phase …)"
    fi
    ok="$(clients_for "$id" 2)"
    [ -z "$ok" ] || echo "  клиенты: $ok"
    no="$(clients_for "$id" 3)"
    [ -z "$no" ] || echo "  не работают: $no"
done

echo
printf '%b\n' "${C_BLUE}-- Приложения через VPN (остальное — мимо) --${C_RESET}"
echo "  Android (AmneziaWG, WG Tunnel): ${ALLOW_ANDROID:-—}"
echo "  Windows (v2rayN): ${ALLOW_WINDOWS:-—}"
[ ! -s "$V2RAYN_FILE" ] || echo "  правила для v2rayN: $V2RAYN_FILE (v2rayN → Настройки маршрутизации → Импорт правил из файла)"
echo "  изменить: zoo allow list | add | del | reset (--user ИМЯ — свой список) или админка → «Приложения»"

echo
if [ -n "${ZOO_WEB_PORT:-}" ]; then
    printf '%b\n' "${C_BLUE}-- Админка zoo: один раз на своём компьютере --${C_RESET}"
    zoo_raw="https://raw.githubusercontent.com/Art-Frich/vpn-zoo-installer/main/tools"
    echo "  Windows:      iwr $zoo_raw/zoo-admin.ps1 -OutFile zoo-admin.ps1"
    echo "                powershell -ExecutionPolicy Bypass -File .\\zoo-admin.ps1 -Setup $ssh_user@$ssh_host${ssh_p:+ -Port $ssh_port}"
    echo "  macOS/Linux:  curl -fsSLO $zoo_raw/zoo-admin.sh && bash zoo-admin.sh --setup $ssh_user@$ssh_host$ssh_p"
    echo "  дальше каждый день: zoo-admin (админка в браузере) · ssh zoo (консоль сервера)"
    echo
fi
printf '%b\n' "${C_BLUE}-- Панель 3x-ui (только через SSH-туннель) --${C_RESET}"
echo "  1) на своём компьютере: $SSH_CMD"
echo "  2) в браузере:          $PANEL_URL"
echo "  логин: ${PANEL_USER:-?}   пароль: ${PANEL_PASS:-?}"
[ "${PANEL_2FA:-0}" != "1" ] || echo "  2FA включена: секрет PANEL_2FA_SECRET в $CONFIG_FILE"
[ -z "${ZOO_WEB_PORT:-}" ] || echo "  админка zoo: http://127.0.0.1:$ZOO_WEB_PORT/ (тот же туннель), токен: ${ZOO_WEB_TOKEN:-?}"
if [ -f "$SSH_STATE_DIR/pending.env" ]; then
    log_warn "перенос SSH ждёт подтверждения (фаза 01b): команды выше — на текущий порт $ssh_port, после подтверждения $CRED_OUT обновится сам"
fi

echo
printf '%b\n' "${C_BLUE}-- Клиентам --${C_RESET}"
for a in "${ADVICE[@]}"; do echo "  * $a"; done
echo
echo "  Всё это же: $CRED_OUT; данные для пробника: $PROBE_EXPORT"
printf '%b\n' "${C_GREEN}$(line)${C_RESET}"

# ------------------------------------------------------------
# 2. CREDENTIALS.md
# ------------------------------------------------------------

md_escape() { sed 's/|/\\|/g'; }

{
    echo "# Доступы VPN-зоопарка — $(date -Iseconds)"
    echo
    echo "> Секреты сервера $SERVER_IP. Не публиковать и не пересылать целиком."
    echo
    echo "## Протоколы (пользователь $WHO)"
    for m in "${M[@]}"; do
        id="$(jq -r '.id' <<< "$m")"
        echo
        echo "### $(jq -r '.name // .id' <<< "$m") — $(jq -r '.layer // "?"' <<< "$m")/$(jq -r '.port // "?"' <<< "$m")"
        echo
        if [ "$(jq -r '.enabled != false' <<< "$m")" != "true" ]; then
            echo "Выключен (флаг ENABLE_* = 0)."
            continue
        fi
        echo "- id: \`$id\`, движок: \`$(jq -r '.engine // "?"' <<< "$m")\`, сервис: \`$(jq -r '.service // "?"' <<< "$m")\`"
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            case "$f" in
                *-android.conf) echo "- Android (AmneziaWG, WG Tunnel), через VPN только список приложений: \`$f\`" ;;
                *) echo "- конфиг: \`$f\`" ;;
            esac
            [ ! -s "${f%.conf}.png" ] || echo "- QR: \`${f%.conf}.png\`"
        done < <(m_files "$m")
        links="$(m_links "$m")"
        if [ -n "$links" ]; then
            echo
            echo '```text'
            printf '%s\n' "$links"
            echo '```'
        fi
        notes="$(jq -r '.notes // empty' <<< "$m")"
        [ -z "$notes" ] || { echo; echo "$notes"; }
    done
    echo
    echo "QR в терминале: \`qrencode -t ansiutf8 'ссылка'\` или \`zoo links $WHO --qr\`."
    echo
    echo "## Приложения через VPN"
    echo
    echo "Через VPN идут только эти приложения, всё остальное (банки, Госуслуги, MAX) — напрямую (D31):"
    echo
    echo "- Android (QR \`amneziawg-android\` для AmneziaWG и WG Tunnel): \`${ALLOW_ANDROID:-—}\`"
    echo "- Windows (правила v2rayN): \`${ALLOW_WINDOWS:-—}\`"
    [ ! -s "$V2RAYN_FILE" ] || echo "- правила v2rayN: \`$V2RAYN_FILE\` → v2rayN, «Настройки маршрутизации» → «Добавить набор правил» → «Импорт правил из файла»"
    echo "- изменить: \`sudo zoo allow list --catalog\`, \`zoo allow add youtube\`, \`zoo allow del telegram --user ИМЯ\`, \`zoo allow reset\`; или админка → «Приложения». После изменения пользователю нужен новый QR"
    echo "- приложения из списка — поставить до включения туннеля; отсутствующий пакет Android по коду пропускает (не проверено на телефоне)"
    echo "- инструкция для пользователей: \`$REPO_ROOT/docs/USER-GUIDE.md\`"
    echo
    echo "## Панель 3x-ui"
    echo
    echo "Слушает только 127.0.0.1. Туннель с вашего компьютера:"
    echo
    echo '```sh'
    echo "$SSH_CMD"
    echo '```'
    echo
    echo "- адрес: $PANEL_URL"
    echo "- логин: \`${PANEL_USER:-?}\`"
    echo "- пароль: \`${PANEL_PASS:-?}\`"
    [ "${PANEL_2FA:-0}" != "1" ] || echo "- 2FA: секрет \`PANEL_2FA_SECRET\` в \`$CONFIG_FILE\`"
    [ -z "${ZOO_WEB_PORT:-}" ] || echo "- админка zoo: http://127.0.0.1:$ZOO_WEB_PORT/ (тот же туннель), токен: \`${ZOO_WEB_TOKEN:-?}\`"
    echo
    echo "## Какие клиенты с чем работают"
    echo
    echo "| Протокол | Работают | Не работают |"
    echo "|---|---|---|"
    for m in "${M[@]}"; do
        id="$(jq -r '.id' <<< "$m")"
        [ "$(jq -r '.enabled != false' <<< "$m")" = "true" ] || continue
        ok="$(clients_for "$id" 2 | md_escape)"; no="$(clients_for "$id" 3 | md_escape)"
        [ -n "$ok$no" ] || continue
        echo "| $(jq -r '.name // .id' <<< "$m" | md_escape) | $ok | $no |"
    done
    echo
    for a in "${ADVICE[@]}"; do echo "- $a"; done
    echo
    echo "## Открытые порты (ufw, реестр $PORTS_FILE)"
    echo
    echo "| Порт | Кто | Режим |"
    echo "|---|---|---|"
    [ ! -f "$PORTS_FILE" ] || awk -F'\t' 'NF >= 2 {printf "| %s | %s | %s |\n", $1, $2, ($4 == "" ? "allow" : $4)}' "$PORTS_FILE"
    echo
    echo "## Проверка протоколов"
    echo
    echo "- с самого сервера, «работает в принципе»: \`sudo zoo probe --local --summary\` (её же печатает install.sh в конце)"
    echo "- с вашей машины, «блокирует ли провайдер»: пакет \`$PROBE_EXPORT\` (ключи служебного пользователя zoo-probe и итог самопроверки) → контейнер zoo-probe, см. docker/probe/README.md:"
    echo
    echo '```sh'
    echo "git clone https://github.com/Art-Frich/vpn-zoo-installer.git && cd vpn-zoo-installer"
    echo "mkdir probe && scp${ssh_p:+ -P $ssh_port} root@$ssh_host:$PROBE_EXPORT probe/probe-export.json"
    echo "docker build -f docker/probe.Dockerfile -t zoo-probe ."
    echo "docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v \"\$PWD/probe:/data\" zoo-probe"
    echo '```'
    echo
    echo "Windows PowerShell: в \`docker run\` — \`-v \"\${PWD}\probe:/data\"\`. Вход root по SSH запрещён — README, «Если вход root по SSH запрещён»."
    echo
    echo "## Пользователи и админка"
    echo
    echo "- новый пользователь во всех протоколах: \`sudo zoo user add ИМЯ --note \"кто\"\`, его ссылки и QR: \`sudo zoo links ИМЯ --qr\`"
    echo "- админка: \`sudo zoo web --info\` (туннель, адрес, токен); трафик: \`sudo zoo traffic\`; состояние: \`sudo zoo status\`"
    echo "- клон репозитория на сервере: \`$REPO_ROOT\` (не удалять: из него \`zoo upgrade\` и \`scripts/install.sh\`)"
    echo
    echo "## Файлы на сервере"
    echo
    echo "- конфиг и секреты: \`$CONFIG_FILE\`"
    echo "- манифесты протоколов: \`$MANIFEST_DIR/\`"
    echo "- ссылки пользователей: \`$ZOO_CLIENTS_DIR/<имя>/\`"
    echo "- для пробника: \`$PROBE_EXPORT\`"
    echo "- журналы установки: \`$LOG_DIR/\`"
} > "$CRED_OUT.tmp.$$"
chmod 600 "$CRED_OUT.tmp.$$"
mv -f "$CRED_OUT.tmp.$$" "$CRED_OUT"

# ------------------------------------------------------------
# 3. probe-export.json
# ------------------------------------------------------------

# Через zoo — креды служебного пользователя zoo-probe и итог последней самопроверки
# (install.sh после 99 перезапишет пакет свежей самопроверкой); без zoo — probe owner
# из манифестов
if ! { command -v zoo >/dev/null && zoo export-probe --quiet --out "$PROBE_EXPORT" 2>/dev/null; }; then
    printf '%s\n' "${M[@]}" | jq -s --arg ip "$SERVER_IP" --arg lbl "${LABEL:-vpn}" --arg ts "$(date -Iseconds)" '{
        version: 1, generated: $ts, server_ip: $ip, label: $lbl,
        protocols: [.[] | select(.probe != null and .probe != {})
            | {id, name, layer, port, engine, enabled: (.enabled != false), probe}]}' > "$PROBE_EXPORT.tmp.$$"
    chmod 600 "$PROBE_EXPORT.tmp.$$"
    mv -f "$PROBE_EXPORT.tmp.$$" "$PROBE_EXPORT"
fi
n="$(jq '[.protocols[] | select(.probe != null)] | length' "$PROBE_EXPORT")"
[ "$n" -gt 0 ] || log_warn "ни в одном манифесте нет probe — пробнику нечего проверять"

log_ok "CREDENTIALS.md: $CRED_OUT (0600)"
log_ok "экспорт для пробника: $PROBE_EXPORT ($n протоколов)"
