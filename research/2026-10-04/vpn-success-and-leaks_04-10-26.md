# Секреты успеха работающих VPN в РФ и компрометаторы: что заложить в инсталлер

**Дата:** 2026-10-04
**Для кого:** владелец репозитория и ИИ-ассистент, который будет вносить изменения в `vpn-zoo-installer`.
**Контекст:** self-hosted VPS (Ubuntu) для пользователей в РФ: 3x-ui (VLESS+REALITY+Vision, в плане XHTTP+REALITY), Hysteria2 (apernet, standalone), AmneziaWG (kernel, в плане рандомизированный профиль 3.x). Планы: пин 3x-ui v3.9.0, панель на 127.0.0.1, опционально вторая RU-VPS как входная нода.

## 0. Как собрано и как читать

- **Сбор:** 7 углов поиска (архитектура рабочих сервисов, транспорты, хостинг/IP, клиенты и подписки, роутинг, эксплуатация, компрометаторы), затем merge дублей.
- **Проверка:** каждое утверждение прошли 2 адверсария:
  - **evidence:** подтверждают ли источники то, что заявлено;
  - **applicability:** применимо ли это к самоделу на 1–2 VPS, и что из этого следует для сервера.
- **Калибровка.** Форумы, issue-трекеры и посты «X заблокирован» смещены в негатив: о сбоях пишут, о работающем молчат. Факты против этой картины: десятки коммерческих сервисов прямо сейчас продают VPN для всех устройств в РФ, а Hy2 владельца стабильно работает на мобильной сети. В этом докладе приоритет у **позитивных** данных: что работает, как устроены рабочие сервисы, разборы, замеры. Единичные жалобы считаются анекдотами. Маркетинг и самоотчёты сервисов помечены отдельно.
- **Классы выводов** (как в `research/2026-10-04/vpn-news_04-10-26.md`):
  - **[репо]** дефект этого инсталлера;
  - **[версия]** проблема появляется только при `latest`;
  - **[среда]** блокировки, то есть операционные издержки.

**Легенда статусов**

| Значок | Значение |
|---|---|
| ✅ verified | Оба адверсария не опровергли и подтвердили |
| 🟡 partly | Подтверждено с сужением. **Используется исправленная формулировка**, она и приведена в тексте |
| ⚔️ contested | Один адверсарий опроверг |
| ❓ unverified | Не проверялось: не хватило лимита. Иногда стоит пометка «косвенно», если факт всплыл в голосах по другому пункту или в коде |
| 📄 код | Факт проверен по исходникам или файлам при подготовке черновика конфига (3x-ui v3.9.0, runetfreedom .dat), адверсариев не проходил |

**Итог проверки.** 36 утверждений дошли до двух адверсариев. **Все 36 получили 🟡:** у каждого есть сужение, у 35 по обоим голосам «partly», у s-hosting-law «holds» + «partly». Чистых ✅ нет, ⚔️ нет. Ещё 27 утверждений про компрометаторов не уложились в лимит (❓). Это нормально для темы, где почти всё держится на самоотчётах сервисов и полевых постах. Целиком опровергнутых утверждений нет, но много опровергнутых **частей**: они собраны в §7.

---

## 1. TL;DR

### Секреты успеха (12)

Колонка «Сервер?» отвечает на вопрос, можно ли заложить это в инсталлер.

| # | Секрет | Класс | Сервер? |
|---|---|---|---|
| 1 | **Магии нет.** У рынка стандартный стек: VLESS+Reality обязателен у всех; запасной у всех свой: AWG, SS, OpenVPN, WG, у некоторых Hy2 и XHTTP. Конкурируют поддержкой и скоростью реакции (The Bell 25.01.2026). Нынешний набор инсталлера (Reality + Hy2 + AWG) рыночный, экзотика не нужна. 🟡 | [среда] | ✅ уже есть |
| 2 | **Подписка с автообновлением вместо ссылок.** В ней несколько транспортов, сервер меняет адреса, клиент их подтягивает. Сама подписка — единая точка отказа, а кривая миграция через автообновление может стереть узлы у клиентов. 🟡 | [репо] | ✅ фаза `09` |
| 3 | **Выживает тот, кто быстро передеплоится, а не тот, у кого «идеальный протокол».** IP-баны 2026 года шли по префиксам VPS-хостингов, выборочно, а не по ASN целиком, и параллельно блокировали по протоколу и поведению. Самоделу нужны бэкап одной командой, переустановка за минуты и 2+ exit на разных ASN. 🟡 | [среда] | ✅ export/import |
| 4 | **Хостер важен не меньше протокола.** Для TCP+TLS «заморозка 16–20 КБ» бьёт по засвеченным ASN: Hetzner, DO, OVH, Contabo, Amazon. SSH и TCP без TLS она не трогает. Проверять IP из РФ до раздачи ключей. 🟡 | [среда] | ✅ печать ASN + чекер |
| 5 | **Раздельная маршрутизация «РФ напрямую» приходит вместе с подпиской** (Happ routing, roscomvpn, runetfreedom). Она прячет **только IP выхода**, а не сам факт VPN. 🟡 | [репо] | ✅ routing-заголовок |
| 6 | **Каскад RU-вход → зарубежный exit** работает на периоды белых списков (БС). По умолчанию клиент ходит прямо на exit, RU-вход держать отдельным включаемым профилем. ТСПУ стоят и в RU-ДЦ, белый IP получить почти невозможно (только Yandex Cloud), а ToS и законы против. 🟡 | [среда] | ✅ опциональная роль `entry` |
| 7 | **SNI-донор:** малоизвестный TLS1.3/H2-сайт из своего ASN (RealiTLScanner) или selfsteal, fp=chrome. Массовых одинаковых SNI избегать: такие кластеры видны в censys. RU-SNI на чужом IP ненадёжен, но не «рвётся всегда». 🟡 | [репо] | ✅ сканер + 2–3 serverNames |
| 8 | **Второй inbound XHTTP+Reality** на другом порту с другим SNI — дешёвая страховка от RST и порогов на 443. Xmux настраивается на клиенте. Против бана IP он не помогает. 🟡 | [репо] | ✅ |
| 9 | **AWG: уникальные параметры на каждую установку.** Сейчас они одинаковы у всех, а РКН получает отпечаток протокола из купленных конфигов и бьёт по нему массово. AWG 3.x требует клиент ≥5.0.1.5. 🟡 | [репо] | ✅ фаза `06` |
| 10 | **Закрытость:** не публиковать ключи, подписку держать на неугадываемом пути, панель прятать. Серверы, чьи адреса попадают в купленные подписки, горят за часы. Self-hosted горит реже, но бан всей AS задевает и его. 🟡 | [репо] | ✅ |
| 11 | **Пинить версии клиентов и компонентов.** Xray ≥v26.9.8 на Go 1.27 ломает xmux на **клиенте** XHTTP. Пин Xray на сервере это не лечит. Hy2 пинить на app/v2.12.3. 🟡 | [версия] | ✅ частично (Hy2, README) |
| 12 | **Мониторинг изнутри РФ сериями.** Нужна передача больше 20 КБ в течение 2–3 мин, а не ping: короткая проба не видит заморозку. Проба из ДЦ не видит ни мобильных БС, ни ТСПУ домашних провайдеров. 🟡 | [среда] | 🟡 опциональный `probe.sh` |

### Компрометаторы (7)

| # | Пункт | Сервер? |
|---|---|---|
| 1 | **Главный задокументированный канал утечки адреса сервера:** RU-приложение (MAX, март 2026) запрашивает свой внешний IP через echo-сервисы (ipify, checkip.amazonaws, ifconfig.me). Если этот запрос идёт в туннель, приложение видит IP exit. Что эти данные доходят до РКН/ТСПУ, **не доказано**. 🟡 | ✅ `ip-echo → warp/blocked` |
| 2 | **Серверный блок RU-назначений на exit** (geoip:ru, category-ru, tld-ru → blackhole/WARP). Это страховка: RU-сервис не увидит IP VPS при прямом обращении. От локального SOCKS + зарубежного echo, `/proc/net/route` и RU-сервисов на зарубежных IP не спасает. 🟡 | ✅ фаза `07`, Hy2 ACL, AWG ipset |
| 3 | **Разнос входа и выхода.** RU-вход + гео-сплит: RU-приложения видят российский IP. Второй IP через `sendThrough`/WARP на том же VPS лишь переживает точечный бан выхода. 🟡 | ✅ опционально |
| 4 | **Факт VPN на Android сервер не скроет.** TRANSPORT_VPN, tun0, список установленных VPN-клиентов видны и исключённым из туннеля приложениям. По RKS Global, 22 из 30 топ-приложений, к 16.04 все 30. Помогает только роутер, второй телефон или рабочий профиль. ❓ (косвенно подтверждено в голосах по s-split-routing) | ❌ только клиент |
| 5 | **Открытый локальный SOCKS у клиентов** (v2rayNG, Happ и др.) позволяет любому приложению узнать IP exit. Happ ещё и отдавал xray API с UUID и ключами. **Наша находка 📄:** JSON-подписка 3x-ui v3.9.0 сама кладёт клиенту `socks 127.0.0.1:10808 auth:noauth`. ❓ + 📄 | ✅ частично: `subHappLocalProxyAuth`, JSON-подписка выключена по умолчанию |
| 6 | **Регуляторика:** методичка Минцифры (IP → устройство → десктоп), карта IP абонентов (216-ФЗ), 210-ФЗ против хостеров, проект «Антифрод 3.0» с годовым баном у RU-хостеров с 01.03.2028. Всё это бьёт по RU-входу, а не по зарубежному exit. 🟡 / ❓ | ✅ RU-вход только опцией |
| 7 | **РКН покупает подписки** коммерческих VPN и автоматически вытаскивает из них конфиги. Для самодела это повод не пускать чужих или пробных пользователей на основную ноду. 🟡 | ✅ отдельная нода для внешних |

---

## 2. Секреты успеха по категориям

### 2.1 Архитектура

