# Тестовый стенд (docker/)

Стенд прогоняет инсталлер на «почти настоящем» VPS: Ubuntu 22.04/24.04 в контейнере, где systemd работает как PID 1. Сервисы (x-ui, hysteria-server, ufw, fail2ban) поднимаются по-настоящему, порты слушают, `systemctl` и `journalctl` работают.

## Быстрый старт

```bash
# полный прогон на 24.04, по фазе за раз, отчёт в docker/out/<ts>/
docker/test.sh

# как у пользователя: один install.sh, права файлов как после git clone
docker/test.sh --mode full

# только часть фаз, 22.04, контейнер оставить для отладки
docker/test.sh --distro 22.04 --phases 00-bootstrap,03-3xui --keep

# флаги инсталлера
docker/test.sh --env ENABLE_TUIC=1 --env AWG_ENGINE=userspace
```

Ручное управление сервером:

```bash
docker/run-server.sh up dev --distro 24.04     # контейнер zoo-dev в сети zoo-net, репо в /repo
docker/run-server.sh sync dev                  # залить свежие правки рабочего дерева
docker/run-server.sh install dev ENABLE_XHTTP=0 -- --phase 04-vless-reality
docker/run-server.sh shell dev                 # bash внутри, cwd=/repo
docker/run-server.sh down dev                  # или down --all — все контейнеры стенда и сеть

# смоук адаптера API 3x-ui (scripts/lib/xui.sh) после фазы 03
docker/run-server.sh exec dev bash /repo/docker/xui-smoke.sh
```

На Windows/Git Bash скрипты сами выставляют `MSYS_NO_PATHCONV=1`. Если запускаете `docker` руками, ставьте его перед командой, иначе Git Bash испортит пути `/sys/fs/cgroup` и `/repo`.

## Окружение `ZOO_TEST_ENV=docker`

Это контракт между стендом и инсталлером. Стенд передаёт `ZOO_TEST_ENV=docker` в каждый `docker exec` и в окружение контейнера. В `scripts/lib.sh`:

- `is_test_docker` — истина при `ZOO_TEST_ENV=docker`;
- `is_container` при `ZOO_TEST_ENV=docker` возвращает **ложь**, поэтому старые ветки «контейнер — пропускаю systemd» в стенде не срабатывают.

Старый `CONTAINER_MODE=1` и автодетект по `/.dockerenv` означали «systemd нет, почти всё пропускаем». В стенде systemd есть, поэтому при `is_test_docker` фаза пропускает **только** то, что в контейнере невозможно или опасно для хоста:

| Что | Почему | Как фаза должна себя вести |
|---|---|---|
| Сборка и загрузка модуля ядра amneziawg (DKMS, `modprobe`, `linux-headers-$(uname -r)`) | ядро общее с хостом (WSL2 6.6 / ядро Linux-хоста), заголовков под него в apt нет, модуля amneziawg нет | `AWG_ENGINE=auto` выбирает `userspace` (amneziawg-go); `/dev/net/tun` в контейнере есть |
| Установка HWE-ядра и reboot (02) | ядро не наше, reboot контейнера бессмысленен | проверка совместимости выполняется, установка ядра — нет |
| swap | `swapon` в контейнере влияет на VM Docker | пропуск с `[!]` |
| Глобальные sysctl: `vm.*`, `net.core.*`, `net.ipv4.udp_mem`, `net.netfilter.nf_conntrack_max` | они не привязаны к сетевому неймспейсу. `vm.swappiness` из privileged-контейнера меняется для всей VM Docker (проверено: после старого 00 соседний контейнер видит 10). `net.core.*` и `udp_mem` в неймспейсе контейнера просто отсутствуют, `nf_conntrack_max` даёт EPERM | файл `/etc/sysctl.d/…` писать можно, применять только namespaced-параметры (`net.ipv4.ip_forward`, `net.ipv4.tcp_*`) |
| BBR | в Docker Desktop (WSL2 6.6) `tcp_bbr` подгружается при первом `sysctl tcp_congestion_control=bbr` из privileged-контейнера. На другом хосте модуля может не быть | отсутствие BBR — предупреждение, не ошибка |

Всё остальное выполняется как на VPS: apt, ufw enable (правила живут в неймспейсе контейнера), fail2ban, systemd-юниты, ожидание портов, API 3x-ui.

## Права файлов (`--modes`)

`--modes git` (по умолчанию) раскладывает файлы с правами из индекса git, как после `git clone` на VPS. Неотслеживаемые файлы получают 644: именно так их запишет `git add` на Windows (`core.filemode=false`). Скрипт добавляйте командой `git add --chmod=+x <файл>`, исполняемость уже закоммиченного файла меняет `git update-index --chmod=+x <файл>`. `--modes fs` делает исполняемыми все файлы с shebang. Он нужен, чтобы погонять логику фаз, пока права в git не исправлены.

## Отчёт

`docker/out/<ts>/` (в `.gitignore`):

