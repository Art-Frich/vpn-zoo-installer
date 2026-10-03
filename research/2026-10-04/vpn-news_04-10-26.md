# VPN-news 04.10.2026: что изменилось и как обновлять vpn-zoo-installer

**Дата среза:** 2026-10-04. **База репо:** единственный коммит от 2026-04-27 (3x-ui latest, VLESS+REALITY+Vision на 8443/tcp, Hysteria2 на 443/udp, AmneziaWG «v2» на 51822/udp, Ubuntu 22.04/24.04).

**Как собрано.** Рой исследователей разобрал 8 тем: Xray/REALITY, 3x-ui и панели, Hysteria2, AmneziaWG, ТСПУ в РФ, мировые протоколы и исследования, клиенты, серверная эксплуатация. По каждой теме отдельный фактчекер сверил утверждения с первоисточниками: коммитами, исходниками на тегах, release notes, статьями FOCI/USENIX, Launchpad, GitHub API. После этого 6 gap-fill-агентов закрыли противоречия: совместимость клиентов с ML-KEM, API 3x-ui v3.9.0, выбор REALITY-target и порта, XHTTP-конфиг, ссылку Hy2 для разных клиентов, профиль AWG 3.1 и матрицу DKMS.

**Дополнительно** работали ещё два роя. Первый — по новым и ожившим протоколам: 4 угла поиска, 44 кандидата, 24 проверены поштучно (§5.5). Второй — повторный проход по ожившей классике: ещё 24 техники с проверкой (§5.6). Отдельный документ по единой админке (все протоколы, учётки, трафик, метрики сервера): [admin-panel_04-10-26.md](admin-panel_04-10-26.md).

**Легенда статусов**

| Знак | Значение |
|---|---|
| ✅ | подтверждено фактчекером по первоисточнику |
| ✏️ | исправлено фактчекером. В тексте уже стоит исправленная версия, на неё и опираться |
| ❓ | не проверено: один источник, форумный отчёт или вывод из кода без живого теста |
| 🔬 | gap-fill: проверено по коду или замеру, но одним агентом. Уверенность высокая там, где сказано «по коду» |

Утверждения с вердиктом *refuted* в основной текст не вошли. Самые заметные из них собраны в §5.4 «Мифы».

---

## 1. TL;DR (15 пунктов)

> **Калибровка: прочитать перед списком.** Источники роя — в основном форумы, issue-трекеры и каналы о блокировках (ntc.party, habr, net4people, GitHub issues), а сами запросы были сформулированы через «что заблокировано и как детектят». Такие источники систематически перекошены в негатив: о сбоях пишут, о том, что работает, молчат. Реальность рынка этому противоречит: десятки коммерческих сервисов стабильно продают VPN на все устройства в РФ на тех же протоколах (VLESS/REALITY, XHTTP, Hysteria2, AWG). Они закрепляют версии, держат несколько транспортов, ротируют IP и используют входные ноды в РФ. Hysteria2 владельца репо стабильно работает на мобильном интернете (10.2026).
>
> Поэтому пункты ниже делятся на три класса:
> - **[репо]** — дефекты именно этого инсталлера: код, версии, безопасность. Это факты, их надо чинить (п. 1, 6, 11, 12, 13, 14).
> - **[версия]** — проблема появляется, только если ставить `latest` и pre-release. Закрепление проверенных версий снимает её целиком (п. 2–5).
> - **[среда]** — ландшафт блокировок. Это **операционные издержки** (несколько транспортов, выбор хостинга, ротация IP, каскад для белых списков), а не «VPN в РФ не работает». Отдельные цифры взяты из пользовательских репортов, на конкретной сети всё может быть иначе (п. 7–10, таблица §2.3).

