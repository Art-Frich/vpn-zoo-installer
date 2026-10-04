#!/usr/bin/env bash
# lib/awg-params.sh — параметры обфускации AmneziaWG: генерация на каждую установку (D4)
# и проверка ограничений (research vpn-news §4.4). Только функции, без побочных эффектов.
#
# Профили:
#   v2 — совместим с клиентами AWG 2.0: Jc/Jmin/Jmax, S1–S4, H1–H4, I1. Без 3.x-расширений
#        формат пакетов 3.x побайтно совпадает с 2.0.
#   v3 — плюс HeaderProtectionKey (нужен клиент 3.1). AWG_RT=1 дополнительно включает
#        RandomTrailers: тогда S1=S2=S3=S4 (обход бага go#186 / kmod#226).
#
# H1–H4 — одиночные значения, а не диапазоны. Приём (DeterminePacketTypeAndPadding) сверяет
# заголовок по смещению S у любого транспортного пакета, чей размер совпал с размером
# рукопожатия; при широком H заметная доля таких пакетов (ACK) теряется.

# shellcheck disable=SC2153 # AWG_* приходят из config.env
AWG_H_MIN=5
AWG_H_MAX=2147483647   # INT32_MAX: выше не принимает Windows-клиент

# Случайное целое из [MIN, MAX]
awg_rand() {
    local min="$1" max="$2" r
    r="$(od -An -N4 -tu4 /dev/urandom | tr -d ' \n')"
    printf '%s\n' $(( min + r % (max - min + 1) ))
}

_awg_hex() { printf '%s' "$1" | od -An -tx1 | tr -d ' \n'; }

# I1 в форме DNS-ответа (A-запись) ≤128 байт: свой домен, TTL и адреса на каждую установку.
# ID запроса — <r 2>, остальное статично в пределах установки (I1 шлёт инициатор)
awg_i1_dns() {
    local cons=bcdfghklmnprstvz vow=aeiou label="" i n tld ans hex o1
    local tlds=(com net org ru io info pro)
    n="$(awg_rand 5 10)"
    for (( i = 0; i < n; i++ )); do
        if (( i % 2 == 0 )); then label+="${cons:$(awg_rand 0 15):1}"; else label+="${vow:$(awg_rand 0 4):1}"; fi
    done
    tld="${tlds[$(awg_rand 0 $(( ${#tlds[@]} - 1 )))]}"
    ans="$(awg_rand 1 2)"
    # флаги 8180: ответ, RD, RA, NOERROR; QD=1, AN=ans
    hex="81800001$(printf '%04x' "$ans")00000000"
    hex+="$(printf '%02x' "${#label}")$(_awg_hex "$label")$(printf '%02x' "${#tld}")$(_awg_hex "$tld")00"
    hex+="00010001"
    for (( i = 0; i < ans; i++ )); do
        # публичный на вид адрес: без 0/10/100/127/169/172/192/198/203 и ≥224
        while :; do
            o1="$(awg_rand 2 223)"
            case "$o1" in 10|100|127|169|172|192|198|203) continue ;; esac
            break
        done
        hex+="c00c00010001$(printf '%08x' "$(awg_rand 60 3600)")0004"
        hex+="$(printf '%02x%02x%02x%02x' "$o1" "$(awg_rand 0 255)" "$(awg_rand 0 255)" "$(awg_rand 1 254)")"
    done
    printf '<r 2><b 0x%s>\n' "$hex"
}

# awg_params_generate PROFILE RT — печатает «КЛЮЧ ЗНАЧЕНИЕ» для config.env:
# AWG_JC AWG_JMIN AWG_JMAX AWG_S1..S4 AWG_H1..H4 AWG_I1 [AWG_HPK]
awg_params_generate() {
    local profile="${1:-v2}" rt="${2:-0}" jmin s1 s2 s3 s4 q i
    local order=(0 1 2 3) j t

    jmin="$(awg_rand 40 89)"
    printf 'AWG_JC %s\n' "$(awg_rand 3 6)"
    printf 'AWG_JMIN %s\n' "$jmin"
    printf 'AWG_JMAX %s\n' "$(( jmin + $(awg_rand 50 250) ))"

    if [ "$profile" = "v3" ] && [ "$rt" = "1" ]; then
        s1="$(awg_rand 12 40)"; s2="$s1"; s3="$s1"; s4="$s1"
    else
        s1="$(awg_rand 15 150)"
        while :; do s2="$(awg_rand 15 150)"; [ "$s2" -ne $(( s1 + 56 )) ] && break; done
        while :; do s3="$(awg_rand 12 55)"; [ "$s3" -ne $(( s2 + 28 )) ] && break; done
        s4="$(awg_rand 12 27)"
    fi
    printf 'AWG_S1 %s\nAWG_S2 %s\nAWG_S3 %s\nAWG_S4 %s\n' "$s1" "$s2" "$s3" "$s4"

    # по одному H в каждой четверти [5, INT32_MAX], четверти перемешаны
    for (( i = 3; i > 0; i-- )); do
        j="$(awg_rand 0 "$i")"; t="${order[$i]}"; order[i]="${order[$j]}"; order[j]="$t"
    done
    q=$(( (AWG_H_MAX - AWG_H_MIN + 1) / 4 ))
    for i in 0 1 2 3; do
        printf 'AWG_H%s %s\n' $(( i + 1 )) $(( AWG_H_MIN + order[i] * q + $(awg_rand 0 $(( q - 1 ))) ))
    done

    printf 'AWG_I1 %s\n' "$(awg_i1_dns)"
    if [ "$profile" = "v3" ]; then
        printf 'AWG_HPK %s\n' "$(openssl rand -base64 32)"
    fi
}