**s-sub-multi. Подписка с автообновлением и несколькими транспортами 🟡**
- Исправленная формулировка. У рабочих сервисов одна подписка с автообновлением, несколькими узлами и конфигами. Сервер меняет IP, клиент их подтягивает. При этом «пользователь ничего не чинит» — неправда: сервисы до сих пор просят вручную обновить подписку или включить автообновление.
- URL подписки — единая точка отказа. Из РФ её отдают через RU-зеркало или резервный URL.
- У самодела с одним VPS подписка даёт только разнообразие протоколов: при бане IP недоступна и подписка с того же IP.
- **Ограничение 3x-ui:** в подписку попадают только её собственные inbound. Standalone Hy2 и ядерный AWG туда не попадут: AWG в 3x-ui сделан userspace-relay и в JSON-формат не входит. Обход — external links, куда можно положить `hysteria2://`.
- `subUpdates` по умолчанию 12 ч (📄). Контрпример: Habr 1040846 — при смене IP почасовое автообновление вместе с кэшем DNS стёрло узлы у клиентов.
- Источники: [Liberty](https://t.me/s/vpn_liberty), [Habr 1040846](https://habr.com/ru/articles/1040846/), [Теплица 30.09](https://te-st.org/2026/09/30/vlessmore/), [SecurityLab](https://www.securitylab.ru/blog/personal/Bitshield/361724.php), [3x-ui docs subscription](https://docs.sanaei.dev/docs/config/subscription/), [Quazar middleware](https://github.com/Quazar-VPN/remnawave-subscription-middleware).

**s-entry-cascade. Каскад RU-вход → зарубежный exit 🟡**
- Это основной рабочий приём против «заморозки 16 КБ» и мобильных белых списков по CIDR: net4people #490, Habr 1040846 (3x-ui, 4 RU-входа + 8 выходов), 38 БС-конфигов igareck на Selectel/Timeweb/VK от 03.10.2026.
- **Но** с июня 2026 РКН целенаправленно бьёт по каскадам: блокирует подсети Timeweb/Selectel/Beget, хостеров обязали отключать VPN-клиентов. VK отозвал белые IP без предупреждения (4→2).
- Детекторы приложений каскад скрывает только по GeoIP и только при split-routing. Android-проверку TRANSPORT_VPN он не скрывает.
- Прямой exit (Hy2/Reality) остаётся основным.
- Источники: [net4people #490](https://github.com/net4people/bbs/issues/490), [Habr 1046025 (RUVDS)](https://habr.com/ru/news/1046025/), [igareck БС](https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt), [Habr 990206](https://habr.com/en/articles/990206/).

**s-entry-caveat. ТСПУ в RU-ДЦ 🟡**
- Liberty (пост №309, 16.06.2026, самоотчёт): ТСПУ «начали принудительно устанавливать» у хостеров, и это «полностью убило каскад для сервисов».
- Независимо: ntc 131928 (11.04.2026) — RU-ДЦ фильтруют зарубежный трафик.
- «Потерял смысл» — перебор: каскады используют и после июня, с деградацией (ntc 16061#1204, 25362#6, 25437#8, где Hy2 в каскаде).
- Тот же ТСПУ в ДЦ бьёт и по плечу RU→зарубеж в режиме БС, поэтому RU-вход не гарантирован. Межузловой транспорт должен быть обфусцирован.
- Источники: [Liberty #309](https://t.me/vpn_liberty/309), [ntc 131928](https://ntc.party/p/131928), [ntc 25437](https://ntc.party/t/25437).

**s-sacrificial-front. «Жертвенный» фронт 🟡**
- Liberty в Meduza (03.04.2026): VPS за ~$30 стоит перед bare metal за ~$1000/мес на ~10 тыс. пользователей. Это единственный источник и слова оператора.
- Для самодела выигрыш меньше: один дешёвый VPS и так легко пересоздать.
- Фронт без перевыпуска конфигов возможен, только если клиент ходит по домену или подписке.
- Минусы: ядро видит IP фронта, поэтому лимиты по IP в 3x-ui ломаются. Двойной трафик. ACME для Hy2 только через DNS-01.
- Источник: [Meduza 03.04](https://meduza.io/feature/2026/04/03/mozhno-li-deystvitelno-zablokirovat-vse-vpn), [Xray tunnel](https://xtls.github.io/en/config/inbounds/tunnel.html).

**s-layered-cdn. Многоуровневый fallback (Reality → CDN → RU-облако → WebRTC) 🟡**
- Это опыт одного автора (Habr 1021160 / pvsm 449328), а не проверенная практика. L3 (WebRTC через Телемост) он только подготовил. L1 на Cloudflare сам называет деградировавшим: Cloudflare в РФ с 06.2025 под порогом 16 КБ.
- Факт 📄: в Xray с v26.1.31 есть `uplinkHTTPMethod` (по умолчанию POST). При GET данные идут в заголовках чанками по 3–4 КБ, отдача медленная.
- «POST режут почти все CDN» относится только к российским CDN.
- sing-box-клиенты XHTTP не умеют. CDN Яндекса требует аккаунт с идентификацией личности.
- Источники: [pvsm 449328](https://www.pvsm.ru/vpn/449328), [proxy-via-russian-cdn](https://github.com/ServerTechnologies/proxy-via-russian-cdn), [Xray splithttp config.go](https://raw.githubusercontent.com/XTLS/Xray-core/v26.1.31/transport/internet/splithttp/config.go).

**l-entry-exit-split. Разнос входа и выхода 🟡** (см. §5)

### 2.2 Транспорты

**s-stack-diversity. Рынок однороден 🟡**
- The Bell (25.01.2026): «стандартный набор протоколов», «никакой магии нет», конкуренция в поддержке и реакции. Сам набор The Bell не называет.
- У названных сервисов общий только VLESS (Reality). Запасные разные:
  - Amnezia — AWG (Hy2 нет);
  - Blanc — WG EXTRA, OpenVPN;
  - VPN Generator и Liberty — SS/Outline.
- Hy2 и XHTTP у этих сервисов не подтверждены. Есть и нестандартные протоколы: TrustTunnel (AdGuard), NaiveProxy.
- Устойчивость даёт прежде всего инфраструктура (расходные VPS-входы, балансировка, разные хостинги, RU-реле), а не набор протоколов.
- Для сервера: VLESS Reality обязателен. Как дополнительный запасной стоит рассмотреть **Shadowsocks-2022**: в net4people #546 надёжнее XHTTP помогали не-TLS протоколы.
- Источники: [The Bell](https://thebell.io/kak-ustroen-rossiyskiy-rynok-vpn-pochemu-topy-lyubyat-ai-i-vebinar-o-tom-est-li-v-etoy-tekhnologii-puzyr), [Meduza 03.08](https://meduza.io/feature/2026/08/03/teoreticheski-oni-mogut-zablokirovat-lyuboy-servis-tselikom), [BlancVPN help](https://blancvpn.pro/ru/help/quick-help).

**s-field-config-slice. Срез живых публичных конфигов igareck 🟡**
- Снимок 04.10.2026 00:46 МСК, пересчитан адверсарием: 133 VLESS-ссылки, 40 уникальных UUID (операторов), 61 хост.
- Порт 443 у 123 ссылок.
- По операторам: Reality-TCP у 23 (Vision у 15), XHTTP+TLS у 6, WS+TLS у 5, **XHTTP+Reality у 1**.
- Живой XHTTP — это TLS со своим доменом (CDN). Доли WS раздуты одним оператором: 1 UUID на 32 IP дают 24% строк.
- «Mobile» значит облегчённый ТОП-150 для телефона. Проверка идёт с сервера в РФ, а не на мобильных сетях.
- Главный вывод держится: 443 + Reality+Vision основным, вторым XHTTP через TLS/CDN. Сигнал слабый: ошибка выжившего, бесплатные конфиги быстро умирают.
- Источник: [BLACK_VLESS_RUS_mobile.txt](https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt), [README](https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/README.md).

**s-second-inbound. Запасной XHTTP+Reality на другом порту 🟡**
- Дешёвая страховка, но с анекдотичными доказательствами.
- Habr 1049680 (19.06.2026): RST через ~1,5 мес. ушли после перехода на XHTTP **и** одновременной смены донора. Какая мера сработала, не разделить.
- net4people #546 (МГТС, JustLan, RTK, ноябрь 2025): лучше всего помогала смена порта с 443; XHTTP/mux «помогает не всем».
- Технические ограничения:
  - mux.cool с XHTTP запрещён, разрешён только xmux;
  - mux несовместим с Vision;
  - при REALITY mode `auto` не даёт packet-up, его нужно задать явно.
- Логический риск: Reality на 8443 пересылает пробы на `dest:443`, а у донора 8443 нет — это аномалия.
- Источники: [Habr 1049680](https://habr.com/ru/articles/1049680/), [net4people #546](https://github.com/net4people/bbs/issues/546), [Xray discussion 4113](https://github.com/XTLS/Xray-core/discussions/4113).

**s-xhttp-xmux. Xmux и порог параллельных TLS 🟡**
- Подтверждено: RPRX поменял клиентский дефолт xmux. Коммит 18b85ad (27.06.2026, anti-RKN) сменил maxConcurrency=1 на maxConnections=6, коммит 18e2839 (28.07.2026, anti-TSPU) снизил 6→3. Релиз v26.7.28.
- **Не подтверждено:**
  - порог «больше 3 TLS — заморозка ~120 с» — гипотеза из одного источника, срабатывает в связке (AND) с ASN и отпечатком;
  - «месяц стабильно на МТС» есть только в пересказе sing-box-lx #32.
- Vision-TCP открывает много соединений, и рынок на нём живёт. Значит, жёсткого лимита 3 нет.
- xmux — **клиентская** настройка. 3x-ui её не хранит: issue #5194 закрыт как not planned. Нельзя одновременно задавать `maxConcurrency>0` и `maxConnections`.
- Источники: [коммит 18e2839](https://github.com/XTLS/Xray-core/commit/18e2839), [коммит 18b85ad](https://github.com/XTLS/Xray-core/commit/18b85adb4e288f49a7894351c6e0f2428c0beef6), [#6376](https://github.com/XTLS/Xray-core/issues/6376), [Habr 1047442](https://habr.com/ru/articles/1047442/), [3x-ui #5194](https://github.com/MHSanaei/3x-ui/issues/5194).

**s-pin-xray. Баг xmux в Xray ≥v26.9.8 (Go 1.27) 🟡 [версия]**
- Баг реален: #6797, открыт, мейнтейнеры не ответили. PR #6820 с фиксом закрыт без merge, Fangliding ждёт фикса в Go.
- Затронуты v26.9.8, v26.9.9, main и **v26.9.30**, которую тянет 3x-ui v3.9.0 (go 1.27, x/net v0.59.0). Версия v26.7.28 (go 1.26, x/net v0.57.0) чиста.
- Баг живёт в **клиентском** XHTTP-дозвоне. Пин Xray на сервере его не лечит, а откат ядра внутри 3x-ui v3.9.0 может сломать панель: она мигрирует конфиг под новое ядро.
- Причинно-следственная связь обратная: сначала ТСПУ рвёт хендшейки (у репортера 1258 из 1261 без ответа), а шторм соединений и OOM идут уже следствием.
- Действия:
  - в README перечислить клиенты с ядром ≤v26.7.28 или со сборкой `-tags http2legacy`;
  - пинить Xray только на RU-входе с XHTTP-outbound.
- v2rayNG #6224 (Hy2 mport, libXray 26.9.9) — отдельная клиентская регрессия.
- Источники: [#6797](https://github.com/XTLS/Xray-core/issues/6797), [PR #6820](https://github.com/XTLS/Xray-core/pull/6820), [go.mod v26.9.30](https://raw.githubusercontent.com/XTLS/Xray-core/v26.9.30/go.mod), [v2rayNG #6224](https://github.com/2dust/v2rayNG/issues/6224).

**s-hy2. Hysteria2 🟡**
- Работает на большинстве сетей. FOCI 2026 (net4people #654): ТСПУ с середины 2023 режет QUIC v1 только по SNI из блоклиста, а не весь QUIC. Hy2 на своём домене этот фильтр обходит.
- Против говорят только посты на ntc, замеров нет:
  - T2: полный отказ (окт. 2025 – май 2026), хотя у автора ветки сервер стоял в RU-ДЦ;
  - Билайн: в мае 2026 Hy2 не работал;
  - МегаФон: с TLS и masquerade заработал у одного человека, но к 31.05 подключался через раз.
- Поэтому Hy2 даём **дополнением** к Reality/XHTTP.
- Что реальный сертификат лучше самоподписанного, не доказано. Port hopping в РФ против шейпинга одного порта не подтверждён: помогает, только если режут конкретный порт.
- Встроенный listen-диапазон брать только с **v2.12.3+**: там исправлен баг, когда правила перехватывали исходящий UDP.
- В официальном URI hopping задаётся в host (`host:20000-50000`), без интервала. `mport`/`mportHopInt` — расширение Happ, его документацию проверить не удалось.
- Salamander несовместим с masquerade/HTTP3, поэтому ему нужен отдельный порт.
- Источники: [ntc 20340](https://ntc.party/t/hysteria2-%D0%BD%D0%B5-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%B5%D1%82-%D0%BD%D0%B0-%D1%82%D0%B5%D0%BB%D0%B52-%D0%BF%D0%BE%D1%85%D0%BE%D0%B4%D1%83-%D0%BF%D0%BE%D0%BB%D0%BD%D1%8B%D0%B9-%D0%B1%D0%BB%D0%BE%D0%BA-quic/20340), [net4people #654](https://github.com/net4people/bbs/issues/654), [Habr 1008554](https://habr.com/ru/articles/1008554/), [Port Hopping](https://v2.hysteria.network/docs/advanced/Port-Hopping/), [URI Scheme](https://v2.hysteria.network/docs/developers/URI-Scheme/), [releases](https://github.com/apernet/hysteria/releases).

**s-awg. AmneziaWG после атаки лета 2026 🟡**
- Атака 20.05–25.07.2026 была гибридной: баны IP и /24, DDoS, разведка API, поиск новых серверов за часы. Это удар по инфраструктуре коммерческого сервиса.
- Восстановились за счёт клиента 5.0.1.5 (AWG 3.x) и отключения старых клиентов.
- Детект AWG 2.0 «по статистике потока» — гипотеза самой Amnezia.
- Self-hosted блокировки «почти не коснулись» (Habr 1080534, Meduza: «self-hosted работает даже на устаревшей AWG»).
- Kernel module: AWG 3.0 с 30.07, 3.1 с 12.08, фиксы до 06.09.2026. Дата «AWG 2.0 для self-hosted 27.08» неверна: 2.0 описан 25.03.2026.
- Что нового в 3.1:
  - HeaderProtectionKey, для него нужны **S1–S4 ≥ 12**;
  - RandomTrailers, ContentPaddingAddition;
  - диапазоны таймингов keepalive и rekey;
  - DisableCookies.
- 3.x несовместим с клиентами 2.0, нужен AmneziaVPN ≥5.0.1.5. MTU 1376→1280 советуют в посте о 3.1.
- Источники: [постмортем](https://amnezia.org/ru/blog/amnezia-vpn-hybrid-attack-postmortem), [AWG 3.1](https://amnezia.org/ru/blog/amneziawg-3-1-is-here), [kernel module commits](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/commits/master), [Habr 1080534](https://habr.com/ru/companies/amnezia/articles/1080534/), [Habr 1014636](https://habr.com/ru/companies/amnezia/articles/1014636/).

### 2.3 Хостинг и IP

**s-asn-choice. ASN хостера 🟡**
- Для TCP/TLS (Reality) репутация IP/ASN решает многое, но фраза «прежде всего ASN, а не протокол» неверна: SSH и TCP без TLS проходят.
- Скан Ростелекома 10.02.2026 (Habr 997088): 72 ASN заморожены полностью, 391 частично. Полностью: Hetzner AS24940, Contabo AS51167, DO, OVH, Amazon. С 06.2026 в список попали и RU-облака.
- Aeza в 10.2025 — это блок подсетей 138.124/16 по списку РКН, а не «заморозка 16 КБ».
- При БС любой зарубежный ASN мёртв. Обходы для засвеченного ASN: SNI из вайтлиста и промежуточный узел.
- Чекер hyperion-cs «TCP 16-20» принимает свой хост, но, похоже, нужен валидный HTTPS (selfsteal). Голый IP с REALITY вряд ли пройдёт.
- Источники: [net4people #490](https://github.com/net4people/bbs/issues/490), [Habr 997088](https://habr.com/ru/articles/997088/), [чекер TCP 16-20](https://hyperion-cs.github.io/dpi-checkers/ru/tcp-16-20), [dpi-checkers README](https://github.com/hyperion-cs/dpi-checkers/blob/main/README.md).

**s-fast-redeploy. Ротация и передеплой 🟡**
- Массовые IP-баны 04.08 и 21.09.2026 прошли по префиксам VPS-хостингов, выборочно. По net4people #671: Veesp 5000 из 30000 IP, Amnezia Hosting 131 из 20000.
- Параллельно продолжаются блокировки по протоколу и поведению: QUIC, DTLS-отпечатки, заморозка 16 КБ.
- Liberty меняет адреса минимум раз в сутки, блок включается по всей стране одновременно (пост 316).
- Ротация раз в сутки — режим коммерческих сервисов, чьи ноды вычисляют через клиенты и API. Самоделу важнее три вещи:
  - хостинг вне «VPN-кластерных» AS;
  - второй exit на другом ASN;
  - передеплой за минуты.
- Источники: [Liberty 316](https://t.me/s/vpn_liberty/316), [net4people #671](https://github.com/net4people/bbs/issues/671), [Euronews](https://ru.euronews.com/my-europe/2026/08/05/vpn-blockage-russia), [AppleInsider](https://appleinsider.ru/news/posle-vyborov-v-gosdumu-vpn-v-rossii-nachali-blokirovat-eshhe-zhestche-chto-proishodit.html).

**s-whitelist-mechanics. Белые списки на мобильном интернете 🟡**
- Нужен IP назначения из разрешённых пулов. По скану через МегаФон их 63 126 из ~46 млн (~0,14%), из них 12 906 у Yandex Cloud.
- По операторам:
  - **МегаФон:** нужен ещё SNI из whitelist, белый SNI сверяется с CIDR;
  - **МТС/T2:** фильтр L3, по полевым постам (ntc, май 2026) — «если IP пингуется, доступен полностью», проходят любые порты и протоколы, в том числе Hy2/AWG.
- «Только связка IP+SNI» — неверно. Есть туннели через белые сервисы: YC Functions/API Gateway, WebRTC/TURN, DNS.
- Yandex Cloud банит физлиц за прокси через 1–2 мес. Случайный RU-VPS почти наверняка не белый.
- Источники: [pvsm 450362](https://www.pvsm.ru/vpn/450362), [ntc 24230](https://ntc.party/t/vpsvds-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D1%8E%D1%89%D0%B8%D0%B5-%D0%BF%D1%80%D0%B8-%D0%B1%D0%B5%D0%BB%D1%8B%D1%85-%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%D1%85/24230), [net4people #650](https://github.com/net4people/bbs/issues/650), [runetfreedom rules-dat](https://github.com/runetfreedom/russia-v2ray-rules-dat).

**s-ru-hosters-risk. RU-хостеры 🟡**
- Белые IP в полевых отчётах находят чаще всего у Yandex Cloud (нужно подбирать диапазоны) и VK Cloud (ntc 20117: «самый высокий шанс», но VK отзывал непокупные IP).
- ToS RUVDS, Beget (shared) и NTX запрещают публичные VPN и прокси. AUP Yandex Cloud от 26.08.2026 VPN прямо не запрещает.
- «Без ключей» входная нода невозможна: у неё свой inbound. Ключи генерировать из переменных.
- Брать статический (купленный) IP. Проверять «белость» так: поднять веб на :443 и открыть с мобильного без VPN.
- Источники: [Habr 990206](https://habr.com/ru/articles/990206/), [ntc 20117](https://ntc.party/t/20117), [Теплица 12.05](https://te-st.org/2026/05/12/hostingrules/), [Yandex AUP](https://yandex.ru/legal/cloud_aup/).

**s-ipv6. IPv6 🟡**
- 16–18.02.2026 VLESS/Reality массово ложились на IPv4 ряда хостеров, а IPv6 того же сервера часто оставался доступен (ntc 22270, 4625).
- Но IPv6 в РФ есть у ~2,9% пользователей (APNIC): МТС 5,16%, МегаФон 4,88%, Ростелеком 2,57%, T2 0%.
- Вывод: IPv6-endpoint держать дополнительным, слушать на `[::]`, файрвол настроить и для v6.
- Источники: [Habr 1000694](https://habr.com/ru/articles/1000694/), [ntc 22270](https://ntc.party/t/22270), [APNIC RU](https://stats.labs.apnic.net/ipv6/RU).

**s-hosting-law. Право для RU-входа 🟡** (evidence: holds, applicability: partly)
- 210-ФЗ от 26.06.2026 («Антифрод 2.0»), по пересказам Хакера и Медиазоны, запрещает хостерам давать мощности владельцам ресурсов, нарушающих ст. 15.8 149-ФЗ. Дата вступления этого пункта по первичному тексту не проверена.
- «Антифрод 3.0» (01.10.2026) пока **проект**: обсуждение до 15.10.2026, вступление предполагается 01.03.2028. В нём реестр РКН, KYC с целью аренды, годовой бан у **любого** RU-хостера. Есть исключение для корпоративных VPN с «ограниченным кругом» пользователей.
- Статус личного relay для своих серый. Реальный риск — расторжение договора и бан по ToS.
- Источники: [Хакер 10.06](https://xakep.ru/2026/06/10/antifraud/), [Медиазона 01.10](https://zona.media/news/2026/10/01/vpn), [CNews 01.10](https://www.cnews.ru/news/top/2026-10-01_po_vpn_v_rossii_nanesen_tyazhelejshij), [Коммерсантъ](https://www.kommersant.ru/doc/8590872), [Право.ру](https://pravo.ru/news/264452/).

### 2.4 Клиенты и подписки

**s-happ-headers. Заголовки Happ 🟡. Самое важное сужение доклада.**
- Механизм задокументирован: Happ берёт настройки из заголовков ответа подписки или из строк тела с префиксом `#`.
- **Бесплатно, без Provider ID:** `profile-update-interval`, `profile-title`, `subscription-userinfo`, `announce`, `support-url`, `routing` и отключение роутинга.
- **Требуют Provider ID с премиум-статусом:** autoconnect(-type), ping-onopen, ping-type, auto-update-enable/open-enable, глобальные fragmentation/noises, per-app-proxy, server-address-resolve, fallback-url. Нужно зарегистрировать домен подписки в кабинете happ-proxy.com, тариф считается по числу HWID.
- С Provider ID Happ раз в сутки шлёт на `check.happ-proxy.com` HWID, ОС и хэш домена. Это минус для приватности.
- Верные имена: `subscription-autoconnect-type`, `subscription-ping-onopen-enabled`.
- 3x-ui v3.9.0 (`internal/sub/happ.go`) отдаёт Happ-заголовки, только если UA содержит «happ» **и** включён `subHappAutoDetect` (📄).
- У v2RayTun `update-always: true` и `network-filter: true` бесплатные.
- Для самодела остаются: `profile-update-interval` + `routing` + `update-always` + auth-mode локального прокси (📄: комментарий в коде «standard headers (no ProviderID)»).
- Источники: [Happ app-management](https://docs.happ.info/main/dev-docs/app-management), [Provider ID](https://docs.happ-proxy.com/getting-started/provider-id.md), [premium](https://docs.happ-proxy.com/getting-started/premium-functionality.md), [v2RayTun headers](https://docs.v2raytun.com/overview/supported-headers), [3x-ui v3.9.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0).

**s-client-balancer. Профиль «Авто» и балансер 🟡**
- 3x-ui ≥v3.7.0 (PR #6243): Settings → Sub Balancers.
- Работает только для **JSON-подписки** и только по инбаундам 3x-ui.
- burstObservatory по умолчанию: HEAD `https://www.google.com/generate_204`, 1m, timeout 5s, sampling 2. fallbackTag = первый участник, ставится только при leastPing/leastLoad.
- leastPing выбирает узел по задержке, порядок участников не важен, поэтому «RU-вход последним» не делает его резервным. Приоритет задаётся весами leastLoad (v3.8.0).
- Clash-подписка 3x-ui даёт только группу `select`, url-test в ней нет.
- HEAD-проба весит единицы КБ, поэтому заморозку после 15–20 КБ не видит.
- Какие клиенты (Happ, v2RayTun, Streisand) поддерживают балансеры, не подтверждено.
- ⚠️ Конфликт с §4: JSON-подписка 3x-ui кладёт клиенту SOCKS без пароля (📄).
- Источники: [PR #6243](https://github.com/MHSanaei/3x-ui/pull/6243), [json_service.go](https://raw.githubusercontent.com/MHSanaei/3x-ui/main/internal/sub/json_service.go), [clash_service.go](https://raw.githubusercontent.com/MHSanaei/3x-ui/main/internal/sub/clash_service.go).

**s-sub-url-resilience. URL подписки как точка отказа 🟡**
- Если URL подписки недоступен, уже скачанные конфиги работают, но ротация и новые узлы до клиента не доходят.
- Опасен и некорректный ответ: Happ удалял серверы из подписки (Habr 1040846). Автообновление там **не** выключали, совет выключать дан задним числом.
- Happ `sub-change` доступен только на платном Provider-плане. Поэтому смену домена заранее объявлять вручную, со ссылкой на запасной вариант.
- Для самодела: HTTPS на своём (под)домене, RU-зеркало — опционально. Кеш вместо пустого ответа — разумно, но источником не подтверждено.
- Источники: [Quazar middleware](https://github.com/Quazar-VPN/remnawave-subscription-middleware), [Happ API](https://docs.happ-proxy.com/getting-started/api.md).

**s-clients. Дистрибуция клиентов 🟡**
- Happ удаляли из RU App Store несколько раз: в 2025, 25.06.2026 (в тот же день вернули), ~07.07.2026. 30.03.2026 убрали Streisand, V2Box и v2RayTun.
- Чем заменяли:
  - Liberty: INCY, v2RayTun, смена региона Apple ID (Karing не предлагал);
  - Blanc: Karing, с 07.07 — INCY.
- Happ 4.4.6/4.4.8 — это **desktop pre-release** (стабильная 4.3.0), к мобильным не относится.
- «Клиент — слабое звено чаще сервера» не доказано: Blanc 12.06 пишет, что «блокировки затронули практически всех».
- 3x-ui v3.9.0 отдаёт base64, Xray-JSON и Mihomo, плюс external links. Отдельный sing-box JSON не нужен: Karing берёт Mihomo и base64.
- Источники: [Liberty 310](https://t.me/vpn_liberty/310), [Blanc 129](https://t.me/blancvpn/129), [Blanc 131](https://t.me/blancvpn/131), [happ-desktop releases](https://github.com/Happ-proxy/happ-desktop/releases), [3x-ui sub](https://github.com/MHSanaei/3x-ui/tree/v3.9.0/internal/sub).

**s-fragmentation. Фрагментация ClientHello и UDP-noise 🟡**
- Happ включает fragment удалённо только через Remote Control API на тарифе Pro/Enterprise, заголовков noise у него нет. INCY получает fragmentation/noises заголовками от 3x-ui (`incy.go`).
- noise — UDP-маска, на VLESS+REALITY по TCP она бесполезна. tlshello тормозит рукопожатие, а не поток.
- В 3x-ui:
  - `subJsonFragment` есть с v2.3.5, а не с v3.3.1;
  - глобальный `subJsonFinalMask` действует на **все** конфиги;
  - отдельный профиль «Anti-DPI» делается как Host с finalMask fragment.
- От IP-банов, заморозки и БС fragment не спасает. По умолчанию держать выключенным.
- Источники: [Happ premium](https://docs.happ-proxy.com/ru/getting-started/premium-functionality.md), [Xray finalmask](https://xtls.github.io/en/config/transports/finalmask.html), [vpnon](https://vpnon.io/ru/blog/happ-settings-fragmentation-noise-guide).

**s-doh-resolve. DoH pre-resolve адреса узла 🟡**
- Механизм есть: Happ `server-address-resolve-*` (премиум), в 3x-ui v3.9.0 заголовки «DoH pre-resolution».
- Pre-resolve идёт **до туннеля**, напрямую через сеть РФ. С 21.08.2026 есть отчёты о точечной блокировке cloudflare-dns.com, dns.google и quad9 у Dom.ru и Билайна (ntc 23007). При БС иностранный DoH недоступен.
- Сплит DNS RU→77.88.8.8 / remote 8.8.8.8 (roscomvpn) касается трафика пользователя, а не адреса узла.
- Вывод: если в ссылке IP, проблемы нет. Домен нужен только ради ротации IP.
- Источники: [Happ app-management](https://happ.mintlify.app/technical-docs/app-management), [Happ routing](https://happ.mintlify.app/technical-docs/routing), [ntc 23007](https://ntc.party/t/23007).

**s-kakadu-reverse. Вскрытие «невидимого протокола» 🟡**
- Habr 1033400 (09.05.2026), один сервис: в APK лежат libbox.so (sing-box) и wireguard-go, патента нет.
- Вход — Selectel 155.212.215.82, SNI say-today.ru (фасадный сайт).
- VLESS+Reality автор лишь предположил по Wireshark. n=1, на рынок не обобщается.
- Мораль та же: решает инфраструктура, а не секретный протокол.

### 2.5 Роутинг

**s-split-routing. «РФ напрямую» в подписке 🟡**
- Стандарт рынка:
  - BlancVPN: режим «Отключать VPN для российских сервисов»;
  - ProxysVPN: «RU Split»;
  - roscomvpn-routing (2.7k★): профили DEFAULT/WHITELIST/JSONSUB, deeplink для Happ/INCY;
  - runetfreedom: обновление каждые 6 ч.
- **Опровергнуто:** «банки не видят VPN». Сплит прячет только IP выхода. RKS Global: исключение приложений из VPN бесполезно, tun0 и TRANSPORT_VPN видны. Яндекс блокирует по серверному флагу. Даже в Habr 1040846 со включённым VPN все приложения, кроме Госуслуг, отказывались работать.
- Баг 3x-ui: INCY получает Happ-профиль (#6273, not planned).
- Серверный `block geoip:ru` без клиентского сплита ломает RU-сайты.
- Источники: [roscomvpn-routing](https://github.com/hydraponique/roscomvpn-routing), [rules-dat](https://github.com/runetfreedom/russia-v2ray-rules-dat), [RKS Global](https://rks.global/ru/research/vpn-detection/), [kod.ru](https://kod.ru/vse-30-ru-prilojeniy-sledyat-za-vpn), [ProxysVPN](https://proxysvpn.com/guides/happ-routing-profiles), [3x-ui #6273](https://github.com/MHSanaei/3x-ui/issues/6273).

**s-sni-choice. Выбор SNI-донора 🟡**
- Механизм не «гео-IP», а сверка «важных» RU-доменов с их реальными CIDR. ozon.ru на **RU-VPS** не работал на МегаФоне, а rutube и google работали (ntc 24119).
- vk.com/cloudflare.com на VPS в Германии неделями стабильны на МТС (#650).
- Подтверждённые блокировки RU-SNI были точечными: в сторону подсетей Aeza (10.2025).
- Крупные зарубежные SNI (github, microsoft, twitch) в полевых отчётах работают, Habr 1009542 против только Apple.
- Главный риск — тысячи одинаковых SNI («yahoo») на одном хостинге: они видны в censys, и за них режут хостинг целиком.
- Лучшие варианты: сосед по ASN через RealiTLScanner или selfsteal. Держать 2–3 target: сосед может упасть или сменить сертификат.
- .ru на зарубежном exit **предупреждать**, а не жёстко запрещать.
- Источники: [ntc 24119](https://ntc.party/t/есть-ли-какие-то-известные-специфические-проблемы-для-vless-через-мегафон/24119), [ntc 13887](https://ntc.party/t/realityscanner-показывает-странную-картину/13887), [ntc 4625/1145](https://ntc.party/t/4625/1145), [ntc 16061/567](https://ntc.party/t/16061/567), [Habr 1009542](https://habr.com/ru/articles/1009542/), [REALITY docs](https://xtls.github.io/en/config/transports/reality.html).

**l-server-ru-block 🟡** — см. §5 и §6.1.

### 2.6 Эксплуатация и мониторинг

**s-hide-panel. Скрытие панели 🟡**
- Это дешёвая практика из документации: Remnawave (secret login route), eGames (cookie, 404, selfsteal).
- Что коммерческие сервисы так делают, не подтверждено. Kakadu процитирован неверно: у него фасадные сайты компании, а не selfsteal-цель.
- Скрытие панели защищает от поиска и брутфорса, а **не** от active probing: от него защищает сам REALITY, который форвардит неаутентифицированных на target.
- Selfsteal убирает несоответствие SNI и IP/ASN. Цена: связка домен↔IP видна в CT-логах, шаблонные заглушки распознаются, при бане домена падают и прокси, и подписка.
- Подписка должна быть публичной. Для одного владельца проще всего панель на 127.0.0.1 + SSH-туннель.
- Источники: [remnawave-reverse-proxy](https://github.com/eGamesAPI/remnawave-reverse-proxy/blob/main/README-RU.md), [docs.rw panel-security](https://docs.rw/docs/install/panel-security/), [XTLS/REALITY](https://github.com/XTLS/REALITY).

**s-rkn-buys-subs. Контрольные закупки 🟡**
- Анонимный разработчик Amnezia в Meduza 03.08: «контрольные закупки они уже автоматизировали». Затем баны IP и подсетей. По словам Климарева, банят все префиксы десятков AS.
- Источники заинтересованные, независимых измерений нет. «Небольшие закрытые сервисы живут дольше» — прямых данных нет.
- Конфиги дают и **отпечаток протокола**, который бьёт по всем, в том числе по self-hosted. Поэтому уникальные параметры AWG важнее закрытости.
- Источники: [Meduza 03.08](https://meduza.io/feature/2026/08/03/teoreticheski-oni-mogut-zablokirovat-lyuboy-servis-tselikom), [НГЕ 05.08](https://novayagazeta.eu/articles/2026/08/05/rkn-provel-novuiu-masshtabnuiu-volnu-blokirovok-vpn-servisov-news).

**s-ru-probing. Мониторинг из РФ 🟡**
- Liberty: серверы в РФ со скриптами, бот NetWatch. Paper VPN: волонтёры в Мск, СПб, Екб, Нск. Amnezia: Downdetector. Всё это самоотчёты.
- Методика Теплицы: 3 точки в РФ, TCP плюс полный VLESS-handshake, передача 2–3 мин.
- Habr 1053250 нерелевантен (Китай).
- Инструменты: `kutovoys/xray-checker` (только VLESS/VMess/Trojan/SS), `ku78/tspu-checker`.
- Алерты слать через туннель: Telegram из РФ ненадёжен.
- Источники: [Meduza 03.04](https://meduza.io/feature/2026/04/03/mozhno-li-deystvitelno-zablokirovat-vse-vpn), [Теплица](https://te-st.org/2026/09/30/vlessmore/), [xray-checker](https://github.com/kutovoys/xray-checker), [tspu-checker](https://github.com/ku78/tspu-checker).

**s-ip-rotator. Авторотация IP 🟡**
- Проект `UIbodulloev/vpn-ip-rotator`: 2 недели, 1★, только UpCloud + Cloudflare DNS, отчётов об эксплуатации нет.
- Полезное ядро для самодела: адрес по домену с TTL 60 и хук смены IP, который обновляет DNS и подписку.
- Автодетект не помогает против бана подсети, отпечатка протокола и БС.
- Клиенты AWG/WG не перерезолвят домен без переподключения.
- Источник: [vpn-ip-rotator](https://github.com/UIbodulloev/vpn-ip-rotator).

**s-panel-stack. Remnawave против 3x-ui 🟡**
- Remnawave + бот Bedolaga (только Remnawave 3.0+) заметны у Telegram-сервисов, но слово «де-факто» ничем не измерено.
- Тезис «3x-ui на 1–2 сервера» устарел:
  - Habr 1040846: 12 машин на 3x-ui;
  - в v3.7.0–v3.9.0 есть мульти-нода, HWID-лимит, балансер в JSON-подписке, выдача по UA.
- Риски Remnawave для малого сетапа: отдельный хост под панель, Docker/Postgres/Redis, лицензия AGPL.
- **v3.9.0 вышла 03.10.2026, ей один день. Запасной вариант — v3.8.5.**
- Источники: [3x-ui releases](https://github.com/MHSanaei/3x-ui/releases), [Bedolaga](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot).

**s-three-signal-model. Модель «трёх сигналов» 🟡**
- Модель «зарубежный ASN + fingerprint + больше 3 параллельных TLS» — реконструкция по одному источнику (Habr 1047442).
- ntc 16061/1080 (06.06.2026): триггер — залп соединений. Режут даже настоящий Chromium при 10 быстрых соединениях, значит fingerprint вторичен. Фильтрация зависит от хостера: Fornex режут, NetGrid нет.
- Против измеренного порога по объёму mux/XHTTP не спасает: долгая сессия превысит 16 КБ ещё быстрее.
- Полезный рычаг — адрес входа (RU или малоизвестная подсеть) плюс Hy2 как резерв.
- Источники: [Habr 1047442](https://habr.com/ru/articles/1047442/), [ntc 16061/1080](https://ntc.party/t/16061/1080), [ntc 16061/1230](https://ntc.party/t/16061/1230).

---

## 3. Компрометаторы

### 3.1 Какие приложения палят VPN

| Приложение | Что делает | Метод | Дата / источник | Статус |
|---|---|---|---|---|
| **MAX** (Android) | Опрашивает echo (api.ipify.org, checkip.amazonaws.com, ifconfig.me), проверяет main.telegram.org и mmg.whatsapp.net. На api.oneme.ru уходят IP, тип сети, код оператора, VPN-флаг | Echo-IP + reachability, TRANSPORT_VPN | 04.03.2026, [Habr 1006394](https://habr.com/ru/articles/1006394/), [Хакер 06.03](https://xakep.ru/2026/03/06/max-reverse/) | 🟡. MAX объясняет запросы WebRTC и отрицает TG/WA. Связь с ТСПУ не доказана. VPN-флаг в payload на Habr не показан |
| **30 топ RU-приложений** | 22 из 30 детектят VPN, к 16.04 все 30. 19 шлют статус на бэкенд, включая все банки | TRANSPORT_VPN (17, включая MAX), tun0 (13), список VPN-приложений через `queryIntentServices` (7) | 04.2026, [RKS Global](https://rks.global/ru/research/vpn-detection/), [The Bell](https://thebell.io/22-iz-30-populyarnykh-rossiyskikh-prilozheniy-sledyat-za-tem-vklyuchen-li-u-vas-vpn), [Meduza 11.04](https://meduza.io/feature/2026/04/11/rossiyskie-prilozheniya-dlya-android-sledyat-za-vami-po-suti-eto-polnotsennye-shpionskie-programmy) | ❓, косвенно 🟡: 22/30, 13 по tun0, 7 сканируют — повторено в голосах по s-split-routing |
| **WB, Ozon, 2ГИС, RuStore, Самокат, MegaMarket** | Собирают список установленных VPN-клиентов | `queryIntentServices` | 04.2026, RKS Global, [iXBT](https://www.ixbt.com/news/2026/05/02/vpn-android-wildberries-ozon-2-rustore-vpn.html) | ❓, косвенно 🟡 |
| **Яндекс** | Блокирует по серверному флагу | Сервер получает VPN-флаг | [kod.ru](https://kod.ru/vse-30-ru-prilojeniy-sledyat-za-vpn) | 🟡 (голос по s-split-routing) |
| **VK, Т-Банк, Сбер, Госуслуги, Avito** | IceCandidate — основной HIGH-признак | WebRTC/STUN: перечисление интерфейсов (виден tun) и server-reflexive кандидат с IP exit | 09.2026, [Habr 1081580](https://habr.com/ru/articles/1081580/), [Habr 1006950](https://habr.com/ru/news/1006950/) | ❓ |
| **МТС, Ozon, WB, банки** | Реакция на детект: предупреждение или частичная работа (МТС: «данные могут не отображаться», у Ozon не грузятся картинки и checkout). Банки по части отчётов не пускали | — | 16.04.2026, [ComNews](https://www.comnews.ru/content/244810/2026-04-16/2026-w16/1009/rossiyskie-servisy-chastichno-ogranichivayut-rabotu-pri-vklyuchennom-vpn), [Meduza 16.04](https://meduza.io/feature/2026/04/16/krupneyshie-rossiyskie-servisy-zakryvayut-dostup-polzovatelyam-s-vklyuchennym-vpn) | ❓ |
| **Ozon, WB, Яндекс, ритейл** | 27–28.04 после падения продаж снова пустили пользователей с VPN | — | [MT 28.04](https://ru.themoscowtimes.com/2026/04/28/marketpleisi-stali-puskat-rossiyan-s-vpn-posle-padeniya-prodazh-a193979), [Ведомости](https://www.vedomosti.ru/technology/news/2026/04/27/1193249-mintsifri-obyasnilo) | ❓ |
| **Xray/sing-box-клиенты на Android** (v2rayNG и др.) | SOCKS5/HTTP на 127.0.0.1 без пароля. Любое приложение, даже исключённое или в Shelter/Knox (loopback общий), узнаёт через него IP exit | Сканирование localhost + echo | 07.04.2026, [Habr 1020080](https://habr.com/ru/articles/1020080/), [PoC](https://github.com/runetfreedom/per-app-split-bypass-poc), [v2rayNG #5467](https://github.com/2dust/v2rayNG/issues/5467), [Хакер](https://xakep.ru/2026/04/07/vless-bug/) | ❓, косвенно 🟡: голоса по l-server-ru-block и l-entry-exit-split опираются на этот кейс |
| **Happ** | Открывал xray gRPC API: из него выгружались UUID, ключи, адрес входа, SNI | Локальный API | 04.2026, [kod.ru](https://kod.ru/vpn-happ-problems) | ❓, косвенно 🟡: «Happ API отдаёт и входной IP» в голосе по l-entry-exit-split |
| **Happ (с Provider ID)** | Раз в сутки шлёт HWID, ОС и хэш домена подписки на check.happ-proxy.com | Телеметрия клиента | [docs.happ-proxy.com](https://docs.happ-proxy.com/getting-started/provider-id.md) | 🟡 (голос по s-happ-headers) |
| **3x-ui v3.9.0, JSON-подписка** | Кладёт клиенту `{"listen":"127.0.0.1","port":10808,"protocol":"socks","settings":{"auth":"noauth"}}` | Тот же вектор открытого SOCKS, но со стороны сервера | [default.json](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/sub/default.json) | 📄 |
| **Детекторы (RKNHardering)** | Сканируют 1024–65535 на 127.0.0.1/::1 (200 параллельных, SOCKS5-greeting, HTTP CONNECT), Xray gRPC, Clash API. Один случайный порт не спасает, нужна авторизация | Скан loopback | [RKNHardering](https://github.com/xtclovver/RKNHardering/blob/main/docs/README.en.md) | ❓ |
| **Android 16 (ОС)** | Обычное приложение шлёт UDP мимо VPN через `registerQuicConnectionClosePayload`, даже при Always-on. Google ответил Won't Fix. Выдаёт факт VPN и реальный IP, а не сервер | Баг ОС | 05.2026, [Forbes](https://www.forbes.com/sites/daveywinder/2026/05/15/wont-fix-all-vpn-apps-affected-as-google-android-16-leaks-info/), [lowlevel.fun](https://lowlevel.fun/posts/tiny-udp-cannon-android-vpn-bypass/) | ❓ |
| **iOS (все)** | Детект слабее: sandbox не даёт сканировать loopback. Остаются эвристики (`__SCOPED__`, `getifaddrs`) и echo через туннель | Эвристики | [tarkalabs](https://tarkalabs.com/blogs/vpn-detection-guide-ios-android/) | ❓, косвенно 🟡: Минцифры признаёт, что на iOS детект «проблематичен» |

### 3.2 Системные признаки, видимые без разрешений (Android) ❓

- TRANSPORT_VPN/IS_VPN; интерфейсы tun, wg, ppp; маршруты (`/proc/net/route`); loopback-DNS; MTU<1500.
- Протокол значения не имеет: VLESS, Hy2 и AWG выглядят одинаково.
- Per-app bypass убирает проверки по IP и гео, но не по интерфейсу.
- Источники: [RKNHardering](https://github.com/xtclovver/RKNHardering/blob/main/docs/README.en.md), [Habr 1081580](https://habr.com/ru/articles/1081580/).

**Классификация IP ❓.** Базы оценивают три поля: страна, hosting, proxy.
- Зарубежный VPS даёт три красных флага.
- RU-хостинг даёт country=RU, но hosting-метку в 63–91% случаев.
- Чистым выглядит только домашний IP.
- WARP — это AS13335 Cloudflare, тоже признак VPN/прокси.
- Источник: [Habr 1067230](https://habr.com/ru/articles/1067230/).

**IPv6-утечка при сплите ❓.** Замер августа 2026: ни одна крупная GeoIP-база не относила российские IPv6-диапазоны к RU. Поэтому `geoip:ru` не ловит IPv6 российских сервисов, они уходят в туннель и видят exit. Источник: [Habr 1067230](https://habr.com/ru/articles/1067230/).
- 📄 Однако runetfreedom `ru.txt` содержит 12 150 IPv6-строк из 25 094. Для ipset и Xray с этим набором вопрос частично снят, но качество покрытия не проверено.

### 3.3 Регуляторика

| Мера | Суть | Статус |
|---|---|---|
| Методичка Минцифры (04.2026) | Три этапа: 1) сверка IP с RU-диапазонами и реестром РКН; 2) устройство: ConnectivityManager, tun0, порты SOCKS 1080/9000/5555, HTTP 3128/8080, Tor 9050; 3) десктоп. Признаёт, что iOS, роутер, VM и split детектировать трудно. Данные о новых VPN передавать в РКН | ❓, косвенно 🟡: упомянута в голосах по s-split-routing и s-entry-cascade. [Meduza](https://meduza.io/news/2026/04/06/mintsifry-razoslalo-rossiyskim-kompaniyam-metodichku-po-poisku-vpn-na-ustroystvah-polzovateley-v-vedomstve-zayavili-chto-vyyavlenie-takih-servisov-v-ayfonah-problematichno), [SecurityLab](https://www.securitylab.ru/news/571257.php) |
| Требование к 20+ платформам | Конец марта 2026: к 15.04 ограничить доступ пользователям с VPN, иначе отзыв IT-аккредитации. Исполнение мягкое | ❓ [Фонтанка](https://www.fontanka.ru/2026/04/02/76345065/), [CNews](https://www.cnews.ru/news/top/2026-04-06_rossijskim_it-kompaniyam) |
| Карта IP абонентов | 216-ФЗ, приказ РКН №51, в силе с 12.04.2025. Передаются IPv4/IPv6, муниципалитет, ID ТСПУ. В марте 2026 уведомления получили 1359 операторов, в мае оштрафованы 85 | ❓ [Теплица](https://te-st.org/2026/05/27/collectip/), [MT](https://ru.themoscowtimes.com/2026/05/26/roskomnadzor-nachal-trebovat-u-operatorov-svyazi-ip-adresa-rossiyan-dlya-blokirovok-vpn-a196222) |
| 210-ФЗ «Антифрод 2.0» | Хостерам запрещено давать мощности под VPN по ст. 15.8 | 🟡 (s-hosting-law) |
| Проект «Антифрод 3.0» | Реестр, KYC, годовой бан у всех RU-хостеров, с 01.03.2028 | 🟡 (s-hosting-law) |
| Мониторинг хостеров (08.2026) | Если VPN-инфраструктура регулярно видна на IP неделю, хостер получает запрос. Идентификация клиента через Госуслуги, биометрию или паспорт. IP белого списка вынести в отдельные подсети | ❓, косвенно 🟡 (голос по s-entry-cascade: «31.08 Минцифры обязало…»). [MT 04.08](https://ru.themoscowtimes.com/2026/08/04/desyatki-vpn-servisov-perestali-rabotat-v-rossii-posle-novih-trebovanii-mintsifri-k-provaideram-a202667), [hightech.fm](https://hightech.fm/2026/08/31/fsb-vpn) |
| Белый список корпоративных VPN | ~57–75 тыс. IP исключены из фильтрации ТСПУ | ❓ [cisoclub](https://cisoclub.ru/roskomnadzor-rasshiril-spisok-razreshjonnyh-korporativnyh-vpn-do-57-tysjach-adresov/) |
| Плата за международный трафик | Сверх 15 ГБ/мес, отложено. 23.09 идея вернулась для 5G. Вымпелком: VPN не отличить от прочего зарубежного трафика | ❓ [Meduza 23.09](https://meduza.io/news/2026/09/23/bi-bi-si-v-pravitelstve-vernulis-k-idee-platy-za-mezhdunarodnyy-trafik-no-teper-v-seti-5g-v-rossii-ona-dostupna-tolko-vladeltsam-androidov) |
| Ответственность пользователя | Пользоваться VPN законно. Штраф 3–5 тыс. ₽ за умышленный поиск экстремистских материалов, VPN — отягчающее обстоятельство. Реклама VPN: 50–80 тыс. ₽ для физлиц, 200–500 тыс. ₽ для юрлиц | ❓ [Ведомости](https://www.vedomosti.ru/press_releases/2025/08/27/v-rossii-vveli-otvetstvennost-za-poisk-ekstremistskih-materialov-i-reklamu-vpn) |

**Волны IP-банов ❓.** 04.08.2026, с 07.09 и после выборов (пик 25.09). Под них попали и self-hosted VPS. Из двух одинаковых серверов в одной /24 забанили один. Прямой связи «приложение сообщило IP → бан» публично **не доказано**. Источник: [ntc 25528](https://ntc.party/t/%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-ip-%D0%B0%D0%B4%D1%80%D0%B5%D1%81%D0%BE%D0%B2-vpn-%D1%81%D0%B5%D1%80%D0%B2%D0%B5%D1%80%D0%BE%D0%B2-%D1%80%D0%BA%D0%BD-04082026/25528).

### 3.4 Технические методы детекта на стороне сети

| Метод | Что бьёт | Статус |
|---|---|---|
| «Заморозка 16–20 КБ» (~25 пакетов) к зарубежным ДЦ-ASN, без RST | TCP+TLS к Hetzner, DO, OVH, Contabo, Amazon, Cloudflare | 🟡 s-asn-choice, s-three-signal |
| Залп параллельных хендшейков | Клиенты, открывающие много TLS разом | 🟡 гипотеза (s-xhttp-xmux, s-three-signal) |
| Сверка «важного» RU-SNI с CIDR (МегаФон) | Reality с RU-SNI на чужом IP | 🟡 s-sni-choice |
| QUIC по SNI из блоклиста; отчёты о полном отказе QUIC на T2 | Hy2 | 🟡 s-hy2 |
| Белые списки CIDR (+SNI у МегаФона) на мобильном | Всё, кроме белых IP | 🟡 s-whitelist-mechanics |
| Контрольные закупки → конфиги → бан IP и /24 → отпечаток протокола | Коммерческие VPN; по отпечатку — все | 🟡 s-rkn-buys-subs |
| Статистика потока AWG 2.0 (размеры, интервалы, keepalive) | AWG с одинаковыми параметрами | 🟡 гипотеза Amnezia (s-awg) |
| Поиск кластеров одинаковых SNI в censys | Массовые шаблонные Reality-установки | 🟡 s-sni-choice (голос) |
| DPI в RU-ДЦ | Плечо каскада RU→зарубеж | 🟡 s-entry-caveat |
| Бан подсетей и префиксов AS хостеров | Всех, кто хостится рядом | 🟡 s-fast-redeploy |

---

## 4. Контрмеры: что закрывает сервер, а что нет

| Угроза | Серверная мера | Закрывает? | Клиентская мера |
|---|---|---|---|
| RU-сервис через туннель видит IP exit | `geoip:ru` + `category-ru` + `tld-ru` → `blocked`/`warp` на exit (Xray, Hy2 ACL, AWG ipset) | ✅ для RU-назначений | Сплит «РФ напрямую» |
| Echo-сервисы (ipify и т.п.) через туннель | `category-ip-geo-detect` → `warp`/`blocked` (📄 165 доменов в runetfreedom) | ✅ для доменов из списка. Echo вне списка — нет | Per-app исключения |
| Открытый локальный SOCKS у клиента | `subHappLocalProxyAuth=auto` (только Happ, 📄). JSON-подписку не включать по умолчанию | 🟡 частично | Включить auth, обновить клиент, Amnezia/WG без моста tun2socks |
| Утёкший exit-IP банят | Второй IP через `sendThrough` / WARP / второй VPS | 🟡 спасает от точечного бана, не от /24 | — |
| RU-приложения видят зарубежный IP | RU-вход + гео-сплит на входе (RU и echo → `direct` со входа) | ✅ IP становится российским, но это RU-хостинг с hosting-флагом | Сплит |
| TRANSPORT_VPN, tun0, MTU, маршруты | — | ❌ | Роутер, второй телефон, Island/Shelter (прячет VpnService, не loopback), Anubis (Shizuku) замораживает RU-приложения |
| Список установленных VPN-клиентов | — | ❌ | Рабочий профиль, другое устройство |
| WebRTC ICE (интерфейсы) | — | ❌. Server-reflexive через туннель частично прикрывает блок STUN-назначений, но это не проверено | — |
| `/proc/net/route`, Android 16 UDP | — | ❌ | GrapheneOS, роутер |
| Контрольные закупки / утечка конфига | Не раздавать публично; подписка на случайном пути; внешних держать на отдельной ноде | ✅ | Не пересылать подписку |
| Отпечаток AWG | Уникальные Jc/Jmin/Jmax, S1–S4, H1–H4, I1 на установку | ✅ | Клиент ≥5.0.1.5 для 3.x |
| Active probing / сканирование панели | REALITY (уже есть); панель на 127.0.0.1; Hy2 masquerade | ✅ | — |
| Заморозка 16 КБ | Хостер вне засвеченных ASN; RU-вход; Hy2 как UDP-резерв | 🟡 | Смена транспорта |
| БС на мобильном | RU-вход на **белом** IP | 🟡 только при белом IP | — |

**Главный вывод.** Сервер закрывает **утечку адреса сервера** и **отпечаток**, но не **факт VPN на устройстве**. Последнее решается только на клиенте или роутере, и в CREDENTIALS нужно честно об этом написать.

---

## 5. Что заложить в инсталлер

Ниже — почищенный черновик конфига. Проверен 2026-10-04 по исходникам 3x-ui тега `v3.9.0`, документации Xray и Hysteria2 и по самим файлам runetfreedom: .dat скачаны, теги разобраны. Знаком ❓ помечено то, что не удалось подтвердить. Значения `${...}` подставляет инсталлер.

### 5.0 Проверенные факты, на которые опирается конфиг (📄)

- **3x-ui v3.9.0** собран с Xray **v26.9.30** (`release.yml`, строка 118). Тарбол уже содержит `bin/geoip_RU.dat` и `bin/geosite_RU.dat`, это снимок runetfreedom на момент сборки.
  - Автообновления geo в 3x-ui нет: в `internal/web/job/*` такой задачи нет. Обновить можно только через меню `x-ui` → `update_geofiles "RU"`.
- **Шаблон Xray по умолчанию** лежит в `internal/web/service/config.json`.
  - Там уже есть `freedom.finalRules` с block `geoip:private`, а routing блокирует `geoip:private` и `bittorrent`.
  - Шаблон хранится в настройке `xrayTemplateConfig`, через API это `POST /panel/xray/update` с form-полем `xraySetting`.
- **Встроенный WARP 3x-ui:** `POST /panel/xray/warp/{reg|config|changeIp|license|interval}`.
  - Outbound получает tag `warp`, протокол `wireguard`, `noKernelTun: true`, `sockopt.domainStrategy`/`targetStrategy: "ForceIPv4v6"` (смена формата с 26.9.30, issue #5205).
  - Плановая смена IP WARP — `warp_ip_job.go`, интервал в днях.
- **Теги runetfreedom:**
  - geoip: `ru`, `private`, `ru-blocked`, `ru-whitelist`, `yandex`, `telegram`;
  - geosite: `category-ru` (1103), `tld-ru`, `category-gov-ru`, `category-bank-ru`, `category-ip-geo-detect` (165: `ipify.org`, `ifconfig.me`, `checkip.amazonaws.com`, `ip.mail.ru`, `2ip.ua`, `ip-api.com` …), `ru-blocked`, `category-ads-all`, `win-spy`, `private`.
  - ⚠️ **`category-ru` не включает зону `.ru` целиком.** `max.ru` и `oneme.ru` ловит только `tld-ru`.
- **CIDR для ipset:** `runetfreedom/russia-blocked-geoip/release/text/ru.txt`, 25 094 строки, из них 12 150 IPv6. Рядом лежит `ru-whitelist.txt`.
- **Hysteria app/v2.12.3** (16.09.2026) исправляет перехват исходящего UDP правилами port hopping.
  - Если не задать `acl.geoip`/`acl.geosite`, Hysteria при старте сама качает файлы Loyalsoldier, где нужных тегов нет. Пути задавать явно.
- **JSON-подписка** (`internal/sub/default.json`) кладёт клиенту SOCKS `127.0.0.1:10808` с `auth: noauth`. Это тот самый вектор утечки (§3.1).

### 5.1 Маршрутизация Xray: новая фаза `07-xray-routing.sh`

Полная замена `xrayTemplateConfig`. `${RU_EGRESS}` принимает значение `warp` или `blocked`. `${ECHO_EGRESS}` = `warp` при включённом WARP, иначе `blocked`. Значение `blocked` по умолчанию без WARP — мой инженерный вывод: легитимным пользователям IP-эхо почти не нужно, а `direct` означает осознанно принятую утечку.

```json
{
  "log": { "access": "none", "dnsLog": false, "error": "", "loglevel": "warning", "maskAddress": "" },
  "api": { "tag": "api", "services": ["HandlerService","LoggerService","StatsService","RoutingService"] },
  "metrics": { "listen": "127.0.0.1:11111", "tag": "metrics_out" },
  "inbounds": [
    { "tag": "api", "listen": "127.0.0.1", "port": 62789, "protocol": "tunnel",
      "settings": { "rewriteAddress": "127.0.0.1" } },
    { "tag": "hy2-egress", "listen": "127.0.0.1", "port": 40000, "protocol": "socks",
      "settings": { "auth": "password", "udp": true,
        "accounts": [ { "user": "hy2", "pass": "${HY2_EGRESS_PASS}" } ] },
      "sniffing": { "enabled": true, "destOverride": ["http","tls","quic"], "routeOnly": true } }
  ],
  "dns": {
    "servers": [ "https+local://1.1.1.1/dns-query", "https+local://8.8.8.8/dns-query" ],
    "queryStrategy": "UseIPv4"
  },
  "outbounds": [
    { "tag": "direct", "protocol": "freedom",
      "settings": { "finalRules": [ { "action": "block", "ip": ["geoip:private"] }, { "action": "allow" } ] } },
    { "tag": "blocked", "protocol": "blackhole", "settings": {} },
    { "tag": "warp", "protocol": "wireguard",
      "streamSettings": { "sockopt": { "domainStrategy": "ForceIPv4v6" } },
      "targetStrategy": "ForceIPv4v6",
      "settings": { "mtu": 1280, "noKernelTun": true,
        "secretKey": "${WARP_PRIV}", "address": ["${WARP_V4}/32","${WARP_V6}/128"],
        "reserved": [${WARP_RESERVED}],
        "peers": [ { "publicKey": "${WARP_PEER_PUB}", "endpoint": "engage.cloudflareclient.com:2408" } ] } }
  ],
  "policy": { "levels": { "0": { "statsUserDownlink": true, "statsUserUplink": true } },
    "system": { "statsInboundDownlink": true, "statsInboundUplink": true } },
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      { "type": "field", "inboundTag": ["api"], "outboundTag": "api" },
      { "type": "field", "ruleTag": "private", "ip": ["geoip:private"], "outboundTag": "blocked" },
      { "type": "field", "ruleTag": "bt", "protocol": ["bittorrent"], "outboundTag": "blocked" },
      { "type": "field", "ruleTag": "ru-blocked-exempt",
        "domain": ["ext:geosite_RU.dat:ru-blocked"], "outboundTag": "direct" },
      { "type": "field", "ruleTag": "ip-echo",
        "domain": ["ext:geosite_RU.dat:category-ip-geo-detect"], "outboundTag": "${ECHO_EGRESS}" },
      { "type": "field", "ruleTag": "ru-domains",
        "domain": ["ext:geosite_RU.dat:category-ru","ext:geosite_RU.dat:tld-ru","ext:geosite_RU.dat:category-gov-ru"],
        "outboundTag": "${RU_EGRESS}" },
      { "type": "field", "ruleTag": "ru-ip", "ip": ["ext:geoip_RU.dat:ru"], "outboundTag": "${RU_EGRESS}" }
    ]
  },
  "stats": {}
}
```

Если WARP выключен, outbound `warp` из шаблона убрать целиком: Xray не стартует с пустыми ключами.

**Почему правила стоят в таком порядке**
1. `ru-blocked → direct` идёт первым. Иначе заблокированные РКН `.ru`-домены, которые пользователь сознательно открывает через VPN, ушли бы в `block`/`warp` по `tld-ru`. Из РФ такой ресурс недоступен, поэтому слить IP регулятору он вряд ли может.
2. `ip-echo` стоит выше `ru-*`, потому что `ip.mail.ru` попадает и в `tld-ru`. При `warp` приложение увидит IP Cloudflare и не сломается.
3. `IPIfNonMatch`: если домен не совпал ни с одним правилом, он резолвится и сверяется с `geoip:ru`. Так ловится RU-сервис на `.com` с RU-IP.

**Sniffing inbound** (дефект [репо] в `04-vless-reality.sh`). Сейчас там `["http","tls","quic","fakedns"]`, `routeOnly:false`. Нужно:
```json
{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":true}
```
`fakedns` на сервере без настроенного fakedns бесполезен. `routeOnly:true` берёт домен только для маршрутизации и не переписывает назначение.

**Применение**
- Инсталлер собирает JSON через `jq` и логинится на `http://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}/login`. Затем `POST .../panel/xray/update -F xraySetting=@tmpl.json`.
- API предпочтительнее, потому что 3x-ui сам делает `CheckXrayConfig` и `UnwrapXrayTemplateConfig`. Прямой `UPDATE settings` в БД — только запасной путь, это тот же класс дефекта [репо], что в фазе 04.
- Перед применением прогнать `xray -test -c`.
- Smoke-тест: `vk.com` → `${RU_EGRESS}`, `ifconfig.me` → `${ECHO_EGRESS}`, `rutracker.org` → `direct`. ❓ Параметры эндпоинта `POST /panel/xray/routeTest` не проверены.
- Путь к xray брать по `$ARCH` из `03-3xui.sh`, а не `xray-linux-amd64` (дефект [репо] в `04`, `XRAY_BIN`).

**Geo-файлы: runetfreedom.** 3x-ui уже их поставляет, обновляются каждые 6 ч, все нужные теги на месте. roscomvpn отдавать **клиенту** в routing-заголовке (§5.5), а на сервере держать один набор.

Файл `scripts/lib-geo.sh` + systemd timer, ставится в фазе `07`:

```bash
# /usr/local/sbin/vpnzoo-geo-update
set -euo pipefail
SRC=https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download
BIN=/usr/local/x-ui/bin; HY=/etc/hysteria/geo; mkdir -p "$HY"
changed=0
for f in geoip geosite; do
  tmp=$(mktemp); sum=$(mktemp)
  curl -fsSL --max-time 300 -o "$tmp" "$SRC/$f.dat"
  curl -fsSL -o "$sum" "$SRC/$f.dat.sha256sum"
  [ "$(sha256sum "$tmp" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "$sum")" ] || { echo "$f: sha mismatch"; exit 1; }
  [ -s "$tmp" ] || exit 1
  if ! cmp -s "$tmp" "$BIN/${f}_RU.dat"; then
    install -m644 "$tmp" "$BIN/${f}_RU.dat"; install -m644 "$tmp" "$HY/$f.dat"; changed=1
  fi
  rm -f "$tmp" "$sum"
done
/usr/local/sbin/vpnzoo-ipset-ru   # §5.4
[ $changed = 1 ] && systemctl restart x-ui hysteria-server
```

```ini
# /etc/systemd/system/vpnzoo-geo.timer
[Timer]
OnCalendar=*-*-* 01:30:00 UTC
RandomizedDelaySec=20m
Persistent=true
```

- ❓ `*.dat.sha256sum` проверен только в `raw/release/`, а не как asset `releases/latest/download/`. Если asset'а нет, брать файл из `raw.githubusercontent.com/.../release/`.
- Рестарт x-ui рвёт соединения, поэтому обновлять раз в сутки ночью.

**Компромиссы**

| Решение | Плюс | Цена или поломка |
|---|---|---|
| `RU_EGRESS=blocked` | IP exit не виден RU-сервисам вообще | У клиента без сплита не открываются RU-сайты («VPN сломал Госуслуги») |
| `RU_EGRESS=warp` | RU-сайты работают у любого клиента | IP Cloudflare AS13335 тоже признак VPN. Часть банков и маркетплейсов режет ДЦ-IP или требует капчу. Внешняя зависимость |
| `ip-echo → warp` | Exit-IP не утекает через ipify и подобные | Если WARP упал, эти домены не отвечают. WebRTC-логика приложений может ошибаться |
| `IPIfNonMatch` | Ловит RU-IP за не-RU доменами | DNS-запрос на каждое несовпавшее соединение. При недоступном DoH соединения висят до таймаута |
| geosite_RU.dat ≈ 74 МБ | Полные списки | ❓ ОЗУ и время старта на VPS с 1 ГБ не измерены |
| `bittorrent → blocked` | Меньше abuse-жалоб хостеру | Работает только при sniffing. Шифрованный BT (MSE) не ловится |

**Не закрывает:** открытый локальный SOCKS в связке с echo вне списка, `/proc/net/route`, RU-сервисы на зарубежных IP с не-RU доменом. Это страховка, а не гарантия.

### 5.2 Cloudflare WARP: новая фаза `08-warp.sh`, opt-in `WARP_ENABLE=1`

**Вариант A (по умолчанию): встроенный WARP 3x-ui, только для Xray.**
```bash
kp="$("$XUI_DIR/bin/xray-linux-$ARCH" wg)"          # PrivateKey / PublicKey
api POST /panel/xray/warp/reg -F privateKey=... -F publicKey=...
cfg="$(api POST /panel/xray/warp/config)"           # interface.addresses, peers[0], client_id
# reserved = байты base64(client_id), endpoint = peers[0].endpoint.host
api POST /panel/xray/warp/interval -F interval=7     # смена IP WARP раз в 7 дней
```
После этого подставить значения в outbound `warp` из §5.1 и повторить `update`.

**Вариант B: ядерный `warp0` через wgcf, общий для Xray, Hy2 и AWG.**
```bash
wgcf register --accept-tos && wgcf generate
sed -e 's/^\[Interface\]/[Interface]\nTable = off/' -e '/^DNS/d' wgcf-profile.conf > /etc/wireguard/warp0.conf
systemctl enable --now wg-quick@warp0
```
- Xray: `{"tag":"warp","protocol":"freedom","streamSettings":{"sockopt":{"interface":"warp0"}}}`.
- Hy2: `direct` с `bindDevice: warp0`.
- AWG: fwmark (§5.4).
- ❓ Не проверено вживую, что `SO_BINDTODEVICE` на интерфейс с `Table = off` маршрутизирует. Проверка: `curl --interface warp0 https://www.cloudflare.com/cdn-cgi/trace` должен дать `warp=on`.

**Оговорки**
- WARP не делает трафик «российским». Это средство против утечки exit-IP, а не замена сплиту или RU-входу.
- Использование WARP на сервере неофициальное: регистрацию могут закрыть.
- IPv6: если v6 на хосте недонастроен, рукопожатие с WARP молча уходит в чёрную дыру (3x-ui #5205). Отсюда `ForceIPv4v6`.
- `noKernelTun: true` надёжнее на VPS, но тратит больше CPU.
- У Xray нет автофолбэка одного outbound. ❓ Можно обернуть `warp` в balancer `{selector:["warp"], fallbackTag:"blocked"}` с observatory, но связка observatory + wireguard не проверена.

### 5.3 Hysteria2: `05-hysteria2.sh`

```yaml
listen: :${HY2_PORT}          # или :20000-50000 для port hopping (только ≥ app/v2.12.3)
tls: { cert: /etc/hysteria/cert.pem, key: /etc/hysteria/key.pem }
auth: { type: password, password: ${HY2_PASSWORD} }
masquerade: { type: proxy, proxy: { url: https://${HY2_MASQ_HOST}/, rewriteHost: true } }
ignoreClientBandwidth: true

sniff:
  enable: true
  timeout: 2s
  rewriteDomain: false
  tcpPorts: all
  udpPorts: all

outbounds:
  - name: direct          # первый в списке = default
    type: direct
    direct: { mode: auto }
  - name: warp
    type: socks5          # вариант A: через Xray inbound hy2-egress (§5.1)
    socks5: { addr: 127.0.0.1:40000, username: hy2, password: ${HY2_EGRESS_PASS} }
  # вариант B: { name: warp, type: direct, direct: { bindDevice: warp0 } }

acl:
  geoip:   /etc/hysteria/geo/geoip.dat     # runetfreedom, обновляет vpnzoo-geo-update
  geosite: /etc/hysteria/geo/geosite.dat
  inline:
    - reject(geoip:private)
    - direct(geosite:ru-blocked)
    - ${ECHO_EGRESS_HY}(geosite:category-ip-geo-detect)
    - ${RU_EGRESS_HY}(geosite:category-ru)
    - ${RU_EGRESS_HY}(suffix:ru)
    - ${RU_EGRESS_HY}(suffix:su)
    - ${RU_EGRESS_HY}(suffix:xn--p1ai)
    - ${RU_EGRESS_HY}(geoip:ru)
    - direct(all)
```

- `RU_EGRESS_HY` и `ECHO_EGRESS_HY` принимают значение `reject` или `warp`. Без WARP outbound `warp` из списка убрать.
- Синтаксис `outbound(address[, proto/port])`, матчеры `geoip:`/`geosite:`/`suffix:`/`all` — по документации v2.hysteria.network.
- **Пиннинг:** сейчас `bash <(curl https://get.hy2.sh/)` без версии, нужно `--version app/v2.12.3`. ❓ Флаг `--version` не перепроверен.
- Повторный запуск фазы бинарник не обновляет: проверка `[ -x $HY_BIN ]` пропускает шаг. Для обновления нужна отдельная команда (дефект [репо]).
- Компромиссы:
  - ACL резолвит доменный запрос, то есть DNS на каждое соединение. Задать `resolver:` DoH.
  - В варианте A Hy2 зависит от x-ui: при его рестарте отваливаются только RU-трафик и echo.
  - Без `sniff` TUN-клиент шлёт голый IP, и `geosite:` не срабатывает.
  - BitTorrent Hy2 не распознаёт.
  - ❓ ОЗУ при загрузке 74 МБ geosite не измерено.
- Транспорт:
  - реальный сертификат + masquerade proxy;
  - port hopping — опция, ссылка `hy2://…@host:20000-50000` (официальный URI) и `mport`/`mportHopInt` для Happ ❓;
  - Salamander по флагу на отдельном порту, он несовместим с masquerade.

### 5.4 AmneziaWG: `06-amneziawg.sh`

```bash
# /usr/local/sbin/vpnzoo-ipset-ru — атомарная перезаливка
set -euo pipefail
URL=https://raw.githubusercontent.com/runetfreedom/russia-blocked-geoip/release/text/ru.txt
t=$(mktemp); curl -fsSL --max-time 120 -o "$t" "$URL"
[ "$(wc -l <"$t")" -gt 10000 ] || exit 1                   # защита от пустого/битого файла
{
  echo "create vz-ru4-new hash:net family inet  maxelem 131072 -exist"
  echo "create vz-ru6-new hash:net family inet6 maxelem 131072 -exist"
  echo "flush vz-ru4-new"; echo "flush vz-ru6-new"
  grep -v ':' "$t" | sed 's/^/add vz-ru4-new /'
  grep    ':' "$t" | sed 's/^/add vz-ru6-new /'
} | ipset restore
ipset create vz-ru4 hash:net family inet  maxelem 131072 -exist
ipset create vz-ru6 hash:net family inet6 maxelem 131072 -exist
ipset swap vz-ru4-new vz-ru4; ipset swap vz-ru6-new vz-ru6
ipset destroy vz-ru4-new; ipset destroy vz-ru6-new
```

**Режим `block`** (`awg0.conf`; `-I` ставит правило выше `FORWARD -i %i -j ACCEPT`):
```ini
PostUp   = iptables  -I FORWARD -i %i -m set --match-set vz-ru4 dst -j REJECT --reject-with icmp-admin-prohibited
PostUp   = ip6tables -I FORWARD -i %i -m set --match-set vz-ru6 dst -j REJECT --reject-with icmp6-adm-prohibited
PostDown = iptables  -D FORWARD -i %i -m set --match-set vz-ru4 dst -j REJECT --reject-with icmp-admin-prohibited
PostDown = ip6tables -D FORWARD -i %i -m set --match-set vz-ru6 dst -j REJECT --reject-with icmp6-adm-prohibited
```

**Режим `warp`** (нужен `warp0` из варианта B):
```ini
PostUp = iptables -t mangle -A PREROUTING -i %i -m set --match-set vz-ru4 dst -j MARK --set-mark 0x5255
PostUp = ip rule add fwmark 0x5255 lookup 5255 priority 5255
PostUp = ip route replace default dev warp0 table 5255
PostUp = iptables -t nat -A POSTROUTING -s ${AWG_NETWORK} -o warp0 -j MASQUERADE
PostUp = iptables -t mangle -A FORWARD -o warp0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
# PostDown — зеркально
```

**Уникальный профиль 3.x (s-awg):**
- генерировать на каждую установку Jc/Jmin/Jmax, S1–S4 (≥12 для HeaderProtectionKey), непересекающиеся диапазоны H1–H4 и I1;
- MTU 1280 — опцией;
- в CREDENTIALS указать, что нужен AmneziaVPN ≥5.0.1.5;
- ❓ синтаксис ключей 3.1 по man не проверен (`awg.8` отдал 404).

**Компромиссы**
- Ловится только IP-назначение. Домены RU-сервисов на зарубежных CDN проходят мимо.
- Клиент с `AllowedIPs = 0.0.0.0/0` в режиме `block` ломает себе RU-сайты. «0.0.0.0/0 минус ru.txt» — это ~13k v4-сетей, а у мобильных клиентов есть лимит на число маршрутов. Реалистичнее рекомендовать сплит по приложениям.
- ❓ `-m set` с iptables-nft на Ubuntu 22.04/24.04 не проверен. Запасной путь — нативный `nft` set с `flags interval`.
- Сейчас `PostUp` использует только `iptables`. IPv6 FORWARD/NAT для AWG не настроен, хотя `ipv6.forwarding=1` включён (дефект [репо]). Либо отключить v6 в туннеле, либо дублировать правила.

### 5.5 Подписка: новая фаза `09-subscription.sh`, пишет settings через API

| Ключ 3x-ui v3.9.0 (📄 `setting.go`) | Значение | Эффект |
|---|---|---|
| `subUpdates` | `2` | `Profile-Update-Interval: 2` (по умолчанию 12) |
| `subTitle` | имя | `Profile-Title` |
| `subHappAutoDetect` | `true` | **Без этого Happ-заголовки не отдаются вообще** |
| `subHappLocalProxyAuth` | `auto` | `Socks-Auth-Mode`/`Http-Auth-Mode`: Happ ставит пароль на локальный SOCKS/HTTP. Без Provider ID |
| `subEnableRouting` + `subRoutingRules` | профиль roscomvpn DEFAULT (URL) | `Routing-Enable` + `Routing`. HTTPS-URL кешируется 10 мин, лимит 16 KiB |
| `subJsonEnable` | **`false` по умолчанию** | См. ниже |
| `subHappPerAppMode`/`List` | `exclude` + банки, MAX | `Per-App-Proxy-Mode: bypass` (Android). ⚠️ Вероятно, требует Provider ID и будет проигнорирован |
| путь подписки | случайный (v3.8.0+) | Защита от угадывания |
| домен | свой (под)домен, TTL 60–300 | Ротация IP без перевыпуска |

- Для v2RayTun отдавать `update-always: true`. Hy2 класть в подписку через external links (`hysteria2://`). AWG отдавать отдельным файлом.
- **JSON-подписка и балансер (конфликт s-client-balancer ↔ утечка):** `default.json` открывает клиенту SOCKS без пароля. Варианты:
  - (а) по умолчанию отдавать raw + Happ, JSON-подписку не включать;
  - (б) ❓ переписать `noauth` на `password` через nginx `sub_filter` на `/json/`. Может сломать клиенты, чей TUN-слой ходит в этот SOCKS без пароля;
  - (в) завести issue в 3x-ui.
  - Балансер включать только вместе с (б) или после фикса в апстриме.
- ❓ Допустимые значения `Socks-Auth-Mode`, кроме `auto`, не найдены. URL Happ-профиля roscomvpn брать из README `hydraponique/roscomvpn-routing`.

**Текст для CREDENTIALS (`99-print-creds.sh`)**
1. Клиенты: Happ, v2RayTun, на iOS при отсутствии Happ — INCY/Karing, на Android — v2rayNG. Добавлять **подписку**, автообновление включить. Для XHTTP нужно ядро клиента ≤v26.7.28 или сборка с http2legacy.
2. **Сплит обязателен:** профиль «РФ напрямую». Без него RU-сайты не откроются (`blocked`) или пойдут через Cloudflare (`warp`).
3. Android: исключить банки, Госуслуги, MAX, маркетплейсы. **Это прячет только IP, а не факт VPN.** tun и TRANSPORT_VPN видны всем, детект есть у 22 из 30 топ-приложений. Полностью прячет только роутер или второй телефон.
4. Включить авторизацию локального SOCKS/HTTP или выключить локальный прокси. Обновлять клиент.
5. Режим TUN, а не системный прокси.
6. Порядок транспортов при проблемах: Reality-TCP → XHTTP → Hy2 → AWG. «Wi-Fi не работает, LTE работает» значит менять транспорт, а не сервер.
7. Подписку и ключи никому не пересылать.
8. Печатать ASN сервера (`curl ipinfo.io/org`) с предупреждением, если он из засвеченных (Hetzner AS24940, Contabo AS51167, DO, OVH, Amazon). Подавать как подсказку: список устаревает. Дать ссылку на чекер «TCP 16-20» и попросить прогнать его из РФ до раздачи.

### 5.6 Эксплуатация

**Раздельные IP входа и выхода на одном VPS** (если есть второй IPv4):
- Xray: `"sendThrough": "${EGRESS_IP}"` в outbound `direct`;
- Hy2: `direct.bindIPv4`;
- AWG: `SNAT --to-source ${EGRESS_IP}` вместо MASQUERADE.
- Ограничение: если оба IP в одной /24, бан подсети убивает оба. RU-приложения всё равно видят иностранный IP.

**Входная нода в РФ** (`scripts/entry.sh`, роль `--role entry`, **по умолчанию выключена**). Нода без состояния: без БД и панели, ключи из переменных.
```json
{
  "inbounds": [{ "tag": "in", "port": 443, "protocol": "vless",
     "settings": { "clients": [{ "id": "${ENTRY_UUID}", "flow": "xtls-rprx-vision" }], "decryption": "none" },
     "streamSettings": { "network": "raw", "security": "reality",
       "realitySettings": { "target": "${ENTRY_SNI}:443", "serverNames": ["${ENTRY_SNI}"],
         "privateKey": "${ENTRY_PRIV}", "shortIds": ["${ENTRY_SID}"] } },
     "sniffing": { "enabled": true, "destOverride": ["http","tls","quic"], "routeOnly": true } }],
  "outbounds": [
    { "tag": "to-exit", "protocol": "vless",
      "settings": { "vnext": [{ "address": "${EXIT_HOST}", "port": ${EXIT_PORT},
        "users": [{ "id": "${EXIT_UUID}", "flow": "xtls-rprx-vision", "encryption": "none" }] }] },
      "streamSettings": { "network": "raw", "security": "reality",
        "realitySettings": { "serverName": "${EXIT_SNI}", "publicKey": "${EXIT_PUB}",
          "shortId": "${EXIT_SID}", "fingerprint": "chrome" } } },
    { "tag": "direct", "protocol": "freedom" },
    { "tag": "blocked", "protocol": "blackhole" }
  ],
  "routing": { "domainStrategy": "IPIfNonMatch", "rules": [
    { "type": "field", "ip": ["geoip:private"], "outboundTag": "blocked" },
    { "type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked" },
    { "type": "field", "domain": ["ext:geosite_RU.dat:category-ip-geo-detect"], "outboundTag": "direct" },
    { "type": "field", "domain": ["ext:geosite_RU.dat:category-ru","ext:geosite_RU.dat:tld-ru"], "outboundTag": "direct" },
    { "type": "field", "ip": ["ext:geoip_RU.dat:ru"], "outboundTag": "direct" },
    { "type": "field", "network": "tcp,udp", "outboundTag": "to-exit" } ] }
}
```
- Echo и RU-трафик выходят со входа (`direct`). Детекторы видят RU-IP входа, и он совпадает с тем, что видят RU-сайты. Это единственный вариант, где приложения видят «российский» IP, хотя и с hosting-флагом.
- Положить `geoip_RU.dat`/`geosite_RU.dat` рядом с xray на входе: entry использует `ext:`, а 3x-ui там нет.
- ❓ `network: "raw"` и `target` — имена из актуальных версий Xray. Инсталлер пока пишет `tcp`/`dest`, проверить.
- Плечо вход→exit: Reality-TCP или XHTTP. При XHTTP **на входе** пин Xray ≤v26.7.28 или сборка с `http2legacy`: баг #6797 живёт на стороне, которая звонит.
- UDP (Hy2/AWG) — L4-проброс: `nft add rule ip nat prerouting udp dport ${AWG_PORT} dnat to ${EXIT_IP}` + masquerade. ❓ Работу при БС никто не подтвердил.
- На exit (`01-firewall.sh`): `ufw allow from ${ENTRY_IP} to any port ${VLESS_PORT_ENTRY} proto tcp`. Этот inbound в клиентскую подписку не публиковать.
- В подписке вход — отдельная группа «LTE/БС». Проверка: IP входа есть в `ru-whitelist.txt` → «пригоден для БС», иначе предупреждение.
- Риски: ToS RUVDS/Beget/NTX, 210-ФЗ, проект «Антифрод 3.0», ТСПУ в ДЦ, KYC и СОРМ на входе.

**Проверки из РФ** (`scripts/probe.sh`, опционально):
- серия из 3 проб раз в 10 мин;
- по каждому транспорту передача **больше 20 КБ в течение 2–3 мин** (xray-клиент + `curl` файла ~1 МБ), а не 204-пинг;
- готовые инструменты: `xray-checker` (без Hy2/AWG), `tspu-checker`;
- проба из ДЦ не видит мобильные БС;
- алерты слать через туннель или почту.

**Смена IP:**
- В подписке домен с TTL 60–300.
- Хук `vpnzoo-ip-changed NEW_IP` обновляет A-запись через API DNS, затем `externalProxy`/Host в 3x-ui, затем перевыпускает подписку с теми же UUID.
- Никогда не отдавать пустую подписку. TTL снижать заранее.
- AWG/WG не перерезолвят домен без переподключения.

**Бэкап одной командой** (s-fast-redeploy) — важнее автодетекта:
- `vpnzoo-export` собирает в архив `x-ui.db`, `config.env`, ключи AWG, сертификат Hy2, x25519, UUID;
- `vpnzoo-import` разворачивает его на чистом VPS идемпотентно;
- автобэкап наружу.

### 5.7 Сводка по фазам

| Фаза / файл | Изменение | Класс |
|---|---|---|
| `00-bootstrap.sh` | `ipset`, `jq`, опционально `wireguard-tools`/`wgcf`; определение второго IP | — |
| `01-firewall.sh` | Панель не открывать наружу (127.0.0.1 + SSH-туннель); UDP-диапазон Hy2 при hopping; allow-from-entry; правила v6 | [репо] |
| `03-3xui.sh` | `XUI_VERSION=v3.9.0` (fallback v3.8.5); listen 127.0.0.1 (ключ `webListen` есть, ❓ флаг CLI не проверен) | [версия]/[репо] |
| `04-vless-reality.sh` | Inbound через API, а не прямой INSERT; xray по `$ARCH`; sniffing `routeOnly:true` без fakedns; 2–3 serverNames из RealiTLScanner; второй inbound XHTTP+Reality (packet-up) на альтернативном порту с другим SNI | [репо] |
| **`07-xray-routing.sh`** (новая) | Шаблон §5.1, `RU_EGRESS`/`ECHO_EGRESS`, geo-таймер | новое |
| **`08-warp.sh`** (новая, opt-in) | WARP A или B | новое |
| `05-hysteria2.sh` | `--version app/v2.12.3`, команда обновления, `sniff`/`acl`/`outbounds`, masquerade proxy | [версия]/[репо] |
| `06-amneziawg.sh` | Уникальные параметры 3.x, ipset + PostUp, v6-правила | [репо] |
| **`09-subscription.sh`** (новая) | Настройки §5.5, external link Hy2 | новое |
| `99-print-creds.sh` | Текст §5.5, ASN, чекер, предупреждения | [репо] |
| `scripts/entry.sh`, `probe.sh`, `vpnzoo-export/import` | §5.6 | новое |

### 5.8 Приоритеты

**P0. Дефекты и безопасность (день-два)**
1. Панель на 127.0.0.1 + SSH-туннель. Сейчас она открыта по HTTP наружу.
2. Фаза 04: API вместо INSERT по старой схеме, `XRAY_BIN` по `$ARCH`, sniffing.
3. Пины: 3x-ui v3.9.0 (проверить на стенде, иначе v3.8.5), Hy2 app/v2.12.3 + отдельная команда обновления. Список клиентов в README.
4. Уникальные параметры AWG на установку. Если 3.x, то с предупреждением о клиенте ≥5.0.1.5.
5. Подписка: случайный путь, `subUpdates=2`, `subHappAutoDetect`, `subHappLocalProxyAuth=auto`, routing-профиль «РФ напрямую», **JSON-подписка выключена**.
6. Серверный RU-egress (`07`): `ru-blocked→direct`, `ip-echo→blocked|warp`, `RU→blocked|warp`. Плюс Hy2 ACL и AWG ipset. Без клиентского сплита это ломает RU-сайты, поэтому идёт только вместе с п.5.

**P1. Живучесть**
7. `vpnzoo-export`/`import` + автобэкап наружу.
8. Домен с TTL 60–300 для подписки и узлов + хук смены IP.
9. Второй inbound XHTTP+Reality на альтернативном порту с другим SNI.
10. RealiTLScanner по /24 при установке; предупреждать о .ru/Apple SNI.
11. Печать ASN + ссылка на чекер «TCP 16-20» в `99-print-creds.sh`.
12. Hy2: masquerade proxy, port hopping как опция.

**P2. Опции**
13. WARP (`08`) — вариант A.
14. `sendThrough` на второй IP.
15. Роль `entry` (RU, opt-in, профиль «LTE/БС») + проверка по `ru-whitelist.txt`.
16. `probe.sh` с RU-узла.
17. IPv6-endpoint дополнительным.
18. Shadowsocks-2022 как не-TLS запасной (inbound 3x-ui).

**P3. Экзотика и потом**
19. Профиль «Anti-DPI» (Host с finalMask fragment) для INCY.
20. XHTTP за nginx под CDN (GET-uplink, xPaddingBytes).
21. Интеграция с API хостера (floating IP).
22. Заметка в README: Remnawave как путь миграции при многих нодах.

---

## 6. Мифы и опровергнутое

Целиком опровергнутых утверждений нет. Ниже — **части** утверждений, которые адверсарии опровергли. Это то, что нельзя писать в README и CREDENTIALS.

| Миф | Почему неверно |
|---|---|
| «Сплит прячет VPN от банков» | tun0 и TRANSPORT_VPN видны и исключённым приложениям (RKS Global). Сплит прячет только IP |
| «С подпиской пользователь ничего не чинит» | Сервисы до сих пор просят вручную обновить подписку. Автообновление при кривой миграции стирает узлы (Habr 1040846) |
| «Блокируют только IP, не протоколы» | Параллельно идут QUIC, DTLS, заморозка 16 КБ, Reality на голом TCP. Amnezia спасла смена AWG→VLESS, а не ротация |
| «Банят ASN целиком» | Выборочно: Veesp 5000 из 30000, Amnezia Hosting 131 из 20000 |
| «Решает прежде всего ASN, а не протокол» | SSH и TCP без TLS через засвеченные ASN проходят |
| «Больше 3 TLS-сессий — заморозка», «xmux=3 месяц стабилен на МТС» | Гипотеза из одного источника, AND-логика. «Месяц на МТС» — пересказ. Vision-TCP с множеством соединений работает |
| «xmux=3 — настройка сервера» | Клиентская. 3x-ui её не хранит (#5194 not planned) |
| «Пин Xray на сервере лечит баг xmux», «баг — ровно то, что режет ТСПУ» | Баг в клиентском дозвоне. Причинность обратная: ТСПУ рвёт хендшейк, шторм идёт следствием |
| «RU-SNI на зарубежном IP рвётся всегда, МегаФон сверяет гео» | vk.com на DE-VPS неделями стабилен на МТС. Механизм — сверка RU-доменов с их CIDR, а не гео-IP (ozon.ru не работал и на RU-VPS) |
| «Крупные бренды SNI не брать» | github/microsoft/twitch работают; Habr 1009542 против только Apple. Риск — массовость, а не бренд |
| «Каскад через РФ потерял смысл» | Каскады используются и после июня 2026, с деградацией |
| «При БС работает только связка IP+SNI» | У МТС/T2 L3: белый IP пропускает любые протоколы. Есть туннели через YC Functions, WebRTC, DNS |
| «Happ настраивается заголовками бесплатно» | autoconnect, ping-type, per-app, fallback-url, resolve, fragmentation требуют Provider ID (премиум) |
| «3x-ui годится только на 1–2 сервера» | Habr 1040846: 12 машин на 3x-ui. В v3.7–3.9 мульти-нода и балансеры |
| «Remnawave — де-факто стандарт» | Не измерено, звёзды GitHub ничего не говорят о доле рынка |
| «Liberty перевёл на Karing» | Liberty советовал INCY, v2RayTun, смену региона. Karing советовал Blanc, потом тоже INCY |
| «Happ 4.4.6–4.4.8 чинят мобильный клиент» | Это desktop pre-release, стабильная 4.3.0 |
| «Клиент — слабое звено чаще сервера» | Не доказано: Blanc пишет, что блокировки «затронули практически всех» |
| «AWG 2.0 для self-hosted с 27.08» | 2.0 описан 25.03.2026. 3.1 в kernel module с 12.08 |
| «AWG 2.0 детектили по статистике потока» | Гипотеза самой Amnezia, без независимых данных |
| «Hy2 с настоящим сертификатом работает лучше» | Не доказано: на МегаФоне заработал самоподписанный с SNI донора |
| «Port hopping снимает шейпинг UDP-порта в РФ» | Шейпинг отдельного порта в РФ не подтверждён. Hopping — средство против блокировки по порту в Китае |
| «Скрытие панели закрывает active probing» | Probing отражает сам REALITY. Скрытие защищает от поиска панели и брутфорса |
| «Kakadu — это VLESS+Reality, типичный рынок» | Reality предположен по Wireshark. n=1 |
| «Fragment + noise помогают Reality» | noise — только UDP. tlshello замедляет хендшейк. От IP-банов, заморозки и БС fragment не спасает |
| «`subJsonFragment` с v3.3.1» | С v2.3.5. Маска глобальная, а не профильная |
| «IPv6 обходит блокировки» | IPv6 есть у ~2,9% пользователей в РФ (APNIC) |
| «POST режут почти все CDN» | Только российские CDN. Cloudflare POST пропускает, но сам под заморозкой 16 КБ |
| «Self-hosted защищён от банов закрытостью» | От бана всей AS и от бана по отпечатку протокола — нет |
| «Echo-утечка — главный механизм, данные доходят до ТСПУ за 6–12 ч» | 6–12 ч — ритм пакетных банов по словам Liberty. Путь MAX→РКН не доказан, MAX отрицает |
| «Серверный блок geoip:ru: кривой клиент не спалит» | Локальный SOCKS + зарубежный echo (ifconfig.me) обходят блок |
| «Каскад скрывает VPN от детекторов» | Только IP по GeoIP и только при сплите. TRANSPORT_VPN остаётся |
| «Habr 1053250 — опыт RU-сервиса» | Это VPN для Китая. Нерелевантен |
| «Пробой раз в минуту можно мониторить» | Короткий запрос пропускает заморозку после 16–20 КБ, нужна передача 2–3 мин |

---

## 7. Открытые вопросы и что проверить руками

**Версии и стенд**
- [ ] 3x-ui **v3.9.0 вышла 03.10.2026, ей один день.** Поставить на стенд, проверить миграцию конфига, Happ-заголовки, external links. Запасной вариант — v3.8.5.
- [ ] Флаг `--version app/v2.12.3` у `get.hy2.sh`.
- [ ] Флаг CLI 3x-ui для `webListen=127.0.0.1`.
- [ ] Имена `raw`/`target` против `tcp`/`dest` в Xray v26.9.30 (что пишет инсталлер и что принимает ядро).
- [ ] Синтаксис ключей AWG 3.1 (`awg.8`, S1–S4 ≥12, I1, диапазоны H).

**Ресурсы и ядро**
- [ ] ОЗУ и время старта Xray и Hysteria с geosite ~74 МБ на VPS с 1 ГБ (`systemd-cgtop`).
- [ ] `-m set` с iptables-nft на Ubuntu 22.04/24.04 или переход на `nft` set.
- [ ] `SO_BINDTODEVICE` на `warp0` с `Table = off` (`curl --interface warp0 …/cdn-cgi/trace`).
- [ ] Observatory + wireguard outbound как фолбэк WARP→blocked.
- [ ] `*.dat.sha256sum` как release asset.

**Подписка и клиенты**
- [ ] Значения `Socks-Auth-Mode`, кроме `auto`. Применяет ли Happ его без Provider ID.
- [ ] Переписывание `noauth` в JSON-подписке через `sub_filter`: какие клиенты ломаются.
- [ ] Какие клиенты (Happ, v2RayTun, Streisand, v2rayNG) понимают балансеры из JSON-подписки.
- [ ] Поддержка `awg://` и `hysteria2://` через external links в Happ/v2rayNG/Karing; `mport`/`mportHopInt` в Happ.
- [ ] Версия Xray-ядра в Happ, v2RayTun, v2rayNG: попадают ли они под баг #6797 при XHTTP.
- [ ] Точный URL Happ-профиля roscomvpn DEFAULT.
- [ ] Эндпоинт `POST /panel/xray/routeTest` для smoke-теста маршрутов.

**Полевые проверки из РФ** (сериями по 2–3 мин, больше 20 КБ)
- [ ] МТС, МегаФон, T2, Билайн (мобильный) + 1–2 домашних провайдера: Reality-TCP 443, XHTTP на альтернативном порту, Hy2 (с hopping и без), AWG 3.x.
- [ ] Чекер «TCP 16-20» со своим хостом: принимает ли REALITY без selfsteal.
- [ ] После включения `07`: открываются ли Госуслуги, банки, Ozon у клиента **со** сплитом и **без** него в обоих режимах `RU_EGRESS`.
- [ ] Что видит `ifconfig.me` и `ip.mail.ru` из клиента без сплита (ожидается IP Cloudflare или ошибка).

**RU-вход**
- [ ] Проходит ли UDP-проброс Hy2/AWG через RU-вход при БС.
- [ ] Белый ли IP у выбранного RU-хостера (`ru-whitelist.txt` + открыть :443 с мобильного без VPN).
- [ ] Дата вступления в силу пункта 210-ФЗ о хостинге по первичному тексту.

**Неподтверждённое (❓), что стоит добить отдельным раундом**
- [ ] l-local-socks-vuln, l-3xui-socks-auth, l-echo-domains, l-hy2-acl, l-awg-ipset: напрямую влияют на P0.
- [ ] l-ipv6-leak: насколько runetfreedom `ru.txt` покрывает RU-IPv6 сервисов.
- [ ] l-webrtc: можно ли на сервере резать STUN-назначения без поломки звонков.

---

## 8. Источники

**Сервисы (самоотчёты)**
- Liberty: https://t.me/s/vpn_liberty, https://t.me/vpn_liberty/309, https://t.me/s/vpn_liberty/316, https://t.me/vpn_liberty/310
- BlancVPN: https://t.me/s/blancvpn, https://t.me/blancvpn/118, https://t.me/blancvpn/129, https://t.me/blancvpn/131, https://blancvpn.pro/ru/help/quick-help
- Amnezia: https://t.me/s/amnezia_vpn_news_ru, https://amnezia.org/ru/blog/amnezia-vpn-hybrid-attack-postmortem, https://amnezia.org/ru/blog/amneziawg-3-1-is-here, https://amnezia.org/blog/amneziawg-2-0-available-for-self-hosted, https://amnezia.org/ru/blog/cascading-hosting-issues-may-june-2026, https://docs.amnezia.org/documentation/amnezia-wg/
- VPN Generator: https://t.me/s/vpngen
- OpenRoad (маркетинг): https://openroadvpn.ru/vpn-protocols.html
- ProxysVPN: https://proxysvpn.com/guides/happ-routing-profiles
- vpnstatus (пользовательский каталог): https://vpnstatus.site/protocols/vless, https://vpnstatus.site/whitelist

**СМИ**
- The Bell: https://thebell.io/kak-ustroen-rossiyskiy-rynok-vpn-pochemu-topy-lyubyat-ai-i-vebinar-o-tom-est-li-v-etoy-tekhnologii-puzyr, https://thebell.io/22-iz-30-populyarnykh-rossiyskikh-prilozheniy-sledyat-za-tem-vklyuchen-li-u-vas-vpn
- Meduza:
  - https://meduza.io/feature/2026/04/03/mozhno-li-deystvitelno-zablokirovat-vse-vpn
  - https://meduza.io/feature/2026/08/03/teoreticheski-oni-mogut-zablokirovat-lyuboy-servis-tselikom
  - https://meduza.io/feature/2026/08/19/novye-blokirovki-esli-i-otkatyat-to-ne-polnostyu-protsentov-na-pyatdesyat-a-polovinu-ostavyat-i-eto-stanet-novoy-normoy
  - https://meduza.io/news/2026/04/06/mintsifry-razoslalo-rossiyskim-kompaniyam-metodichku-po-poisku-vpn-na-ustroystvah-polzovateley-v-vedomstve-zayavili-chto-vyyavlenie-takih-servisov-v-ayfonah-problematichno
  - https://meduza.io/feature/2026/04/11/rossiyskie-prilozheniya-dlya-android-sledyat-za-vami-po-suti-eto-polnotsennye-shpionskie-programmy
  - https://meduza.io/feature/2026/04/16/krupneyshie-rossiyskie-servisy-zakryvayut-dostup-polzovatelyam-s-vklyuchennym-vpn
  - https://meduza.io/feature/2026/08/04/nekotorye-servisy-soobschili-o-novoy-volne-blokirovok-vpn-v-rossii-ee-nazyvayut-odnoy-iz-krupneyshih-za-poslednee-vremya
  - https://meduza.io/news/2026/09/23/bi-bi-si-v-pravitelstve-vernulis-k-idee-platy-za-mezhdunarodnyy-trafik-no-teper-v-seti-5g-v-rossii-ona-dostupna-tolko-vladeltsam-androidov
- Новая газета Европа: https://novayagazeta.eu/articles/2026/08/05/rkn-provel-novuiu-masshtabnuiu-volnu-blokirovok-vpn-servisov-news, https://novayagazeta.eu/articles/2026/03/05/v-max-obnaruzhili-shpionskii-modul-kotoryi-otslezhivaet-ispolzovanie-vpn-news
- Euronews: https://ru.euronews.com/my-europe/2026/08/05/vpn-blockage-russia
- AppleInsider: https://appleinsider.ru/news/posle-vyborov-v-gosdumu-vpn-v-rossii-nachali-blokirovat-eshhe-zhestche-chto-proishodit.html
- CNews: https://www.cnews.ru/news/top/2026-10-01_po_vpn_v_rossii_nanesen_tyazhelejshij, https://safe.cnews.ru/news/top/2026-10-01_po_vpn_v_rossii_nanesen_tyazhelejshij, https://www.cnews.ru/news/top/2026-04-06_rossijskim_it-kompaniyam
- Хакер: https://xakep.ru/2026/06/10/antifraud/, https://xakep.ru/2026/03/06/max-reverse/, https://xakep.ru/2026/04/07/vless-bug/, https://xakep.ru/2026/04/10/apps-research/
- Медиазона: https://zona.media/news/2026/10/01/vpn, https://zona.media/news/2026/05/21/trafik
- Коммерсантъ: https://www.kommersant.ru/doc/8590872, https://www.kommersant.ru/doc/8625432
- Право.ру: https://pravo.ru/news/264452/
- The Moscow Times:
  - https://ru.themoscowtimes.com/2026/04/28/marketpleisi-stali-puskat-rossiyan-s-vpn-posle-padeniya-prodazh-a193979
  - https://ru.themoscowtimes.com/2026/05/26/roskomnadzor-nachal-trebovat-u-operatorov-svyazi-ip-adresa-rossiyan-dlya-blokirovok-vpn-a196222
  - https://ru.themoscowtimes.com/2026/08/04/desyatki-vpn-servisov-perestali-rabotat-v-rossii-posle-novih-trebovanii-mintsifri-k-provaideram-a202667
- Ведомости: https://www.vedomosti.ru/technology/news/2026/04/27/1193249-mintsifri-obyasnilo, https://www.vedomosti.ru/press_releases/2025/08/27/v-rossii-vveli-otvetstvennost-za-poisk-ekstremistskih-materialov-i-reklamu-vpn
- Фонтанка: https://www.fontanka.ru/2026/04/02/76345065/
- ComNews: https://www.comnews.ru/content/244810/2026-04-16/2026-w16/1009/rossiyskie-servisy-chastichno-ogranichivayut-rabotu-pri-vklyuchennom-vpn
- kod.ru: https://kod.ru/vse-30-ru-prilojeniy-sledyat-za-vpn, https://kod.ru/vpn-happ-problems, https://kod.ru/rkn-shtraf-operatory-ip-adresa-vpn
- iXBT: https://www.ixbt.com/news/2026/05/02/vpn-android-wildberries-ozon-2-rustore-vpn.html
- SecurityLab: https://www.securitylab.ru/blog/personal/Bitshield/361724.php, https://www.securitylab.ru/news/571207.php, https://www.securitylab.ru/news/571257.php
- Прочее: https://doxa.team/news/2026-04-06-mincifry-vpn, https://hightech.fm/2026/08/31/fsb-vpn, https://cisoclub.ru/roskomnadzor-rasshiril-spisok-razreshjonnyh-korporativnyh-vpn-do-57-tysjach-adresov/, https://www.forbes.com/sites/daveywinder/2026/05/15/wont-fix-all-vpn-apps-affected-as-google-android-16-leaks-info/, https://lowlevel.fun/posts/tiny-udp-cannon-android-vpn-bypass/, https://tarkalabs.com/blogs/vpn-detection-guide-ios-android/, https://blog.cloudflare.com/russian-internet-users-are-unable-to-access-the-open-internet/

**Теплица социальных технологий**
- https://te-st.org/2026/09/30/vlessmore/, https://te-st.org/2026/05/12/hostingrules/, https://te-st.org/2026/05/27/collectip/

**Исследования**
- RKS Global: https://rks.global/ru/research/vpn-detection/
- APNIC: https://stats.labs.apnic.net/ipv6/RU

**Habr**
- 1040846, 1047442, 1049680, 1020080, 1006394, 1000694, 1009542, 979128, 997088, 990206, 1008554, 1033400, 1021160 (зеркало https://www.pvsm.ru/vpn/449328), 1027276 (зеркало https://www.pvsm.ru/vpn/450362), 1022586, 1067230, 1081580, 1023352, 1056220, 1053250 (нерелевантен), 885276, 1046025, 1006950, 1080534, 1014636, 1021392, 1022422.
- Адрес: `https://habr.com/ru/articles/<id>/`; 1046025 и 1006950 — `/ru/news/`, 1080534 и 1014636 — `/ru/companies/amnezia/articles/`.

**ntc.party**
- Темы: 20340, 23653, 24230, 20117, 24119, 13887, 4625 (/1145), 16061 (/567, /1076, /1080, /1204, /1230), 22270, 22222, 22237, 23007, 24230, 25437, 25528.
- Пост 131928.
- «16кб блокировка»: https://ntc.rkn.quest/t/16%D0%BA%D0%B1-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0/22516

**net4people/bbs**
- Issues #490, #546, #650, #654, #671, #108: https://github.com/net4people/bbs/issues/<n>

**Код и документация**
- 3x-ui:
  - releases: https://github.com/MHSanaei/3x-ui/releases, https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0, …/v3.8.0, …/v3.7.0
  - PR/issues: PR #6243, #6545, #6628; issues #5194, #6273, #3961
  - исходники v3.9.0: https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/sub/default.json, …/internal/sub/happ.go, …/internal/web/service/config.json, …/internal/web/service/setting.go, …/internal/web/controller/xray_setting.go, …/internal/web/service/integration/warp.go, …/.github/workflows/release.yml
  - документация: https://docs.sanaei.dev/docs/config/subscription/, https://docs.sanaei.dev/docs/operations/outbounds-routing/
- Xray-core:
  - issues и PR: #6376, #6797, PR #6820, discussions #4113, #3518
  - коммиты 18b85ad и 18e2839
  - go.mod v26.7.28 / v26.9.30
  - splithttp/config.go
  - https://xtls.github.io/en/config/transports/reality.html, …/finalmask.html, …/outbounds/freedom.html, …/outbounds/wireguard.html, …/inbounds/tunnel.html, …/routing.html
  - https://github.com/XTLS/REALITY
- Hysteria: https://v2.hysteria.network/docs/advanced/ACL/, https://v2.hysteria.network/docs/advanced/Full-Server-Config/, https://v2.hysteria.network/docs/advanced/Port-Hopping/, https://v2.hysteria.network/docs/developers/URI-Scheme/, https://github.com/apernet/hysteria/releases
- AmneziaWG: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/commits/master, https://github.com/amnezia-vpn/amnezia-client/pull/2453
- Happ: https://docs.happ.info/main/dev-docs/app-management, https://www.happ.su/main/ru/dev-docs/app-management, https://docs.happ-proxy.com/getting-started/api.md, …/provider-id.md, …/premium-functionality.md, https://happ.mintlify.app/technical-docs/routing, https://github.com/Happ-proxy/happ-desktop/releases
- v2RayTun: https://docs.v2raytun.com/overview/supported-headers
- v2rayNG: https://github.com/2dust/v2rayNG/issues/6224, …/5467, …/5549
- Remnawave: https://docs.rw/learn/server-routing/, https://docs.rw/learn/xray-json-advanced/, https://docs.rw/docs/install/panel-security/, https://github.com/remnawave/panel, https://github.com/eGamesAPI/remnawave-reverse-proxy, https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot, https://github.com/Quazar-VPN/remnawave-subscription-middleware
- Geo и маршрутизация: https://github.com/runetfreedom/russia-v2ray-rules-dat, https://github.com/runetfreedom/russia-blocked-geoip/tree/release, https://github.com/hydraponique/roscomvpn-routing, https://github.com/hydraponique/roscomvpn-geosite, https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/category-ip-geo-detect
- Конфиги и инструменты:
  - igareck: https://github.com/igareck/vpn-configs-for-russia, https://github.com/kort0881/russia-whitelist/discussions/21
  - чекеры: https://github.com/hyperion-cs/dpi-checkers, https://github.com/kutovoys/xray-checker, https://github.com/ku78/tspu-checker, https://github.com/matador955/TSPU_checker
  - ротация и CDN: https://github.com/UIbodulloev/vpn-ip-rotator, https://github.com/ServerTechnologies/proxy-via-russian-cdn
  - уязвимость SOCKS и детекторы: https://github.com/runetfreedom/per-app-split-bypass-poc, https://github.com/xtclovver/RKNHardering, https://publish.obsidian.md/zapret/VLESS-SOCKS5-vulnerability
  - WARP: https://github.com/ViRb3/wgcf
  - прочее: https://github.com/legiz-ru/my-remnawave, https://github.com/petrochen/xray-double-hop, https://github.com/Leadaxe/sing-box-lx/issues/32
