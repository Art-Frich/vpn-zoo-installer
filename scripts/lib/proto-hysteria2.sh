#!/usr/bin/env bash
# lib/proto-hysteria2.sh — Hysteria2 (HyNetworks/hysteria, standalone, D2).
# Функции для фазы 05, zoo и 99. source'ится после lib.sh, нужен config_load.
#
# Пользователи: auth.type=command. Hysteria на каждое подключение запускает
# /usr/local/lib/vpn-zoo/hy2-auth ADDR AUTH TX, тот ищет AUTH в /etc/hysteria/users.tsv
# и печатает имя (id для trafficStats). Почему не userpass (проверено на v2.12.3):
#   - userpass читается только при старте, SIGHUP не перечитывает → любое
#     add/del = рестарт и обрыв сессий всех пользователей; command — без рестарта;
#   - userpass не принимает строку без «:», а у владельца уже есть рабочая ссылка
#     вида hysteria2://ПАРОЛЬ@… — с command она продолжает работать.
# AUTH принимается в двух видах: «токен» (ссылки, которые мы выдаём) и «имя:токен».
#
# Контракт ACL (пишет фаза 07-routing, 05 только подключает):
#   /etc/hysteria/acl.txt          правила ACL Hysteria, по одному в строке; # и пустые
#                                  строки пропускаются. Копируются в config.yaml (acl.inline)
#   /etc/hysteria/geo/geoip.dat    \ если есть → acl.geoip / acl.geosite (без них Hysteria
#   /etc/hysteria/geo/geosite.dat  / при geoip:/geosite: в правилах сама качает чужие файлы)
#   /etc/hysteria/outbounds.yaml   YAML-фрагмент с ключами верхнего уровня только из
#                                  outbounds / resolver / sniff — вставляется как есть
# После правки этих файлов вызвать proto_hysteria2_apply: правила попадают в сам
# config.yaml, поэтому изменение видно, и при сбое старта откат возвращает и правила.
# Обновлённые geo-файлы тоже ведут к рестарту (в конфиге их размер и mtime).

HY2_ID="hysteria2"
HY2_OBFS_ID="hysteria2-obfs"
HY_ETC="${HY_ETC:-/etc/hysteria}"
HY_BIN="${HY_BIN:-/usr/local/bin/hysteria}"
HY_USERS="$HY_ETC/users.tsv"
HY_AUTH_BIN="/usr/local/lib/vpn-zoo/hy2-auth"
HY_SVC_USER="hysteria"
HY_UNIT="hysteria-server.service"
HY_OBFS_UNIT="hysteria-server@obfs.service"
HY_HOP_UNIT="vpn-zoo-hy2-hop.service"
HY_HOP_TABLE="vpnzoo_hy2_hop"
HY_UNIT_DIR="/etc/systemd/system"
HY_STATS_HDR="$VPN_ETC/hy2-stats.hdr"
HY_CLIENTS_DIR="$ZOO_CLIENTS_DIR"
HY_LOCK="/run/lock/vpn-zoo-hy2.lock"

# Имя как у остальных протоколов (zoo_user_valid из lib.sh)
_hy2_user_valid() { zoo_user_valid "${1:-}"; }
_hy2_user_require() {
    _hy2_user_valid "${1:-}" || die "hysteria2: недопустимое имя пользователя: «${1:-}» (латиница, цифры, _ . -, до 32 символов)"
}
# Токен идёт в userinfo ссылки без percent-encoding (mihomo его не декодирует)
_hy2_token_valid() { [[ "${1:-}" =~ ^[A-Za-z0-9._~-]{8,128}$ ]]; }

# ============================================================
# Бинарь, сертификат, служебные файлы
# ============================================================

hy2_installed_version() {
    [ -x "$HY_BIN" ] || return 0
    "$HY_BIN" version 2>/dev/null | awk '$1 == "Version:" {print $2; exit}'
}

# Ставит закреплённый бинарь, если sha256 установленного не совпадает с пином.
# Возврат 0 — бинарь заменён, 1 — уже был нужный
hy2_install_binary() {
    local sum cache tmp
    sum="$(version_for HY2_SHA256 "$ZOO_ARCH")"
    if [ -x "$HY_BIN" ] && [ "$(sha256sum "$HY_BIN" | awk '{print $1}')" = "$sum" ]; then
        log_info "hysteria $HY2_VERSION уже установлен (sha256 совпадает)"
        return 1
    fi
    cache="/var/cache/vpn-zoo/hysteria-${HY2_VERSION}-linux-${ZOO_ARCH}"
    download_verified "$HY2_URL_BASE/hysteria-linux-${ZOO_ARCH}" "$sum" "$cache"
    tmp="$(mktemp "${HY_BIN}.new.XXXXXX")"
    install -m 0755 "$cache" "$tmp"
    mv -f "$tmp" "$HY_BIN"
    log_ok "hysteria $(hy2_installed_version) установлен в $HY_BIN"
    return 0
}

