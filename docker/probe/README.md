# docker/probe — клиентский пробник

Пробник отвечает на два вопроса (ARCHITECTURE.md §7):

- **работает ли протокол в принципе** — `zoo probe --local` на самом сервере;
- **блокируется ли он у конкретного пользователя** — контейнер `zoo-probe` на его машине: трафик идёт через его провайдера.

Сравнение двух прогонов даёт вердикт по каждому протоколу: «работает у вас», «работает в принципе, блокируется у вас (тип)» или «не работает на самом сервере».

## Как проверить свой интернет (Windows, macOS, Linux)

Нужен Docker (Docker Desktop на Windows и macOS). VPN на компьютере на время проверки выключите, иначе проверяться будет чужой VPN, а не ваш провайдер.

**1. На сервере** — самопроверка и пакет для пробника. `install.sh` делает это сам в конце установки: пакет с итогом самопроверки лежит в `/etc/vpn-setup/probe-export.json`. Обновить вручную:

```bash
sudo zoo probe --local --summary --export /etc/vpn-setup/probe-export.json  # самопроверка + пакет с её итогом
sudo zoo export-probe --out /etc/vpn-setup/probe-export.json                # только пакет; --user NAME — креды другого пользователя
```

Креды в пакете — служебного пользователя `zoo-probe` (не owner): проба с вашей машины не выбивает сессию телефона владельца и не попадает в его трафик.

В пакете **ключи доступа к серверу**. Копируйте его только по `scp`, после проверки удалите и на сервере, и у себя. Утёкший пакет отзывается без смены ключей owner (из каталога клона на сервере): `sudo zoo user del zoo-probe --force` (старый пакет удаляется вместе с пользователем), `sudo bash scripts/install.sh --phase 09` (новый `zoo-probe`), `sudo zoo probe --local --summary --export /etc/vpn-setup/probe-export.json` (новый пакет).

**2. На своей машине** — забрать пакет и собрать образ (один раз, из клона репозитория):

```bash
git clone https://github.com/Art-Frich/vpn-zoo-installer.git && cd vpn-zoo-installer
mkdir probe
scp root@СЕРВЕР:/etc/vpn-setup/probe-export.json probe/probe-export.json   # PowerShell: probe\probe-export.json
docker build -f docker/probe.Dockerfile -t zoo-probe .
```

Нестандартный порт SSH — `scp -P ПОРТ`. Если вход root по SSH запрещён: пакет root-only (0600 в каталоге 0700), сначала скопируйте его себе на сервере — `ssh -t user@СЕРВЕР 'sudo install -m 600 -o "$USER" /etc/vpn-setup/probe-export.json ~/probe-export.json'`, затем `scp user@СЕРВЕР:probe-export.json probe/` и `ssh user@СЕРВЕР rm probe-export.json`. Образ собирается несколько минут (`amneziawg-go` и `awg` — из исходников), на x86-64 и arm64; arm64 (Apple Silicon) не проверялся.

**3. Запуск:**

| Где | Команда |
|---|---|
| Linux, macOS | `docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe` |
| Windows, PowerShell | `docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "${PWD}\probe:/data" zoo-probe` |
| Windows, Git Bash | `MSYS_NO_PATHCONV=1 docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$(pwd -W)/probe:/data" zoo-probe` |

Только часть протоколов: добавьте после имени образа `--proto vless-reality --proto hysteria2` (аргументы с `-` уходят в `zoo probe --remote`). Клон и сборка образа — один раз; повторная проверка (например, из другой сети) — только `scp` и `docker run`; после `git pull` пересоберите образ.

`--cap-add NET_ADMIN --device /dev/net/tun` нужны только для AmneziaWG: клиент поднимает интерфейс внутри контейнера. Без них AmneziaWG получит `CLIENT_ERROR`, остальные протоколы проверятся.

**4. Результат** — таблица в терминале и файлы `probe/probe-report.md` и `probe/probe-report.json`. Если в пакете была самопроверка сервера, в конце будет сравнение. Удалите `probe/probe-export.json`. Отчёт `probe-report.json` можно вставить на странице «Проверка» админки или скопировать на сервер (`scp probe/probe-report.json root@СЕРВЕР:`) и сравнить там: `sudo zoo probe --compare /var/lib/vpn-zoo/probe-local.json probe-report.json`.

Мобильный интернет и домашний провайдер блокируют по-разному. Проверяйте из той сети, которая важна: например, раздайте интернет с телефона на ноутбук.

**Без Docker** (любой Linux с клиентами в `PATH`: `xray`, `hysteria`, `sing-box`, `amneziawg-go`, `awg`; пути можно задать через `ZOO_XRAY_BIN`, `ZOO_HYSTERIA_BIN`, `ZOO_SINGBOX_BIN`, `ZOO_AWG_GO_BIN`, `ZOO_AWG_BIN`):

