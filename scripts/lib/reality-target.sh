#!/usr/bin/env bash
# lib/reality-target.sh — выбор и проверка REALITY target (D10).
# source'ится после lib.sh и lib/xui.sh (нужен xui_xray_bin).
#
# Требования к target (XTLS/REALITY, Xray 26.9.30): TLS 1.3, группа X25519MLKEM768
# или X25519 без HRR, запись Certificate ≤17 KiB, h2, без редиректа на чужой хост,
# SAN покрывает SNI. Желательно ML-KEM и цепочка ≥3500 Б (запас под ML-DSA-65).
# Не .ru/.su/.рф/.ir/.cn и не apple/icloud/microsoft: на них Xray ≥26.7.28 пишет warning,
# а SNI российского или чужого сервиса на зарубежном IP — заметное несоответствие.

# Крупные сайты на CDN: аварийный вариант, лучше self-steal или сосед по ASN (VLESS_SNI).
# Порядок перемешивается, чтобы установки не делили один SNI.
REALITY_CANDIDATES="${REALITY_CANDIDATES:-www.samsung.com www.nvidia.com addons.mozilla.org www.mozilla.org www.yahoo.com www.oracle.com www.lovelive-anime.jp www.asus.com www.cisco.com www.ibm.com www.logitech.com www.sony.com}"

# Максимум цепочки сертификатов: буфер REALITY 17 KiB минус запас на заголовки записи
REALITY_MAX_CHAIN=16000

# Последняя причина отказа / результат проверки (для логов вызывающего)
REALITY_REASON=""
REALITY_PQ=""
REALITY_CHAIN=""

# Домен из запрещённых зон или брендов
reality_target_denied() {
    local h="${1,,}"
    case "$h" in
        *.ru|*.su|*.xn--p1ai|*.ir|*.cn) return 0 ;;
        *apple*|*icloud*|*microsoft*) return 0 ;;
    esac
    return 1
}