hy2_ensure_user() {
    id "$HY_SVC_USER" >/dev/null 2>&1 && return 0
    useradd --system --home-dir /var/lib/hysteria --no-create-home \
        --shell /usr/sbin/nologin "$HY_SVC_USER" || die "не удалось создать пользователя $HY_SVC_USER"
}

# Помощник авторизации: на успешном пути только встроенные команды bash, без внешних процессов
# (logger — лишь на отказе)
hy2_install_auth_helper() {
    local tmp
    mkdir -p "$(dirname "$HY_AUTH_BIN")"
    tmp="$(mktemp "${HY_AUTH_BIN}.XXXXXX")"
    cat > "$tmp" <<'EOF'
#!/bin/bash
# vpn-zoo: auth.type=command для Hysteria2. $1=адрес клиента $2=строка auth $3=tx.
# Успех: код 0 и имя пользователя в stdout. Формат users.tsv: имя<TAB>токен<TAB>1|0
# Отказ: в журнал (тег zoo-hy2-auth) уходит только адрес клиента — это источник для zoo journal;
# строка auth (секрет) и имя не пишутся. Сама Hysteria при неверном ключе молчит.
rej() {
    case "${1-}" in *[!0-9a-fA-F:.\[\]]*|"") set -- "?" ;; esac
    [ -x /usr/bin/logger ] && /usr/bin/logger -t zoo-hy2-auth -p authpriv.notice -- "reject $1" 2>/dev/null
    exit 1
}
a="${2-}"
[ -n "$a" ] && [ "${#a}" -le 300 ] || rej "${1-}"
u="" p="$a"
case "$a" in *:*) u="${a%%:*}" p="${a#*:}" ;; esac
[ -n "$p" ] || rej "${1-}"
off=""
while IFS=$'\t' read -r n t e _; do
    [ -n "$t" ] && [ "$t" = "$p" ] || continue
    [ -z "$u" ] || [ "${u,,}" = "${n,,}" ] || continue
    [ "$e" = 1 ] || { off=1; continue; }
    printf '%s\n' "$n"
    exit 0
done < /etc/hysteria/users.tsv
# верный ключ выключенного пользователя — свой человек, не атака: адрес не пишем
[ -z "$off" ] || exit 1
rej "${1-}"
EOF
    chmod 0755 "$tmp"
    mv -f "$tmp" "$HY_AUTH_BIN"
}

# Сертификат годится: есть, ECDSA P-256 (Ed25519 ломает Chrome parrot), SAN=SNI,
# ключ от этого сертификата, живёт ещё ≥30 дней
hy2_cert_ok() {
    local c="$HY_ETC/cert.pem" k="$HY_ETC/key.pem"
    local txt
    [ -s "$c" ] && [ -s "$k" ] || return 1
    openssl x509 -in "$c" -noout -checkend 2592000 >/dev/null 2>&1 || return 1
    # через переменную: `openssl | grep -q` при pipefail ложно падает по SIGPIPE
    txt="$(openssl x509 -in "$c" -noout -text 2>/dev/null)" || return 1
    [[ "$txt" == *prime256v1* ]] || return 1
    txt="$(openssl x509 -in "$c" -noout -ext subjectAltName 2>/dev/null)" || return 1
    [[ "$txt" == *"DNS:${HY2_SNI}"* ]] || return 1
    [ "$(openssl x509 -in "$c" -noout -pubkey 2>/dev/null)" = "$(openssl pkey -in "$k" -pubout 2>/dev/null)" ] || return 1
}

# Возврат 0 — сертификат перевыпущен
hy2_ensure_cert() {
    local days="${HY2_CERT_DAYS:-3650}" kt ct
    if hy2_cert_ok; then
        chgrp "$HY_SVC_USER" "$HY_ETC/key.pem"; chmod 0640 "$HY_ETC/key.pem"; chmod 0644 "$HY_ETC/cert.pem"
        return 1
    fi
    [[ "$days" =~ ^[0-9]+$ ]] && [ "$days" -ge 30 ] && [ "$days" -le 3650 ] || days=3650
    [ -e "$HY_ETC/cert.pem" ] && backup_path "$HY_ETC/cert.pem" "$HY_ETC/key.pem" >/dev/null
    log_info "выпускаю self-signed ECDSA P-256 на ${HY2_SNI} (${days} дней)"
    kt="$(mktemp "$HY_ETC/.key.XXXXXX")"; ct="$(mktemp "$HY_ETC/.cert.XXXXXX")"
    if ! openssl ecparam -name prime256v1 -genkey -noout -out "$kt" \
        || ! openssl req -new -x509 -days "$days" -key "$kt" -out "$ct" \
            -subj "/CN=${HY2_SNI}" -addext "subjectAltName=DNS:${HY2_SNI}" 2>/dev/null; then
        rm -f "$kt" "$ct"; die "openssl: не удалось выпустить сертификат"
    fi
    chgrp "$HY_SVC_USER" "$kt"; chmod 0640 "$kt"; chmod 0644 "$ct"
    mv -f "$kt" "$HY_ETC/key.pem"; mv -f "$ct" "$HY_ETC/cert.pem"
    return 0
}

