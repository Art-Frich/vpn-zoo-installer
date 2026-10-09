#!/usr/bin/env bash
# lib/routing.sh — анти-утечки (фазы 07-routing и 08-warp, таймер geo, zoo).
# source'ится после lib.sh и lib/xui.sh, нужен config_load.
#
# Что делает (DECISIONS D9, RISK-REDUCTION §5):
#   Xray     — шаблон 3x-ui: private/bittorrent → blocked, echo-сервисы → warp|blocked,
#              RU (category-ru, tld-ru, geoip ru) → RU_EGRESS; sniffing routeOnly на всех inbound
#   Hysteria — /etc/hysteria/acl.txt с теми же правилами (RU — по HY2_RU_EGRESS)
#   AWG      — только L3: nft-таблица с RU-сетями, REJECT из awg* (AWG_RU_EGRESS=block)
#   geo      — runetfreedom: при установке закреплённый релиз (versions.env), дальше
#              таймер берёт latest, проверяет sha256 + категории + xray -test, при сбое откат
#   WARP     — встроенная регистрация 3x-ui + wireguard-outbound «warp» (фаза 08)
#
# Значения config.env: RU_EGRESS, HY2_RU_EGRESS, AWG_RU_EGRESS = direct|block|warp;
# ENABLE_BITTORRENT = 0 (блок, по умолчанию)|1; ROUTING_ECHO_EXTRA — доп. домены echo через запятую
# (только правкой config.env: install.sh не переносит ROUTING_* из окружения);
# WARP_* — ключи и состояние WARP (WARP_READY=1 после успешной проверки).

ROUTING_GEO_DIR="${ROUTING_GEO_DIR:-/var/lib/vpn-zoo/geo}"
ROUTING_HY_DIR="${ROUTING_HY_DIR:-/etc/hysteria}"
ROUTING_HY_ACL="${ROUTING_HY_ACL:-$ROUTING_HY_DIR/acl.txt}"
ROUTING_HY_CONFIG="${ROUTING_HY_CONFIG:-$ROUTING_HY_DIR/config.yaml}"
ROUTING_HY_GEOIP="$ROUTING_GEO_DIR/hy-geoip.dat"
ROUTING_HY_GEOSITE="$ROUTING_GEO_DIR/hy-geosite.dat"
ROUTING_AWG_NFT="${ROUTING_AWG_NFT:-$VPN_ETC/awg-ru.nft}"
ROUTING_AWG_TABLE="vpnzoo_awg_ru"
ROUTING_SHARE="${ROUTING_SHARE:-/usr/local/lib/vpn-zoo}"
ROUTING_UPDATER="/usr/local/sbin/vpn-zoo-geo-update"
ROUTING_GEO_SRC="${ROUTING_GEO_SRC:-https://github.com/runetfreedom/russia-v2ray-rules-dat/releases}"

# Категории, без которых правила не работают: их наличие проверяется в каждом
# новом geo-файле (тег geosite:ru в runetfreedom НЕ существует — только category-ru)
ROUTING_GEOSITE_CODES="category-ip-geo-detect,category-ru,tld-ru,ru-blocked"
ROUTING_GEOIP_CODES="ru,private"

# Echo-сервисы вне category-ip-geo-detect (тесты утечек и RU-сервисы «мой IP»)
ROUTING_ECHO_BUILTIN="browserleaks.com,browserleaks.net,whatleaks.com,dnsleaktest.com,2ip.me,2ip.kz,eth0.me,ifconfig.ru,ip.nf,ipof.me,ifconfig.so,ip.gs,api.myip.com"

# ---------- значения политики ----------

_routing_norm() {
    case "${1:-}" in
        direct|"") echo direct ;;
        block|blocked|reject) echo block ;;
        warp) echo warp ;;
        *) return 1 ;;
    esac
}

routing_warp_ready() {
    [ "${ENABLE_WARP:-0}" = "1" ] && [ "${WARP_READY:-0}" = "1" ] && [ -n "${WARP_PRIV:-}" ]
}

# Эффективное значение: warp без готового WARP → block (безопаснее, чем direct:
# владелец выбрал «не показывать IP сервера», значит лучше не открыть, чем слить)
routing_effective() {
    local v
    v="$(_routing_norm "${1:-direct}")" || { log_warn "routing: неизвестное значение «$1» — считаю block"; v=block; }
    if [ "$v" = "warp" ] && ! routing_warp_ready; then v=block; fi
    echo "$v"
}

routing_echo_egress() { if routing_warp_ready; then echo warp; else echo block; fi; }

# Домены echo: встроенные + ROUTING_ECHO_EXTRA, без путей, по одному в строке
routing_echo_domains() {
    printf '%s,%s\n' "$ROUTING_ECHO_BUILTIN" "${ROUTING_ECHO_EXTRA:-}" \
        | tr ',' '\n' | sed 's#/.*##; s/^[[:space:]]*//; s/[[:space:]]*$//' \
        | grep -E '^[A-Za-z0-9.-]+\.[A-Za-z0-9-]+$' | sort -u
}

# Адреса самого сервера (SERVER_IP + адреса интерфейсов без loopback и link-local), по одному
# в строке. Клиент прокси, идущий на них, попадает в локальные сервисы мимо ufw: пакет
# приходит через lo, а ufw пропускает lo целиком. geoip:private публичный адрес не покрывает
routing_self_ips() {
    { printf '%s\n' "${SERVER_IP:-}"; ip -o addr show 2>/dev/null | awk '{split($4, a, "/"); print a[1]}' || true; } \
        | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ { if ($0 !~ /^127\./) print; next }
               /^[0-9A-Fa-f:]+$/ && /:/ { l = tolower($0); if (l != "::1" && l !~ /^fe80:/) print }' \
        | sort -u
}

# SSH-порты через запятую: к ним на адрес сервера из туннеля можно (ssh из-под VPN)
_routing_ssh_ports() {
    tr ',' '\n' <<< "${SSH_PORTS:-22}" | tr -d ' ' | awk '/^[0-9]+$/ && $1 >= 1 && $1 <= 65535' | sort -un | paste -sd, -
}

# ---------- geo-файлы ----------

# Разбор .dat (protobuf v2ray geoip/geosite) без зависимостей:
#   filter SRC DST code1,code2  — оставить только эти категории (для Hysteria: полный
#                                 geosite_RU даёт ~500 МБ RSS, отфильтрованный ~45 МБ)
#   cidrs SRC code              — CIDR категории geoip построчно (для nft AWG)
#   codes SRC                   — список категорий
_routing_geodat() {
    python3 - "$@" <<'PY'
import ipaddress, sys

def varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7f) << s
        if c < 0x80:
            return r, i
        s += 7

