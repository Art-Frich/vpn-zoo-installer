# Совместимость ядра Hiddify (и sing-box) с нашим сервером: настоящий прогон на стенде

**Дата:** 2026-10-07
**Для кого:** владелец `vpn-zoo-installer` и ИИ-ассистент, который правит `zoo/data/clients.json` и подбор клиентов.
**Контекст:** в `clients-and-allowlist_04-10-26` Hiddify и sing-box получили статусы «почти наверняка ❌ / не проверено» по чужим отчётам и коду. Здесь те же протоколы прогнаны настоящим ядром Hiddify против настоящего сервера инсталлера. Приложения (Flutter-оболочка, TUN, Android/iOS) **не** запускались: проверено именно ядро.

## 0. Как собрано

Стенд `zoo-hid` (Ubuntu 24.04 в Docker, `docker/test.sh --mode full --env ENABLE_SS=1 --env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1 --keep`, установка 127 с, самопроверка PASS). Серверные версии: Xray 26.9.30 (3x-ui v3.9.0), Hysteria 2.12.3, sing-box 1.14.2 (только TUIC), AmneziaWG 2.0 (профиль `v2`: Jc/Jmin/Jmax, S1–S4, H1–H4, I1).

Клиент — отдельный контейнер в `zoo-net` с закрытым прямым выходом (iptables: наружу только адрес сервера), поэтому успех возможен только через туннель. Ссылки — те, что zoo выдаёт пользователю `owner` (`protocols.d/<id>.json`, `links[]`). Для каждой пары «ядро × протокол» ядро стартует с mixed-входом на `127.0.0.1`, затем три запроса через него (`curl -x socks5h://…`):

1. `https://www.cloudflare.com/cdn-cgi/trace`: строка `ip=` равна IP выхода сервера;
2. `https://www.gstatic.com/generate_204`: код 204;
3. `https://speed.cloudflare.com/__down?bytes=2000000`: ровно 2 000 000 байт.

PASS — все три; один неудачный запрос — FAIL. Прогон матрицы повторён чистым скриптом после разбора ошибок (порт освобождается после каждого теста, проверено), результаты совпали с первым прогоном. Контрольный образец: клиент Xray 26.9.30 из `docker/tests/vless-reality.sh` и `vless-xhttp.sh` на том же сервере — PASS (в том числе XHTTP).

### Версии

| Что | Версия | Откуда |
|---|---|---|
| Hiddify (приложение) | 4.1.1 от 05.03.2026 — последняя стабильная; на 07.10 новее только pre-release `draft` от 04.10 | GitHub releases hiddify-app |
| hiddify-core | **v4.1.0**, commit `c9d6f0f0`, `hiddify-sing-box version 1.13.1`, go1.25.7 | `hiddify-core version`; сабмодуль `hiddify-core` в теге hiddify-app v4.1.1 указывает ровно на этот commit. Скачано `hiddify-core-linux-amd64-glibc.tar.gz` (sha256 `ba5a8bfb…6e2e`), запуск как CLI: `hiddify-core run -c <файл со ссылкой> --in-proxy-port N` |
| sing-box 1.13.1 | upstream, sha256 `e68f9a19…1cb9` (= digest ассета GitHub) | та же минорная версия, что в форке Hiddify (1.13.1) |
| sing-box 1.14.2 | upstream, sha256 `a684484d…a0c6` (пин `versions.env`) | текущий стабильный |
| sing-box 1.15.0-alpha.10 | upstream, sha256 `c17da275…d86d`, только REALITY, XHTTP, AWG | последний pre-release на 07.10 |

Ядро Hiddify сразу понимает ссылки (`vless://`, `hysteria2://`, `tuic://`, `ss://`): разбор — `ray2sing`, итоговый конфиг пишется в `data/current-config.json`. Для sing-box 1.13.1 и 1.14.2 конфиги собраны по документации sing-box из тех же ссылок (у sing-box ядра разбора ссылок нет).

## 1. Результаты

Ячейка: ✓ — PASS (три запроса), ✕ — FAIL. Текст — что показало ядро.