# pinSHA256 для ссылок: sha256 DER листового сертификата, hex lowercase без «:»
hy2_pin() {
    openssl x509 -in "$HY_ETC/cert.pem" -outform DER 2>/dev/null | sha256sum | awk '{print $1}'
}

hy2_stats_hdr_write() {
    ( umask 077; printf 'Authorization: %s\n' "${HY2_STATS_SECRET:?}" > "$HY_STATS_HDR" )
    chmod 600 "$HY_STATS_HDR"
}

# ============================================================
# Конфиги и юниты
# ============================================================

# Блок ACL/outbounds по контракту из шапки файла
_hy2_routing_yaml() {
    local frag="$HY_ETC/outbounds.yaml" bad g line
    if [ -f "$HY_ETC/acl.txt" ]; then
        echo "acl:"
        for g in geoip geosite; do
            [ -f "$HY_ETC/geo/$g.dat" ] || continue
            echo "  $g: $HY_ETC/geo/$g.dat  # $(stat -c '%s-%Y' "$HY_ETC/geo/$g.dat")"
        done
        echo "  inline:"
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%$'\r'}"
            [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
            # YAML single-quoted: ' удваивается
            printf "    - '%s'\n" "${line//\'/\'\'}"
        done < "$HY_ETC/acl.txt"
    else
        # ACL от 07 ещё нет (05 раньше 07, 07 упала или пропущена): без ACL Hysteria пускает
        # клиентов на 127.0.0.1 сервера — API Xray (без пароля), панель. Базовый запрет —
        # литералами CIDR: geoip: без локальных geo-файлов Hysteria скачала бы сама
        echo "acl:"
        echo "  inline:"
        printf "    - 'reject(%s)'\n" 0.0.0.0/8 127.0.0.0/8 10.0.0.0/8 100.64.0.0/10 169.254.0.0/16 \
            172.16.0.0/12 192.168.0.0/16 ::1/128 fc00::/7 fe80::/10
    fi
    if [ -f "$frag" ]; then
        bad="$(grep -E '^[^[:space:]#][^:]*:' "$frag" | cut -d: -f1 | grep -vxE 'outbounds|resolver|sniff' || true)"
        [ -z "$bad" ] || die "$frag: недопустимые ключи верхнего уровня: $(echo "$bad" | tr '\n' ' ')(можно outbounds, resolver, sniff)"
        echo "# --- $frag ---"
        cat "$frag"
    fi
}

# hy2_render INSTANCE (main|obfs) — текст config.yaml в stdout
hy2_render() {
    local inst="$1" listen stats
    if [ "$inst" = "obfs" ]; then
        listen=":$HY2_OBFS_PORT"; stats="$HY2_OBFS_STATS_PORT"
    else
        listen=":$HY2_PORT"; stats="$HY2_STATS_PORT"
    fi
    cat <<EOF
# сгенерировано vpn-zoo (05-hysteria2.sh, proto_hysteria2_apply) — правки руками перезапишутся.
# Свои правила маршрутизации: $HY_ETC/acl.txt и outbounds.yaml (см. scripts/lib/proto-hysteria2.sh)
listen: "$listen"

tls:
  cert: $HY_ETC/cert.pem
  key: $HY_ETC/key.pem
  sniGuard: dns-san

auth:
  type: command
  command: $HY_AUTH_BIN

congestion:
  type: bbr
  bbrProfile: standard
ignoreClientBandwidth: true
speedTest: false

trafficStats:
  listen: 127.0.0.1:$stats
  secret: "$HY2_STATS_SECRET"
EOF
    if [ "$inst" = "obfs" ]; then
        cat <<EOF

# Salamander: QUIC Initial не виден SNI-фильтру; HTTP/3-маскировка при этом бессмысленна
obfs:
  type: salamander
  salamander:
    password: "$HY2_OBFS_PASSWORD"
EOF
    else
        cat <<EOF

masquerade:
  type: proxy
  proxy:
    url: "$HY2_MASQ_URL"
    rewriteHost: true
EOF
    fi
    echo
    _hy2_routing_yaml
}

