#!/usr/bin/env bash
# history.sh — история проб на машине владельца: каталог history/ в репо (history/README.md).
#
#   history.sh push [-p ПОРТ] [--tag МЕТКА] [--device УСТРОЙСТВО] USER@СЕРВЕР [ОТЧЁТ]
#       отправить клиентский отчёт (по умолчанию probe/probe-report.json) в историю сервера:
#       ssh … zoo history add -
#   history.sh pull [-p ПОРТ] [--since 30d] USER@СЕРВЕР
#       забрать с сервера анонимный jsonl и сырые отчёты (age → history/recipients.txt) и
#       дополнить history/ без дублей: ssh … zoo history export --tar | tar -x
#   history.sh keygen [--github USER]… [--key ФАЙЛ.pub]… [--label МЕТКА]
#       создать ключ истории (age): публичный → history/recipients.txt (на него шифруются все
#       сырые отчёты), приватный — копиями history/key/<метка>.age, по одной на каждый SSH-ключ
#       (по умолчанию — ключи с github.com/USER.keys и ~/.ssh/*.pub)
#   history.sh addkey [--github USER]… [--key ФАЙЛ.pub]… [--label МЕТКА]
#       добавить копию ключа истории для нового SSH-ключа (старые отчёты перешифровывать не нужно)
#   history.sh decrypt [-i КЛЮЧ] ФАЙЛ.age…
#       расшифровать сырой отчёт: ключ истории открывается любым своим SSH-ключом из ~/.ssh
#       (или -i КЛЮЧ); старые отчёты, зашифрованные прямо на SSH-ключ, тоже читаются
#   history.sh install-age
#       скачать закреплённый age (versions.env: AGE_VERSION, sha256) в ~/.local/bin
#
# Нужны ssh и tar (Git Bash на Windows их содержит). USER — root или пользователь с sudo без пароля.
# На сервере пользуется `zoo`: фаза 09 ставит его и age. Каталог истории — ZOO_HISTORY_DIR
# (по умолчанию history/ рядом со скриптами). В коммит попадает только history/ — IP там нет.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HIST="${ZOO_HISTORY_DIR:-$ROOT/history}"

TMP_DIR=""
trap '[ -z "$TMP_DIR" ] || rm -rf "$TMP_DIR"' EXIT

die() { echo "[x] $*" >&2; exit 1; }
info() { echo "[i] $*" >&2; }
usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

# server_run ПОРТ USER@HOST аргументы zoo… — `zoo` на сервере; stdin уходит туда же
server_run() {
    local port="$1" target="$2" remote sudo_prefix="sudo -n " arg opts=()
    shift 2
    [ -z "$port" ] || opts=(-p "$port")
    [ "${target%%@*}" != "root" ] || sudo_prefix=""
    remote="${sudo_prefix}zoo"
    for arg in "$@"; do remote+=" $(printf '%q' "$arg")"; done
    ssh "${opts[@]}" "$target" "$remote"
}

cmd_push() {
    local port="" tag="" device="" target="" file="" extra=()
    while [ $# -gt 0 ]; do
        case "$1" in
            -p) port="${2:?-p ПОРТ}"; shift 2 ;;
            --tag) tag="${2:?--tag МЕТКА}"; shift 2 ;;
            --device) device="${2:?--device УСТРОЙСТВО}"; shift 2 ;;
            -*) usage ;;
            *) if [ -z "$target" ]; then target="$1"; else file="$1"; fi; shift ;;
        esac
    done
    [ -n "$target" ] || usage
    file="${file:-$ROOT/probe/probe-report.json}"
    [ -f "$file" ] || die "нет отчёта $file (запустите пробник: docker run … zoo-probe)"
    [ -z "$tag" ] || extra+=(--tag "$tag")
    [ -z "$device" ] || extra+=(--device "$device")
    server_run "$port" "$target" history add "${extra[@]}" - < "$file"
}

