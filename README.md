# vpn-zoo-installer

«Зоопарк» VPN-протоколов на одном VPS для пользователей из России. Одна команда на чистой Ubuntu 22.04/24.04 ставит несколько независимых протоколов: TCP и UDP, с TLS-маскировкой и без. ТСПУ режет разное у разных провайдеров и на мобильном; если один протокол заблокирован, остаются другие.

После установки инсталлер сам проверяет каждый протокол с сервера («работает в принципе»), а пробник в Docker на вашем компьютере показывает, что из этого блокирует ваш провайдер.

> Проверено на тестовом стенде (systemd в Docker, Ubuntu 22.04 и 24.04) настоящими клиентами Xray, sing-box, hysteria и amneziawg-go, плюс эмулятор ТСПУ. На боевом VPS через российского провайдера, с модулем ядра AmneziaWG и на arm64 — ещё нет (см. [Ограничения](#ограничения-и-что-не-проверено)). MIT, без гарантий.

**Содержание:** [Протоколы](#что-вы-получите) · [Установка](#установка) · [Самопроверка](#самопроверка-в-конце-установки) · [Проверка у себя](#блокирует-ли-ваш-провайдер-пробник-на-вашей-машине) · [Админка и zoo](#админка-и-инструмент-zoo) · [Пользователи](#пользователи) · [Анти-детект](#анти-детект-и-утечки-ip) · [Параметры](#параметры) · [Обновление](#обновление-закреплённых-версий) · [С первой версии](#обновление-с-первой-версии) · [Тесты](#тесты) · [Ограничения](#ограничения-и-что-не-проверено) · [FAQ](#faq-и-неполадки) · [Структура](#структура-проекта)

## Что вы получите

| id | Протокол | Транспорт, порт | По умолчанию | Зачем | Клиенты |
|---|---|---|---|---|---|
| `vless-reality` | VLESS + REALITY + Vision | TCP 443 | вкл. | Основной TCP: выглядит как TLS к чужому сайту | v2rayN ≥7.25, v2rayNG ≥2.2.6, Happ, INCY, Streisand, Throne; mihomo ≥1.19.31 (подписка/YAML) |
| `vless-xhttp` | VLESS + XHTTP + REALITY | TCP, случайный порт | вкл. | Запасной TCP: свой порт, свои ключи и SNI, не падает вместе с 443 | v2rayN, v2rayNG, Happ, Throne; mihomo ≥1.19.22 |
| `ss2022` | Shadowsocks-2022 | TCP+UDP, случайный порт | вкл. | Запасной без TLS: его не ловят правила по TLS ClientHello | v2rayN/v2rayNG, Happ, Hiddify, sing-box, mihomo |
| `hysteria2` | Hysteria2 | UDP 443 | вкл. | Основной UDP (QUIC), быстрый на плохих каналах; ссылка первой версии продолжает работать | hysteria 2.12, v2rayN/v2rayNG, Happ, mihomo, Throne |
| `amneziawg` | AmneziaWG 2.0 | UDP, случайный порт | вкл. | Полный L3-туннель, обфусцированный WireGuard; в клиентах нет локального прокси | AmneziaVPN ≥5.0.1.5, AmneziaWG ≥2.0, WG Tunnel ≥4.2, DefaultVPN (iOS), mihomo ≥1.19.14 |
| `hysteria2-obfs` | Hysteria2 + Salamander | UDP, случайный порт | `ENABLE_HY2_OBFS=1` | QUIC, который не похож на QUIC: если режут по сигнатуре QUIC | hysteria, sing-box, mihomo, v2rayN/v2rayNG |
| `tuic` | TUIC v5 | UDP, случайный порт | `ENABLE_TUIC=1` | Ещё один QUIC-протокол для клиентов на sing-box | sing-box/SFA/SFI, Hiddify, Karing, NekoBox, mihomo |
| — | Cloudflare WARP | исходящий | `ENABLE_WARP=1` | Выход для echo-сервисов «мой IP» (и RU по флагу) не с адреса сервера | — |

Движки: VLESS, XHTTP, SS-2022 и TUIC — Xray внутри [3x-ui](https://github.com/MHSanaei/3x-ui) v3.9.0 (Xray 26.9.30); Hysteria2 — standalone [HyNetworks/hysteria](https://github.com/HyNetworks/hysteria) v2.12.3; AmneziaWG — модуль ядра (DKMS) или `amneziawg-go`. Всё скачиваемое закреплено в [scripts/versions.env](scripts/versions.env) и проверяется по sha256.

**Важно про клиентов VLESS/XHTTP.** Xray 26.9.30 на сервере отклоняет ClientHello без X25519MLKEM768. Клиенты на sing-box (SFA/SFI, Hiddify, NekoBox, Karing), Shadowrocket и старые ядра Xray показывают «подключено», но трафика нет. Для VLESS и XHTTP берите клиент на ядре Xray ≥26.x. NekoBox и Hiddify игнорируют пин сертификата Hysteria2, для неё они не рекомендуются.

Кроме протоколов:
- анти-утечки: echo-сервисы «узнай свой IP» через туннель блокируются или идут через WARP, sniffing, журналы посещений Xray выключены, BitTorrent заблокирован, из туннеля не видны сервисы самого сервера;
- панель 3x-ui слушает только `127.0.0.1`, доступ через `ssh -L`;
- инструмент `zoo`: пользователи во всех протоколах сразу, ссылки и QR, трафик, самопроверка, обновление версий, веб-админка;
- UFW (default deny), fail2ban, unattended-upgrades.

## Установка

Нужно: VPS с Ubuntu 22.04 или 24.04 (x86-64; arm64 не проверен), root или пользователь с `sudo`, systemd, около 1 ГБ свободного места. Не ставьте поверх чужого 3x-ui, Hysteria или AmneziaWG без `--force`: инсталлер найдёт их и остановится.

```bash
sudo apt-get update && sudo apt-get install -y git     # если git нет (минимальные образы хостеров)
sudo git clone https://github.com/Art-Frich/vpn-zoo-installer.git /opt/vpn-zoo-src
cd /opt/vpn-zoo-src
sudo bash scripts/install.sh
```

Установка идёт 5–15 минут (дольше всего — сборка `amneziawg-go`, если модуль ядра не подходит). Каталог клона не удаляйте: из него `zoo upgrade` берёт новые версии. Все команды `sudo bash scripts/install.sh …` ниже запускаются из него (`cd /opt/vpn-zoo-src`).

**Перезагрузка.** На Ubuntu 22.04 со старым ядром фаза 02 может поставить HWE-ядро 6.8 (для модуля AmneziaWG) и попросить перезагрузку. Тогда `sudo reboot` и та же команда ещё раз: установка продолжится с места остановки. Если хостер не даёт сменить ядро, запустите с `AWG_NO_HWE=1`: AmneziaWG пойдёт в userspace (`amneziawg-go`).

**Где ключи.** Фаза 99 печатает ссылки и QR пользователя `owner` по каждому протоколу, `.conf` и ключ `vpn://` AmneziaWG, команду туннеля к панели и таблицу «какие клиенты с чем работают». То же сохраняется в `/root/CREDENTIALS.md` (0600). Позже ссылки выдаёт `sudo zoo links owner --qr`.

Файлы на сервере:

| Путь | Что |
|---|---|
| `/etc/vpn-setup/config.env` | параметры и секреты (0600) |
| `/etc/vpn-setup/protocols.d/*.json` | манифесты протоколов |
| `/etc/vpn-setup/users.json` | реестр пользователей |
| `/etc/vpn-setup/clients/<имя>/` | ссылки и конфиги пользователя |
| `/etc/vpn-setup/probe-export.json` | пакет для клиентского пробника (ключи `zoo-probe`) |
| `/var/lib/vpn-zoo/` | история трафика, итог самопроверки `probe-local.json` |
| `/var/log/vpn-zoo/install-*.log` | журналы установки (с ключами; хранятся 10 последних) |
| `/var/backups/vpn-setup/<время>/` | бэкапы перед перезаписью конфигов |

## Самопроверка в конце установки

После итога `install.sh` поднимает на самом сервере клиент каждого протокола (`zoo probe --local`), подключается к публичному IP сервера и делает малый запрос, загрузку 2 МБ и проверку IP выхода. Получается таблица «работает в принципе»:

```
протокол        порт       работает в принципе  итог  скорость
amneziawg       27559/udp  да                   OK    110 Мбит/с
hysteria2       443/udp    да                   OK    180 Мбит/с
vless-reality   443/tcp    да                   OK    59 Мбит/с
...
Работает в принципе: 7 из 7.
```

Протокол, который не работает даже на самом сервере, выделяется красным «НЕ РАБОТАЕТ на самом сервере» с причиной и командой для подробностей. Установку это не прерывает. Ниже таблицы — готовые команды для следующего шага (проверки с вашей машины).

- Проверка идёт кредами служебного пользователя `zoo-probe`, не `owner`: проба не выбивает сессию AmneziaWG у телефона владельца и не смешивается с его трафиком.
- Если хостер не пускает трафик с сервера на его же публичный IP (нет hairpin), проба повторяется через `127.0.0.1`, в отчёте будет `куда: loopback`.
- Итог сохраняется в `/var/lib/vpn-zoo/probe-local.json` (страница «Проверка» админки) и вкладывается в пакет `/etc/vpn-setup/probe-export.json`.
- Повторить: `sudo zoo probe --local --summary --export /etc/vpn-setup/probe-export.json`. Отключить при установке: `ZOO_SELFTEST=0`.

## Блокирует ли ваш провайдер: пробник на вашей машине

«Работает в принципе» ещё не значит «работает у вас». Второй шаг — тот же набор проверок из вашей сети: контейнер `zoo-probe` подключается к серверу через вашего провайдера и сравнивает результат с самопроверкой сервера.

Нужны Docker (на Windows и macOS — Docker Desktop), `git` и `ssh`/`scp` (в Windows 10/11 OpenSSH встроен). **VPN на компьютере на время проверки выключите**, иначе проверяется чужой туннель, а не ваш провайдер. Мобильный и домашний интернет блокируют по-разному: проверяйте из той сети, которая важна (например, раздайте интернет с телефона на ноутбук).

**Linux, macOS:**

```bash
git clone https://github.com/Art-Frich/vpn-zoo-installer.git && cd vpn-zoo-installer
mkdir probe
scp root@СЕРВЕР:/etc/vpn-setup/probe-export.json probe/probe-export.json
docker build -f docker/probe.Dockerfile -t zoo-probe .
docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe
rm probe/probe-export.json
```

**Windows, PowerShell:**

```powershell
git clone https://github.com/Art-Frich/vpn-zoo-installer.git; cd vpn-zoo-installer
mkdir probe
scp root@СЕРВЕР:/etc/vpn-setup/probe-export.json probe\probe-export.json
docker build -f docker/probe.Dockerfile -t zoo-probe .
docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "${PWD}\probe:/data" zoo-probe
Remove-Item probe\probe-export.json
```

Git Bash на Windows: `MSYS_NO_PATHCONV=1 docker run ... -v "$(pwd -W)/probe:/data" zoo-probe`. Нестандартный порт SSH: `scp -P ПОРТ ...`. Только часть протоколов: добавьте в конец `docker run` после `zoo-probe` аргументы `--proto vless-reality --proto hysteria2`. Клон и `docker build` нужны один раз (сборка — несколько минут, `amneziawg-go` собирается из исходников); для повторной проверки, например из другой сети, — только `scp` и `docker run`. После `git pull` пересоберите образ.

**Если вход root по SSH запрещён** (`PermitRootLogin no`): пакет лежит в каталоге root с правами 0600, сначала скопируйте его себе на сервере:

```bash
ssh -t user@СЕРВЕР 'sudo install -m 600 -o "$USER" /etc/vpn-setup/probe-export.json ~/probe-export.json'
scp user@СЕРВЕР:probe-export.json probe/probe-export.json
ssh user@СЕРВЕР rm probe-export.json
```

**Без Docker** (любой Linux с `xray`, `hysteria`, `sing-box`, `amneziawg-go`, `awg` в `PATH`): `sudo python3 zoo/zoo probe --remote probe/probe-export.json --md probe/probe-report.md --out probe/probe-report.json`. Клиента, которого нет, пробник пропустит (`SKIPPED`). Подробности, переменные путей к клиентам и параметры измерений — [docker/probe/README.md](docker/probe/README.md).

**В пакете ключи доступа к серверу** (служебного пользователя `zoo-probe`). Передавайте только по `scp`, после проверки удалите у себя. На сервере его можно удалить командой `sudo rm /etc/vpn-setup/probe-export.json`; новый создаст `sudo zoo probe --local --summary --export /etc/vpn-setup/probe-export.json`. Если пакет утёк — отозвать ключи и выпустить новые (ключи `owner` не меняются):

```bash
cd /opt/vpn-zoo-src
sudo zoo user del zoo-probe --force          # ключи из пакета больше не работают, сам пакет удаляется
sudo bash scripts/install.sh --phase 09      # новый zoo-probe
sudo zoo probe --local --summary --export /etc/vpn-setup/probe-export.json   # новый пакет
```

### Как читать результат

Таблица в терминале и файлы `probe/probe-report.md`, `probe/probe-report.json`. В конце — сравнение «сервер ↔ у вас» по каждому протоколу:

| Вывод | Что значит | Что делать |
|---|---|---|
| **работает у вас** | Протокол проходит через вашего провайдера | Пользоваться. Держите в клиенте 2–3 рабочих протокола |
| **работает в принципе, блокируется у вас (тип)** | Сервер в порядке, режет сеть по пути | Переключиться на рабочий протокол. Тип блокировки — ниже |
| **не работает на самом сервере** | Дело не в блокировке | `sudo zoo status`, `sudo zoo probe --local --proto <id>` |
| **у вас не проверено (вердикт)** | Клиент пробника не запустился (`CLIENT_ERROR`, обычно нет `/dev/net/tun`) или его нет (`SKIPPED`) | Для AmneziaWG добавить `--cap-add NET_ADMIN --device /dev/net/tun` |
| **у вас не проверялось** | Протокол не попал в прогон (`--proto`) | Запустить без `--proto` |
| **не работает у вас (вердикт); серверной самопроверки нет** | В пакете нет итога самопроверки (пакет из `zoo export-probe` без `--local`) | `sudo zoo probe --local --summary --export /etc/vpn-setup/probe-export.json` и проверить снова |

Вердикты отдельного протокола:

| Вердикт | Когда | Обычно значит |
|---|---|---|
| `OK` | всё прошло, ≥ 2 Мбит/с, задержка ≤ 1,5 с | работает |
| `SLOW` | медленно, или загрузка оборвалась после 64 КБ | работает плохо |
| `FREEZE_16K` | TCP: малый запрос прошёл, загрузка встала до 64 КБ | «заморозка» ТСПУ на TCP+TLS к сети хостера; помогают UDP-протоколы или другой хостер |
| `HANDSHAKE_FAIL` | порт отвечает, туннель не поднимается | блок по сигнатуре (RST на ClientHello) или устаревшие ключи |
| `IP_BLOCKED` | TCP-порт молчит; у UDP нет ответа и TCP тоже молчит | заблокирован IP (или только порт — в причине так и написано) |
| `UDP_BLOCKED` | UDP молчит, TCP того же сервера отвечает | режут UDP/QUIC; остаются TCP-протоколы |
| `SERVER_DOWN` | порт закрыт (RST, ICMP) или самопроверка сервера тоже не прошла | проблема сервера |
| `CLIENT_ERROR` | клиент пробника не запустился | проблема пробника, не сети |
| `SKIPPED` | нет клиента или в манифесте нет probe | не проверялось |

Сравнение уже есть в выводе пробника. Чтобы оно было и на сервере: вставьте `probe/probe-report.json` на странице «Проверка» админки или скопируйте отчёт и сравните в консоли:

```bash
scp probe/probe-report.json root@СЕРВЕР:      # со своей машины
sudo zoo probe --compare /var/lib/vpn-zoo/probe-local.json probe-report.json   # на сервере
```

### Нужен ли второй сервер?

**Нет.** Оба вопроса закрываются без него:
- «работает ли протокол в принципе» — клиент поднимается на самом сервере и идёт на его публичный IP (а без hairpin — через loopback);
- «блокирует ли ваш провайдер» — на этот вопрос может ответить только ваша сеть. Второй сервер за рубежом этого не скажет, а сервер в российском ДЦ скажет только про свою сеть: ТСПУ в ДЦ и у домашнего или мобильного провайдера ведут себя по-разному.

Что дала бы вторая точка (необязательно, автоматизации для неё нет): независимую проверку «снаружи». Самопроверка через собственный публичный IP может не пройти через внешний файрвол хостера (security group, анти-DDoS), так что порт, закрытый хостером снаружи, на сервере выглядит рабочим. Запуск пробника с любой машины вне РФ (тот же `docker run` или `zoo probe --remote`) отделяет «хостер не пропускает порт» от «блокирует ваш провайдер». Постоянный мониторинг из РФ — тоже только с отдельной машины; в проекте его нет.

## Админка и инструмент zoo

Веб-админка слушает только `127.0.0.1` и доступна через SSH-туннель:

```bash
sudo zoo web --info        # на сервере: команда туннеля, адрес и токен входа
```

```bash
ssh -N -L ПОРТ:127.0.0.1:ПОРТ root@СЕРВЕР    # на своём компьютере (Linux, macOS, PowerShell), окно не закрывать
# браузер: http://127.0.0.1:ПОРТ/  → токен из zoo web --info
```

`ssh -N` ничего не печатает и не возвращает приглашение — так и должно быть, туннель работает, пока окно открыто. Один туннель сразу к админке и панели 3x-ui — готовая команда в `/root/CREDENTIALS.md` (UFW ограничивает частые SSH-подключения с одного IP, поэтому лучше один туннель, чем несколько).

Страницы: **Обзор** (сервисы, порты, сертификаты, нагрузка, проблемы), **Пользователи** (добавить во все протоколы, отключить, включить, удалить, ссылки, QR, файлы AWG), **Трафик** (графики по пользователям и протоколам), **Проверка** (запустить самопроверку, вставить отчёт клиентского пробника и сравнить), **Журнал**, **Настройки** (перезапуск сервисов, smoke-проверка, снятие трафика, обновление geo-файлов). Новый токен (старые сессии закроются): `sudo zoo web --new-token`.

Панель 3x-ui тоже только на `127.0.0.1`: команда туннеля и путь — в `/root/CREDENTIALS.md`, логин и пароль — `PANEL_USER`/`PANEL_PASS` в `config.env`. Пользователей заводите через `zoo`, а не в панели: панель не видит Hysteria2 и AmneziaWG.

Шпаргалка `zoo` (от root; у любой команды есть `--json`):

| Команда | Что делает |
|---|---|
| `zoo status [--no-api]` | сервисы, порты, сертификаты, версии, лишние listen, метрики, коллектор трафика; код 1 при проблемах |
| `zoo user add ИМЯ [--note "кто"] [--proto ID]` | креды во всех включённых протоколах (или только в указанных) |
| `zoo user list [--all] [--verify]` | список; `--all` — со служебным `zoo-probe`; `--verify` — сверить с протоколами |
| `zoo user show ИМЯ` | протоколы, ссылки и файлы пользователя |
| `zoo user disable ИМЯ` / `enable ИМЯ` | отключить (креды сохраняются) / вернуть |
| `zoo user del ИМЯ [--force]` | удалить из всех протоколов |
| `zoo user sync [ИМЯ...]` | завести креды в протоколах, включённых позже |
| `zoo links [ИМЯ] [--qr] [--invert] [--svg-dir DIR] [--proto ID]` | ссылки и QR (по умолчанию `owner`) |
| `zoo traffic [ИМЯ] [--period 1h\|24h\|7d\|30d\|90d] [--by protocol] [--all]` | трафик; `--collect` — снять счётчики сейчас |
| `zoo probe --local [--summary] [--export FILE] [--proto ID] [--user ИМЯ]` | самопроверка с сервера |
| `zoo probe --remote FILE [--md FILE] [--out FILE]` | клиентский прогон по пакету |
| `zoo probe --compare LOCAL REMOTE` | сравнить отчёты сервера и клиента |
| `zoo export-probe --out FILE [--user ИМЯ]` | пакет для клиентского пробника |
| `zoo smoke [--no-probe]` | быстрая проверка здоровья (сервисы, порты, API, UFW, коллектор, самопроверка) |
| `zoo upgrade [--fetch] [--pull] [--apply] [--phase ФАЗА]` | обновление закреплённых версий (ниже) |
| `zoo version [--all]` | версия zoo; `--all` — версии компонентов и пины |
| `zoo web [--info] [--new-token]` | веб-админка (как сервис её держит `zoo-web.service`) |

Трафик снимает `zoo-collector.timer` каждые 5 минут (3x-ui API, trafficStats Hysteria, `awg show`), история — в SQLite. Счётчики Xray 3x-ui обновляет примерно раз в 10 секунд: `zoo traffic --collect` сразу после подключения может ещё не увидеть трафик VLESS/XHTTP/SS-2022/TUIC.

## Пользователи

Один пользователь зоопарка — это креды во всех включённых протоколах сразу. `owner` создаётся при установке.

```bash
sudo zoo user add masha --note "сестра"
sudo zoo links masha --qr          # ссылки и QR; файлы AWG — в /etc/vpn-setup/clients/masha/
sudo zoo user disable masha        # временно отключить; enable — вернуть
sudo zoo user del masha
```

- Имя: латиница в нижнем регистре, цифры, `-` и `_`, до 32 символов, первый символ — буква или цифра. Имя `zoo-probe` зарезервировано.
- Добавить можно и в админке (страница «Пользователи»): там же ссылки, QR и скачивание `.conf` AmneziaWG. Файлы AWG с сервера: `/etc/vpn-setup/clients/ИМЯ/amneziawg.conf` (и `.png` с QR).
- Отключение в Hysteria2 и AmneziaWG рвёт сессии сразу, без рестарта сервиса.
- Xray-протоколы (VLESS, XHTTP, SS-2022, TUIC) у пользователя общие: один клиент 3x-ui на все, включается и выключается сразу во всех четырёх (D18).
- Протокол, включённый после создания пользователей, получает их при следующем `install.sh` (или `zoo user sync`).
- Служебный `zoo-probe` скрыт из списков и отчётов по пользователям, но учтён в итогах по протоколам. Удалить или отключить — только с `--force`.

У каждого свой ключ: утёкший отключается одной командой, остальных это не задевает.

## Анти-детект и утечки IP

Главное из [docs/RISK-REDUCTION.md](docs/RISK-REDUCTION.md):

- **Реальный риск — утечка IP сервера**, а не то, что приложение «видит VPN». Адрес утекает, когда российское приложение ходит в интернет через VPN. Главная защита — на телефоне: сплит «РФ напрямую», исключение RU-приложений из VPN, пароль на локальный прокси клиента (или его отключение), «Allow LAN» не включать. Пошагово для Happ, v2rayNG, AmneziaVPN — §4.2–4.4.
- **Что делает сервер** (§5): echo-сервисы «мой IP» (все 6 из MAX) блокируются или идут через WARP; sniffing; opt-in блок или WARP для RU-направлений (`RU_EGRESS`, `HY2_RU_EGRESS`, `AWG_RU_EGRESS`); из туннеля не видны 127.0.0.1, частные сети и сервисы сервера кроме SSH; журналы посещений выключены; BitTorrent заблокирован; уникальные параметры обфускации AmneziaWG на каждую установку; JSON-подписка 3x-ui (SOCKS без пароля у клиента) никогда не включается; панель не торчит наружу. Для AmneziaWG echo-правило не работает: там защищает только сплит на клиенте.
- **MAX, банки, Госуслуги** — §4 «Как совмещать VPN и MAX»: варианты от отдельного телефона и VPN на роутере до per-app исключения и автоматизации на iPhone; что из этого прячет факт VPN, а что только адрес.
- **Чего сервер не может**: скрыть факт VPN на телефоне, закрыть локальный прокси без пароля у клиента, спасти от бана подсети хостера (§5, «Чего сервер не может в принципе»).
- **Гигиена владельца** (§6): ничего не публиковать, не раздавать широко, ключи не пересылать через MAX/VK, бэкап `/etc/vpn-setup/` вне сервера, план на случай бана IP.

## Параметры

Задаются через окружение, сохраняются в `/etc/vpn-setup/config.env` и действуют при следующих запусках:

```bash
ENABLE_TUIC=1 ENABLE_HY2_OBFS=1 RU_EGRESS=block sudo -E bash scripts/install.sh
```

Повторный запуск с изменённым параметром перезапускает только фазу-владельца (и маршрутизацию). `ENABLE_X=0` на установленном сервере выключает протокол: сервис или inbound остановлен, порт закрыт, манифест `enabled=false`. `ENABLE_X=1` возвращает его.

| Группа | Ключи | По умолчанию |
|---|---|---|
| Протоколы | `ENABLE_VLESS`, `ENABLE_XHTTP`, `ENABLE_SS`, `ENABLE_HY2`, `ENABLE_AWG` | `1` |
| | `ENABLE_TUIC`, `ENABLE_HY2_OBFS`, `ENABLE_WARP` | `0` |
| | `ENABLE_ZOO` (инструмент zoo, админка, самопроверка) | `1`; выключение после установки не реализовано |
| Движки | `AWG_ENGINE=auto\|kernel\|userspace` | `auto`: модуль ядра, если DKMS собирается, иначе `amneziawg-go` |
| | `HY2_ENGINE` | `apernet` (единственный) |
| Выход в RU | `RU_EGRESS=direct\|block\|warp` (Xray-протоколы) | `direct` |
| | `HY2_RU_EGRESS=direct\|block\|warp`, `AWG_RU_EGRESS=direct\|block` | `direct` |
| | `ENABLE_BITTORRENT=1` — пропускать BT; `ROUTING_ECHO_EXTRA=a.com,b.com` — свои echo-домены | `0`, пусто |
| Порты | `VLESS_PORT` | `443` |
| | `HY2_PORT` | `443` (UDP) |
| | `XHTTP_PORT`, `SS_PORT`, `TUIC_PORT`, `AWG_PORT`, `HY2_OBFS_PORT` | случайные высокие, выбираются один раз; никогда 1080, 3128, 8080, 9050, 2053, 54321 |
| VLESS | `VLESS_SNI` (+`VLESS_SNI_CHECK=0`), `VLESS_TARGET=host:port` | target выбирает валидатор из списка кандидатов |
| XHTTP | `XHTTP_PLACEMENT=port\|fallback`, `XHTTP_SNI`, `XHTTP_PATH`, `XHTTP_MODE` | `port` (отдельный порт, D16) |
| Hysteria2 | `HY2_SNI`, `HY2_MASQ_URL`, `HY2_HOP=1` + `HY2_HOP_RANGE` (port hopping) | `bing.com`, `40000-49999` |
| AmneziaWG | `AWG_PROFILE=v2\|v3`, `AWG_RT=1`, `AWG_NETWORK`, `AWG_MTU`, `AWG_DNS`, `AWG_KEEPALIVE`, `AWG_NO_HWE=1` | `v2`, `10.66.66.0/24`, `1280`, Cloudflare DNS, `25` |
| Сервер | `SERVER_IP`, `LABEL` (имя в ссылках), `SSH_PORTS` | определяются сами, `vpn` |
| Панель | `PANEL_PORT`, `PANEL_PATH`, `PANEL_USER`, `PANEL_2FA=1` | случайные, 2FA выкл. |
| Обновления ОС | `AUTO_REBOOT=0`, `AUTO_REBOOT_TIME=ЧЧ:ММ` (применяются через `--phase 00`) | автоперезагрузка в `04:00` |
| Подписка | `SUB_PUBLIC=1`, `DOMAIN` | `0`. **Заготовка:** фазы публичной подписки с доменом и TLS нет, порт подписки не открывается; ссылки выдаёт `zoo` |

Разовые переключатели, которые не сохраняются: `ZOO_SELFTEST=0` (без самопроверки), `ZOO_KEEP_LOGS`, `ZOO_KEEP_BACKUPS` (сколько журналов и бэкапов хранить, по умолчанию 10), `ZOO_VLESS_REPICK=1` вместе с `--phase 04` (выбрать REALITY target заново), `WARP_REREGISTER=1` вместе с `--phase 08` (новая регистрация WARP). Полный список и владельцы ключей — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3, решения по умолчанию с обоснованием — [docs/DECISIONS.md](docs/DECISIONS.md).

Аргументы `install.sh`:

```bash
sudo bash scripts/install.sh --dry-run       # план без изменений
sudo bash scripts/install.sh --phase 05      # одна фаза принудительно
sudo bash scripts/install.sh --only 05,07     # только эти фазы (выполненные пропускаются); --skip 06 — все, кроме
sudo bash scripts/install.sh --rerun         # пройти заново и выполненные фазы (идемпотентно)
sudo bash scripts/install.sh --force         # перезаписать чужую установку (с бэкапом)
sudo bash scripts/install.sh --reset         # очистить state (установленное не удаляется)
```

## Обновление закреплённых версий

Версии компонентов меняются в `scripts/versions.env` (версия + sha256, проверка на стенде). На сервере:

```bash
cd /opt/vpn-zoo-src && sudo git pull
sudo zoo upgrade            # план: что устарело относительно versions.env и какие фазы пройти
sudo zoo upgrade --apply    # smoke до, фазы по плану, smoke после, подсказка отката
```

`--pull` делает `git pull --ff-only` сам, `--fetch` показывает отставание от upstream, `--phase ФАЗА` добавляет фазу в план. Простой `sudo bash scripts/install.sh` после `git pull` тоже работает: копия `zoo` в `/opt/vpn-zoo` обновится (фаза 09), итог и самопроверка напечатаются снова.

Geo-файлы маршрутизации обновляются отдельно: ежедневный таймер `vpn-zoo-geo-update.timer` с проверкой и откатом (D19).

## Обновление с первой версии

Склонируйте v2 в постоянный каталог (первая версия советовала `/tmp`) и запустите `sudo bash scripts/install.sh` поверх старой установки. State первой версии сохраняется рядом (`state.v1`) и сбрасывается, все фазы проходят заново и переносят то, что было:

- **Hysteria2:** старый пароль становится паролем `owner`, ссылка `hysteria2://…` продолжает работать. Сервер обновится до закреплённой версии; лучше перейти на новую ссылку с `pinSHA256`.
- **VLESS:** переезжает с 8443 на 443 и получает новый REALITY target вместо `www.cbr.ru`. Inbound первой версии переносится вместе с клиентами, но ссылку VLESS нужно заменить.
- **AmneziaWG:** ключи клиента и порт сохраняются, параметры обфускации становятся уникальными. Конфиг AWG тоже нужно заменить.
- **Панель 3x-ui:** уходит с открытого порта 2053 на `127.0.0.1`, доступ через `ssh -L`.
- Появляются XHTTP, SS-2022, инструмент `zoo`, анти-утечки и самопроверка.

## Тесты

Стенд в Docker: сервер — Ubuntu с systemd в контейнере, клиенты — отдельные контейнеры, между пробником и сервером — эмулятор ТСПУ. На Windows/Git Bash `docker`-команды руками запускайте с `MSYS_NO_PATHCONV=1`.

```bash
bash docker/test.sh --mode full --tests all                       # как у пользователя + все тесты
bash docker/test.sh --mode full --tests all --distro 22.04 \
    --env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1 --name t1 --keep   # с флагами, контейнер оставить
```

Прогон включает: самопроверку в конце `install.sh`, `zoo probe --local`, клиентский пробник напрямую и через цензор, трафик настоящими клиентами по каждому протоколу и по выданным ссылкам, анти-утечки, безопасность (сокеты, права, недоступность сервисов сервера из туннеля), админку через формы, коллектор трафика. Отчёт — `docker/out/<время>/summary.md`.

Матрица цензора (ожидаемые вердикты сверяются автоматически):

| Профиль | TCP с TLS (VLESS, XHTTP) | SS-2022 | UDP (Hy2, AWG, TUIC) |
|---|---|---|---|
| `clean` | OK | OK | OK |
| `drop-udp` | OK | OK | UDP_BLOCKED |
| `ip-block` | IP_BLOCKED | IP_BLOCKED | IP_BLOCKED |
| `freeze-16k` | FREEZE_16K | FREEZE_16K | OK |
| `rst-tls` | HANDSHAKE_FAIL | OK | OK |

Юнит-тесты `zoo`: `cd zoo && python3 -m unittest discover -s tests -t .`. Подробно: [docker/README.md](docker/README.md), [docker/probe/README.md](docker/probe/README.md), [docker/censor/README.md](docker/censor/README.md).

## Ограничения и что не проверено

- **Не проверено вживую:** боевой VPS с российским провайдером и настоящим ТСПУ (только эмулятор); модуль ядра AmneziaWG (DKMS) и цикл «HWE-ядро → reboot» — в Docker ядро чужое, проверен только `amneziawg-go`; arm64 (сервер и образ пробника на Apple Silicon); хостер без hairpin.
- **Клиентские приложения:** ссылки проверены разбором ядрами Xray и sing-box, как это делают клиенты, но не импортом в Happ, v2rayN/NG, Hiddify, NekoBox, AmneziaVPN, mihomo. Ключ `vpn://` AmneziaWG проверен по структуре. В итоге установки у AmneziaWG QR только для `.conf`; `zoo links --qr` и админка строят QR и для `vpn://`, но импорт по нему не проверялся.
- **Пробник у пользователя:** проверен на стенде и с Windows 11 (PowerShell 5.1 + Docker Desktop: `git clone`, `scp`, `docker build`, `docker run -v "${PWD}\probe:/data"`, вариант без входа root); Docker Desktop на macOS и нативный Docker на Linux-десктопе не прогонялись.
- **Не прогонялись режимы:** `XHTTP_PLACEMENT=fallback`, `AWG_PROFILE=v3`, смена `PANEL_*` после установки, `ENABLE_ZOO=0` после установки (выключения нет), `SUB_PUBLIC=1` (фазы подписки нет), кнопка обновления geo в админке.
- **Port hopping Hysteria** пробником не проверяется: hop-порты — DNAT на внешнем интерфейсе, с сервера они недостижимы; проверяется основной порт.
- **Известные компромиссы:** TUIC-ссылка с `allow_insecure=1` без пина сертификата (в клиентах легко перехватить; пин есть только у пробника); токен пользователя Hysteria кратко виден в списке процессов (D17); API Xray и мост TUIC на 127.0.0.1 без пароля (так устроен 3x-ui; из туннеля недоступны); geo-обновление проверяет целостность, но не пин; пакеты AWG из PPA не закреплены по версии; WARP не мониторится постоянно; исходящий порт 25 не закрыт.
- Трафик проб `zoo-probe` входит в итоги по протоколам.

## FAQ и неполадки

**Протокол «НЕ РАБОТАЕТ на самом сервере».** `sudo zoo status` (сервисы, порты, проблемы), `sudo zoo probe --local --proto <id>`, журналы: `journalctl -u x-ui`, `-u hysteria-server`, `-u awg-quick@awg0`. Повтор фазы: `sudo bash scripts/install.sh --phase <номер>`.

**VLESS «подключено», но трафика нет.** Клиент на sing-box или старое ядро Xray (см. [клиенты](#что-вы-получите)). Нужен клиент на Xray ≥26.x.

**Всё работает на сервере, у меня — нет.** Запустите [пробник](#блокирует-ли-ваш-провайдер-пробник-на-вашей-машине) из своей сети. `UDP_BLOCKED` — берите TCP-протоколы; `FREEZE_16K` — UDP-протоколы или хостер в другой сети; `HANDSHAKE_FAIL` на VLESS — SS-2022, Hysteria2 или другой SNI (`VLESS_SNI=… sudo -E bash scripts/install.sh`); `IP_BLOCKED` везде — нужен новый IP.

**Потерял ссылки.** `sudo zoo links owner --qr` или `sudo cat /root/CREDENTIALS.md`; заново напечатать итог: `sudo bash scripts/install.sh --phase 99`.

**Забыл токен админки.** `sudo zoo web --info`; новый — `sudo zoo web --new-token`.

**«ни одна фаза не запускалась».** Всё уже установлено. Перезапуск выполненных фаз: `--rerun`; одной фазы: `--phase`.

**«install.sh уже запущен».** Ждите окончания другого запуска (блокировка `/run/vpn-setup.lock`).

**После reboot ядро не сменилось, фаза 02 снова просит перезагрузку.** Ядро задаёт хостер: `AWG_NO_HWE=1 sudo -E bash scripts/install.sh`.

**Фаза остановилась на «чужой установке».** На сервере уже был 3x-ui, Hysteria или AmneziaWG не от этого инсталлера. `--force` сделает бэкап в `/var/backups/vpn-setup/` и перезапишет.

**`zoo status` возвращает 1.** Есть проблема из списка: упавший сервис, закрытый порт, устаревший или ошибающийся коллектор трафика, интерфейс AWG пропал при «активном» юните.

**AmneziaWG в пробнике `CLIENT_ERROR`.** Контейнер запущен без `--cap-add NET_ADMIN --device /dev/net/tun`.

**IP сервера заблокировали.** Новый IP или VPS: склонировать репозиторий и `install.sh` на новом сервере, раздать новые ссылки. Бэкап `/etc/vpn-setup/` и `x-ui.db` держите вне сервера ([RISK-REDUCTION](docs/RISK-REDUCTION.md) §6).

## Структура проекта

```
scripts/install.sh       оркестратор фаз: state, флаги, перезапуск по изменённым ключам, самопроверка
scripts/lib.sh           общие функции: лог, state, config, манифесты, firewall, бэкапы
scripts/lib/xui.sh       единственный адаптер к API 3x-ui (+ общие пользователи Xray-протоколов)
scripts/lib/proto-*.sh   пользователи, ссылки, probe и манифест каждого протокола
scripts/versions.env     закреплённые версии и sha256 всего, что скачивается
scripts/NN-*.sh          фазы 00…09, 99
zoo/                     инструмент zoo (Python 3 stdlib): CLI, веб-админка, пробник, коллектор
docker/                  тестовый стенд, тесты docker/tests/, пробник docker/probe/, цензор docker/censor/
docs/                    архитектура, решения, снижение рисков
research/                исследования по датам
```

Документы: [ARCHITECTURE](docs/ARCHITECTURE.md) — как устроено; [DECISIONS](docs/DECISIONS.md) — принятые решения и как их поменять; [RISK-REDUCTION](docs/RISK-REDUCTION.md) — детект VPN, утечки IP, MAX, инструкции для клиентов; [PROBE-SURFACE](docs/PROBE-SURFACE.md) — что видит сканер снаружи (порты, баннеры, неотличимость REALITY); [research/](research/README.md) — исследования.

## Лицензия

[MIT](LICENSE). Без гарантий. Пользоваться VPN в России законно, а рекламировать средства обхода блокировок — нет: не публикуйте ссылки и адрес сервера (RISK-REDUCTION §6).
