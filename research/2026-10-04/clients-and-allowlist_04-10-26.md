# Клиенты для массового пользователя, белый список приложений, браузер

**Дата:** 2026-10-04
**Для кого:** владелец `vpn-zoo-installer` и ИИ-ассистент, который будет вносить изменения.
**Контекст:** сервер инсталлера: VLESS RAW+REALITY+Vision на 443, VLESS XHTTP+REALITY (3x-ui v3.9.0, Xray 26.9.30), Shadowsocks-2022, Hysteria2 (pinSHA256, самоподписанный), опционально TUIC, AmneziaWG. Пользователи — небольшой частный круг в РФ, многие не технари.
**Новый принцип владельца:** обратный сплит. Через VPN идут только **разрешённые** приложения (allowlist), всё остальное, включая все RU-приложения, идёт мимо туннеля. Для VPN — отдельный браузер.

Связанные документы: [vpn-news §6](vpn-news_04-10-26.md) (клиенты), [vpn-success-and-leaks §3–4](vpn-success-and-leaks_04-10-26.md) (компрометаторы), [docs/RISK-REDUCTION.md](../../docs/RISK-REDUCTION.md) (пошаговые настройки в старом режиме «исключить RU-приложения»). Свой клиент — в [own-android-client_04-10-26.md](own-android-client_04-10-26.md).

## 0. Как собрано и как читать

Три потока исследования (клиенты, механика Android, браузер), у каждого свой фактчекер. Фактчекер сверял утверждения с исходниками (AOSP, 3x-ui v3.9.0, v2rayNG, sing-box-for-android, WG Tunnel, brave-core), документацией и GitHub API.

| Значок | Значение |
|---|---|
| ✅ | Подтверждено фактчекером |
| ✏️ | Исправлено фактчекером. В тексте стоит **исправленная** формулировка |
| ❓ | Не проверено или проверить невозможно без устройства |
| 📄 | Факт по коду или документации, на устройстве не проверяли |

Опровергнутые утверждения и опровергнутые части собраны в §9 «Мифы».

**Калибровка.** Почти всё здесь проверено по коду и документации, а не замерами на телефоне. Детект VPN со стороны RU-приложений — это статический анализ RKS (апрель 2026) и PoC, а не наблюдение «приложение X заблокировало пользователя». Allowlist — правильное безопасное умолчание, но не волшебная кнопка.

---

## 1. TL;DR

1. **Allowlist на Android — штатный механизм** (`VpnService.Builder.addAllowedApplication`). Через VPN идут только перечисленные приложения, остальные «как будто VPN нет». Новые, забытые и предустановленные приложения по умолчанию идут **мимо** туннеля. Это главный плюс по сравнению с исключением RU-приложений по списку. ✅
2. **Allowlist закрывает утечку адреса сервера** через echo-сервисы, бэкенды и STUN RU-приложений. **Факт VPN он не прячет:** `tun0`, VPN-сеть в `getAllNetworks()` и список установленных VPN-клиентов видны всем. Без root факт VPN на Android не скрыть ничем. ✅
3. **Allowlist не закрывает три канала:** открытый SOCKS на `127.0.0.1` в клиенте, привязку сокета к `tun0` и маршрут-исключение к серверу в `LinkProperties`. Это закрывает только клиент. ✅
4. **«Блокировать подключения без VPN» с allowlist не включать:** все приложения вне списка, то есть RU-приложения, останутся без сети. ✅
5. **Клиенты на sing-box** (Hiddify, Karing, NekoBox, SFA/SFI) и Shadowrocket против нашего Xray 26.9.30 по VLESS-REALITY **не работают**. Работают Xray-клиенты и mihomo (3x-ui v3.9.0 сама кладёт ML-KEM-флаг в ссылку и Clash-подписку). ✏️
6. **Allowlist с сервера, без рук пользователя, бесплатно** приезжает сегодня в двух местах: в заголовках подписки **INCY** и в `.conf` **AmneziaWG** (ключ `IncludedApplications`, работает в WG Tunnel 5.7.5). Третье место — JSON-профиль sing-box (`include_package`), но там только Hy2/SS-2022/TUIC. В Happ push allowlist требует Provider ID с суточной телеметрией HWID. ✅/✏️
7. **iOS:** per-app VPN без MDM невозможен, только сплит по доменам и IP. ✅
8. **Браузер под VPN — Brave**, но не браузером по умолчанию, с поиском не Яндекс и с сохранённым «РФ напрямую» внутри туннеля. Ставить из Google Play или APK с GitHub, **не из RuStore** (там версия 2024 года). ✏️

**Рекомендация «для ленивых»** — §3. Что заложить на сервере — §4.

---

## 2. Таблица клиентов