# Юнит: свой, не из get.hy2.sh
_hy2_unit_text() {
    local inst="$1" caps="CAP_NET_BIND_SERVICE" conf="$HY_ETC/config.yaml" desc="Hysteria2 (vpn-zoo)"
    if [ "$inst" = "template" ]; then
        conf="$HY_ETC/%i.yaml"; desc="Hysteria2 (vpn-zoo, %i)"
    fi
    cat <<EOF
# сгенерировано vpn-zoo (05-hysteria2.sh)
[Unit]
Description=$desc
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$HY_SVC_USER
Group=$HY_SVC_USER
StateDirectory=hysteria
WorkingDirectory=/var/lib/hysteria
ExecStart=$HY_BIN server --config $conf
Environment=HYSTERIA_LOG_LEVEL=${HY2_LOG_LEVEL:-error}
Environment=HYSTERIA_DISABLE_UPDATE_CHECK=1
AmbientCapabilities=$caps
CapabilityBoundingSet=$caps
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
LimitNOFILE=1048576
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

# _hy2_put FILE MODE GROUP [backup] — stdin в FILE атомарно; код 0, если содержимое
# изменилось. backup: чужой файл (без метки vpn-zoo в шапке) сначала в бэкап
_hy2_put() {
    local f="$1" mode="$2" grp="$3" bk="${4:-}" tmp head
    tmp="$(mktemp "$(dirname "$f")/.$(basename "$f").XXXXXX")"
    cat > "$tmp"
    chgrp "$grp" "$tmp"; chmod "$mode" "$tmp"
    if [ -f "$f" ] && cmp -s "$tmp" "$f"; then
        chgrp "$grp" "$f"; chmod "$mode" "$f"
        rm -f "$tmp"; return 1
    fi
    if [ "$bk" = "backup" ] && [ -f "$f" ]; then
        head="$(head -c 200 "$f")"
        [[ "$head" == *vpn-zoo* ]] || backup_path "$f" >/dev/null
    fi
    mv -f "$tmp" "$f"
    return 0
}

hy2_obfs_enabled() { [ "${ENABLE_HY2_OBFS:-0}" = "1" ]; }

# Port hopping: свой nft-redirect HY2_HOP_RANGE → HY2_PORT отдельным юнитом.
# Не встроенный `listen: :443,A-B` Hysteria: тот на ip6 требует nft fib (nft_fib_ipv6),
# сбой redirect роняет весь Hy2 вместе с 443, и сервису нужен CAP_NET_ADMIN.
# Только prerouting и только с внешнего интерфейса: пересылаемый трафик AWG-клиентов
# (iif awg*) к чужим портам A-B не трогаем; ответы открытых соединений nat не проходят.
_hy2_hop_nft() {
    cat <<EOF
# сгенерировано vpn-zoo (05-hysteria2.sh): port hopping Hysteria2
table inet $HY_HOP_TABLE {}
delete table inet $HY_HOP_TABLE
table inet $HY_HOP_TABLE {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "$HY2_HOP_IFACE" udp dport $HY2_HOP_RANGE counter redirect to :$HY2_PORT
    }
}
EOF
}

_hy2_hop_unit() {
    local nft
    nft="$(command -v nft)"
    cat <<EOF
# сгенерировано vpn-zoo (05-hysteria2.sh)
[Unit]
Description=Hysteria2 port hopping (vpn-zoo): UDP $HY2_HOP_RANGE -> $HY2_PORT
After=network-online.target nftables.service
Wants=network-online.target
Before=$HY_UNIT

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$nft -f $HY_ETC/hop.nft
ExecStop=-$nft delete table inet $HY_HOP_TABLE

[Install]
WantedBy=multi-user.target
EOF
}

# hy2_hop_apply — включить или выключить по HY2_HOP; код 1 при сбое
hy2_hop_apply() {
    local ch=1
    if [ "${HY2_HOP:-0}" != "1" ]; then
        if [ -f "${HY_UNIT_DIR:?}/${HY_HOP_UNIT:?}" ]; then
            systemctl disable --now "$HY_HOP_UNIT" >/dev/null 2>&1 || true
            rm -f "${HY_UNIT_DIR:?}/${HY_HOP_UNIT:?}" "${HY_ETC:?}/hop.nft"
            systemctl daemon-reload
            log_info "port hopping выключен"
        fi
        return 0
    fi
    command -v nft >/dev/null || { log_err "port hopping: нет nft"; return 1; }
    _hy2_hop_nft | _hy2_put "$HY_ETC/hop.nft" 0640 root && ch=0
    _hy2_hop_unit | _hy2_put "$HY_UNIT_DIR/$HY_HOP_UNIT" 0644 root && ch=0
    [ "$ch" = 0 ] && systemctl daemon-reload
    systemctl enable "$HY_HOP_UNIT" >/dev/null 2>&1 || true
    if [ "$ch" = 0 ] || ! systemctl is-active --quiet "$HY_HOP_UNIT"; then
        systemctl restart "$HY_HOP_UNIT" || { journalctl -u "$HY_HOP_UNIT" -n 10 --no-pager >&2 || true; return 1; }
    fi
    nft list table inet "$HY_HOP_TABLE" >/dev/null 2>&1
}