| Протокол | hiddify-core 4.1.0 (sing-box 1.13.1) | sing-box 1.13.1 | sing-box 1.14.2 | Как проверено |
|---|---|---|---|---|
| **vless-reality** (Xray 26.9.30, Vision, ML-KEM-подсказка в ссылке) | ✕ `reality verification failed` | ✕ то же | ✕ то же; 1.15.0-alpha.10 тоже ✕ | ссылка → ядро → curl |
| **vless-xhttp** (REALITY) | ✕ `connection download handshake: read payload: io: read/write on closed pipe` | ✕ `unknown transport type: xhttp` | ✕ то же; 1.15.0-alpha.10 тоже ✕ | то же |
| **hysteria2** | ✓ | ✓ | ✓ | ссылка с `insecure=1&pinSHA256=…` |
| **hysteria2-obfs** (Salamander) | ✓ | ✓ | ✓ | то же + `obfs=salamander` |
| **tuic** (v5, bbr, native, h3) | ✓ | ✓ | ✓ | ссылка / конфиг по ссылке |
| **ss2022** (`2022-blake3-aes-128-gcm`) | ✓ | ✓ | ✓ | то же |
| **amneziawg** (2.0: Jc, S1–S4, H1–H4, I1) | ✕ не поддерживает | ✕ не поддерживает | ✕ не поддерживает (и 1.15.0-alpha.10) | конфиг с `endpoints[].amnezia` и импорт `.conf` |

### 1.1 VLESS-REALITY: ✕ у всех sing-box, причина известна