# SAN (список из xray tls ping) покрывает host, с учётом *.домен
_reality_san_covers() {
    local host="$1" sans="$2" s
    for s in $sans; do
        [ "$s" = "$host" ] && return 0
        if [[ "$s" == \*.* ]] && [[ "$host" == *."${s#\*.}" ]] && [[ "${host%."${s#\*.}"}" != *.* ]]; then
            return 0
        fi
    done
    return 1
}

# reality_target_check SNI [TARGET] — 0, если пара годится; иначе REALITY_REASON.
# TARGET — host:port, куда REALITY пересылает чужие соединения (по умолчанию SNI:443)
reality_target_check() {
    local host="${1,,}" target="${2:-}" thost port ip xray out sec ver pq chain sans cr hv code redir rhost
    REALITY_REASON=""; REALITY_PQ=""; REALITY_CHAIN=""
    [ -n "$target" ] || target="$host:443"
    thost="${target%:*}"; port="${target##*:}"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$thost" != "$target" ] || { REALITY_REASON="target не в формате host:port ($target)"; return 1; }
    if ! [[ "$host" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
        REALITY_REASON="не похоже на домен"; return 1
    fi
    if reality_target_denied "$host"; then
        REALITY_REASON="запрещённая зона/бренд (.ru/.ir/.cn/apple/icloud/microsoft)"; return 1
    fi
    xray="$(xui_xray_bin)"
    [ -x "$xray" ] || { REALITY_REASON="нет xray ($xray)"; return 1; }

    # xray tls ping всегда выходит с 0: разбираем блок «Pinging with SNI».
    # Формат: tls ping [-ip IP] SNI[:порт]; -ip принимает только IP
    if [ "${thost,,}" = "$host" ]; then
        out="$(timeout 20 "$xray" tls ping "$host:$port" 2>&1 || true)"
    else
        ip="$thost"
        if ! [[ "$ip" =~ ^[0-9.]+$ || "$ip" == *:* ]]; then
            ip="$(getent ahostsv4 "$thost" 2>/dev/null | awk 'NR==1 {print $1}')"
            [ -n "$ip" ] || { REALITY_REASON="не резолвится $thost"; return 1; }
        fi
        out="$(timeout 20 "$xray" tls ping -ip "$ip" "$host:$port" 2>&1 || true)"
    fi
    sec="$(sed -n '/Pinging with SNI/,$p' <<< "$out")"
    if ! grep -q 'Handshake succeeded' <<< "$sec"; then
        REALITY_REASON="TLS-рукопожатие с SNI не прошло: $(grep -m1 -iE 'fail|error|refused|timeout' <<< "$out" | head -c 200)"
        return 1
    fi
    ver="$(awk -F':[[:space:]]+' '/^TLS Version/ {print $2; exit}' <<< "$sec")"
    pq="$(awk -F':[[:space:]]+' '/^TLS Post-Quantum key exchange/ {print $2; exit}' <<< "$sec")"
    chain="$(awk -F':[[:space:]]+' '/^Certificate chain.s total length/ {print $2; exit}' <<< "$sec" | awk '{print $1}')"
    sans="$(awk -F':[[:space:]]+' '/^Cert.s allowed domains/ {print $2; exit}' <<< "$sec" | tr -d '[]')"
    [ "$ver" = "TLS 1.3" ] || { REALITY_REASON="не TLS 1.3 ($ver)"; return 1; }
    case "$pq" in
        "true (X25519MLKEM768)") REALITY_PQ=1 ;;
        "false (X25519)") REALITY_PQ=0 ;;
        *) REALITY_REASON="группа не X25519MLKEM768/X25519 ($pq) — будет HRR"; return 1 ;;
    esac
    [[ "$chain" =~ ^[0-9]+$ ]] || { REALITY_REASON="не удалось узнать длину цепочки"; return 1; }
    REALITY_CHAIN="$chain"
    [ "$chain" -le "$REALITY_MAX_CHAIN" ] || { REALITY_REASON="цепочка $chain Б > $REALITY_MAX_CHAIN"; return 1; }
    if [ -n "$sans" ] && ! _reality_san_covers "$host" "$sans"; then
        REALITY_REASON="сертификат не покрывает $host"; return 1
    fi

    # h2 и редирект: то, что увидит браузер
    cr="$(curl -sS -o /dev/null --max-time 15 --connect-to "$host:443:$thost:$port" \
        -w '%{http_version} %{http_code} %{redirect_url}' "https://$host/" 2>/dev/null || true)"
    read -r hv code redir <<< "$cr"
    [ "${hv:-}" = "2" ] || { REALITY_REASON="нет h2 (HTTP-версия: ${hv:-нет ответа})"; return 1; }
    if [[ "${code:-}" =~ ^3 ]] && [ -n "${redir:-}" ]; then
        rhost="${redir#*://}"; rhost="${rhost%%[/:?#]*}"
        [ "${rhost,,}" = "$host" ] || { REALITY_REASON="редирект на другой хост ($rhost)"; return 1; }
    fi
    return 0
}

# reality_target_select — печатает выбранный домен. Сначала кандидаты с ML-KEM
# и цепочкой ≥3500, затем любые годные. Причины отказов — в stderr
reality_target_select() {
    local h fallback="" list
    list="$(tr ' ' '\n' <<< "$REALITY_CANDIDATES" | awk 'NF' | shuf)"
    for h in $list; do
        # REALITY_EXCLUDE — SNI других inbound (XHTTP): у каждого транспорта свой target
        case " ${REALITY_EXCLUDE:-} " in *" $h "*) continue ;; esac
        if reality_target_check "$h"; then
            if [ "$REALITY_PQ" = "1" ] && [ "$REALITY_CHAIN" -ge 3500 ]; then
                printf '%s\n' "$h"; return 0
            fi
            [ -n "$fallback" ] || fallback="$h"
            log_info "REALITY target $h годится, но без ML-KEM или с короткой цепочкой — ищу лучше" >&2
        else
            log_warn "REALITY target $h отклонён: $REALITY_REASON" >&2
        fi
    done
    [ -n "$fallback" ] || return 1
    printf '%s\n' "$fallback"
}