def fields(b):
    i, n = 0, len(b)
    while i < n:
        key, i = varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = varint(b, i)
        elif wt == 2:
            ln, i = varint(b, i)
            v = b[i:i + ln]; i += ln
            if len(v) != ln:
                raise ValueError("обрезанный файл")
        elif wt == 5:
            v = b[i:i + 4]; i += 4
        elif wt == 1:
            v = b[i:i + 8]; i += 8
        else:
            raise ValueError("неизвестный wire type %d" % wt)
        yield f, wt, v

def entries(path):
    data = open(path, 'rb').read()
    for f, wt, v in fields(data):
        if f != 1 or wt != 2:
            raise ValueError("не geoip/geosite .dat")
        code = None
        for f2, wt2, v2 in fields(v):
            if f2 == 1 and wt2 == 2:
                code = v2.decode().lower(); break
        yield code, v

def enc_varint(n):
    out = bytearray()
    while True:
        c = n & 0x7f; n >>= 7
        if n:
            out.append(c | 0x80)
        else:
            out.append(c); return bytes(out)

try:
    cmd = sys.argv[1]
    if cmd == 'filter':
        src, dst, want = sys.argv[2], sys.argv[3], [c.lower() for c in sys.argv[4].split(',')]
        got = {}
        for code, raw in entries(src):
            if code in want:
                got[code] = raw
        miss = [c for c in want if c not in got]
        if miss:
            sys.exit("%s: нет категорий %s" % (src, ",".join(miss)))
        with open(dst, 'wb') as o:
            for c in want:
                o.write(b'\x0a' + enc_varint(len(got[c])) + got[c])
    elif cmd == 'cidrs':
        src, want = sys.argv[2], sys.argv[3].lower()
        for code, raw in entries(src):
            if code != want:
                continue
            out = []
            for f, wt, v in fields(raw):
                if f != 2:
                    continue
                ip, pfx = b'', 0
                for f3, wt3, v3 in fields(v):
                    if f3 == 1: ip = v3
                    elif f3 == 2: pfx = v3
                out.append(str(ipaddress.ip_network((ip, pfx), strict=False)))
            sys.stdout.write("\n".join(out) + "\n")
            break
        else:
            sys.exit("%s: нет категории %s" % (src, want))
    elif cmd == 'codes':
        sys.stdout.write("\n".join(c for c, _ in entries(sys.argv[2]) if c) + "\n")
    else:
        sys.exit("geodat: неизвестная команда " + cmd)
except (ValueError, IndexError, OSError) as e:
    sys.exit("geodat: %s" % e)
PY
}

_routing_sha() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

# Правила, которыми проверяются geo-файлы и шаблон (те же категории, что в шаблоне)
_routing_test_config() {
    jq -n --arg gs "$ROUTING_GEOSITE_CODES" --arg gi "$ROUTING_GEOIP_CODES" '{
        log: {loglevel: "warning"},
        outbounds: [{tag: "direct", protocol: "freedom"}, {tag: "blocked", protocol: "blackhole"}],
        routing: {rules: (
            [$gs | split(",")[] | {type: "field", domain: ["ext:geosite_RU.dat:" + .], outboundTag: "blocked"}] +
            [$gi | split(",")[] | {type: "field", ip: ["ext:geoip_RU.dat:" + .], outboundTag: "blocked"}] +
            [{type: "field", ip: ["geoip:private"], outboundTag: "blocked"}])}}'
}

# routing_geo_validate DIR — DIR содержит geoip_RU.dat/geosite_RU.dat-кандидаты:
# размер, разбор, нужные категории, xray -test с реальными правилами
routing_geo_validate() {
    local dir="$1" xray cfg asset rc=0
    xray="$(xui_xray_bin)"
    [ -s "$dir/geoip_RU.dat" ] && [ -s "$dir/geosite_RU.dat" ] || { log_err "geo: нет файлов в $dir"; return 1; }
    # Порог — примерно половина нынешних размеров (geoip 18 МБ, geosite 74 МБ)
    [ "$(stat -c %s "$dir/geoip_RU.dat")" -gt 5000000 ] || { log_err "geo: geoip_RU.dat подозрительно мал"; return 1; }
    [ "$(stat -c %s "$dir/geosite_RU.dat")" -gt 20000000 ] || { log_err "geo: geosite_RU.dat подозрительно мал"; return 1; }
    _routing_geodat filter "$dir/geosite_RU.dat" /dev/null "$ROUTING_GEOSITE_CODES" || return 1
    _routing_geodat filter "$dir/geoip_RU.dat" /dev/null "$ROUTING_GEOIP_CODES" || return 1
    asset="$(mktemp -d)"
    cfg="$asset/test.json"
    ln -s "$(readlink -f "$dir/geoip_RU.dat")" "$asset/geoip_RU.dat"
    ln -s "$(readlink -f "$dir/geosite_RU.dat")" "$asset/geosite_RU.dat"
    ln -s "$XUI_DIR/bin/geoip.dat" "$asset/geoip.dat"
    ln -s "$XUI_DIR/bin/geosite.dat" "$asset/geosite.dat"
    _routing_test_config > "$cfg"
    XRAY_LOCATION_ASSET="$asset" "$xray" run -test -c "$cfg" >"$asset/out" 2>&1 || rc=1
    [ "$rc" = 0 ] || log_err "geo: xray -test не принял файлы: $(tail -1 "$asset/out")"
    rm -rf "$asset"
    return "$rc"
}

# Производные файлы из проверенных: урезанные .dat для Hysteria, CIDR для AWG
routing_geo_derive() {
    local bin="$XUI_DIR/bin" tmp
    ( umask 022; mkdir -p "$ROUTING_GEO_DIR" )
    chmod 755 "$ROUTING_GEO_DIR" "$(dirname "$ROUTING_GEO_DIR")"
    tmp="$(mktemp "$ROUTING_GEO_DIR/.hy.XXXXXX")"
    _routing_geodat filter "$bin/geosite_RU.dat" "$tmp" "$ROUTING_GEOSITE_CODES" || { rm -f "$tmp"; return 1; }
    chmod 644 "$tmp"; mv -f "$tmp" "$ROUTING_HY_GEOSITE"
    tmp="$(mktemp "$ROUTING_GEO_DIR/.hy.XXXXXX")"
    _routing_geodat filter "$bin/geoip_RU.dat" "$tmp" "$ROUTING_GEOIP_CODES" || { rm -f "$tmp"; return 1; }
    chmod 644 "$tmp"; mv -f "$tmp" "$ROUTING_HY_GEOIP"
    tmp="$(mktemp "$ROUTING_GEO_DIR/.ru.XXXXXX")"
    _routing_geodat cidrs "$bin/geoip_RU.dat" ru > "$tmp" || { rm -f "$tmp"; return 1; }
    [ "$(wc -l < "$tmp")" -gt 5000 ] || { rm -f "$tmp"; log_err "geo: в geoip ru меньше 5000 сетей"; return 1; }
    chmod 644 "$tmp"; mv -f "$tmp" "$ROUTING_GEO_DIR/ru-cidr.txt"
}