# Записать юниты; код 0, если что-то изменилось (нужен daemon-reload)
hy2_write_units() {
    local ch=1
    _hy2_unit_text main | _hy2_put "$HY_UNIT_DIR/$HY_UNIT" 0644 root backup && ch=0
    _hy2_unit_text template | _hy2_put "$HY_UNIT_DIR/hysteria-server@.service" 0644 root backup && ch=0
    [ "$ch" = 0 ] && systemctl daemon-reload
    return "$ch"
}

_hy2_conf_path() { if [ "$1" = "obfs" ]; then echo "$HY_ETC/obfs.yaml"; else echo "$HY_ETC/config.yaml"; fi; }
_hy2_unit_of() { if [ "$1" = "obfs" ]; then echo "$HY_OBFS_UNIT"; else echo "$HY_UNIT"; fi; }
_hy2_port_of() { if [ "$1" = "obfs" ]; then echo "$HY2_OBFS_PORT"; else echo "$HY2_PORT"; fi; }

hy2_instances() { echo main; hy2_obfs_enabled && echo obfs; return 0; }

# Сервис активен и порт слушает (до SECS секунд)
hy2_instance_healthy() {
    local inst="$1" secs="${2:-15}" unit port
    unit="$(_hy2_unit_of "$inst")"; port="$(_hy2_port_of "$inst")"
    wait_port "$port" udp "$secs" || return 1
    sleep 1
    systemctl is-active --quiet "$unit"
}

# proto_hysteria2_apply [force] — перерисовать конфиги всех инстансов и перезапустить
# изменившиеся. Если инстанс с новым конфигом не поднялся — откат на прежний конфиг.
# Рестарт рвёт сессии этого инстанса; пользователей так менять не нужно (users.tsv).
proto_hysteria2_apply() {
    local force="${1:-}" inst conf unit prev rc=0 changed txt
    for inst in $(hy2_instances); do
        conf="$(_hy2_conf_path "$inst")"; unit="$(_hy2_unit_of "$inst")"
        # сначала целиком в переменную: ошибка в фрагменте маршрутизации не должна
        # оставить на диске половину конфига
        txt="$(hy2_render "$inst")" || { log_err "hysteria2: конфиг $inst не собран"; return 1; }
        prev=""
        if [ -f "$conf" ]; then prev="$(mktemp)"; cp -p "$conf" "$prev"; fi
        changed=1
        printf '%s\n' "$txt" | _hy2_put "$conf" 0640 "$HY_SVC_USER" backup && changed=0
        systemctl enable "$unit" >/dev/null 2>&1 || true
        if [ "$changed" = 0 ] || [ "$force" = "force" ] || ! systemctl is-active --quiet "$unit"; then
            log_info "перезапуск $unit"
            systemctl restart "$unit" || true
        fi
        if ! hy2_instance_healthy "$inst" 15; then
            log_err "$unit не поднялся. Журнал:"
            journalctl -u "$unit" -n 20 --no-pager >&2 || true
            if [ -n "$prev" ] && [ "$changed" = 0 ]; then
                log_warn "$unit: возвращаю прежний конфиг $conf"
                cp -p "$prev" "$conf"
                systemctl restart "$unit" || true
                hy2_instance_healthy "$inst" 15 && log_warn "$unit работает на прежнем конфиге"
            fi
            rc=1
        fi
        [ -z "$prev" ] || rm -f "$prev"
    done
    # obfs выключили — останавливаем инстанс
    if ! hy2_obfs_enabled && systemctl is-enabled --quiet "$HY_OBFS_UNIT" 2>/dev/null; then
        systemctl disable --now "$HY_OBFS_UNIT" >/dev/null 2>&1 || true
        log_info "$HY_OBFS_UNIT остановлен (ENABLE_HY2_OBFS=0)"
    fi
    return "$rc"
}

# ============================================================
# Пользователи: users.tsv (root:hysteria 0640) + clients/<имя>/hysteria2.pass (0600)
# ============================================================

# _hy2_users_get NAME → «токен<TAB>1|0», код 1 если нет
_hy2_users_get() {
    [ -f "$HY_USERS" ] || return 1
    ZOO_HY_N="$1" awk -F'\t' '$1 == ENVIRON["ZOO_HY_N"] {print $2 "\t" $3; f=1; exit} END {exit !f}' "$HY_USERS"
}

# _hy2_users_set NAME TOKEN ENABLED | _hy2_users_set NAME "" "" (удалить). Под flock
_hy2_users_set() {
    local name="$1" tok="$2" en="$3"
    (
        exec 9>"$HY_LOCK"
        flock 9
        [ -f "$HY_USERS" ] || : > "$HY_USERS"
        {
            ZOO_HY_N="$name" awk -F'\t' '$1 != ENVIRON["ZOO_HY_N"] && NF >= 3' "$HY_USERS"
            [ -z "$tok" ] || printf '%s\t%s\t%s\n' "$name" "$tok" "$en"
        } | _hy2_put "$HY_USERS" 0640 "$HY_SVC_USER" || true
    )
}

