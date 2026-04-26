# vpn-zoo-installer

Bash-скрипт для развёртывания **трёх VPN-протоколов** на одном Ubuntu VPS одной командой:

- 🔵 **VLESS+Reality+Vision** (TCP) — TLS-маскировка под чужой сайт через Xray-core
- 🟢 **Hysteria2** (UDP/QUIC) — BBR + masquerade, основной канал в РФ
- 🟠 **AmneziaWG v2** (UDP) — WireGuard с CPS-обфускацией под TLS ClientHello

Зачем три протокола разом: ТСПУ режет разные стэки у разных провайдеров — наличие `vless://`, `hysteria2://` и AmneziaWG `.conf` одновременно даёт страховку от единичной блокировки.

> ⚠️ **WIP. Тестировано только в Docker.**
>
> Не запускать на production-сервере где **уже** стоит AmneziaVPN-server, кастомный WireGuard или Hysteria — скрипт перезапишет `/etc/amnezia/`, `/etc/hysteria/`, `awg0.conf` **без backup'а**. Safety guards в TODO.
>
> На чистой Ubuntu 22.04/24.04 — отрабатывает end-to-end в контейнере, но фазы `02-kernel-hwe` и `modprobe amneziawg` в Docker скипаются (shared kernel) — на реальном VPS пока **не проверены**.
>
> **MIT license, без поддержки. Использовать на свой страх и риск.**

## Статус по фазам

Что **проверено** в Docker контейнере (см. ниже):
- Все 8 фаз отрабатывают end-to-end
- Конфиги генерируются, пакеты ставятся, БД 3x-ui принимает inbound
- CREDENTIALS.md и клиентские ссылки печатаются

Что **НЕ проверено** на реальном VPS:
- HWE kernel install + reboot loop (`02-kernel-hwe.sh`) — в Docker shared kernel, фаза скипается
- `modprobe amneziawg` + `awg-quick@awg0` start — то же самое
- systemd активация всех сервисов
- UFW enable

Что **не реализовано** (важно перед прод-запуском):
- ⚠️ **Safety guards** — скрипт перезаписывает существующие конфиги без проверки. Если на сервере уже стоит Amnezia/Hysteria — **сломает их**. См. секцию "Известные риски".
- SSH harden (отключить пароль, разрешить только ключ)
- Бэкап существующих файлов перед перезаписью
- `--force`/`--overwrite` флаги

---

## Быстрый старт

На чистой Ubuntu 22.04/24.04 от root:

```bash
git clone <этот репозиторий> /tmp/vpn-setup
cd /tmp/vpn-setup
./scripts/install.sh
```

Если надо HWE kernel:
1. Скрипт остановится после `02-kernel-hwe` с сообщением "сделай reboot, потом запусти заново"
2. `reboot`
3. Снова `./scripts/install.sh` — продолжит с того же места

В конце получишь:
- `/root/CREDENTIALS.md` — все секреты + клиентские ссылки
- `/etc/vpn-setup/config.env` — конфиг скрипта (тоже секреты, gitignore-нись)
- `/etc/vpn-setup/amneziawg-client.conf` — для импорта в AmneziaVPN
- В stdout — VLESS/Hy2 ссылки + QR-коды

## Флаги

```bash
./scripts/install.sh                       # обычный — продолжает с места остановки
./scripts/install.sh --phase 04-vless-reality  # запустить только одну фазу
./scripts/install.sh --reset               # очистить state (НЕ удаляет установленное!)
./scripts/install.sh --dry-run             # показать что будет делать
CONTAINER_MODE=1 ./scripts/install.sh      # Docker-режим (skip kernel ops)
```

## Структура

```
scripts/
├── install.sh         orchestrator: читает state, диспатчит саб-скрипты
├── lib.sh             общие функции: log, state, config, gen_password, retry
│
├── 00-bootstrap.sh    apt update + base packages + swap + sysctl + BBR
├── 01-firewall.sh     UFW (22, 2053, 8443, 443/udp, 51822/udp) + fail2ban
├── 02-kernel-hwe.sh   linux-generic-hwe-22.04 (требует reboot)
├── 03-3xui.sh         3x-ui панель из релиза + auto-creds
├── 04-vless-reality.sh  Reality keypair + UUID + INSERT в SQLite
├── 05-hysteria2.sh    apernet/hysteria + self-signed cert + config.yaml
├── 06-amneziawg.sh    PPA amnezia/ppa + DKMS + ключи + awg0.conf
└── 99-print-creds.sh  vless://, hy2://, AWG.conf, CREDENTIALS.md
```

## State machine

`/var/lib/vpn-setup/state` — текстовый kv-файл, по строке на фазу:

```
00-bootstrap=done
01-firewall=done
02-kernel-hwe=rebooting        ← поставлено фазой 02, требует reboot
03-3xui=
...
```

Возможные значения:
- `done` — фаза успешно завершилась, при перезапуске install.sh скипается
- `rebooting` — фаза установила kernel/что-то, требующее reboot. install.sh выходит с инструкцией. После reboot пользователь снова запускает install.sh, фаза перезапускается, видит правильное ядро → mark_done.
- пустое / отсутствует — фаза ещё не выполнялась

Сброс: `./install.sh --reset` или `rm /var/lib/vpn-setup/state`. Это **не uninstall** — пакеты и конфиги остаются. Просто скрипт начнёт думать что ничего не делал.

## Конфиг

