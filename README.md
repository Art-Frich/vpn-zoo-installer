# 🦓 vpn-zoo-installer

**Зоопарк VPN-протоколов на одном VPS — для команды в России.**
Одна команда ставит несколько протоколов, сама проверяет, какие работают, и даёт простую админку: кто подключён, сколько трафика, кто стучался в сервер.

![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white)
![Протоколы](https://img.shields.io/badge/протоколы-6%20вкл.%20%2B%201%20по%20флагу-2ea44f)
![Лицензия](https://img.shields.io/badge/лицензия-MIT-blue)

Два этапа: **[1. Зоопарк](#1-зоопарк)** — ставите один раз; **[2. Администрирование](#2-администрирование)** — каждый день, одной командой.

> [!NOTE]
> Проверено на реальном VPS (Ubuntu 24.04, Финляндия) пробником через домашнего российского провайдера: 6 из 7 протоколов работают. [Отчёт полевого теста →](research/2026-10-05/field-test-vps_05-10-26.md) · [что ещё не проверено →](docs/REFERENCE.md#ограничения-и-что-не-проверено)

---

## 1. Зоопарк

### 🚀 Установка

На чистой **Ubuntu 22.04/24.04**, под root или с `sudo`:

```bash
sudo apt-get update && sudo apt-get install -y git && sudo git clone https://github.com/Art-Frich/vpn-zoo-installer.git /opt/vpn-zoo-src && cd /opt/vpn-zoo-src && sudo bash scripts/install.sh
```

Время — в основном скачивание с GitHub: на Docker-стенде полная установка с проверкой всех протоколов заняла ≈12 минут, на реальном VPS 05.10.2026 — ≈30 минут из-за медленного в тот день GitHub (оборванная загрузка докачается при повторном запуске). В конце:
- ✅ таблица «работает в принципе» — сервер сам подключился каждым протоколом к себе;
- 🔑 ссылки и QR каждого протокола (копия — `/root/CREDENTIALS.md`);
- 🧭 готовые команды для следующих шагов, в том числе для этапа 2.

> [!TIP]
> Попросило перезагрузку (старое ядро на 22.04) — `sudo reboot` и та же команда ещё раз, продолжит с места. SSH ненадолго оборвался — это `sshd` перезапустился при обновлении: переподключитесь, установка идёт. Обрыв загрузки не страшен — повторный запуск докачает.

### 🧩 Протоколы

| | Протокол | Порт | Включён | Зачем |
|---|---|---|---|---|
| 🔵 | **VLESS + REALITY** | TCP 443 | ✅ | основной: выглядит как обычный HTTPS к чужому сайту |
| 🔷 | **VLESS + XHTTP** | TCP, свой | ✅ | запасной TCP, не падает вместе с 443 |
| 🟢 | **Hysteria2** | UDP 443 | ✅ | быстрый QUIC, хорош на плохих каналах |
| 🟩 | **Hysteria2 + Salamander** | UDP, свой | ✅ | QUIC, не похожий на QUIC, — если режут по сигнатуре |
| 🟡 | **TUIC v5** | UDP, свой | ✅ | ещё один QUIC, для клиентов на sing-box |
| 🟠 | **AmneziaWG** | UDP, свой | ✅ | обфусцированный WireGuard, полный туннель |
| ⚪ | Shadowsocks-2022 | TCP+UDP, свой | `ENABLE_SS=1` | запасной без TLS; в полевом тесте терял данные — выключен |
| ⚪ | Cloudflare WARP | исходящий | `ENABLE_WARP=1` | сервисы «узнай свой IP» видят адрес Cloudflare, а не сервера |

Режут один протокол — остаются другие.

> [!WARNING]
> Для VLESS нужен клиент на ядре **Xray** (v2rayN, v2rayNG, Happ, INCY). Клиентам на sing-box (Hiddify, NekoBox, Karing) подойдут Hysteria2, TUIC и AmneziaWG.

### 🔍 Блокирует ли мой провайдер?

«Работает на сервере» ≠ «работает у вас». Пробник в Docker проверяет все протоколы **через вашу сеть**; второй сервер не нужен.

```bash
git clone https://github.com/Art-Frich/vpn-zoo-installer.git && cd vpn-zoo-installer && mkdir probe
scp root@СЕРВЕР:/etc/vpn-setup/probe-export.json probe/
docker build -f docker/probe.Dockerfile -t zoo-probe .
docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe --tag home
```

> [!IMPORTANT]
> Пробник должен ходить к серверу **через вашего провайдера, а не через ваш VPN**. Выключать VPN нельзя — исключите из него только адрес сервера:
> - **Windows** (PowerShell от администратора): шлюз — `Get-NetRoute 0.0.0.0/0`, затем `route add СЕРВЕР mask 255.255.255.255 ШЛЮЗ`
> - **macOS:** `sudo route -n add -host СЕРВЕР ШЛЮЗ` · **Linux:** `sudo ip route add СЕРВЕР via ШЛЮЗ`
>
> Если трафик всё ещё идёт через VPN, пробник предупредит сам.

Итог по каждому протоколу: ✅ работает у вас · 🚫 блокируется (IP, порт, UDP, «заморозка» после 16 КБ…) · ⚠️ не работает на сервере. Проверяйте из разных сетей (дом, мобильный, кафе) и отправляйте отчёты на сервер — `scripts/history.sh push root@СЕРВЕР`. По истории `zoo probe --rank` покажет **топ протоколов под ваши условия**.

👉 [Windows/PowerShell, как читать вердикты, история и рейтинг](docs/REFERENCE.md#блокирует-ли-ваш-провайдер-пробник-на-вашей-машине)

### 🛡 Что защищено по умолчанию

- Панель и админка слушают только `127.0.0.1`; снаружи — только порты протоколов.
- Сервисы «узнай свой IP» из туннеля заблокированы или идут через WARP.
- Всё скачиваемое закреплено по версии и sha256, без `latest`.
- UFW, fail2ban, автообновления; `SSH_HARDEN=1` — SSH на новом порту и только по ключу, без риска запереться.

Подробно о рисках и детекте VPN — [docs/RISK-REDUCTION.md](docs/RISK-REDUCTION.md).

---

## 2. Администрирование

Админка снаружи не видна — на сервере она слушает только `127.0.0.1`. `zoo-admin` сам поднимает SSH-туннель, берёт одноразовую ссылку входа и открывает браузер: токен не нужен.

### Один раз на своём компьютере

Нужен SSH-доступ к серверу (пароль или ключ). Команду `root@СЕРВЕР` (и `-p`, если порт не 22) печатает установщик в конце.

**Windows (PowerShell):**

```powershell
iwr https://raw.githubusercontent.com/Art-Frich/vpn-zoo-installer/main/tools/zoo-admin.ps1 -OutFile zoo-admin.ps1
powershell -ExecutionPolicy Bypass -File .\zoo-admin.ps1 -Setup root@СЕРВЕР      # порт не 22: добавьте -Port 2222
```

**macOS / Linux / Git Bash:**

```bash
curl -fsSLO https://raw.githubusercontent.com/Art-Frich/vpn-zoo-installer/main/tools/zoo-admin.sh
bash zoo-admin.sh --setup root@СЕРВЕР                                            # порт не 22: добавьте -p 2222
```

Что произойдёт: создастся SSH-ключ (если его нет), он ляжет на сервер (пароль спросят один раз), в `~/.ssh/config` появится запись `zoo`, поставится команда `zoo-admin`. Повторный запуск безопасен. Другое имя вместо `zoo` — `--name` (Windows: `-Name`). Windows: команда появится в новом окне PowerShell; если профиль не грузится — `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. macOS/Linux: если `~/.local/bin` нет в `PATH`, скрипт подскажет, как добавить.

### Каждый день

```bash
zoo-admin     # админка в браузере, уже со входом
ssh zoo       # консоль сервера
```

Вход в админку запоминается до 30 дней (без заходов — на 14); потом снова `zoo-admin`. Туннель остаётся в фоне.

### Частые действия

| Что | Как |
|---|---|
| Первая настройка | админка → «Обзор» → **«Подключить людей»**: кто ставит приложения, готовый вариант, люди списком, карточки для раздачи |
| Добавить человека | админка → «Пользователи» · консоль: `sudo zoo user add anna --note "бухгалтер"` |
| Карточки, ссылки и QR | страница человека → «Подключение» · `sudo zoo links anna --qr` |
| Группы с общими настройками | админка → «Группы» · `zoo group` |
| Состояние и трафик | «Обзор», «Трафик» · `zoo status`, `zoo traffic` |
| Логи и кто стучался в сервер | «Логи», «Атаки» · `zoo journal` |
| Обновить | `sudo zoo upgrade --pull --apply` |

Людям отправляйте [docs/USER-GUIDE.md](docs/USER-GUIDE.md). Для Android проще всего QR **AmneziaWG**: через VPN идут только выбранные приложения, банки и Госуслуги — мимо. Все команды `zoo` — в [справочнике](docs/REFERENCE.md#админка-и-инструмент-zoo).

<details><summary>Без помощника, вручную</summary>

```bash
sudo zoo web --info                                            # на сервере: порт, команда туннеля, ссылка входа и токен
ssh -N -L ПОРТ:127.0.0.1:ПОРТ root@СЕРВЕР                      # у себя, держать открытым; потом http://127.0.0.1:ПОРТ/
ssh -t -L ПОРТ:127.0.0.1:ПОРТ root@СЕРВЕР sudo zoo web --link  # туннель и одноразовая ссылка (3 минуты) сразу
```

Новый токен (закрывает все сессии) — `sudo zoo web --new-token`.
</details>

---

## 📚 Документация

| Документ | Что там |
|---|---|
| [docs/REFERENCE.md](docs/REFERENCE.md) | **справочник**: все команды `zoo`, параметры, обновления, файлы на сервере, тесты, FAQ |
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | инструкция для пользователей по телефонам и компьютерам |
| [docs/RISK-REDUCTION.md](docs/RISK-REDUCTION.md) | детект VPN, утечки IP, MAX и банки |
| [docs/PROBE-SURFACE.md](docs/PROBE-SURFACE.md) | что видит сканер на каждом порту |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/DECISIONS.md](docs/DECISIONS.md) | как устроено и почему так |
| [history/](history/README.md) · [research/](research/README.md) | история проверок и исследования протоколов |
| [docker/README.md](docker/README.md) · [CLAUDE.md](CLAUDE.md) | тестовый стенд и работа с Claude Code |

---

MIT, без гарантий. Не публикуйте ссылки и подписки.
