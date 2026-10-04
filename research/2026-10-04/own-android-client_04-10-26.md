# Свой Android-клиент: оценка и бриф MVP

**Дата:** 2026-10-04
**Для кого:** владелец `vpn-zoo-installer` и ИИ-ассистент.
**Вопрос владельца:** сделать свой клиент «для нас», не на все случаи жизни: все наши протоколы, allowlist приложений по умолчанию, максимум защиты от трекинга, минимум действий от пользователя. «Странно, что такого нет?»

Связано: [clients-and-allowlist_04-10-26.md](clients-and-allowlist_04-10-26.md) (готовые клиенты и механика Android, §5). Статусы те же: ✅ подтверждено, ✏️ исправлено фактчекером (стоит исправленная формулировка), ❓ не проверено, 📄 по коду или документации без теста на устройстве. **Оценки трудозатрат — экспертные, без первоисточника.**

---

## 1. Вердикт

**Свой клиент делать имеет смысл, но вторым этапом и как форк, а не с нуля.** Первый этап — преднастройка готовых клиентов с сервера (AWG `.conf` с allowlist, INCY-заголовки): это 2–5 дней и закрывает большую часть потребности. Свой клиент добавляет то, чего готовые не дают вместе: allowlist по умолчанию без платного white-label, отсутствие локального порта, защиту от привязки к `tun0`, fail-closed внутри приложения, нашу маршрутизацию и автообновление.

**Чего свой клиент не сможет, как и любой другой без root:** скрыть сам факт VPN (`tun0`, VPN-сеть в `getAllNetworks()`, VPN-сервис в списке пакетов) ✅. Обещать пользователям «приложения не увидят VPN» нельзя.

---

## 2. Почему публичные клиенты так не делают

Это **вывод**, а не измерение ✅ (фактчекер подтвердил, что это рассуждение, а не факт).