_routing_geo_state() {
    local source="$1" tag="$2"
    jq -n --arg src "$source" --arg tag "$tag" \
        --arg gi "$(_routing_sha "$XUI_DIR/bin/geoip_RU.dat")" --arg gs "$(_routing_sha "$XUI_DIR/bin/geosite_RU.dat")" \
        --arg at "$(date -Iseconds)" \
        '{source: $src, tag: $tag, geoip_sha256: $gi, geosite_sha256: $gs, updated_at: $at}' \
        > "$ROUTING_GEO_DIR/state.json"
}

# Установить кандидатов из DIR в bin 3x-ui (атомарно, старые — в prev/)
_routing_geo_install() {
    local dir="$1" f
    ( umask 022; mkdir -p "$ROUTING_GEO_DIR/prev" )
    for f in geoip_RU.dat geosite_RU.dat; do
        [ -f "$XUI_DIR/bin/$f" ] && cp -f "$XUI_DIR/bin/$f" "$ROUTING_GEO_DIR/prev/$f"
        install -m 644 "$dir/$f" "$XUI_DIR/bin/.$f.new"
        mv -f "$XUI_DIR/bin/.$f.new" "$XUI_DIR/bin/$f"
    done
}

_routing_geo_rollback() {
    local f
    for f in geoip_RU.dat geosite_RU.dat; do
        [ -f "$ROUTING_GEO_DIR/prev/$f" ] || continue
        install -m 644 "$ROUTING_GEO_DIR/prev/$f" "$XUI_DIR/bin/.$f.new"
        mv -f "$XUI_DIR/bin/.$f.new" "$XUI_DIR/bin/$f"
    done
}

# Фаза 07: файлы в bin 3x-ui должны пройти проверку. Совпали с закреплёнными (versions.env)
# или их уже обновил таймер и они валидны — оставляем. Иначе качаем закреплённый релиз.
routing_geo_ensure() {
    local bin="$XUI_DIR/bin" cache tmpd
    if [ "$(_routing_sha "$bin/geoip_RU.dat")" = "$GEO_RU_GEOIP_SHA256" ] \
       && [ "$(_routing_sha "$bin/geosite_RU.dat")" = "$GEO_RU_GEOSITE_SHA256" ]; then
        log_info "geo: файлы runetfreedom совпадают с закреплённым релизом $GEO_RU_TAG"
        routing_geo_validate "$bin" || die "geo: закреплённые файлы не прошли проверку"
        [ -f "$ROUTING_GEO_DIR/state.json" ] || { mkdir -p "$ROUTING_GEO_DIR"; _routing_geo_state pinned "$GEO_RU_TAG"; }
    elif routing_geo_validate "$bin" 2>/dev/null; then
        log_info "geo: файлы новее закреплённых (обновлены таймером) и проходят проверку — оставляю"
    else
        log_warn "geo: файлы в $bin отсутствуют или не проходят проверку — ставлю закреплённый релиз $GEO_RU_TAG"
        cache=/var/cache/vpn-zoo/geo-$GEO_RU_TAG
        download_verified "$ROUTING_GEO_SRC/download/$GEO_RU_TAG/geoip.dat" "$GEO_RU_GEOIP_SHA256" "$cache/geoip_RU.dat"
        download_verified "$ROUTING_GEO_SRC/download/$GEO_RU_TAG/geosite.dat" "$GEO_RU_GEOSITE_SHA256" "$cache/geosite_RU.dat"
        routing_geo_validate "$cache" || die "geo: закреплённый релиз $GEO_RU_TAG не прошёл проверку"
        tmpd="$cache"
        _routing_geo_install "$tmpd"
        mkdir -p "$ROUTING_GEO_DIR"
        _routing_geo_state pinned "$GEO_RU_TAG"
    fi
    routing_geo_derive || die "geo: не удалось подготовить файлы для Hysteria/AWG"
}

# Таймер: latest с GitHub → sha256sum релиза → проверка → установка → рестарт Xray
# (и Hysteria) → самопроверка. Любой сбой после установки — откат на прежние файлы.
#   routing_geo_update [--force]   (--force — установить, даже если sha не изменился)
routing_geo_update() {
    local tmpd rc=0
    tmpd="$(mktemp -d /var/tmp/vpn-zoo-geo.XXXXXX)"
    _routing_geo_update "$tmpd" "${1:-}" || rc=$?
    rm -rf "$tmpd"
    return "$rc"
}