cmd_pull() {
    local port="" since="all" target="" n tmp
    while [ $# -gt 0 ]; do
        case "$1" in
            -p) port="${2:?-p ПОРТ}"; shift 2 ;;
            --since) since="${2:?--since ПЕРИОД}"; shift 2 ;;
            -*) usage ;;
            *) target="$1"; shift ;;
        esac
    done
    [ -n "$target" ] || usage
    tmp="$(mktemp -d)"
    TMP_DIR="$tmp"
    # recipients.txt уходит на stdin сервера; tar приходит в stdout, сообщения zoo — в stderr
    if [ -f "$HIST/recipients.txt" ] && grep -Eqv '^[[:space:]]*(#|$)' "$HIST/recipients.txt"; then
        server_run "$port" "$target" history export --tar --recipients - --since "$since" < "$HIST/recipients.txt" \
            | tar -x -C "$tmp"
    else
        info "в $HIST/recipients.txt нет ключей (cat ~/.ssh/id_ed25519.pub >> history/recipients.txt): забираю только анонимный jsonl"
        server_run "$port" "$target" history export --tar --no-raw --since "$since" < /dev/null | tar -x -C "$tmp"
    fi
    mkdir -p "$HIST/raw"
    n=0
    for f in "$tmp"/*.jsonl; do
        [ -f "$f" ] || continue
        # ts — первый ключ строки: сортировка строк = по времени; одинаковые строки схлопываются
        if [ -f "$HIST/$(basename "$f")" ]; then
            LC_ALL=C sort -u "$HIST/$(basename "$f")" "$f" > "$tmp/merged"
        else
            LC_ALL=C sort -u "$f" > "$tmp/merged"
        fi
        mv "$tmp/merged" "$HIST/$(basename "$f")"
        n=$((n + 1))
    done
    local raw_new=0
    for f in "$tmp"/raw/*.age; do
        [ -f "$f" ] || continue
        if [ ! -e "$HIST/raw/$(basename "$f")" ]; then
            cp "$f" "$HIST/raw/"
            raw_new=$((raw_new + 1))
        fi
    done
    info "history/: файлов jsonl обновлено $n, новых сырых отчётов $raw_new — проверьте git status и закоммитьте"
}

# host_path ПУТЬ — путь для `docker -v` (на Windows — вида C:\…)
host_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else readlink -f "$1"; fi
}

KEYDIR="$HIST/key"
NO_AGE="нет age: winget install FiloSottile.age (Windows), brew install age (macOS), apt install age (Linux) или scripts/history.sh install-age"

need_age() { command -v age >/dev/null 2>&1 || die "$NO_AGE"; }

# Приватные SSH-ключи на этой машине: -i/ZOO_AGE_KEY, затем ~/.ssh/* с парой .pub
ssh_identities() {
    local f
    [ -z "${ZOO_AGE_KEY:-}" ] || echo "$ZOO_AGE_KEY"
    for f in "$HOME"/.ssh/*.pub; do
        [ -f "$f" ] && [ -f "${f%.pub}" ] && echo "${f%.pub}"
    done
}

# Публичные SSH-ключи из --github/--key (без них — github.com/<origin-владелец>.keys и ~/.ssh/*.pub)
collect_pubkeys() {
    local gh=() files=() f
    while [ $# -gt 0 ]; do
        case "$1" in
            --github) gh+=("${2:?--github USER}"); shift 2 ;;
            --key) files+=("${2:?--key ФАЙЛ.pub}"); shift 2 ;;
            *) shift ;;
        esac
    done
    if [ ${#gh[@]} -eq 0 ] && [ ${#files[@]} -eq 0 ]; then
        for f in "$HOME"/.ssh/*.pub; do [ -f "$f" ] && files+=("$f"); done
    fi
    for u in "${gh[@]}"; do curl -fsSL --max-time 15 "https://github.com/$u.keys" || die "не получил ключи github.com/$u.keys"; done
    for f in "${files[@]}"; do [ -f "$f" ] || die "нет $f"; cat "$f"; echo; done
}

# Отпечаток публичного SSH-ключа (строка «тип base64 …») — без комментария с почтой
fingerprint() {
    echo "$1" > "$TMP_DIR/pk.pub"
    ssh-keygen -lf "$TMP_DIR/pk.pub" 2>/dev/null | awk '{print $2}'
}

# wrap_for СТРОКА-КЛЮЧА ФАЙЛ-ЛИЧНОСТИ [МЕТКА] — копия ключа истории для одного SSH-ключа
wrap_for() {
    local line="$1" identity="$2" label="${3:-}" fp n
    fp="$(fingerprint "$line")"
    [ -n "$fp" ] || { info "пропуск нераспознанного ключа: ${line:0:30}…"; return 0; }
    if [ -f "$KEYDIR/devices.txt" ] && grep -q " $fp\$" "$KEYDIR/devices.txt"; then
        info "ключ $fp уже есть"; return 0
    fi
    if [ -z "$label" ] || [ -e "$KEYDIR/$label.age" ]; then
        n=1; while [ -e "$KEYDIR/device-$n.age" ]; do n=$((n + 1)); done; label="device-$n"
    fi
    age -a -r "$line" -o "$KEYDIR/$label.age" "$identity" || die "age: не зашифровал копию для $fp"
    echo "$label ${line%% *} $fp" >> "$KEYDIR/devices.txt"
    info "копия ключа истории: $KEYDIR/$label.age ($fp)"
}

# unwrap ФАЙЛ-ЛИЧНОСТИ — открыть ключ истории любым своим SSH-ключом
unwrap() {
    local out="$1" id copy
    while IFS= read -r id; do
        for copy in "$KEYDIR"/*.age; do
            [ -f "$copy" ] || continue
            if age -d -i "$id" -o "$out" "$copy" 2>/dev/null; then return 0; fi
        done
    done < <(ssh_identities)
    return 1
}

cmd_keygen() {
    local label="" args=() identity line
    while [ $# -gt 0 ]; do
        case "$1" in
            --label) label="${2:?--label МЕТКА}"; shift 2 ;;
            --github|--key) args+=("$1" "${2:?$1 ЗНАЧЕНИЕ}"); shift 2 ;;
            *) usage ;;
        esac
    done
    need_age; command -v age-keygen >/dev/null 2>&1 || die "нет age-keygen (идёт вместе с age)"
    [ ! -e "$KEYDIR/devices.txt" ] || die "ключ истории уже есть ($KEYDIR) — новое устройство: history.sh addkey"
    TMP_DIR="$(mktemp -d)"; chmod 700 "$TMP_DIR"
    identity="$TMP_DIR/history.key"
    ( umask 077; age-keygen -o "$identity" 2>/dev/null )
    mkdir -p "$KEYDIR"
    {
        echo "# Ключ истории (age): на него шифруются сырые отчёты history/raw/*.age."
        echo "# Приватная часть — в history/key/<метка>.age, по копии на каждый SSH-ключ владельца."
        age-keygen -y "$identity"
    } > "$HIST/recipients.txt"
    while IFS= read -r line; do
        case "$line" in ssh-ed25519\ *|ssh-rsa\ *) wrap_for "$line" "$identity" "$label"; label="" ;; esac
    done < <(collect_pubkeys "${args[@]}")
    [ -s "$KEYDIR/devices.txt" ] || { rm -rf "$KEYDIR"; die "не нашлось ни одного SSH-ключа (ssh-ed25519/ssh-rsa)"; }
    info "готово: закоммитьте history/recipients.txt и history/key/"
}

cmd_addkey() {
    local label="" args=() identity line
    while [ $# -gt 0 ]; do
        case "$1" in
            --label) label="${2:?--label МЕТКА}"; shift 2 ;;
            --github|--key) args+=("$1" "${2:?$1 ЗНАЧЕНИЕ}"); shift 2 ;;
            *) usage ;;
        esac
    done
    need_age
    [ -f "$KEYDIR/devices.txt" ] || die "ключа истории ещё нет — history.sh keygen"
    TMP_DIR="$(mktemp -d)"; chmod 700 "$TMP_DIR"
    identity="$TMP_DIR/history.key"
    unwrap "$identity" || die "ни один SSH-ключ этой машины не открывает ключ истории — добавьте ключ с машины, где он есть"
    while IFS= read -r line; do
        case "$line" in ssh-ed25519\ *|ssh-rsa\ *) wrap_for "$line" "$identity" "$label"; label="" ;; esac
    done < <(collect_pubkeys "${args[@]}")
}

cmd_decrypt() {
    local f identity
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) export ZOO_AGE_KEY="${2:?-i КЛЮЧ}"; shift 2 ;;
            -*) usage ;;
            *) break ;;
        esac
    done
    [ $# -gt 0 ] || usage
    need_age
    TMP_DIR="$(mktemp -d)"; chmod 700 "$TMP_DIR"
    identity="$TMP_DIR/history.key"
    if [ -d "$KEYDIR" ] && unwrap "$identity"; then :; else identity=""; fi
    for f in "$@"; do
        [ -f "$f" ] || die "нет файла $f"
        if [ -n "$identity" ] && age -d -i "$identity" "$f" 2>/dev/null; then continue; fi
        # старые отчёты — прямо на SSH-ключ
        local id ok=0
        while IFS= read -r id; do
            if age -d -i "$id" "$f" 2>/dev/null; then ok=1; break; fi
        done < <(ssh_identities)
        [ "$ok" = 1 ] || die "$f: не открывается ни ключом истории, ни SSH-ключами этой машины"
    done
}

# shellcheck disable=SC2154 # AGE_* задаёт versions.env
cmd_install_age() {
    local env="$ROOT/scripts/versions.env" ver base sum url dest="$HOME/.local/bin" tmp os pkg
    [ -f "$env" ] || die "нет $env"
    # shellcheck source=versions.env
    . "$env"
    ver="$AGE_VERSION"
    base="$AGE_URL_BASE"
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) os=windows; sum="$AGE_SHA256_windows_amd64"; url="$base/age-$ver-windows-amd64.zip" ;;
        Linux)
            case "$(uname -m)" in
                x86_64) sum="$AGE_SHA256_amd64"; url="$base/age-$ver-linux-amd64.tar.gz" ;;
                aarch64) sum="$AGE_SHA256_arm64"; url="$base/age-$ver-linux-arm64.tar.gz" ;;
                *) die "архитектура $(uname -m) не закреплена: apt install age" ;;
            esac
            os=linux ;;
        *) die "эта ОС не закреплена в versions.env: brew install age" ;;
    esac
    tmp="$(mktemp -d)"
    TMP_DIR="$tmp"
    pkg="$tmp/age.${url##*.}"        # Expand-Archive берёт только .zip
    info "скачиваю $url"
    curl -fsSL --retry 3 -o "$pkg" "$url"
    echo "$sum  $pkg" | sha256sum -c --quiet - || die "sha256 не совпал: $url"
    mkdir -p "$dest"
    if [ "$os" = windows ]; then
        powershell.exe -NoProfile -Command "Expand-Archive -Force -LiteralPath '$(host_path "$pkg")' -DestinationPath '$(host_path "$tmp")'" \
            >/dev/null
        cp "$tmp/age/age.exe" "$tmp/age/age-keygen.exe" "$dest/"
    else
        tar -xzf "$pkg" -C "$tmp" age/age age/age-keygen
        install -m 0755 "$tmp/age/age" "$tmp/age/age-keygen" "$dest/"
    fi
    info "age $ver → $dest (добавьте каталог в PATH, если его там нет)"
}

case "${1:-}" in
    push) shift; cmd_push "$@" ;;
    pull) shift; cmd_pull "$@" ;;
    keygen) shift; cmd_keygen "$@" ;;
    addkey) shift; cmd_addkey "$@" ;;
    decrypt) shift; cmd_decrypt "$@" ;;
    install-age) shift; cmd_install_age "$@" ;;
    -h|--help) usage 0 ;;
    *) usage ;;
esac
