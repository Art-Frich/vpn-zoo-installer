# Архитектура vpn-zoo-installer (v2)

Контракт для разработки. Каждый модуль владеет своими файлами и общается с остальными через манифесты и функции `lib.sh`. Основание для решений — исследования в `research/2026-10-04/`, конкретные выборы перечислены в [DECISIONS.md](DECISIONS.md).

## 1. Слои

```
scripts/install.sh            оркестратор фаз (state machine, флаги)
scripts/lib.sh                общие функции: лог, state, config, версии, манифесты, firewall, backup, xui_api
scripts/versions.env          закреплённые версии и sha256 всех внешних компонентов
scripts/lib/*.sh              помощники модулей (lib/xui.sh — единственный адаптер к API 3x-ui)
scripts/NN-*.sh               фазы: каждая ставит и настраивает один компонент, пишет свой манифест
zoo/                          инструмент `zoo` (Python 3, только stdlib): CLI + веб-админка + пробник
docker/                       тестовый стенд: systemd-контейнер сервера, контейнер-пробник, цензор
docs/                         архитектура, решения, снижение рисков
research/YYYY-MM-DD/          исследования
```

## 2. Фазы

| Фаза | Файл | Что делает |
|---|---|---|
| 00 | `00-bootstrap.sh` | apt, базовые пакеты, swap, sysctl (без udp_mem, без ipv6 forwarding глобально), conntrack modules-load, unattended-upgrades |
| 01 | `01-firewall.sh` | UFW: SSH (определяется, не хардкод), default deny, без `ufw reset` на живой системе; fail2ban. Порты протоколов открывает каждая фаза через `fw_allow` |
| 02 | `02-kernel.sh` | Проверка совместимости ядра с DKMS amneziawg (не «≥6.2»), блок известных сломанных ядер |
| 03 | `03-3xui.sh` | 3x-ui закреплённой версии + sha256, панель на `127.0.0.1`, API-токен, ожидание API |
| 04 | `04-vless-reality.sh` | VLESS RAW+REALITY+Vision через API 3x-ui, 443/tcp, валидированный target |
| 04b | `04b-vless-xhttp.sh` | VLESS XHTTP+REALITY (запасной TCP) |
| 04c | `04c-ss2022.sh` | Shadowsocks-2022 (запасной не-TLS) через 3x-ui |
| 04d | `04d-tuic.sh` | TUIC v5 через 3x-ui (флаг); закреплённый sing-box в `/usr/local/lib/vpn-zoo/bin` — клиент самопроверки TUIC (не сервис, не в PATH) |
| 05 | `05-hysteria2.sh` | Hysteria2 закреплённой версии, `auth.type: command` (D17), pinSHA256, опционально Salamander-инстанс и port hopping |
| 06 | `06-amneziawg.sh` | AmneziaWG: движок kernel (DKMS) или userspace (amneziawg-go), уникальные параметры на каждую установку |
| 07 | `07-routing.sh` | Анти-утечки: echo-сервисы → WARP или блок, RU-egress (opt-in), sniffing, geo-файлы и таймер обновления |
| 08 | `08-warp.sh` | Cloudflare WARP как outbound (opt-in) |
| 09 | `09-zoo.sh` | Установка `zoo` (CLI + админка + коллектор трафика + таймеры), служебный пользователь пробника `zoo-probe` (§5) |
| 99 | `99-print-creds.sh` | Ссылки, QR, CREDENTIALS.md, пакет для клиентского пробника (`zoo export-probe`). Читает манифесты |

Фаза, которой нет на диске или которая выключена флагом, пропускается. Порядок задаётся массивом `PHASES` в `install.sh`.

После 99 (полный прогон или `--only` с 99, не `--phase`) `install.sh` запускает самопроверку `zoo probe --local --summary` (§7): таблица «работает в принципе» по каждому протоколу, провалы подсвечены, но установку не валят; затем — точная инструкция для проверки с машины пользователя. Итог сохраняется в `/var/lib/vpn-zoo/probe-local.json` (админка, страница «Проверка») и вкладывается в пакет `/etc/vpn-setup/probe-export.json`. `ZOO_SELFTEST=0` — без самопроверки.