_hy2_token_taken() {
    [ -f "$HY_USERS" ] && ZOO_HY_T="$1" awk -F'\t' '$2 == ENVIRON["ZOO_HY_T"] {f=1} END {exit !f}' "$HY_USERS"
}

_hy2_pass_file() { printf '%s/%s/hysteria2.pass\n' "$HY_CLIENTS_DIR" "$1"; }

_hy2_pass_write() { printf '%s\n' "$2" | zoo_client_file_write "$1" hysteria2.pass; }

# Отключить живые сессии пользователя через trafficStats API (POST /kick).
# Hysteria рвёт соединение при следующем пакете этого id
_hy2_kick() {
    local name="$1" inst port body
    [ -f "$HY_STATS_HDR" ] || return 0
    body="$(jq -cn --arg n "$name" '[$n]')"
    for inst in $(hy2_instances); do
        port="$HY2_STATS_PORT"; [ "$inst" = "obfs" ] && port="$HY2_OBFS_STATS_PORT"
        [ -n "$port" ] || continue
        # только тем, кто онлайн: иначе id остаётся в KickMap и оборвёт первое
        # подключение после повторного включения
        curl -fsS --max-time 5 -H @"$HY_STATS_HDR" "http://127.0.0.1:$port/online" 2>/dev/null \
            | jq -e --arg n "$name" 'has($n)' >/dev/null 2>&1 || continue
        curl -fsS --max-time 5 -H @"$HY_STATS_HDR" -X POST -d "$body" "http://127.0.0.1:$port/kick" >/dev/null 2>&1 \
            || log_warn "hysteria2: kick $name на 127.0.0.1:$port не удался"
    done
}

# Манифест обновляем, только если фаза уже писала его (zoo вызывает функции и до установки)
_hy2_manifest_touch() {
    [ -f "$(manifest_path "$HY2_ID")" ] || return 0
    proto_hysteria2_manifest_refresh
}

# proto_hysteria2_user_add NAME [TOKEN] — идемпотентно; без рестарта сервиса
proto_hysteria2_user_add() {
    local name="${1:-}" tok="${2:-}" pf cur
    _hy2_user_require "$name"
    if cur="$(_hy2_users_get "$name")"; then
        _hy2_pass_write "$name" "${cur%%$'\t'*}"
        return 0
    fi
    pf="$(_hy2_pass_file "$name")"
    if [ -z "$tok" ] && [ -f "$pf" ]; then tok="$(head -1 "$pf")"; fi
    if [ -n "$tok" ] && { ! _hy2_token_valid "$tok" || _hy2_token_taken "$tok"; }; then
        log_warn "hysteria2: токен для $name недопустим или занят — генерирую новый"
        tok=""
    fi
    while [ -z "$tok" ] || _hy2_token_taken "$tok"; do tok="$(gen_random_alnum 24)"; done
    _hy2_users_set "$name" "$tok" 1
    _hy2_pass_write "$name" "$tok"
    _hy2_manifest_touch
}

proto_hysteria2_user_del() {
    local name="${1:-}"
    _hy2_user_require "$name"
    [ "$name" != "owner" ] || die "hysteria2: owner не удаляется (можно отключить: user_enable owner false)"
    _hy2_users_get "$name" >/dev/null || { log_warn "hysteria2: пользователя $name нет"; return 0; }
    _hy2_users_set "$name" "" ""
    zoo_client_file_del "$name" hysteria2.pass
    _hy2_kick "$name"
    _hy2_manifest_touch
}

# proto_hysteria2_user_enable NAME true|false
proto_hysteria2_user_enable() {
    local name="${1:-}" want="${2:-}" cur en
    _hy2_user_require "$name"
    case "$want" in true|1) en=1 ;; false|0) en=0 ;; *) die "hysteria2: user_enable NAME true|false" ;; esac
    cur="$(_hy2_users_get "$name")" || die "hysteria2: пользователя $name нет"
    _hy2_users_set "$name" "${cur%%$'\t'*}" "$en"
    [ "$en" = 1 ] || _hy2_kick "$name"
    _hy2_manifest_touch
}

# «имя<TAB>true|false» по строке
proto_hysteria2_user_list() {
    [ -f "$HY_USERS" ] || return 0
    awk -F'\t' 'NF >= 3 {print $1 "\t" ($3 == 1 ? "true" : "false")}' "$HY_USERS"
}

_hy2_user_names() { proto_hysteria2_user_list | cut -f1; }

proto_hysteria2_user_enabled() {
    local cur
    cur="$(_hy2_users_get "${1:-}")" || return 1
    [ "${cur#*$'\t'}" = 1 ]
}

_hy2_token_of() {
    local cur
    cur="$(_hy2_users_get "$1")" || { log_err "hysteria2: пользователя $1 нет"; return 1; }
    printf '%s\n' "${cur%%$'\t'*}"
}

