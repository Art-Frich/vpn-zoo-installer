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
| 04d | `04d-tuic.sh` | TUIC v5 через 3x-ui (флаг) |
| 05 | `05-hysteria2.sh` | Hysteria2 apernet закреплённой версии, userpass, pinSHA256, опционально Salamander-инстанс и port hopping |
| 06 | `06-amneziawg.sh` | AmneziaWG: движок kernel (DKMS) или userspace (amneziawg-go), уникальные параметры на каждую установку |
| 07 | `07-routing.sh` | Анти-утечки: echo-сервисы → WARP или блок, RU-egress (opt-in), sniffing, geo-файлы и таймер обновления |
| 08 | `08-warp.sh` | Cloudflare WARP как outbound (opt-in) |
| 09 | `09-zoo.sh` | Установка `zoo` (CLI + админка + коллектор трафика + таймеры) |
| 99 | `99-print-creds.sh` | Ссылки, QR, CREDENTIALS.md, экспорт для пробника. Читает манифесты |

Фаза, которой нет на диске или которая выключена флагом, пропускается. Порядок задаётся массивом `PHASES` в `install.sh`.

## 3. Конфиг

- `/etc/vpn-setup/config.env` (0600). Все параметры и сгенерированные секреты. Каждый модуль добавляет свои переменные через `config_set KEY VALUE`, а не через переписывание файла целиком.
- Флаги включения: `ENABLE_VLESS=1 ENABLE_XHTTP=1 ENABLE_SS=1 ENABLE_TUIC=0 ENABLE_HY2=1 ENABLE_HY2_OBFS=0 ENABLE_AWG=1 ENABLE_WARP=0 RU_EGRESS=direct|block|warp`.
- Движки: `AWG_ENGINE=auto|kernel|userspace` (`auto` = kernel, если DKMS собирается, иначе userspace), `HY2_ENGINE=apernet`.
- Порты по умолчанию: VLESS 443/tcp, Hy2 443/udp, остальные — случайные высокие порты при первой установке, сохраняются в config.env. Никогда не используем 1080, 3128, 8080, 9050, 2053, 54321.

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

## 5. Пользователи

Один пользователь зоопарка = креды во всех включённых протоколах:
- Xray-семейство (VLESS, XHTTP, SS, TUIC): клиент 3x-ui, общий `subId`, `email=<name>`.
- Hysteria2: `auth.type: userpass`, запись `<name>: <password>` в config.yaml, reload.
- AmneziaWG: отдельный peer (ключи, PSK, /32 из пула), живое применение через `awg set`/`awg syncconf`, `.conf` в `/etc/vpn-setup/clients/<name>/`.

Хранилище: `/etc/vpn-setup/users.json` (источник правды для зоопарка), операции — `zoo user add|del|disable|enable|list|show`. Пользователь `owner` создаётся при установке.

## 6. Инструмент zoo (Python 3 stdlib)

- `zoo status` — сервисы, порты, сертификаты, версии, лишние listen на 0.0.0.0, метрики хоста.
- `zoo user ...` — управление пользователями во всех протоколах.
- `zoo links <user>` — ссылки и QR.
- `zoo traffic [user] [--period]` — трафик по пользователю и протоколу (3x-ui API, Hy2 trafficStats API, `awg show dump`), история в SQLite.
- `zoo probe --local` — самопроверка с сервера: «работает ли протокол в принципе».
- `zoo export-probe` — пакет для клиентского пробника.
- `zoo web` — веб-админка на `127.0.0.1:$ZOO_WEB_PORT` с токеном; доступ через `ssh -L`.
- `zoo upgrade`/`zoo smoke` — обновление закреплённых версий с проверкой.

## 7. Пробник

1. **Сервер, «в принципе»**: `zoo probe --local` поднимает клиент каждого протокола на самом сервере и идёт на публичный IP сервера (если хостер режет hairpin, то на 127.0.0.1) → маленький запрос, большой запрос (≥512 КБ), проверка IP выхода.
2. **Клиент, «блокируется ли»**: контейнер `zoo-probe` (Docker на машине пользователя, трафик идёт через его провайдера) или `zoo probe --remote` на любой Linux-машине. Для каждого протокола: TCP/UDP-доступность порта, рукопожатие, маленький и большой запрос, задержка, скорость. Классификация: `OK`, `IP_BLOCKED`, `HANDSHAKE_FAIL`, `FREEZE_16K`, `UDP_BLOCKED`, `SLOW`, `SERVER_DOWN`. Сравнение с серверным прогоном даёт вердикт «работает, но блокируется у вас».

Второй сервер не обязателен. Пункт 1 выполняется с самого сервера. Вторая точка вне РФ полезна как контроль, но не требуется.

## 8. Тестовый стенд (docker/)

- `docker/server.Dockerfile` — Ubuntu 22.04/24.04 с systemd (privileged, cgroupns=host). Инсталлер работает в реальном режиме, кроме загрузки kernel-модуля amneziawg: в Docker AWG проверяется через userspace-движок.
- `docker/probe.Dockerfile` — клиенты xray, hysteria, amneziawg-go и тест-раннер.
- `docker/censor/` — маршрутизатор между пробником и сервером с правилами «как ТСПУ»: drop UDP, обрыв TCP после 16 КБ, блок IP. Используется для проверки классификации пробника.
- `docker/test.sh` — полный e2e: установка → `zoo probe --local` → клиентский пробник напрямую и через цензора → отчёт.
- На Windows/Git Bash все docker-команды запускать с `MSYS_NO_PATHCONV=1`.

## 9. Правила кода

- bash: `set -euo pipefail`, идемпотентность, никаких секретов в argv там, где есть альтернатива, `umask 077` для секретов, LF.
- Всё, что скачивается, закреплено в `versions.env` и проверяется по sha256.
- Перед перезаписью чужого конфига — бэкап в `/var/backups/vpn-setup/<ts>/`; при чужой установке — отказ без `--force`.
- Любой вызов API 3x-ui — только через `scripts/lib/xui.sh`.
- Каждая фаза проверяет себя (сервис активен, порт слушает) и падает с понятным сообщением.