1. **Фаза 04 сломана на любой свежей установке.** ✅ С 3x-ui **v3.1.0 (2026-05-23)** колонку `inbounds.all_time` удаляют, а клиенты переехали в таблицы `clients` и `client_inbounds`. Xray-конфиг собирается из этих таблиц, а не из `settings.clients`. Поэтому `INSERT` из `04-vless-reality.sh` либо падает с ошибкой `no column all_time`, либо создаёт inbound без пользователей. Правильный путь: `POST /panel/api/inbounds/add` с Bearer-токеном. Он сам вызывает `SyncInbound` и `AddClientStat` ([inbound.go v3.9.0](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound.go)) 🔬.
2. **[версия] 3x-ui latest = v3.9.0 (2026-10-03), внутри Xray v26.9.30 (pre-release).** ✅ Касается только сервера, на который через `latest` попало pre-release ядро; на закреплённом стабильном ядре этого нет. С Xray **v26.9.8** REALITY молча отбрасывает ClientHello без `X25519MLKEM768` ([REALITY 8cdf7bf](https://github.com/XTLS/REALITY/commit/8cdf7bf9c7f09cb9814bf08c3eb877f68b85fba8)). Клиенты на **sing-box (Hiddify, Karing, NekoBox, SFA/SFI, sing-box ≤1.15.0-alpha.10)** и **Shadowrocket 2.2.92** к VLESS-REALITY **не подключаются**. Пользователь видит «подключено, но ничего не грузится» ([sing-box#4520](https://github.com/SagerNet/sing-box/issues/4520), open) 🔬. Работают Xray-клиенты (v2rayN/NG, Happ, INCY, Streisand) с `fp=chrome`, а также mihomo с `support-x25519mlkem768=true`.
3. **Откатиться на Xray v26.3.27 ради sing-box нельзя.** 🔬 Панель v3.9.0 разрешает выбрать только ядра ≥ v26.6.27, а дефолтный API-инбаунд использует `rewriteAddress`, которого нет до v26.5.9. Оставаться на v26.9.30. Если REALITY для sing-box обязателен, единственный вариант — **v26.6.27** через переключатель панели.
4. **[версия] Ссылка Hy2 `?insecure=1&sni=bing.com` не работает в Xray-клиентах с новым ядром** (v2rayNG 2.3.x, v2rayN 7.25). В sing-box-клиентах (Hiddify, NekoBox, Karing) и в официальном клиенте Hysteria она работает. Добавление пина только расширяет совместимость и рабочие клиенты не ломает. ✅ `allowInsecure` удалён (Xray v26.2.6, жёсткий отказ с 2026-06-01). Универсальная ссылка, проверенная по коду 7 клиентов: `hysteria2://PASS@IP:443/?sni=bing.com&insecure=1&pinSHA256=<hex64 lowercase>` 🔬.
5. **REALITY на 8443 и target www.cbr.ru — оба против прямых предупреждений Xray.** ✅ С v26.3.27 ядро пишет warning на любой REALITY не на 443, с v26.7.28 — на SNI `*.ru/.ir/.cn` и `apple/icloud/microsoft`. Нужно перенести на **443/tcp** (с Hy2 на 443/udp не конфликтует) и сменить target. ✏️ Геоблока у cbr.ru нет: из DE он отвечает TLS1.3+X25519+h2. Убирать его надо из-за несоответствия SNI и ASN (anycast DDOS-GUARD RU), warning ядра, отсутствия ML-KEM и цепочки 3479 Б.
6. **Панель по HTTP на публичном :2053 — фактор бана и утечки.** ✅ RPRX прямо называет HTTP-панели причиной блокировок. Плюс 8 GHSA и 2 CVE у 3x-ui за 2025–2026. Решение: `x-ui setting -listenIP 127.0.0.1`, доступ через `ssh -L`, правило 2053 из UFW убрать.
7. **[среда] ТСПУ 2025–2026 бьёт по поведению и инфраструктуре, а не по сигнатуре протокола.** Эффект сильно зависит от провайдера, региона и хостинга. ✅ Заморозка TCP после ~16 КБ к зарубежным ЦОД (с 09.06.2025). Заморозка на ~120 с при >3 параллельных TLS-рукопожатиях к одному SNI (порог ❓ гипотеза). Лимит одновременных соединений урезан с 12 до 4 (Москва, июль 2026, ❓ пользовательский репорт). Больше всего страдает схема VLESS RAW+Vision «1 TCP на соединение».
8. **[среда] Массовые баны IP 04.08 и 21.09.2026** ✅ — точечно, по отдельным хостингам; коммерческие сервисы решают это ротацией IP. задели популярные VPS (Veesp ~5k, FirstByte ~3k, Play2Go ~2k, Fornex 596, Amnezia Hosting 131 IP), включая self-steal REALITY и серверы, где стоял только AmneziaWG. Протокол от бана IP не спасает. Нужны rotate/migrate и правильный выбор хостинга.
9. **[среда] Мобильные «белые списки» стали постоянным режимом** ✅ — включаются по регионам и периодам, а не везде и не всегда (default-deny по SNI и CIDR, в марте 2026 — в 68–71 регионе). Пока режим активен, **прямое** подключение к зарубежному IP не проходит. Стандартное решение рынка — входная нода в РФ (каскад). Это обычная практика сервисов, а не экзотика; для self-hosted она требует второго VPS ❓.
10. **Нужен второй TCP-транспорт: VLESS+XHTTP+REALITY.** ✅ Дефолт XMUX `maxConnections=3` «for anti-TSPU» с v26.7.28. flow `""`. Варианты: fallback с master-443 в loopback-child или отдельный IP:443 🔬.
11. **AmneziaWG — уже 3.1, а не 2.0.** ✅ PPA раздаёт kmod 3.1 (`4569c4c` = v3.1.20260906), хотя в версии пакета написано `1.0.0`. Статичные H/S/I в репо одинаковы у всех установок, это готовая сигнатура. Генерировать параметры при каждой установке. **HPK и RandomTrailers на kmod-сервере по умолчанию не включать** 🔬 (баги kmod#222, #226 и go#186 открыты).
12. **«AWG DKMS требует ядро ≥6.2» — миф.** ✅ Реальная проблема в другом: ядро **7.0.0-38** (24.04.5 HWE и 26.04, в -updates с 2026-10-01) **ломает сборку DKMS** ([kmod#259](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/259), open) 🔬. Фазе 02 нужна защита от этого ядра, а не HWE «на всякий случай».
13. **Hysteria: v2.12.3 (2026-09-16), репозиторий переехал в HyNetworks/hysteria.** ✅ `05-hysteria2.sh` никогда не обновляет бинарь. Серверы старше v2.8.2 ломают UDP у новых клиентов, DoS/SSRF закрыты в 2.9.2. Нужно закрепить версию. Опционально: Salamander, port hopping, ACME.
14. **sysctl в 00-bootstrap содержит баги.** ✅ `udp_mem` задан в страницах, а не в байтах: получается 32–64 ГиБ, то есть защиты нет. `nf_conntrack_max` не применяется при загрузке. ✏️ `ipv6.conf.all.forwarding=1` опасен на хостах, где RA обрабатывает ядро. Под netplan/networkd, скорее всего, безвреден, но не нужен.
15. **В клиентских инструкциях нужно учесть реалии РФ.** ✅ Из RU App Store 28.03.2026 удалены Streisand, V2Box, v2RayTun, Happ. С 01.04.2026 в РФ отключены платежи Apple. С 07.04.2026 известна уязвимость неавторизованного локального SOCKS5: через неё раскрывается IP сервера. Импорт `.conf` в AmneziaVPN работает только с **5.0.1.5+**.

---

## 2. Ландшафт блокировок в РФ

### 2.1 Хронология

| Дата | Событие | Статус |
|---|---|---|
| 02–03.2022 | Дроп всего международного QUIC; широкий фильтр снят примерно 2022-06-03 | ✏️ [FOCI 2026](https://www.petsymposium.org/foci/2026/foci-2026-0010.pdf) |
| 05.2022–07.2023 | Переход на SNI-фильтрацию QUIC v1 Initial (июль 2023 — нижняя граница, «как минимум с») | ✏️ |
| 11.2023 | РКН объявил о блокировке Shadowsocks на трансграничных соединениях | ❓ |
| 2024-11-05 | Блок Cloudflare ECH (SNI `cloudflare-ech.com` + расширение ECH), в том числе в QUIC | ✅ [net4people#417](https://github.com/net4people/bbs/issues/417) |
| 05–06.2025 | Первые региональные отключения мобильного интернета с белыми списками | ✅ |
| 2025-06-09 | «Заморозка» TCP/TLS к ASN зарубежных ЦОД после ~15–20 КБ (~25 пакетов, затрагивает и UDP) | ✅ [#490](https://github.com/net4people/bbs/issues/490), [Cloudflare](https://blog.cloudflare.com/russian-internet-users-are-unable-to-access-the-open-internet/) |
| лето 2025 | AWG 1.0 перестал работать, ответом стали AWG 1.5, затем 2.0 (09.2025) | ✅ |
| 2025-09-01 | Запрет рекламы VPN; ст. 13.53 КоАП (поиск экстремистских материалов, 3–5 тыс. ₽); № 281-ФЗ | ✅ |
| 11.2025 | Первые волны против VLESS. С 12.11 у MTS/MGTS рвётся vision+443, помогает смена порта или mux | ✅ [#546](https://github.com/net4people/bbs/issues/546) |
| 12.2025 | Начало active probing VPS: поддельные сертификаты Yandex/Google/Apple, сервисы на нестандартных портах | ✏️ [Habr 1084862](https://habr.com/ru/articles/1084862/) |
| 2026-02-16 | Хабр: ограничены 72 зарубежных AS (Hetzner, OVH, DO, AWS, CF, Contabo, Vultr, Oracle), для них действует белый список SNI, пострадали 391 AS | ✏️ [Habr 997088](https://habr.com/ru/articles/997088/) (72 AS — это *ограничиваемые* сети, а не allowlist) |
| 2026-02-17 | VLESS+Reality определяют по косвенным признакам: несоответствие SNI и источника, нетипичные паттерны | ✅ [Теплица](https://te-st.org/2026/09/30/vlessmore/) |
| конец 02.2026 | По закону 2017 года заблокировано 469 VPN-сервисов | ✅ |
| 06–13.03.2026 | Отключение мобильного интернета в Москве; на 12.03 режим действует в 68–71 регионе | ✏️ |
| 2026-03-28 | Apple удалила из RU App Store Streisand, V2Box, v2RayTun, Happ — Proxy Utility | ✅ |
| 2026-03-30 | Фильтрация Snowflake DTLS по отпечатку | ✅ [#603](https://github.com/net4people/bbs/issues/603) |
| 2026-04-01 | В РФ отключены все платежи Apple | ✅ |
| 2026-04-07 | runetfreedom раскрыл неавторизованный локальный SOCKS5 в клиентах: можно узнать выходной IP | ✅ [Habr 1020080](https://habr.com/ru/articles/1020080/) |
| 2026-04-15 | Дедлайн Минцифры: >20 крупных компаний должны детектировать VPN. Уже 30 из 30 популярных приложений это делают | ✏️ [RKS Global](https://rks.global/ru/research/vpn-detection/) |
| 20.05–07.2026 | Блокировки IP и DDoS (до ~500k rps) против Amnezia | ❓ |
| 21–25.05.2026 | Волна против TLS-хендшейка VLESS на 443 (Сибирь, ДВ, Москва) | ✅ |
| 2026-05-28 | RPRX: в РФ ограничено число TCP-соединений для «полностью стандартного Chrome 133», схема «XTLS flow 1:1 больше не подходит» | ✅ [#6181](https://github.com/XTLS/Xray-core/pull/6181#issuecomment-4567373533) |
| 2026-06-05 | Волна: MTProto-прокси и российские хостеры (Beget, Timeweb, Selectel, Reg.ru) | ❓ |
| 2026-06-10/26 | «Антифрод 2.0» (№ 210-ФЗ): хостерам запрещено обслуживать нарушителей ст. 15.8. Вступает поэтапно с 01.09.2026 | ✏️ |
| 2026-06-24/29 | Удалён Happ Plus, затем вернулся как «Happ — Proxy Utility+» | ✏️ |
| 7, 10, 25.07.2026 | Волны против AWG 2.0, ответом стал AWG 3.0/3.1 | ✅ |
| 2026-07-10 | Фильтрация распространена на роуминговые SIM/eSIM | ✏️ |
| с 2026-07-16 | Перебои с App Store в РФ (OONI ~50% на 20.07); РКН отрицает причастность | ✏️ |
| 2026-07-28 | Репорт: лимит одновременных соединений урезан с 12 до 4, бан на несколько минут, в том числе в Москве | ❓ [#6376](https://github.com/XTLS/Xray-core/issues/6376#issuecomment-5101210849) |
| 2026-08-04 | Массовый бан IP популярных VPS (по HRW, затронуто 20 VPN-провайдеров) | ✅ [#671](https://github.com/net4people/bbs/issues/671) |
| 2026-09-21/22 | Вторая волна массовых банов, после выборов | ✅ |
| 2026-10-01 | Минцифры выносит на обсуждение «Антифрод 3.0»: реестр РКН, годовой запрет на российский хостинг, ввод с 03.2028 | ✏️ |
| 2026-10-02 | Отчёт: TCP к VPS заблокирован по всей РФ, UDP и ICMP при этом работают | ❓ #671 |

Также: плату за международный мобильный трафик сверх 15 ГБ откладывали несколько раз, сейчас обсуждают вариант для 5G (~50 ГБ), дата не определена ✅. Минцифры планирует нарастить ТСПУ до 954 Тбит/с к 2030 году ✅.

### 2.2 Механизмы ТСПУ (что важно для инсталлятора)

- **Заморозка ~16 КБ** ✅: TCP (и UDP, по [#490 upd5](https://github.com/net4people/bbs/issues/490)) к ASN зарубежных ЦОД, без RST. Обход: SNI или Host из белого списка, на мобильных — IP из белого CIDR. ✏️ Утверждение «AWS и Fastly не затронуты» не подтверждено.
- **Поведенческий «сибирский» модуль** ❓ ([Habr 1047442](https://habr.com/ru/articles/1047442/), один источник, автор сам называет это гипотезой): логическое И трёх условий — подозрительный ASN, синтетический uTLS-Chrome, больше 3 параллельных TLS к одному SNI за ~350–400 мс. Итог: заморозка ~120 с с эскалацией до 600 с. ✏️ Тезис «UDP этим не задевается» касается только эвристики хендшейков. Заморозка 16 КБ бьёт и по UDP.
- **QUIC** ✅ (FOCI 2026): расшифровка Initial QUIC v1, дроп по SNI-блоклисту (≈ блоклист TLS), на любых портах (в СПб и Новосибирске все порты, в Москве 22% системных портов не фильтруются). Residual-блок 4-tuple на 420 с, каждый пакет сбрасывает таймер. QUIC v2 не трогается. Фильтруется только исходящий трафик. ✏️ «Residual clearance» (чистый первый пакет — поток дальше не проверяется) сейчас работает только для отдельных доменов (31 паттерн).
- **Active probing** ✏️: с 12.2025 сканируют VPS. Красные флаги: поддельные сертификаты брендов, «подозрительные» сервисы на нестандартных портах (пример — 2096, это порт подписок 3x-ui), fallback, не похожий на настоящий веб-сервер. Упоминание именно 3x-ui и порта 2053 — экстраполяция.
- **Сбор IP через приложения** ✅: российские приложения детектят VPN, а через локальный SOCKS5 клиента могут узнать exit-IP. HRW связывает эти данные с атаками на инфраструктуру. Каким каналом собраны IP серверов, где был только AWG, не установлено ❓.

### 2.3 Что работает и что нет (октябрь 2026)

> ⚠️ Таблица собрана из форумных и пользовательских отчётов о **проблемах**, поэтому показывает худшие случаи, а не типичную картину. Ячейка «❌»/«плохо» значит «есть отчёты о сбоях», а не «не работает у всех». Полевой контрпример: Hysteria2 без obfs у владельца репо стабильно работает на мобильном интернете (10.2026). Коммерческие сервисы на этих же протоколах работают массово.

| Транспорт | Проводной / Wi-Fi | Мобильный без белых списков | Белые списки | Статус |
|---|---|---|---|---|
| Голый WireGuard / OpenVPN | ❌ | ❌ | ❌ | ✅ |
| SS-2022 / Trojan без маскировки | нестабильно | нестабильно | ❌ | ✅/❓ |
| VLESS RAW+REALITY+Vision, зарубежный ЦОД | частично, хуже с каждой волной | плохо | ❌ (кроме SNI-only фильтра у части операторов) | ✅ |
| VLESS XHTTP+REALITY (xmux 1–4) / gRPC+REALITY на 443 | лучше, но есть отчёты о сбоях в Москве и МО | лучше | ❌ | ❓ |
| Hysteria2 без obfs | работает не везде | T2 режет QUIC целиком, Билайн «не работает совсем», МегаФон нестабильно | ❌ | ✏️ |
| Hysteria2 + Salamander | ❓ «лучший результат на большинстве провайдеров» (вторичный источник) | ❓ (на T2 не тестировался) | ❌ | ❓ |
| AmneziaWG 2.0 со статичными параметрами | уходит под классификатор (лето 2026) | — | ❌ | ✅ |
| AWG 3.x / рандомизированный профиль + DNS-I1 | ❓ работает | DNS-I1 проходил на МТС (09.2026, один замер) | ❌ | ❓ |
| Каскад через RU-VPS из белого CIDR (Yandex Cloud) | — | — | работает, но «сегодня IP белый, завтра серый», аккаунты банят | ❓ |
| XHTTP через российские CDN (Yandex, VK, Selectel) | работает нестабильно | работает нестабильно | работает нестабильно | ❓ |

---

## 4. Протоколы

### 4.1 Xray-core / VLESS / REALITY / XHTTP

**Версии** ✅: последний stable **v26.3.27** (2026-03-27). Дальше только pre-release: v26.4.13 … v26.7.11, v26.7.28, v26.9.8, v26.9.9, **v26.9.30** (2026-09-30, без собственных release notes; изменения можно восстановить только по коммитам). ✏️ Версии v26.7.17 не существует. v26.2.6 и v26.3.27 — полноценные релизы.

**Ключевые изменения 2025–2026**

| Версия | Что | Влияние на репо | Статус |
|---|---|---|---|
| v24.10.31 → | `realitySettings.dest` → `target` (dest остаётся алиасом) | 04 пишет `dest`; лучше `target`, как и в UI 3x-ui | ✅ |
| v25.3.6 | клиентский `publicKey` → `password` (алиас) | — | ✅ |
| v25.5.16 | REALITY использует X25519MLKEM768 к target | — | ✅ |
| v25.6.8 | Имитация post-handshake records target (ответ на Aparecium). При старте сервер ~30 с пробует target, прокси в это время блокируется | `sleep 3` в 04 может застать пробинг | ✅ |
| v25.7.26 | ML-DSA-65 (`mldsa65Seed`/`mldsa65Verify`, `pqv` в ссылке); нужна цепочка target ≥3500 Б | опция P3 | ✅ |
| v25.8.29/v25.9.5 | VLESS Encryption `mlkem768x25519plus` (`xray vlessenc`); вывод `x25519`: `PrivateKey/Password/Hash32` | — | ✏️ |
| v25.10.15 | XHTTP `maxConcurrency=1` по умолчанию: в РФ получились сотни соединений | — | ✅ |
| v26.1.23 | Hysteria2 outbound + udphop + Salamander udpmask | — | ✏️ |
| v26.2.6 | **`allowInsecure` удалён** (date-switch 2026-06-01, затем PrintRemovedFeatureError). Замена: `pinnedPeerCertSha256` (pcs) / `verifyPeerCertByName` (vcn). Finalmask (XDNS, XICMP, header-*) | **ломает Hy2-ссылку из 99** | ✅ |
| v26.3.27 | **Warning на REALITY не на 443**; Hysteria2 inbound; header-custom, Sudoku, fragment/noise в finalmask; автопробинг `maxUselessRecords`; вывод `Password (PublicKey)`; uTLS Firefox/Safari с ML-KEM; убран warning «VLESS without flow» (#5671) | 8443 → 443 | ✏️ |
| v26.5.x | `clients`→`users` (#6083, алиас); `echForceQuery` удалён; `rewriteAddress` в tunnel (v26.5.9) | — | ✏️ |
| v26.6.27 | XMUX `maxConnections=6` «for anti-RKN» | — | ✅ |
| v26.7.7 | `network`→`method` (#6426, алиас); запрет нешифрованных VLESS/Trojan outbound в публичный интернет (#6303) | — | ✅ |
| v26.7.11–v26.7.28 | **Дефолт `minClientVer=26.3.27`**: отсекает все не-Xray клиенты | не ставить ядра v26.7.x | ✏️ |
| v26.7.28 | XMUX `maxConnections=3` «for anti-TSPU»; warning на target `.ru/.ir/.cn/apple/icloud/microsoft` (#6508; в PR написано «rejects», но в коде только warning) | cbr.ru | ✅ |
| **v26.9.8** | **REALITY отклоняет ClientHello без X25519MLKEM768 перед X25519**; дефолт minClientVer снят; в тестах «рабочие» отпечатки только chrome/firefox/safari; буфер target 17 KiB | sing-box и Shadowrocket не работают | ✅ |
| v26.9.9 | `quicParams.udpHop` → маска `udphop` (старый ключ молча игнорируется) | mport в v2rayN/NG сломан | 🔬 |
| v26.9.30 | MASQUE in/out (RFC 9484), XDRIVE (Google Drive как транспорт), новый формат XDNS (клиенту нужен ≥ v26.9.30), noise `exp`, Vision CloseNotify | — | ✅ |

**Best practices REALITY (2026)**

- Порт **443/tcp** ✅ ([157e65b](https://github.com/XTLS/Xray-core/commit/157e65b34d32363528088c592d4e415d84f01a63)). ✏️ Исходная формулировка «may get your IP blocked» позже ужесточена до «will increase the likelihood». Отчёты ntc.party о блокировке именно 443 касаются конкретных IP, абонентов и операторов, а уход на 444 помогал ненадолго 🔬.
- **Target**: жёсткие требования по коду `XTLS/REALITY tls.go` 🔬 — TLS 1.3; в первом ServerHello группа X25519 или X25519MLKEM768, **без HRR** (✏️ [#6861](https://github.com/XTLS/Xray-core/issues/6861) закрыт как NOT_PLANNED: такой target непригоден в принципе); запись Certificate ≤17 KiB (до v26.9.8 — 8192 Б, на этом ломался microsoft); h2; без редиректа на другой хост; SAN покрывает SNI. Желательно: ML-KEM у target, тогда сессия тоже PQ; тот же ASN или CDN, что у VPS; не `.ru` и не apple/icloud/microsoft.
- **Лучше всего — self-steal** (свой домен + Let's Encrypt + локальный Caddy/nginx как target через 127.0.0.1 или unix-сокет): ByeByeVPN и Теплица (09.2026) ✅/❓. Вторичный вариант — сосед по ASN (RealiTLScanner, сканер в 3x-ui ≥v3.4.2; сканирование с самого VPS — риск). Self-steal **не спасает от бана IP по ASN** (#671) ✅. Caddy ≥2.10 даёт X25519MLKEM768. nginx из репозиториев 22.04/24.04 (OpenSSL 3.0) не даёт 🔬.
- **Замер кандидатов 2026-10-04 из DE** 🔬 (по одному хосту; из РФ результат может отличаться). TLS1.3 + X25519MLKEM768 + h2, длина цепочки в байтах: www.samsung.com 4181, www.nvidia.com 4333, addons.mozilla.org 4085, www.yahoo.com 4572, www.oracle.com 4105, www.lovelive-anime.jp 3798, www.asus.com 3765, dl.google.com 3510, www.cisco.com 4934, www.bing.com 3891. Без ML-KEM: github.com, www.cbr.ru. Короче 3500: cloudflare, google, speedtest, intel, tesla, amd. **Это крупные бренды на CDN, использовать только как аварийный fallback** («Never pick amazon/apple/microsoft/google/cloudflare on a random VPS»).
- **fingerprint = chrome** ✅. На v26.9.8+ в Xray-клиентах работают chrome/firefox/safari, `randomized` даёт HRR-сбой (#6714). ios/edge/qq убраны из списка рабочих. ✏️ Совет Хабра (июнь 2026) переходить на Edge устарел. mihomo работает только с chrome.
- `minClientVer` оставлять пустым ✅. ✏️ Ключи `minClient`/`maxClient`, которые пишет 04, Xray **никогда не распознавал**. Правильные — `minClientVer`/`maxClientVer`.
- `shortId`: hex, длина кратна 2, ≤16 символов, 8 байт допустимы ✅. `spiderX` должен различаться у клиентов ✅. `fakedns` в `destOverride` без настроенного FakeDNS бесполезен ✅. Rate-limit fallback (`limitFallback*`) — это признак, не включать ✅.
- **Порядок ключей**: Xray-examples и README REALITY по-прежнему используют `clients`/`network`. Переход на `users`/`method` не нужен и может конфликтовать с UI 3x-ui. Для 3x-ui писать в формате самой панели: `target`, `minClientVer` ✏️.
- Разбор `xray x25519` ✅: формат менялся дважды (до v25.8.3: `Private key/Public key`; v25.8.29+: `PrivateKey/Password/Hash32`; v26.3.27+: `Password (PublicKey)`). `awk '/Password/{print $NF}'` сейчас работает. Надёжнее `xray x25519 -i <priv>` или регулярка `^(Password|Public key)` с проверкой длины 43.
- **ECH** ✅: RFC 9849 (03.2026). ✏️ OpenSSL лишь объявил реализацию (цикл 4.0). В РФ блокируется Cloudflare-ECH. Блокируется ли собственный ECH, неизвестно ❓. В инсталлятор не включать.

**XHTTP+REALITY** (рекомендуемый второй TCP-inbound)

- Дефолты XMUX на клиенте ✅ 🔬: `maxConnections=3`, `hMaxRequestTimes=600-900`, `hMaxReusableSecs=1800-3000`. Применяются **только если блок xmux пустой целиком**: стоит задать хоть одно поле, и остальные дефолты пропадают. `maxConcurrency` и `maxConnections` взаимоисключающие.
- Режим ✏️ 🔬: на сервере `auto` (принимает всё). Клиент в `auto` с REALITY выбирает **stream-one**, с `downloadSettings` — stream-up, во всех остальных случаях, включая TLS H2, — packet-up. packet-up нужен для CDN и H3.
- `flow: ""` ✅. Vision поверх XHTTP работает только вместе с VLESS Encryption. `fallbacks` нельзя совмещать с `decryption` на master.
- ✏️ `sockopt.trustedXForwardedFor` **не обязателен**: без него XFF просто игнорируется с warning. Нужен только за CDN, чтобы видеть реальный IP.
- Не класть в extra `scMinPostsIntervalMs=30` / `scMaxEachPostBytes=1000000` ❓: по репорту 3x-ui#5141 литерал 30 ловится на мобильных сетях.
- Размещение 🔬: официальный [Xray-examples VLESS-XHTTP-Reality](https://github.com/XTLS/Xray-examples/tree/main/VLESS-XHTTP-Reality/minimal-steal_others) — отдельный inbound на 443. Схема «master RAW+REALITY+Vision на 443 → fallback в XHTTP child без security на 127.0.0.1» описана в сообществе ([#4118](https://github.com/XTLS/Xray-core/discussions/4118)) и **штатно поддерживается 3x-ui v3.9.0** (таблица `inbound_fallbacks`, `POST /panel/api/inbounds/:id/fallbacks`). Схему тела API я не проверял ❓. Оговорка из #4826: при Vision+fallback отпечаток всё равно остаётся Xray-шным ❓.
- **VLESS Encryption** ✅: для прямого REALITY не нужен. ✏️ Фраза «REALITY 则不需要再加密 VLESS» в PR зачёркнута, это шутка. Официальная рекомендация для CDN: «XHTTP + VLESS enc». Поддержка: mihomo ≥1.19.24 (xhttp поверх encryption).

**Детект и блокировки по REALITY** ✅: в 2025–2026 нет академических работ, которые бы «вскрыли» REALITY+Vision. Единственный PoC — Aparecium (закрыт мимикрией в v25.6.8). Ломают инфраструктура (IP/ASN), поведение (число соединений), несоответствие SNI и IP и нестандартные порты.

### 4.2 3x-ui и альтернативные панели

**Версии** ✅: v2.9.3 (2026-04-27, стояла на момент коммита репо; Xray v26.4.25), v2.9.4, v3.0.0–v3.0.2 (prerelease, Vue 3), **v3.1.0 (2026-05-23, первая стабильная v3; React; таблицы clients; удаление all_time)**, v3.3.0 (MTProto; ломающее изменение: /panel/setting и /panel/xray перенесены под /panel/api), v3.3.1 (live-apply через gRPC; CVE-2026-55477), v3.4.0 (IP-limit только через fail2ban, Gecko packetSize), v3.4.2 (WG multi-client, сканер target, минимальное ядро ≥v26.6.27), v3.5.0 (подписки из нормализованных таблиц), v3.6.0 (freedom блокирует egress в приватные сети), **v3.7.0 (2026-08-24: встроенный AWG 3.1 userspace, scoped API tokens, автоматические миграции схемы)**, v3.8.0 (2026-09-14: `.sha256` к архивам, 401 вместо 404, права БД 0700/0600, TUIC через sidecar, Xray v26.9.9), v3.8.5, **v3.9.0 (2026-10-03: Xray v26.9.30, нативный TUIC v5, `inbounds/update` не трогает клиентов, `support-x25519mlkem768=true` в vless-ссылках)**. Канал `dev-latest` — prerelease, его не брать.

**API-провижининг (основа нового 04)** ✅ 🔬

```bash
# токен (работающая панель не нужна; при повторном вызове токен с тем же именем перевыпускается, старый перестаёт действовать)
TOKEN=$(/usr/local/x-ui/x-ui setting -getApiToken -tokenName vpn-zoo -tokenScope admin | awk '/^apiToken:/{print $2}')
umask 077; printf 'Authorization: Bearer %s\n' "$TOKEN" > /etc/vpn-setup/xui-auth.hdr
# вызов (флаг -getApiToken ставить ДО позиционных аргументов)
jq -n ... | curl -fsS -H @/etc/vpn-setup/xui-auth.hdr -H 'Content-Type: application/json' \
  --data-binary @- "http://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/panel/api/inbounds/add" | jq -e '.success'
```

Минимальное тело `/inbounds/add` 🔬 (settings/streamSettings/sniffing можно передавать вложенными объектами — `Inbound.UnmarshalJSON` принимает и строки, и объекты):

```json
{"remark":"vless-reality","enable":true,"listen":"","port":443,"protocol":"vless","expiryTime":0,"total":0,
 "settings":{"clients":[{"id":"<UUID>","email":"zoo-user1","flow":"xtls-rprx-vision","enable":true,
   "limitIp":0,"totalGB":0,"expiryTime":0,"tgId":0,"subId":"<rand16>","reset":0}],"decryption":"none","fallbacks":[]},
 "streamSettings":{"network":"tcp","security":"reality","tcpSettings":{"header":{"type":"none"}},
   "realitySettings":{"show":false,"xver":0,"target":"<sni>:443","serverNames":["<sni>"],"privateKey":"<priv>",
     "shortIds":["<hex>"],"minClientVer":"","maxClientVer":"",
     "settings":{"publicKey":"<pub>","fingerprint":"chrome","spiderX":"/<rand>"}}},
 "sniffing":{"enabled":true,"destOverride":["http","tls","quic"]}}
```

- **email обязателен**: клиента без email `syncInboundClients` пропускает, и он не попадёт в Xray 🔬. `tgId` — число, не `""` ✅.
- `/inbounds/add` **сам** заполняет `clients`, `client_inbounds` и `client_traffics`, вызывать `/clients/add` не нужно 🔬. Это снимает противоречие между темами: в v3.9.0 клиентов **не трогает** только `/inbounds/update/:id` ✏️. Для add-client: `POST /panel/api/clients/add {"client":{...},"inboundIds":[id]}`, правка — `/clients/update/:email`, удаление — `/del/:email`, ссылки — `/clients/links/:email`. Включение и выключение inbound — `/inbounds/setEnable/:id`.
- Без заголовка ответ маскируется под 404, с неверным Bearer с v3.8.0 приходит 401. CSRF для Bearer пропускается ✅.
- Аварийный фолбэк ✏️: `stop x-ui → INSERT без all_time с явным уникальным tag → x-ui migrate → start`. Только SQLite. Обходит всю валидацию, при новых колонках каждого релиза хрупок. Одного рестарта недостаточно: сидер ClientsTable на свежей БД уже помечен выполненным.
- `x-ui setting` в v3.9.0 ✅: `-username -password -port -webBasePath -listenIP -resetTwoFactor -getListen -getCert -getApiToken -tokenName -tokenScope -webCert -webCertKey …`. Подкоманды: `run, migrate, encrypt-tokens, migrate-db, setting, cert`. **Пароль передаётся только через argv** ✅, так же делает и upstream install.sh. Вариант без argv: `/panel/api/setting/updateUser` через stdin, на Bearer не тестировался ❓.
- Официальный `install.sh` неинтерактивный ✏️: `XUI_NONINTERACTIVE=1` или нет TTY. Переменные `XUI_USERNAME/PASSWORD/PANEL_PORT/WEB_BASE_PATH/SSL_MODE(=none)/DB_TYPE/ENABLE_FAIL2BAN…`. Если порт, логин или путь не заданы, они **случайные** (порт 1024–62000). Пишет `/etc/x-ui/install-result.env` (0600, в том числе `XUI_API_TOKEN`), сам запускает `x-ui migrate`, проверяет sha256.
- Состав архива ✅: `x-ui-linux-{386,amd64,arm64,armv5,armv6,armv7,s390x}.tar.gz` + `.sha256` (с v3.8.0). Внутри `x-ui`, `x-ui.sh`, `x-ui.service.{debian,arch,rhel}` (**общего `x-ui.service` нет**), `bin/xray-linux-<arch>`, geo*.dat (с RU-вариантами), `mtg-linux-<arch>`. ❓ Для armv7 бинарь может называться `xray-linux-arm32` (один источник).
- Подписки ✅: `subEnable=true` по умолчанию, порт 2096, случайный subPath на свежей БД. Форматы: raw, JSON (без TUIC/AWG), `/mihomo/<subId>` (Clash, с пином Hy2 с v3.9.0). Для Happ есть заголовки `subHappLocalProxyAuth` (#6628).

**Безопасность 3x-ui** ✏️ (по [GitHub Security Advisories API](https://github.com/MHSanaei/3x-ui/security/advisories); патчи получает только последний релиз)

| ID | Severity | Затронуто | Исправлено |
|---|---|---|---|
| CVE-2025-29331 | 9.8, обновление через wget без проверки сертификата | <2.5.3 | 2.5.3 |
| CVE-2026-55477 / GHSA-jm48-m3rr-9hgg | High 7.2, запись файла от root через импорт БД и путь логов | ≤3.3.0 | 3.3.1 |
| GHSA-h4x8-qc42-f6wv | High 7.2, обход предыдущего фикса через регистр ключей логов | 3.3.1–3.7.0 | 3.8.0 |
| GHSA-rr44-v4rv-x654 | Medium 4.7, node-sync | ≤3.7.0 | патч в advisory не указан |
| GHSA-fqw4-8j9p-5r3v | Medium 5.5, запись SQLite через ATTACH | ≤3.8.5 | 3.9.0 |
| GHSA-32x3-9376-fh92 | Low 2.7, SSRF | 3.4.0–3.8.5 | 3.9.0 |
| GHSA-xqqw-jqqv-99h6 | Medium, замена 2FA без кода | ≤3.6.0 | 3.7.0 |
| GHSA-cfpf-wmjp-gh6c | Low, SSRF-guard | ≤3.6.0 | 3.7.0 |
| GHSA-7ww3-8rcw-wrr2 | Low, обход HWID | ≤3.7.0 | 3.8.0 |

Почти все требуют аутентификации в панели, поэтому закрытая наружу панель резко снижает риск. До v3.8.0 БД создавалась world-readable: внутри хеш пароля, UUID, приватные ключи REALITY. На старых установках нужно сделать `chmod 700 /etc/x-ui; chmod 600 /etc/x-ui/x-ui.db*` ✅.

**Встроенные протоколы v3.x** ✏️: Hysteria inbound (с v2.9.0), AmneziaWG 3.1 userspace (amneziawg-go v3.1.20260828 + gVisor, без DKMS, с v3.7.0), TUIC v5 (v3.8.0 через sidecar, v3.9.0 нативно), MTProto. Его генератор AWG: HPK on, RandomTrailers on, DisableCookies on, одиночные H, I1 = `<r 32..256>` (по замеру на МТС такой I1 может резаться ❓).

**Альтернативные панели** ✅: Remnawave 3.4.4 (2026-09-12; Docker; Panel требует 2 ГБ RAM; путь к масштабированию). PasarGuard v5.4.1 (2026-09-12; активный форк Marzban). s-ui v1.6.3 (2026-09-16; sing-box). Hiddify-Manager v13.0.3 (✏️ 2026-09-26) / v14.0.0b5 (✏️ 2026-09-30). Marzban заброшен (v0.8.4, 2025-01-09). Marzneshin почти не развивается. **Вывод:** для однохостового инсталлятора остаётся 3x-ui.

### 4.3 Hysteria2

**Версии** ✅: app/v2.12.3 (2026-09-16) — актуальная. ✏️ Репозиторий переехал: `apernet/hysteria` → **`HyNetworks/hysteria`** (редирект работает). Хронология: 2.6.2 (фрагментация ClientHello на несколько Initial; ✏️ что это обходит ТСПУ, не доказано), 2.6.4 (pin только leaf), 2.7.0 (баг BBR), 2.8.0 (`congestion.type bbr|reno`, `bbrProfile`, `listen` с диапазоном портов, min/maxHopInterval), 2.8.1 (`HYSTERIA_FIREWALL_BACKEND`), **2.8.2 (клиенты 2.8.2+ теряют UDP на старых серверах; OOM sniff)**, 2.9.0 (Realms, `hysteria cert`), **2.9.2 (Gecko obfs; DoS/SSRF/обход UDP ACL)**, 2.10.0 (ECH, `disableLossCompensation`), 2.11.0 (Chrome QUIC parrot по умолчанию; ACME-стек), 2.12.0 (mimic — fake-TCP через XDP), 2.12.1 (stateless reset), 2.12.2 (`disableStatelessReset`), 2.12.3 (`hysteria ech`, фикс port hopping, который заворачивал исходящий UDP).

**Уязвимости** ✏️: GHSA-vgrc-hq28-p3xp, GHSA-qh5x-rfwf-rvfv, GHSA-jqc5-2p7q-fqfc (2026-05-23) и GHSA-9fw6-xgg2-mq9q (2026-04-27, ≤2.8.1). Все закрыты в ≥2.9.2.

**Ссылка (главное)** 🔬, по коду 7 клиентов:

| Клиент | `insecure=1` без пина (как сейчас в репо) | `insecure=1&pinSHA256=hex` |
|---|---|---|
| Официальный hysteria 2.12.3 | работает, без защиты от MITM | работает, с пиннингом. **Без `insecure=1` self-signed сертификат не примет даже с пином**: пин проверяется после стандартной проверки цепочки |
| v2rayN 7.25.4 (ядро Xray) | конфиг загрузится, но TLS упадёт | работает: `pinnedPeerCertSha256`, allowInsecure в JSON не пишется |
| v2rayNG 2.3.10 | **ядро не загрузит конфиг** (allowInsecure:true) | работает: allowInsecure=false + pin. Ключ должен быть именно `pinSHA256`, не `pcs` |
| Happ | не подключается после 2026-06-01 ([Hiddify-Manager#5438](https://github.com/hiddify/Hiddify-Manager/issues/5438)) | ❓ должен работать: нужен hex, base64 даёт ошибку (3x-ui#4818) |
| Hiddify (ray2sing), NekoBox 1.4.2 | работает | работает, но пин игнорируется (только skip-verify) |
| mihomo (Clash Verge Rev, FlClash) | работает | работает: `pinSHA256` → `fingerprint`, это настоящий leaf-пин |
| sing-box 1.14.2 JSON | — | пин из URI не переносится. Для пина: `certificate_public_key_sha256` (base64 SPKI); `certificate_sha256` есть только в 1.15-alpha |

Пин: `openssl x509 -in /etc/hysteria/cert.pem -outform DER | sha256sum | cut -d' ' -f1`. Результат — 64 hex-символа в нижнем регистре **без двоеточий** (Xray убирает только `:`, `-` не убирает). Альтернатива: `xray tls hash --cert cert.pem`. ✅ Пароль держать в `[A-Za-z0-9]`: mihomo не декодирует userinfo. SPKI-пин для sing-box JSON: `openssl x509 -in cert.pem -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`.

**Схема URI** ✅ ([спецификация](https://v2.hysteria.network/docs/developers/URI-Scheme/)): `hysteria2://[auth@]host[:port]/?obfs=salamander|gecko&obfs-password=&sni=&insecure=&pinSHA256=&ech=#name`. Мульти-порт задаётся в authority: `host:443,20000-29999`. Существует также `hysteria2+realm://`. ✏️ **`mport` — не официальный параметр**, это расширение v2rayN/NG и 3x-ui. 🔬 С Xray v26.9.9 ключ `quicParams.udpHop` удалён, а v2rayNG 2.3.10 и v2rayN 7.25.4 всё ещё пишут в него значение `mport`. Поэтому **port hopping в v2rayN/NG сейчас не работает**: клиент ходит только на основной порт. mihomo `mport` игнорирует и берёт порты из authority.

**Best practices**

- **Версия** ✅: закрепить `--version v2.12.3`. Установщик `get.hy2.sh` хеши не проверяет. С `--version` он **каждый раз** переустанавливает бинарь и перезапускает сервисы, поэтому версию нужно сравнивать заранее: `hysteria version | grep '^Version' | grep -o 'v[.0-9]*'` (у вывода есть ASCII-лого, поэтому `head -1` в 05 возвращает мусор). Установщик **перезаписывает** `hysteria-server.service` и `@.service` при каждом запуске, так что правки делать только через drop-in. При первой установке он кладёт пример `config.yaml` (с `your.domain.net`), это важно для safety-guard ✏️. В каждом релизе есть `hashes.txt`.
- **Сервис работает от пользователя `hysteria`** с `CAP_NET_ADMIN/NET_BIND_SERVICE/NET_RAW`, поэтому cert и key должны быть читаемы этим пользователем ✏️.
- **Congestion** ✅: оставить `ignoreClientBandwidth: true`. Это включает BBR (`congestion.type bbr`, `bbrProfile standard`) вместо Brutal. Brutal детектируется по фиксированной скорости при потерях: Wang et al., *«Is Custom Congestion Control a Bad Idea for Circumvention Tools?»*, FOCI 2025 (синтетика, 100% на Stage 1). Прописать явно.
- **Сертификат** ✅: ECDSA P-256 (как сейчас). **Не Ed25519**: Chrome parrot (Hysteria ≥2.11, sing-box ≥1.14, Xray ≥v26.9.8) с ним не проходит рукопожатие. 100 лет — непривычный срок, разумнее 1–10 лет. `sniGuard` по умолчанию `dns-san` (SAN=bing.com совпадает с sni).
- **Masquerade** ✏️: в 05 баг: URL собирается как `https://www.${HY2_SNI}/`, и при `HY2_SNI=www.x` получится `www.www`. Активный зонд видит контент bing с недоверенным сертификатом на чужом ASN. Это логический вывод, а не измерение. Варианты: `HY2_DOMAIN` + ACME (тогда insecure и пин не нужны) или obfs.
- **Salamander / Gecko** ✅: скрывают QUIC Initial от SNI-фильтра, но сервер перестаёт быть HTTP/3, и masquerade теряет смысл. TLS внутри остаётся, **пин всё равно нужен**. Gecko экспериментальный, v2rayNG его не поддерживает (всегда salamander). Ставить вторым инстансом: `hysteria-server@obfs.service` + `/etc/hysteria/obfs.yaml` на отдельном UDP-порту.
- **Port hopping** ✅: встроенный `listen: :20000-50000` (только Linux) слушает **первый порт диапазона**, а nft/iptables-правила ставит сам. Чтобы сохранить 443 и старые ссылки, проще ручной redirect: `table inet hysteria_porthopping { chain prerouting { type nat hook prerouting priority dstnat; iifname $IF udp dport 20000-29999 redirect to :443 } }` через drop-in. Помогает уйти от residual-блока 420 с (это вывод ❓). С mimic несовместим. Диапазон не должен пересекаться с 51822.
- **Не включать по умолчанию** ✅: mimic (нужен root, DKMS-модуль, клиенты только на Linux), ECH (в РФ блокируется CF-ECH, свой ECH не измерялся), Realms.
- Для add-client в будущем: `auth.type: userpass` + `trafficStats: {listen: 127.0.0.1:9999, secret}` ✅.
- **Роль в РФ** ✏️: Hy2 — запасной канал для проводного интернета и Wi-Fi, на мобильных сетях он часто не работает. Но при TCP-only бане IP (#671, 02.10) UDP может пережить блокировку ❓.
- **Не переносить Hy2 в Xray/3x-ui** ✅: Xray#6717 («Hy2 через Xray не работает в РФ, sing-box работает») закрыт как not planned. Эталонная реализация свежее.
- sysctl ✅: quic-go просит rmem/wmem_max ≥7.5 МБ, документация Hysteria — 16 МБ. TCP BBR на QUIC не влияет.

### 4.4 AmneziaWG (2.0 → 3.1)

**Версии** ✅: tools v3.1.20260812, amneziawg-go v3.1.20260828, kmod v3.1.20260906. PPA `ppa:amnezia/ppa`: kmod `1.0.0-0~202609061402+4569c4c` (jammy/noble/…), resolute и 26.10 от 2026-09-14. Tools `+ee0f0a9` (= 3.1). **Версия dpkg «1.0.0» обманывает.** `/sys/module/amneziawg/version` покажет `3.1.20260812` и для 4680320, и для 4569c4c. Уровень фиксов виден только по git-хешу в версии dpkg ✅. Для oracular и plucky собраны старые сборки.

**Параметры** ✅ ([README amneziawg-go](https://github.com/amnezia-vpn/amneziawg-go/blob/master/README.md)):
- 2.0: S1–S4, H1–H4 (диапазоны), I1–I5 (CPS-теги `<b> <t> <r> <rc> <rd>`; тег `<c>` клиенты отвергают 🔬).
- 3.0: `HeaderProtectionKey` (ChaCha20; **обязан совпадать**; при нём **все S1..S4 ≥12**, иначе EINVAL), `ContentPaddingAddition` (✏️ регрессия go#198), таймеры диапазонами (`RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts`; односторонние; в kmod RejectAfterTime игнорируется, #227), `PersistentKeepalive` диапазоном.
- 3.1: `RandomTrailers` (двусторонний), `DisableCookies` (односторонний, снимает защиту от флуда рукопожатиями).
- Должны совпадать: S1–S4, H1–H4, HPK, RandomTrailers. Не обязаны: Jc/Jmin/Jmax, I1–I5 (их шлёт **инициатор**, то есть клиент; комментарий «I2 = ответ» в 06 неверен ✅), таймеры. Удалить параметр через `awg syncconf` нельзя, нужно пересоздать интерфейс ❓.
- Без HPK и RT формат 3.x побайтно совпадает с 2.0, и старые клиенты работают ✅.

**Известные баги (на 2026-10-04 все открыты)** 🔬
- **RandomTrailers** ([go#186](https://github.com/amnezia-vpn/amneziawg-go/issues/186), [kmod#226](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/226), PR #242 не влит). Транспортные пакеты принимаются за рукопожатие, если S не равны, а H широкие или низкие. Скорость падает с 11.9 до 0.09–0.14 МБ/с. ✏️ Коммиты 08-28 и 09-06 это **не** исправили. Безопасно только при S1=S2=S3=S4 и одиночных H (так делают AmneziaVPN: S=12, H=1..4; и 3x-ui).
- **HPK kmod↔go** ([kmod#222](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/222)): kmod-клиент не проходит рукопожатие с go-сервером. Обратная связка (наш kmod-сервер с go-клиентами Amnezia) не проверена ❓. Любое несовпадение HPK даёт 100% потерь без диагностики.
- **Jmin>Jmax**: `kzalloc(jmax)` + запись `jmin..jmax` даёт переполнение кучи и kernel panic ([#225](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/225), [#254](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/254)). В go это паника демона (#189). Отрицательные размеры `<r>` тоже не валидируются (#233). Генератор обязан это проверять.

**Безопасный серверный профиль для kmod** 🔬 (совместим с клиентами 2.0):
- Генерировать при каждой установке и хранить в config.env:
  - Jc 3–6; Jmin 40–89; Jmax = Jmin+50..250, строго Jmin<Jmax<1280;
  - S1, S2 в 15–150, S1+56≠S2; S3 в 12–55, S3≠S2+28; S4 в 12–27;
  - H1..H4 — одиночные значения, по одному в каждой четверти 5..2147483647, ≤INT32_MAX (это нужно Windows-клиенту);
  - пределы iOS: S1≤1552, S2≤1608, S3≤1636.
- **HPK и RandomTrailers по умолчанию OFF**, включаются только флагами (`AWG_HPK=1` → S≥12, нужен клиент 3.1; `AWG_RT=1` → общее S и одиночные H). DisableCookies — опционально, только на сервере.
- **I1 в форме DNS-ответа** ≤128 Б (`<r 2><b 0x8580…>`) со случайным доменом, TTL и A-записями. I2–I5 не задавать ✏️. Почему: в замере bivlked (МТС, 09.2026, один маршрут) случайные I1 ≥96 Б резались, DNS-форма проходила. ✏️ Доказательств, что текущий 36-байтный `<b 0x16030101><r 32>` режется, нет. Но формой он не похож ни на один реальный протокол: это заголовок TLS-записи с битой длиной поверх UDP. Дефолтный I1 AmneziaVPN (DNS-ответ icloud.com → 77.88.55.55) одинаков у всех, кроме 2 байт, копировать его нельзя.
- **MTU 1280** (AmneziaVPN использует 1376 на десктопе и 1280 на мобильных) + TCPMSS clamp в PostUp/PostDown ✅.
- **Профиль Amnezia для сравнения** ✅ (awgInstaller.cpp): Jc 4–6, Jmin 10, Jmax 50, S1–S4=12, H=1..4, HPK, RT=on, DisableCookies=on, таймеры 100-120/3-7/150-180/5-15/15-20, порт 55424, MTU 1376/1280. Работает на userspace go в Docker, не на kmod.

**Ядро и DKMS** 🔬

| Ядро | Сборка kmod 3.1 из PPA | Статус |
|---|---|---|
| 22.04 GA 5.15 | собирается после фикса nla_put_uint (2026-07-31, #204). Живых тестов нет. Угроза: бэкпорт timer_delete (PR #257) | ❓ |
| 22.04 HWE 6.8 | самый проверенный путь | ✅ |
| 24.04 GA 6.8 | собирается | ✅ |
| 24.04.5 HWE **7.0.0-38** | **НЕ собирается** (#259; PR #218/#250 не влиты). apt upgrade оставляет ядро ненастроенным | ✅ |
| 26.04 GA 7.0.0-38 | **НЕ собирается**; на 7.0.0-34 собиралось | ✅ |
| <5.5 / <4.3 | header protection и strscpy ломаются (#210, #251) | ✏️ |

✏️ Откуда миф «≥6.2»: до 2025-10-03 в `dkms.conf` был `PRE_BUILD=prepare-sources.sh`, и требовалось полное дерево исходников ядра. README kmod до сих пор не обновлён. Сейчас сборке нужны только заголовки.

**Блокировки** ✅: AWG 1.0 сломан летом 2025. По 2.0 летом 2026 удар пришёлся комбинацией поведенческого анализа и банов IP: Amnezia пишет, что цензор «изучил технические особенности трафика AWG 2.0». ✏️ Гипотеза «ТСПУ считает скор с порогом» — интерпретация, в постмортеме её нет. Письмо Remnawave (пересказ RPRX): «AWG работает только потому, что его доля рынка ещё <1%» ✅.

**Альтернатива без DKMS** ✅/❓: встроенный AWG 3.1 в 3x-ui ≥v3.7.0 (userspace go + gVisor). Снимает и матрицу ядер, и вопрос HPK kmod↔go. Но скорость на 1 vCPU никто не мерил, а схему AWG-инбаунда через API ещё нужно изучить.

---

## 5. Новые и альтернативные протоколы, мировые исследования

### 5.1 Добавлять / не добавлять в «зоопарк»

| Протокол | Решение | Почему | Статус |
|---|---|---|---|
| VLESS+XHTTP+REALITY | **Добавить (P1)** | Устойчивее к лимитам соединений ТСПУ, Project X настраивает его дефолты под ТСПУ | ✅ |
| CDN: XHTTP+TLS+VLESS Encryption | Опция при наличии домена (P2) | Не зависит от IP VPS. Cloudflare в РФ ограничен ~16 КБ, нужен CDN, работающий в РФ. Доступность CF зависит от префикса ([#662](https://github.com/net4people/bbs/issues/662)) | ✅/❓ |
| Hy2 Salamander + port hopping | Опция (P1) | QUIC SNI-фильтр, residual-блок 420 с | ✅ |
| AWG 3.x / userspace в 3x-ui | Рандомизация (P0), вопрос перехода открыт | — | 🔬 |
| RU-мост (каскад) | Опциональная роль (P2) | Единственное, что работает при мобильных белых списках. По «Антифрод 2.0/3.0» RU-узел — расходник | ✅/❓ |
| XDRIVE (Xray v26.9.30) | Наблюдать | Транспорт через Google Drive, обходит IP-белые списки. Pre-release | ✅ |
| NaiveProxy (v154.0.8037.49-2) | P3, только с доменом | Настоящий Chromium-стек, нужен Caddy forwardproxy | ✅ |
| Tor WebTunnel-мост | P3 | WebTunnel и Snowflake в РФ массово режутся | ✅ |
| mieru v3.38.0 | Только эксперимент | Протокол со случайным видом, ловится энтропийными эвристиками | ✅ |
| TUIC v5, Juicity | Не добавлять | Дублируют Hy2, нет обфускации. Upstream TUIC заморожен (1.0.0, 2023) | ✅ |
| ShadowTLS | Не добавлять | Aparecium, ✏️ последний релиз v0.2.25 от **2023-12-13** | ✏️ |
| SS-2022 / Outline | Не добавлять как основной | Ловится по энтропии (FET), в Xray помечен deprecated в пользу VLESS Encryption | ✅ |
| Trojan | Не добавлять | Преимуществ над VLESS+REALITY нет | ✅ |
| OpenVPN 2.7 / голый WG / Rosenpass | Не добавлять | Сигнатуры (детектор OpenVPN в TSG взят из nDPI) | ✅ |
| MASQUE | Не добавлять | alpha/pre-release, QUIC под SNI-фильтром, WARP есть в сигнатурах TSG | ✅ |
| AnyTLS | Не добавлять | Нишевый, ✏️ эталон v0.0.13 | ✏️ |
| TrustTunnel (AdGuard) | Наблюдать | Открыт в 01.2026 | ❓ |

### 5.2 Мировые исследования 2025–2026

- **Утечка Geedge/TSG** ✅ ([USENIX Sec '26](https://www.usenix.org/system/files/usenixsecurity26-ablove.pdf)). Коммерческий DPI: дешёвые сигнатуры VPN (JA3, issuer сертификата, «длина первого UDP payload = 54 + фиксированные байты»), WARP, OpenVPN из nDPI. Установлен в Казахстане, Пакистане, Эфиопии, Мьянме. **Вывод для репо:** одинаковые для всех установок параметры AWG, shortId и SNI — готовая статическая сигнатура.
- **Россия, QUIC SNI** ✏️ (FOCI 2026, Heitmann и др.): см. §2.2. Данные: [UPB-SysSec/RussiaQuicResults](https://github.com/UPB-SysSec/RussiaQuicResults).
- **Иран** ✏️ (FOCI 2026): 100% блок QUIC Initial начался **до** шатдауна 18–25.06.2025 (после удара 13.06) и остался после него. Фрагментация ClientHello с 07.2026 не работает (#640). Курс на белые списки.
- **Китай** ✅ (USENIX Sec '25): расшифровка QUIC Initial с 2024-04. QUICstep (PETS 2026) обходит через connection migration.
- **Custom CC** ✅ (FOCI 2025, Censored Planet): Brutal отличим от BBR. Обоснование для `ignoreClientBandwidth`.
- **Snowflake** ✅ (arXiv 2609.12242, CCS 2026): энумерация и блокировка.
- Также: экосистема китайских «аэропортов» (arXiv 2606.18427), Huma (NDSS 2026), Aparecium (2025).

### 5.3 Ответ на «стоит ли пинить старое ядро ради sing-box»

🔬 Нет. sing-box 1.14.2 и 1.15.0-alpha.10 **специально вырезают** X25519MLKEM768 и пишут версию 1.8.1 в SessionId ([reality_client.go](https://github.com/SagerNet/sing-box/blob/v1.14.2/common/tls/reality_client.go)). PR с исправлением нет. RPRX считает такой урезанный ClientHello сильным признаком для ТСПУ. Ядро v26.6.27 — компромисс только «по требованию», без XDNS v26.9.30 и udphop.

### 5.4 Мифы и опровергнутое

- ❌ «AmneziaWG DKMS требует ядро ≥6.2». Нужны только заголовки. Реальная проблема — ядро 7.0.0-38.
- ❌ «cbr.ru геоблокирует зарубежные IP» (bfm.ru про cbr.ru ничего не пишет). Из DE: 200, TLS1.3, h2.
- ❌ «Имитация Chrome QUIC есть только в официальном клиенте и sing-box ≥1.13.18». На самом деле Hysteria ≥2.11, **sing-box ≥1.14.0** (в 1.13.x её нет), **Xray ≥ v26.9.8** (`finalmask.quicParams.disableChromeParrot`). В mihomo нет.
- ❌ «Коммиты kmod 08-28/09-06 исправили RandomTrailers».
- ❌ «#6861 (HRR от target) исправлен». Закрыт NOT_PLANNED: target с HRR непригоден.
- ❌ «`mport` — официальный параметр Hysteria2».
- ❌ «Без trustedXForwardedFor XHTTP не работает».
- ❌ «В auto при TLS H2 клиент выбирает stream-up». Выбирает packet-up.
- ❌ «Salamander не помог на T2». Там его не тестировали.
- ❌ «72 AS — белый список хороших сетей». Это ограничиваемые сети.
- ❌ «Karing и v2rayN уязвимы к SOCKS5-утечке» (runetfreedom их уязвимыми не называл). ✏️ В v2rayNG ≥2.1.0 авторизация есть, но **выключена по умолчанию**.
- ❌ «Совет переходить на fp=edge». edge на v26.9.8+ не работает.
- ❌ «HWE для 24.04 — 6.17». С 24.04.5 (11.09.2026) это 7.0.
- ❌ «OpenSSH 10.4 — последняя». Последняя 10.5 (11.08.2026).
- ❌ «Сентябрьская KEV-тройка — три LPE». CVE-2025-39682 — kTLS RX (9.8, возможно удалённо), CVE-2026-53266 — LPE в ebtables, CVE-2025-39964 — гонка в AF_ALG.

---

### 5.5 Новые и ожившие протоколы (2025–2026, отдельный рой)

Срез на 2026-10-04. Каждый пункт проверен фактчекером по первоисточникам (GitHub API, релизы, issues, ntc.party, Хабр).
Обозначения: ✅ подтверждено, ✏️ подтверждено с поправками (ниже приведены уже исправленные факты), ❓ не проверено.

#### Краткий вывод

1. **Мобильные белые списки (WL) обходят только «чужие» каналы.** Это TURN-релеи звонков VK, SFU видеосервисов, документы Яндекса и Mail.ru, Yandex Cloud, российские CDN, DNS и каскад через RU-VPS с белым IP. Никакой протокол напрямую к зарубежному VPS (Reality, Hy2, AWG, AnyTLS, TUIC, MASQUE, ShadowQUIC) WL не проходит.
2. **Все WL-каналы хрупкие.** VK регулярно меняет капчу. Яндекс ограничил TURN Телемоста и отвечает капчами OpenFlux. 31.08.2026 Минцифры потребовало вынести белые IP хостеров и CDN в отдельные подсети. У всех этих схем риск бана аккаунта и нарушение ToS. В инсталлер их стоит добавлять только опциональными экспериментальными модулями с закреплённой версией.
3. **Самое срочное для текущего стека — AmneziaWG 3.1.** Amnezia официально пишет, что AWG 2.0 «has become vulnerable to blocking», и рекомендует 3.1. Конфиг 3.x несовместим с клиентами 2.0, поэтому нужен второй интерфейс рядом с 2.0.
4. **Hysteria2 нужно обновлять.** `05-hysteria2.sh` пропускает установку, если бинарник уже есть, и сервер застревает на старой версии. Актуальная версия — app/v2.12.3. Chrome parrot включается сам на клиенте. Gecko (experimental) стоит давать отдельным профилем: он **несовместим по смыслу** с parrot и masquerade.
5. **Xray: стабильный релиз — v26.3.27, всё новее идёт как pre-release (до v26.9.30).** Начиная с v26.9.8 REALITY требует от клиента key share X25519MLKEM768, и клиенты на mihomo без этой поддержки отваливаются. XDNS в v26.9.30 сменил wire-формат. Обновлять core нужно осознанно и с закреплённой версией.
6. **Каскад через RU-VPS (bridge) и «XHTTP за nginx/CDN» дёшево ложатся в существующий Xray-стек.** Оба полезны и вне WL. Обещать с ними обход белых списков нельзя.
7. **Отслеживать, но пока не добавлять:** XDRIVE, MASQUE, VLESS Reverse, TrustTunnel, AnyTLS, ShadowQUIC, Sudoku, TUIC (ждать патч 3x-ui 3.9.x), olcRTC (EoL).

#### Сводная таблица

| Протокол | Новый / оживший | Суть | Статус / версия | Работает в РФ | В инсталлер? | Проверка |
|---|---|---|---|---|---|---|
| AmneziaWG 3.0/3.1 | эволюция | WG + шифрование заголовков (HeaderProtectionKey), паддинг, тайминги; в 3.1 добавлены RandomTrailers и DisableCookies | go v3.0.0 (24.07), 3.1 (12.08.2026), модуль v3.1.20260906 | против ТСПУ — по заявлению вендора; WL не обходит | **да**, вторым профилем рядом с 2.0 | ✏️ |
| Hysteria2 Gecko / Chrome parrot / Mimic / Realms | эволюция | Gecko = Salamander + фрагментация хендшейка; parrot = отпечаток QUIC как у Chrome | app/v2.12.3 (16.09.2026); Gecko experimental | данных по Gecko в РФ нет; WL не обходит | **обновить бинарник — да**, Gecko — опцией, Mimic/Realms — нет | ✏️ |
| VK TURN-туннели (vk-turn-proxy, Turnable, WDTT→CSQTT, qWDTT) | новый | WG/Hy2/VLESS в DTLS через TURN-релеи звонков VK | upstream заморожен (v1.8.3, 16.04); Turnable 0.6.4 (26.09.2026) | **да, под WL**, но хрупко: капча, ~5 Мбит/с на поток | maybe: эксперимент. модуль (Turnable) | ✏️ |
| OpenFlux | новый | IPv4-туннель через Яндекс.Документы, Mail.ru, MAX, cups.online | v0.3.0 (30.09.2026), node-v1.1.0 | задуман под WL; независимых замеров нет; Яндекс уже ставит капчи | maybe: обёртка над `node-install.sh` | ✏️ |
| whitelist-bypass (kulikov0) | новый | туннель через VK Call, Телемост, WB Stream, DION, Bitrix (DC/VP8) | v0.4.4 (22.09.2026) | в основном со слов автора; в трекере много сбоев | maybe: только документированный сценарий | ✏️ |
| olcRTC / Ghostlane | новый | TCP-over-WebRTC через Jitsi, Телемост, WB Stream | upstream архивирован 30.09.2026 (EoL); форк Ghostlane | под WL доступны только медленные vp8/sei/video-каналы | нет, отслеживать | ✏️ |
| Yandex Cloud API Gateway (yac-ws-bridge) | новый | TCP-over-WS через YC API Gateway и gRPC wsSend | v20260714, с июля без коммитов | смешанно: Рязань работает, СПб нет; «как dial-up» | maybe: ручной рецепт + adapter | ✅ |
| Каскад через RU-VPS с белым IP | эволюция | VLESS+Reality/XHTTP на RU-VPS → зарубежный VPS | метод (Xray на обоих концах) | работает, пока IP белый; Минцифры закрывает лазейку | maybe: режим bridge/relay | ✏️ |
| XHTTP через российские CDN | ожил | VLESS+XHTTP → CDN (Beeline/CDNvideo, Timeweb, VK) → nginx → Xray | метод | частично: Yandex режет POST, VK только GET | maybe: универсальная фаза «XHTTP за nginx/CDN» | ✏️ |
| DNS-туннели (dnstt, MasterDnsVPN, Slipstream) | ожил | данные в DNS-запросах через резолвер оператора, Яндекса или НСДИ | MasterDnsVPN 13.06.2026; slipstream v0.1.1; Rust-порт архивирован | зависит от оператора; 0.5–2 Мбит/с, с июня троттлинг 53 | maybe: аварийный fallback | ✏️ |
| Xray finalmask / XDNS / XICMP | эволюция | слой масок в Xray; XDNS — DNS-туннель на mKCP | XDNS сменил формат в v26.9.30 (pre-release) | XDNS под WL в основном **не работает** (апрель–май 2026) | maybe: крайний резерв, без обещаний | ✏️ |
| Xray XDRIVE | новый | прокси через облачные хранилища (Google Drive, WebDAV) | влит 19.09.2026, pre-release | полевых данных нет; аудит: неэффективен на мобильном default-drop | нет, отслеживать | ✏️ |
| MASQUE (CONNECT-IP) | новый | IETF L3-туннель по HTTP/3 и HTTP/2 | Xray (pre-release, 24–27.09), sing-box 1.15.0-alpha.7+ | данных нет; зарубежный QUIC часто режут | нет, «на горизонте» | ✏️ |
| VLESS Reverse (новый) | эволюция | обратный туннель внутри VLESS, `reverse.tag` на UUID | с v25.9.11; помечен нестабильным; регрессия в v26.5+ | WL сам по себе не обходит | нет сейчас; позже для двухсерверной фазы | ✏️ |
| VLESS Encryption (ML-KEM-768) | новый (2025) | PQ-шифрование внутри VLESS | stable с v25.9.5 | данных о выигрыше нет; WL не обходит | опция для XHTTP-через-CDN, не по умолчанию | ✏️ |
| TUIC v5 в 3x-ui | ожил (в панели) | QUIC-прокси, нативный Go-сервер в 3x-ui | 3x-ui v3.9.0 (03.10.2026) | как Hy2-класс; WL не обходит | позже, после патча 3.9.x | ✅ |
| AnyTLS | новый | TLS-туннель с паддингом против TLS-in-TLS | anytls-go v0.0.13; sing-box 1.12+, mihomo | данных нет; WL не обходит | низкий приоритет, только через sing-box | ✏️ |
| TrustTunnel (AdGuard) | новый | VPN поверх HTTPS (H1/H2/H3) | v1.1.0 stable, v1.3.0-beta.1 | данных нет; WL не обходит | низкий приоритет (нужен домен) | ✏️ |
| ShadowQUIC | новый | QUIC + JLS-камуфляж, «Reality по UDP» | v0.4.0 (30.09.2026) | данных нет; QUIC часто режут | нет | ✅ |
| Sudoku | новый | обфускация под ASCII или шум через таблицы судоку | v0.5.1 (25.09.2026), mihomo | данных нет | нет | ✏️ |

#### Подробнее

##### AmneziaWG 3.0 / 3.1 ✏️ — главный кандидат
- Релизы: amneziawg-go v3.0.0 (2026-07-24); kernel и tools v3.0.20260730; линия 3.1 (v3.1.20260812) синхронно в go, kernel и tools; модуль уже на v3.1.20260906 (поддержка ядра 7.2).
- В 3.0 появились HeaderProtectionKey (ChaCha20 по заголовкам), ContentPaddingAddition, тайминги и диапазон PersistentKeepalive, S1–S4 теперь ≥12 байт. В 3.1 добавились RandomTrailers (должны совпадать на обеих сторонах) и DisableCookies. Режимы 1.0/1.5/2.0 удалены: **конфиг 3.x несовместим с клиентами 2.0**.
- Клиенты: AmneziaVPN ≥5.0.2.1 (Win, macOS 13+, Linux, Android 9+, iOS), отдельный AmneziaWG для Android (GitHub, в Play пока нет) и Windows 3.1.0, mihomo v1.19.30. Keenetic и OpenWrt v3 не поддерживают.
- Вендор: «AmneziaWG 2.0, like 1.5, has become vulnerable to blocking», рекомендует 3.1. Независимых датированных замеров блокировки 2.0 нет.
- Источники: [docs.amnezia.org](https://docs.amnezia.org/documentation/instructions/new-amneziawg-selfhosted/), [блог 3.1](https://amnezia.org/blog/amneziawg-3-1-is-here), [tools releases](https://github.com/amnezia-vpn/amneziawg-tools/releases), [kernel tags](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/tags), [Хабр 1080342](https://habr.com/ru/articles/1080342/).

##### Hysteria2: Gecko, Chrome parrot, Mimic, Realms ✏️
- Хронология: Realms в app/v2.9.0 (10.05), UPnP/NAT-PMP в v2.9.3 (27.06), Gecko в v2.9.2 (23.05, **experimental**), ECH в v2.10.0 (13.07), Chrome parrot по умолчанию в v2.11.0 (01.08), Mimic в v2.12.0 (06.08), последняя v2.12.3 (16.09.2026).
- Parrot настраивается **на клиенте** (`disableChromeParrot=false` по умолчанию). С сертификатом Ed25519 хендшейк падает; у нас ECDSA P-256, поэтому всё в порядке.
- Gecko работает поверх Salamander, и трафик перестаёт выглядеть как QUIC. Поэтому Gecko и parrot — **взаимоисключающие режимы**, и masquerade под HTTP/3 с obfs теряет смысл.
- Mimic требует Linux и root **на обеих сторонах**, телефонам и Windows он бесполезен. Realms не нужен VPS с публичным IP.
- Клиенты Gecko: sing-box 1.14.0, mihomo, Xray (finalmask salamander.packetSize), 3x-ui ≥3.4.0. NekoBox/Throne не поддерживают ([#1563](https://github.com/throneproj/Throne/issues/1563)). Встречается молчаливая подмена gecko→salamander при импорте URI.
- Источники: [releases](https://github.com/apernet/hysteria/releases), [Changelog](https://v2.hysteria.network/docs/Changelog/), [sing-box 1.14.0](https://github.com/SagerNet/sing-box/releases/tag/v1.14.0), [Tele2: полный блок QUIC](https://ntc.party/t/hysteria2-не-работает-на-теле2-походу-полный-блок-quic/20340).

##### VK TURN-туннели: vk-turn-proxy, Turnable, WDTT/CSQTT, qWDTT ✏️
- Схема: WG/Hy2 (с v1.8.0 и VLESS через KCP+smux) упаковывается в DTLS 1.2 и уходит через TURN-релеи звонков VK на VPS (UDP 56000). Для DPI это звонок на IP VK из белого списка. MTU 1280, около 5 Мбит/с на поток (обходят через `-n`). Телемост «закрыт»: Яндекс ограничил свои TURN только своими IP.
- Upstream [cacggghp/vk-turn-proxy](https://github.com/cacggghp/vk-turn-proxy) (~4k★) **заморожен**: v1.8.3 вышел 16.04.2026, капча из [#182](https://github.com/cacggghp/vk-turn-proxy/issues/182) (25.06) не исправлена, PR #183 не влит.
- WDTT (amurcanov) архивирован 11.09.2026. Официальный наследник — [CSQTT](https://github.com/amurcanov/csqtt) (Rust, PolyForm Noncommercial, без бинарных релизов). qWDTT — **отдельный** форк SpaceNeuroX (v1.4.4, 13.09). Серверные протоколы семейства **между собой несовместимы**.
- [Turnable](https://github.com/TheAirBlow/Turnable) — отдельная реализация (GPL-2.0, ~315★, 0.6.4 от 26.09.2026). Бинарники под linux amd64/arm64/arm/riscv64. Нужен аккаунт VK и `call_id` в конфиге. Android-клиент — сторонний [WireTurn](https://github.com/spkprsnts/WireTurn) v9.1 (29.09), клиента под iOS нет. Самый живой iOS-клиент — [anton48/vk-turn-proxy-ios](https://github.com/anton48/vk-turn-proxy-ios/releases) (build443 от 30.09 чинит капчу).
- В РФ: подтверждено на ntc.party (весна 2026). На Хабре 25.04: «работает, но быстро банят». VK несколько раз ломал капчу (08.09, 30.09.2026), фиксы выходили за часы или дни. Источники: [ntc 21884](https://ntc.party/t/рабочий-обход-белых-списков/21884), [ntc 24402](https://ntc.party/t/24402), [pvsm/Хабр](https://www.pvsm.ru/vpn/450362).

##### OpenFlux ✏️
- [p1neappleXpress/OpenFlux](https://github.com/p1neappleXpress/OpenFlux): создан 21.06.2026, 1941★, GPL-3.0+, v0.3.0 (30.09.2026), ядро ноды node-v1.1.0. Транспорты: Яндекс.Документы, Mail.ru Docs, cups.online, MAX (experimental), Direct.
- Официальный [`deploy/node-install.sh`](https://raw.githubusercontent.com/p1neappleXpress/OpenFlux/main/deploy/node-install.sh) работает на любом systemd 219+ (amd64/arm64/arm): сервис `openflux-node@<channel>`, ufw/firewalld, режим **l4 (gVisor)**. Бинарники выходят с SHA256SUMS. На каждый канал нужны hex-ключ и URL документа или комнаты.
- Хрупкость: Яндекс ставит капчу ([#89](https://github.com/p1neappleXpress/OpenFlux/issues/89), [#90](https://github.com/p1neappleXpress/OpenFlux/issues/90)), фикс регрессировал ([#106](https://github.com/p1neappleXpress/OpenFlux/issues/106)). Капчи бывают и на стороне выходной ноды. Проект называет себя «research tool», поддерживает только IPv4, iOS есть только в TestFlight.
- Предупреждение про бан аккаунта MAX есть в форке [damnurmum](https://github.com/damnurmum/OpenFlux-Android), а не в основном README. Независимых замеров под WL нет.

##### whitelist-bypass (kulikov0) ✏️
- [Репозиторий](https://github.com/kulikov0/whitelist-bypass): MIT, 1698★, [v0.4.4](https://github.com/kulikov0/whitelist-bypass/releases/tag/v0.4.4) (22.09.2026). Платформы: VK Call, Телемост, WB Stream, DION, Bitrix24. Есть APK, .ipa (sideload), Electron и CLI.
- Headless-creator на VPS требует куки, экспортированные из GUI. Автор советует запускать creator **с домашнего IP**, потому что IP дата-центра ведёт к бану аккаунта ([SETUP.md](https://github.com/kulikov0/whitelist-bypass/blob/main/docs/SETUP.md)). Systemd-юнитов нет ([#173](https://github.com/kulikov0/whitelist-bypass/issues/173)).
- [net4people #618](https://github.com/net4people/bbs/issues/618) написал сам автор. Цифра 6.5 Мбит/с — тоже от автора. Нестабильность: [#184](https://github.com/kulikov0/whitelist-bypass/issues/184) (Телемост, 0/8 загрузок), [#169](https://github.com/kulikov0/whitelist-bypass/issues/169).

##### olcRTC / Ghostlane ✏️
- [openlibrecommunity/olcrtc](https://github.com/openlibrecommunity/olcrtc) **архивирован 30.09.2026 (EoL)**. Есть только тег v0.0.1, релизов нет, сборка через Podman/mage. «Наследник» [snolc](https://github.com/owenewans/snolc) пока WebRTC не содержит.
- По [матрице совместимости](https://github.com/openlibrecommunity/olcrtc/blob/master/docs/settings.md): на Телемосте datachannel убран, работает только vp8channel. На WB Stream datachannel требует прав модератора. Быстрый datachannel стабилен только на Jitsi, а Jitsi в WL не входит.
- Форк [ghostlane-project/olcrtc](https://github.com/ghostlane-project/olcrtc) (2★) и клиент [Ghostlane](https://github.com/ghostlane-project/ghostlane/releases) (v1.0.446 от 01.10.2026) активны. Датированных замеров за сентябрь–октябрь нет.

##### Yandex Cloud API Gateway / yac-ws-bridge ✅
- [noiseonwires/yac-ws-bridge](https://github.com/noiseonwires/yac-ws-bridge): adapter и helper на Go, функция на JS, клиент на MAUI. Релизы с v20260516 по v20260714, после этого коммитов нет. Бинарники под linux amd64/arm64 и win; под iOS и macOS нужно собирать самим.
- [ntc 24735](https://ntc.party/t/как-обойти-белые-списки-используя-wss-и-yandex-cloud/24735) (13.05–02.06.2026): в Рязани (T2, МТС) работает, в СПб не работает; «как dial-up», gateway терминирует соединения. В README автора: до ~20 Мбит/с. В статье [Хабр 1027276](https://habr.com/ru/articles/1027276/) у методов №2/№3 (Cloud Functions SOCKS5 и API Gateway) 40/5 Мбит/с.
- Нужны аккаунт YC с картой, деплой функции и шлюза, обфускация JS. Автор пишет: «сколько пройдёт до бана от Яндекса — непонятно».

##### Каскад через RU-VPS с белым IP ✏️
- Хронология: гайд по VK Cloud ([kort0881 #21](https://github.com/kort0881/russia-whitelist/discussions/21), 31.12.2025); массовая потеря серверов 24.01.2026 ([xakep](https://xakep.ru/2026/01/26/rkn-whitelist/)); YC убран из «гарантированных» 17.04.2026 ([vpn-setup](https://github.com/Sergei-thinker/vpn-setup)); требование Минцифры от 31.08.2026 ([CNews](https://www.cnews.ru/news/top/2026-08-31_mintsifry_potrebovalo_ot)).
- В отчётах [net4people #650](https://github.com/net4people/bbs/issues/650) (19.08.2026) у одного всё работает, у другого полный блэкаут. VK Cloud принимает только юрлиц, Yandex Cloud банит за прокси ([ntc 24230](https://ntc.party/t/vpsvds-работающие-при-белых-списках/24230)).
- SNI белого домена (storage.yandex.net, userapi.com, cdnvideo.ru и т.п.) взят из [Хабр 1027276](https://habr.com/ru/articles/1027276/). Белые IP проверяют по [hxehex/russia-mobile-internet-whitelist](https://github.com/hxehex/russia-mobile-internet-whitelist) и [openlibrecommunity/rewl](https://github.com/openlibrecommunity/rewl) (twl архивирован 30.09.2026).

##### XHTTP через российские CDN ✏️
- Источник — [frank-underwood64/whitelists_bypass](https://github.com/frank-underwood64/whitelists_bypass/discussions/1) (28★, обсуждение с 27.07.2026). Yandex CDN режет POST (30.07), к сентябрю публичный конфиг перестал работать. VK Cloud работает только в режиме packet-up на GET. Рабочими называют Beeline/CDNvideo и Timeweb. Автор продаёт платные гайды, на них есть жалобы.
- Технически это не классический domain fronting, а собственный CDN-ресурс на edge-адресах из WL. Требование Минцифры от 31.08.2026 закрывает именно этот механизм.

##### DNS-туннели: dnstt, MasterDnsVPN, Slipstream ✏️
- [MasterDnsVPN](https://github.com/masterking32/MasterDnsVPN) (~7.1k★, последний релиз 13.06.2026, есть Docker). [slipstream](https://github.com/EndPositive/slipstream) v0.1.1 (C), Rust-порт архивирован 28.08.2026. Менеджер [slipgate](https://github.com/anonvector/slipgate) v1.6.4 (Ubuntu 20.04+, systemd, нужны домен и 53/udp). Клиенты: SlipNet (только Android), [dnstt_xyz_app](https://github.com/dnstt-xyz/dnstt_xyz_app).
- В РФ ([ntc 23468](https://ntc.party/t/белые-списки-че-делать/23468), [23730](https://ntc.party/t/актуальность-slipstreamdnstt-для-обхода-бс/23730), [24640](https://ntc.party/t/кто-то-использовал-masterdnsvpn/24640)): dnstt работает через НСДИ 195.208.4.1. На Tele2 СПб до ~11/1.3 Мбит/с, на МТС LTE 0.5–1.5 Мбит/с. С июня жалобы на ~30 КБ/с и троттлинг 53-го порта. На Мегафоне НСДИ не работает.

##### Xray finalmask / XDNS / XICMP ✏️
- PR: XDNS [#5560](https://github.com/XTLS/Xray-core/pull/5560) (31.01), XICMP #5633 (02.02), header-custom/noise #5657 и Sudoku-маска #5685 (07.03), XMC #6210 (11.07), udpHop #6327 (08.09), рефактор XDNS [#6718](https://github.com/XTLS/Xray-core/pull/6718) (29.09, новый JSON-формат, **несовместим**). [3x-ui v3.9.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0) сам мигрирует маски; клиентам нужен core ≥26.9.30.
- В РФ XDNS под WL в основном не работает: [ntc 23018 #50](https://ntc.party/t/23018/50) (02.04), [24845 #39](https://ntc.party/t/24845/39) (Билайн СПб, 23.05). MasterDnsVPN быстрее.
- Побочные выводы для текущего стека: коммит 28.07.2026 «XHTTP client: Reduce default maxConnections 6→3 for anti-TSPU»; [#6303](https://github.com/XTLS/Xray-core/pull/6303) запрещает **outbound** VLESS/Trojan без TLS и без encryption (касается клиентских конфигов, на REALITY не влияет).

##### Xray XDRIVE ✏️
- [#5645](https://github.com/XTLS/Xray-core/pull/5645) (RPRX, влит 19.09.2026), Google Drive + template [#6748](https://github.com/XTLS/Xray-core/pull/6748) (Risaro), local #6745. OneDrive, S3 и FTP пока только цель; Яндекс.Диск подключается через WebDAV-template.
- Google Drive: 50 МБ за 21.6 с (~18.5 Мбит/с) из NL. Яндекс.Диск требует **платного** тарифа (на бесплатном 402), аплоад ~440 KiB/s на 32 потоках. Аудит [just1kbot#319](https://github.com/justik13/just1kbot/pull/319): неэффективен против мобильного default-drop, rate limit 429 уже при 2–4 пользователях, мобильных клиентов нет.

##### MASQUE (CONNECT-IP) ✏️
- Xray: outbound [#6807](https://github.com/XTLS/Xray-core/pull/6807) (24.09), H2 Extended CONNECT #6810 (26.09, клиент), inbound [#6844](https://github.com/XTLS/Xray-core/pull/6844) (27.09). sing-box: 1.15.0-alpha.7 (22.09), alpha.10 (03.10); в stable 1.14.2 MASQUE нет. mihomo: outbound ещё до v1.19.28, ориентирован на WARP.
- В РФ зарубежный QUIC часто режут ([net4people #108](https://github.com/net4people/bbs/issues/108)), поэтому реалистичен только вариант на HTTP/2 по TCP. Полевых данных нет.

##### VLESS Reverse (новый) ✏️
- [#5101](https://github.com/XTLS/Xray-core/pull/5101) влит 09.09.2025, первый релиз v25.9.11. Legacy reverse удалён весной 2026 ([3x-ui #4115](https://github.com/MHSanaei/3x-ui/issues/4115)). Upstream помечает функцию нестабильной. [#6242](https://github.com/XTLS/Xray-core/issues/6242): на v26.5+ сломана связка с Observatory/балансировкой (not planned).
- В 3x-ui reverse-тег задаётся в UI, но клиентский конфиг не генерируется ([#5213](https://github.com/MHSanaei/3x-ui/issues/5213)). Гайды под WL используют прямой каскад, а не reverse.

##### VLESS Encryption (mlkem768x25519plus) ✏️
- [#5067](https://github.com/XTLS/Xray-core/pull/5067) влит 28.08.2025, **stable с v25.9.5** — это не новинка 26.9.x. 3x-ui генерирует ключи. Работает в Xray-клиентах и mihomo ≥1.19.13; **не работает в upstream sing-box** ([#3599](https://github.com/SagerNet/sing-box/issues/3599)): Hiddify, SFA/SFI, Karing отпадают.
- REALITY уже даёт X25519MLKEM768 в рукопожатии (с v25.5.16). Важно: **с Xray v26.9.8 REALITY требует от клиента key share X25519MLKEM768**, и mihomo-клиенты без него отваливаются ([3x-ui PR #6451](https://github.com/MHSanaei/3x-ui/pull/6451)).

##### TUIC v5 в 3x-ui ✅
- v3.8.0 (14.09.2026) запускал Rust-сайдкар в обход маршрутизации ([#6560](https://github.com/MHSanaei/3x-ui/issues/6560)). [v3.9.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0) (03.10.2026, [PR #6577](https://github.com/MHSanaei/3x-ui/pull/6577)) встроил нативный Go-сервер, трафик идёт через loopback SOCKS5 в Xray (порты 64001–65000). Перезапуск TUIC перезапускает весь Xray.
- Открытые баги: [#6704](https://github.com/MHSanaei/3x-ui/issues/6704) (routing), [#6714](https://github.com/MHSanaei/3x-ui/issues/6714) (учёт трафика). Против ТСПУ ничего не добавляет к Hy2.

##### AnyTLS, TrustTunnel, ShadowQUIC, Sudoku — коротко
- **AnyTLS ✏️**: [Xray отказался](https://github.com/XTLS/Xray-core/pull/5907) (PR закрыт RPRX 10.04.2026), поэтому в 3x-ui его не добавить; нужен отдельный sing-box. AnyTLS+Reality работает только в sing-box. v2rayNG ([#5684](https://github.com/2dust/v2rayNG/issues/5684)) и Hiddify его не умеют. Протокол распознаётся по признакам ([net4people #528](https://github.com/net4people/bbs/issues/528)). Версия 0.0.x, «демонстрационная».
- **TrustTunnel ✏️**: [v1.1.0](https://github.com/TrustTunnel/TrustTunnel/releases) (01.09.2026), есть в mihomo с v1.19.21, в sing-box нет ([#3723](https://github.com/SagerNet/sing-box/issues/3723)). Защиты от active probing нет ([#100](https://github.com/TrustTunnel/TrustTunnel/issues/100): на чужой SNI сервер рвёт соединение). Нужны домен и сертификат.
- **ShadowQUIC ✅**: [v0.4.0](https://github.com/spongebob888/shadowquic/releases/tag/v0.4.0) (30.09.2026), mihomo v1.19.29. В sing-box отказано ([#3140](https://github.com/SagerNet/sing-box/issues/3140)), в 3x-ui только запрос ([#6645](https://github.com/MHSanaei/3x-ui/issues/6645)). Нишу UDP/QUIC уже занимает Hy2.
- **Sudoku ✏️**: основной репозиторий — [SUDOKU-ASCII/sudoku](https://github.com/SUDOKU-ASCII/sudoku) (v0.5.1, 25.09.2026), в mihomo с ≤v1.19.21. Маска «sudoku» в Xray finalmask с этим протоколом **несовместима**. Данных по РФ нет.

#### Кандидаты в инсталлер

Порядок — по соотношению пользы к трудозатратам. Оценки даны для одного разработчика с ИИ-ассистентом на существующей кодовой базе.

| # | Что | Обоснование | Трудозатраты |
|---|---|---|---|
| 1 | **AmneziaWG 3.1 вторым профилем** (отдельный интерфейс и порт, 2.0 остаётся как запасной) | вендор объявил 2.0 уязвимым; модуль 3.x приходит из того же PPA/DKMS (ядро ≥6.7 = HWE); клиенты есть на всех основных платформах | 1–2 дня: генерация HeaderProtectionKey, RandomTrailers, S1–S4 ≥12, второй интерфейс, вывод с пометкой «AmneziaVPN ≥5.0.2.1» |
| 2 | **Обновление Hysteria2 до 2.12.x** + опциональный профиль Gecko | сейчас при уже установленном бинарнике фаза пропускает установку; parrot бесплатен для клиентов; Gecko нужен на сетях, где режут весь QUIC | 0.5 дня на `hysteria update` или повторный get.hy2.sh с закреплённой версией; 0.5–1 день на второй профиль Gecko (отдельный порт, без masquerade, предупреждение о клиентах) |
| 3 | **Ревизия обновления Xray-core** (не новый протокол, а гигиена) | stable v26.3.27 против pre-release 26.9.x; REALITY с v26.9.8 требует от клиента key share X25519MLKEM768; XDNS сменил формат; XHTTP maxConnections; #6303 для клиентских ссылок; путь `xray-linux-amd64` в 04-фазе ломает ARM | 0.5–1 день: закрепить версию, учитывать архитектуру, записать требования к клиентам в итоговый вывод |
| 4 | **Режим bridge/relay** (RU-VPS → зарубежный выход) | тот же Xray, клиенты прежние; полезен и при L3-блоке зарубежного IP | 2–3 дня: второй прогон с ролью «мост», выбор SNI вручную, офлайн-подсказка по cidrwhitelist (hxehex/rewl), предупреждения про ToS и временность белого IP |
| 5 | **Фаза «XHTTP за nginx/CDN»** (packet-up / GET-upstream) | универсальна: Cloudflare на домашнем интернете и российские CDN по инструкции; база для VLESS Encryption через CDN | 2–3 дня: нужны домен и сертификат, nginx, inbound XHTTP; настройка CDN только инструкцией без гарантий |
| 6 | **Экспериментальный WL-модуль: TURN** (одна реализация, предпочтительно Turnable) | единственный класс методов, который подтверждённо проходит мобильные WL; на сервере один бинарник + systemd | 1–2 дня: бинарник с закреплённой версией, отдельный WG-интерфейс (совместимость AWG-junk внутри DTLS не проверена), конфиг с `call_id`, явные предупреждения (аккаунт VK, капча, ToS) |
| 7 | **Экспериментальный WL-модуль: выходная нода OpenFlux** | официальный `node-install.sh` со SHA256 и ARM, клиенты на всех платформах | 0.5–1 день как тонкая обёртка с закреплённым node-v1.1.0; вернуться к вопросу через 2–3 месяца |
| 8 | **Аварийный DNS-туннель** (dnstt или MasterDnsVPN, не slipstream) | последний рубеж, когда WL убил всё остальное | 1–2 дня: делегированный NS, порт 53 конфликтует со stub-резолвером systemd-resolved (биндить на публичный IP или отключать DNSStubListener) |
| 9 | TUIC (одной галочкой через 3x-ui) | бесплатно при 3x-ui ≥3.9.x, но выигрыша против ТСПУ почти нет | 0.5 дня, но только после патча 3.9.x и закрытия #6704/#6714 |

Отслеживать, но пока не брать: XDRIVE (ждать stable и клиентов), MASQUE (ждать sing-box 1.15 stable и отчётов про H2/TCP), VLESS Reverse (для будущей двухсерверной фазы), yac-ws-bridge (только ручной рецепт), whitelist-bypass (только документированный сценарий), TrustTunnel (ждать закрытия #100), AnyTLS (только если в стек придёт sing-box). Не брать: olcRTC (EoL), ShadowQUIC, Sudoku, XICMP, Mimic, Realms.

#### Не проверено / выпало

##### Опровергнутый хайп (в выводы не включать)
- «Beeline сломал dnstt» (март–апрель 2026): подтверждения нет; сам пост датирован 2025-12-24. «Работает через резолверы МТС» неточно: в отчётах работал НСДИ.
- «Хабр 1036100 тестирует DNS-туннели под WL»: это обзор с советами без замеров.
- «XDNS выживает при WL»: в найденных отчётах апреля–мая 2026 обратное.
- «Gecko + Chrome parrot вместе усиливают маскировку»: это взаимоисключающие режимы.
- «AnyTLS легко добавить inbound'ом в 3x-ui»: Xray отказался от AnyTLS.
- «#6303: сервер Xray не стартует без шифрования» / «VLESS Encryption стал мейнстримом»: #6303 касается только outbound без TLS и без encryption.
- «Яндекс.Диск — идеальный backend XDRIVE»: нужен платный тариф, аплоад около 440 KiB/s.
- «AWG: держать 2.0»: устарело, вендор рекомендует 3.1.
- «olcRTC datachannel до 10 МБ/с под WL»: так быстро работает только Jitsi, а Jitsi вне WL.
- «Проверка соответствия SNI и IP на ТСПУ» (Хабр 979128): гипотеза из чатов без доказательств.
- samosvalishe/free-turn-proxy: удалён (404). «qWDTT — продолжение WDTT»: неверно, наследник WDTT — CSQTT.
- «TrustTunnel без мультиплексирования»: неверно, H2/H3 мультиплексируют.

##### Не проверено ❓ (собрано разведкой, фактчекер не подтверждал)
- **Cheburnet** ([nil2x/cheburnet](https://github.com/nil2x/cheburnet)): SOCKS через сообщения и посты VK, OK, Яндекс.Диск и IMAP. Скрытый канал, по смыслу похож на OpenFlux.
- **«Белый SNI» / fake-TLS-десинхронизация против отсечки 16 КБ** (zapret, `--dpi-desync-fake-tls-mod=rnd,sni=...`): клиентская сторона. Для Reality вывод тот же: выбирать dest/SNI, к которому отсечка не применяется ([ntc 22516](https://ntc.party/t/16кб-блокировка/22516?page=4)).
- **XHTTP с xmux / gRPC на 443** против блокировки IP по числу сессий. Это настройка транспорта, перекликается с maxConnections 6→3 ([ntc 24845](https://ntc.party/t/24845)).
- **XHTTP (в том числе XHTTP/3) как основной транспорт вместо RAW+Vision**, gRPC объявлен deprecated ([Хабр 1047442](https://habr.com/ru/articles/1047442/)).
- **NaiveProxy** снова в обсуждениях ([ntc 23843](https://ntc.party/t/naive-proxy-пока-лучший-вариант/23843)); slipgate ставит его вместе с Caddy.
- **Hysteria2 внутри Xray** (inbound, Salamander через finalmask, udpHop) — альтернатива отдельному бинарнику apernet.
- **SSH-туннель**, **paqet** (raw-сокеты без SYN, клиенту нужен root), **Tor WebTunnel** (Snowflake в РФ режут по отпечатку), **mieru**.
- **MTProto Fake-TLS как inbound в 3x-ui v3.9.0** — только для Telegram.
- Дубли, влитые в проверенные пункты выше: TURN-туннели (+Turnel/KillTheCensorship, SRTP-мимикрия), WebRTC/SFU-туннели, DNS-туннели (NoizDNS, VayDNS, findns; статья wiki.zapret.moe про DNAT на НСДИ в августе 2026 не проверена), XHTTP через российские CDN, каскад через RU-VPS, finalmask, TrustTunnel, TUIC.
- Отдельно не проверено: скорость VK TURN около 250 кбит/с с конца апреля (первоисточник Хабр отдаёт 451); «Yandex Cloud: вечные баны за прокси» по тексту ToS; поддержка нового формата XDNS в iOS-клиентах; AmneziaWG 3.x отдельным приложением под iOS.

### 5.6 Ожившие классические протоколы и техники (доп. проход)

Обозначения статуса: ✅ подтверждено, ✏️ исправлено (в тексте уже исправленные факты), ❓ не проверено. Срез на 04.10.2026.

#### Краткий вывод

- По-настоящему ожившего классического протокола для РФ не нашлось. Почти всё, что числилось «возрождённым», либо эволюционирует в обычном режиме (OpenVPN 2.7, wstunnel, shadowsocks-rust, gost, stunnel), либо ожило только в Иране (ICMP, GRE/IPIP, мосты Iran↔kharej).
- В РФ всё, что идёт по TLS/WS к зарубежному ДЦ или Cloudflare, упирается в заморозку TCP на ~16 КБ (с 09.06.2025) и в мобильные белые списки. Обёртка протокола от этого не спасает, нужен свой непубличный IP, российский релей или CDN.
- Голые протоколы мертвы: L2TP, PPTP, IKEv2, SSTP, OpenVPN, SOCKS5, Shadowsocks/SS-2022, obfs4 без zapret. Коммерческие stealth-режимы (Mullvad, Proton, Windscribe) в РФ тоже ненадёжны: их серверы блокируют по IP.
- Баны 08–09.2026 у хостеров выборочные, по отдельным IP, а не по целым ASN (n4p #671). Под них попали и IP только с AWG, так что ни SPA/port-knocking, ни ротация /64 в IPv6 не помогают.
- Единственный кандидат с `worth_adding: yes`: **WARP как outbound** на зарубежном VPS (egress для AI и стриминга), с запасным путём на случай 429 при регистрации wgcf.
- Опциональные модули «по флагу»: pingtunnel как аварийный ICMP-канал, GRE/IPIP как транспорт каскада, Psiphon Conduit как «помочь другим», dual-stack listen на `[::]`. Всё выключено по умолчанию.

#### Сводная таблица

| Техника | Суть | Почему ожила / дата | Работает в РФ сейчас | Стоит ли в инсталлер | Статус |
|---|---|---|---|---|---|
| ICMP-туннели (pingtunnel, udp2raw icmp, hans) | TCP/UDP внутри ICMP echo, внутри WG/KCP | pingtunnel 2.9/2.10 (13–14.09.2026) после паузы с 2.8 (29.11.2023). Иран, Pingify 26.09.2026: ICMP — единственный прямой транспорт, который ходил при бане IP | Под белыми списками по-разному: где-то совсем не ходит, где-то почти куда угодно (Хабр 17.05.2026). При бане IP не проверялось | Может быть: только флагом, как аварийный канал | ✏️ |
| Fake-TCP (udp2raw / phantun) | UDP в пакетах с TCP-заголовками через raw socket | phantun v0.8.0/0.8.1 (22–23.08.2025), форк sagan fix–fix6 (09.2025). udp2raw застыл на 20230206.0 | Отчётов нет. В #505 (01.2024) DPI ловил все режимы udp2raw | Нет | ✏️ |
| WireGuard через wstunnel | WG в WS/HTTP2/WebTransport по TLS на 443 | v10.7.0 (28.08.2026) добавил WebTransport, v11.0.0 (19.09.2026) сделал его стабильным. Не возрождение, обычное развитие | Свежих отчётов нет. TLS к зарубежному ДЦ, поэтому попадает под 16 КБ и белые списки | Нет | ✏️ |
| OpenVPN 2.7 / OVPN+Cloak | OpenVPN TCP 443, tls-crypt-v2; Cloak маскирует под браузерный TLS | OpenVPN 2.7.0 (11.02.2026). Cloak 2.10.0 (11.10.2024), 2.11.0 (30.06.2025), 2.12.0 (23.07.2025, только сигнатуры) | Голый: нет (с 02.2026 по обзорам). С Cloak медленно и частично; Amnezia его сворачивает | Нет (legacy, можно упомянуть в доках) | ✏️ |
| Shadowsocks / SS-2022 / Outline SS-over-WS | SS-2022 (shadowsocks-rust), Outline WSS, prefix | ss-rust v1.24.0 (10.12.**2025**), v1.25.0 (26.08.2026). outline-ss-server WS с v1.9.0 (11.02.2025), последний v1.9.2 (04.03.2025). Outline Foundation с 01.2026 | Голый SS: нет, режут с 04.2024 по отпечатку TLS внутри туннеля (#363). WSS этот отпечаток не снимает | Может быть: только как внутреннее звено каскада RU-релей→зарубеж (sing-box/xray). Клиентам не выдавать | ✏️ |
| SOCKS5/HTTP CONNECT в TLS (stunnel, gost v3) | Классический прокси в TLS | stunnel 5.80 (CVE-2026-70367), последний 5.82 (17.09.2026). gost v3.3.0 (30.08.2026), nightly 3.3.1 (03.10.2026) | Голый SOCKS5 заблокирован с конца 11.2025. В TLS: данных нет, к зарубежу 16 КБ | Нет (NaiveProxy/VLESS/каскад покрывают) | ✏️ |
| GRE / IPIP / SIT / GRE-FOU | L3-туннель без шифрования между серверами, внутри WG/AWG/Xray | Иран: скрипты Azumi67 (~409★), kalilovers, Pingify. Дата 31.07.2025 не проверена | Не проверено. Есть старые жалобы, что провайдеры режут GRE (протокол 47) | Может быть: опция транспорта каскада, низкий приоритет, обязателен firewall по IP пира | ❓ |
| SSTP (SoftEther / accel-ppp / MikroTik) | PPP поверх HTTPS 443 | Возрождения нет. SoftEther тег 5.2.5188 (17.07.2025), коммиты в master до 01.10.2026 (OpenSSL 4.0) | Нет: ТСПУ определяет по отпечатку TLS | Нет (для MikroTik/Keenetic дать AWG) | ✏️ |
| L2TP/IPsec, PPTP, IKEv2 | Классический IPsec-VPN без обфускации | Только whitelist РКН: 57 тыс.+ адресов 1730 компаний (23.04.2026) | Нет, кроме корпоративного whitelist. L2TP протокольно с конца 11.2025, IKEv2 регионально с 29.05.2022 | Нет | ✅ |
| Port-knocking / SPA (fwknop) | Порт закрыт до подписанного пакета | Возрождения нет, fwknop 2.6.11 (07.02.2025) | Не помогает: баны по IP у хостеров, IP только с AWG тоже попали | Нет (разве что как hardening SSH) | ✏️ |
| IPv6-обходы | Обход через v6, ротация /64 | Это закрывающаяся лазейка: с 07.10.2024 ТСПУ режет OpenVPN по v6 у РТ. SNI по v6 «через раз» — разовый отчёт 06.2025 | Частично и непредсказуемо на проводных. На мобильных и под белыми списками нет | Может быть: dual-stack `[::]` и v6 запасным endpoint'ом. Ротацию /64 не делать | ✏️ |
| Мосты Iran↔kharej (Backhaul, BackPack, rathole, Pingify) | Мультитранспортные туннели сервер→сервер | Иран: отключение 8–18.01.2026, затем allowlist. Backhaul 877★, BackPack 435★. Pingify/Eris по 1–2★, rathole стоит на v0.5.0 (10.2023) | Не проверено | Нет как зависимость. Можно взять идею UX: замер и выбор транспорта между нодами | ✏️ |
| Psiphon Conduit | Волонтёрский in-proxy узел Psiphon в Docker | CLI 1.0.0 (25.01.2026), 2.0.0 (20.02), 2.1.0 (30.09.2026). Пост «To Russians with Love» (16.06.2026) | Цифр по РФ нет, Psiphon работает с перебоями | Может быть: опция «помочь другим», выключена по умолчанию. Риск засветить IP VPS | ✅ |
| Tor Snowflake (standalone proxy) | Мост Tor через WebRTC/DTLS | С 30.03.2026 ТСПУ блокировали DTLS-отпечаток standalone-прокси (pion). Исправлено в v2.13.1 (16.04.2026) флагом `-covertdtls-config randomizemimic`. «~100%» на 01.10.2026 сообщил один тестер | Да, на проводных. Под белыми списками нет | Нет (своим пользователям ничего не даёт, юридический риск) | ✏️ |
| Tor obfs4 / meek / Conjure | Классические мосты Tor | obfs4 режут DPI с 24.12.2025. meek вернули встроенным мостом на другом CDN (TB 15.0a4). Conjure отлаживают в Китае (01.10.2026) | obfs4 только с zapret (multisplit). Conjure по РФ: данных нет | Нет | ✏️ |
| VLESS/Trojan/VMess WS/gRPC за Cloudflare | Прокси за CDN, IP сервера скрыт | Не возрождение, а деградация: 16 КБ к CF с 09.06.2025, трафик CF в РФ −30% (29.12.2025). Иран собирает фрагменты ClientHello (#628, 10.06.2026) | Нет / нестабильно. На российских PoP CF (DME/LED/KJA) часть доменов фильтруется | Нет для РФ. Только как необязательный вариант для пользователей вне РФ | ✅ |
| CF Workers-панели (BPB, edgetunnel) | Бессерверный VLESS/Trojan на Workers | BPB v5.1.1 (19.07.2026, дашборд и Telegram-бот). XHTTP/VLESS Encryption/FinalMask с v4.2.2. ~13.6k★ | Слабо: 16 КБ к CF. На workers.dev иногда лучше своего домена (PR 25.09.2026) | Нет (это не VPS). Заметка в доках для IR/CN | ✅ |
| **Cloudflare WARP как outbound** | wgcf-профиль → WireGuard outbound в Xray/sing-box | Не новое (с 2022). Новое: 429 на старый отпечаток wgcf с IP дата-центров (09.2026), wgcf v2.3.0 (18.09.2026) | Клиентом из РФ: нет или эпизодически. Как egress зарубежного VPS: да | **Да**: опциональный WARP-outbound для маршрутизации отдельных доменов (AI, стриминг) | ✏️ |
| CDN domain fronting (Shir-o-Khorshid) | Android-форк Psiphon с FRONTED-MEEK через Akamai/Fastly к серверам Psiphon | net4people #626 (04.06.2026), Aether PR #113. В Иране нестабильно, зависит от региона | Данных нет. Akamai под ударом по зарубежному хостингу (16 КБ) | Нет (клиент сети Psiphon, на сервер ставить нечего) | ✏️ |
| ECH-fronting через Cloudflare | Общий ECHConfig CF, скрытый SNI (Xray echConfigList) | #529 (23.09.2025) про Китай. #5999 (22.04.2026) советует ключ `cloudflarestaging.com+udp://1.1.1.1` | Нет: с 05.11.2024 режется связка внешнего SNI `cloudflare-ech.com` и ECH. Плюс 16 КБ и перехват DNS 1.1.1.1/8.8.8.8 | Нет | ✅ |
| Lantern Samizdat (lantern-box) | TLS 1.3 с отпечатком Chrome + HTTP/2 CONNECT, Reality-подобная PSK-аутентификация | Первый коммит 17.02.2026, последний 19.08.2026 (7★). Lantern v10.0.0 (18.09.2026), учётные данные меняются раз в час | Независимых замеров нет, устойчивость к ТСПУ заявлена только в README | Нет, наблюдать (по сути NaiveProxy, который уже покрыт) | ✅ |
| tg-ws-proxy (Flowseal) | Telegram Desktop через WSS `kws*.web.telegram.org`, CF Worker как запасной путь | ~10.3k★. v1.9.0 (28.07.2026), v1.10.3/1.10.4 (19.09.2026). Аккаунт на GitHub был заблокирован 10–17.07.2026 | Частично и хрупко: держится на одном IP Telegram из белого списка для DC2/DC4 (#180) | Нет (клиент для ПК). Ссылка в доках | ✏️ |
| Stealth-режимы коммерческих VPN | Mullvad QUIC/MASQUE и LWO, Proton Stealth, Windscribe | Mullvad QUIC (09.09.2025, мобильные с 2025.8), LWO (12.11.2025) | Нет / нестабильно: большинство серверов Mullvad заблокированы. Proton (06–09.2026) и Windscribe не работали в СПб в 09.2026 даже в stealth | Нет. Вывод: свой непубличный IP + AWG/Reality надёжнее | ✏️ |

#### Кратко по значимым

**Cloudflare WARP как outbound (единственное «да»).** Против цензуры бесполезен, но даёт чистый egress-IP для гео-ограниченных сервисов. В Xray есть родной WireGuard outbound. Регистрация через wgcf с IP ДЦ может получить 429 (09.2026): нужен v2.3.0+ и запасной путь (готовый профиль, повтор, ручной ввод ключа). Клиентский WARP пользователям из РФ не предлагать.
Ссылки: https://github.com/ViRb3/wgcf/releases · https://github.com/Cmd81/up-marznode-xray/pull/1 · https://ntc.party/t/%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D1%82%D0%B0%D0%BB-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D1%82%D1%8C-warp-amneziawg/16807 · https://github.com/CluvexStudio/Aether/issues/138

**ICMP-туннели (pingtunnel).** Релизы 2.9/2.10 (09.2026) после трёх лет тишины. Иранский кейс: при бане IP ICMP оставался единственным прямым транспортом, который ходил. Скорость не замерялась: цифра 9.5 Мбит/с относится к WSS MUX. Клиенты есть под Win/Linux/Android, под iOS нет, антивирусы помечают как HackTool. В РФ проверять самим.
Ссылки: https://github.com/esrrhs/pingtunnel/releases · https://github.com/GreatTeejay/Pingify · https://habr.com/ru/articles/1036100/ · https://ntc.party/t/pingtunnel-tcpudp-over-icmp/7052

**GRE/IPIP для каскада.** Дешёвый L3-линк RU-релей→зарубеж, это несколько строк `ip link`. Шифрования и аутентификации нет (CERT VU#199397), поэтому нужен AWG/WG внутри и firewall по IP пира. Протокол 47 многие NAT/ACL режут, для РФ подтверждений нет.
Ссылки: https://github.com/Azumi67/6TO4-GRE-IPIP-SIT · https://kb.cert.org/vuls/id/199397

**Psiphon Conduit.** Один контейнер `ghcr.io/psiphon-inc/conduit/cli` с лимитами клиентов и полосы (1–40 Мбит/с на пира). Своим пользователям ничего не даёт, клиентов раздаёт брокер Psiphon. Российские клиенты Psiphon будут подключаться к IP VPS напрямую, а значит, могут засветить IP, на котором работают VLESS/Hy2. conduit-manager v1.3.4 — сторонний скрипт (MIT).
Ссылки: https://github.com/Psiphon-Inc/conduit/releases · https://blog.psiphon.ca/2026/06/16/to-russians-with-love/ · https://github.com/SamNet-dev/conduit-manager

**Контекст банов и 16 КБ.** Выборочные баны IP у популярных хостеров (Veesp 5k/30k, Play2Go 2k/37k, FirstByte 3k/29k, Fornex 596/24k, Amnezia 131/20k; 30.09.2026). Заморозка TCP к зарубежным ДЦ и CF на ~16 КБ. Это и обесценивает большинство TLS-обёрток.
Ссылки: https://github.com/net4people/bbs/issues/671 · https://github.com/net4people/bbs/issues/490 · https://blog.cloudflare.com/russian-internet-users-are-unable-to-access-the-open-internet/

#### Кандидаты в инсталлер

1. **WARP-outbound** (приоритет: средне-высокий). Опция: wgcf-профиль → WireGuard outbound в Xray/sing-box, маршрутизация по списку доменов. Обработка 429, wgcf ≥ v2.3.0.
2. **Dual-stack listen** (дёшево). Inbound'ы на `[::]`; если у VPS есть v6, отдавать его запасным endpoint'ом в ссылке. Ротацию /64 не делать.
3. **Аварийный ICMP-канал** (флаг, выкл.). pingtunnel server + `net.ipv4.icmp_echo_ignore_all=1`; внутри WG/KCP. Предупредить, что под iOS клиента нет и что скорость низкая.
4. **Транспорт каскада GRE/IPIP** (флаг, низкий приоритет). Только между нодами, AWG/WG внутри, iptables-правило «протокол 47/4 только от IP пира». Заодно можно взять идею UX иранских менеджеров: замер и выбор транспорта между нодами.
5. **SS-2022 как внутреннее звено каскада** (может быть). Через sing-box/xray RU-релей→зарубеж, клиентам не выдавать.
6. **Psiphon Conduit** (флаг «помочь другим», выкл.). Docker с лимитами; явно предупредить о риске засветить IP основного VPS.
7. **Только документация:** tg-ws-proxy (ПК-клиент для Telegram), BPB/edgetunnel и VLESS-WS за CF (для пользователей вне РФ), OpenVPN+Cloak и SSTP как legacy.

#### Мифы / не работает

- «Pingify показал 9.5 Мбит/с по ICMP»: неверно, это WSS MUX через Cloudflare.
- «Баны 08–09.2026 шли по ASN хостера»: неверно, банили часть IP у популярных хостеров.
- «ntc.party заблокировали по IPv4, а по IPv6 он работал»: неверно, 28.08.2025 IPv4 из DNS убрал сам админ.
- «meek мёртв»: неверно, в Tor Browser 15.0a4 вернули встроенный meek на другом CDN.
- «Windscribe WStunnel = erebe/wstunnel»: неверно, у Windscribe свой протокол.
- «Amnezia держит OVPN+Cloak про запас»: неверно, сворачивает из-за детекта DPI.
- «SoftEther едва жив»: неверно, коммиты идут до 01.10.2026. Но SSTP в РФ всё равно не работает.
- «Snowflake блокировали у клиентов»: неверно, блокировали отпечаток DTLS у standalone-прокси, исправлено в v2.13.1.
- «SS режут по энтропии»: по #363 срабатывает отпечаток TLS внутри туннеля (≥3 пакета от клиента ≥411 байт).
- Не работают в РФ: голые L2TP/PPTP/IKEv2/SSTP/OpenVPN/SOCKS5/SS, obfs4 без zapret, ECH к Cloudflare, VLESS-WS за CF, коммерческие stealth-режимы, SPA как обход, fake-TCP (udp2raw ловился DPI ещё в 2024).

---

## 6. Клиенты

### 6.1 Матрица (сервер: 3x-ui v3.9.0 / Xray v26.9.30; Hy2 self-signed + pin; AWG — профиль 2.0 из §4.4)

| Клиент (версия) | Платформы | VLESS-REALITY | XHTTP | Hy2 (pin) | AWG 2.0 / 3.1 | Примечания |
|---|---|---|---|---|---|---|
| **v2rayN 7.25.4** (Xray v26.9.30) | Win/Linux/macOS | ✅ chrome | ✅ | ✅ pcs; ❌ mport | ❌ | ≥7.24.9 закрывает MITM в обновлении. Если для Hy2 выбрать ядро sing-box, пин теряется 🔬 |
| **v2rayNG 2.3.10** pre (v26.9.30) / 2.2.6 stable (v26.6.27) | Android (APK с GitHub) | ✅ | ✅ | ✅ нужен `pinSHA256`; gecko ❌; mport ❌ | ❌ | авторизацию SOCKS (≥2.1.0) включать вручную |
| **Happ** (iOS 5.x / Android / desktop, Xray ≥v26.6.27) | все | ✅ | ✅ | ✅❓ hex | ❓ | убран из RU App Store; есть Socks-Auth через подписку |
| **INCY** 2.6.x (Xray 26.9.9) | iOS (RU store) / Android / Desktop pre-alpha | ✅ | ✅❓ | ✅❓ | ❌ (заявлен только WG) | open-core |
| **Streisand** 1.6.76 (core 26.09.09) | iOS (только иностранный Apple ID) | ✅❓ | ❓ | ✅❓ | ❓ | |
| **mihomo ≥1.19.31** (Clash Verge Rev 2.5.7, FlClash 0.8.99) | Win/macOS/Linux/Android | ✅ только chrome + `support-x25519mlkem768=true` | ✅ (≥1.19.22) | ✅ `fingerprint`; мульти-порт из authority | ✅ 2.0 (≥1.19.14) / 3.x (≥1.19.30) | импорт через подписку или YAML, отдельные ссылки не принимает; Chrome parrot нет |
| **sing-box 1.14.2** / SFA / «sing-box MT» (iOS, id6785326793) | все | ❌ | ❌ (PR #4326 закрыт) | ✅ insecure; пин только SPKI в JSON | ❌ | в RU App Store ❓ |
| **Hiddify 4.1.1** (форк sing-box 1.13) | все | ❌ | ❓ (🔬 xray-core в go.mod закомментирован) | ✅ insecure, пин игнорируется | ❓ | |
| **NekoBox for Android 1.4.2** | Android | ❌ | ❌ | insecure, пин игнорируется | ❌ | не рекомендовать (поддержка минимальна) |
| **Karing** 1.2.26.2906 | все; iOS через App Store «karing vpn» | ❌ (sing-box) | ✅❓ | ❓ | ❓ (атрибут amnezia у WG) | все сборки на GitHub — pre-release |
| **Throne** 1.3.2 / ThroneForAndroid 2.0.1 | desktop / Android | ✅❓ на ядре Xray | ✅ | ✅ | ✅❓ | замена NekoRay; macOS-сборка без подписи |
| **Shadowrocket 2.2.92** | iOS | ❌ ([config#4](https://github.com/Shadowrocket/config/issues/4)) | ❓ | ❓ | ❓ | купить в РФ нельзя |
| **hysteria CLI 2.12.3** | все | — | — | ✅ нужен `insecure=1` + pin | — | лучшая маскировка Hy2 |
| **AmneziaVPN 5.0.3.0** | все (Android 9+, macOS 13+) | — | — | — | ✅ 2.0 (≥4.8.12.9) / 3.1 (≥5.0.1.5) | **импорт `.conf` вернули только в 5.0.1.5** ✏️; скрыт в RU App Store |
| **DefaultVPN ≥2.0.1.1** | iOS (RU store) | — | — | — | ✅ через `vpn://` | |
| **AmneziaWG** Android v3.1.20260814 / Win 3.1.0 / Apple v3.1.x | все | — | — | — | ✅ 2.0 (≥2.0.0) / 3.1 | лёгкий клиент в Google Play не обновлялся с 12.06 ❓ |
| **WG Tunnel 5.7.5** | Android (Play, F-Droid, Izzy) | — | — | — | ✅ 2.0 (≥4.2.0) / 3.1 (≥5.6.0) | |

Источники матрицы: [v2rayN](https://github.com/2dust/v2rayN/releases/tag/7.25.4), [v2rayNG](https://github.com/2dust/v2rayNG/releases), [mihomo](https://github.com/MetaCubeX/mihomo/releases), [sing-box changelog](https://github.com/SagerNet/sing-box/blob/testing/docs/changelog.md), [ray2sing](https://github.com/hiddify/ray2sing/blob/main/ray2sing/hysteria2.go), [AmneziaVPN](https://github.com/amnezia-vpn/amnezia-client/releases), [WG Tunnel](https://github.com/wgtunnel/android/releases), App Store, [3x-ui#6568](https://github.com/MHSanaei/3x-ui/issues/6568).

**Распространение** ✏️: в RU App Store остаются INCY, Karing, ❓ Hiddify и DefaultVPN. Через иностранный Apple ID доступны Streisand, Happ, v2RayTun, V2Box, AmneziaVPN. Google Play в РФ удаляет VPN гораздо реже (из 212 приложений, названных РКН, удалено 6; Google оштрафован на 22,8 млн ₽). Запасной канал — APK на GitHub.

**Безопасность клиента** ✏️: включить логин и пароль локального SOCKS (v2rayNG: Settings; Happ: Inbounds → Authorization mode) или использовать TUN без локального прокси. Не держать российские банки и маркетплейсы под TUN на том же устройстве. Серверные меры по совету runetfreedom: разные входной и выходной IP (или выход через WARP) и `geoip:ru → block` на сервере.

### 6.2 Форматы ссылок (канонические)

```text
# VLESS RAW+REALITY+Vision (стандарт #716 + ML-KEM-подсказка для mihomo, как в 3x-ui v3.9.0)
vless://UUID@IP:443?encryption=none&type=tcp&headerType=none&security=reality&pbk=PUB&fp=chrome&sni=SNI&sid=SID&spx=%2F<rnd>&flow=xtls-rprx-vision&support-x25519mlkem768=true#zoo-vless

# VLESS XHTTP+REALITY (без flow, без xmux/extra)
vless://UUID2@IP:443?encryption=none&type=xhttp&path=%2F<rnd>&mode=auto&security=reality&sni=SNI&fp=chrome&pbk=PUB&sid=SID&spx=%2F&support-x25519mlkem768=true#zoo-xhttp

# Hysteria2 (универсальная)
hysteria2://PASS@IP:443/?sni=bing.com&insecure=1&pinSHA256=<hex64>#zoo-hy2
#  + obfs-инстанс:  ...@IP:<OBFS_PORT>/?sni=..&insecure=1&pinSHA256=..&obfs=salamander&obfs-password=<pw>
#  + hopping:       ...@IP:443,20000-29999/?...   (официальный клиент/sing-box/mihomo; mport в v2rayN/NG сейчас сломан)

# AmneziaWG: .conf + QR текста .conf (AmneziaWG/WG Tunnel) и vpn:// (AmneziaVPN ≥5.0.1.5, DefaultVPN ≥2.0.1.1)
vpn:// + base64url_nopad( uint32_be(len(json)) || zlib(json) )   # = qCompress
```

`vpn://` ✅ ([реализация bivlked](https://github.com/bivlked/amneziawg-installer/blob/main/awg_common_en.sh)). Внешний JSON: `{containers:[{awg:{isThirdPartyConfig:true,last_config:"<inner JSON строкой>",port:"<str>",protocol_version:"2",transport_proto:"udp"},container:"amnezia-awg"}],defaultContainer:"amnezia-awg",description,dns1,dns2,hostName}`. Внутренний: `H1-H4,Jc,Jmin,Jmax,S1-S4,I1-I5,allowed_ips[],client_ip,client_priv_key,psk_key,config,hostName,mtu,persistent_keep_alive,port(число),server_pub_key`. **Без `psk_key` теряется PSK, и рукопожатие не проходит.** Клиент берёт mtu и keepalive из структурных полей.

Ключи стандарта VLESS (#716) ✅: `encryption` (none или mlkem…), `pqv`, `pcs`/`vcn`, `fm` (finalmask), `ech`, `extra` (XHTTP JSON, через encodeURIComponent), `mode`. ✏️ Явный `encryption=none` не обязателен (по стандарту его отсутствие = none), но безвреден. Утверждение «iOS без него ломается» не подтверждено.

---

## 7. Серверные практики

**ОС** ✏️: Ubuntu **26.04 LTS** (GA 2026-04-23; ядро 7.0, OpenSSH 10.2p1, systemd 259, chrony, sudo-rs, **rust-coreutils**, APT 3.1, Python 3.14). 26.04.1 вышла 2026-08-27. Апгрейд с 24.04 открыли ~2026-09-29 из-за регрессий rust-coreutils (откат: `apt install coreutils-from-gnu`). **24.04.5 (2026-09-11) — HWE 7.0.** 22.04 HWE — 6.8. Debian 13.7 (2026-09-12, ядро 6.12). Debian 12 в LTS с 2026-07-12. На Debian AWG ставится через PPA: focal для Debian 12, noble для Debian 13, через `signed-by` (apt-key устарел) ✅. **Сейчас для 26.04 и 24.04+HWE AWG kmod не собирается** (см. §4.4).

**sysctl** ✅
- `net.ipv4.udp_mem` **удалить**: значение задаётся в страницах, 8388608 страниц ≈ 32 ГиБ.
- `rmem_max/wmem_max` и `tcp_*mem` max: 16777216 вместо 64 МБ (потолок, вреда нет, но избыточно).
- `nf_conntrack`: `/etc/modules-load.d/vpn-conntrack.conf` + `modprobe` до `sysctl -p`; 262144 достаточно; ошибки не глушить.
- ✏️ `net.ipv6.conf.all.forwarding=1` убрать. AWG работает только по IPv4. На ifupdown/Debian без networkd это отключает приём RA, а под netplan/networkd RA обрабатывает userspace, и вред маловероятен ❓. Если появится IPv6 в туннеле: `accept_ra=2` (ядро) или `accept-ra: true` / `IPv6AcceptRA=yes` (netplan).
- fq+bbr оставить ✏️ (BBRv3 в mainline нет, ❓ по первоисточнику). BBR влияет только на TCP (VLESS).

**Firewall** ✅
- **Не делать `ufw --force reset`**: он стирает чужие правила. Не глушить `|| true` на allow/enable. До enable проверять, что правило для SSH на месте.
- SSH-порт брать из `ss -tlnp` (sshd/systemd) с fallback на `sshd -T` и 22 ✏️ (`sshd -T` не видит переопределения `ListenStream` в ssh.socket). Использовать `ufw limit`.
- Открыть 443/tcp, 443/udp, 51822/udp (+ диапазон hopping, + 80/tcp для ACME при self-steal). **Закрыть 2053 и 8443, 2096 не открывать.**
- Docker обходит UFW (ufw-docker). Упомянуть в README.

**fail2ban** ✏️: на 24.04/26.04 `python3-systemd` — Depends, на jammy — Recommends (и так ставится). Явно добавить. `backend=systemd`, `port=<фактический>`, `banaction=ufw`, `[recidive]`.

**SSH** ✅
- Drop-in `/etc/ssh/sshd_config.d/00-vpn-hardening.conf` (сортируется раньше `50-cloud-init.conf`, а в sshd **побеждает первое значение**, [LP#2088207](https://bugs.launchpad.net/bugs/2088207)): `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`, `GSSAPIAuthentication no`. Только если ключ уже есть в authorized_keys. Затем `sshd -t`.
- На 22.10+ работает socket activation: после смены Port нужны `systemctl daemon-reload && systemctl restart ssh.socket` ([LP#2069041](https://bugs.launchpad.net/ubuntu/+source/openssh/+bug/2069041)).
- ✏️ OpenSSH 10.5 (2026-08-11): restrict теперь применяется и к tunnel forwarding, плюс исправления в ssh-agent. 10.4: pre-auth DoS при GSSAPI. Pre-auth RCE в sshd в 2026 году нет.

**Обновления и CVE** ✏️
- unattended-upgrades включён по умолчанию, но `Automatic-Reboot=false`. Для одиночного VPN: `Automatic-Reboot "true"`, `"04:00"` или Livepatch (Pro бесплатно на 5 машин). Проверять `/var/run/reboot-required` **до** сборки DKMS. `wait_for_apt` поднять до 600 с.
- KEV 2026: Copy Fail CVE-2026-31431 (algif_aead, LPE, PoC на 732 Б, исправлено в 6.18.22/6.19.12/7.0), CVE-2025-39682 (kTLS), CVE-2026-53266 (ebtables LPE), CVE-2025-39964 (AF_ALG). Временная мера: `/etc/modprobe.d` с `install algif_aead /bin/false` (и `algif_skcipher`, `algif_hash`, `af_alg`) плюс ebtables. Стеку это не нужно ❓ (проверить, что ничего не использует AF_ALG).

**Supply chain** ✅
- Закрепить версии и sha256 в `scripts/versions.env`: `XUI_VERSION=v3.9.0` (+ `.sha256`), `HY2_VERSION=app/v2.12.3` (+ `hashes.txt`, прямое скачивание `hysteria-linux-<arch>` из HyNetworks/hysteria).
- `releases/latest` без токена упирается в rate limit (60 запросов в час) и тянет непроверенное. Официальный install.sh 3x-ui определяет тег через редирект.
- Xray: `/releases/latest` = v26.3.27, а 3x-ui использует pre-release ядро. Не тянуть Xray отдельно.

**Хостинг и ASN** ✏️
- Избегать массовых ASN: Hetzner, DO, Vultr, OVH, Contabo, Oracle (72 ограничиваемых AS), а по #671 также FirstByte, Veesp, Play2Go, Fornex. Держать запасной IP.
- В 99 печатать ASN сервера (whois или ipinfo) для диагностики.
- Проверять доступность из РФ отдельно по TCP и по полному рукопожатию (2–3 мин), например через [dpi-checkers «RU :: TCP 16-20»](https://github.com/hyperion-cs/dpi-checkers).
- «Антифрод 2.0»: хостерам запрещено обслуживать нарушителей ст. 15.8. RU-мост — расходник.

**Секреты** ✅: `umask 077` в install.sh; `/etc/vpn-setup` с правами 700; не печатать секреты, если вывод не TTY (логи cloud-init); `set -x` не включать на строках с паролем; токен API хранить в файле 0600 и передавать curl через `-H @file`.

**CI** ✏️: shellcheck v0.11.0, bats-core v1.14.0, e2e на настоящих VM (22.04, 24.04, 26.04 с rust-coreutils). Docker не подходит: в нём нет ядра и UFW. `gen_random_alnum` (`tr | head` при pipefail) даёт 141. Сейчас это латентный баг, его маскирует `: "${X:=$(...)}"`.

---

## 8. Что устарело или сломано в vpn-zoo-installer

### 8.1 Таблица

| # | Файл | Проблема | Что сделать | Приор. | Статус |
|---|---|---|---|---|---|
| 1 | `scripts/04-vless-reality.sh` | `INSERT INTO inbounds (… all_time …)`. С 3x-ui v3.1.0 колонки нет, клиенты лежат в `clients`/`client_inbounds` | Перейти на `POST /panel/api/inbounds/add` (тело из §4.2, `jq -n`, `.success`). Сохранить `.obj.id`. Идемпотентность: сначала `GET /inbounds/list` и поиск по port | **P0** | ✅🔬 |
| 2 | `scripts/03-3xui.sh` | `XUI_VERSION=latest`, нет sha256, `rm -rf` без бэкапа, ветка «уже установлен» не обновляет | Пин `v3.9.0` в versions.env, `sha256sum -c`, `cp -a /etc/x-ui` перед переустановкой, сравнение `x-ui -v` с пином, лог `xray version`. Выпуск API-токена в файл 0600, сохранение port и webBasePath в state, ожидание `/panel/api/server/status` | **P0** | ✅ |
| 3 | `03-3xui.sh`, `01-firewall.sh`, `99-print-creds.sh` | Панель по HTTP на публичном :2053 | `x-ui setting … -listenIP 127.0.0.1`. Убрать `ufw allow 2053/tcp`. Печатать `ssh -N -L 2053:127.0.0.1:2053 root@IP` и URL `http://127.0.0.1:2053/<path>/`. Убрать `\| tail -5 \|\| true`, проверять через `x-ui setting -show`. Советовать 2FA | **P0** | ✅ |
| 4 | `04`, `01`, `lib.sh`/config.env | VLESS на 8443 | `VLESS_PORT=${VLESS_PORT:-443}`, проверка `ss -ltnp 'sport = :443'`, `ufw allow 443/tcp`, удалить 8443 | **P0** | ✅ |
| 5 | `04`, `lib.sh:147` (`VLESS_SNI=www.cbr.ru`) | SNI `.ru` на зарубежном IP: warning ядра, несоответствие ASN, нет ML-KEM, цепочка 3479 | `validate_reality_target()`: `xray tls ping` (TLS1.3; группа X25519MLKEM768 или X25519, иначе FAIL; цепочка ≤16000), `openssl s_client -alpn h2 -tls1_3`, `curl -sI` без редиректа на чужой хост, запрет `.ru/.ir/.cn/apple/icloud/microsoft`, сравнение ASN. Без `--sni` перебор кандидатов. С `--domain` — self-steal (см. #22) | **P0** | ✅✏️🔬 |
| 6 | `99-print-creds.sh` (VLESS) | Пользователи sing-box и Shadowrocket получают «подключено, но не работает» | Добавить `&support-x25519mlkem768=true` и `encryption=none&headerType=none`. Раздел «какой клиент для какого протокола» (§6.1). Запрет `fp=randomized` | **P0** | ✅🔬 |
| 7 | `99-print-creds.sh:23` + `05` | Hy2-ссылка без пина: не работает в v2rayN/NG/Happ | В 05 считать `HY2_PIN` (DER→sha256, hex lowercase) и сохранять в config.env. В 99: `…/?sni=…&insecure=1&pinSHA256=$HY2_PIN#…`, пин записать и в CREDENTIALS.md. Для IPv6 адрес в `[]` | **P0** | ✅🔬 |
| 8 | `05-hysteria2.sh:17-25` | Бинарь не обновляется, лог установщика уходит в `/dev/null`, `version \| head -1` возвращает мусор | `HY2_VERSION=${HY2_VERSION:-v2.12.3}`, сравнение версии через `grep '^Version'`, при расхождении `get.hy2.sh --version` (или прямое скачивание + hashes.txt). Лог в `/var/log/vpn-zoo/` | **P0** | ✅ |
| 9 | `06-amneziawg.sh:126-144`, `99:40-52`, `lib.sh` | Статичные Jc/S/H/I1/I2 в двух местах; S4=0; Jmax=1000; MTU 1420 | `gen_awg_params()` по §4.4 с проверками (Jmin<Jmax<1280, S1+56≠S2, S3≠S2+28, H без пересечений ≤INT32_MAX). Хранить в config.env (`AWG_*`), 06 и 99 читают из одного места. MTU 1280 + TCPMSS clamp | **P0** | ✅🔬 |
| 10 | `06`, `99` | I1 `<b 0x16030101><r 32>`, I2 `<t><rc 16>`, комментарий «I2 = ответ» | DNS-I1 ≤128 Б со случайными доменом, TTL и IP. I2 убрать. Опция `AWG_I1_MODE=dns\|none`. Исправить комментарий | **P0** | ✏️❓ |
| 11 | `00-bootstrap.sh:80` | `net.ipv4.udp_mem` в страницах = 32–64 ГиБ | Удалить строку | **P0** | ✅ |
| 12 | `06:22-26`, `02-kernel-hwe.sh` | `die` при ядре <6.2; ложное обоснование; нет защиты от 7.0.0-38 | Убрать die. После apt проверять `dkms status amneziawg` **для всех ядер** в /lib/modules. При ошибке прерывать **до** reboot и показывать make.log. 02: на jammy HWE, если ядро <6.7; на noble блок при `linux-generic-hwe-24.04` или 7.0.0-(≥38), опционально патч PR #218 с sha256. Добавить 26.04 в case (пока с предупреждением) | **P0** | ✅🔬 |
| 13 | `06` | HPK/RT не заданы (это хорошо), но нет защиты на будущее | HPK и RT по умолчанию OFF. Флаги `AWG_HPK=1` (S≥12, предупреждение о клиентах 3.1) и `AWG_RT=1` (общее S, одиночные H) | **P0** | 🔬 |
| 14 | `00-bootstrap.sh`, `06:68` | `net.ipv6.conf.all.forwarding=1` | Удалить в обоих местах | P1 | ✏️ |
| 15 | `04:12` | `XRAY_BIN=…xray-linux-amd64` | `xui_arch()` в lib.sh (uname -m), общая для 03 и 04 | P1 | ✅ |
| 16 | `04b-vless-xhttp-reality.sh` (новый) | Нет TCP-резерва против лимитов ТСПУ | Child VLESS: listen 127.0.0.1:<rand>, network xhttp, security none, `path=/<rand16+>`, mode auto, flow `""`, decryption none + `POST /panel/api/inbounds/<masterId>/fallbacks`. Либо отдельный IP:443 по Xray-examples. Ссылка по §6.2 | P1 | ✅🔬❓ |
| 17 | `01-firewall.sh` | Захардкожен 22/tcp, `--force reset`, `\|\| true` | SSH-порт из `ss`/`sshd -T`, `ufw limit`, без reset, ошибки не глушить, проверка правила SSH до enable | P1 | ✅ |
| 18 | `01b-ssh-harden.sh` (новый) | Хардениг SSH отсутствует | Drop-in `00-vpn-hardening.conf` при наличии ключа, `sshd -t`, daemon-reload, restart ssh/ssh.socket | P1 | ✅ |
| 19 | `00-bootstrap.sh` | Новые ядра не применяются без перезагрузки; нет проверки reboot-required | unattended-upgrades + `Automatic-Reboot`. Если `/var/run/reboot-required` — state=rebooting до фазы 06 | P1 | ✅ |
| 20 | `05-hysteria2.sh` | Нет obfs и hopping | Второй инстанс `hysteria-server@obfs` (Salamander, отдельный порт, без masquerade). `HY2_HOP_RANGE` через nft redirect на :443 (drop-in), без пересечения с 51822. Вторая и третья ссылки | P1 | ✅ |
| 21 | `05-hysteria2.sh` | self-signed «bing.com» + masquerade bing | Опция `HY2_DOMAIN` → блок `acme:` вместо `tls:`, ссылка без insecure и пина | P1 | ✏️ |
| 22 | `04a-selfsteal-web.sh` (новый) | Нет self-steal | С `--domain`: Caddy ≥2.10 на 127.0.0.1, ACME (HTTP-01 → 80/tcp), target=127.0.0.1:PORT, serverNames=[domain] | P1 | ✅❓ |
| 23 | `06` | `systemctl enable --now` не применяет новый конфиг | `enable` + `restart awg-quick@awg0` | P1 | ✅ |
| 24 | `lib.sh` | Нет обёртки API | `xui_api METHOD PATH [JSON]`: Bearer из файла, разбор `.success/.msg` через jq. Использовать в 04, 04b и будущих add-client и rotate | P1 | 🔬 |
| 25 | `99-print-creds.sh` | AWG: только подсказка для AmneziaVPN | `vpn://` (python3/perl, обязательно `psk_key`) + PNG-QR; QR текста `.conf`; файлы в `/root/vpn-links/` (0600), `sub.txt`/`sub.b64`; минимальные версии клиентов; блок «безопасность клиента» | P1 | ✅ |
| 26 | `04` | `dest`, `minClient/maxClient` (Xray их не знает), `tgId:""`, одинаковый `spiderX`, `fakedns` | `target`, `minClientVer:""`, `tgId:0`, `spiderX=/<rand>` на клиента, без fakedns, shortIds списком | P2 | ✅✏️ |
| 27 | `04:24-26` | Хрупкий разбор `xray x25519` | `xray x25519 -i "$PRIV"` или `^(Password\|Public key)` + проверка длины 43 | P2 | ✅ |
| 28 | `05` | `https://www.${HY2_SNI}/` → баг `www.www`; не указаны congestion и sniGuard; cert может быть нечитаем пользователю hysteria | `HY2_MASQ_URL`; `congestion:{type: bbr, bbrProfile: standard}`, `sniGuard: dns-san`, `speedTest: false`; chown/chmod cert для пользователя hysteria; срок сертификата 1–10 лет | P2 | ✅✏️ |
| 29 | `05` | Перезаписываются чужие конфиги | Guard: проверять **до** запуска установщика или распознавать шаблон (`your.domain.net`), иначе сломается свежая установка. Бэкап `.bak.<ts>` | P2 | ✏️ |
| 30 | `06` | Ставится только `linux-headers-$(uname -r)` | Метапакет заголовков под flavour (generic/virtual/hwe), `dkms status` после установки | P2 | ✅ |
| 31 | `06` (шаг 5) | Проверка «v2 CPS» по i1/i2; `awg show` печатает HPK открытым текстом | Проверять `/sys/module/amneziawg/version` (major ≥3) + git-хеш из версии dpkg. HPK в логе маскировать | P2 | ✅ |
| 32 | `00-bootstrap.sh` | conntrack не применяется при загрузке; буферы 64 МБ | modules-load + 262144; 16 МБ | P2 | ✅ |
| 33 | `01-firewall.sh` | fail2ban: `port=ssh`, нет banaction | `python3-systemd`, фактический порт, `banaction=ufw`, recidive | P2 | ✏️ |
| 34 | `lib.sh` | `wait_for_apt` 60 с; Debian пропускается, хотя PPA для него не настроен | 600 с; явный список ОС; Debian — die или ветка с deb822 | P2 | ✅ |
| 35 | `README.md` | Нет модели угроз РФ 2026 | Разделы: выбор хостинга и ASN, 16 КБ, баны IP, белые списки и каскад, роль каждого протокола (Hy2/AWG — запасной UDP), совместимость клиентов с версией Xray, панель только через SSH, таблица проверенных версий, «почему не TUIC/ShadowTLS/…» | P2 | ✅ |
| 36 | `.github/workflows/ci.yml` (новый) | Тесты только в Docker | shellcheck + bats (lib.sh) + e2e на VM 22.04/24.04/26.04 | P2 | ✏️ |
| 37 | `rotate.sh`, `migrate.sh`, `add-client.sh` (новые) | TODO: rotate-keys, add-client, переезд на новый IP | Через API 3x-ui (`/clients/add`, `/clients/update/:email`, `/inbounds/update/:id` для ключей REALITY), экспорт и импорт config.env, перевыпуск всех ссылок | P2 | ✅ |
| 38 | `migrate-reality-443.sh` (новый) | У существующих установок поменяются ссылки | Бэкап x-ui.db, новый inbound 443 через API, переходный период, затем удаление 8443 | P2 | 🔬 |
| 39 | `07-cdn-fallback.sh` (опция) | Нет резерва, не зависящего от IP | XHTTP+TLS+VLESS Enc за CDN, работающим в РФ | P2 | ✅❓ |
| 40 | `07-bridge.sh` / `ROLE=bridge` (опция) | Белые списки на мобильных сетях | Xray на RU-VPS (вход Reality/XHTTP с белым SNI) → outbound на зарубежный узел; проверка «белого» IP по [hxehex](https://github.com/hxehex/russia-mobile-internet-whitelist) | P2 | ❓ |
| 41 | `install.sh` | Нет `umask 077`, мёртвая логика `final_state` | Исправить | P3 | ✅ |
| 42 | `06` | Лишний `resolvconf` (на jammy это настоящий пакет) | Убрать из apt_install | P3 | ✅ |
| 43 | `04` | — | Опция `ENABLE_PQV` (`xray mldsa65`, `pqv=`), только при цепочке ≥3500. С LE self-steal, скорее всего, не выйдет ❓ | P3 | ✅ |
| 44 | `05` | — | Для add-client: `auth.type: userpass` + `trafficStats` на 127.0.0.1 с secret | P3 | ✅ |
| 45 | `00-bootstrap.sh` | — | modprobe-blacklist `af_alg`/`algif_*`/ebtables | P3 | ✏️❓ |
| 46 | `06` | — | Fallback на amneziawg-go при LXC или ошибке DKMS; deb822 для Debian 12/13 | P3 | ✅ |

### 8.2 План обновления (по порядку)

**Этап 0 — «чтобы ставилось вообще» (P0, один PR)**
1. `scripts/versions.env`: `XUI_VERSION=v3.9.0`, `HY2_VERSION=v2.12.3` и sha256. В `lib.sh`: `xui_arch()`, `xui_api()`, `umask 077`.
2. 03: пин + sha256 + бэкап + `-listenIP 127.0.0.1` + выпуск токена + ожидание API. 01: убрать 2053.
3. 04: переписать на API (`/inbounds/add`). Порт 443. `target` из валидатора (без cbr.ru). `minClientVer`, `tgId:0`, бинарь по arch.
4. 99: ссылки по §6.2 (ML-KEM-подсказка, пин Hy2), блок клиентов, SSH-туннель к панели.

**Этап 1 — профили против блокировок (P0/P1)**
5. 06: `gen_awg_params` + DNS-I1 + MTU 1280 + MSS clamp + restart. HPK и RT за флагами. Параметры в config.env.
6. 05: пин версии, `HY2_PIN`, явный BBR, исправить masquerade, права на cert.
7. 02/06: защита DKMS (проверка всех ядер, блок 7.0.0-38, jammy <6.7 → HWE), убрать die на 6.2.

**Этап 2 — гигиена хоста (P1/P2)**
8. 00: убрать udp_mem и ipv6 forwarding, conntrack modules-load, буферы 16 МБ, unattended-upgrades + reboot-required.
9. 01: SSH-порт, ufw limit, без reset, fail2ban. 01b: хардениг SSH.

**Этап 3 — новые транспорты (P1)**
10. 04b XHTTP (fallback-child, при этом проверить схему API fallbacks и то, как ссылка child переписывается под REALITY master).
11. 05: Salamander-инстанс + nft hopping. 04a self-steal / 05 ACME при `--domain`.

**Этап 4 — эксплуатация (P2)**
12. add-client, rotate, migrate (через API), migrate-reality-443 для старых установок.
13. CI (shellcheck, bats, VM e2e на 22.04/24.04/26.04). README с моделью угроз.

**Этап 5 — исследование и опции (P2/P3)**
14. Решить: AWG kmod или встроенный userspace в 3x-ui (замерить скорость, изучить схему API AWG-инбаунда).
15. Опции: RU-мост, CDN-резерв, PQV, Debian 12/13.

---

## 9. Открытые вопросы (проверить руками на реальном VPS)

1. **3x-ui v3.9.0 API**:
   - генерирует ли сервер `subId`, если его не передали;
   - схема тела `POST /inbounds/:id/fallbacks`;
   - переписывает ли панель ссылку child-XHTTP под REALITY-параметры master (pbk/sni/sid);
   - что возвращает `/setting/updateUser` на Bearer-запросе.
2. **Fallback Vision→XHTTP на 443**: работает ли связка end-to-end в v2rayNG и Happ, сколько соединений реально открывает клиент (xmux=3).
3. **HPK kmod-сервер ↔ go-клиенты** (AmneziaVPN 5.0.3.0, AmneziaWG Android и Win 3.1): проходит ли рукопожатие. Пока HPK выключен, это не блокер.
4. **DKMS на jammy 5.15.0-xxx** с kmod 4569c4c: собирается ли сегодня и попал ли бэкпорт timer_delete. На 24.04: дошёл ли 7.0.0-39 до -updates и починили ли #259.
5. **UFW + ручной nft redirect** диапазона на 443/udp на 22.04 и 24.04: не конфликтуют ли iptables-nft и nft-таблица.
6. **REALITY-target с конкретного VPS и из РФ**: `xray tls ping` по списку кандидатов, ASN target и VPS, RTT.
7. **Hy2 на мобильных РФ** (T2, Билайн, МегаФон, МТС): plain с Chrome parrot против Salamander против Gecko.
8. **DNS-I1** на разных операторах (замер был только на МТС Москва→США). Не стал ли шаблон с icloud массовой сигнатурой.
9. **Happ, INCY, Streisand, Karing**: фактическая поддержка `pinSHA256` (hex), `support-x25519mlkem768`, AWG 2.0 и XHTTP. Версия Xray в Happ iOS 5.9.0.
10. Какой первый релиз mihomo разбирает `support-x25519mlkem768` из URI (по дате, вероятно, v1.19.31).
11. Скорость userspace AWG в 3x-ui (go + gVisor) против kmod на 1 vCPU.
12. Какая из HWE-сборок на облачных образах ставится по умолчанию (`dpkg -S /boot/vmlinuz-$(uname -r)`), то есть затронет ли конкретного провайдера проблема 7.0.0-38.
13. Не используют ли x-ui или Xray AF_ALG, прежде чем блокировать `af_alg` через modprobe.
14. Поведение скриптов под rust-coreutils на 26.04 (`tr`, `head -c`, `cut`, `stat`).
15. Нерешённые на уровне сообщества: точные пороги ТСПУ (>3 рукопожатий, 4 соединения, 120/600 с); каким каналом собраны IP AWG-only серверов; работает ли собственный ECH в РФ; вступила ли в силу хостинговая норма «Антифрод 2.0».

---

## 10. Источники

**Xray-core / REALITY**
[Releases](https://github.com/XTLS/Xray-core/releases) · [v26.2.6](https://github.com/XTLS/Xray-core/releases/tag/v26.2.6) · [v26.3.27](https://github.com/XTLS/Xray-core/releases/tag/v26.3.27) · [compare v26.3.27…v26.9.30](https://github.com/XTLS/Xray-core/compare/v26.3.27...v26.9.30) · [REALITY 8cdf7bf (ML-KEM)](https://github.com/XTLS/REALITY/commit/8cdf7bf9c7f09cb9814bf08c3eb877f68b85fba8) · [REALITY 393f8de (17 KiB)](https://github.com/XTLS/REALITY/commit/393f8de3ee2d685271d79ee608334e441b2db324) · [REALITY README](https://github.com/XTLS/REALITY#readme) · [157e65b (non-443)](https://github.com/XTLS/Xray-core/commit/157e65b34d32363528088c592d4e415d84f01a63) · [PR #6508 (.ru target)](https://github.com/XTLS/Xray-core/pull/6508) · [af7eb68 / 47cfe99 (minClientVer)](https://github.com/XTLS/Xray-core/commit/47cfe9994a6b39b1f673ba35e62b091bcce15a71) · [18e2839 (xmux 3)](https://github.com/XTLS/Xray-core/commit/18e283909ca253d4220136eccca0a27006ea709f) · [#6376](https://github.com/XTLS/Xray-core/issues/6376) · [#6181 (RPRX, май 2026)](https://github.com/XTLS/Xray-core/pull/6181#issuecomment-4567373533) · [#6714](https://github.com/XTLS/Xray-core/issues/6714) · [#6861](https://github.com/XTLS/Xray-core/issues/6861) · [#6717](https://github.com/XTLS/Xray-core/issues/6717) · [#4113 XHTTP](https://github.com/XTLS/Xray-core/discussions/4113) · [#4118 fallback→XHTTP](https://github.com/XTLS/Xray-core/discussions/4118) · [#716 share links](https://github.com/XTLS/Xray-core/discussions/716) · [PR #5067 VLESS Enc](https://github.com/XTLS/Xray-core/pull/5067) · [PR #4915 ML-DSA](https://github.com/XTLS/Xray-core/pull/4915) · [PR #5671](https://github.com/XTLS/Xray-core/pull/5671) · [PR #6309](https://github.com/XTLS/Xray-core/pull/6309) · [transport_security.go v26.9.30](https://github.com/XTLS/Xray-core/blob/v26.9.30/infra/conf/transport_security.go) · [splithttp/dialer.go](https://github.com/XTLS/Xray-core/blob/v26.9.30/transport/internet/splithttp/dialer.go) · [tls ping](https://github.com/XTLS/Xray-core/blob/v26.9.30/main/commands/all/tls/ping.go) · [Xray-examples XHTTP-Reality](https://github.com/XTLS/Xray-examples/tree/main/VLESS-XHTTP-Reality/minimal-steal_others) · [TLS docs (pcs)](https://github.com/XTLS/Xray-docs-next/blob/main/docs/en/config/transports/tls.md) · [t.me/projectXtls](https://t.me/s/projectXtls/1752)

**3x-ui и панели**
[v3.9.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0) · [v3.1.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.1.0) · [v3.7.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.7.0) · [v3.8.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.0) · [inbound.go](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound.go) · [inbound_migration.go](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound_migration.go) · [xray.go](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/xray.go) · [model.go](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/database/model/model.go) · [main.go](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/main.go) · [API docs](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/docs/content/docs/en/reference/api/inbounds.mdx) · [install.sh](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/install.sh) · [Security advisories](https://github.com/MHSanaei/3x-ui/security/advisories) · [PR #6712](https://github.com/MHSanaei/3x-ui/pull/6712) · [ntc.party: HTTP-панели](https://ntc.party/t/%D0%B2%D0%BD%D0%B8%D0%BC%D0%B0%D0%BD%D0%B8%D1%8E-%D0%BF%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D0%B5%D0%BB%D0%B5%D0%B9-3x-ui/12243) · [Remnawave](https://github.com/remnawave/backend/releases) · [PasarGuard](https://github.com/PasarGuard/panel/releases) · [s-ui](https://github.com/alireza0/s-ui)

**Hysteria2**
[Releases](https://github.com/apernet/hysteria/releases) (→ HyNetworks/hysteria) · [URI scheme](https://v2.hysteria.network/docs/developers/URI-Scheme/) · [Full server config](https://v2.hysteria.network/docs/advanced/Full-Server-Config/) · [Port hopping](https://v2.hysteria.network/docs/advanced/Port-Hopping/) · [Performance](https://v2.hysteria.network/docs/advanced/Performance/) · [client.go](https://github.com/apernet/hysteria/blob/master/app/cmd/client.go) · [get.hy2.sh](https://get.hy2.sh/) · [Advisories](https://github.com/apernet/hysteria/security/advisories) · [Wang et al., FOCI 2025](https://www.petsymposium.org/foci/2025/foci-2025-0001.php) · [quic-go UDP buffers](https://github.com/quic-go/quic-go/wiki/UDP-Buffer-Sizes)

**AmneziaWG**
[kmod](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module) · [#259](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/259) · [#222](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/222) · [#226](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/226) · [#254](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/issues/254) · [PR #218](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/pull/218) · [amneziawg-go README](https://github.com/amnezia-vpn/amneziawg-go/blob/master/README.md) · [go#186](https://github.com/amnezia-vpn/amneziawg-go/issues/186) · [tools releases](https://github.com/amnezia-vpn/amneziawg-tools/releases) · [PPA](https://launchpad.net/~amnezia/+archive/ubuntu/ppa) · [docs.amnezia.org AWG](https://docs.amnezia.org/documentation/amnezia-wg/) · [AWG 3.1 blog](https://amnezia.org/blog/amneziawg-3-1-is-here) · [Постмортем](https://amnezia.org/blog/amnezia-vpn-hybrid-attack-postmortem) · [awgInstaller.cpp](https://github.com/amnezia-vpn/amnezia-client/blob/dev/client/core/installers/awgInstaller.cpp) · [bivlked ADVANCED](https://github.com/bivlked/amneziawg-installer/blob/main/ADVANCED.en.md) · [zapret notes](https://publish.obsidian.md/zapret/amnezia-3-0/reference)

**Клиенты**
[sing-box#4520](https://github.com/SagerNet/sing-box/issues/4520) · [sing-box changelog](https://github.com/SagerNet/sing-box/blob/testing/docs/changelog.md) · [v2rayN 7.25.4](https://github.com/2dust/v2rayN/releases/tag/7.25.4) · [v2rayN #9460](https://github.com/2dust/v2rayN/discussions/9460) · [v2rayNG](https://github.com/2dust/v2rayNG/releases) · [mihomo](https://github.com/MetaCubeX/mihomo/releases) · [mihomo converter](https://github.com/MetaCubeX/mihomo/blob/Meta/common/convert/converter.go) · [ray2sing](https://github.com/hiddify/ray2sing/blob/main/ray2sing/hysteria2.go) · [Shadowrocket config#4](https://github.com/Shadowrocket/config/issues/4) · [INCY](https://github.com/INCY-DEV/incy-platforms) · [Throne](https://github.com/throneproj/Throne/releases) · [Karing](https://github.com/KaringX/karing/releases) · [WG Tunnel](https://github.com/wgtunnel/android/releases) · [Amnezia на iOS в РФ](https://docs.amnezia.org/documentation/instructions/amnezia-on-ios-in-russia/) · [SOCKS5-уязвимость (Habr)](https://habr.com/ru/articles/1020080/) · [Медиазона: App Store](https://zona.media/news/2026/03/28/app-vpn)

**Россия, ТСПУ, право**
[net4people #490](https://github.com/net4people/bbs/issues/490) · [#546](https://github.com/net4people/bbs/issues/546) · [#650](https://github.com/net4people/bbs/issues/650) · [#663](https://github.com/net4people/bbs/issues/663) · [#671](https://github.com/net4people/bbs/issues/671) · [#417](https://github.com/net4people/bbs/issues/417) · [Habr 1047442](https://habr.com/ru/articles/1047442/) · [Habr 997088](https://habr.com/ru/articles/997088/) · [Habr 1084862](https://habr.com/ru/articles/1084862/) · [Habr 1008554](https://habr.com/ru/articles/1008554/) · [Теплица 30.09.2026](https://te-st.org/2026/09/30/vlessmore/) · [ByeByeVPN](https://ntc.party/t/byebyevpn/24325) · [ntc.party: Hy2 на T2](https://ntc.party/t/hysteria2-%D0%BD%D0%B5-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%B5%D1%82-%D0%BD%D0%B0-%D1%82%D0%B5%D0%BB%D0%B52-%D0%BF%D0%BE%D1%85%D0%BE%D0%B4%D1%83-%D0%BF%D0%BE%D0%BB%D0%BD%D1%8B%D0%B9-%D0%B1%D0%BB%D0%BE%D0%BA-quic/20340) · [ntc.party: VPS при белых списках](https://ntc.party/t/vpsvds-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D1%8E%D1%89%D0%B8%D0%B5-%D0%BF%D1%80%D0%B8-%D0%B1%D0%B5%D0%BB%D1%8B%D1%85-%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%D1%85/24230) · [Википедия: белые списки](https://ru.wikipedia.org/wiki/%C2%AB%D0%91%D0%B5%D0%BB%D1%8B%D0%B5_%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B8%C2%BB_%D1%81%D0%B0%D0%B9%D1%82%D0%BE%D0%B2_%D0%B2_%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D0%B8) · [HRW 24.09.2026](https://www.hrw.org/news/2026/09/24/russia-crackdown-on-bypassing-online-censorship) · [CNews: VPN-детект](https://www.cnews.ru/news/top/2026-04-06_rossijskim_it-kompaniyam) · [RKS Global](https://rks.global/ru/research/vpn-detection/) · [Xakep: Антифрод 2.0](https://xakep.ru/2026/06/10/antifraud/) · [Медиазона: Антифрод 3.0](https://zona.media/news/2026/10/01/vpn) · [Медиазона: цензура 2026](https://en.zona.media/article/2026/04/07/russian_internet_censorship_2026)

**Исследования**
[FOCI 2026: Russia QUIC SNI](https://www.petsymposium.org/foci/2026/foci-2026-0010.pdf) · [FOCI 2026: Iran shutdown](https://www.petsymposium.org/foci/2026/foci-2026-0016.pdf) · [USENIX Sec '26: Geedge leak](https://www.usenix.org/system/files/usenixsecurity26-ablove.pdf) · [USENIX Sec '25: GFW QUIC](https://www.usenix.org/system/files/usenixsecurity25-zohaib.pdf) · [QUICstep (PETS 2026)](https://gfw.report/publications/pets26a/en/) · [Snowflake (arXiv 2609.12242)](https://arxiv.org/abs/2609.12242) · [Aparecium](https://github.com/ban6cat6/aparecium)

**Сервер / ОС / безопасность**
[Ubuntu 26.04 release notes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/) · [24.04.5 с ядром 7.0](https://www.omgubuntu.co.uk/2026/09/ubuntu-24-04-5-lts-released-with-linux-kernel-7-0) · [Debian 13.7](https://www.debian.org/News/2026/20260912) · [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html) · [systemd.network (noble)](https://manpages.ubuntu.com/manpages/noble/man5/systemd.network.5.html) · [OpenSSH release notes](https://www.openssh.org/releasenotes.html) · [LP#2088207](https://bugs.launchpad.net/bugs/2088207) · [LP#2069041](https://bugs.launchpad.net/ubuntu/+source/openssh/+bug/2069041) · [Ubuntu auto-updates](https://ubuntu.com/server/docs/how-to/software/automatic-updates/) · [CISA KEV: Copy Fail](https://thehackernews.com/2026/05/cisa-adds-actively-exploited-linux-root.html) · [KEV-тройка (Qualys)](https://blog.qualys.com/product-tech/2026/09/23/cisa-bod-26-04-timelines-for-three-linux-kernel-cves) · [ufw-docker](https://github.com/chaifeng/ufw-docker) · [shellcheck](https://github.com/koalaman/shellcheck/releases)
