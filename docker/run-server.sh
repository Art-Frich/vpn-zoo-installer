#!/usr/bin/env bash
# run-server.sh — жизненный цикл тестового сервера (Ubuntu + systemd в контейнере)
#
#   docker/run-server.sh build [24.04|22.04]
#   docker/run-server.sh up NAME [--distro 24.04|22.04] [--modes git|fs]
#   docker/run-server.sh sync NAME [--modes git|fs]
#   docker/run-server.sh install NAME [VAR=val ...] [-- аргументы install.sh]
#   docker/run-server.sh exec NAME команда [аргументы...]
#   docker/run-server.sh shell NAME
#   docker/run-server.sh ip NAME
#   docker/run-server.sh down NAME|--all
#
# NAME без префикса «zoo-» получает его автоматически. Трогаем только контейнеры
# с меткой zoo.harness=1. Файл можно source'ить — так делает docker/test.sh.

set -euo pipefail

# Git Bash иначе превращает /sys/fs/cgroup и /repo в C:/Program Files/Git/...
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

ZOO_DOCKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZOO_REPO_ROOT="$(cd "$ZOO_DOCKER_DIR/.." && pwd)"
ZOO_NET="zoo-net"
ZOO_IMAGE="zoo-test-server"
ZOO_DEFAULT_DISTRO="24.04"

zoo_log()  { echo "[zoo] $*" >&2; }
zoo_warn() { echo "[zoo][!] $*" >&2; }
zoo_die()  { echo "[zoo][x] $*" >&2; exit 1; }

# docker.exe — нативный Windows-бинарь: пути для -f и контекста сборки нужны в виде D:/...
zoo_host_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}

zoo_name() {
    case "$1" in
        zoo-*) printf '%s\n' "$1" ;;
        "")    zoo_die "не задано имя контейнера" ;;
        *)     printf 'zoo-%s\n' "$1" ;;
    esac
}

zoo_check_distro() {
    case "$1" in
        22.04|24.04) ;;
        *) zoo_die "неподдерживаемый дистрибутив: $1 (есть 22.04, 24.04)" ;;
    esac
}

zoo_build() {
    local distro="${1:-$ZOO_DEFAULT_DISTRO}"
    zoo_check_distro "$distro"
    zoo_log "сборка $ZOO_IMAGE:$distro"
    docker build -q \
        -f "$(zoo_host_path "$ZOO_DOCKER_DIR/server.Dockerfile")" \
        --build-arg "UBUNTU=$distro" \
        -t "$ZOO_IMAGE:$distro" \
        "$(zoo_host_path "$ZOO_DOCKER_DIR")" >/dev/null
}

zoo_net_ensure() {
    docker network inspect "$ZOO_NET" >/dev/null 2>&1 \
        || docker network create --label zoo.harness=1 "$ZOO_NET" >/dev/null
}

zoo_exists() { docker container inspect "$1" >/dev/null 2>&1; }

zoo_is_ours() {
    [ "$(docker container inspect -f '{{index .Config.Labels "zoo.harness"}}' "$1" 2>/dev/null)" = "1" ]
}

zoo_ip() {
    local name; name="$(zoo_name "$1")"
    docker container inspect -f "{{(index .NetworkSettings.Networks \"$ZOO_NET\").IPAddress}}" "$name"
}

zoo_wait_systemd() {
    local name="$1" state="" i
    for i in $(seq 1 90); do
        state="$(docker exec "$name" systemctl is-system-running 2>/dev/null || true)"
        case "$state" in
            running) return 0 ;;
            degraded)
                zoo_warn "$name: systemd degraded, упавшие юниты:"
                docker exec "$name" systemctl --failed --no-legend --plain >&2 || true
                return 0 ;;
        esac
        [ "$i" -eq 1 ] || sleep 1
    done
    zoo_die "$name: systemd не поднялся за 90 с (состояние: ${state:-нет ответа})"
}