# Разбор H: «N» или «A-B» → печатает «A B»
_awg_h_bounds() {
    if [[ "$1" =~ ^([0-9]{1,10})$ ]]; then
        printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[1]}"
    elif [[ "$1" =~ ^([0-9]{1,10})-([0-9]{1,10})$ ]]; then
        printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    else
        return 1
    fi
}

# awg_params_check — проверка AWG_* из окружения; ошибки в stderr, код 1 при нарушении.
# Учитывает AWG_PROFILE (v3 → HPK) и AWG_RT.
awg_params_check() {
    local errs=() k v i j lo hi lo2 hi2 mtu="${AWG_MTU:-1280}"
    for k in AWG_JC AWG_JMIN AWG_JMAX AWG_S1 AWG_S2 AWG_S3 AWG_S4; do
        v="${!k:-}"
        [[ "$v" =~ ^[0-9]{1,5}$ ]] || errs+=("$k=«$v»: нужно целое число")
    done
    [[ "$mtu" =~ ^[0-9]{3,4}$ ]] && [ "$mtu" -ge 1280 ] && [ "$mtu" -le 1420 ] \
        || errs+=("AWG_MTU=$mtu: допустимо 1280..1420")
    if [ "${#errs[@]}" -eq 0 ]; then
        [ "$AWG_JC" -ge 1 ] && [ "$AWG_JC" -le 128 ] || errs+=("AWG_JC=$AWG_JC: допустимо 1..128")
        # Jmin>Jmax — переполнение кучи в kmod (#225/#254), паника демона в go (#189)
        [ "$AWG_JMIN" -lt "$AWG_JMAX" ] || errs+=("AWG_JMIN=$AWG_JMIN должен быть меньше AWG_JMAX=$AWG_JMAX")
        [ "$AWG_JMAX" -lt 1280 ] || errs+=("AWG_JMAX=$AWG_JMAX: должен быть < 1280 (иначе фрагментация)")
        # совпадение размеров рукопожатий: init=148+S1, response=92+S2, cookie=64+S3
        [ "$AWG_S2" -ne $(( AWG_S1 + 56 )) ] || errs+=("S1+56 = S2: init и response одного размера")
        [ "$AWG_S3" -ne $(( AWG_S2 + 28 )) ] || errs+=("S2+28 = S3: response и cookie одного размера")
        [ "$AWG_S3" -ne $(( AWG_S1 + 84 )) ] || errs+=("S1+84 = S3: init и cookie одного размера")
        # пределы iOS-клиента
        [ "$AWG_S1" -le 1552 ] || errs+=("AWG_S1 > 1552 (предел iOS)")
        [ "$AWG_S2" -le 1608 ] || errs+=("AWG_S2 > 1608 (предел iOS)")
        [ "$AWG_S3" -le 1636 ] || errs+=("AWG_S3 > 1636 (предел iOS)")
        [ "$AWG_S4" -le 64 ] || errs+=("AWG_S4 > 64")
        if [ "${AWG_PROFILE:-v2}" = "v3" ]; then
            for k in AWG_S1 AWG_S2 AWG_S3 AWG_S4; do
                [ "${!k}" -ge 12 ] || errs+=("$k=${!k}: с HeaderProtectionKey нужно ≥ 12")
            done
            [ -n "${AWG_HPK:-}" ] || errs+=("AWG_PROFILE=v3 без AWG_HPK")
            if [ "${AWG_RT:-0}" = "1" ] && { [ "$AWG_S1" != "$AWG_S2" ] || [ "$AWG_S1" != "$AWG_S3" ] || [ "$AWG_S1" != "$AWG_S4" ]; }; then
                errs+=("AWG_RT=1 требует S1=S2=S3=S4 (баг RandomTrailers go#186)")
            fi
        fi
    fi
    local hs=()
    for i in 1 2 3 4; do
        k="AWG_H$i"; v="${!k:-}"
        if ! read -r lo hi < <(_awg_h_bounds "$v"); then
            errs+=("$k=«$v»: нужно число или диапазон A-B"); continue
        fi
        if [ "$lo" -lt "$AWG_H_MIN" ] || [ "$hi" -gt "$AWG_H_MAX" ] || [ "$lo" -gt "$hi" ]; then
            errs+=("$k=$v: вне $AWG_H_MIN..$AWG_H_MAX или A > B")
        fi
        [ "${AWG_RT:-0}" = "1" ] && [ "$lo" != "$hi" ] && errs+=("$k=$v: с AWG_RT=1 только одиночные H")
        hs+=("$lo $hi")
    done
    if [ "${#hs[@]}" -eq 4 ]; then
        for (( i = 0; i < 4; i++ )); do
            for (( j = i + 1; j < 4; j++ )); do
                read -r lo hi <<< "${hs[i]}"; read -r lo2 hi2 <<< "${hs[j]}"
                if [ "$lo" -le "$hi2" ] && [ "$lo2" -le "$hi" ]; then
                    errs+=("H$(( i + 1 )) и H$(( j + 1 )) пересекаются")
                fi
            done
        done
    fi
    local i1_re='^(<(b 0x([0-9a-fA-F][0-9a-fA-F])+|r [0-9]{1,4}|rc [0-9]{1,4}|rd [0-9]{1,4}|t)>)+$'
    if [ -n "${AWG_I1:-}" ] && ! [[ "$AWG_I1" =~ $i1_re ]]; then
        errs+=("AWG_I1: допустимы теги <b 0x..> <r N> <rc N> <rd N> <t> без пробелов между ними")
    fi
    if [ "${#errs[@]}" -gt 0 ]; then
        printf '[x] параметры AmneziaWG: %s\n' "${errs[@]}" >&2
        return 1
    fi
}