`/etc/vpn-setup/config.env` — все параметры + сгенерированные секреты. Идемпотентен: при повторном запуске install.sh читает существующие значения, генерит только то чего не хватает. Если хочешь регенерировать всё — удалить файл.

Дефолты:
```bash
PANEL_PORT=2053
VLESS_PORT=8443    VLESS_SNI=www.cbr.ru
HY2_PORT=443       HY2_SNI=bing.com
AWG_PORT=51822     AWG_NETWORK=10.66.66.0/24
LABEL=vpn          # тэг в клиентских ссылках
```

Auto-generated (на первом запуске):
- `PANEL_PATH` (16 hex), `PANEL_USER` (10 alnum), `PANEL_PASS` (12 alnum)
- `HY2_PASSWORD` (24 alnum)
- `VLESS_UUID`, `VLESS_PRIV`, `VLESS_PUB`, `VLESS_SID` (генерятся в 04-фазе через `xray x25519`/`xray uuid`)
- `AWG_SERVER_KEY/PUB`, `AWG_CLIENT_KEY/PUB`, `AWG_CLIENT_PSK` (через `awg genkey/pubkey/genpsk`)

## Тестирование в Docker

```bash
docker run --rm -v "$(pwd):/vpn-setup" -e CONTAINER_MODE=1 ubuntu:22.04 bash -c '
    cd /vpn-setup
    apt-get update -qq && apt-get install -yq curl ca-certificates
    ./scripts/install.sh
'
```

`CONTAINER_MODE=1` (или авто-детект через `/.dockerenv`) включает:
- Skip swap (нет cap_sys_admin)
- Skip sysctl writes (нет /proc/sys access)
- Skip ufw enable (есть cap_net_admin? обычно нет)
- Skip systemd (нет init)
- Skip HWE kernel (shared с хостом)
- Skip modprobe amneziawg (нет kernel headers под хост-ядро)

Полный прогон в чистом Ubuntu 22.04 контейнере → ~2-3 минуты, всё green.

## Известные риски (важно перед прод-запуском)

### 🔴 Перезапись существующей конфигурации

Если на сервере **уже** стоит:
- AmneziaVPN-server (полный комплект от amnezia.org с web UI)
- Чужой WireGuard на `/etc/wireguard/` или `/etc/amnezia/amneziawg/`
- Hysteria1 или Hysteria2 другой версии

Скрипт **перезаписывает** без backup'а:
- `/etc/amnezia/amneziawg/server.key`, `client*.{key,psk,pub}`, `awg0.conf`
- `/etc/hysteria/{cert.pem,key.pem,config.yaml}`
- Бинарник `/usr/local/x-ui/x-ui` (БД 3x-ui сохраняется)

**Перед запуском на не-чистом VPS — проверять что нет конфликтов:**
```bash
ls /etc/amnezia/ /etc/hysteria/ /etc/wireguard/ /usr/local/x-ui/ 2>/dev/null
systemctl list-units 'amnezia*' 'hysteria*' 'awg-quick*' 'x-ui*' 'wg-quick*' --all
```

Если что-то нашлось — текущий скрипт **сломает** это. Доделать safety guards перед использованием.

### 🟡 ifconfig.me detect: предполагает что есть outbound интернет

Если VPS за NAT или с firewall у провайдера, `curl ifconfig.me` упадёт. Тогда `SERVER_IP` останется пустым, и клиентские ссылки сгенерятся с пустым host. Workaround: задать `SERVER_IP` в `/etc/vpn-setup/config.env` руками **до** первого запуска.

### 🟡 PPA amnezia/ppa требует Ubuntu

На Debian `add-apt-repository` для launchpad PPA не работает. Нужно вручную добавлять ключ + sources.list.

### 🟢 Reality SNI

Дефолт `www.cbr.ru` — RU-сайт с TLS 1.3. Хорош для российских юзеров, плох для остальных. На западном клиенте лучше `www.microsoft.com` или `dl.google.com`. Менять в `config.env`.

## TODO

В порядке приоритета:

1. **Safety guards** — проверка существующих файлов в `06-amneziawg.sh` и `05-hysteria2.sh`. Минимум: `die` с понятным сообщением если конфликт.
2. **Backup перед перезаписью** — в `/var/backups/vpn-setup-$(date)/` копировать всё что собираемся переписать.
3. **`--force` флаг** — пропустить safety guards (для пере-установки на свой же сервер).
4. **Тест на реальном VPS** — Hetzner CX22 или Multipass. Проверить HWE-reboot loop, modprobe, systemd активацию.
5. **uninstall.sh** — обратная операция: остановить сервисы, снести конфиги, очистить UFW. Требует `--really-uninstall` подтверждения.
6. **add-client.sh** — добавить нового AWG-клиента (новый keypair + INSERT в `awg0.conf`).
7. **rotate-keys.sh** — перегенерация всех секретов (полезно при компрометации или подозрении).
8. **Поддержка Debian 12** — другая логика установки PPA Amnezia.
9. **Ansible refactor** — если разрастётся до управления N серверами. Сейчас bash достаточно.

## Зачем не Ansible

Для одного personal VPN — bash проще:
- Не нужен Ansible на dev-машине (Python + ssh + inventory + playbook)
- Скрипт работает локально на target-VPS, не нужен SSH-канал из dev
- bash + apt + systemd идиоматично для Ubuntu, читается без обучения
- Текущий объём (8 фаз × ~80 строк) умещается в голове

Когда станет N>3 серверов или сложнее одного зоопарка — переезд на Ansible / Terraform.