_routing_geo_update() {
    local tmpd="$1" force=0 tag f sum got changed=0
    [ "${2:-}" = "--force" ] && force=1
    tag="$(curl -fsS -o /dev/null --max-time 30 -w '%{redirect_url}' "$ROUTING_GEO_SRC/latest/download/geoip.dat" \
        | sed -n 's#.*/releases/download/\([^/]*\)/.*#\1#p')"
    [ -n "$tag" ] || { log_err "geo: не удалось узнать последний релиз runetfreedom"; return 1; }
    for f in geoip geosite; do
        sum="$(curl -fsSL --max-time 60 "$ROUTING_GEO_SRC/download/$tag/$f.dat.sha256sum" | awk '{print $1}')"
        [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || { log_err "geo: нет sha256sum для $f.dat ($tag)"; return 1; }
        if [ "$force" = 0 ] && [ "$(_routing_sha "$XUI_DIR/bin/${f}_RU.dat")" = "$sum" ]; then
            cp -f "$XUI_DIR/bin/${f}_RU.dat" "$tmpd/${f}_RU.dat"
            continue
        fi
        retry 3 curl -fsSL --connect-timeout 15 --max-time 600 -o "$tmpd/${f}_RU.dat" "$ROUTING_GEO_SRC/download/$tag/$f.dat" \
            || { log_err "geo: не скачался $f.dat ($tag)"; return 1; }
        got="$(_routing_sha "$tmpd/${f}_RU.dat")"
        [ "$got" = "$sum" ] || { log_err "geo: sha256 $f.dat не совпал ($got != $sum)"; return 1; }
        changed=1
    done
    if [ "$changed" = 0 ]; then
        log_info "geo: релиз $tag уже установлен"
        [ -s "$ROUTING_HY_GEOSITE" ] || routing_geo_derive
        return 0
    fi
    routing_geo_validate "$tmpd" || { log_err "geo: релиз $tag отвергнут, остаются прежние файлы"; return 1; }
    _routing_geo_install "$tmpd"
    if ! { routing_geo_derive && _routing_restart_dataplanes; }; then
        log_err "geo: после установки $tag сервисы не поднялись — откат"
        _routing_geo_rollback
        routing_geo_derive || true
        _routing_restart_dataplanes || true
        return 1
    fi
    _routing_geo_state latest "$tag"
    log_ok "geo: установлен релиз runetfreedom $tag"
}

# Перечитать geo: Xray (через API), Hysteria (копии в /etc/hysteria/geo + рестарт), nft AWG
_routing_restart_dataplanes() {
    local ok=0
    if systemctl is-active --quiet x-ui; then
        { xui_xray_restart && routing_xray_wait_running 30; } || ok=1
    fi
    if [ -d "$ROUTING_HY_DIR" ]; then
        routing_hy2_apply || ok=1
    fi
    if [ -f "$ROUTING_AWG_NFT" ]; then
        routing_awg_apply || ok=1
    fi
    return "$ok"
}

# ---------- Xray ----------

routing_xray_wait_running() {
    local secs="${1:-30}" st=""
    for _ in $(seq 1 "$secs"); do
        st="$(xui_xray_state 2>/dev/null || true)"
        [ "$st" = "running" ] && return 0
        sleep 1
    done
    log_err "Xray не запустился (state=${st:-?}): $(xui_server_status 2>/dev/null | jq -r '.xray.errorMsg // empty')"
    return 1
}

# Endpoint WARP: без IPv6 на хосте — IPv4-литерал. Xray резолвит engage.cloudflareclient.com
# и в A, и в AAAA и может выбрать v6: на хосте без v6 рукопожатие молча не проходит
# (проверено на стенде: «sendto: network is unreachable», 3x-ui #5205)
routing_warp_endpoint() {
    local host="${WARP_ENDPOINT:-engage.cloudflareclient.com:2408}" port v4
    if ip -6 route show default 2>/dev/null | grep -q .; then
        echo "$host"; return 0
    fi
    port="${host##*:}"; [[ "$port" =~ ^[0-9]+$ ]] || port=2408
    v4="${WARP_ENDPOINT_V4:-}"
    [ -n "$v4" ] || v4="$(getent ahostsv4 "${host%:*}" 2>/dev/null | awk 'NR==1 {print $1}')"
    if [ -n "$v4" ]; then echo "$v4:$port"; else echo "$host"; fi
}

# Outbound «warp» в формате фронтенда 3x-ui (buildWarpOutbound) для Xray 26.9.30
routing_warp_outbound() {
    jq -cn --arg sk "$WARP_PRIV" --arg pk "$WARP_PEER_PUB" --arg ep "$(routing_warp_endpoint)" \
        --arg v4 "$WARP_V4" --arg v6 "${WARP_V6:-}" --arg res "${WARP_RESERVED:-}" '{
        tag: "warp", protocol: "wireguard",
        settings: {
            mtu: 1280, noKernelTun: true, secretKey: $sk,
            address: ([$v4 + "/32"] + (if $v6 != "" then [$v6 + "/128"] else [] end)),
            reserved: ($res | split(",") | map(select(. != "") | tonumber)),
            peers: [{publicKey: $pk, endpoint: $ep, allowedIPs: ["0.0.0.0/0", "::/0"]}],
            domainStrategy: "ForceIPv4v6"
        }}'
}

# Локальный socks-вход для Hysteria → warp и для самопроверки WARP (только 127.0.0.1, пароль)
routing_warp_inbound() {
    jq -cn --argjson port "$WARP_SOCKS_PORT" --arg u "$WARP_SOCKS_USER" --arg p "$WARP_SOCKS_PASS" '{
        tag: "zoo-warp-in", listen: "127.0.0.1", port: $port, protocol: "socks",
        settings: {auth: "password", udp: true, ip: "127.0.0.1", accounts: [{user: $u, pass: $p}]}}'
}

