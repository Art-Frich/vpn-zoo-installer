# docker/probe — клиентский пробник

Пробник отвечает на два вопроса (ARCHITECTURE.md §7):

- **работает ли протокол в принципе** — `zoo probe --local` на самом сервере;
- **блокируется ли он у конкретного пользователя** — контейнер `zoo-probe` на его машине: трафик идёт через его провайдера.

Сравнение двух прогонов даёт вердикт по каждому протоколу: «работает у вас», «работает в принципе, блокируется у вас (тип)» или «не работает на самом сервере».

## Как проверить свой интернет (Windows, macOS, Linux)

Нужен Docker (Docker Desktop на Windows и macOS). VPN на компьютере на время проверки выключите, иначе проверяться будет чужой VPN, а не ваш провайдер.

**1. На сервере** — самопроверка и пакет для пробника:

```bash
sudo zoo probe --local                              # результат попадёт в пакет для сравнения
sudo zoo export-probe --out /root/probe-export.json # --user NAME — креды другого пользователя
```

В пакете **ключи доступа к серверу**. Копируйте его только по `scp`, после проверки удалите и на сервере, и у себя.

**2. На своей машине** — забрать пакет и собрать образ (один раз, из клона репозитория):

```bash
mkdir probe
scp root@СЕРВЕР:/root/probe-export.json probe/
docker build -f docker/probe.Dockerfile -t zoo-probe .
```

**3. Запуск:**

| Где | Команда |
|---|---|
| Linux, macOS | `docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe` |
| Windows, PowerShell | `docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "${PWD}\probe:/data" zoo-probe` |
| Windows, Git Bash | `MSYS_NO_PATHCONV=1 docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$(pwd -W)/probe:/data" zoo-probe` |

Только часть протоколов: добавьте в конец `zoo-probe --proto vless-reality --proto hysteria2`.

`--cap-add NET_ADMIN --device /dev/net/tun` нужны только для AmneziaWG: клиент поднимает интерфейс внутри контейнера. Без них AmneziaWG получит `CLIENT_ERROR`, остальные протоколы проверятся.

**4. Результат** — таблица в терминале и файлы `probe/probe-report.md` и `probe/probe-report.json`. Если в пакете была самопроверка сервера, в конце будет сравнение. Удалите `probe/probe-export.json`.

Мобильный интернет и домашний провайдер блокируют по-разному. Проверяйте из той сети, которая важна: например, раздайте интернет с телефона на ноутбук.

**Без Docker** (любой Linux с клиентами в `PATH`: `xray`, `hysteria`, `sing-box`, `amneziawg-go`, `awg`; пути можно задать через `ZOO_XRAY_BIN`, `ZOO_HYSTERIA_BIN`, `ZOO_SINGBOX_BIN`, `ZOO_AWG_GO_BIN`, `ZOO_AWG_BIN`):

```bash
sudo python3 zoo/zoo probe --remote probe-export.json --md report.md
sudo python3 zoo/zoo probe --compare server-report.json client-report.json
```

Root нужен только для AmneziaWG. Клиент, которого нет на машине, даёт `SKIPPED`.

## Что меряется

Для каждого протокола из пакета (probe-объекты манифестов, ARCHITECTURE.md §4):

1. **L3/L4.** TCP — `connect` к порту сервера (3 попытки, медиана — RTT). UDP — лучшее, что можно без протокола: ICMP «порт закрыт» означает `SERVER_DOWN`, тишина ничего не значит. На сервере вместо этого смотрим, слушает ли порт (`ss`).
2. **Клиент и рукопожатие.** Xray (VLESS/XHTTP/SS), Hysteria и sing-box (TUIC) — отдельный процесс с SOCKS5 на `127.0.0.1:<случайный порт>` **с паролем**, конфиг во временном каталоге 0700. Hysteria подключается сразу, рукопожатие видно по логу. Xray и sing-box подключаются по первому запросу. AmneziaWG: модуль ядра или `amneziawg-go` плюс `awg setconf`; рукопожатие — `awg show latest-handshakes`. На сервере клиент AWG живёт в отдельном netns с veth, в контейнере — на своём интерфейсе: в туннель идут только сокеты, привязанные к нему (`SO_BINDTODEVICE` и своя таблица маршрутов), DNS тоже через туннель.
3. **Малый запрос** — `https://www.gstatic.com/generate_204` (запасной — `cp.cloudflare.com`). Второй такой же запрос даёт задержку через туннель.
4. **Большой запрос** — 2 МБ с `speed.cloudflare.com/__down` (запасной — `proof.ovh.net`), со счётчиком байтов и временем. Если 8 с нет новых данных, это застой. Застой раньше 64 КБ, в том числе до заголовков ответа, — это `FREEZE_16K`: «заморозка» ТСПУ после 16–20 КБ, бюджет съедают рукопожатия REALITY и TLS внутри.
5. **IP выхода** — `https://cloudflare.com/cdn-cgi/trace`, поле `ip=` (запасной адрес — `https://1.1.1.1/cdn-cgi/trace`). Echo-сервисы вроде ipify не годятся: фаза 07 блокирует `geosite:category-ip-geo-detect` или уводит его в WARP. `cloudflare.com` в эту категорию не входит (проверено по geosite runetfreedom 202610031847: там только `*-check-perf.radar.cloudflare.com`). IP выхода — справка: совпадение с IP сервера, «другой адрес (WARP или NAT хостера)» или совпадение с вашим прямым IP. На стенде последнее нормально: сервер и пробник за одним NAT.
6. **Скорость** — байты большого запроса за время тела ответа, Мбит/с.

Параметры: `--timeout` (рукопожатие и малый запрос, 10 с), `--stall` (8 с), `--large-bytes` (2 000 000), `--slow-mbps` (2), `--small-url`, `--large-url`, `--ip-url`, `--out`, `--md`, `--json`.

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

AWG проверяется ключами `owner`. Если телефон владельца в этот момент подключён, его сессия на несколько секунд «переедет» на пробник (роуминг WireGuard), потом вернётся сама.

## Стенд (docker/test.sh)

| Файл | Назначение |
|---|---|
| `../probe.Dockerfile` | образ `zoo-probe`: клиенты по `scripts/versions.env` с проверкой sha256 (Xray — из архива 3x-ui, как на сервере; hysteria; sing-box; amneziawg-go и awg собираются из исходников) и пакет `zoo` |
| `install-clients.sh` | сборочная стадия образа: скачивание, sha256, сборка |
| `entrypoint.sh` | `CMD` образа (`zoo-probe`): `zoo probe --remote /data/probe-export.json`, отчёт в `/data`. `ZOO_PROBE_VIA=<IP>` — маршрут к серверу через цензор |
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
