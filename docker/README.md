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

# полный e2e, как в приёмке: установка, самопроверка, пробник с цензором, все тесты
docker/test.sh --mode full --tests all --distro 24.04 --name e2e1     --env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1 --env HY2_HOP=1 --env ENABLE_WARP=1
```

Остальные ключи: `--name NAME` (контейнер `zoo-NAME`; для параллельных прогонов — разные имена), `--timeout СЕК` (на каждый вызов `install.sh`, по умолчанию 1800), `--stop-on-fail`, `--probe-profiles` (ниже), `--serial` (всё по очереди — для отладки флаппинга; по умолчанию тесты протоколов и links идут параллельно, профили цензора — параллельно, AmneziaWG — вторым проходом по очереди: у пира один endpoint). Код выхода 0 — все фазы `PASS` или выключены флагом, все выбранные тесты `PASS`, самопроверка и пробники не `FAIL`.

Не правьте `docker/*.sh` во время прогона: bash читает скрипт по ходу выполнения, и запущенный `test.sh` упадёт. На Windows/Git Bash долгий прогон иногда обрывается, когда завершается соседний процесс (код 0 без `summary.md`); надёжнее запускать его отдельным окном и с `--keep`, чтобы дотестировать оставшееся руками (`docker/tests/<тест>.sh zoo-NAME`).

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

Итоги фаз: `PASS`, `FAIL`, `TIMEOUT`, `NOEXEC` (код 126, нет +x), `SKIPPED` (install.sh пропустил фазу сам; «выключена (ENABLE_…=0)» провалом не считается), `REBOOT`, `NOT_RUN`. Ниже фаз в `summary.tsv` — строки `install-selftest`, `probe-local`, `probe-client`, `probe:<профиль>` и по строке на каждый тест из `--tests`.

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
docker/tests/collector.sh zoo-dev        # коллектор трафика под песочницей, AWG-счётчики, скрытый zoo-probe
docker/tests/journal.sh zoo-dev          # журнал атак: скан портов, перебор SSH до бана, неверный ключ Hy2, «свой» адрес
docker/tests/history.sh zoo-dev          # история проб: метрики, SQLite, рейтинг, анонимный экспорт без IP, age на ключ SSH,
                                         # scripts/history.sh через подменённый ssh, админка (нужны образ zoo-probe и age от фазы 09)
docker/tests/ssh-harden.sh zoo-dev       # SSH_HARDEN (фаза 01b): отказ без ключа, шаг 1, автооткат, подтверждение,
                                         # SSH_HARDEN=0 в два запуска, повторы; при полной установке — 07/99/zoo на новом порту

# или сразу после установки: --tests all | список через запятую
docker/test.sh --mode full --tests all
```

Каждый тест поднимает отдельный клиентский контейнер в `zoo-net` (`<сервер>-…`), строит клиента из `probe` манифеста, **закрывает клиенту прямой выход в интернет** (iptables, `--cap-add NET_ADMIN`) и проверяет: малый запрос (204), загрузку ≥2 МБ, IP выхода = IP сервера, а также `user_add` / `user_enable false|true` / `user_del` через `scripts/lib/proto-<id>.sh`. Без блокировки выхода проверка IP бессмысленна: клиент и сервер выходят через один NAT хоста.

`links.sh` проверяет не `probe`, а сами ссылки, которые человек вставит в приложение: ловит расхождения «probe работает, ссылка нет». С `LINKS_FILE=файл` (строки `id<TAB>uri`) и `EXPECT_FAIL=1` — проверка, что сохранённые ссылки удалённого или выключенного пользователя больше не пускают.

IP выхода берётся из `https://www.cloudflare.com/cdn-cgi/trace` (строка `ip=`): после фазы 07 `api.ipify.org` и другие echo-сервисы через туннель блокируются или уходят в WARP (D9). jq на хосте не нужен — тесты вызывают его внутри контейнеров.

## Пробники и цензор

- `--mode full`: `install.sh` сам запускает самопроверку после 99. Строка `install-selftest` в итоге — PASS, если в журнале есть сводка «работает в принципе» и следующий шаг, а в `/etc/vpn-setup/probe-export.json` вложены её вердикты;
- после установки `test.sh` выполняет `zoo probe --local --json` (креды служебного пользователя `zoo-probe`) и сохраняет `probe-local.json`;
- `docker/probe/run.sh <контейнер-сервер> <каталог-отчёта>` — клиентский пробник напрямую (`direct`) и через цензора по профилям; сверка вердиктов с ожидаемыми — `probe/expect.tsv`, строки `probe:<профиль>` в итоге. Профили — `--probe-profiles direct,clean,drop-udp,ip-block,freeze-16k,rst-tls` (по умолчанию все, `none` — без пробника; `port-block` только явно). Подробно — [probe/README.md](probe/README.md);
- цензор — [censor/README.md](censor/README.md).

## Админка и коллектор трафика

С `--tests all` при установленном `zoo` после тестов протоколов идут ещё и эти:

- `web` — `zoo/tests/web_smoke.sh --users` на сервере: вход, CSP, CSRF, все страницы и полный цикл пользователя через формы (добавить во все протоколы, отключить, включить, удалить);
- `journal` — [tests/journal.sh](tests/journal.sh): три клиентских контейнера (`<сервер>-scan`, `-ssh`, `-own`) делают настоящие вещи — сканируют закрытые порты и шлют мусор вместо баннера SSH, перебирают пароли SSH до бана fail2ban, предъявляют Hysteria2 неверный ключ, входят по ключу; затем `zoo-collector.service` (журнал атак идёт его `ExecStartPost` под песочницей) и проверки: адреса источников, виды событий, порты, бан, «свой» и «локальный» (контейнеры стенда — приватные адреса), страница «Атаки», и чего в базе быть не должно (пароли, имена, строки журнала, пакеты с VPN-интерфейса, назначения пользователей в логе Xray). REALITY-пробу тест намеренно не требует (D32). Ограничение стенда: LOG из сети контейнера не попадает в журнал ядра, поэтому строки `[UFW BLOCK]` вбрасываются через `/dev/kmsg` в формате ufw (в выводе — «вброшено»); тракт «журнал ядра → коллектор → база» проверен, само появление строки ядром — нет. Тест около 45 с (замер 05.10.2026). Адреса контейнеров стенда переиспользуются, поэтому тест стирает следы своих адресов в `journal.sqlite` до и после.
- `live` — [tests/live.sh](tests/live.sh): живые метрики и вкл/выкл с карточки под настоящим systemd и через настоящую админку. `zoo-live.timer`/`.path`/`zoo-job.path` включены, у `zoo-live.service` `Nice=10` и `CPUQuota=30%`; прогон `zoo-live.service` — у каждого протокола строка в `live` с задержкой, джиттером и (первый раз) скоростью, второй прогон скорость не меряет, печатаются время, CPU и байты на eth0; `POST /live/<id>` из песочницы админки → заявка → `zoo-live.path` → новый замер, повторный в первую минуту отклонён, без CSRF — 403; `POST /protocols/<id>/disable` → `zoo-job.path` → `ENABLE_*=0` в config.env, манифест `enabled=false`, правило ufw снято, затем `enable` — всё вернулось (ссылки, пользователи); защита «последний протокол». Протокол — `LIVE_TOGGLE_PROTO` (по умолчанию `tuic`). Около 110 с (из них ≈60 с — ожидание rate-limit ↻).
- `collector` — [tests/collector.sh](tests/collector.sh): `zoo-collector.service` под своей песочницей (ProtectSystem=strict, PrivateDevices) снимает счётчики без ошибок, у каждого включённого протокола есть серия, счётчик AmneziaWG у `zoo-probe` растёт после пробы через туннель; `zoo-probe` скрыт из `zoo user list` и `zoo traffic` (виден с `--all`), удалить его без `--force` нельзя.
- `history` — [tests/history.sh](tests/history.sh), только если есть образ `zoo-probe` (его собирает прогон пробника): `zoo probe --local` с метками и замером отдачи (метрики, запись в `probe-history.sqlite`, без дублей), клиентский пробник с каталогом `/history` (jsonl без IP, `.age` открывается ключом ssh-ed25519 и не открывается чужим), `zoo history add`, `zoo probe --rank`, `zoo history export --tar`, `scripts/history.sh pull/push/decrypt` через подменённый ssh и блоки истории на странице «Проверка». Тест 63 с на 24.04 (замер 05.10.2026); отдельно: `ZOO_PROBE_IMAGE=… docker/tests/history.sh zoo-NAME`.

## Закрытие SSH (`ssh-harden`)

С `--tests all` последним идёт [tests/ssh-harden.sh](tests/ssh-harden.sh): он переносит SSH сервера и в конце возвращает исходное (`SSH_HARDEN=0`). Клиент — контейнер `<сервер>-sshcli` со своим ключом; на время теста ключ root сервера откладывается в сторону, заводится пользователь с sudo `zoosshtest`, а адрес клиента — в `ignoreip` fail2ban (пробы «без ключа» иначе дали бы бан). Между SSH-подключениями — пауза 7 с: `ufw limit` отвергает шестое подключение за 30 с с одного адреса. Тест идёт около 4 минут (замер 05.10.2026); таймер отката на стенде — 15 с (`ZOO_TEST_REVERT_SEC`, действует только при `ZOO_TEST_ENV=docker`). Без фазы 01 на сервере тест сам ставит 00 и 01, так что годится и голый контейнер: `docker/run-server.sh up ssh --distro 22.04 && docker/tests/ssh-harden.sh zoo-ssh`.

## Образы, сеть, метки

| Объект | Имя |
|---|---|
| образ сервера | `zoo-test-server:24.04`, `zoo-test-server:22.04` (`server.Dockerfile`, база `jrei/systemd-ubuntu`) |
| образ пробника | `zoo-probe` (`probe.Dockerfile`, контекст — корень репо) |
| сеть | `zoo-net` |
| контейнеры | `zoo-<имя>`, метка `zoo.harness=1`; `down` и `up` удаляют только контейнеры с этой меткой |

Образ сервера минимален: curl, ca-certificates, iproute2, iptables, sudo, openssh-server, kmod, procps. Остальное (jq, sqlite3, python3, ufw, psmisc…) инсталлер обязан ставить сам. На облачных образах Ubuntu часть этих пакетов уже стоит, а на минимальных образах хостеров её может не быть.