# ============================================================
# Ссылки, probe, манифест, трафик
# ============================================================

_hy2_host() {
    case "${SERVER_IP:?}" in *:*) printf '[%s]\n' "$SERVER_IP" ;; *) printf '%s\n' "$SERVER_IP" ;; esac
}

# proto_hysteria2_links NAME — основная ссылка; + hop и obfs, если включены.
# Вид проверен по коду 7 клиентов (research §4.3): токен в userinfo, insecure=1 + pinSHA256
proto_hysteria2_links() {
    local name="${1:-owner}" tok host pin q
    _hy2_user_require "$name"
    tok="$(_hy2_token_of "$name")" || return 1
    proto_hysteria2_user_enabled "$name" || log_warn "hysteria2: $name отключён — ссылки сейчас не работают" >&2
    host="$(_hy2_host)"; pin="${HY2_PIN:-$(hy2_pin)}"
    q="sni=${HY2_SNI}&insecure=1&pinSHA256=${pin}"
    printf 'hysteria2://%s@%s:%s/?%s#%s-hy2-%s\n' "$tok" "$host" "$HY2_PORT" "$q" "${LABEL:-vpn}" "$name"
    if [ "${HY2_HOP:-0}" = "1" ]; then
        # мульти-порт в authority — официальный URI; v2rayN/NG его не понимают (research §4.3)
        printf 'hysteria2://%s@%s:%s,%s/?%s#%s-hy2hop-%s\n' "$tok" "$host" "$HY2_PORT" "$HY2_HOP_RANGE" "$q" "${LABEL:-vpn}" "$name"
    fi
    if hy2_obfs_enabled && [ -n "${HY2_OBFS_PORT:-}" ]; then
        printf 'hysteria2://%s@%s:%s/?%s&obfs=salamander&obfs-password=%s#%s-hy2obfs-%s\n' \
            "$tok" "$host" "$HY2_OBFS_PORT" "$q" "$HY2_OBFS_PASSWORD" "${LABEL:-vpn}" "$name"
    fi
}

# proto_hysteria2_probe NAME [main|obfs] — {kind:"hysteria", client:{…}}: client — готовый
# конфиг клиента Hysteria (JSON = YAML), пробнику остаётся добавить socks5/http
proto_hysteria2_probe() {
    local name="${1:-owner}" inst="${2:-main}" tok port hop=""
    _hy2_user_require "$name"
    tok="$(_hy2_token_of "$name")" || return 1
    port="$HY2_PORT"; [ "$inst" = "obfs" ] && port="$HY2_OBFS_PORT"
    [ "$inst" = "main" ] && [ "${HY2_HOP:-0}" = "1" ] && hop="$HY2_HOP_RANGE"
    jq -cn --arg u "$name" --arg tok "$tok" --arg srv "$(_hy2_host):$port" --arg sni "$HY2_SNI" \
        --arg pin "${HY2_PIN:-$(hy2_pin)}" --arg inst "$inst" --arg opw "${HY2_OBFS_PASSWORD:-}" \
        --arg hop "$hop" --arg v "${HY2_INSTALLED_VERSION:-${HY2_VERSION:-}}" '
        {kind: "hysteria", user: $u, version: $v,
         client: ({server: $srv, auth: $tok,
                   tls: {sni: $sni, insecure: true, pinSHA256: $pin}}
                  + (if $inst == "obfs" then {obfs: {type: "salamander", salamander: {password: $opw}}} else {} end))}
        + (if $hop != "" then {hop: {server: "\($srv),\($hop)", hopInterval: "30s"}} else {} end)'
}

# Пользователь для probe в манифесте: owner, иначе первый включённый
_hy2_probe_user() {
    if proto_hysteria2_user_enabled owner; then echo owner; return 0; fi
    awk -F'\t' '$3 == 1 {print $1; exit}' "$HY_USERS" 2>/dev/null
}