Журналы установки (`/var/log/vpn-zoo/install-*.log`) и бэкапы (`/var/backups/vpn-setup/<ts>/`) содержат ключи. `install.sh` при каждом запуске оставляет 10 последних (`ZOO_KEEP_LOGS`, `ZOO_KEEP_BACKUPS`); из остальных удаляются журналы старше 7 дней и бэкапы старше 30 дней.

Повторный полный запуск `install.sh` (D24):
- фаза со state `done` и флагом `ENABLE_*=0` запускается ещё раз и выключает свой протокол (inbound/сервис, порт, манифест `enabled=false`), state становится `disabled`; так умеют 04, 04b, 04c, 04d, 05, 06, 08. У 09 пути выключения нет: `ENABLE_ZOO=0` только не даёт поставить zoo, уже установленный остаётся;
- ключ, изменённый через окружение, перезапускает фазу-владельца (`phase_owns_key` в `install.sh`): `VLESS_*` → 04, `XHTTP_*` → 04b, `SS_*` → 04c, `TUIC_*` → 04d, `HY2_*`/`ENABLE_HY2_OBFS` → 05, `AWG_*` → 06, `RU_EGRESS`/`HY2_RU_EGRESS`/`AWG_RU_EGRESS`/`ENABLE_BITTORRENT`/`ROUTING_ECHO_EXTRA`/`ENABLE_WARP` → 07 (и 08), `SERVER_IP`/`LABEL` → 04–06, `PANEL_*`/`SUB_PUBLIC`/`DOMAIN` → 03. `AUTO_REBOOT*` и `SSH_PORTS` фазу-владельца не перезапускают: после смены — `--phase 00`, для `SSH_PORTS` — `--phase 01` и `--phase 07` (SSH-порт сервера разрешён из туннеля, D25);
- после любой отработавшей фазы протокола заново применяется 07 (новые inbound, ACL Hysteria), в конце печатается 99;
- упавшая фаза получает state `failed` и проходит заново при следующем запуске: значение из окружения уже записано в config.env, и пропуск «done» оставил бы его неприменённым. `failed`/`disabled` сбрасываются перед запуском фазы, успешный проход ставит `done`;
- 09 проходит снова, если копия `zoo/` и `scripts/` в `/opt/vpn-zoo` отличается от репозитория (после `git pull`): zoo вызывает `lib/proto-*.sh` из этой копии;
- 99 перед выводом делает `zoo user sync`: протокол, включённый после установки, получает уже заведённых пользователей (§5).

## 3. Конфиг

- `/etc/vpn-setup/config.env` (0600). Все параметры и сгенерированные секреты. Каждый модуль добавляет свои переменные через `config_set KEY VALUE`, а не через переписывание файла целиком.
- Флаги включения: `ENABLE_VLESS=1 ENABLE_XHTTP=1 ENABLE_SS=1 ENABLE_TUIC=0 ENABLE_HY2=1 ENABLE_HY2_OBFS=0 ENABLE_AWG=1 ENABLE_WARP=0 RU_EGRESS=direct|block|warp`.
- Движки: `AWG_ENGINE=auto|kernel|userspace` (`auto` = kernel, если DKMS собирается, иначе userspace), `HY2_ENGINE=apernet`.
- Порты по умолчанию: VLESS 443/tcp, Hy2 443/udp, остальные — случайные высокие порты при первой установке, сохраняются в config.env. Никогда не используем 1080, 3128, 8080, 9050, 2053, 54321.
- Ключи модулей (через окружение install.sh, сохраняются в config.env; список — `CONFIG_ENV_KEYS_RE` в lib.sh):

