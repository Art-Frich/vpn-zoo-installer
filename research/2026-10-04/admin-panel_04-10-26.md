# Админка для vpn-zoo-installer — дизайн-док (04.10.2026)

Для кого: владелец репо (выбирает путь) и ИИ-ассистент (реализует).
Статус: предложение, решение не принято. Всё, что помечено ❓, не подтверждено: это противоречие между источниками, оценка без замера или факт, который надо проверить на живом VPS.

Исходное состояние репо (по `scripts/`):
- `03-3xui.sh` скачивает tarball 3x-ui напрямую с GitHub, по умолчанию `XUI_VERSION=latest`. Версия **не закреплена**, SHA-256 **не проверяется**. Панель доступна по `http://IP:2053/<PANEL_PATH>` на 0.0.0.0.
- `01-firewall.sh` открывает `PANEL_PORT/tcp` в ufw.
- `04-vless-reality.sh` вставляет inbound в `/etc/x-ui/x-ui.db` через `sqlite3 <<SQL`. VLESS слушает 8443/tcp.
- `05-hysteria2.sh`: apernet `hysteria-server`, `auth: type: password` с одним общим `HY2_PASSWORD`, порт 443/udp.
- `06-amneziawg.sh`: модуль ядра, `awg-quick@awg0`, сеть `10.66.66.0/24`, порт 51822/udp, один `[Peer]`, H1..H4 заданы одиночными значениями (`H1 = 1700000001`).
- `99-print-creds.sh` печатает креды. Конфиг лежит в `/etc/vpn-setup/config.env`, state — в `/var/lib/vpn-setup/state`.

> ⚠ Срочно, независимо от выбора админки. При `XUI_VERSION=latest` сейчас ставится v3.9.0. По данным исследования, в v3.x поменялась схема БД (клиенты вынесены в отдельные таблицы, при старте идут миграции). Значит, прямой INSERT из `04-vless-reality.sh` на свежей установке, вероятно, уже ломается ❓ (на живом VPS не проверено). Плюс панель на plain HTTP на 0.0.0.0 передаёт приватный ключ Reality открытым текстом (предупреждение RPRX от 24.10.2024).

---

## 1. Цель и требования

### 1.1 Функциональные цели
| # | Цель | Критерий готовности |
|---|---|---|
| G1 | Видеть **все** протоколы и inbound'ы и их состояние | Один экран: VLESS+Reality, Hy2, AWG (и в будущем XHTTP, TUIC, AWG 3.x). У каждого видно, вкл/выкл, слушает ли порт, жив ли процесс или юнит |
| G2 | Управлять учётками | Один пользователь получает креды на все протоколы. Создать, отключить, включить, удалить, задать срок и квоту трафика. Ссылки, QR, AWG `.conf`, подписка |
| G3 | Трафик | По пользователю и по протоколу: live (онлайн, текущая скорость) и история по часам, дням, месяцам |
| G4 | Метрики сервера | CPU, RAM/swap, диск, сеть, load, uptime, здоровье сервисов (systemd-юниты, порты) |

### 1.2 Нефункциональные требования
- **Ресурсы.** VPS с 1–2 vCPU и 1–2 ГБ RAM, на нём же крутятся Xray, Hy2 и AWG. Админка с мониторингом добавляет не больше ~50 МБ RAM. Docker, Postgres и Redis не используем.
- **Безопасность.**
  - Админка не отвечает на публичном интерфейсе: ТСПУ сканирует все 65535 портов и копит «VPN-score», так что случайный порт ничего не скрывает.
  - Нет plain HTTP с секретами.
  - 2FA обязательна.
  - Секреты хранятся с правами 0600.
  - Версии закреплены, хэши проверяются.
  - Ни один пользователь VPN не должен достучаться до 127.0.0.1 сервера (блок `geoip:private` и loopback во всех data plane).
- **Специфика РФ.**
  - Tailscale из РФ отвечает 451, Cloudflare (Access/Tunnel) режется до 16 КБ на соединение с 09.06.2025. Для доступа к админке ни тот, ни другой не годятся.
  - Массовые баны IP у хостеров (04.08 и 21.09.2026) задели даже серверы, где был только AWG. Каждый лишний открытый порт повышает риск бана.
- **Поддержка.** Один мейнтейнер. Нужен минимум своего кода, совместимость с фазовым bash-инсталлером (state-файл, идемпотентность) и предсказуемые обновления.

---

## 2. TL;DR — рекомендация

**Свою админку не пишем. Делаем 3x-ui v3.9.0 единой панелью зоопарка** (вариант A, «ZOO-on-3x-ui»), с прививками из двух других вариантов. В 3x-ui v3.9.0 (03.10.2026) в одном Go-процессе уже есть:
- VLESS/Reality/XHTTP;
- Hysteria2 (inbound `hysteria` в Xray);
- AmneziaWG 3.1 (userspace amneziawg-go/gVisor, с v3.7.0);
- TUIC v5 (нативно);
- WireGuard.

Там же один клиент на несколько inbound'ов, квоты и сроки, лимиты IP/HWID, подписки raw/JSON/Clash, TOTP, scoped API-токены и Overview с историей метрик. Все три судьи поставили этот вариант на первое место: 48, 45 и 48 баллов из 60, всего 141. Сайдкар zoo-admin набрал 122, своя zoopanel — 113. На G1–G4 из коробки не хватает только временного ряда трафика по пользователю и видимости движков вне панели. Это закрываем ~150–250 строками bash.