# Новый шаблон из текущего: наши правила (ruleTag zoo-*) — сразу после api, остальное
# (правила владельца) сохраняется ниже. Печатает JSON.
# finalRules у direct: адрес назначения уже после резолва (домен на 127.0.0.1 или на IP
# сервера тоже ловится). Свои правила там узнаём по geoip:private или адресам сервера в ip.
#   ROUTING_WARP_PROBE=1 — включить outbound warp, даже если WARP ещё не проверен (08)
routing_xray_template_build() {
    local cur="$1" ru echo bt warp_ob="null" warp_in="null" with_warp=0
    ru="$(routing_effective "${RU_EGRESS:-direct}")"
    echo="$(routing_echo_egress)"
    bt="$(_routing_bt_policy)"
    if [ "${ENABLE_WARP:-0}" = "1" ] && [ -n "${WARP_PRIV:-}" ] && [ -n "${WARP_SOCKS_PORT:-}" ] \
       && { routing_warp_ready || [ "${ROUTING_WARP_PROBE:-0}" = "1" ]; }; then
        with_warp=1
        warp_ob="$(routing_warp_outbound)"
        warp_in="$(routing_warp_inbound)"
    fi
    jq -c --arg ru "$ru" --arg echo "$echo" --arg bt "$bt" --argjson ww "$with_warp" \
        --argjson wob "$warp_ob" --argjson win "$warp_in" \
        --arg self "$(routing_self_ips | paste -sd, -)" --arg sshp "$(_routing_ssh_ports)" \
        --arg extra "$(routing_echo_domains | sed 's/^/domain:/' | paste -sd, -)" '
        def tagof($v): if $v == "warp" then "warp" elif $v == "block" then "blocked" else "direct" end;
        def default_rule: (.ruleTag == null) and (.outboundTag == "blocked")
            and ((.ip == ["geoip:private"] and (.domain == null) and (.protocol == null))
                 or (.protocol == ["bittorrent"] and (.ip == null) and (.domain == null)));
        (.routing.rules // []) as $rules
        | ($rules | map(select((.inboundTag // []) | index("api")))) as $api
        | ($rules | map(select(((.inboundTag // []) | index("api")) | not)
                    | select(((.ruleTag // "") | startswith("zoo-")) | not)
                    | select(default_rule | not))) as $owner
        | ([{type: "field", ruleTag: "zoo-private", ip: ["geoip:private"], outboundTag: "blocked"}]
           + (if $ww == 1 then [{type: "field", ruleTag: "zoo-warp-in", inboundTag: ["zoo-warp-in"], outboundTag: "warp"}] else [] end)
           + (if $bt == "block" then [{type: "field", ruleTag: "zoo-bittorrent", protocol: ["bittorrent"], outboundTag: "blocked"}] else [] end)
           + [{type: "field", ruleTag: "zoo-echo",
               domain: (["ext:geosite_RU.dat:category-ip-geo-detect"] + ($extra | split(",") | map(select(. != "")))),
               outboundTag: tagof($echo)}]
           + (if $ru != "direct" then [
                {type: "field", ruleTag: "zoo-ru-blocked", domain: ["ext:geosite_RU.dat:ru-blocked"], outboundTag: "direct"},
                {type: "field", ruleTag: "zoo-ru-domain",
                 domain: ["ext:geosite_RU.dat:category-ru", "ext:geosite_RU.dat:tld-ru", "domain:su", "domain:xn--p1ai"],
                 outboundTag: tagof($ru)},
                {type: "field", ruleTag: "zoo-ru-ip", ip: ["ext:geoip_RU.dat:ru"], outboundTag: tagof($ru)}]
              else [] end)) as $zoo
        | .routing.rules = ($api + $zoo + $owner)
        | .routing.domainStrategy = (if $ru != "direct" then "IPIfNonMatch" else "AsIs" end)
        | .log = ((.log // {}) + {access: "none", dnsLog: false})
        | .outbounds = ((.outbounds // []) | map(select(.tag != "warp")))
        | (if any(.outbounds[]; .tag == "direct") then . else .outbounds = [{tag: "direct", protocol: "freedom", settings: {}}] + .outbounds end)
        | (if any(.outbounds[]; .tag == "blocked") then . else .outbounds += [{tag: "blocked", protocol: "blackhole", settings: {}}] end)
        | ($self | split(",") | map(select(. != ""))) as $sip
        | .outbounds |= map(if .tag == "direct" and .protocol == "freedom" then
              .settings = ((.settings // {}) as $s | $s + {finalRules: (
                  (if ($sip | length) > 0 and $sshp != "" then [{action: "allow", network: "tcp", port: $sshp, ip: $sip}] else [] end)
                  + [{action: "block", ip: (["geoip:private"] + $sip)}]
                  + (($s.finalRules // []) | map(select(any((.ip // [])[]; . == "geoip:private" or (. as $x | $sip | index($x) != null)) | not))))})
            else . end)
        | (if $ww == 1 then .outbounds += [$wob] else . end)
        | .inbounds = ((.inbounds // []) | map(select(.tag != "zoo-warp-in")))
        | (if $ww == 1 then .inbounds += [$win] else . end)
        ' <<< "$cur"
}

_routing_bt_policy() {
    case "${ENABLE_BITTORRENT:-0}" in
        1) echo allow ;;
        *) echo block ;;
    esac
}

# Проверка шаблона тем же Xray до отправки в панель
routing_xray_template_test() {
    local tmpl="$1" tmp rc=0
    tmp="$(mktemp -d)"
    printf '%s' "$tmpl" > "$tmp/config.json"
    ( cd "$XUI_DIR/bin" && XRAY_LOCATION_ASSET="$XUI_DIR/bin" "$(xui_xray_bin)" run -test -c "$tmp/config.json" ) >"$tmp/out" 2>&1 || rc=1
    [ "$rc" = 0 ] || log_err "routing: xray -test не принял шаблон: $(grep -v '^$' "$tmp/out" | tail -1)"
    rm -rf "$tmp"
    return "$rc"
}

# Применить шаблон. Если Xray после этого не поднялся — вернуть прежний.
routing_xray_apply() {
    local cur new
    cur="$(xui_xray_template_get)" || { log_err "routing: не удалось прочитать шаблон Xray"; return 1; }
    new="$(routing_xray_template_build "$cur")" || { log_err "routing: не удалось собрать шаблон"; return 1; }
    if [ "$(jq -S . <<< "$cur")" = "$(jq -S . <<< "$new")" ] && [ "$(xui_xray_state)" = "running" ]; then
        log_info "routing: шаблон Xray уже актуален"
        return 0
    fi
    routing_xray_template_test "$new" || return 1
    # xray/update сам перезапускает работающий Xray
    xui_xray_template_set "$new" || { log_err "routing: панель не приняла шаблон"; return 1; }
    if ! routing_xray_wait_running 30 && ! { xui_xray_restart && routing_xray_wait_running 30; }; then
        log_err "routing: Xray не стартует с новым шаблоном — возвращаю прежний"
        xui_xray_template_set "$cur" || true
        xui_xray_restart || true
        routing_xray_wait_running 30 || true
        return 1
    fi
    log_ok "routing: шаблон Xray применён (echo → $(routing_echo_egress), RU → $(routing_effective "${RU_EGRESS:-direct}"), bittorrent → $(_routing_bt_policy))"
}

# Какой outbound выберет Xray: routing_route_test domain=api.ipify.org [inboundTag=...]
# Печатает тег (пусто = первый outbound, direct)
routing_route_test() {
    xui_api_form xray/routeTest "$@" | jq -r '.obj.outboundTag // ""'
}

# Sniffing на всех inbound Xray (mtproto — не Xray, его обслуживает mtg): без него сервер не видит доменов и правила выше
# не работают. routeOnly — домен только для маршрутизации, назначение не переписывается.
# Печатает число исправленных inbound.
routing_sniffing_fix() {
    local list id ib fixed=0 want
    want='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":true}'
    list="$(xui_inbound_list)" || return 1
    for id in $(jq -r '.[] | select(.protocol != "wireguard" and .protocol != "tunnel" and .protocol != "mtproto") | .id' <<< "$list"); do
        ib="$(jq -c --argjson id "$id" '.[] | select(.id == $id)' <<< "$list")"
        if jq -e --argjson w "$want" '(.sniffing // {}) as $s
              | $s.enabled == true and $s.routeOnly == true and ($s.metadataOnly // false) == false
                and (["http","tls","quic"] - ($s.destOverride // []) | length) == 0
                and (($s.destOverride // []) | index("fakedns") | not)' <<< "$ib" >/dev/null; then
            continue
        fi
        ib="$(xui_inbound_get "$id")" || return 1
        jq -c --argjson w "$want" '.sniffing = $w' <<< "$ib" | xui_api POST "inbounds/update/$id" - >/dev/null \
            || { log_err "routing: не удалось включить sniffing на inbound $id"; return 1; }
        log_info "routing: sniffing (routeOnly) включён на inbound $id ($(jq -r '.remark' <<< "$ib"))" >&2
        fixed=$((fixed + 1))
    done
    echo "$fixed"
}

# ---------- Hysteria ----------
# Контракт с 05-hysteria2 (шапка lib/proto-hysteria2.sh): 07 пишет файлы, 05 подключает их
# в config.yaml при рендере (proto_hysteria2_apply — рестарт с откатом):
#   /etc/hysteria/acl.txt            → acl.file
#   /etc/hysteria/geo/geo{ip,site}.dat → acl.geoip / acl.geosite (урезанные: полный
#                                      geosite_RU.dat даёт Hysteria ~500 МБ RSS, урезанный ~45 МБ)
#   /etc/hysteria/outbounds.yaml     → sniff (+ outbounds direct/warp при готовом WARP)
# /etc/hysteria не создаём: каталог без Hysteria сломал бы guard_foreign_install фазы 05.

# Текст ACL (первое совпадение сверху вниз). Встроенные outbound Hysteria: direct, reject
routing_hy2_acl_text() {
    local echo ru d out_e out_r
    echo="$(routing_echo_egress)"
    ru="$(routing_effective "${HY2_RU_EGRESS:-direct}")"
    out_e="$([ "$echo" = warp ] && echo warp || echo reject)"
    out_r="$(case "$ru" in warp) echo warp ;; block) echo reject ;; *) echo direct ;; esac)"
    echo "# vpn-zoo 07-routing — генерируется, правки затрутся (install.sh --phase 07)"
    # адреса сервера: мимо ufw к локальным сервисам нельзя, SSH можно (как finalRules Xray)
    while read -r d; do
        [ -n "$d" ] || continue
        case "$d" in *:*) d="$d/128" ;; *) d="$d/32" ;; esac
        tr ',' '\n' <<< "$(_routing_ssh_ports)" | while read -r p; do [ -z "$p" ] || echo "direct($d, tcp/$p)"; done
        echo "reject($d)"
    done < <(routing_self_ips)
    echo "reject(geoip:private)"
    echo "$out_e(geosite:category-ip-geo-detect)"
    while read -r d; do echo "$out_e(suffix:$d)"; done < <(routing_echo_domains)
    if [ "$out_r" != "direct" ]; then
        echo "direct(geosite:ru-blocked)"
        echo "$out_r(geosite:category-ru)"
        echo "$out_r(suffix:ru)"
        echo "$out_r(suffix:su)"
        echo "$out_r(suffix:xn--p1ai)"
        echo "$out_r(geoip:ru)"
    fi
}

# Фрагмент для config.yaml: sniff нужен, чтобы geosite срабатывал для клиентов, которые
# шлют IP (TUN); outbounds — только с WARP (direct первым = по умолчанию)
routing_hy2_fragment() {
    echo "# vpn-zoo 07-routing — генерируется"
    echo "sniff:"
    echo "  enable: true"
    echo "  timeout: 2s"
    echo "  rewriteDomain: false"
    echo "  tcpPorts: 80,443"
    echo "  udpPorts: 443"
    if routing_warp_ready; then
        echo "outbounds:"
        echo "  - name: direct"
        echo "    type: direct"
        echo "  - name: warp"
        echo "    type: socks5"
        echo "    socks5:"
        echo "      addr: 127.0.0.1:$WARP_SOCKS_PORT"
        echo "      username: $WARP_SOCKS_USER"
        echo "      password: $WARP_SOCKS_PASS"
    fi
}

# _routing_hy_put SRC DST MODE — установить, если отличается; 0 = изменён
_routing_hy_put() {
    local src="$1" dst="$2" mode="$3" grp=root
    getent group hysteria >/dev/null 2>&1 && grp=hysteria
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then rm -f "$src"; return 1; fi
    chgrp "$grp" "$src"; chmod "$mode" "$src"
    mv -f "$src" "$dst"
}

routing_hy2_apply() {
    local d="$ROUTING_HY_DIR" tmp changed=0 bdir f
    if [ ! -d "$d" ]; then
        log_info "routing: Hysteria не установлена ($d нет) — ACL пропущен"
        return 0
    fi
    [ -s "$ROUTING_HY_GEOSITE" ] && [ -s "$ROUTING_HY_GEOIP" ] || routing_geo_derive || return 1
    bdir="$(mktemp -d)"
    for f in acl.txt outbounds.yaml geo/geoip.dat geo/geosite.dat; do
        [ -f "$d/$f" ] && { mkdir -p "$bdir/$(dirname "$f")"; cp -p "$d/$f" "$bdir/$f"; }
    done
    ( umask 022; mkdir -p "$d/geo" ); chmod 755 "$d/geo"
    tmp="$(mktemp "$d/.acl.XXXXXX")"; routing_hy2_acl_text > "$tmp"
    _routing_hy_put "$tmp" "$d/acl.txt" 0640 && changed=1
    tmp="$(mktemp "$d/.outb.XXXXXX")"; routing_hy2_fragment > "$tmp"
    _routing_hy_put "$tmp" "$d/outbounds.yaml" 0640 && changed=1
    tmp="$(mktemp "$d/geo/.gi.XXXXXX")"; cp "$ROUTING_HY_GEOIP" "$tmp"
    _routing_hy_put "$tmp" "$d/geo/geoip.dat" 0644 && changed=1
    tmp="$(mktemp "$d/geo/.gs.XXXXXX")"; cp "$ROUTING_HY_GEOSITE" "$tmp"
    _routing_hy_put "$tmp" "$d/geo/geosite.dat" 0644 && changed=1

    # выключена флагом (ENABLE_HY2=0): файлы готовы, сервис не поднимаем — их подхватит 05
    if [ "${ENABLE_HY2:-1}" != "1" ]; then
        rm -rf "$bdir"
        log_info "routing: Hysteria выключена (ENABLE_HY2=0) — ACL записан, сервис не трогаю"
        return 0
    fi
    if ! declare -F proto_hysteria2_apply >/dev/null; then
        if [ -f "$SCRIPTS_DIR/lib/proto-hysteria2.sh" ]; then
            # shellcheck source=proto-hysteria2.sh
            . "$SCRIPTS_DIR/lib/proto-hysteria2.sh"
        fi
    fi
    if ! declare -F proto_hysteria2_apply >/dev/null; then
        log_warn "routing: нет lib/proto-hysteria2.sh — файлы ACL записаны, подключит фаза 05 при следующем запуске"
        rm -rf "$bdir"; return 0
    fi
    # Рендер сам решает, менялся ли config.yaml; наши файлы читаются при старте процесса,
    # поэтому при их изменении — принудительный рестарт
    if proto_hysteria2_apply "$([ "$changed" = 1 ] && echo force)"; then
        rm -rf "$bdir"
        log_ok "routing: ACL Hysteria $([ "$changed" = 1 ] && echo применён || echo уже актуален) (echo → $(routing_echo_egress), RU → $(routing_effective "${HY2_RU_EGRESS:-direct}"))"
        return 0
    fi
    log_err "routing: Hysteria не поднялась с новым ACL — возвращаю прежние файлы"
    for f in acl.txt outbounds.yaml geo/geoip.dat geo/geosite.dat; do
        if [ -f "$bdir/$f" ]; then cp -p "$bdir/$f" "$d/$f"; else rm -f "$d/$f"; fi
    done
    rm -rf "$bdir"
    proto_hysteria2_apply force || true
    return 1
}

# ---------- AmneziaWG (L3) ----------
# Доменов на L3 нет: echo-правило для AWG невозможно (только сплит на клиенте).
# RU-назначения: отдельная nft-таблица с приоритетом раньше filter — не зависит от
# порядка правил iptables (PostUp AWG вставляет ACCEPT в начало FORWARD, ufw перестраивает
# свои цепочки), REJECT в любой базовой цепочке окончателен.

routing_awg_apply() {
    local mode tmp
    mode="$(_routing_norm "${AWG_RU_EGRESS:-direct}")" || mode=block
    if [ "$mode" = "warp" ]; then
        log_warn "routing: AWG_RU_EGRESS=warp не поддерживается (нужен ядерный warp0) — использую block"
        mode=block
    fi
    if [ "$mode" = "direct" ]; then
        if [ -f "$ROUTING_AWG_NFT" ] || systemctl is-enabled --quiet vpn-zoo-awg-ru 2>/dev/null; then
            systemctl disable --now vpn-zoo-awg-ru >/dev/null 2>&1 || true
            command -v nft >/dev/null && nft delete table inet "$ROUTING_AWG_TABLE" 2>/dev/null || true
            rm -f "$ROUTING_AWG_NFT" /etc/systemd/system/vpn-zoo-awg-ru.service
            systemctl daemon-reload
            log_info "routing: RU-фильтр AWG снят"
        fi
        return 0
    fi
    command -v nft >/dev/null || apt_install nftables >/dev/null || { log_err "routing: не удалось установить nftables"; return 1; }
    [ -s "$ROUTING_GEO_DIR/ru-cidr.txt" ] || routing_geo_derive || return 1
    tmp="$(mktemp "$VPN_ETC/.awg-ru.XXXXXX")"
    {
        echo "# vpn-zoo 07-routing: AWG → RU-сети = REJECT (AWG_RU_EGRESS=block). Генерируется."
        echo "table inet $ROUTING_AWG_TABLE"
        echo "delete table inet $ROUTING_AWG_TABLE"
        echo "table inet $ROUTING_AWG_TABLE {"
        echo "  set ru4 { type ipv4_addr; flags interval; auto-merge; elements = {"
        grep -v ':' "$ROUTING_GEO_DIR/ru-cidr.txt" | sed 's/$/,/'
        echo "  } }"
        echo "  set ru6 { type ipv6_addr; flags interval; auto-merge; elements = {"
        grep ':' "$ROUTING_GEO_DIR/ru-cidr.txt" | sed 's/$/,/'
        echo "  } }"
        echo "  chain forward { type filter hook forward priority filter - 5; policy accept;"
        echo "    iifname \"awg*\" ip daddr @ru4 meta l4proto tcp reject with tcp reset"
        echo "    iifname \"awg*\" ip daddr @ru4 reject with icmpx admin-prohibited"
        echo "    iifname \"awg*\" ip6 daddr @ru6 meta l4proto tcp reject with tcp reset"
        echo "    iifname \"awg*\" ip6 daddr @ru6 reject with icmpx admin-prohibited"
        echo "  }"
        echo "}"
    } > "$tmp"
    nft -c -f "$tmp" || { rm -f "$tmp"; log_err "routing: nft не принял таблицу RU-сетей"; return 1; }
    chmod 600 "$tmp"; mv -f "$tmp" "$ROUTING_AWG_NFT"
    cat > /etc/systemd/system/vpn-zoo-awg-ru.service <<EOF
[Unit]
Description=vpn-zoo: AmneziaWG -> RU networks REJECT (AWG_RU_EGRESS=block)
After=network-pre.target
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $ROUTING_AWG_NFT
ExecStop=/usr/sbin/nft delete table inet $ROUTING_AWG_TABLE

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable vpn-zoo-awg-ru >/dev/null 2>&1
    nft -f "$ROUTING_AWG_NFT" || { log_err "routing: nft -f $ROUTING_AWG_NFT не применился"; return 1; }
    systemctl start vpn-zoo-awg-ru >/dev/null 2>&1 || true
    log_ok "routing: AWG → RU-сети REJECT ($(wc -l < "$ROUTING_GEO_DIR/ru-cidr.txt") сетей, таблица inet $ROUTING_AWG_TABLE)"
}

# ---------- таймер обновления geo ----------

# Снимок библиотек (репо может быть удалён после установки) + обёртка + юниты.
# Копируется весь scripts/lib: routing_hy2_apply в таймере нужен proto-hysteria2.sh
routing_timer_install() {
    local tmp f rel
    tmp="$(mktemp -d "$(dirname "$ROUTING_SHARE")/.vpn-zoo.XXXXXX")"
    mkdir -p "$tmp/scripts/lib" "$ROUTING_SHARE/scripts/lib"
    for f in "$SCRIPTS_DIR/lib.sh" "$SCRIPTS_DIR/versions.env" "$SCRIPTS_DIR"/lib/*.sh; do
        rel="${f#"$SCRIPTS_DIR"/}"
        install -m 644 "$f" "$tmp/scripts/$rel"
    done
    for f in "$tmp/scripts/lib.sh" "$tmp/scripts/versions.env" "$tmp"/scripts/lib/*.sh; do
        rel="${f#"$tmp"/scripts/}"
        mv -f "$f" "$ROUTING_SHARE/scripts/$rel"
    done
    rm -rf "$tmp"
    cat > "$ROUTING_UPDATER.tmp" <<EOF
#!/usr/bin/env bash
# vpn-zoo: обновление geo-файлов runetfreedom (фаза 07-routing). Ручной запуск:
#   $ROUTING_UPDATER [--force]
set -euo pipefail
export REPO_ROOT="$ROUTING_SHARE" SCRIPTS_DIR="$ROUTING_SHARE/scripts"
. "\$SCRIPTS_DIR/lib.sh"
. "\$SCRIPTS_DIR/lib/xui.sh"
. "\$SCRIPTS_DIR/lib/routing.sh"
require_root
config_load
exec 7>/run/lock/vpn-zoo-geo.lock
flock -n 7 || die "обновление geo уже идёт"
routing_geo_update "\$@"
EOF
    chmod 755 "$ROUTING_UPDATER.tmp"; mv -f "$ROUTING_UPDATER.tmp" "$ROUTING_UPDATER"
    cat > /etc/systemd/system/vpn-zoo-geo-update.service <<EOF
[Unit]
Description=vpn-zoo: update runetfreedom geo files (validated, with rollback)
After=network-online.target x-ui.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$ROUTING_UPDATER
Nice=10
EOF
    # Ночью: рестарт Xray рвёт соединения. До AUTO_REBOOT_TIME (04:00) обычно успевает
    cat > /etc/systemd/system/vpn-zoo-geo-update.timer <<EOF
[Unit]
Description=vpn-zoo: daily geo files update

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=45m
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now vpn-zoo-geo-update.timer >/dev/null 2>&1 || { log_err "routing: таймер vpn-zoo-geo-update не включился"; return 1; }
}

# ---------- WARP (фаза 08) ----------

# Регистрация через встроенный WARP 3x-ui (POST xray/warp/reg, поля privateKey/publicKey —
# так в v3.9.0; в исследовании ошибочно skey/pkey). Ключи и адреса — в config.env.
warp_register() {
    local kp priv pub tmp resp cfg cid
    kp="$("$(xui_xray_bin)" wg)" || { log_err "warp: xray wg не сработал"; return 1; }
    priv="$(awk -F': ' '/^PrivateKey/ {print $2}' <<< "$kp")"
    pub="$(awk -F': ' '/PublicKey/ {print $2}' <<< "$kp")"
    [ -n "$priv" ] && [ -n "$pub" ] || { log_err "warp: не разобрал вывод xray wg"; return 1; }
    tmp="$(mktemp -d)"
    ( umask 077; printf '%s' "$priv" > "$tmp/priv"; printf '%s' "$pub" > "$tmp/pub" )
    resp="$(xui_api_form xray/warp/reg "privateKey@$tmp/priv" "publicKey@$tmp/pub")" || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    cfg="$(jq -r '.obj' <<< "$resp")"
    jq -e '.data.private_key and .config.config.peers[0].public_key and .config.config.interface.addresses.v4' <<< "$cfg" >/dev/null \
        || { log_err "warp: неожиданный ответ регистрации: $(head -c 300 <<< "$cfg")"; return 1; }
    cid="$(jq -r '.config.config.client_id // .data.client_id // ""' <<< "$cfg")"
    config_set WARP_PRIV "$(jq -r '.data.private_key' <<< "$cfg")"
    config_set WARP_PEER_PUB "$(jq -r '.config.config.peers[0].public_key' <<< "$cfg")"
    config_set WARP_ENDPOINT "$(jq -r '.config.config.peers[0].endpoint.host // "engage.cloudflareclient.com:2408"' <<< "$cfg")"
    # IPv4 эндпоинта из регистрации приходит с портом 0
    config_set WARP_ENDPOINT_V4 "$(jq -r '.config.config.peers[0].endpoint.v4 // "" | sub(":[0-9]+$"; "")' <<< "$cfg")"
    config_set WARP_V4 "$(jq -r '.config.config.interface.addresses.v4' <<< "$cfg")"
    config_set WARP_V6 "$(jq -r '.config.config.interface.addresses.v6 // ""' <<< "$cfg")"
    # reserved = байты base64(client_id)
    config_set WARP_RESERVED "$(printf '%s' "$cid" | base64 -d 2>/dev/null | od -An -tu1 | xargs | tr ' ' ',')"
    config_set WARP_DEVICE_ID "$(jq -r '.data.device_id // ""' <<< "$cfg")"
}

# curl через локальный socks-вход zoo-warp-in; пароль — в файле, не в argv
warp_curl() {
    local url="$1" cfg rc=0
    cfg="$(mktemp)"
    ( umask 077; printf 'proxy = "socks5h://127.0.0.1:%s"\nproxy-user = "%s:%s"\n' \
        "$WARP_SOCKS_PORT" "$WARP_SOCKS_USER" "$WARP_SOCKS_PASS" > "$cfg" )
    curl -fsS --max-time 20 -K "$cfg" "$url" || rc=$?
    rm -f "$cfg"
    return "$rc"
}

# Трафик через outbound warp выходит с адреса Cloudflare: cdn-cgi/trace даёт warp=on|plus
warp_smoke() {
    local out i
    for i in 1 2 3 4 5 6; do
        out="$(warp_curl https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null || true)"
        if grep -qE '^warp=(on|plus)$' <<< "$out"; then
            WARP_EXIT_IP="$(sed -n 's/^ip=//p' <<< "$out")"
            return 0
        fi
        sleep "$i"
    done
    log_warn "warp: проверка не прошла: $(tr '\n' ' ' <<< "${out:-нет ответа}" | head -c 200)"
    return 1
}

# ---------- статус для zoo ----------

# JSON: geo, xray (правила, sniffing), hysteria, awg, warp, таймер.
#   routing_status [--live] — с --live ещё живая проверка WARP (warp.live, до ~20 с)
routing_status() {
    local tmpl inb hy_acl="" awg_set=0 timer_next="" geo='{}' live="null"
    if [ "${1:-}" = "--live" ] && routing_warp_ready; then
        if warp_curl https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -qE '^warp=(on|plus)$'; then live=true; else live=false; fi
    fi
    tmpl="$(xui_xray_template_get 2>/dev/null || echo '{}')"
    inb="$(xui_inbound_list 2>/dev/null || echo '[]')"
    [ -f "$ROUTING_GEO_DIR/state.json" ] && geo="$(cat "$ROUTING_GEO_DIR/state.json")"
    [ -f "$ROUTING_HY_ACL" ] && hy_acl="$(grep -v '^#' "$ROUTING_HY_ACL" | head -3 | paste -sd';' -)"
    if command -v nft >/dev/null && nft list table inet "$ROUTING_AWG_TABLE" >/dev/null 2>&1; then awg_set=1; fi
    timer_next="$(systemctl show vpn-zoo-geo-update.timer -p NextElapseUSecRealtime --value 2>/dev/null || true)"
    jq -n --argjson t "$tmpl" --argjson inb "$inb" --argjson geo "$geo" \
        --arg ru "$(routing_effective "${RU_EGRESS:-direct}")" --arg ru_cfg "${RU_EGRESS:-direct}" \
        --arg echo "$(routing_echo_egress)" --arg bt "$(_routing_bt_policy)" \
        --arg hy_ru "$(routing_effective "${HY2_RU_EGRESS:-direct}")" --arg hy_acl "$hy_acl" \
        --arg hy_present "$([ -f "$ROUTING_HY_CONFIG" ] && echo 1 || echo 0)" \
        --arg hy_linked "$(grep -qs "$ROUTING_HY_ACL" "$ROUTING_HY_CONFIG" && echo 1 || echo 0)" \
        --arg awg "${AWG_RU_EGRESS:-direct}" --argjson awg_set "$awg_set" \
        --arg warp_en "${ENABLE_WARP:-0}" --arg warp_ready "${WARP_READY:-0}" --arg warp_ip "${WARP_EXIT_IP:-}" \
        --arg warp_checked "${WARP_CHECKED_AT:-}" --argjson live "$live" \
        --arg timer "$(systemctl is-active vpn-zoo-geo-update.timer 2>/dev/null || true)" --arg next "$timer_next" \
        --arg last "$(systemctl show vpn-zoo-geo-update.service -p Result --value 2>/dev/null || true)" '{
        geo: $geo,
        xray: {
            ru_egress: $ru, ru_egress_config: $ru_cfg, echo_egress: $echo, bittorrent: $bt,
            domain_strategy: ($t.routing.domainStrategy // "AsIs"),
            zoo_rules: [($t.routing.rules // [])[] | select((.ruleTag // "") | startswith("zoo-")) | {tag: .ruleTag, out: .outboundTag}],
            access_log: ($t.log.access // ""),
            inbounds_without_sniffing: [$inb[] | select(.protocol != "wireguard" and .protocol != "tunnel" and .protocol != "mtproto")
                | select(((.sniffing.enabled // false) and (.sniffing.routeOnly // false)) | not) | {id, remark, port}]
        },
        hysteria: {present: ($hy_present == "1"), acl_linked: ($hy_linked == "1"), ru_egress: $hy_ru, acl_head: $hy_acl},
        awg: {ru_egress: $awg, nft_table: ($awg_set == 1), echo: "не поддерживается (L3)"},
        warp: {enabled: ($warp_en == "1"), ready: ($warp_ready == "1"), exit_ip: $warp_ip, checked_at: $warp_checked,
               outbound: any(($t.outbounds // [])[]; .tag == "warp"), live: $live},
        geo_timer: {state: $timer, next: $next, last_result: $last}
    }'
}