| Модуль | Ключи |
|---|---|
| общие | `SERVER_IP` (определяется сам), `LABEL` (имя в ссылках, `vpn`) |
| 00 bootstrap | `AUTO_REBOOT=0\|1`, `AUTO_REBOOT_TIME=ЧЧ:ММ` (D12) |
| 01 firewall | `SSH_PORTS` (через запятую; дополняется найденными: текущее подключение, `sshd -T`, `ssh.socket`, `ss`) |
| 02 kernel | `AWG_NO_HWE=1` — не ставить HWE-ядро, AWG в userspace; пишет `AWG_ENGINE_HINT`, `AWG_ENGINE_REASON` |
| 03 3x-ui | `PANEL_PORT`, `PANEL_PATH`, `PANEL_USER` (генерируются), `PANEL_2FA=1` (D15); пишет `PANEL_PASS`, `XUI_API_TOKEN`. `SUB_PUBLIC=1` — **заготовка**: включает подписку и Happ-заголовки в настройках панели, но фазы с доменом (`DOMAIN`), TLS и открытием порта подписки нет (D8) |
| 04 VLESS | `VLESS_PORT`, `VLESS_SNI` (+`VLESS_SNI_CHECK=0`), `VLESS_TARGET=host:port`; пишет `VLESS_PRIV/PUB/SID/UUID/SNI_PICKED/PORT_MIGRATED` |
| 04b XHTTP | `XHTTP_PLACEMENT=port\|fallback` (D16), `XHTTP_PORT`, `XHTTP_SNI`, `XHTTP_PATH`, `XHTTP_MODE=auto\|packet-up\|…`; пишет `XHTTP_PRIV/PUB/SID` |
| 04c SS-2022 | `SS_PORT`; пишет `SS_PSK` |
| 04d TUIC | `TUIC_PORT`, `TUIC_SNI` |
| 05 Hysteria2 | `HY2_PORT`, `HY2_SNI`, `HY2_MASQ_URL`, `HY2_HOP=1`, `HY2_HOP_RANGE`, `HY2_HOP_IFACE`, `HY2_OBFS_PORT`, `HY2_LOG_LEVEL`; пишет `HY2_PASSWORD` (токен owner), `HY2_PIN`, `HY2_STATS_*`, `HY2_OBFS_PASSWORD` |
| 06 AmneziaWG | `AWG_PORT`, `AWG_NETWORK`, `AWG_PROFILE=v2\|v3`, `AWG_RT=1`, `AWG_MTU`, `AWG_DNS`, `AWG_KEEPALIVE`, параметры обфускации `AWG_JC…AWG_I1` (генерируются) |
| 07 маршрутизация | `RU_EGRESS`, `HY2_RU_EGRESS`, `AWG_RU_EGRESS` (D21), `ENABLE_BITTORRENT` (D20), `ROUTING_ECHO_EXTRA` (домены через запятую) |
| 08 WARP | пишет `WARP_*`; разовый `WARP_REREGISTER=1` не сохраняется |

Разовые переключатели `ZOO_*` (`ZOO_FORCE`, `ZOO_VLESS_REPICK`, `ZOO_AWG_TOOLS_SRC`, `ZOO_TEST_ENV`, `ZOO_SELFTEST`, `ZOO_KEEP_LOGS`, `ZOO_KEEP_BACKUPS`, …) в config.env не попадают.

## 4. Манифест протокола

Каждая фаза протокола после успешной настройки пишет `/etc/vpn-setup/protocols.d/<id>.json` через `manifest_write`:

```json
{
  "id": "vless-reality",
  "name": "VLESS + REALITY + Vision",
  "layer": "tcp",
  "port": 443,
  "engine": "xray",
  "service": "x-ui",
  "enabled": true,
  "users_backend": "xui",
  "links": [{"user": "owner", "uri": "vless://..."}],
  "files": [{"user": "owner", "path": "/etc/vpn-setup/clients/owner/amneziawg.conf"}],
  "probe": {"kind": "xray", "outbound": { /* готовый Xray outbound JSON для owner */ }},
  "notes": "что важно знать клиенту"
}
```