| Файл | Что внутри |
|---|---|
| `summary.md`, `summary.tsv` | фаза, итог, код возврата, длительность, число `[!]`, причина |
| `phases/<фаза>.log` | полный вывод `install.sh --phase <фаза>` (в `--mode full` — `phases/full.log`) |
| `diag.txt` | systemd, `ss -tulpn`, ufw, iptables/nft, версии компонентов, state, журналы сервисов |
| `files/` | `/etc/vpn-setup`, state, CREDENTIALS.md, конфиги hysteria/awg/xray, БД x-ui. **Секреты тестового стенда** |
| `meta.txt` | дистрибутив, режимы, git HEAD и число незакоммиченных файлов |

Итоги фаз: `PASS`, `FAIL`, `TIMEOUT`, `NOEXEC` (код 126, нет +x), `SKIPPED` (install.sh пропустил фазу сам), `REBOOT`, `NOT_RUN`.

## Тесты протоколов (`docker/tests/`)

Сквозные тесты трафика по одному на протокол. Аргумент — уже установленный сервер стенда:

```bash
docker/tests/vless-reality.sh zoo-dev    # VLESS RAW+REALITY+Vision (клиент Xray)
docker/tests/vless-xhttp.sh zoo-dev      # VLESS XHTTP+REALITY (клиент Xray)
docker/tests/ss2022.sh zoo-dev           # Shadowsocks-2022 (клиент Xray), + UDP (DNS)
docker/tests/tuic.sh zoo-dev             # TUIC v5 (клиент sing-box, пин из versions.env)
docker/tests/hysteria2.sh zoo-dev        # Hysteria2 (+ Salamander и hopping, если включены)
docker/tests/amneziawg.sh zoo-dev        # AmneziaWG (amneziawg-go + awg с сервера)
docker/tests/routing.sh zoo-dev          # анти-утечки: echo, RU_EGRESS, sniffing, WARP, AWG L3
docker/tests/security.sh zoo-dev         # сокеты/ufw/права/секреты/3x-ui + из каждого туннеля: echo, loopback и
                                         # сервисы на адресе сервера за ufw недоступны (на время теста ALIAS_IP на lo)
docker/tests/links.sh zoo-dev [USER]     # ссылки USER (owner) из манифестов, разобранные как в клиентах
                                         # (v2rayN-разбор vless/ss → Xray, tuic → sing-box, hysteria2:// — как есть)

# или сразу после установки: --tests all | список через запятую
docker/test.sh --mode full --tests all
```

Каждый тест поднимает отдельный клиентский контейнер в `zoo-net` (`<сервер>-…`), строит клиента из `probe` манифеста, **закрывает клиенту прямой выход в интернет** (iptables, `--cap-add NET_ADMIN`) и проверяет: малый запрос (204), загрузку ≥2 МБ, IP выхода = IP сервера, а также `user_add` / `user_enable false|true` / `user_del` через `scripts/lib/proto-<id>.sh`. Без блокировки выхода проверка IP бессмысленна: клиент и сервер выходят через один NAT хоста.

`links.sh` проверяет не `probe`, а сами ссылки, которые человек вставит в приложение: ловит расхождения «probe работает, ссылка нет». С `LINKS_FILE=файл` (строки `id<TAB>uri`) и `EXPECT_FAIL=1` — проверка, что сохранённые ссылки удалённого или выключенного пользователя больше не пускают.

IP выхода берётся из `https://www.cloudflare.com/cdn-cgi/trace` (строка `ip=`): после фазы 07 `api.ipify.org` и другие echo-сервисы через туннель блокируются или уходят в WARP (D9). jq на хосте не нужен — тесты вызывают его внутри контейнеров.

## Пробники и цензор

`test.sh` подхватывает их по мере появления:

- если на сервере есть команда `zoo`, после установки выполняется `zoo probe --local --json` и результат сохраняется в `probe-local.json`;
- если есть `docker/probe/run.sh`, он вызывается как `run.sh <контейнер-сервер> <каталог-отчёта>` и пишет результаты в каталог отчёта (см. [probe/README.md](probe/README.md));
- цензор — [censor/README.md](censor/README.md).

## Образы, сеть, метки

| Объект | Имя |
|---|---|
| образ сервера | `zoo-test-server:24.04`, `zoo-test-server:22.04` (`server.Dockerfile`, база `jrei/systemd-ubuntu`) |
| образ пробника | `zoo-probe` (`probe.Dockerfile`, контекст — корень репо) |
| сеть | `zoo-net` |
| контейнеры | `zoo-<имя>`, метка `zoo.harness=1`; `down` и `up` удаляют только контейнеры с этой меткой |

Образ сервера минимален: curl, ca-certificates, iproute2, iptables, sudo, openssh-server, kmod, procps. Остальное (jq, sqlite3, python3, ufw, psmisc…) инсталлер обязан ставить сам. На облачных образах Ubuntu часть этих пакетов уже стоит, а на минимальных образах хостеров её может не быть.
