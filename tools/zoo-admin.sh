#!/usr/bin/env bash
# Открыть админку vpn-zoo со своего компьютера (macOS, Linux, Git Bash на Windows):
# SSH-туннель до сервера + одноразовая ссылка входа (живёт 3 минуты) в браузере — токен не нужен.
#
#   bash tools/zoo-admin.sh root@СЕРВЕР                 # обычный вход по ключу
#   bash tools/zoo-admin.sh root@СЕРВЕР -p 2222 -i ~/.ssh/vps   # свой порт SSH / ключ (любые опции ssh)
#
# Туннель остаётся в фоне и после выхода скрипта; закрыть — pkill -f "ПОРТ:127.0.0.1:ПОРТ".
# ZOO_ADMIN_NO_OPEN=1 — не открывать браузер, только напечатать ссылку.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "использование: bash tools/zoo-admin.sh root@СЕРВЕР [опции ssh]" >&2
    exit 2
fi
host="$1"
shift

# root — напрямую, иначе через sudo; без кавычек, чтобы одинаково проходило через любые оболочки
remote='zoo web --info --json 2>/dev/null || sudo zoo web --info --json'
if ! info="$(ssh "$@" "$host" "$remote")"; then
    echo "не удалось спросить сервер: zoo установлен? SSH-ключ добавлен? sudo без пароля?" >&2
    exit 1
fi
port="$(printf '%s\n' "$info" | sed -n 's/.*"port": *"\{0,1\}\([0-9][0-9]*\).*/\1/p' | head -n 1)"
link="$(printf '%s\n' "$info" | sed -n 's/.*"link": *"\([^"]*\)".*/\1/p' | head -n 1)"
if [ -z "$port" ] || [ -z "$link" ]; then
    echo "сервер ответил неожиданно — обновите zoo на сервере (sudo zoo upgrade --pull --apply)" >&2
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