Что делаем:
1. **Срочно.** Закрепить `XUI_VERSION=v3.9.0` и **захардкодить SHA-256** архива в инсталлере (файлу `.sha256` из того же релиза не доверяем). Добавить `x-ui setting -listenIP 127.0.0.1`, убрать `PANEL_PORT` из ufw, доступ к панели только через `ssh -L`, включить 2FA.
2. Переписать `04-vless-reality.sh` с INSERT в `x-ui.db` на **REST API 3x-ui** с Bearer-токеном (`inbounds/add`, `clients/add`, `attach`). Перед каждым `zoo upgrade` гонять контрактный smoke-тест API.
3. Добавить переключатели движков `HY2_ENGINE=xray|apernet` и `AWG_ENGINE=panel|kernel`. **Переключение по умолчанию на `xray`/`panel` делаем только после гейта:** бенчмарк на 1 vCPU (AWG userspace против ядра, Hy2-Xray против apernet) и проверка H1–H4 (#6557) на реальных клиентах (AmneziaVPN, Keenetic). Если гейт провален, остаются `kernel`/`apernet`.
4. В routing Xray первым правилом поставить `geoip:private → blocked`. Для fallback-движков отдельно: ACL Hy2 `reject(127.0.0.0/8, 10.0.0.0/8)` и `nft`/`iptables`: `-i awg0` → DROP на локальные порты.
5. **Коллектор трафика** (systemd timer раз в 5 мин): `clients/list` и `inbounds/list` → дельты с детекцией сброса → SQLite со свёрткой 5 мин/48 ч, 1 ч/60 дн, 1 д/2 года. Это закрывает G3 по истории.
6. **`zoo status`**: `systemctl is-active` и `NRestarts` юнитов, `ss -ltnup` против ожидаемого списка портов, срок сертификатов, `vnstat --json m`. Это закрывает G1/G4 для того, чего панель не видит.
7. vnStat обязателен (месячный трафик VPS). Beszel ≥0.19 — опционально, по флагу.
8. Оценка: **7–11 человеко-дней** (5–8 по дизайну A плюс 2–3 на прививки). Своих демонов не появляется, только timer и bash.

---

## 3. Обзор существующих решений

| Решение | Протоколы | Юзеры (1 → все протоколы) | Трафик per-user | Метрики сервера | Ресурсы | Активность (на 10.2026) | Лицензия |
|---|---|---|---|---|---|---|---|
| **3x-ui (MHSanaei)** | VLESS/Reality/XHTTP, VMess, Trojan, SS, WG, **AWG 3.1 (userspace)**, **Hy2 (Xray)**, **TUIC v5**, MTProto | Да: email + attach к нескольким inbound'ам, квоты, сроки, IP/HWID, подписки | Накопительно по клиенту и inbound, онлайн, IP. Временного ряда нет | CPU/RAM/swap/диск/IO/сеть/TCP-UDP/load/uptime, история (7 дн ❓ / ~6 ч ❓) | Go + SQLite, без Docker | v3.9.0 от 03.10.2026, релиз раз в 1–3 нед, 47.4k★ | GPL-3.0 |
| LucX-UI (форк 3x-ui) | Всё из 3x-ui + AWG на модуле ядра, импорт существующего awg0, сайдкары (Naive, mieru, AnyTLS…) | Как 3x-ui + подписки `vpn://`/Happ | Как 3x-ui | Как 3x-ui | Как 3x-ui | v3.9.0-lucx.280, создан 05.2026, 226★, bus factor 1 | GPL-3.0 / PolyForm-NC ❓ |
| Remnawave | Только Xray (VLESS/Trojan/SS, Hy2 с 2.7.0). AWG/WG/TUIC нет | Да, сильный движок подписок, HWID | Да, по нодам, Prometheus | Да (load нод) | Docker + Postgres + Redis, ≥2 ГБ/2 ядра | 3.4.4 от 12.09.2026, 5.2k★, critical RCE-advisory 08.2026 | AGPL-3.0 |
| PasarGuard | Xray (VLESS/VMess/Trojan/SS/Hy2), WG. AWG только в PR #932, TUIC нет | Да, HWID | Да | Да | Docker, panel + node + NATS | v5.4.1 от 12.09.2026, 2.65k★ | AGPL-3.0 |
| s-ui (sing-box) | VLESS/Reality, Hy2, TUIC, WG, Naive, ShadowTLS, AnyTLS… XHTTP и AWG нет | Да | Да, live-сессии + kick | Да | Один бинарь + SQLite | v1.6.3 от 16.09.2026, 10k★ | GPL-3.0 |
| Hiddify-Manager | 20+ (Xray + sing-box, Hy2, TUIC, WG). AWG нет | Да | Да | Базовые | Тяжёлый, забирает 80/443 и весь сервер | v13.0.3, 569 открытых issues | GPL-3.0 |
| h-ui | Только Hy2 (apernet) | Да (только Hy2) | Да | Да | ≥256 МБ, один бинарь | v0.0.25 от 07.2026 | GPL-3.0 |
| Blitz | Только Hy2 | — | — | — | Python | **Архивирован** в 07.2026 | GPL-3.0 |
| Amnezia-Web-Panel | AWG 2.0/3.1, WG, Xray Reality, MTProxy. Hy2/TUIC нет | Да | По пирам | — | Docker в раскладке Amnezia, root по SSH | v1.7.3 от 02.10.2026, 476★ | GPL-3.0 |
| wg-easy v15 | WG, AWG (экспериментально; 3.1 только в master) | Только WG/AWG | Live Tx/Rx, Prometheus | Нет | Docker/Node | v15.4.0 от 14.08.2026, 27k★, CVE-2026-72603 (9.9) | AGPL-3.0 |
| WGDashboard | WG, AWG 2.0 (3.x нет, #1345) | Только WG/AWG | История в SQLite | Да | Python | v4.3.3 от 04.2026, CVE-2026-44343 (critical) | Apache-2.0 |
| Marzban / Marzneshin / x-panel | Xray | Да | Да | — | — | Marzban стоит с 01.2025, Marzneshin заброшен, x-panel платный | AGPL / — |

**Выводы**
- Только 3x-ui v3.9.0 закрывает весь зоопарк (VLESS/XHTTP + Hy2 + AWG + TUIC) в одном процессе без Docker. Остальные решения либо без AWG (Remnawave, PasarGuard, s-ui, Hiddify), либо однопротокольные (h-ui, wg-easy, WGDashboard), либо тяжёлые (Remnawave ≥2 ГБ), либо мёртвые (Blitz, Marzban).
- Цена 3x-ui: Hy2 работает на реализации Xray, а не apernet (нет Gecko и mimic, паритет Brutal/bandwidth не подтверждён ❓). AWG работает в userspace (дороже по CPU на 1 vCPU ❓, без замера). Частые advisory: 03.10.2026 вышло пять сразу, все закрыты в v3.9.0, кроме GHSA-rr44 (node-sync, патча нет).
- Запасные варианты: LucX-UI, если встроенный AWG окажется слабым (умеет AWG на ядре и импорт awg0, но лицензия и bus factor). PasarGuard, если примут PR #932.

---

## 4. Что умеют data planes

### 4.1 Xray-core: StatsService и HandlerService (gRPC)
Конфиг:
```json
{
  "api":   { "tag": "api", "listen": "127.0.0.1:8080",
             "services": ["HandlerService","StatsService","RoutingService","LoggerService"] },
  "stats": {},
  "policy": {
    "levels": { "0": { "statsUserUplink": true, "statsUserDownlink": true, "statsUserOnline": true } },
    "system": { "statsInboundUplink": true, "statsInboundDownlink": true }
  }
}
```
- Счётчики называются `user>>>EMAIL>>>traffic>>>uplink|downlink` и `inbound>>>TAG>>>traffic>>>uplink`.
- RPC: `GetStats`, `QueryStats(pattern, reset)`, `GetStatsOnline`, `GetStatsOnlineIpList`, `GetAllOnlineUsers`, `GetUsersStats(include_traffic, reset)` (одним вызовом отдаёт email, IP с last_seen и трафик), `GetSysStats`.
- Управление: `AlterInbound(tag, AddUserOperation | RemoveUserOperation{email})`, `ListInbounds`, `GetInboundUsers`. CLI: `xray api adu --server=127.0.0.1:8080 c1.json`, `xray api rmu`, `xray api statsonlineiplist -all -include-traffic`. `adu` поддерживает VMess, VLESS, Trojan, SS, SS2022, MASQUE и Hysteria.
- Ограничения:
  - gRPC работает **без аутентификации**, слушать только loopback.
  - Флага disable нет: отключить пользователя значит удалить его.
  - Изменения на лету не сохраняются в конфиг.
  - **Пока Xray принадлежит 3x-ui, в gRPC напрямую не писать и не читать с `reset=true`.** 3x-ui перезапишет пользователей из своей БД, а сброс счётчиков создаст гонку.

### 4.2 Inbound `hysteria` в Xray (Hy2 внутри Xray)
```json
{ "protocol": "hysteria",
  "settings": { "version": 2, "users": [ { "auth": "<secret>", "level": 0, "email": "u42" } ] } }
```
- Транспорт hysteria, port range, Salamander через finalmask. Gecko не нашли ❓.
- Есть с Xray v26.3.27. Управление пользователями через API добавлено в PR #6847.
- В 3x-ui правка клиентов Hy2 без пересоздания inbound появилась в PR #6606 (слит 26.09.2026). До него каждая правка подвешивала все QUIC-сессии примерно на 30 с.

### 4.3 3x-ui REST API (v3.9.0)
- Аутентификация: заголовок `Authorization: Bearer <token>`. Токен создаётся в Settings → Security, у него есть scope и срок (с v3.7.0). С v3.8.0 неверный токен даёт 401. Получить из CLI: `x-ui setting -getApiToken` ❓ (упомянуто в одном источнике).
- Ответ всегда в обёртке `{success, msg, obj}`. OpenAPI: `/panel/api/openapi.json` (только после входа).

| Группа | Эндпоинты |
|---|---|
| Клиенты | `/panel/api/clients/list`, `list/paged`, `get/{email}`, `add`, `update/{email}`, `del/{email}`, `{email}/attach`, `{email}/detach`, `bulkCreate/Enable/Disable/Adjust`, `resetTraffic/{email}`, `updateTraffic/{email}`, `delDepleted`, `onlines`, `ips/{email}`, `hwids/{email}`, `traffic/{email}`, `links/{email}`, `subLinks`, `groups/*` |
| Inbound'ы | `/panel/api/inbounds/list`, `add`, `update/{id}`, `setEnable/{id}`, `allLinks`, `resetTraffic`, `pushClientTraffics` |
| Сервер | `/panel/api/server/status` (CPU, RAM/swap, диск, сеть, uptime, Xray, соединения; обновляется раз в 2 с), `/history/{metric}/{bucket}`, `/cpuHistory/{bucket}`, `/xrayMetricsHistory`, а также start/stop/restart Xray, генерация ключей X25519/ML-KEM/ECH, бэкап |

Изменение, ломающее совместимость: в v3.9.0 `inbounds/update` **больше не трогает клиентов**, для них есть отдельные эндпоинты. Тело `clients/add` и `attach` надо брать из `openapi.json` закреплённой версии ❓.

Расхождение источников по глубине истории `/history`: «~6 ч, шаг 2/30/60/120/180/300 с» против «7 дней, уровни 2 с/1 ч, 1 мин/48 ч, 10 мин/7 дн в `system_metrics.gob`, ~1,5 МБ» ❓. Проверить на v3.9.0.

Пример (только через `ssh -L` или loopback):
```bash
curl -s -H "Authorization: Bearer $XUI_TOKEN" \
  "http://127.0.0.1:$PANEL_PORT/$PANEL_PATH/panel/api/clients/list" | jq '.obj'
```

### 4.4 Hysteria2 (apernet): Traffic Stats API и http-auth
```yaml
auth:
  type: http
  http: { url: http://127.0.0.1:8089/hy2/auth, insecure: false }
trafficStats:
  listen: 127.0.0.1:9999
  secret: ${HY2_STATS_SECRET}
acl:
  inline:
    - reject(127.0.0.0/8)
    - reject(10.0.0.0/8)
```
- http-auth: на каждое подключение сервер шлёт `POST {"addr":"1.2.3.4:5","auth":"<строка>","tx":N}`, ответ `{"ok":true,"id":"u42"}`. Есть варианты `command` (печатает id и выходит с кодом 0) и `userpass` (статический словарь, меняется только рестартом). Если бэкенд недоступен, подключения отклоняются (fail-closed).
- Stats API (заголовок `Authorization: <secret>` без Bearer):
  - `GET /traffic` → `{"u42":{"tx":N,"rx":N}}`, с `?clear=1` счётчики обнуляются после чтения;
  - `GET /online` → `{"u42":2}`;
  - `POST /kick` с телом `["u42"]`;
  - `GET /dump/streams`.
- Счётчики хранятся в памяти и пропадают при рестарте.
- **При нынешнем `type: password` id у всех один, поэтому разбивки по пользователям нет.** `kick` без блокировки в auth бесполезен: клиент переподключится.
- Версия: app/v2.12.3 от 16.09.2026 (Gecko, mimic, Chrome QUIC fingerprint).

### 4.5 AmneziaWG (ядро): `awg show dump` и `awg set`
У AWG нет API-демона, только CLI и netlink, и нужен root (CAP_NET_ADMIN).
```bash
awg show awg0 dump
# строка 1 — интерфейс (в 3.x ~30 полей: ключи, порт, Jc..H1-H4, I1-I5, HPK, тайминги, fwmark)
# строки пиров (8 полей; с `all` — 9):
# pubkey  psk  endpoint  allowed-ips  latest-handshake(unix)  rx  tx  keepalive

awg set awg0 peer "$PUB" allowed-ips 10.66.66.7/32 preshared-key /run/psk   # добавить
awg set awg0 peer "$PUB" remove                                             # отключить
awg syncconf awg0 <(awg-quick strip awg0)   # применить diff конфига, остальные сессии не рвутся
```
- «Онлайн» считается эвристикой: `latest-handshake` не старше ~180 с, с запасом. В 3.x тайминги rekey могут быть случайными, порог 300 с.
- rx/tx сбрасываются при рестарте интерфейса или удалении пира, поэтому храним дельты с детекцией сброса.
- Смена S1–S4, H1–H4 или HPK требует рестарта и перевыдачи конфигов всем клиентам.
- Квоту лучше применять через `nft quota` на /32 пира (идея из W-UI), а не опросом.
- Версии: amneziawg-tools v3.1.20260812, kernel module v3.1.20260906.

### 4.6 AmneziaWG внутри 3x-ui (v3.7.0+)
- amneziawg-go на gVisor внутри процесса панели. Пакеты идут через per-peer SOCKS5 в Xray, поэтому routing, статистика и лимиты Xray действуют и для AWG.
- Поля 3.1: Jc/Jmin/Jmax, S1–S4, H1–H4, I1–I5, HeaderProtectionKey, ContentPaddingAddition, тайминги, RandomTrailers, DisableCookies.
- Если поля 3.1 оставить пустыми, конфиг совместим со старыми клиентами. Но совместимость именно с нашими клиентами AWG 2.0 не подтверждена ❓, а запрос на AWG 2.0 (#5113) закрыт как not planned.
- Открытые проблемы:
  - **H1–H4 генерируются одиночными значениями (#6557)**: ломаются Keenetic и часть клиентов 2.0. Наш `06-amneziawg.sh` тоже ставит одиночные H, так что для наших текущих клиентов это, возможно, не регресс ❓.
  - Opt-in ядерный datapath отклонён (#6419).
  - Баг со скоростью ~20 Мбит/с в v3.7.0 (#6423) исправлен.

### 4.7 sing-box — для справки
`v2ray_api` даёт только статистику и требует своей сборки с `with_v2ray_api`. Live-управление пользователями есть только у SS (`ssm-api`). На роль объединяющего бэкенда не годится.

---

## 5. Три варианта архитектуры

**A. ZOO-on-3x-ui.** Единственный «мозг» — 3x-ui v3.9.0, версия закреплена.
- Hy2 переезжает в inbound `hysteria` Xray, AWG — во встроенный AWG 3.1. TUIC и XHTTP подключаются флагами.
- Один клиент (email) прикреплён ко всем inbound'ам.
- Плюс vnStat, опционально Beszel.
- Ядерный awg0 и apernet-hy2 остаются fallback-движками за флагами, но панель их не видит.
- Своего кода около 300–500 строк bash, 5–8 человеко-дней.

**B. zoo-admin (Go-сайдкар).** Xray остаётся за 3x-ui, Hy2 (apernet) и AWG (ядро) — нативные. Рядом ставится бинарник `zoo-admin`, он единственный источник правды о пользователях.
- Три адаптера: xui (REST `/clients`), hy2 (http-auth + trafficStats), awg (root-хелпер через sudoers).
- Коллектор каждые 30 с, энфорсер срока и квоты, reconcile каждые 5 мин.
- templ + htmx + SSE, Argon2id + TOTP, аудит.
- 3–5k LOC, 3–4 недели, RAM ~15–25 МБ.

**C. zoopanel (своя панель целиком).** 3x-ui удаляется.
- Свой Go-бинарник рендерит конфиги standalone Xray (gRPC Alter/Stats), apernet Hy2 (http-auth) и ядерного AWG (helper через unix-сокет с SO_PEERCRED).
- Своя телеметрия на gopsutil.
- 6–9k LOC, MVP за 5–7 недель, плюс ежемесячно догонять proto Xray.

### Оценки судей (0–10 по каждому критерию, сумма из 60)
| Вариант | Судья | Цели | Безоп. | Ресурсы | Поддержка | Трудозатр. | Расшир. | **Итого** |
|---|---|---|---|---|---|---|---|---|
| A ZOO-on-3x-ui | 1 | 8 | 7 | 9 | 7 | 9 | 8 | **48** |
| | 2 | 7 | 6 | 9 | 7 | 9 | 7 | **45** |
| | 3 | 8 | 6 | 9 | 7 | 9 | 9 | **48** |
| B zoo-admin | 1 | 9 | 7 | 7 | 5 | 4 | 7 | 39 |
| | 2 | 9 | 7 | 7 | 6 | 6 | 8 | 43 |
| | 3 | 9 | 7 | 7 | 5 | 5 | 7 | 40 |
| C zoopanel | 1 | 9 | 7 | 8 | 4 | 2 | 7 | 37 |
| | 2 | 9 | 8 | 8 | 4 | 3 | 7 | 39 |
| | 3 | 9 | 7 | 8 | 4 | 3 | 6 | 37 |
| **Сумма** | | | | | | | | **A 141 · B 122 · C 113** |

Все три судьи выбрали победителем A.

### Критика (сводно)
- **A.**
  - Нет временного ряда трафика по пользователю: суточного снимка мало.
  - При fallback-движках цель G1 нарушается, потому что панель их не видит.
  - AWG userspace на 1 vCPU не замерен. H1–H4 (#6557). Hy2 без Gecko.
  - Всё в одной корзине: процесс от root плюс поток advisory, GHSA-rr44 без патча.
  - `.sha256` из того же релиза не защищает от подмены релиза. «install.sh сверяет .sha256» в релиз-нотах не подтверждено.
  - Зато своего кода минимум, а loopback + `ssh -L` + 2FA сделаны правильно.
- **B.**
  - Лучшее покрытие целей: нативные движки, история 5 мин/1 ч/1 д.
  - Но два источника правды (`zoo.db` и `x-ui.db`): reconcile перезаписывает ручные правки в UI 3x-ui.
  - Поверхность атаки удваивается: 3x-ui от root, свой веб и sudo-хелпер.
  - Hy2 http-auth работает fail-closed: падение сайдкара означает DoS для Hy2.
  - Срок 3–4 недели оптимистичен. Сайдкар ломается при каждом изменении API 3x-ui и заново пишет то, что 3x-ui уже умеет.
- **C.**
  - Самая чистая модель и потенциально лучшая безопасность: нет root-панели, helper с allowlist.
  - Но для одного мейнтейнера неподъёмно: bus factor 1, ежемесячный дрейф proto Xray, свои непроаудированные баги.
  - Теряются HWID, боты, ноды, подписки Clash/AWG. TUIC через sing-box обойдётся ещё в 1–2 недели.
  - Закреплённая v26.3.27 уже отстаёт от v26.9.x.
  - Переинжиниринг для одного VPS.

---

## 6. Рекомендуемая архитектура: A + прививки

### 6.1 Компоненты
| Компонент | Что | Где |
|---|---|---|
| `x-ui` (3x-ui v3.9.0, закреплён) | Панель + Xray v26.9.x + amneziawg-go + TUIC. Единственный хозяин пользователей | systemd `x-ui`, UI на `127.0.0.1:$PANEL_PORT/$PANEL_PATH`, БД `/etc/x-ui/x-ui.db` |
| `vpn-zoo-collector` (новый, bash + jq + sqlite3) | Раз в 5 мин читает `clients/list` и `inbounds/list` и пишет дельты и свёртки | systemd timer, `/var/lib/vpn-zoo/traffic.sqlite` |
| `zoo` (новый CLI, bash) | `zoo status`, `zoo traffic [user] [period]`, `zoo upgrade <tag>`, `zoo smoke` | `/usr/local/bin/zoo` |
| vnStat 2.13 | История трафика eth0 (и awg0 при `AWG_ENGINE=kernel`) по дням и месяцам | apt, `/var/lib/vnstat/vnstat.db` |
| Beszel ≥0.19 (опционально, `ENABLE_BESZEL=1`) | Алерты по CPU и диску, статусы systemd | агент на `127.0.0.1:45876`, хаб на `127.0.0.1:8090` |
| fallback `hysteria-server` / `awg-quick@awg0` | Только при `HY2_ENGINE=apernet` / `AWG_ENGINE=kernel` | как сейчас, плюс ACL и nft |

### 6.2 Схема
```
                Интернет
                   │
   8443/tcp VLESS+Reality+Vision   443/udp Hy2 (+hop 20000-40000 ❓)   51822/udp AWG   [TUIC/XHTTP по флагу]
                   │
          ┌────────▼─────────────────────────────────────────────┐
          │ x-ui (Go, root, v3.9.0 закреплён)                    │
          │  ├ Xray-core: VLESS, XHTTP, hysteria(v2), routing     │
          │  │    правило №1: geoip:private → blocked             │
          │  ├ amneziawg-go/gVisor → per-peer SOCKS → Xray        │
          │  ├ TUIC v5 (Go)                                       │
          │  ├ REST API + UI  127.0.0.1:$PANEL_PORT/$PANEL_PATH   │
          │  └ SQLite /etc/x-ui/x-ui.db, system_metrics.gob       │
          └───────▲───────────────────────▲──────────────────────┘
                  │ Bearer (scope: clients,inbounds,server:read)
     ┌────────────┴─────────┐   ┌─────────┴──────────┐   ┌──────────────┐
     │ vpn-zoo-collector    │   │ zoo status / smoke │   │ vnStat       │
     │ timer 5 мин → SQLite │   │ systemctl, ss, cert│   │ eth0, awg0   │
     └──────────────────────┘   └────────────────────┘   └──────────────┘
                  ▲
  админ: ssh -L 8443:127.0.0.1:$PANEL_PORT root@VPS  → https?/http://127.0.0.1:8443/$PANEL_PATH
  запасной путь: mgmt-пир AWG (только к порту панели)
```

### 6.3 Модель пользователя
- 1 пользователь = 1 клиент 3x-ui.
  - `email = u<id>`: без персональных данных, они не попадают в счётчики и логи. Имя человека пишем в comment клиента.
  - `subId` берём из CSPRNG, ≥128 бит.
- Создание: `clients/add`, затем `{email}/attach` к каждому включённому inbound'у.

| Протокол | Креды клиента | Выдача |
|---|---|---|
| VLESS+Reality+Vision | uuid, `flow=xtls-rprx-vision` | `vless://…` + QR |
| XHTTP (флаг) | тот же uuid | `vless://…` |
| Hy2 (Xray) | auth-строка | `hysteria2://…` |
| AWG (panel) | своя пара ключей + /32 из пула панели | `.conf` + QR (+ AWG в Clash-подписке) |
| TUIC (флаг) | uuid + пароль | `tuic://…` |

- Квота, срок, автопродление, лимиты IP и HWID ставятся один раз на клиента. Отключение действует на всех протоколах сразу.
- Подписка (raw/JSON/Clash): sub-сервер по умолчанию слушает только 127.0.0.1. Ссылки и QR админ пересылает вручную. `SUB_PUBLIC=1` включает подписку на домене с TLS (acme) и случайным путём, но это открытый порт под сканером ТСПУ.
- Миграция:
  - старый общий `HY2_PASSWORD` становится клиентом `legacy-hy2` (у Hy2 есть только поле auth, поэтому старые ссылки должны продолжить работать ❓);
  - текущий AWG-пир перевыпускается (новый `.conf`), потому что пул и ключи теперь у панели;
  - текущий UUID VLESS переносится в клиента `u1`/owner.

### 6.4 Сбор трафика
- **Live:** UI 3x-ui (онлайн, lastOnline, IP, накопленные up/down) и `clients/onlines`.
- **История:** `vpn-zoo-collector`. Схема:
```sql
CREATE TABLE snap   (email TEXT, inbound TEXT, up INT, down INT, ts INT, PRIMARY KEY(email,inbound));  -- последнее значение
CREATE TABLE t5m    (email TEXT, proto TEXT, ts INT, up INT, down INT, PRIMARY KEY(email,proto,ts));  -- 48 ч
CREATE TABLE t1h    (… та же схема …);   -- 60 дней
CREATE TABLE t1d    (… та же схема …);   -- 2 года
```
- Алгоритм на каждом тике:
  1. `GET clients/list` (по клиенту) и `GET inbounds/list` (по inbound, то есть по протоколу).
  2. `delta = new >= old ? new - old : new` (детекция сброса: рестарт или `resetTraffic`).
  3. INSERT в `t5m`, свёртка `t5m → t1h → t1d`, чистка по ретеншену.
- Счётчики 3x-ui **не сбрасываем**, только читаем. gRPC Xray не трогаем.
- Ёмкость: 50 пользователей × 3 протокола × 288 точек в сутки ≈ 43k строк в сутки до свёртки, это мелочь.
- Нюанс: разбивку «пользователь × протокол» `clients/list` может не отдавать (только итог по клиенту) ❓. Если не отдаёт, то в MVP храним по пользователю и отдельно по inbound.
- Просмотр: `zoo traffic u42 30d` (таблица в терминале). Графики — фаза 2, либо Beszel или простой статический HTML, который генерирует тот же timer.
- При fallback-движках коллектор дополнительно читает:
  - Hy2 `GET /traffic?clear=1`. Даёт per-user только при `userpass`/`http` auth. При `password` весь Hy2 считается одной строкой.
  - `awg show awg0 dump`: по пирам, маппинг pubkey→user лежит в `config.env`/SQLite.

### 6.5 Метрики и здоровье
- Из 3x-ui: CPU, RAM, swap, диск, diskIO, сеть, pkt, TCP/UDP, load1/5/15, uptime, статус Xray (`/server/status`, `/history/*`).
- `zoo status` (bash, без демона):
  - `systemctl is-active` и `NRestarts` для `x-ui`, `ssh`, `fail2ban`, `vnstat` и, при fallback-движках, `hysteria-server` и `awg-quick@awg0`;
  - `ss -ltnup` сверяется с ожидаемым списком портов: ловит случайно открытые 2053, 2096 (sub) ❓, 45876 и 8090 на 0.0.0.0;
  - срок сертификатов (если включён TLS или SUB_PUBLIC);
  - `vnstat --json m`: трафик VPS за месяц против лимита хостера;
  - при `AWG_ENGINE=kernel` возраст handshake пиров;
  - код возврата ≠0 при деградации, чтобы это можно было использовать из cron или Beszel.
- Алерты: Telegram-бот 3x-ui (квоты, сроки, логины) и/или Beszel. Внешний Uptime Kuma имеет смысл только на другом хосте.

### 6.6 Безопасность и доступ к панели
1. **Сеть.**
   - `x-ui setting -listenIP 127.0.0.1`, `PANEL_PORT` удаляется из ufw (`01-firewall.sh`).
   - Доступ: `ssh -L 8443:127.0.0.1:$PANEL_PORT root@IP`, команду печатает `99-print-creds.sh`.
   - Запасной путь: отдельный mgmt-пир AWG. Ему разрешён только порт панели, остальным пирам доступ к host-портам закрыт.
2. **Изоляция data plane.**
   - Xray: `geoip:private → blocked` первым правилом. Закрывает VLESS, Hy2 и AWG внутри панели.
   - При fallback-движках: Hy2 ACL `reject(127.0.0.0/8)`, `reject(10.0.0.0/8)`; `iptables/nft INPUT -i awg0 → DROP`, кроме DNS (и mgmt-пира).
3. **Поставка.**
   - `XUI_VERSION=v3.9.0` и `XUI_SHA256_amd64`/`_arm64`, захардкоженные в `lib.sh`. Проверка `sha256sum -c` до распаковки. Файлу `.sha256` из релиза не доверяем.
   - `zoo upgrade <tag>` требует новый хэш и проходит `zoo smoke`.
4. **Аутентификация.**
   - Включить TOTP 2FA (`x-ui setting` или через UI ❓).
   - Логин, пароль, `webBasePath` и API-токен со scope `clients, inbounds, server:read` и сроком хранятся в `/etc/vpn-setup/config.env` (0600 root).
5. **Функции не используем.** Мультинода выключена (GHSA-rr44 без патча). Импорт БД и правка xray-шаблона из UI запрещены регламентом: это классы root-RCE 2026 года (CVE-2026-55477, GHSA-h4x8, GHSA-fqw4).
6. **systemd-hardening для `x-ui`.** `PrivateTmp=yes`, `ProtectHome=yes`, `ReadWritePaths=/etc/x-ui /usr/local/x-ui` (через drop-in, проверить, что Xray, AWG/gVisor и логи не ломаются ❓).
7. **Аудит.** В 3x-ui своего append-only аудит-лога нет ❓. Минимум: journald для `sshd` (кто открывал туннель) и копия логов x-ui в append-only файл (`chattr +a`).
8. **Бэкап.** Ежедневно `sqlite3 /etc/x-ui/x-ui.db ".backup /var/backups/x-ui-$(date +%F).db"` с правами 0600 и ротацией 14 дней.

### 6.7 Интеграция в инсталлер
Номера фаз привязаны к текущему `scripts/`.

| Фаза | Изменение |
|---|---|
| `lib.sh` / `00-bootstrap.sh` | Новые ключи: `HY2_ENGINE=apernet\|xray`, `AWG_ENGINE=kernel\|panel` (оба по умолчанию на fallback **до прохождения гейта**), `ENABLE_TUIC=0`, `ENABLE_XHTTP=0`, `SUB_PUBLIC=0`, `ENABLE_BESZEL=0`, `XUI_VERSION=v3.9.0`, `XUI_SHA256_*`, `XUI_TOKEN`, `HY2_STATS_SECRET` |
| `01-firewall.sh` | Убрать `ufw allow $PANEL_PORT`. Открывать порты только включённых протоколов. При `AWG_ENGINE=kernel` правило `-i awg0` DROP на host-порты |
| `03-3xui.sh` | Закреплённый тег и проверка SHA-256, `-listenIP 127.0.0.1`, получение API-токена → `config.env`, включение 2FA ❓, drop-in hardening |
| `04-vless-reality.sh` → **`04-xui-inbounds.sh`** | Вместо `sqlite3 INSERT`: `inbounds/add` (VLESS+Reality+Vision; Hy2, если `HY2_ENGINE=xray`; AWG, если `AWG_ENGINE=panel`; TUIC/XHTTP по флагам), затем `clients/add` owner и `attach` ко всем. Routing `geoip:private → blocked`. Идемпотентность: проверка `inbounds/list` по remark и порту |
| `05-hysteria2.sh` | Выполняется только при `HY2_ENGINE=apernet`. Добавить `acl` и `trafficStats` на 127.0.0.1. Опционально `userpass` вместо `password` |
| `06-amneziawg.sh` | Выполняется только при `AWG_ENGINE=kernel`. Добавить PostUp с DROP на host-порты |
| **`07-migrate-engines.sh`** (новая) | При смене движка останавливает и отключает `hysteria-server`/`awg-quick@awg0`, создаёт клиента `legacy-hy2`, перевыпускает AWG-конфиг. Требует явного `--confirm` |
| **`08-monitoring.sh`** (новая) | vnStat, `vpn-zoo-collector` (unit + timer), `/usr/local/bin/zoo`, бэкап-timer, опционально Beszel |
| `99-print-creds.sh` | Печатает `ssh -L …`, URL панели, путь к 2FA, `zoo status`. Секреты в консоль не выводит, только пути к ним |

### 6.8 MVP-скоуп
1. Закреплённая версия 3x-ui v3.9.0 и захардкоженный SHA-256. Панель на 127.0.0.1, 2FA, токен в `config.env`, порт панели закрыт в ufw.
2. `04-xui-inbounds.sh` через REST API вместо INSERT. `zoo smoke` (inbounds/add, clients/add, attach, list на тестовом клиенте с последующим удалением).
3. Routing `geoip:private → blocked`. ACL Hy2 и DROP с awg0 для fallback-движков.
4. Флаги движков и миграция с `--confirm`.
5. Гейт (ручной прогон, результат записывается в README): iperf/speedtest через AWG userspace против ядра и Hy2-Xray против apernet на 1 vCPU, плюс проверка H1–H4 на AmneziaVPN и Keenetic.
6. `vpn-zoo-collector` (5 мин, свёртки), `zoo traffic`, `zoo status`, vnStat.
7. Обновлённый `99-print-creds.sh`.

Не входит в MVP: публичная подписка, Beszel, TUIC/XHTTP (только флаги), графики трафика по пользователю в вебе, одноразовые share-ссылки (TTL 24 ч, нужен свой код — фаза 2, если понадобится).

### 6.9 Этапы и трудозатраты (экспертная оценка, без замеров)
| Этап | Содержание | Дни |
|---|---|---|
| 0 | Срочные исправления безопасности: pin + SHA, listenIP, ufw, 2FA, `ssh -L` в print-creds | 1 |
| 1 | `04-xui-inbounds.sh` на API, `zoo smoke`, routing private | 2 |
| 2 | Гейт: бенчмарки и проверка клиентов на Ubuntu 22.04/24.04, 1 и 2 vCPU | 2 |
| 3 | Флаги движков, `07-migrate-engines.sh`, ACL и nft для fallback | 1–2 |
| 4 | Коллектор, `zoo status`/`traffic`, vnStat, бэкап | 1–2 |
| 5 | Hardening drop-in, аудит-логи, документация и регресс | 0.5–1 |
| — | **Итого MVP** | **7–11** |
| 6 (опц.) | Beszel, SUB_PUBLIC с TLS, TUIC/XHTTP, веб-графики, профиль AWG 3.x | +3–6 |

Своего кода примерно 400–700 строк bash. Новых долгоживущих демонов нет, кроме vnStat (и Beszel по флагу). RAM прирастает на ~5 МБ без Beszel и ~65 МБ с ним.

### 6.10 Риски и что с ними делать
| Риск | Митигация |
|---|---|
| AWG userspace на 1 vCPU медленнее ядра ❓ | Гейт, если провален, `AWG_ENGINE=kernel` (но AWG выпадает из панели, его видимость обеспечивает `zoo status` и коллектор по dump) |
| H1–H4 одиночные (#6557): Keenetic и часть клиентов 2.0 ломаются | Проверка на целевых клиентах в гейте, следить за issue |
| Hy2-Xray без Gecko/mimic, паритет Brutal не подтверждён ❓ | Гейт. `HY2_ENGINE=apernet` остаётся рабочим вариантом |
| Всё в одной корзине: x-ui от root, пачки advisory, медленное раскрытие (#4135) | Loopback, 2FA, отказ от импорта БД/шаблонов и мультиноды, быстрые обновления только через `zoo upgrade` + smoke |
| API ломается между минорами (v3.9.0 `inbounds/update`) | Закреплённая версия, `zoo smoke` перед апгрейдом, все вызовы API в одном файле-адаптере `lib-xui.sh` |
| Перевыпуск конфигов AWG (и, возможно, Hy2) всем пользователям | Миграция только с `--confirm`, сначала owner, потом остальные |
| Нет временного ряда per-user из коробки | Коллектор (прививка). Если `clients/list` не даёт per-inbound, MVP хранит итог по пользователю ❓ |
| SUB_PUBLIC и Beszel открывают порт под сканер ТСПУ | По умолчанию выключены, `zoo status` ловит лишние listen на 0.0.0.0 |
| Fallback-движки вне 3x-ui означают две модели пользователя | Если гейт провален по обоим движкам **и** нужен per-user учёт Hy2/AWG, пересмотреть вариант B (сайдкар) |

---

## 7. Открытые вопросы к владельцу

1. **Движки после гейта.** Согласны ли вы на AWG 3.1 в userspace внутри 3x-ui (минус скорость на 1 vCPU ❓, перевыпуск конфигов) и на Hy2 через Xray (минус Gecko/mimic)? Или ядро и apernet важнее единой панели?
2. **Критерий гейта.** Какой проигрыш скорости допустим: например, не хуже 80% от ядра/apernet? Какие клиенты обязательны: AmneziaVPN (какая версия), Keenetic, Happ, v2rayN, Hiddify?
3. **Доступ к панели.** Только `ssh -L` или ещё mgmt-пир AWG? Нужен ли когда-нибудь режим «наружу» (тогда Caddy, TLS, cookie-gate, 2FA, нужен домен)?
4. **Подписка.** Раздавать ссылки и QR вручную или поднимать публичную подписку на домене (открытый порт и риск бана IP)? Есть ли домен?
5. **Масштаб и назначение.** Сколько пользователей, нужны ли квоты, сроки и HWID? Использование личное или платное? Платное меняет оценку LucX-UI: PolyForm-NC.
6. **Миграция.** Можно ли перевыпустить AWG-конфиг (и при необходимости Hy2-ссылку) текущим пользователям, и когда?
7. **Мониторинг.** Хватает `zoo status` + vnStat + бот 3x-ui или ставить Beszel (+15/+50 МБ)? Нужен ли внешний сторож (Uptime Kuma на другом хосте)?
8. **Политика обновлений.** Кто и как часто поднимает закреплённый тег 3x-ui? Готовы ли обновляться в течение 1–2 дней после advisory?
9. **TUIC и XHTTP.** Включать в MVP или оставить флаги выключенными?
10. **Срочная часть (этап 0).** Делать её прямо сейчас, отдельным коммитом, до решения по админке? Рекомендация: да.

---

## 8. Источники

**3x-ui:**
- https://github.com/MHSanaei/3x-ui
- https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0
- https://github.com/MHSanaei/3x-ui/releases/tag/v3.7.0
- https://github.com/MHSanaei/3x-ui/releases/tag/v3.4.1
- https://github.com/MHSanaei/3x-ui/releases/tag/v3.3.0
- https://github.com/MHSanaei/3x-ui/commit/293c1e44
- https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/amneziawg.mdx
- https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/clients.mdx
- https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/clients.mdx
- https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/api-tokens.mdx
- https://docs.sanaei.dev/docs/reference/api/
- https://docs.sanaei.dev/docs/reference/api/server/
- https://github.com/MHSanaei/3x-ui/pull/6105
- https://github.com/MHSanaei/3x-ui/pull/6606
- issues: https://github.com/MHSanaei/3x-ui/issues/6557, https://github.com/MHSanaei/3x-ui/issues/6378, https://github.com/MHSanaei/3x-ui/issues/6423, https://github.com/MHSanaei/3x-ui/issues/5113, https://github.com/MHSanaei/3x-ui/issues/4135
- https://github.com/MHSanaei/3x-ui/security/advisories (GHSA-h4x8-qc42-f6wv, GHSA-fqw4-8j9p-5r3v, GHSA-xqqw-jqqv-99h6, GHSA-rr44-v4rv-x654)
- https://www.strix.ai/cve/CVE-2026-55477
- https://raw.githubusercontent.com/MHSanaei/3x-ui/main/install.sh

**Xray:**
- https://xtls.github.io/en/config/api.html
- https://xtls.github.io/en/config/stats.html
- https://xtls.github.io/en/config/policy.html
- https://xtls.github.io/en/config/inbounds/hysteria.html
- https://github.com/XTLS/Xray-core/releases
- https://github.com/XTLS/Xray-core/pull/6847
- https://github.com/XTLS/Xray-core/tree/main/main/commands/all/api

**Hysteria2:**
- https://v2.hysteria.network/docs/advanced/Traffic-Stats-API/
- https://v2.hysteria.network/docs/advanced/Full-Server-Config/
- https://github.com/apernet/hysteria/releases

**AmneziaWG:**
- https://github.com/amnezia-vpn/amneziawg-tools/releases
- https://github.com/amnezia-vpn/amneziawg-tools/blob/master/src/show.c
- https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/tags
- https://github.com/amnezia-vpn/amnezia-client/tree/dev/client/server_scripts/awg
- https://github.com/bivlked/amneziawg-installer
- https://github.com/AbolfazlTafakori/w-ui

**Панели:**
- https://github.com/AlexeyLCP/lucx-ui
- https://github.com/remnawave/panel
- https://docs.rw/docs/install/requirements
- https://github.com/PasarGuard/panel
- https://github.com/PasarGuard/panel/pull/932
- https://github.com/PasarGuard/panel/discussions/158
- https://github.com/alireza0/s-ui
- https://github.com/hiddify/Hiddify-Manager
- https://github.com/jonssonyan/h-ui
- https://github.com/ReturnFI/Blitz
- https://github.com/PRVTPRO/Amnezia-Web-Panel
- https://github.com/wg-easy/wg-easy/releases
- https://github.com/WGDashboard/WGDashboard/issues/1345
- https://github.com/h44z/wg-portal/issues/649
- https://github.com/Gozargah/Marzban
- https://github.com/gozargah/marzban/issues/2012
- https://github.com/coinman-dev/3ax-ui

**Мониторинг:**
- https://github.com/henrygd/beszel
- https://beszel.dev/guide/systemd
- https://github.com/vergoh/vnstat
- https://learn.netdata.cloud/docs/netdata-agent/resource-utilization/ram
- https://github.com/prometheus/node_exporter
- https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/
- https://github.com/louislam/uptime-kuma
- https://github.com/cockpit-project/cockpit

**Безопасность и РФ:**
- https://ntc.party/t/%D0%B2%D0%BD%D0%B8%D0%BC%D0%B0%D0%BD%D0%B8%D1%8E-%D0%BF%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D0%B5%D0%BB%D0%B5%D0%B9-3x-ui/12243
- https://www.avsisp.com/blog/our-takes/three-ways-russia-breaks-xray/
- https://github.com/net4people/bbs/issues/671
- https://habr.com/ru/articles/1084862/
- https://news.ycombinator.com/item?id=41717465
- https://en.zona.media/article/2025/06/19/cloudflare
- https://hol.org/guard/security/cves/CVE-2026-72603-wg-easy-wg-easy-os-command-injection
- https://www.samnet.dev/learn/networking/xray-routing/

**Стек своей админки (для вариантов B и C):**
- https://github.com/a-h/templ/releases
- https://github.com/bigskysoftware/htmx/releases/tag/v4.0.0
- https://pkg.go.dev/modernc.org/sqlite
- https://github.com/shirou/gopsutil/releases
- https://arxiv.org/html/2609.11932
