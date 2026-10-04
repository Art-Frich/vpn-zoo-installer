#!/usr/bin/env bash
# history.sh — история проб на машине владельца: каталог history/ в репо (history/README.md).
#
#   history.sh push [-p ПОРТ] [--tag МЕТКА] [--device УСТРОЙСТВО] USER@СЕРВЕР [ОТЧЁТ]
#       отправить клиентский отчёт (по умолчанию probe/probe-report.json) в историю сервера:
#       ssh … zoo history add -
#   history.sh pull [-p ПОРТ] [--since 30d] USER@СЕРВЕР
#       забрать с сервера анонимный jsonl и сырые отчёты (age → history/recipients.txt) и
#       дополнить history/ без дублей: ssh … zoo history export --tar | tar -x
#   history.sh decrypt [-i КЛЮЧ] ФАЙЛ.age…
#       расшифровать сырой отчёт приватным ключом SSH (по умолчанию ~/.ssh/id_ed25519)
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
PROBE_IMAGE="${ZOO_PROBE_IMAGE:-zoo-probe}"

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

cmd_decrypt() {
    local key="${ZOO_AGE_KEY:-$HOME/.ssh/id_ed25519}" f
    while [ $# -gt 0 ]; do
        case "$1" in
            -i) key="${2:?-i КЛЮЧ}"; shift 2 ;;
            -*) usage ;;
            *) break ;;
        esac
    done
    [ $# -gt 0 ] || usage
    [ -f "$key" ] || die "нет ключа $key (-i КЛЮЧ)"
    for f in "$@"; do
        [ -f "$f" ] || die "нет файла $f"
        if command -v age >/dev/null 2>&1; then
            age -d -i "$key" "$f"
        elif command -v docker >/dev/null 2>&1 && docker image inspect "$PROBE_IMAGE" >/dev/null 2>&1; then
            # age есть в образе пробника; ключ с парольной фразой так не открыть — нужен age на машине
            MSYS_NO_PATHCONV=1 docker run --rm -i --entrypoint age \
                -v "$(host_path "$(dirname "$key")"):/key:ro" "$PROBE_IMAGE" \
                -d -i "/key/$(basename "$key")" < "$f"
        else
            die "нет age: winget install FiloSottile.age (Windows), brew install age (macOS), apt install age (Linux) или scripts/history.sh install-age; либо соберите образ пробника ($PROBE_IMAGE)"
        fi
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
    decrypt) shift; cmd_decrypt "$@" ;;
    install-age) shift; cmd_install_age "$@" ;;
    -h|--help) usage 0 ;;
    *) usage ;;
esac