_hy2_manifest_json() {
    local inst="$1" id name port svc u links="[]" link probe="{}" pu notes evar=ENABLE_HY2
    if [ "$inst" = "obfs" ]; then
        id="$HY2_OBFS_ID"; name="Hysteria2 + Salamander"; port="$HY2_OBFS_PORT"; svc="$HY_OBFS_UNIT"; evar=ENABLE_HY2_OBFS
        notes="Salamander-обфускация, отдельный UDP-порт, без HTTP/3-маскировки. Клиенты: hysteria, sing-box, mihomo, v2rayN/NG (не gecko). insecure=1 + pinSHA256 обязательны: сертификат self-signed."
    else
        id="$HY2_ID"; name="Hysteria2"; port="$HY2_PORT"; svc="$HY_UNIT"
        notes="Self-signed сертификат (SNI ${HY2_SNI}): в ссылке insecure=1 + pinSHA256 (hex). Без пина не работают v2rayN/NG и Happ на новом Xray. Запасной UDP-канал; на части мобильных сетей QUIC режут."
        [ "${HY2_HOP:-0}" = "1" ] && notes="$notes Port hopping: ${HY2_PORT},${HY2_HOP_RANGE} (ссылка hy2hop — только hysteria/sing-box/mihomo)."
    fi
    for u in $(_hy2_user_names); do
        proto_hysteria2_user_enabled "$u" || continue
        while IFS= read -r link; do
            case "$link" in
                *"-hy2obfs-$u") [ "$inst" = "obfs" ] || continue ;;
                *) [ "$inst" = "main" ] || continue ;;
            esac
            links="$(jq -c --arg u "$u" --arg l "$link" '. + [{user: $u, uri: $l}]' <<< "$links")"
        done < <(proto_hysteria2_links "$u" 2>/dev/null)
    done
    pu="$(_hy2_probe_user)"
    [ -z "$pu" ] || probe="$(proto_hysteria2_probe "$pu" "$inst")"
    jq -cn --arg id "$id" --arg name "$name" --argjson port "$port" --arg svc "$svc" \
\
        --argjson links "$links" --argjson probe "$probe" --arg notes "$notes" \
        --arg en "${ENABLE_HY2:-1}" --arg evar "$evar" --arg ver "${HY2_INSTALLED_VERSION:-${HY2_VERSION:-}}" --arg pin "${HY2_PIN:-$(hy2_pin)}" --arg sni "$HY2_SNI" \
        --arg hop "$( [ "$inst" = main ] && [ "${HY2_HOP:-0}" = "1" ] && echo "$HY2_HOP_RANGE")" '
        {id: $id, name: $name, layer: "udp", port: $port, engine: "hysteria", version: $ver,
         phase: "05-hysteria2", enable_var: $evar, service: $svc, enabled: ($en == "1"), users_backend: "hysteria-command",
         links: $links, files: [], probe: $probe, notes: $notes,
         tls: {sni: $sni, pinSHA256: $pin, self_signed: true}}
        + (if $hop != "" then {hop_ports: $hop} else {} end)'
}

proto_hysteria2_manifest_refresh() {
    manifest_write "$HY2_ID" "$(_hy2_manifest_json main)"
    if hy2_obfs_enabled; then
        manifest_write "$HY2_OBFS_ID" "$(_hy2_manifest_json obfs)"
    else
        manifest_del "$HY2_OBFS_ID"
    fi
}

# Выключить Hysteria2 (ENABLE_HY2=0 после установки): юниты остановлены, UDP-порты
# закрыты, манифест enabled=false. Файлы и пользователи остаются — ENABLE_HY2=1 вернёт всё
proto_hysteria2_disable() {
    local u
    for u in "$HY_UNIT" "$HY_OBFS_UNIT" "$HY_HOP_UNIT"; do
        systemctl disable --now "$u" >/dev/null 2>&1 || true
    done
    [ -z "${HY2_PORT:-}" ] || fw_revoke "$HY2_PORT/udp"
    [ -z "${HY2_OBFS_PORT:-}" ] || fw_revoke "$HY2_OBFS_PORT/udp"
    manifest_del "$HY2_OBFS_ID"
    if [ -f "$(manifest_path "$HY2_ID")" ]; then
        manifest_write "$HY2_ID" "$(manifest_get "$HY2_ID" | jq -c '.enabled = false')"
    fi
}

# JSON-строки {user, up, down}: накопительно с последнего старта сервиса (сумма по
# инстансам). up — от клиента к серверу (tx клиента), down — к клиенту
proto_hysteria2_traffic() {
    local inst port
    [ -f "$HY_STATS_HDR" ] || return 0
    for inst in $(hy2_instances); do
        port="$HY2_STATS_PORT"; [ "$inst" = "obfs" ] && port="$HY2_OBFS_STATS_PORT"
        curl -fsS --max-time 5 -H @"$HY_STATS_HDR" "http://127.0.0.1:$port/traffic" 2>/dev/null || echo '{}'
    done | jq -cs 'map(to_entries[]) | group_by(.key)[]
        | {user: .[0].key, up: (map(.value.tx) | add), down: (map(.value.rx) | add)}'
}

# Число активных подключений по пользователям: {"имя": N}
proto_hysteria2_online() {
    local inst port
    [ -f "$HY_STATS_HDR" ] || { echo '{}'; return 0; }
    for inst in $(hy2_instances); do
        port="$HY2_STATS_PORT"; [ "$inst" = "obfs" ] && port="$HY2_OBFS_STATS_PORT"
        curl -fsS --max-time 5 -H @"$HY_STATS_HDR" "http://127.0.0.1:$port/online" 2>/dev/null || echo '{}'
    done | jq -cs 'reduce (.[] | to_entries[]) as $e ({}; .[$e.key] += $e.value)'
}