Ядро Hiddify, upstream 1.13.1, 1.14.2 и 1.15.0-alpha.10 отказываются одинаково: клиент пишет `reality verification failed`, сервер ответил заглушкой с `www.samsung.com`. Это [SagerNet/sing-box#4520](https://github.com/SagerNet/sing-box/issues/4520) (открыт, на 24.09 без исправления): Xray с 26.9.8 (REALITY commit `8cdf7bf9c7f0`) отвергает ClientHello без `X25519MLKEM768` перед обычным X25519, а `chrome` из uTLS у sing-box такого hello не строит. Перебор `fp=` на sing-box 1.14.2 (`chrome`, `firefox`, `safari`, `ios`, `android`, `edge`, `random`, `randomized`, `360`, `qq`, `chrome_psk`, `chrome_pq`): ни один не проходит, обойти заменой отпечатка нельзя. Параметр ссылки `support-x25519mlkem768=true` ядро Hiddify игнорирует (в конфиге его нет).

### 1.2 XHTTP: у sing-box его нет вообще; у Hiddify есть транспорт, но он не проходит

В прошлом исследовании XHTTP был отнесён к тем же клиентам «из-за ML-KEM». Правка: у upstream sing-box (1.13.1, 1.14.2, 1.15.0-alpha.10) транспорта `xhttp` нет, конфиг не читается. У Hiddify транспорт есть (`"transport": {"type": "xhttp", "mode": "auto"}`), но запрос обрывается после рукопожатия. Что это та же причина (REALITY), не изолировано: отдельного XHTTP-входа без REALITY на стенде нет.

### 1.3 Hysteria2: работает, но Hiddify не проверяет пин

Конфиг, который Hiddify строит из ссылки: `tls: {server_name: "bing.com", insecure: true}`, пина нет. Проверки:

| Ссылка | hiddify-core | sing-box 1.14.2 (ссылка → конфиг по `insecure`) |
|---|---|---|
| `insecure=1`, верный `pinSHA256` (то, что выдаёт zoo) | ✓ | ✓ |
| `insecure=1`, **неверный** `pinSHA256` | ✓ (пин не проверяется) | не проверялось |
| только верный `pinSHA256`, без `insecure` | ✕ `x509: certificate signed by unknown authority` | ✕ то же |

То есть Hiddify подключается только благодаря `insecure=1`, сертификат сервера не проверяется (активный перехват возможен). Это совпадает со строкой в `99-print-creds.sh` («Hiddify игнорируют пин»). У sing-box пин Hysteria (sha256 сертификата) не понимается, но есть SPKI-пин в JSON: `tls.certificate_public_key_sha256` (base64 sha256 SPKI). Проверено на 1.13.1 и 1.14.2: верный SPKI ✓, неверный ✕. Через ссылку это не передать.

### 1.4 AmneziaWG: не поддерживается

- Конфиг `endpoints[0].amnezia {jc, jmin, jmax, s1…s4, h1…h4, i1…}`: hiddify-core 4.1.0 и sing-box 1.13.1 / 1.14.2 / 1.15.0-alpha.10 отвечают `json: unknown field "amnezia"`.
- Источник по коду: в `option/wireguard.go` форка hiddify-sing-box (сабмодуль `0a02b772` из hiddify-core v4.1.0 и main на 05.10) у `WireGuardEndpointOptions` полей Amnezia нет; файл `examples/amnezia/client.json` в репозитории форка с этим кодом не работает. Документация upstream wireguard-endpoint про Amnezia не говорит.
- Обычный WireGuard без обфускации к нашему серверу не подключается: sing-box 1.14.2 повторяет `handshake did not complete after 5 seconds`.
- Импорт `.conf` напрямую в `hiddify-core run -c`: `invalid AllowedIPs … unable to determine config format`.

## 2. Приложение Hiddify (по коду v4.1.1, не по запуску)

| Что | Факт | Источник |
|---|---|---|
| Приложения через VPN (Android) | Есть, только Android (`PlatformUtils.isAndroid`): режимы `off` / `include` (только выбранные) / `exclude`; путь «Настройки → Маршрутизация → Прокси для приложений», режим «Прокси»; импорт и экспорт выбора через буфер и файл | `lib/features/per_app_proxy/*`, `assets/translations/ru.i18n.json` в теге v4.1.1 |
| Импорт | Ссылки общего доступа из буфера, подписка по URL, QR («Сканировать QR») | ядро разбирает ссылки (раздел 1); кнопки — по переводам, на устройстве не нажимались |
| Android | Google Play `app.hiddify.com` (страница отвечает 200 на 07.10), APK на GitHub | README, GitHub releases |
| iOS | Только App Store США (`id6596777532`, v4.0 от 19.02.2026, «Holistic Resilience»); поиск и lookup по магазину РФ возвращают 0. Сборка на 4.0, а не 4.1.1: ядро iOS-версии не проверялось | `itunes.apple.com/lookup?country=ru|us` |
| Windows, macOS, Linux | GitHub releases: Setup/Portable/msix, dmg/pkg, AppImage/deb (+ Microsoft Store по README) | assets релиза 4.1.1 |

Попутно: в документации sing-box у SFI для iOS ссылка на App Store зачёркнута («временно не можем обновлять»), доступен TestFlight для спонсоров; поиск по id `6673731168` в магазинах РФ и США даёт 0; SFA (Android) — Google Play, GitHub, F-Droid. SFA импортирует JSON-профиль (файл, удалённый профиль), разбора ссылок `vless://`/`hysteria2://` в `ProfileImportHandler.kt` нет. Ещё попутно: приложение AmneziaWG (`id6478942365`, Privacy Technologies OU) находится в App Store РФ — в каталоге клиентов у iOS оно не значится.

## 3. Что из этого следует для каталога

- Hiddify: Hysteria2, Hysteria2+Salamander, TUIC, SS-2022 работают на ядре 4.1.0; VLESS-REALITY и XHTTP — нет (подтверждено, было «почти наверняка»); AmneziaWG — нет. У Hysteria2 пометка «с оговоркой»: пин не проверяется.
- sing-box (ядро SFA/SFI): те же четыре работают; VLESS нет; XHTTP нет в принципе. Но SFA не берёт ссылки: нужен JSON-профиль, которого zoo не выдаёт.
- Hiddify — единственный клиент каталога, который на всех десктопах и Android одной программой принимает ссылки Hysteria2, Salamander, TUIC и SS-2022; поэтому для групп без VLESS и AmneziaWG подбор должен предпочитать его (одно приложение на устройствах).

## 4. Не проверено

- Само приложение Hiddify (GUI, TUN, импорт кнопками), Android, iOS (v4.0), macOS, Windows; режим TUN и UDP через туннель (проверены TCP-потоки).
- Сеть РФ и ТСПУ (стенд без цензора).
- hiddify-core main (05.10.2026, не выпущен): в `go.mod` появился `xray-core v1.260327…`, возможно будущий REALITY через Xray; не собирался и не запускался.
- XHTTP у Hiddify без REALITY (нет отдельного входа); порт-хоппинг Hysteria2.
- SPKI-пин у sing-box проверен только как JSON на ядре, не в SFA.

## 5. Как повторить

```bash
bash docker/test.sh --mode full --distro 24.04 --name hid --probe-profiles none --keep \
  --env ENABLE_SS=1 --env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1
# клиент: контейнер в zoo-net, iptables как в docker/tests/_xui-proto-e2e.sh, внутри hiddify-core,
# sing-box-1.13.1, sing-box-1.14.2; ссылки owner — из /etc/vpn-setup/protocols.d/<id>.json
hiddify-core run -c ./vless-reality.link --in-proxy-port 12334 &
curl -x socks5h://127.0.0.1:12334 https://www.cloudflare.com/cdn-cgi/trace
```

Источники: [hiddify-core v4.1.0](https://github.com/hiddify/hiddify-core/releases/tag/v4.1.0), [hiddify-app v4.1.1](https://github.com/hiddify/hiddify-app/releases/tag/v4.1.1), [sing-box#4520](https://github.com/SagerNet/sing-box/issues/4520), [XTLS/REALITY 8cdf7bf9c7f0](https://github.com/XTLS/REALITY/commit/8cdf7bf9c7f09cb9814bf08c3eb877f68b85fba8), [sing-box 1.14.2](https://github.com/SagerNet/sing-box/releases/tag/v1.14.2), [sing-box docs: клиенты](https://sing-box.sagernet.org/clients/).
