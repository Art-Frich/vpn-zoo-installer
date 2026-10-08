#!/usr/bin/env bash
# Открыть админку vpn-zoo со своего компьютера (macOS, Linux, Git Bash на Windows):
# SSH-туннель до сервера + одноразовая ссылка входа (живёт 3 минуты) в браузере — токен не нужен.
#
# Один раз на своём компьютере (ключ, запись «zoo» в ~/.ssh/config, команда zoo-admin):
#   bash zoo-admin.sh --setup root@СЕРВЕР [-p ПОРТ] [--name ИМЯ]
# Потом каждый день:
#   zoo-admin                    # админка (сервер «zoo» из ~/.ssh/config)
#   ssh zoo                      # консоль сервера
# Без настройки:
#   bash zoo-admin.sh root@СЕРВЕР                       # вход по ключу
#   bash zoo-admin.sh root@СЕРВЕР -p 2222 -i ~/.ssh/vps   # свой порт SSH / ключ (любые опции ssh)
#
# Туннель остаётся в фоне и после выхода скрипта; закрыть — pkill -f "ПОРТ:127.0.0.1:ПОРТ".
# ZOO_ADMIN_NO_OPEN=1 — не открывать браузер, только напечатать ссылку.
# ZOO_ADMIN_NO_COPY_ID=1 — при --setup не использовать ssh-copy-id.
set -euo pipefail

usage() {
    echo "использование: zoo-admin [СЕРВЕР|root@IP] [опции ssh]   (без аргументов — сервер zoo)" >&2
    echo "               zoo-admin --setup root@IP [-p ПОРТ] [--name ИМЯ]   (один раз)" >&2
}

setup() {
    local target="" port=22 name=zoo
    while [ $# -gt 0 ]; do
        case "$1" in
            -p|--port) [ $# -ge 2 ] || { usage; return 2; }; port="$2"; shift 2 ;;
            --name) [ $# -ge 2 ] || { usage; return 2; }; name="$2"; shift 2 ;;
            -*) echo "неизвестная опция: $1" >&2; usage; return 2 ;;
            *) [ -z "$target" ] || { usage; return 2; }; target="$1"; shift ;;
        esac
    done
    local user="${target%%@*}" host="${target#*@}"
    if [ -z "$target" ] || [ "$user" = "$target" ] || ! [[ "$user" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]]; then
        echo "нужен адрес вида пользователь@сервер, например root@203.0.113.5" >&2
        return 2
    fi
    host="${host#[}"; host="${host%]}"
    if ! [[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ || ( "$host" == *:* && "$host" =~ ^[0-9A-Fa-f:.]+$ ) ]]; then
        echo "странный адрес сервера: $host" >&2
        return 2
    fi
    if ! [[ "$port" =~ ^[0-9]{1,5}$ ]] || [ "$((10#$port))" -lt 1 ] || [ "$((10#$port))" -gt 65535 ]; then
        echo "порт SSH — число от 1 до 65535" >&2
        return 2
    fi
    port="$((10#$port))"
    if ! [[ "$name" =~ ^[A-Za-z][A-Za-z0-9_-]{0,30}$ ]]; then
        echo "имя для ssh — буквы, цифры, - и _ (например zoo)" >&2
        return 2
    fi

    local ssh_dir="$HOME/.ssh" key="$HOME/.ssh/id_ed25519"
    mkdir -p "$ssh_dir"
    chmod 700 "$ssh_dir" 2>/dev/null || true

    if [ -f "$key" ]; then
        echo "1/4 ключ уже есть: $key"
    else
        echo "1/4 создаю ключ $key (пароль на ключ — по желанию)"
        ssh-keygen -t ed25519 -f "$key" -C "zoo-admin@$(hostname 2>/dev/null || echo pc)"
    fi
    [ -f "$key.pub" ] || ssh-keygen -y -f "$key" > "$key.pub"

    echo "2/4 кладу ключ на сервер (спросит пароль сервера один раз)"
    if [ "${ZOO_ADMIN_NO_COPY_ID:-0}" != "1" ] && command -v ssh-copy-id >/dev/null 2>&1; then
        ssh-copy-id -i "$key.pub" -p "$port" "$user@$host"
    else
        # без кавычек внутри команды (так же в .ps1); tr убирает CR на случай CRLF в ключе; дубликат строки не добавляется
        ssh -p "$port" "$user@$host" \
            'umask 077; mkdir -p ~/.ssh; tr -d \\r > ~/.ssh/zoo-key.tmp; grep -qxFf ~/.ssh/zoo-key.tmp ~/.ssh/authorized_keys 2>/dev/null || cat ~/.ssh/zoo-key.tmp >> ~/.ssh/authorized_keys; rm -f ~/.ssh/zoo-key.tmp' \
            < "$key.pub"
    fi

    local cfg="$ssh_dir/config"
    if [ -f "$cfg" ] && grep -Eiq "^[[:space:]]*Host[[:space:]]+([^#]*[[:space:]])?$name([[:space:]]|\$)" "$cfg"; then
        echo "3/4 в $cfg запись «$name» уже есть — не трогаю"
    else
        echo "3/4 добавляю в $cfg запись «$name»"
        [ -f "$cfg" ] || { : > "$cfg"; chmod 600 "$cfg" 2>/dev/null || true; }
        {
            [ ! -s "$cfg" ] || echo
            printf 'Host %s\n    HostName %s\n    User %s\n    Port %s\n    IdentityFile ~/.ssh/id_ed25519\n    ServerAliveInterval 20\n' \
                "$name" "$host" "$user" "$port"
        } >> "$cfg"
    fi

    local bin_dir="$HOME/.local/bin" self="${BASH_SOURCE[0]}"
    mkdir -p "$bin_dir"
    if [ -f "$self" ]; then
        if [ ! "$self" -ef "$bin_dir/zoo-admin" ]; then
            cp "$self" "$bin_dir/zoo-admin"
        fi
        chmod +x "$bin_dir/zoo-admin"
        echo "4/4 команда zoo-admin: $bin_dir/zoo-admin"
    else
        echo "4/4 команду zoo-admin не поставил: скрипт запущен не из файла (скачайте его и повторите)"
    fi
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) echo "    $bin_dir нет в PATH: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
    esac

    if ssh -o BatchMode=yes -o ConnectTimeout=10 "$name" true >/dev/null 2>&1; then
        echo "вход по ключу работает"
    else
        echo "вход по ключу пока не проверился (пароль на ключе? ssh-agent?) — попробуйте: ssh $name" >&2
    fi
    echo
    echo "готово. Теперь:"
    echo "  ssh $name                 — консоль сервера"
    if [ "$name" = zoo ]; then
        echo "  zoo-admin                — админка в браузере"
    else
        echo "  zoo-admin $name          — админка в браузере"
    fi
}

