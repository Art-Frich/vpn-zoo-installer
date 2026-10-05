# 🦓 vpn-zoo-installer

**Зоопарк VPN-протоколов на одном VPS — для себя и своих в России.**
Одна команда ставит сразу несколько протоколов, сама проверяет, какие из них работают, и даёт простую админку: кто подключён, сколько трафика, кто стучался в сервер.

![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white)
![Протоколы](https://img.shields.io/badge/протоколы-5%20%2B%202%20по%20флагу-2ea44f)
![Лицензия](https://img.shields.io/badge/лицензия-MIT-blue)

> [!NOTE]
> Всё проверено на стенде: настоящие клиенты, systemd в Docker, эмулятор блокировок ТСПУ. На боевом VPS через российского провайдера — ещё нет. [Что именно не проверено →](docs/REFERENCE.md#ограничения-и-что-не-проверено)

## 🧩 Что внутри

| | Протокол | Порт | Включён | Зачем |
|---|---|---|---|---|
| 🔵 | **VLESS + REALITY** | TCP 443 | ✅ | основной: выглядит как обычный HTTPS к чужому сайту |
| 🔷 | **VLESS + XHTTP** | TCP, свой | ✅ | запасной TCP, не падает вместе с 443 |
| 🟣 | **Shadowsocks-2022** | TCP+UDP, свой | ✅ | запасной без TLS |
| 🟢 | **Hysteria2** | UDP 443 | ✅ | быстрый QUIC, хорош на плохих каналах |
| 🟠 | **AmneziaWG** | UDP, свой | ✅ | обфусцированный WireGuard, полный туннель |
| ⚪ | Hysteria2 + Salamander | UDP, свой | `ENABLE_HY2_OBFS=1` | QUIC, не похожий на QUIC, — если режут по его сигнатуре |
| ⚪ | TUIC v5 | UDP, свой | `ENABLE_TUIC=1` | ещё один QUIC для клиентов на sing-box |
| ⚪ | Cloudflare WARP | исходящий | `ENABLE_WARP=1` | сервисы «узнай свой IP» видят адрес Cloudflare, а не сервера |

Если провайдер режет один протокол, остаются другие.

## 🚀 Установка

На чистой **Ubuntu 22.04/24.04** (root или `sudo`):

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/Art-Frich/vpn-zoo-installer.git /opt/vpn-zoo-src
cd /opt/vpn-zoo-src && sudo bash scripts/install.sh
```

Через несколько минут вы получите:
- ✅ **таблицу «работает в принципе»** — сервер сам подключился каждым протоколом к себе;
- 🔑 **ссылки и QR** для каждого протокола (копия — в `/root/CREDENTIALS.md`);
- 🧭 **готовые команды** для следующего шага — проверки с вашей машины.

> [!TIP]
> Если установка попросила перезагрузку (старое ядро на 22.04) — `sudo reboot` и та же команда ещё раз, продолжит с места остановки.

## 🔍 Блокирует ли мой провайдер?

«Работает на сервере» ≠ «работает у вас». Пробник в Docker проверяет все протоколы **через вашу сеть** и говорит, что режет провайдер. Второй сервер не нужен.

```bash
git clone https://github.com/Art-Frich/vpn-zoo-installer.git && cd vpn-zoo-installer && mkdir probe
scp root@СЕРВЕР:/etc/vpn-setup/probe-export.json probe/
docker build -f docker/probe.Dockerfile -t zoo-probe .
docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe --tag home
```

Итог — у каждого протокола: ✅ работает у вас · 🚫 блокируется (IP, порт, UDP, «заморозка» после 16 КБ…) · ⚠️ не работает на сервере. Проверяйте из разных сетей (дом, мобильный, кафе) и отправляйте отчёты на сервер — `scripts/history.sh push root@СЕРВЕР`. По накопленной истории `zoo probe --rank` покажет **топ протоколов под ваши условия**.

👉 [Windows/PowerShell, как читать вердикты, история и рейтинг](docs/REFERENCE.md#блокирует-ли-ваш-провайдер-пробник-на-вашей-машине)

## 🛠 Админка

Веб-админка слушает только `127.0.0.1` — заходите через SSH-туннель:

```bash
sudo zoo web --info                              # на сервере: команда туннеля и токен
ssh -N -L ПОРТ:127.0.0.1:ПОРТ root@СЕРВЕР        # у себя; потом http://127.0.0.1:ПОРТ/
```

В ней: протоколы и здоровье сервера, пользователи, трафик по людям и протоколам, результаты проверок, **журнал атак** (кто и чем стучался в сервер). Всё то же — из консоли: `zoo status`, `zoo traffic`, `zoo journal`.

## 👥 Раздать своим

```bash
sudo zoo user add masha --note "сестра"     # креды сразу во всех протоколах
sudo zoo links masha --qr                   # ссылки и QR
```

Для Android самый простой путь — **QR для AmneziaWG**: через VPN пойдут только нужные приложения (Brave, Telegram, YouTube…), а банки, Госуслуги и MAX — мимо, и адрес сервера им не виден. Инструкцию для людей, которые не разбираются в VPN, отправляйте [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

> [!WARNING]
> Для VLESS нужен клиент на ядре **Xray** (v2rayN, v2rayNG, Happ, INCY). Клиенты на sing-box — Hiddify, NekoBox, Karing — к VLESS-REALITY этого сервера не подключатся: им можно Hysteria2, Shadowsocks и AmneziaWG.

## 🛡 Что уже защищено

- Панель и админка — только на `127.0.0.1`, наружу ничего лишнего.
- Сервисы «узнай свой IP» из туннеля заблокированы или идут через WARP — приложения не узнают адрес сервера.
- Всё скачиваемое закреплено по версии и sha256, без `latest`.
- UFW, fail2ban, автообновления; по флагу `SSH_HARDEN=1` — SSH на новом порту и только по ключу, без риска запереться.

Подробно о рисках, детекте VPN и о том, как жить с MAX, — [docs/RISK-REDUCTION.md](docs/RISK-REDUCTION.md).

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

MIT, без гарантий. Для себя и своего круга — не публикуйте ссылки и подписки.