- **Клиент общего назначения не знает, какие приложения у пользователя.** Главный критерий качества для него — «работает всё сразу». Allowlist по умолчанию ломает незнакомые приложения и рождает обращения в поддержку.
- **Провайдеры получают это за деньги:** Happ даёт выставить `per-app-proxy-mode=on` заголовком, то есть allowlist по умолчанию, но только с Provider ID ✏️ (с суточной отправкой HWID на `check.happ-proxy.com`); у INCY бесплатно per-app, премиум — баннеры.
- **Защиты, которые нужны против RU-детекта, свежие:** PoC с открытым SOCKS — апрель 2026, обход через `tun0` (Amnezia #2457) — апрель 2026, UID-guard (Amnezia PR #3199) открыт с 22.09.2026 и по умолчанию выключен.

В закрытом круге с известным списком приложений первый довод не работает. Поэтому «для нас» иначе, чем «для всех», — это нормально.

---

## 3. Ближайшие аналоги

| Аналог | Что есть | Чего нет для нас |
|---|---|---|
| **Happ + Provider ID** | Allowlist, hide-settings, autoconnect с сервера | Закрытый, платный white-label, телеметрия HWID ✏️ |
| **INCY** | Allowlist с сервера бесплатно ✅ | Закрытый, Crashlytics, нет AWG/Hy2 в нашей проверке ❓ |
| **AmneziaVPN** | Свои протоколы, app-split, UID-guard в PR #3199 | Нет Hy2 и XHTTP; Qt/C++ — тяжёлый форк; баг Doze (#3255) |
| **RethinkDNS** (Apache-2.0) | Файрвол, DNS-блоклисты, WireGuard по приложениям | Не Xray, не наши протоколы |
| **InviZible Pro** (GPL-3.0, 7.5.0 от 12.07) | Tor, DNSCrypt, файрвол | Не Xray |
| **Anubis** (через Shizuku) | Заморозка приложений, группы «Без VPN»/«Только VPN» | Не VPN-клиент |

Allowlist **по умолчанию** в публичных клиентах без white-label нигде не найден ✏️.

---

## 4. Варианты

| | **A. Преднастройка готовых с сервера** | **B. Форк + ребрендинг + преднастройка** | **C. Свой на libXray + amneziawg-go** |
|---|---|---|---|
| Что это | AWG `.conf` с `IncludedApplications`, INCY-заголовки, Happ `Socks-Auth-Mode` | Форк SaeedDev94/Xray или v2rayNG: allowlist и пресеты по умолчанию, импорт `zoo://`, без локального порта | Своя оболочка на libXray (MIT), AWG в том же Go-модуле |
| Срок (оценка) | 2–5 дней | 3–6 недель, один разработчик (Kotlin + немного Go/gomobile) | MVP 8–12 недель, прод-качество 3–4 месяца |
| Allowlist по умолчанию | AWG и INCY — да; Happ/v2rayNG — руками | Да | Да |
| Нет локального порта | AWG — да; INCY ❓; Happ — нет (пароль) | Да (Xray TUN) | Да |
| Защита от `tun0`-bind | Нет (у Happ заявлена, ❓) | Да, если реализовать UID-guard | Да |
| Fail-closed с allowlist | Нет | Да | Да |
| Все наши протоколы в одном | Нет, 2–3 приложения | VLESS-R, XHTTP, SS-2022, Hy2 | + AWG |
| Сопровождение | Почти ноль | 2–4 ч/неделю на пересборки под Xray | То же + AWG |
| Главный риск | Зависимость от чужих закрытых клиентов | Bus factor, ломающие изменения Xray (как ML-KEM в 26.9.8) | То же + объём работы |

**Рекомендация: A сейчас, B вторым этапом. C — только если B упрётся в архитектуру основы.** Режим рабочего профиля и Device Owner — эксперименты, не MVP.

---

## 5. Основа для форка

| Основа | Лицензия | Плюсы | Минусы | Вердикт |
|---|---|---|---|---|
| **SaeedDev94/Xray** 12.7.0 (02.10.2026) | MIT ✅ | Маленький, Kotlin, Android 8+, Xray 26.9.30, в F-Droid | Сейчас на hev-socks5-tunnel 2.18.0 (локальный SOCKS) — менять на Xray TUN | **Основная** ✏️ |
| **v2rayNG** 2.3.10 | GPL-3.0 ✅ | Уже есть per-app, режим Xray Core TUN, выключение локального прокси | Большой код, при сбое ядра снимает tun (fail-open) | Запасная ✏️ |
| OneXray v26.9.5 | GPL-3.0 | Flutter, Android + iOS | iOS у нас вне рамок; Xray 26.7.28 ❓ | Нет |
| CMFA v2.11.35 / FlClash | GPL-3.0 | mihomo: все 5 протоколов, включая AWG и TUIC | Переписанная реализация, отстаёт от эталонного Xray | Запасная, если AWG важнее |
| Hiddify | «Extended GPLv3» | — | Форк **обязан** быть публичным на GitHub ✏️; sing-box не проходит REALITY | Нет |
| NekoBox, Husi, Karing, SFA | разные | — | sing-box: VLESS-REALITY ❌ | Нет |
| AmneziaVPN | GPL | AWG | Qt/C++, тяжёлый | Нет |

---

## 6. Архитектура MVP «zoo-client» (Android 8+)

Это **дизайн**, а не проверенный факт (❓); технические опоры — ✅.

### Ядро и туннель
- **libXray** (MIT, gomobile, API 21+). Одно Go-рантайм на процесс ✅ → amneziawg-go на фазе 2 собирать в тот же Go-модуль, переключение — перезапуском движка.
- **Xray TUN по fd из VpnService**: fd передаётся в `env` конфига (`xray.tun.fd`), метода `SetTunFd` в libXray больше нет ✏️. **Ни одного inbound на loopback.**
- Риск: встроенный TUN Xray на Android глючит (петли, отказ sniffing — v2rayNG #5181) ✏️. Перед ставкой — тест; запасной вариант — Hev TUN со случайными паролем и портом.
- Протоколы MVP: VLESS-REALITY (ML-KEM), XHTTP, SS-2022, Hy2 с pinSHA256 (Hy2 outbound в Xray с v26.1.23) ✅. TUIC не нужен. AWG — фаза 2.
- Failover между outbound'ами — balancer + observatory Xray.

### Allowlist
- `addAllowedApplication` по пресету; **неустановленные пакеты пропускать** (иначе `NameNotFoundException` и `establish()` падает) ✅.
- Слушать `PACKAGE_ADDED` и переподнимать туннель, иначе новое приложение из пресета не попадёт в туннель ✅.
- Пресеты: «Браузер» (Brave), «Мессенджеры», «YouTube», «ИИ». Список приходит с сервера вместе с конфигом.

### Защита от утечек (требования)
- **UID-guard:** `getConnectionOwnerUid` (API 29+) для каждого нового соединения, `INVALID_UID` → drop ✅. Вариант через Xray: `RegisterAndroidProcessFinder` + правило «неизвестный процесс → block» ❓. **Тестировать на DNS и QUIC:** `INVALID_UID` возвращается и для ненайденных соединений, строгий отброс может рвать короткие UDP-потоки ✏️.
- **Не вызывать `excludeRoute`** к серверу (виден всем через `LinkProperties`) и **не вызывать `allowBypass`** ✅.
- Случайный адрес и MTU TUN, DNS не `127.x` ✏️ (отпечаток; что RU-приложения его снимают, не доказано).
- **Fail-closed внутри приложения:** при падении ядра держать tun поднятым и отбрасывать трафик (v2rayNG в этом случае снимает tun) ✅. Системный lockdown не включать — он ломает allowlist ✅.

### Маршрутизация (в конфиге с сервера)
- `geosite:category-ru`, `geoip:ru` → **direct** по умолчанию (разрешённый браузер открывает RU-сайты без адреса сервера); опция → block.
- `category-ads-all` → block, `category-ip-geo-detect` → block. Geo-данные runetfreedom (GPL-3.0, обновление каждые 6 ч).
- DNS разрешённых приложений — через туннель.

### Конфиг и обновления
- Импорт: `zoo://` или QR; JSON подписан **Ed25519**, публичный ключ зашит в APK. Генерирует фаза `zoo` инсталлера.
- **Телеметрии нет**, логи только локальные.
- Самообновление APK с нашего сервера. Обновление поверх возможно только тем же ключом подписи. Без подтверждения — только Android 12+ при `UPDATE_PACKAGES_WITHOUT_USER_ACTION` и требуемом targetSdk (растёт с версией Android: 34 на A16, 35 на A17); на Android 8–11 подтверждение нужно всегда ✏️.

### Против трекинга RU-приложений
Через тот же VPN не получится: только один VPN на устройстве, а в allowlist-режиме клиент не видит пакетов RU-приложений ✅. В клиенте можно дать **инструкцию-мастер**: Private DNS с блок-листом, ограничение фоновых данных, Shizuku-заморозка (как Anubis). Это опционально и не для MVP.

---

## 7. Что можно и чего нельзя без root

| Можно ✅ | Нельзя ✅ |
|---|---|
| Allowlist по умолчанию, пресеты с сервера | Скрыть `tun0` |
| Нет порта на loopback (Xray TUN) | Скрыть `TRANSPORT_VPN` в `getAllNetworks()` |
| Отбрасывать пакеты чужих UID (`tun0`-bind) | Спрятать VPN-сервис от `queryIntentServices` с `<queries>` (intent-filter обязателен; случайное имя пакета спасает только от каталогов по именам) |
| Fail-closed внутри приложения | Системный lockdown одновременно с allowlist |
| Случайные адрес/MTU TUN | Фильтровать трафик RU-приложений тем же VPN |
| Подписанный конфиг, автообновление | Тихое обновление на Android 8–11 |

**Эксперименты (❓, не для ленивых):**
- **Без intent-filter `android.net.VpnService`:** `establish()` ищет сервис по явному компоненту, по коду гипотеза не опровергнута, но always-on станет недоступен. Нужен тест на устройстве.
- **Рабочий профиль** (наш клиент — profile owner, как Shelter): always-on + lockdown только для профиля = настоящий kill-switch для браузера. Факт VPN из другого профиля **виден** (GrapheneOS #5225, #7511) ✏️. Один рабочий профиль на устройство, у части OEM работает с причудами.
- **Device Owner + `lockdownAllowlist`** (DPM, API 29): lockdown с исключениями. Ставится через ADB на устройство без аккаунтов (почти всегда после сброса), список сам не обновляется, системные приложения обходят VPN всегда ✏️.

С root (LSPosed, Hide My Applist, подмена `NetworkCapabilities`) факт VPN скрыть можно, но банки детектят root — для массовой аудитории не подходит ✅.

---

## 8. Трудозатраты по этапам (экспертная оценка ❓)

| Этап | Содержание | Оценка |
|---|---|---|
| 0 | Вариант A: Android-вариант AWG `.conf` с `IncludedApplications`, единый allowlist на сервере, инструкции | 2–5 дней |
| 1 | Форк SaeedDev94/Xray: ребрендинг, свой applicationId, ключ подписи, импорт `zoo://` с Ed25519 | 1–2 недели |
| 2 | Allowlist по умолчанию, пресеты, `PACKAGE_ADDED`, Xray TUN вместо Hev, без loopback | 1–2 недели |
| 3 | UID-guard, fail-closed, failover, маршрутизация из конфига, тесты на DNS/QUIC | 1–2 недели |
| 4 | Самообновление, генерация конфига фазой `zoo` в инсталлере | 3–5 дней |
| 5 | AWG через amneziawg-go в том же Go-модуле | 2–4 недели |
| Сопровождение | Пересборки под релизы Xray (26.9.8 и 26.9.9 вышли в один день, 26.9.30 через 3 недели) | 2–4 ч/неделю |

Этапы 1–4 ≈ 3–6 недель (вариант B). iOS вне рамок: нужен Apple Developer и Network Extension, из РФ это недоступно.

---

## 9. Лицензии, распространение, риски

### Лицензии ✅
- MIT: SaeedDev94/Xray, libXray. Apache-2.0: amneziawg-android. Обязательств нет.
- GPL-3.0: v2rayNG, mihomo, OneXray, InviZible, geo-данные runetfreedom. При раздаче APK даже узкому кругу — отдать исходники **получателям** (публиковать не обязательно).
- Hiddify: форк обязан быть публичным на GitHub — не подходит.

### Распространение ✅/✏️
- **RuStore исключён:** по RKS он сам собирает список установленных VPN-клиентов, публиковать там клиент обхода — значит светить его. Тезис «RuStore удаляет VPN-приложения» фактчекер не подтвердил: kod.ru/rustore-vpn — про собственный VPN RuStore ✏️.
- **Основной канал:** APK по ссылке с нашего сервера + зеркало (GitHub Releases).
- **Верификация разработчиков Google:** с 30.09.2026 только BR, ID, SG, TH; глобально — в 2027. Тогда для неверифицированного APK — «advanced flow»: перезагрузка, 24 ч ожидания, разрешение на 7 дней или навсегда. Установка через ADB освобождена. Аккаунт с ограниченным распространением (до 20 устройств) бесплатный, нужен только email ✏️ — но он привязывает пакет к личности.

### Риски
- **Ломающие изменения Xray** (ML-KEM в 26.9.8 сломал целый класс клиентов). Мы сами контролируем и сервер, и клиент — это скорее плюс, но пересобирать надо быстро.
- **Встроенный TUN Xray** на Android ещё сырой (#5181).
- **Bus factor:** один человек знает сборку; потеря ключа подписи = переустановка у всех.
- **Юридический** 🟡 (не юридическая консультация): КоАП 14.3 ч.18 — реклама средств обхода; 276-ФЗ касается владельцев VPN-сервисов. Практически: не публиковать, не продвигать, репозиторий приватный.
- **Ожидания пользователей:** клиент не прячет факт VPN — это должно быть в инструкции крупно.

---

## 10. Решения, нужные от владельца

1. **Этап 0 сейчас?** Добавить Android-вариант AWG `.conf` с `IncludedApplications` и единый allowlist-файл на сервере.
2. **Доводить публичную подписку** (домен + TLS), чтобы INCY и Happ получали заголовки? Это отдельный открытый порт.
3. **INCY допустим** как основной VLESS-клиент, несмотря на закрытый код и Crashlytics?
4. **Пресет allowlist:** какие приложения кроме Brave и Telegram.
5. **Свой клиент — да/нет и когда.** Если да: основа SaeedDev94/Xray (MIT) или v2rayNG (GPL, больше готового).
6. **AWG в своём клиенте** нужен (+2–4 недели) или достаточно отдельного AmneziaWG/WG Tunnel?
7. **Канал раздачи APK** и кто хранит ключ подписи (с резервной копией).
8. **Эксперименты** (без intent-filter, рабочий профиль, Device Owner) — тестировать на своём телефоне или отложить.

---

## Источники

- VpnService.Builder: https://developer.android.com/reference/android/net/VpnService.Builder
- VpnService: https://developer.android.com/reference/android/net/VpnService
- ConnectivityManager.getConnectionOwnerUid: https://developer.android.com/reference/android/net/ConnectivityManager
- DevicePolicyManager: https://developer.android.com/reference/android/app/admin/DevicePolicyManager
- PackageInstaller.SessionParams: https://developer.android.com/reference/android/content/pm/PackageInstaller.SessionParams
- Vpn.java (AOSP): https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/connectivity/Vpn.java
- Xray TUN: https://github.com/XTLS/Xray-core/blob/main/proxy/tun/README.md; v26.1.23: https://github.com/XTLS/Xray-core/releases/tag/v26.1.23; релизы: https://github.com/XTLS/Xray-core/releases
- Xray routing (process): https://xtls.github.io/en/config/routing.html
- libXray: https://github.com/XTLS/libXray
- SaeedDev94/Xray: https://f-droid.org/packages/io.github.saeeddev94.xray/
- v2rayNG: https://github.com/2dust/v2rayNG, https://github.com/2dust/v2rayNG/issues/5181
- OneXray: https://github.com/OneXray/OneXray
- mihomo: https://github.com/MetaCubeX/mihomo/releases; CMFA: https://github.com/MetaCubeX/ClashMetaForAndroid/releases; FlClash: https://github.com/chen08209/FlClash
- Hiddify LICENSE: https://raw.githubusercontent.com/hiddify/hiddify-app/main/LICENSE.md
- amneziawg-android: https://github.com/amnezia-vpn/amneziawg-android
- Amnezia #2457, PR #3199: https://github.com/amnezia-vpn/amnezia-client/issues/2457, https://github.com/amnezia-vpn/amnezia-client/pull/3199
- Happ: https://docs.happ.info/main/dev-docs/app-management, https://docs.happ-proxy.com/ru/getting-started/provider-id, https://docs.happ-proxy.com/ru/getting-started/premium-functionality, https://kod.ru/vpn-happ-problems
- RethinkDNS: https://docs.rethinkdns.com/proxy/wireguard/; InviZible: https://github.com/Gedsh/InviZible/releases
- Anubis: https://github.com/sogonov/anubis, https://habr.com/ru/articles/1023352/
- GrapheneOS #5225, #7511: https://github.com/GrapheneOS/os-issue-tracker/issues/5225, https://github.com/GrapheneOS/os-issue-tracker/issues/7511
- RKS Global: https://rks.global/ru/research/vpn-detection/
- runetfreedom geo: https://github.com/runetfreedom/russia-v2ray-rules-dat
- Верификация разработчиков Google: https://android-developers.googleblog.com/2026/03/android-developer-verification-rolling-out-to-all-developers.html, https://www.androidauthority.com/google-android-advanced-flow-sideloading-rollout-begins-3700073/
- RuStore: https://kod.ru/rustore-vpn
- Root-скрытие: https://habr.com/ru/articles/1024890/

Сырые данные — [raw/clients-and-own-app.json](raw/clients-and-own-app.json).