Обозначения: VLESS-R — VLESS-REALITY против нашего Xray 26.9.30. «Сервер задаёт» — что приезжает с подпиской, заголовком или в конфиге без участия пользователя. Версии — на 04.10.2026.

| Клиент (версия) | Платформы, ядро | VLESS-R / XHTTP | Другие наши протоколы | Allowlist (Android) | Локальный прокси | Сервер задаёт бесплатно | Доступность в РФ |
|---|---|---|---|---|---|---|---|
| **INCY** (iOS 2.6.2 от 21.09) | Android, iOS; Xray | ✅ / ✅ | ❓ в этом срезе не проверяли | **Из заголовков подписки** ✅ | ❓ закрыт ли SOCKS — только со слов третьих лиц, **проверить** | allowlist (`Per-App-Proxy-*`), hide-url, fragment/noise, резолв адреса сервера (`Server-Address-Resolve-*`), routing | Play (`llc.itdev.incy`); **есть в RU App Store** |
| **Happ** (Android 4.6.1 от 30.09; 4.7.0 pre от 02.10) | Android, iOS, Desktop 4.3.0; Xray | ✅ / ✅ | ❓ | В UI бесплатно; с сервера **только с Provider ID** ✏️ | Есть. Пароль `auto` через заголовок `Socks-Auth-Mode`; «Allow LAN» снимает пароль; с 3.20.0 заявлен «block bind to interface tun» (нужен тест) ✏️ | title, interval, routing, `Socks-/Http-Auth-Mode` | Android — GitHub-релизы; **нет в RU App Store**, там клоны с «Happ» в названии |
| **v2rayNG** (2.2.6 стабильная; 2.3.10 pre от 01.10, Xray 26.9.30) | Android; Xray | ✅ / ✅ | ❓ | В UI ✅ (неотмеченные — напрямую) | **Можно выключить совсем**: Xray TUN вместо Hev + «Enable local proxy» off 📄 | только подписка, `install-config` deeplink, custom JSON; per-app **не** задаётся | только APK с GitHub (Play и F-Droid — 404) |
| **AmneziaWG** / **WG Tunnel** 5.7.5 (26.09) | Android; AWG | — | AWG ✅ | **Из `.conf`/QR** (`IncludedApplications`) ✅ | Нет | весь `.conf`, включая allowlist | ❓ не проверяли |
| **AmneziaVPN** 5.0.3.0 | все; AWG + Xray | ❓ версия Xray неизвестна | AWG ✅ | В UI режим «только приложения из списка»; баг: туннель с app-split молча умирает после Doze (#3255, открыт 03.10) | у Xray-моста случайные логин, пароль, порт | `vpn://` — передаётся ли app-split ❓ | ❓ |
| **sing-box SFA** | Android; sing-box | ❌ / ❌ | Hy2, SS-2022, TUIC ✅ | **Из удалённого JSON-профиля** (`include_package`) ✅ | Нет, если в JSON нет `mixed` и `clash_api` | весь JSON, включая пин Hy2 | ❓ |
| **FlClash** 0.8.99 (03.10) | Android, десктоп; mihomo | ✅ через подписку 3x-ui (флаг ML-KEM) ✏️ | ❓ | В UI (access control) | `mixed-port`/`external-controller` выключать вручную | Clash-подписка | GitHub |
| **v2RayTun** 5.25.82 (27.09) | Android; Xray | ✅ | ❓ | Нет push per-app | ❓ | ровно 8 заголовков: routing, update-always, network-filter и др. | ❓ |
| **v2rayN** 7.25.4 (30.09, Xray 26.9.30) | Windows, Linux; Xray | ✅ / ✅ | ❓ | на десктопе — правило `process` в Xray TUN ✏️ | есть, ❓ | подписка | GitHub |
| **Hiddify** 4.1.1 (05.03.2026) | все; hiddify-sing-box 1.13 | почти наверняка ❌ ✏️ | Hy2/SS/TUIC ❓ | — | — | — | не рекомендуем до проверки нового релиза |
| Karing, NekoBox, SFI, Shadowrocket | sing-box и др. | ❌ | — | — | — | — | не рекомендуем |
| Husi 2.2.0, Exclave 0.17.57-beta.1 | sing-box / Go-TLS | ❓ (у Exclave REALITY без мимикрии под браузер — риск отпечатка) | — | — | — | — | не рекомендуем |

Источники строк — §10.

---

## 3. Рекомендация «для ленивых» по платформам

### Android

| Приоритет | Клиент | Почему | Что от пользователя |
|---|---|---|---|
| **1. Ноль настроек, работает уже сейчас** | **AmneziaWG или WG Tunnel** + наш `.conf`/QR с `IncludedApplications` | Allowlist зашит в QR, локального SOCKS нет, испортить нечего. WG Tunnel 5.7.5 применяет ключ при импорте ✅; парсер amneziawg-android его принимает ✅ | Отсканировать QR. Если AWG не доходит из сети пользователя (проверить пробником) — вариант 2 |
| **2. VLESS «с сервера»** | **INCY** + публичная подписка с заголовками | Единственный массовый Xray-клиент, которому сервер **бесплатно** задаёт allowlist ✅. Есть в Play и RU App Store | Добавить подписку. **До раздачи владелец сам проверяет**, слушает ли INCY порт на loopback без пароля (RKNHardering) |
| 3. Запасной VLESS | **Happ** | Allowlist в UI бесплатный, пароль на локальный прокси приходит заголовком | Включить allowlist руками по инструкции со скриншотами (§7.3) |
| 4. Для гиков | **v2rayNG** | Открытый, локальный прокси можно убрать полностью | Всё руками (§7.4) |

Минусы INCY, которые надо знать: клиент **закрытый** (в репо `incy-platforms` только бинарники), в мобильных сборках **Firebase Crashlytics** ✏️. Если это неприемлемо, основным VLESS-клиентом становится v2rayNG или свой клиент.

### iOS

**INCY** из RU App Store со сплитом по доменам (`geosite:ru` → direct из подписки). Per-app на iOS невозможен без MDM ✅, поэтому принцип владельца на iPhone реализуется только сплитом по доменам и отдельным браузером. Happ — только через иностранный Apple ID; предупредить про клоны в RU App Store ✅. Детект VPN на iOS слабее, чем на Android.

### Windows

**v2rayN 7.25.4** с Xray TUN и пользовательским правилом `process`: `[brave.exe, Telegram.exe] → proxy`, остальное → direct. v2rayN передаёт `process` в routing Xray и поднимает Xray TUN ✅. Альтернатива — Happ Desktop 4.3.0. Плюс Brave с политиками из `.reg` (§6.3).

### macOS

Поле `process` в Xray по документации поддерживается **только на Windows и Linux** ✏️. На macOS остаётся прокси внутри браузера (§6.3, fail-closed) или mihomo-клиент с правилами `PROCESS-NAME` (работа на macOS ❓).

---

## 4. Что заложить на сервере (конкретно)

**Предусловие для заголовков.** Сейчас публичная подписка выключена (D8), а `SUB_PUBLIC=1` только включает её в настройках панели без открытого порта ([RISK-REDUCTION §5](../../docs/RISK-REDUCTION.md)). Пока подписка с доменом и TLS не доведена, заголовки INCY и Happ до клиента **не доходят**. Пункт 4.1 стоит **после** этой работы; 4.2 можно делать сразу.

### 4.0 Один источник allowlist

Предложение: один файл, например `/etc/vpn-zoo/allowlist.txt` (или переменная `ZOO_ALLOWLIST`), из которого генерируются заголовок INCY, строка `.conf` AWG и `include_package` sing-box. Пресет по умолчанию — короткий (уточнить у пользователей):

```
com.brave.browser
org.telegram.messenger
com.google.android.youtube
```

Дополнительно по запросу: WhatsApp, Instagram, ChatGPT, Claude и т. п. Пакеты, которых нет на устройстве, клиент должен пропускать: `addAllowedApplication` на неустановленный пакет бросает `NameNotFoundException` ✅. Как это обрабатывают INCY и AWG-клиенты — ❓, проверить на телефоне без Brave.

### 4.1 INCY (заголовки подписки 3x-ui v3.9.0) ✅📄

3x-ui v3.9.0 (`internal/sub/incy.go`) уже отдаёт:

```
Per-App-Proxy-Enable: 1
Per-App-Proxy-Mode: proxy          # proxy = allowlist, bypass = исключения
Per-App-Proxy-List: com.brave.browser,org.telegram.messenger,…
```

Плюс бесплатно: hide-url (пользователь не видит адрес подписки), `fragmentation-*`/`noises-*`, `Server-Address-Resolve-*` (резолв адреса сервера через DoH), routing с `geosite:ru`/`geoip:ru` → direct. Per-app работает только на Android, `banner-*` — только премиум ✏️. Имена ключей настроек панели, которые включают эти заголовки, сверить с `incy.go` при реализации ❓. Баг 3x-ui #6273 (закрыт not_planned): INCY получает routing в формате Happ. Понимает ли его INCY — проверить ❓.

### 4.2 AmneziaWG: строка в `.conf` ✅📄

В `_awg_client_conf` (`scripts/lib/proto-amneziawg.sh`) добавить в `[Interface]`:

```ini
IncludedApplications = com.brave.browser, org.telegram.messenger, com.google.android.youtube
```

Правила: `IncludedApplications` и `ExcludedApplications` одновременно — ошибка парсера (`BadConfigException`) ✅. Как ключ воспринимают AmneziaWG для Windows/iOS — ❓, поэтому выдавать **отдельный Android-вариант** `.conf`/QR, а не менять общий. Импорт ключа в AmneziaVPN 5.0.3.0 — ❓.

### 4.3 Happ: только то, что бесплатно ✅

`subHappAutoDetect=true` + `subHappLocalProxyAuth=auto` (фаза 03 это уже выставляет) → заголовки `Socks-Auth-Mode`/`Http-Auth-Mode` дают случайный пароль на локальный прокси без Provider ID. Routing тоже не требует Provider ID. **Provider ID не покупать:** per-app и autoconnect работают только с ним, а клиент тогда раз в сутки шлёт на `check.happ-proxy.com` HWID, ОС и хэш домена ✏️.

### 4.4 sing-box JSON для Hy2/SS-2022/TUIC (опционально) ✅📄

Профиль, который генерируем мы: `tun` inbound с `auto_route: true` и `include_package: [...]`, **без** `mixed` и `clash_api` (тогда на loopback ничего не слушает), Hy2 с пином SPKI. Импорт — `sing-box://import-remote-profile?url=…`. SFA передаёт `includePackage` в `addAllowedApplication` ✅. В SFA держать **выключенной** настройку allowBypass ✅. VLESS-REALITY и XHTTP этим путём не работают.

### 4.5 Что не включать

- **JSON-подписку 3x-ui:** шаблон кладёт клиенту SOCKS `127.0.0.1:10808` с `noauth` ✅.
- **Маршрут-исключение к серверу** в своих профилях: `excludeRoute` виден всем приложениям через `LinkProperties` (для приложений с targetSdk ≥33) ✏️. В режиме allowlist сам VPN-клиент и так вне туннеля, исключать сервер не нужно.

### 4.6 Серверная страховка остаётся

Echo → WARP/блок и опционально `RU_EGRESS=block|warp` нужны и при allowlist: разрешённый браузер может открыть RU-сайт через туннель (§6).

---

## 5. Механика allowlist на Android

### Что allowlist даёт ✅

- Через VPN идут только перечисленные пакеты, остальные работают «as if the VPN wasn't running» (AOSP `VpnService`). Смешать с `addDisallowedApplication` нельзя.
- У приложения вне туннеля активная сеть без `TRANSPORT_VPN`, а echo, бэкенды (MAX: `api.oneme.ru`, ipify и др.) и STUN в звонках видят домашний IP, а не адрес сервера.
- Всё неизвестное — новые приложения, предустановленные, SDK — по умолчанию вне туннеля. При старом режиме «исключить RU-приложения» любое забытое приложение шло в туннель.

### Что allowlist НЕ скрывает ✅

| Канал | Почему виден | Что помогает |
|---|---|---|
| VPN-сеть в списке сетей | `getAllNetworks()` и `setIncludeOtherUidNetworks(true)` не фильтруют по UID, `TRANSPORT_VPN` читается | Ничего без root |
| `tun0` | Интерфейсы с IP видны через `NetworkInterface`; RKS: 13 из 30 RU-приложений перечисляют интерфейсы (статанализ) ✏️ | Ничего без root |
| Список VPN-клиентов | Сервис обязан объявить `BIND_VPN_SERVICE`; на Android 11+ хватает `<queries>`. WB, 2ГИС, МТС, Ozon, Мегамаркет, RuStore, ОК собирают список | Рабочий профиль скрывает **список пакетов**, но не факт VPN |
| SOCKS/HTTP на `127.0.0.1` | Loopback внутри профиля доступен всем, allowlist ни при чём | Клиент без локального порта; частично — случайный пароль и порт ✏️ |
| Привязка к `tun0` | Исключённое приложение делает `curl --interface tun0` и получает IP сервера (Amnezia #2457, Android 14) | Проверка `getConnectionOwnerUid` в клиенте (Amnezia PR #3199, не влит; Happ 3.20.0 заявляет «block bind to interface tun») |
| Маршруты и DNS VPN-сети | `LinkProperties` не редактируются | Не исключать сервер маршрутом |

`/proc/net` для обычных приложений закрыт SELinux-политикой AOSP ✏️ (RKNHardering читает его как fallback, на стоковых прошивках это не работает).

### Ловушки

- **«Блокировать без VPN» (lockdown)** с allowlist ломает все приложения вне списка ✅. Включать нельзя.
- **Браузер по умолчанию.** Если Brave — браузер по умолчанию, ссылки и Custom Tabs из RU-приложений (SMS банка, 3DS, Госуслуги) открываются под UID Brave и идут в туннель ✅ (вывод по механике, без теста). WebView внутри RU-приложения остаётся под его UID и идёт мимо.
- **Новое приложение** из списка, поставленное после подключения, в туннель не попадёт, пока туннель не переподнять ✅.
- **Баг Android 16** (Mullvad, 12.05.2026): UDP-пакеты закрытия QUIC разрешённых приложений уходят мимо VPN, Google закрыл как Won't Fix. Утекает **домашний IP**, а не адрес сервера ✏️.
- **Рабочий профиль и Private Space** скрывают список пакетов, но не флаг VPN и `tun0` (GrapheneOS #5225, #7511) ✅. Android 17 запрещает межпрофильный loopback ✅.

### Трекинг RU-приложений «на уровне Android»

Через тот же VPN не получится: одновременно активен только один VPN, а в режиме allowlist клиент не видит пакетов RU-приложений ✅. Если завести RU-приложения в туннель с выходом напрямую, у них появится `TRANSPORT_VPN` и детект станет сильнее. Без root остаются: системный Private DNS с блок-листом, ограничение фоновых данных, заморозка через Shizuku (Anubis), разрешение «Сеть» в GrapheneOS ✅/❓.

---

## 6. Браузер для VPN

### 6.1 Выбор: Brave ✏️

| Браузер | Плюсы | Минусы | Роль |
|---|---|---|---|
| **Brave 1.96.61** (02.10, Chromium 154) | Интерфейс как у Chrome, блокировка рекламы и трекеров, русский язык, политики на десктопе | Яндекс по умолчанию для RU, P3A и stats ping включены, лишние функции | **Основной** |
| Cromite v153.0.8010.37 (15.09) | WebRTC выключен, защита от отпечатков, `chrome://proxy` на Android | Нет в Play, авторы не рекомендуют для стран с ограничениями | Для продвинутых |
| IronFox v157.0.0.1 (04.10) | about:config, прокси в браузере | F-Droid-репо, Accrescent, Obtainium | Для продвинутых |
| Mullvad Browser 15.0.24 | Сильная анти-отпечатковая модель | Нет Android, всегда приватный режим | Запасной, десктоп |
| Tor Browser 15.0.24 | — | Идёт через Tor, а не наш VPN; в РФ нужны мосты | Запасной |

Brave в РФ **не заблокирован** Роскомнадзором (подтверждений нет). VPN и Rewards Brave сам выключает при RU-языке/регионе, Leo не подключается с RU-IP; сбой обновлений Windows с RU-IP Brave на себя не берёт (сотрудники винят фильтрацию CloudFront на стороне РФ) ✏️. Через VPN всё это не мешает.

**Где брать:** Google Play (страница отвечает 200; открывается ли установка из РФ — ❓) или APK с [GitHub Releases](https://github.com/brave/brave-browser/releases/tag/v1.96.61) (`Bravearm64Universal.apk`). APK с GitHub сам не обновляется — ленивым нужен Play или Obtainium. **Не из RuStore:** там 1.68.133 от 04.08.2024 ✏️.

### 6.2 Обязательные настройки (Android, руками)

1. **Не делать браузером по умолчанию** (§5, ловушки).
2. **Поиск:** сменить Яндекс на Brave Search, DuckDuckGo или Startpage. Для RU-региона Brave ставит Яндекс, подсказки на каждый символ уходят на `suggest.yandex.ru` ✅.
3. Выключить P3A, stats ping (ежедневный пинг), Leo, Wallet, Rewards, News, Talk. Синхронизацию не включать.
4. **WebRTC:** «Shields & privacy» → «WebRTC IP Handling Policy» → «Disable non-proxied UDP». В обычном профиле Brave оставляет политику Chromium по умолчанию (меняет её только в Tor-окне) ✅; путь в меню Android подтверждён только вторичными источниками ❓. В режиме TUN это не утечка (STUN видит адрес выхода), но настройка дешёвая.

### 6.3 Десктоп: политики одним `.reg` ✅

Политики есть в `brave_simple_policy_map.h`: `BraveRewardsDisabled`, `BraveWalletDisabled`, `BraveVPNDisabled`, `BraveAIChatEnabled=0`, `BraveNewsDisabled`, `BraveTalkDisabled`, `BraveP3AEnabled=0`, `BraveStatsPingEnabled=0`, `BraveWebDiscoveryEnabled=0`, `BraveReduceLanguageEnabled=1`, `BraveGlobalPrivacyControlEnabled=1`, плюс Chromium: `WebRtcIPHandling=disable_non_proxied_udp`, `DefaultSearchProvider*`, `DnsOverHttpsMode`.

**Fail-closed на десктопе:** `ProxySettings` с `socks5://127.0.0.1:PORT` — при выключенном клиенте браузер покажет ошибку, а не пойдёт напрямую. Цена: Chromium не умеет SOCKS5 с паролем, а открытый локальный порт видит любая программа ✅. На Android fail-closed для одного браузера системными средствами получается только через рабочий профиль с always-on + lockdown (VPN-клиент и Brave в профиле, RU-приложения в основном) ✏️ — для ленивых сложно; проще заложить в свой клиент.

### 6.4 «РФ напрямую» внутри туннеля — оставить ✅

Разрешённый браузер, открывший RU-сайт через туннель, покажет ему адрес сервера. Поэтому в клиенте остаётся `geosite:category-ru`/`geoip:ru` → direct, на сервере — страховка `RU_EGRESS`. Компромисс: любой сайт в «VPN-браузере» может подгрузить ресурс (в том числе STUN) с RU-IP и узнать **домашний** IP пользователя. Адрес сервера не утекает, но анонимность браузера пропадает. Для пользователя проще: «синий» Brave — только для заблокированного, банки и Госуслуги — в обычном браузере вне VPN.

---

## 7. Пошаговая настройка в режиме allowlist

Это **новый режим** вместо «исключить RU-приложения» из RISK-REDUCTION §4.2–4.4. Названия пунктов меню могут отличаться между версиями.

### 7.1 AmneziaWG / WG Tunnel (рекомендуемый для ленивых)

1. Владелец генерирует Android-вариант `.conf` с `IncludedApplications` (§4.2) и показывает QR.
2. Пользователь ставит Brave (§6.1), **потом** WG Tunnel или AmneziaWG, сканирует QR.
3. Проверить: Brave открывает заблокированный сайт; RU-приложения работают; в настройках туннеля видны выбранные приложения.
4. Не включать «Блокировать подключения без VPN» в системных настройках.

### 7.2 INCY (после публичной подписки)

1. Владелец выдаёт ссылку на подписку, заголовки `Per-App-Proxy-*` уже настроены (§4.1).
2. Пользователь ставит Brave, затем INCY из Play, добавляет подписку.
3. Проверить, что per-app включён и в списке только приложения из пресета.
4. Владелец один раз проверяет на своём телефоне через RKNHardering, нет ли открытого порта на loopback ❓.

### 7.3 Happ (руками)

1. Подписка или ссылка от владельца (`zoo links <имя>`).
2. «Настройки» → «Настройки туннеля» → «Прокси для выбранных приложений» → режим **«ВКЛ»** (отмеченные идут через VPN). Это **обратное** тому, что сейчас написано в RISK-REDUCTION §4.2 шаг 3.
3. Отметить Brave, Telegram и другие из пресета.
4. Inbounds → авторизация **auto** (или приходит заголовком). «Разрешить LAN-подключения» не включать.
5. Маршрутизация «РФ напрямую» остаётся включённой.

### 7.4 v2rayNG (для гиков)

1. Импорт подписки или ссылок.
2. Боковое меню → «Выбор приложений» → включить, режим обхода («Bypass Mode») **выключить**: тогда отмеченные приложения идут через прокси, неотмеченные — напрямую ✅.
3. Отметить приложения из пресета.
4. «Настройки» → выключить «Использовать Hev TUN», затем «Использовать локальный прокси» (RISK-REDUCTION §4.3, шаг 3) 📄. Риск встроенного TUN Xray: петли и отказ sniffing (#5181) — если не работает, вернуть Hev TUN.
5. Маршрутизация: «Белый список России» (RISK-REDUCTION §4.3, шаг 1).
6. Учесть: при сбое старта ядра v2rayNG снимает tun (`stopAllService`), и приложения из allowlist выходят напрямую (fail-open) ✅.

---

## 8. Статусы утверждений

| ID | Утверждение (итоговая формулировка) | Статус |
|---|---|---|
| c1 | VLESS-REALITY против Xray ≥26.9.8 проходят Xray-клиенты и mihomo (ML-KEM-флаг кладёт 3x-ui v3.9.0); sing-box — нет | ✏️ |
| c2 | Hiddify: стабильная 4.1.1 от 05.03, разработка активна, ядро sing-box 1.13 — VLESS-REALITY почти наверняка ❌ | ✏️ |
| c3 | INCY бесплатно получает allowlist из заголовков; клиент закрытый, с Crashlytics | ✏️ |
| c4 | Happ: Socks/Http-Auth-Mode без Provider ID, per-app — только с ним; Xray 26.9.9 только в 4.7.0 pre | ✏️ |
| c5 | «Happ уязвим» — история: Xray API убран, auth на inbound с 3.18.0, «block bind tun» с 3.20.0; остаётся локальный SOCKS | ✏️ |
| c6 | v2rayNG: allowlist в UI, локальный прокси выключается; 2.3.10 — pre-release | ✅ |
| c7 | `IncludedApplications` в `.conf` AWG; WG Tunnel 5.7.5 применяет при импорте | ✅ |
| c8 | SFA применяет `include_package` из удалённого профиля; allowBypass держать выключенным | ✅ |
| c9 | Allowlist не закрывает привязку к `tun0` и факт VPN | ✅ |
| c10 | iOS: per-app только с MDM | ✅ |
| c11 | `process` в Xray — Windows и Linux; v2rayN 7.25.4 его передаёт | ✏️ |
| c12 | Brave оправдан, нужен «РФ напрямую»; в RuStore устаревшая версия | ✏️ |
| c13 | Прочие Android-клиенты вторичны; Exclave — риск отпечатка | ✏️ |
| c15 | Матрица серверной преднастройки (§4) | ✅ |
| M1–M16 | Механика Android (§5) | ✅, M3/M4/M9/M12/M15 ✏️, M6 ❓ |
| b1–b14 | Браузер (§6) | ✅, b1/b3 ✏️ |

### Открытые вопросы (нужен телефон)

- Слушает ли INCY Android порт на loopback без пароля? (RKNHardering)
- Как INCY и AWG-клиенты обрабатывают неустановленный пакет из allowlist?
- AmneziaVPN 5.0.3.0: импорт `IncludedApplications`, версия Xray, перенос app-split в `vpn://`.
- Работает ли v2rayNG 2.3.10 на Xray TUN без локального прокси на реальном трафике?
- Отрезает ли Happ 3.20.0+ привязку к `tun0` (тест в Termux `curl --interface tun0`)?
- Открывается ли установка Brave из Google Play в РФ?

---

## 9. Мифы

| Миф | Как на самом деле |
|---|---|
| «Happ уязвим» | Это история. Xray API без авторизации убран весной 2026, пароль на inbound есть с 3.18.0, заявлена защита от привязки к `tun0` (3.20.0). Остаётся локальный SOCKS: пароль `auto`, без «Allow LAN» ✏️ |
| «Hiddify не видно, потому что он заброшен» | Разработка идёт (коммиты 03–04.10), но релиза нет с 05.03, а ядро sing-box не проходит наш REALITY ✏️ |
| «INCY — open-core» | Мобильный клиент закрытый, в репо только бинарники ✏️ |
| «Allowlist прячет VPN от RU-приложений» | Прячет адрес сервера. Факт VPN (`tun0`, VPN-сеть, список VPN-клиентов) виден ✅ |
| «Включу “Блокировать без VPN” — будет kill-switch» | С allowlist это отключит сеть всем RU-приложениям ✅ |
| «Рабочий профиль скрывает VPN» | Скрывает список пакетов, не флаг VPN и не `tun0` ✅ |
| «Brave заблокирован в РФ / Brave режет RU-обновления» | Блокировки РКН не найдено; сотрудники Brave отрицают блокировку обновлений с RU-IP ✏️ |
| «Правило `process` работает в Xray везде» | Только Windows и Linux; на Android нужен отдельный вызов в клиенте ✏️ |
| «Karing починили (#1952 закрыт)» | Закрыт без фикса: «Whoever caused the compatibility issue is responsible» ✏️ |
| «Через Happ allowlist можно раздать бесплатно» | Только с Provider ID, с суточной отправкой HWID ✏️ |
| «В Happ локальный SOCKS есть всегда» | Не доказано: с 3.20.0 есть Xray Tun, с 4.4.0 переход на hev ✏️ |

Отдельно: Xray v26.9.8/26.9.9/26.9.30 на GitHub — pre-release (все релизы Xray помечены так). Если когда-нибудь откатить сервер на 26.7.28, sing-box-клиенты снова заработают (A/B в Karing #1952) — это отдельное решение, не рекомендация.

---

## 10. Источники

**Клиенты**
- sing-box #4520: https://github.com/SagerNet/sing-box/issues/4520
- Karing #1952: https://github.com/KaringX/karing/issues/1952
- mihomo #3193: https://github.com/MetaCubeX/mihomo/issues/3193
- 3x-ui PR #6712 (ML-KEM в ссылках): https://github.com/MHSanaei/3x-ui/pull/6712
- Сводка клиентов (сторонняя): https://gist.github.com/Piska-Kiska/faf0f8fca9953ac86af69f60491658e1
- Hiddify: https://github.com/hiddify/hiddify-app/releases, https://github.com/hiddify/hiddify-app/issues/2350, https://github.com/hiddify/hiddify-core/blob/main/go.mod
- INCY: https://docs.incy.cc/app-management/, https://github.com/INCY-DEV/incy-platforms, https://apps.apple.com/ru/app/incy/id6756943388
- 3x-ui v3.9.0: https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.9.0/internal/sub/incy.go, https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.9.0/internal/sub/happ.go, https://github.com/MHSanaei/3x-ui/issues/6273
- Happ: https://github.com/Happ-proxy/happ-android/releases, https://happ.mintlify.app/technical-docs/app-management, https://docs.happ-proxy.com/getting-started/provider-id.md, https://kod.ru/vpn-happ-problems
- v2rayNG: https://raw.githubusercontent.com/2dust/v2rayNG/master/V2rayNG/app/src/main/res/values/strings.xml, https://github.com/2dust/v2rayNG/pull/5548, https://github.com/2dust/v2rayNG/releases, https://github.com/2dust/v2rayNG/issues/5181, https://github.com/2dust/v2rayNG/blob/master/V2rayNG/app/src/main/java/com/v2ray/ang/service/CoreVpnService.kt
- AmneziaWG/WG Tunnel: https://raw.githubusercontent.com/amnezia-vpn/amneziawg-android/master/tunnel/src/main/java/org/amnezia/awg/config/Interface.java, https://github.com/wgtunnel/android/blob/5.7.5/app/src/main/java/com/zaneschepke/wireguardautotunnel/viewmodel/SplitTunnelViewModel.kt, https://docs.amnezia.org/documentation/instructions/vpn-split-tunneling/, https://github.com/amnezia-vpn/amnezia-client/issues/3255
- sing-box SFA: https://sing-box.sagernet.org/configuration/inbound/tun/, https://github.com/SagerNet/sing-box-for-android/blob/main/app/src/main/java/io/nekohasekai/sfa/bg/VPNService.kt
- v2RayTun: https://docs.v2raytun.com/overview/supported-headers
- Прочие: https://github.com/xchacha20-poly1305/husi/releases, https://github.com/dyhkwong/Exclave/releases, https://github.com/chen08209/FlClash/releases, https://github.com/OneXray/OneXray/releases
- Windows: https://xtls.github.io/en/config/routing.html, https://github.com/2dust/v2rayN/releases/tag/7.25.4
- iOS: https://support.apple.com/guide/deployment/vpn-overview-depae3d361d0/web

**Механика Android**
- VpnService: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/net/VpnService.java
- ConnectivityService: https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service/src/com/android/server/ConnectivityService.java
- NetworkRequest, LinkProperties, VpnTransportInfo: https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/framework/src/android/net/
- Vpn.java: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/connectivity/Vpn.java
- netd NetworkController: https://android.googlesource.com/platform/system/netd/+/refs/heads/main/server/NetworkController.cpp
- sepolicy app.te: https://android.googlesource.com/platform/system/sepolicy/+/refs/heads/main/private/app.te
- LauncherAppsService: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/pm/LauncherAppsService.java
- Android 17 loopback: https://developer.android.com/about/versions/17/behavior-changes-all
- RKS Global: https://rks.global/ru/research/vpn-detection/
- RKNHardering: https://github.com/xtclovver/RKNHardering/blob/main/docs/README.en.md
- Amnezia #2457, PR #3199: https://github.com/amnezia-vpn/amnezia-client/issues/2457, https://github.com/amnezia-vpn/amnezia-client/pull/3199
- GrapheneOS #5225, #7511: https://github.com/GrapheneOS/os-issue-tracker/issues/5225, https://github.com/GrapheneOS/os-issue-tracker/issues/7511
- PoC loopback: https://github.com/runetfreedom/per-app-split-bypass-poc, https://habr.com/ru/articles/1020080/
- Mullvad: https://mullvad.net/en/help/split-tunneling-with-the-mullvad-app, https://mullvad.net/en/blog/2026/5/12/any-app-on-recent-android-versions-can-leak-certain-traffic
- Anubis: https://habr.com/ru/articles/1023352/; root-скрытие: https://habr.com/ru/articles/1024890/

**Браузер**
- Brave: https://github.com/brave/brave-browser/releases/tag/v1.96.61, https://github.com/brave/brave-browser/issues/52983, https://github.com/brave/brave-browser/issues/53979, https://github.com/brave/brave-browser/issues/43465
- brave-core: https://github.com/brave/brave-core/blob/master/browser/tor/tor_profile_manager.cc, https://github.com/brave/brave-core/blob/master/browser/policy/brave_simple_policy_map.h, https://github.com/brave/brave-core/blob/master/components/search_engines/brave_prepopulated_engines.cc
- RuStore Brave: https://www.rustore.ru/catalog/app/com.brave.browser
- Chromium proxy: https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md
- Cromite: https://github.com/uazo/cromite/blob/master/docs/FEATURES.md
- IronFox: https://gitlab.com/ironfox-oss/IronFox
- Mullvad Browser: https://github.com/mullvad/mullvad-browser/releases/tag/15.0.24
- WebRTC: https://mullvad.net/en/help/webrtc

Сырые данные — [raw/clients-and-own-app.json](raw/clients-and-own-app.json).