`probe.kind` ∈ `xray` | `hysteria` | `awg` | `sing-box`. Пробник строит клиентский конфиг из `probe`, без знания внутренностей модуля. Манифесты читают 99, `zoo` и firewall-аудит.

| kind | Содержимое `probe` |
|---|---|
| `xray` | `outbound` — готовый outbound Xray с тегом `proxy` (VLESS, XHTTP, SS-2022); у VLESS/XHTTP ещё `link`/`uri` |
| `hysteria` | `client` — готовый конфиг клиента Hysteria (JSON = YAML: `server`, `auth`, `tls{sni,insecure,pinSHA256}`, `obfs`), `hop` при port hopping, `version` |
| `awg` | `conf` — полный клиентский `.conf`, `endpoint`, `address`, `server_tunnel_ip`, `mtu`, `profile` |
| `sing-box` | `outbound` — outbound sing-box (TUIC: сертификат закреплён PEM в `tls.certificate`), `certificate_public_key_sha256` |

Во всех `probe.user` — чей это конфиг (owner, если он включён). Необязательные поля, которые пишут модули: `links[].enabled`, `files[].enabled`; VLESS — `xui_inbound_id`, `xui_tag`, `sni`, `target`; XHTTP — `params{placement, inbound_id, inbound_port, listen, path, mode}`; SS/TUIC — `transports`, `xui{inbound_id, tag, socks_relay}`, `tls{self_signed, cert_path, cert_sha256, pubkey_sha256}`; Hysteria2 — `version`, `tls{sni, pinSHA256, self_signed}`, `hop_ports`; AWG — `interface`, `network`, `profile`.

Отдельные манифесты: `hysteria2-obfs` (инстанс Salamander, есть только при `ENABLE_HY2_OBFS=1`). Фаза 99 собирает `probe` всех манифестов в `/etc/vpn-setup/probe-export.json` (`{version, server_ip, label, protocols:[{id, name, layer, port, engine, enabled, probe}]}`) — вход клиентского пробника.

## 5. Пользователи

Один пользователь зоопарка = креды во всех включённых протоколах:
- Xray-семейство (VLESS, XHTTP, SS, TUIC): один клиент 3x-ui на все inbound (D18): `email=<name>`, общие uuid, `subId`, `password` (ключ SS-2022). Создание и привязка — только `xui_user_attach`/`xui_user_detach` из `lib/xui.sh`; `enable` у клиента общий для всех Xray-протоколов, счётчик трафика тоже.
- Hysteria2: `auth.type: command` (D17): `/etc/hysteria/users.tsv` (`имя<TAB>токен<TAB>1|0`), помощник `/usr/local/lib/vpn-zoo/hy2-auth`, изменения вживую, отключение рвёт сессии через trafficStats `/kick`.
- AmneziaWG: отдельный peer (ключи, PSK, /32 из пула), живое применение через `awg set`/`awg syncconf`, `.conf` в `/etc/vpn-setup/clients/<name>/`.

Хранилище: `/etc/vpn-setup/users.json` (источник правды для зоопарка), операции — `zoo user add|del|disable|enable|list|show|sync`. Пользователь `owner` создаётся при установке.

Служебный пользователь `zoo-probe` (`"system": true` в users.json) заводится фазой 09 (`zoo setup`) во всех протоколах, новым протоколам его добавляет `zoo user sync` (фаза 99). Его кредами работают `zoo probe --local` и `zoo export-probe` (`--user` — другой пользователь): проба не выбивает сессию AmneziaWG у телефона владельца (роуминг WireGuard по ключу) и не попадает в трафик owner. Он скрыт из `zoo user list` и админки (`--all` — показать), из отчётов трафика по пользователям (`zoo traffic --all`), но учтён в итогах протоколов. Удаление и отключение — отказ (удалить можно `zoo user del zoo-probe --force`, тогда пробник переходит на owner, пакет `probe-export.json` с ключами `zoo-probe` удаляется, а следующий запуск фазы 09 заводит пользователя снова); имя `zoo-probe` для обычного пользователя зарезервировано. Протокол, где его нет, пробник проверяет probe owner из манифеста — с пометкой в отчёте. Имя проверяет zoo (`users.NAME_RE`: строчная латиница, цифры, `-` и `_`, до 32, первый символ — буква или цифра); модули протоколов (`zoo_user_valid` в lib.sh) мягче — допускают ещё заглавные и точку, файлы пользователя — `/etc/vpn-setup/clients/<имя>/` (0700, файлы 0600, `zoo_client_file_write`).