case "${1:-}" in
    --setup) shift; setup "$@"; exit $? ;;
    -h|--help) usage; exit 0 ;;
    ""|-*) host=zoo ;;
    *) host="$1"; shift ;;
esac

# root — напрямую, иначе через sudo; без кавычек, чтобы одинаково проходило через любые оболочки
remote='zoo web --info --json 2>/dev/null || sudo zoo web --info --json'
if ! info="$(ssh "$@" "$host" "$remote")"; then
    echo "не удалось спросить сервер: zoo установлен? SSH-ключ добавлен? sudo без пароля?" >&2
    exit 1
fi
port="$(printf '%s\n' "$info" | sed -n 's/.*"port": *"\{0,1\}\([0-9][0-9]*\)"\{0,1\} *[,}].*/\1/p' | head -n 1)"
link="$(printf '%s\n' "$info" | sed -n 's/.*"link": *"\([^"]*\)".*/\1/p' | head -n 1)"
if [ -z "$port" ] || [ -z "$link" ]; then
    echo "сервер ответил неожиданно — обновите zoo на сервере (sudo zoo upgrade --pull --apply)" >&2
    exit 1
fi
# ответу сервера не доверяем: ссылка уходит в open/start/xdg-open
link_re="^http://127\\.0\\.0\\.1:${port}/login\\?once=[A-Za-z0-9._-]+\$"
if [ "${#port}" -gt 5 ] || [ "$((10#$port))" -lt 1 ] || [ "$((10#$port))" -gt 65535 ] || ! [[ "$link" =~ $link_re ]]; then
    echo "сервер вернул странный порт или ссылку — остановились (обновите zoo на сервере)" >&2
    exit 1
fi

if curl -s -o /dev/null -m 3 "http://127.0.0.1:$port/login" 2>/dev/null; then
    echo "туннель на 127.0.0.1:$port уже открыт"
else
    ssh "$@" -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
        -L "$port:127.0.0.1:$port" "$host"
    echo "туннель открыт: 127.0.0.1:$port → сервер (в фоне)"
fi

echo "вход (ссылка одноразовая, 3 минуты): $link"
if [ "${ZOO_ADMIN_NO_OPEN:-0}" != "1" ]; then
    case "$(uname -s)" in
        Darwin) open "$link" ;;
        # //c: иначе MSYS превращает /c в путь, cmd открывается интерактивно и скрипт «висит»
        MINGW*|MSYS*|CYGWIN*) cmd.exe //c start "" "$link" </dev/null >/dev/null 2>&1 ;;
        *) xdg-open "$link" >/dev/null 2>&1 || echo "откройте ссылку в браузере вручную" ;;
    esac
fi
echo "дальше админка открывается по http://127.0.0.1:$port/ (вход запоминается до 30 дней, без заходов — на 14; потом снова этот скрипт)"
