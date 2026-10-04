# vpn-zoo-installer

Bash-инсталлер «зоопарка» VPN-протоколов на одном VPS с Ubuntu 22.04/24.04 для пользователей из России. Одна команда ставит несколько независимых протоколов: если провайдер режет один, остаются другие.

| Протокол | Транспорт | По умолчанию | Движок |
|---|---|---|---|
| VLESS + REALITY + Vision | TCP 443 | вкл. | Xray в 3x-ui |
| VLESS + XHTTP + REALITY | TCP, случайный порт | вкл. | Xray в 3x-ui |
| Shadowsocks-2022 | TCP+UDP, случайный порт | вкл. | Xray в 3x-ui |
| Hysteria2 | UDP 443 | вкл. | HyNetworks/hysteria |
| AmneziaWG 2.0 | UDP, случайный порт | вкл. | ядро (DKMS) или amneziawg-go |
| TUIC v5 | UDP, случайный порт | `ENABLE_TUIC=1` | 3x-ui |
| Hysteria2 + Salamander | UDP, случайный порт | `ENABLE_HY2_OBFS=1` | hysteria |
| Cloudflare WARP (выход для echo/RU) | — | `ENABLE_WARP=1` | Xray |

Плюс анти-утечки (echo-сервисы «узнай свой IP» блокируются или идут через WARP, opt-in блок RU-направлений, sniffing, `access: none`), панель 3x-ui только на `127.0.0.1`, инструмент `zoo` для пользователей, ссылок и трафика.

> Проверено на тестовом стенде (systemd в Docker, Ubuntu 22.04 и 24.04) настоящими клиентами: Xray, sing-box, hysteria, amneziawg-go. На боевом VPS из РФ и с DKMS-модулем AmneziaWG — ещё нет. MIT, без гарантий.

## Установка

На чистой Ubuntu 22.04/24.04 от root:

```bash
git clone <этот репозиторий> /opt/vpn-zoo-src
cd /opt/vpn-zoo-src
sudo bash scripts/install.sh
```

Если фаза 02 поставит ядро и попросит перезагрузку: `sudo reboot`, затем ту же команду ещё раз — установка продолжится с места остановки.

В конце фаза 99 печатает ссылки и QR для пользователя `owner` по каждому протоколу, команду SSH-туннеля к панели и таблицу «какие клиенты с чем работают». То же сохраняется в `/root/CREDENTIALS.md` (0600).

## Параметры

Через окружение, сохраняются в `/etc/vpn-setup/config.env`:

```bash
ENABLE_TUIC=1 ENABLE_HY2_OBFS=1 RU_EGRESS=block sudo -E bash scripts/install.sh
```

Повторный запуск с изменённым параметром перезапускает только нужную фазу. `ENABLE_X=0` на уже установленном сервере выключает протокол (сервис или inbound остановлен, порт закрыт). Полный список ключей — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3, решения по умолчанию — [docs/DECISIONS.md](docs/DECISIONS.md).

```bash
sudo bash scripts/install.sh --phase 05   # одна фаза принудительно
sudo bash scripts/install.sh --rerun      # все фазы заново (идемпотентно)
sudo bash scripts/install.sh --dry-run    # план без изменений
sudo bash scripts/install.sh --force      # разрешить перезапись чужой установки (с бэкапом)
```

Найдя чужую установку (3x-ui, Hysteria, AmneziaWG, не поставленные этим инсталлером), фаза останавливается. С `--force` она делает бэкап в `/var/backups/vpn-setup/<время>/` и продолжает.

## Обновление с первой версии

Запустить новый `install.sh` поверх старой установки. State первой версии сбрасывается, фазы проходят заново и переносят то, что было:
- пароль Hysteria2 становится паролем `owner`, старая ссылка `hysteria2://…` продолжает работать (сервер обновится до закреплённой версии, лучше перейти на новую ссылку с `pinSHA256`);
- VLESS переезжает с 8443 на 443 и получает новый REALITY target вместо `www.cbr.ru` — ссылку VLESS нужно заменить;
- ключи клиента AmneziaWG и порт сохраняются, параметры обфускации становятся уникальными — конфиг AWG тоже нужно заменить;
- панель 3x-ui уходит с открытого порта 2053 на `127.0.0.1`, доступ через `ssh -L`.

## Структура

```
scripts/install.sh       оркестратор фаз (state, флаги, перезапуск по изменённым ключам)
scripts/lib.sh           общие функции: лог, state, config, манифесты, firewall, бэкапы
scripts/lib/xui.sh       единственный адаптер к API 3x-ui (+ общие пользователи Xray-протоколов)
scripts/lib/proto-*.sh   пользователи, ссылки, probe, манифест каждого протокола
scripts/versions.env     закреплённые версии и sha256 всего, что скачивается
scripts/NN-*.sh          фазы 00…09, 99
zoo/                     инструмент zoo (Python stdlib): CLI, веб-админка, пробник
docker/                  тестовый стенд, тесты протоколов docker/tests/, пробник, цензор
docs/                    архитектура, решения, снижение рисков
research/                исследования по датам
```

Файлы на сервере: `/etc/vpn-setup/config.env` (параметры и секреты), `/etc/vpn-setup/protocols.d/*.json` (манифесты протоколов), `/etc/vpn-setup/clients/<имя>/` (ссылки и конфиги пользователей), `/etc/vpn-setup/probe-export.json` (данные для клиентского пробника), журналы — `/var/log/vpn-zoo/`.

## Тесты

```bash
docker/test.sh --mode full --tests all                 # установка как у пользователя + e2e трафика по всем протоколам
docker/test.sh --distro 22.04 --mode full --env ENABLE_TUIC=1 --tests all
```

Подробности стенда: [docker/README.md](docker/README.md).

## Клиентам

Что делать на телефоне, чтобы VPN не выдавал адрес сервера (сплит, пароль на локальный прокси, MAX и банки): [docs/RISK-REDUCTION.md](docs/RISK-REDUCTION.md).