# Копия рабочего дерева в /repo. modes=git — права как после git clone
# (по индексу git; неотслеживаемые файлы получают 644, как их запишет git add на Windows),
# modes=fs — исполняемыми становятся все файлы с shebang.
zoo_sync() {
    local name; name="$(zoo_name "$1")"
    local modes="${2:-git}"
    local list modelist
    list="$(mktemp)"; modelist="$(mktemp)"

    # git.exe тоже нативный: при MSYS_NO_PATHCONV путь в -C не сконвертируется, поэтому через cd
    if (cd "$ZOO_REPO_ROOT" && git rev-parse --is-inside-work-tree) >/dev/null 2>&1; then
        (cd "$ZOO_REPO_ROOT" && git ls-files -co --exclude-standard -z \
            | while IFS= read -r -d '' f; do [ -f "$f" ] && printf '%s\0' "$f"; done) > "$list"
    else
        [ "$modes" = "git" ] && { zoo_warn "не git-репозиторий — modes=fs"; modes="fs"; }
        (cd "$ZOO_REPO_ROOT" && find . -type f -not -path './.git/*' -not -path './docker/out/*' -print0) > "$list"
    fi

    docker exec "$name" bash -c 'rm -rf /repo && mkdir -p /repo'
    (cd "$ZOO_REPO_ROOT" && tar --null -T "$list" -cf -) \
        | docker exec -i "$name" tar -xf - -C /repo --no-same-owner

    if [ "$modes" = "git" ]; then
        (cd "$ZOO_REPO_ROOT" && git ls-files -s | awk -F'\t' '{split($1,a," "); m=(a[1]=="100755")?"755":"644"; print m "\t" $2}') > "$modelist"
        local untracked=() f
        while IFS= read -r f; do
            [ -f "$ZOO_REPO_ROOT/$f" ] || continue
            printf '644\t%s\n' "$f" >> "$modelist"
            if [ "$(head -c2 "$ZOO_REPO_ROOT/$f")" = '#!' ]; then untracked+=("$f"); fi
        done < <(cd "$ZOO_REPO_ROOT" && git ls-files -o --exclude-standard)
        if [ "${#untracked[@]}" -gt 0 ]; then
            zoo_warn "скрипты вне индекса git получат 644 (так их закоммитит git add на Windows):"
            printf '      %s\n' "${untracked[@]}" >&2
            zoo_warn "добавлять так: git add --chmod=+x <файл>"
        fi
        docker exec -i "$name" bash -c 'cd /repo && while IFS=$'"'"'\t'"'"' read -r m p; do [ -f "$p" ] && chmod "$m" "$p"; done' < "$modelist"
    else
        docker exec "$name" bash -c 'cd /repo && find . -type f -print0 | while IFS= read -r -d "" f; do
            if [ "$(head -c2 "$f")" = "#!" ]; then chmod 755 "$f"; else chmod 644 "$f"; fi; done'
    fi
    rm -f "$list" "$modelist"
    zoo_log "$name: репо скопирован в /repo (modes=$modes)"
}

zoo_up() {
    local name; name="$(zoo_name "$1")"; shift
    local distro="$ZOO_DEFAULT_DISTRO" modes="git"
    while [ $# -gt 0 ]; do
        case "$1" in
            --distro) distro="$2"; shift 2 ;;
            --modes)  modes="$2"; shift 2 ;;
            *) zoo_die "up: неизвестный аргумент $1" ;;
        esac
    done
    zoo_check_distro "$distro"

    docker image inspect "$ZOO_IMAGE:$distro" >/dev/null 2>&1 || zoo_build "$distro"
    zoo_net_ensure

    if zoo_exists "$name"; then
        zoo_is_ours "$name" || zoo_die "контейнер $name существует и создан не стендом — не трогаю"
        docker rm -f "$name" >/dev/null
    fi

    zoo_log "$name: старт ($ZOO_IMAGE:$distro, сеть $ZOO_NET)"
    docker run -d --name "$name" --hostname "$name" \
        --label zoo.harness=1 --label zoo.role=server \
        --network "$ZOO_NET" \
        --privileged --cgroupns=host \
        -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
        --tmpfs /run --tmpfs /run/lock \
        -e ZOO_TEST_ENV=docker \
        "$ZOO_IMAGE:$distro" >/dev/null

    zoo_wait_systemd "$name"
    zoo_sync "$name" "$modes"
    zoo_log "$name: готов, IP в $ZOO_NET: $(zoo_ip "$name")"
}

# Запуск install.sh так, как велит README (из корня репо, ./scripts/install.sh).
# SERVER_IP по умолчанию — адрес контейнера в zoo-net, иначе инсталлер возьмёт
# публичный IP хост-машины и ссылки будут вести мимо стенда.
zoo_install() {
    local name; name="$(zoo_name "$1")"; shift
    local envs=(-e ZOO_TEST_ENV=docker -e "SERVER_IP=$(zoo_ip "$name")")
    while [ $# -gt 0 ]; do
        case "$1" in
            --) shift; break ;;
            *=*) envs+=(-e "$1"); shift ;;
            *) break ;;
        esac
    done
    docker exec -w /repo "${envs[@]}" "$name" ./scripts/install.sh "$@"
}

zoo_down() {
    local target="${1:-}"
    if [ "$target" = "--all" ]; then
        local ids
        ids="$(docker ps -aq --filter label=zoo.harness=1)"
        # shellcheck disable=SC2086 # список id через пробел
        [ -z "$ids" ] || docker rm -f $ids >/dev/null
        docker network rm "$ZOO_NET" >/dev/null 2>&1 || true
        zoo_log "все контейнеры стенда удалены"
        return 0
    fi
    local name; name="$(zoo_name "$target")"
    zoo_exists "$name" || return 0
    zoo_is_ours "$name" || zoo_die "контейнер $name создан не стендом — не трогаю"
    docker rm -f "$name" >/dev/null
    zoo_log "$name: удалён"
}

zoo_main() {
    local cmd="${1:-}"; shift || true
    case "$cmd" in
        build)   zoo_build "${1:-$ZOO_DEFAULT_DISTRO}" ;;
        up)      zoo_up "$@" ;;
        sync)    local n="$1"; shift; local m="git"; [ "${1:-}" = "--modes" ] && m="$2"; zoo_sync "$n" "$m" ;;
        install) zoo_install "$@" ;;
        exec)    local n; n="$(zoo_name "$1")"; shift; docker exec -i -e ZOO_TEST_ENV=docker "$n" "$@" ;;
        shell)   docker exec -it -w /repo -e ZOO_TEST_ENV=docker "$(zoo_name "$1")" bash ;;
        ip)      zoo_ip "$1" ;;
        down)    zoo_down "${1:-}" ;;
        ""|-h|--help) sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
        *) zoo_die "неизвестная команда: $cmd (см. --help)" ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    zoo_main "$@"
fi