## 6. Инструмент zoo (Python 3 stdlib)

- `zoo status` — сервисы, порты, сертификаты, версии, лишние listen на 0.0.0.0, метрики хоста. В проблемах — и интерфейс протокола, которого нет при активном юните (`awg-quick@awg0` — oneshot: при падении amneziawg-go юнит остаётся active), и коллектор трафика: таймер не активен, последнее снятие старше 20 минут, ошибки источников.
- `zoo user add|del|disable|enable|list|show|sync` — управление пользователями во всех протоколах.
- `zoo links [user] [--qr] [--svg-dir]` — ссылки и QR (по умолчанию owner).
- `zoo traffic [user] [--period]` — трафик по пользователю и протоколу (3x-ui API, Hy2 trafficStats API, `awg show dump`), история в SQLite.
- `zoo probe --local` — самопроверка с сервера: «работает ли протокол в принципе» (`--summary` — короткая сводка и следующий шаг, как в конце установки; `--export FILE` — сразу пакет для клиента).
- `zoo probe --remote FILE` — клиентский прогон по пакету (контейнер `zoo-probe` или любой Linux с клиентами); `zoo probe --compare LOCAL REMOTE` — сравнение отчётов.
- `zoo export-probe` — пакет для клиентского пробника (креды `zoo-probe` + итог последней самопроверки).
- `zoo web` — веб-админка на `127.0.0.1:$ZOO_WEB_PORT` с токеном; доступ через `ssh -L`.
- `zoo upgrade [--apply] [--pull]`/`zoo smoke` — обновление закреплённых версий с проверкой до и после; `zoo version --all` — версии компонентов и пины.
- Коллектор трафика — `zoo-collector.timer` (каждые 5 минут, `zoo traffic --collect`) под песочницей systemd; админка — `zoo-web.service`. Юниты — `zoo/systemd/`, ставит фаза 09.

## 7. Пробник

1. **Сервер, «в принципе»**: `zoo probe --local` поднимает клиент каждого протокола на самом сервере (Xray из 3x-ui; hysteria; sing-box для TUIC из `/usr/local/lib/vpn-zoo/bin`, его ставит 04d; AmneziaWG — `amneziawg-go` или модуль в отдельном netns) кредами `zoo-probe` и идёт на публичный IP сервера; если хостер режет hairpin — повтор через 127.0.0.1 (AWG — через адрес хоста на veth), в отчёте `куда: loopback`. Замеры: малый запрос (`generate_204`), большой запрос 2 МБ (`speed.cloudflare.com/__down`), IP выхода — `https://cloudflare.com/cdn-cgi/trace` (поле `ip=`; echo-сервисы вроде ipify фаза 07 блокирует или уводит в WARP). Запускается сам в конце `install.sh` (§2). Итог — `/var/lib/vpn-zoo/probe-local.json`.
2. **Клиент, «блокируется ли»**: контейнер `zoo-probe` (Docker на машине пользователя, трафик идёт через его провайдера) или `zoo probe --remote` на любой Linux-машине, по пакету `zoo export-probe`. Для каждого протокола: TCP-connect или UDP-доступность порта, рукопожатие, малый и большой запрос (застой — 8 с без данных; застой раньше 64 КБ — «заморозка»), задержка, скорость, IP выхода. Сравнение с вложенной в пакет самопроверкой даёт вывод «работает у вас», «работает в принципе, блокируется у вас (тип)» или «не работает на самом сервере».