```bash
sudo python3 zoo/zoo probe --remote probe-export.json --md report.md --out report.json
sudo python3 zoo/zoo probe --compare server-report.json report.json   # server-report.json — /var/lib/vpn-zoo/probe-local.json с сервера
```

Root нужен только для AmneziaWG. Клиент, которого нет на машине, даёт `SKIPPED`.

## Что меряется

Для каждого протокола из пакета (probe-объекты манифестов, ARCHITECTURE.md §4):

1. **L3/L4.** TCP — `connect` к порту сервера (3 попытки, медиана — RTT). UDP — лучшее, что можно без протокола: ICMP «порт закрыт» означает `SERVER_DOWN`, тишина ничего не значит. На сервере вместо этого смотрим, слушает ли порт (`ss`).
2. **Клиент и рукопожатие.** Xray (VLESS/XHTTP/SS), Hysteria и sing-box (TUIC) — отдельный процесс с SOCKS5 на `127.0.0.1:<случайный порт>` **с паролем**, конфиг во временном каталоге 0700. Hysteria подключается сразу, рукопожатие видно по логу. Xray и sing-box подключаются по первому запросу. AmneziaWG: модуль ядра или `amneziawg-go` плюс `awg setconf`; рукопожатие — `awg show latest-handshakes`. На сервере клиент AWG живёт в отдельном netns с veth, в контейнере — на своём интерфейсе: в туннель идут только сокеты, привязанные к нему (`SO_BINDTODEVICE` и своя таблица маршрутов), DNS тоже через туннель.
3. **Малый запрос** — `https://www.gstatic.com/generate_204` (запасной — `cp.cloudflare.com`). Второй такой же запрос даёт задержку через туннель.
4. **Большой запрос** — 5 МБ (`--speed-mb`) с `speed.cloudflare.com/__down` (запасной — `proof.ovh.net`), со счётчиком байтов и временем. Если 8 с нет новых данных, это застой. Застой раньше 64 КБ, в том числе до заголовков ответа, — это `FREEZE_16K`: «заморозка» ТСПУ после 16–20 КБ, бюджет съедают рукопожатия REALITY и TLS внутри.
5. **IP выхода** — `https://cloudflare.com/cdn-cgi/trace`, поле `ip=` (запасной адрес — `https://1.1.1.1/cdn-cgi/trace`). Echo-сервисы вроде ipify не годятся: фаза 07 блокирует `geosite:category-ip-geo-detect` или уводит его в WARP. `cloudflare.com` в эту категорию не входит (проверено по geosite runetfreedom 202610031847: там только `*-check-perf.radar.cloudflare.com`). IP выхода — справка: совпадение с IP сервера, «другой адрес (WARP или NAT хостера)» или совпадение с вашим прямым IP. На стенде последнее нормально: сервер и пробник за одним NAT.
6. **Скорость** — байты большого запроса за время тела ответа, Мбит/с.

7. **Расширенные метрики** (после всего вышеописанного, вердикт не меняют; только у протоколов, прошедших малый запрос) — в `metrics` результата: задержка (TTFB малого запроса, `--latency-samples` замеров на каждую из двух целей: медиана, p90, джиттер, потери, разбивка по целям), загрузка и, если задано `--upload-mb`, отдача (POST на `speed.cloudflare.com/__up`). Все запросы — через локальный прокси протокола, системный туннель не затрагивается. В таблице «задержка» — время всего малого запроса (соединение, TLS, ответ; по ней вердикт `SLOW`), «ответ p50/p90» — только от запроса до первого байта (медиана и 90-й перцентиль), «джиттер» — средняя разница соседних замеров.
8. **Контекст** — в `context` отчёта: метки `--tag`/`--device`, страна, ASN и провайдер прямого подключения (`speed.cloudflare.com/meta`; `--no-lookup` — не спрашивать), тип сети, если определился (в контейнере Docker — нет: различайте метками).

Параметры: `--timeout` (рукопожатие и малый запрос, 10 с), `--stall` (8 с), `--speed-mb` (5; на мобильном интернете `--speed-mb 1`, трафик пробы ≈ объём × число протоколов) или `--large-bytes`, `--upload-mb` (0 — отдачу не мерить), `--upload-url`, `--latency-samples` (5), `--slow-mbps` (2), `--tag`, `--device`, `--no-lookup`, `--small-url`, `--large-url`, `--ip-url`, `--out`, `--md`, `--json`, `--history-dir`, `--history-recipients`. Рейтинг по накопленному: `zoo probe --rank` (README, «История проб и рейтинг»).

### История проб из контейнера

Подключите каталог `history/` репозитория — и каждая проба дописывает в него анонимные строки (без IP) и сырой отчёт под age, если в `history/recipients.txt` есть ключи (age есть в образе); метки задаются аргументами:

```bash
docker run --rm --cap-add NET_ADMIN --device /dev/net/tun \
    -v "$PWD/probe:/data" -v "$PWD/history:/history" zoo-probe --tag mobile-mts --device pixel7
```