| Вердикт | Когда |
|---|---|
| `OK` | всё прошло, скорость ≥ 2 Мбит/с, задержка ≤ 1,5 с |
| `SLOW` | медленно, или большой запрос оборвался после 64 КБ |
| `FREEZE_16K` | TCP: малый запрос прошёл, большой встал до 64 КБ («заморозка» ТСПУ после 16–20 КБ) |
| `HANDSHAKE_FAIL` | порт отвечает, туннель не поднимается (RST на ClientHello, неверный ключ, отказ Hysteria) |
| `IP_BLOCKED` | TCP-connect не проходит; у UDP нет ответа, и TCP-порты тоже молчат. Причина различает блок порта (другие TCP-порты сервера отвечают) и блок всего IP |
| `UDP_BLOCKED` | у UDP нет ответа, а TCP-порты того же сервера отвечают |
| `SERVER_DOWN` | порт закрыт (RST, ICMP), или самопроверка сервера тоже не прошла |
| `CLIENT_ERROR` | клиент пробника не запустился (конфиг, права, нет `/dev/net/tun`) — дело не в сети |
| `SKIPPED` | нет клиента на этой машине, или в манифесте нет probe |

На сервере (`--local`) блокировок не бывает: недоступность считается `SERVER_DOWN`. Код выхода 0 — все протоколы `OK`, `SLOW` или `SKIPPED`. Port hopping Hysteria (`HY2_HOP=1`) отдельно не проверяется: DNAT диапазона висит на PREROUTING внешнего интерфейса, с самого сервера hop-порты недостижимы, поэтому проверяется основной порт. Подробно — [docker/probe/README.md](../docker/probe/README.md).

Второй сервер не обязателен (D11): пункт 1 выполняется с самого сервера, пункт 2 — только из сети пользователя. Вторая точка вне РФ (тот же `zoo probe --remote` или контейнер) — необязательный контроль: самопроверка через собственный публичный IP может не пройти через внешний файрвол хостера (security group, анти-DDoS), и снаружи закрытый порт на сервере выглядит рабочим. Постоянного мониторинга из РФ в проекте нет.

## 8. Тестовый стенд (docker/)

- `docker/server.Dockerfile` — Ubuntu 22.04/24.04 с systemd (privileged, cgroupns=host). Инсталлер работает в реальном режиме, кроме загрузки kernel-модуля amneziawg: в Docker AWG проверяется через userspace-движок.
- `docker/probe.Dockerfile` — клиенты xray, hysteria, sing-box, amneziawg-go/awg и `zoo` (точка входа `zoo-probe`); тот же образ пользователь собирает у себя для клиентской проверки.
- `docker/censor/` — маршрутизатор между пробником и сервером с правилами «как ТСПУ» (профили `clean`, `drop-udp`, `ip-block`, `freeze-16k`, `rst-tls`, по запросу `port-block`). Используется для проверки классификации пробника.
- `docker/test.sh` — полный e2e: установка (в `--mode full` проверяется и самопроверка в конце `install.sh`) → `zoo probe --local` → клиентский пробник напрямую и через цензора по профилям → тесты протоколов, анти-утечек, безопасности → админка формами (`zoo/tests/web_smoke.sh --users`) → коллектор трафика под песочницей systemd (`docker/tests/collector.sh`) → отчёт.
- На Windows/Git Bash все docker-команды запускать с `MSYS_NO_PATHCONV=1`.

## 9. Правила кода

- bash: `set -euo pipefail`, идемпотентность, никаких секретов в argv там, где есть альтернатива, `umask 077` для секретов, LF.
- Всё, что скачивается, закреплено в `versions.env` и проверяется по sha256.
- Перед перезаписью чужого конфига — бэкап в `/var/backups/vpn-setup/<ts>/`; при чужой установке — отказ без `--force`.
- Любой вызов API 3x-ui — только через `scripts/lib/xui.sh`.
- Каждая фаза проверяет себя (сервис активен, порт слушает) и падает с понятным сообщением.