Каталог внутри контейнера — `ZOO_HISTORY_DIR` (по умолчанию `/history`; нет каталога — история не пишется). Модель приватности, формат и расшифровка — [../../history/README.md](../../history/README.md).

## Вердикты

| Вердикт | Когда | Что значит |
|---|---|---|
| `OK` | всё прошло, скорость ≥ 2 Мбит/с, задержка ≤ 1,5 с | работает |
| `SLOW` | медленно, или большой запрос оборвался после 64 КБ | работает плохо |
| `FREEZE_16K` | TCP: малый запрос прошёл, большой встал до 64 КБ | «заморозка» ТСПУ на TCP+TLS |
| `HANDSHAKE_FAIL` | порт отвечает, туннель не поднимается (RST на ClientHello, неверный ключ, Hysteria ответила отказом) | блокировка по сигнатуре, или креды устарели |
| `IP_BLOCKED` | TCP `connect` не проходит; у UDP нет ответа, и TCP-порты тоже молчат | IP сервера недоступен у этого провайдера |
| `UDP_BLOCKED` | у UDP нет ответа, а TCP-порты того же сервера отвечают | режут UDP/QUIC |
| `SERVER_DOWN` | порт закрыт (RST, ICMP), или самопроверка сервера тоже не прошла | дело в сервере, а не в блокировке |
| `CLIENT_ERROR` | клиент пробника не запустился (конфиг, права, нет `/dev/net/tun`) | проблема пробника |
| `SKIPPED` | нет клиента на этой машине, или в манифесте нет probe | не проверялось |

На сервере (`--local`) блокировок не бывает: недоступность считается `SERVER_DOWN`. Если по публичному IP не прошло, проба повторяется через `127.0.0.1` (AWG — через адрес хоста на veth), и в отчёте будет `куда: loopback` с пометкой про hairpin. Код выхода 0 — все протоколы `OK`, `SLOW` или `SKIPPED`.

Проверка идёт кредами служебного пользователя `zoo-probe` (его заводит фаза 09, скрыт из списков и отчётов трафика по пользователям). Поэтому проба AWG не «перетягивает» сессию телефона владельца (роуминг WireGuard по ключу). Протокол, где `zoo-probe` нет, проверяется probe owner из манифеста — с пометкой в отчёте. `--user NAME` — креды другого пользователя.

TUIC на сервере проверяется закреплённым sing-box из `/usr/local/lib/vpn-zoo/bin` (ставит фаза 04d вместе с TUIC). Port hopping Hysteria отдельно не проверяется: hop-порты — DNAT на внешнем интерфейсе, с самого сервера они недостижимы.

## Стенд (docker/test.sh)

| Файл | Назначение |
|---|---|
| `../probe.Dockerfile` | образ `zoo-probe`: клиенты по `scripts/versions.env` с проверкой sha256 (Xray — из архива 3x-ui, как на сервере; hysteria; sing-box; amneziawg-go и awg собираются из исходников) и пакет `zoo` |
| `install-clients.sh` | сборочная стадия образа: скачивание, sha256, сборка |
| `entrypoint.sh` | `ENTRYPOINT` образа (`zoo-probe`): `zoo probe --remote /data/probe-export.json`, отчёт в `/data`, история — в `/history`, если каталог подключён; аргументы с `-` — флаги пробника, иначе команда вместо него (`docker run zoo-probe sleep infinity`). `ZOO_PROBE_VIA=<IP>` — маршрут к серверу через цензор |
| `run.sh` | `run.sh <сервер> <каталог-отчёта>`: самопроверка и экспорт на сервере, пробник напрямую и через цензор по профилям, сверка `expect.py`. Итог: `probe/expect.tsv`, `probe/<профиль>/probe-report.{json,md}`, `probe-client-<профиль>.json` |
| `expect.py` | ожидаемые вердикты для профиля цензора (таблица в [../censor/README.md](../censor/README.md)) |
| `mini-server.sh` | минимальный сервер без фаз 04–06 (VLESS-REALITY и SS-2022 через `lib/xui.sh`, hysteria, amneziawg-go) с манифестами — для отладки пробника |

```bash
docker/test.sh --distro 24.04                               # установка + пробник по всем профилям
docker/test.sh --probe-profiles direct,drop-udp             # только эти профили
docker/test.sh --probe-profiles none                        # без клиентского пробника

# отладка пробника на своём сервере стенда
docker/run-server.sh up p1 --distro 22.04
docker/run-server.sh install p1 -- --only 00,01,02,03
docker/run-server.sh exec p1 bash /repo/docker/probe/mini-server.sh   # нужны amneziawg-go и awg в /usr/local/bin
docker/probe/run.sh p1 /tmp/probe-out
```

Профиль `direct` — пробник прямо в `zoo-net`, без цензора; ожидания у него как у `clean`.
