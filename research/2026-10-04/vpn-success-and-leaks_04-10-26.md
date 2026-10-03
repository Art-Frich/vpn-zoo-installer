# Секреты успеха работающих VPN в РФ и компрометаторы: что заложить в инсталлер

**Дата:** 2026-10-04
**Для кого:** владелец репозитория и ИИ-ассистент, который будет вносить изменения в `vpn-zoo-installer`.
**Контекст:** self-hosted VPS (Ubuntu) для пользователей в РФ: 3x-ui (VLESS+REALITY+Vision, в плане XHTTP+REALITY), Hysteria2 (apernet, standalone), AmneziaWG (kernel, в плане рандомизированный профиль 3.x). Планы: пин 3x-ui v3.9.0, панель на 127.0.0.1, опционально вторая RU-VPS как входная нода.

## 0. Как собрано и как читать

- **Сбор:** 7 углов поиска (архитектура рабочих сервисов, транспорты, хостинг/IP, клиенты и подписки, роутинг, эксплуатация, компрометаторы), затем merge дублей.
- **Проверка:** каждое утверждение прошли 2 адверсария:
  - **evidence:** подтверждают ли источники то, что заявлено;
  - **applicability:** применимо ли это к самоделу на 1–2 VPS, и что из этого следует для сервера.
- **Допроверка:** отдельный проход по 27 утверждениям ветки компрометаторов (`l-*`), которые в основной раунд не уложились. Каждое тоже прошли 2 адверсария (27 × 2 = 54 голоса). Их статусы и исправленные формулировки внесены в §1, §3, §4, §5 и §6.
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
| ❓ unverified | Не проверялось. После допроверки так помечены только детали конфига (флаги, синтаксис, поведение на стенде), а не утверждения |
| 📄 код | Факт проверен по исходникам или файлам при подготовке черновика конфига (3x-ui v3.9.0, runetfreedom .dat), адверсариев не проходил |

**Итог проверки.** Всего проверено 63 утверждения, каждое двумя адверсариями.
- **Основной раунд, 36 утверждений.** Все 36 получили 🟡: у каждого есть сужение. У 35 оба голоса «partly», у s-hosting-law «holds» + «partly».
- **Допроверка компрометаторов, 27 утверждений.** 26 получили 🟡. У l-hy2-acl и l-platform-demand один голос «holds», второй «partly», у остальных оба «partly». Одно утверждение, **l-ipv6-leak, опровергнуто обоими** адверсариями и перенесено в §6.
- Чистых ✅ нет, ⚔️ нет, ❓ среди утверждений не осталось. Это нормально для темы, где почти всё держится на самоотчётах сервисов, пересказах СМИ и полевых постах. Целиком опровергнуто одно утверждение. Опровергнутых **частей** много, они собраны в §6.

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
| 1 | **Главный задокументированный канал утечки адреса сервера:** RU-приложение (MAX, март 2026) запрашивает свой внешний IP через echo-сервисы (ipify, checkip.amazonaws, ifconfig.me). Если этот запрос идёт в туннель, приложение видит IP exit. Что эти данные доходят до РКН/ТСПУ, **не доказано**. Все 6 echo-URL из реверса MAX есть в `category-ip-geo-detect`. Серверное правило прячет IP exit только от этих запросов, но не факт VPN и не IP, который приложение узнаёт через свой бэкенд или STUN. Для AWG (L3, без доменов) правило неприменимо. 🟡 | 🟡 `ip-echo → warp/blocked` (Xray, Hy2; не AWG) |
| 2 | **Серверный блок RU-назначений на exit** (geoip:ru, category-ru, tld-ru → blackhole/WARP). Это страховка: RU-сервис не увидит IP VPS при прямом обращении. От локального SOCKS + зарубежного echo, `/proc/net/route` и RU-сервисов на зарубежных IP не спасает. Цена: у клиента без сплита ломаются банки и Госуслуги. Сплит из подписки (заголовок `Routing`) получают **только Happ и INCY**, а клиенты v2rayNG, Hy2 и AWG его не получают. 🟡 | ✅ фаза `07` (Xray); Hy2 ACL и AWG ipset — только opt-in |
| 3 | **Разнос входа и выхода.** RU-вход + гео-сплит: RU-приложения видят российский IP. Второй IP через `sendThrough`/WARP на том же VPS лишь переживает точечный бан выхода. WARP даёт IP Cloudflare с геолокацией страны VPS, а не российский. 🟡 | ✅ опционально |
| 4 | **Факт VPN на Android сервер не скроет.** Приложение, исключённое из туннеля, не видит TRANSPORT_VPN у своей активной сети, но видит `tun0` (NetworkInterface), VPN-сеть в `getAllNetworks()` и установленные VPN-клиенты. По RKS Global, VPN детектят 22 из 30 топ-приложений, к 16.04 все 30. Помогают только роутер (с RU напрямую) или второй телефон. Рабочий профиль прячет VpnService-API и список пакетов, но не `tun0`, маршруты и loopback. 🟡 | ❌ только клиент |
| 5 | **Открытый локальный SOCKS у клиентов** (Happ, v2RayTun, V2BOX, v2rayNG, Hiddify, NekoBox и др.; «почти все» — преувеличение) позволяет любому приложению узнать IP exit. Happ ещё и отдавал xray API (в 4.7.2 убрали). Исправления опциональны: в v2rayNG 2.1.0 пароль и случайный порт выключены по умолчанию и ломают HEV TUN. **Наша находка 📄:** JSON-подписка 3x-ui v3.9.0 сама кладёт клиенту `socks 127.0.0.1:10808 auth:noauth`. 🟡 + 📄 | 🟡 только Happ: `subHappLocalProxyAuth=auto` + `subHappAutoDetect`; JSON-подписка выключена |
| 6 | **Регуляторика:** методичка Минцифры (IP → устройство → десктоп), сбор адресных пулов операторов с привязкой к ТСПУ (приказ РКН №51; без привязки к абоненту), 210-ФЗ против хостеров, проект «Антифрод 3.0» с годовым баном у RU-хостеров с 01.03.2028. Мониторинг хостеров (08.2026) пока обсуждается и касается только IP из белого списка ЦМУ ССОП. Всё это бьёт по RU-входу, а не по зарубежному exit. Для RU-входа главный риск — уровень идентификации у хостера. 🟡 | ✅ RU-вход только опцией |
| 7 | **РКН покупает подписки** коммерческих VPN и автоматически вытаскивает из них конфиги. Для самодела это повод не пускать чужих или пробных пользователей на основную ноду. Есть и правовая причина: если раздавать подписки посторонним, владелец сервера формально может подпасть под ст. 13.52 КоАП («владелец средства обхода»; так считает адверсарий по l-legal). 🟡 | ✅ отдельная нода для внешних |

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
- **Опровергнуто:** «банки не видят VPN». Сплит прячет только IP выхода. RKS Global: исключение приложений из VPN не прячет VPN: `tun0` и VPN-сеть в списке сетей видны (TRANSPORT_VPN у собственной активной сети исключённое приложение не видит, см. §3.2). Яндекс блокирует по серверному флагу. Даже в Habr 1040846 со включённым VPN все приложения, кроме Госуслуг, отказывались работать.
- Баг 3x-ui: INCY получает Happ-профиль (#6273, not planned). В теге v3.9.0 он не исправлен (подтверждено голосом по l-routing-cost).
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
| **30 топ RU-приложений** | Статический анализ (jadx/apktool, 68 точек; у Хакера 80): VPN детектят 22 из 30. В обновлении 16.04, уже с динамическим тестом на устройстве, — все 30. 19 из 22 по коду шлют VPN-статус на свой сервер (у The Bell — 18). Среди них все три банка выборки: Т-Банк, Сбер, ВТБ. Перехват трафика не публиковался, отправка выведена из кода | TRANSPORT_VPN (цифра 17 и MAX среди них — по Meduza, на странице RKS её нет), сетевые интерфейсы / tun0 (13), `/proc/net/tcp`, прокси, список VPN-приложений через `queryIntentServices` | 04.2026, [RKS Global](https://rks.global/ru/research/vpn-detection/), [Хакер 10.04](https://xakep.ru/2026/04/10/apps-research/), [The Bell](https://thebell.io/22-iz-30-populyarnykh-rossiyskikh-prilozheniy-sledyat-za-tem-vklyuchen-li-u-vas-vpn), [Meduza 11.04](https://meduza.io/feature/2026/04/11/rossiyskie-prilozheniya-dlya-android-sledyat-za-vami-po-suti-eto-polnotsennye-shpionskie-programmy) | 🟡 (l-rks-app-survey). Про проверку exit-IP в исследовании ничего нет |
| **WB, 2ГИС, МТС, Ozon, Мегамаркет, RuStore, ОК** | Собирают список установленных VPN-клиентов (7 приложений после обновления 16.04; до него — только Самокат и MegaMarket) | `queryIntentServices` | 04.2026, RKS Global, [iXBT](https://www.ixbt.com/news/2026/05/02/vpn-android-wildberries-ozon-2-rustore-vpn.html) | 🟡 (l-rks-app-survey) |
| **Яндекс** | Блокирует по серверному флагу | Сервер получает VPN-флаг | [kod.ru](https://kod.ru/vse-30-ru-prilojeniy-sledyat-za-vpn) | 🟡 (голос по s-split-routing) |
| **Приложения со звонками (VK, банки и др.)** | WebRTC/STUN технически может раскрыть VPN: в нативном WebRTC (без mDNS) host-кандидаты содержат IP tun, а srflx через полный туннель показывает IP exit. Что приложения реально так делают, не доказано. В аудите Habr 1081580 IceCandidate — лишь 1 из 8 литералов в smali (вместе с getNetworkInterfaces, tun0, ppp0, TYPE_VPN и др.), которые суммируются в балл (15+ совпадений = HIGH). Разбивки по приложениям нет, литерал есть в любом SDK звонков. MAX (05.03.2026) объяснил сбор IP звонками и отрицал связь с VPN | WebRTC/STUN | 03–09.2026, [Habr 1081580](https://habr.com/ru/articles/1081580/), [Habr 1006950](https://habr.com/ru/news/1006950/) | 🟡 (l-webrtc). Опровергнуто: «IceCandidate — основной HIGH-признак у VK, Т-Банка, Сбера, Госуслуг, Avito» |
| **МТС, Ozon, WB** | Реакция на детект (9–16.04.2026) в основном мягкая. МТС: «Включен VPN. Данные могут не отображаться». Пресс-служба WB: «работа может быть затруднена». У Ozon, по Habr 1021392 (09.04), не грузились фото и checkout. Что банки не пускали, не подтверждено: ComNews у Т-Банка уведомлений не нашёл. Прямой отправки в РКН не доказано, но VPN-статус уходит на серверы компаний, а методичка, по РБК, требует передавать регулятору IP новых VPN | — | 04.2026, [ComNews](https://www.comnews.ru/content/244810/2026-04-16/2026-w16/1009/rossiyskie-servisy-chastichno-ogranichivayut-rabotu-pri-vklyuchennom-vpn), [Habr 1021392](https://habr.com/ru/articles/1021392/), [Meduza 16.04](https://meduza.io/feature/2026/04/16/krupneyshie-rossiyskie-servisy-zakryvayut-dostup-polzovatelyam-s-vklyuchennym-vpn) | 🟡 (l-app-behavior). Для сервера: RU-приложение в туннеле видит exit-IP VPS, по оценке Гильдии VPN это возможный источник IP-банов |
| **Ozon, WB, Яндекс Пэй/Книги/Карты, ВкусВилл, Перекрёсток, Пятёрочка** | К 28.04 снова пускали пользователей с VPN. Связь с падением продаж — со слов селлеров, официально откат не признан. Про банки отката не найдено | — | [MT 28.04](https://ru.themoscowtimes.com/2026/04/28/marketpleisi-stali-puskat-rossiyan-s-vpn-posle-padeniya-prodazh-a193979), [Ведомости](https://www.vedomosti.ru/technology/news/2026/04/27/1193249-mintsifri-obyasnilo) | 🟡 (l-platform-demand) |
| **VLESS-клиенты на Android**: Happ, v2RayTun, V2BOX, v2rayNG, Hiddify, Exclave, Npv Tunnel, NekoBox, «распространённые» конфигурации Clash/sing-box | SOCKS5 на 127.0.0.1 без авторизации. Любое приложение, даже исключённое или в Knox/Shelter/Island (loopback общий), узнаёт через него IP exit. При разнесённых входе и выходе утекает IP выхода. Разработчикам сообщили 10.03.2026. «Почти все» — преувеличение: Husi, SFA и Xray (saeeddev94) позволяли задать пароль ещё до раскрытия. HTTP-прокси без пароля в раскрытии не задокументирован. В v2rayNG 2.1.0 (17.04.2026) есть SOCKS-auth и случайный порт, но выключены по умолчанию и с HEV TUN ломают трафик (#5549, not planned) | Сканирование localhost + echo | 07.04.2026, [Habr 1020080](https://habr.com/ru/articles/1020080/), [PoC](https://github.com/runetfreedom/per-app-split-bypass-poc), [v2rayNG 2.1.0](https://github.com/2dust/v2rayNG/releases/tag/2.1.0), [v2rayNG #5382](https://github.com/2dust/v2rayNG/issues/5382), [#5549](https://github.com/2dust/v2rayNG/issues/5549), [Хакер](https://xakep.ru/2026/04/07/vless-bug/) | 🟡 (l-local-socks-vuln) |
| **Happ** | Открывал xray API с HandlerService без авторизации: из него выгружались UUID, ключи, адрес входа, SNI. В 4.7.2 API убрали, открытый SOCKS остался. При включённом «Allow LAN» Happ сам снимает пароль с локального прокси | Локальный API | 04.2026, [kod.ru 11.04](https://kod.ru/vpn-happ-problems) | 🟡 (l-local-socks-vuln) |
| **Happ (с Provider ID)** | Раз в сутки шлёт HWID, ОС и хэш домена подписки на check.happ-proxy.com | Телеметрия клиента | [docs.happ-proxy.com](https://docs.happ-proxy.com/getting-started/provider-id.md) | 🟡 (голос по s-happ-headers) |
| **3x-ui v3.9.0, JSON-подписка** | Кладёт клиенту `{"listen":"127.0.0.1","port":10808,"protocol":"socks","settings":{"auth":"noauth"}}` | Тот же вектор открытого SOCKS, но со стороны сервера | [default.json](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/sub/default.json) | 📄 |
| **Клиенты без локального прокси**: WireGuard/AmneziaWG, sing-box только с tun (без mixed и clash_api) | К сканированию localhost не уязвимы. Но от детекта VPN это не защищает (tun0 и VPN-сеть видны). На Android приложение, исключённое из split tunneling, может привязать сокет к tun0 (SO_BINDTODEVICE, `curl --interface tun0`) и узнать IP сервера: Amnezia #2457, фикс PR #3199 (22.09.2026) не влит и будет выключен по умолчанию. В клиенте Amnezia открытый SOCKS был только у Xray-моста: исправлен PR #2456 (влит 13.04.2026; PR #2453 закрыт **без** слияния). В 5.0.3.0 (18.09.2026) у моста случайные логин, пароль и порт. У sing-box-клиентов (Hiddify, Karing) отдельный вектор — clash_api | tun2socks → SOCKS, bind к tun0 | [Amnezia PR #2456](https://github.com/amnezia-vpn/amnezia-client/pull/2456), [#2457](https://github.com/amnezia-vpn/amnezia-client/issues/2457), [PR #3199](https://github.com/amnezia-vpn/amnezia-client/pull/3199), [PR #2453](https://github.com/amnezia-vpn/amnezia-client/pull/2453) | 🟡 (l-awg-clients). Отдельного аудита AWG-клиентов и sing-box TUN нет, вывод по архитектуре |
| **RKNHardering** (открытый тестовый детектор, а не госприложение) | В режиме AUTO сканирует 127.0.0.1/::1: сначала популярные порты, потом 1024–65535 по 200 параллельно (SOCKS5-greeting и HTTP CONNECT). Вызывает Xray gRPC listOutbounds (100 потоков), опрашивает Clash API на 9090/19090/9091/9097, подбирает слабые пароли (admin/admin, user/password, пустой). Открытый прокси сам по себе даёт только needsReview, обход подтверждается сменой IP. Что реальные RU-приложения так перебирают весь localhost, не доказано: методичка называет конкретные порты, RKS описывает системные API и список пакетов | Скан loopback | [RKNHardering](https://github.com/xtclovver/RKNHardering/blob/main/docs/README.en.md) | 🟡 (l-client-auth-ports). Вывод: один случайный порт не спасает, нужен **длинный случайный** пароль, `udp:false`, закрытые Xray API и Clash API. Порт с паролем всё равно виден, а VPN выдаёт сам TUN |
| **Android 16 QPR1+ (ОС)** | Скрытый `@hide` API `registerQuicConnectionClosePayload` без проверки прав: приложение с INTERNET через Binder заставляет system_server отправить произвольный UDP мимо VPN, даже при Always-on и Block without VPN. Так утекает реальный IP, и бэкенд может связать его с exit-IP из туннеля. Работает только при включённом флаге DeviceConfig `tethering/close_quic_connection`; что он включён по умолчанию, источники расходятся. Google: Won't Fix. Исправлено в GrapheneOS 2026050400 (05.05.2026). На стоковом Android: `adb shell device_config put tethering close_quic_connection -1`. Использования этого вектора RU-приложениями не найдено | Баг ОС | 05.2026, [lowlevel.fun](https://lowlevel.fun/posts/tiny-udp-cannon-android-vpn-bypass/), [PoC](https://github.com/0x33c0unt/quic-vpn-bypass), [Mullvad](https://mullvad.net/en/blog/2026/5/12/any-app-on-recent-android-versions-can-leak-certain-traffic), [leewoobin](https://leewoobin.com/posts/android-quic-vpn-lockdown-exception/) | 🟡 (l-android16-udp) |
| **iOS (все)** | Детект слабее, чем на Android: у сторонних приложений нет системного API статуса VPN, они не видят чужие приложения и таблицу маршрутов (методичка называет детект на iOS «затруднённым»). Остаются эвристики (`__SCOPED__` в CFNetworkCopySystemProxySettings с utun/ipsec/tun, `getifaddrs`), обе с ложными срабатываниями. **Тезис «sandbox не даёт сканировать loopback» неверен:** iOS-приложения могут слушать и подключаться к локальным портам, Happ iOS, V2BOX и Karing попали в список уязвимых к открытому SOCKS. Главный канал утечки — echo через туннель | Эвристики, loopback, echo | [Meduza 06.04](https://meduza.io/news/2026/04/06/mintsifry-razoslalo-rossiyskim-kompaniyam-metodichku-po-poisku-vpn-na-ustroystvah-polzovateley-v-vedomstve-zayavili-chto-vyyavlenie-takih-servisov-v-ayfonah-problematichno), [zapret](https://publish.obsidian.md/zapret/VLESS-SOCKS5-vulnerability), [Local Mess](https://localmess.github.io/), [tarkalabs](https://tarkalabs.com/blogs/vpn-detection-guide-ios-android/) | 🟡 (l-ios). Работа `subHappLocalProxyAuth` на iOS не проверена |

### 3.2 Системные признаки, видимые без разрешений (Android) 🟡

- **Любому приложению** без разрешений видны: интерфейс `tun0`/`ppp0` и его MTU (NetworkInterface), сеть с TRANSPORT_VPN в `getAllNetworks()`, `getNetworkInfo(TYPE_VPN)=CONNECTED`, установленные VPN-клиенты.
- **Приложение, исключённое из туннеля per-app,** не видит TRANSPORT_VPN/IS_VPN у своей `activeNetwork`. DNS и маршруты в её LinkProperties у него реальные, так что loopback-DNS ему не виден (эмулятор Android 15, linkshield PR #60 от 27.09.2026; правило RKNHardering «tun0 есть, но vpnActive=false»). Loopback-DNS вообще зависит от клиента.
- Протокол почти не влияет: все клиенты работают через VpnService/tun, поэтому VLESS, Hy2 и AWG выглядят одинаково.
- Per-app bypass снимает проверки по IP, гео и `activeNetwork`, но не по перечислению интерфейсов и сетей. По RKS, 13 из 30 приложений смотрят интерфейсы.
- Отдельный профиль (Shelter/work) от детекта не защищает: VPN основного профиля виден и из рабочего (GrapheneOS #7511). Помогают только роутер или другое устройство.
- Habr 1081580 — поиск строк в smali, а не проверка во время работы.
- Источники: [linkshield PR #60](https://github.com/AlexMos555/linkshield/pull/60), [RKNHardering](https://github.com/xtclovver/RKNHardering/blob/main/docs/README.en.md), [GrapheneOS #7511](https://github.com/GrapheneOS/os-issue-tracker/issues/7511), [RKS Global](https://rks.global/ru/research/vpn-detection/), [Habr 1081580](https://habr.com/ru/articles/1081580/).

**Классификация IP 🟡.** Три поля (страна, hosting, proxy) — упрощение для диагностики, а не модель решений сайта. Методичка Минцифры смотрит ещё ASN и репутационные списки VPN/proxy.
- Зарубежный VPS почти наверняка даёт не-RU и hosting. Флаг proxy — только если IP уже попал в репутационные списки, у свежего VPS он не гарантирован.
- RU-хостинг даёт country=RU, но DC-метку ставят 63% (ip-api) и 91% (ipapi.is). Это одна выборка: 14 RU-хостеров, 78 диапазонов, 2 базы, совпадение баз 56%. Флага proxy у RU-IP нет «пока»: репутационные базы со временем могут его поставить.
- Чистыми выглядят домашний и мобильный (CGNAT) IP.
- В RKNHardering RU-IP с hosting даёт NEEDS_REVIEW, а не DETECTED. DETECTED даёт зарубежный GeoIP при российском MCC, а также split tunnel (разные IP напрямую и через прокси, правило R1). То есть RU-вход по IP заметно помогает, но не убирает сигналы с устройства (TRANSPORT_VPN, tun).
- WARP — это AS13335 Cloudflare с геолокацией страны VPS, тоже признак VPN/прокси.
- Источники: [Habr 1067230](https://habr.com/ru/articles/1067230/), [RKNHardering](https://github.com/xtclovver/RKNHardering/blob/main/docs/README.en.md), [ruitunion 09.04](https://ruitunion.org/posts/2026-04-09-the-ministry-of-digital-developments-guidelines-in-simple-terms/).

**IPv6 при сплите 🟡 (исходное утверждение l-ipv6-leak опровергнуто, см. §6).** RU-списки GeoIP содержат IPv6: v2fly ru.txt около 10 006 IPv6 из 22 825 строк, Loyalsoldier и runetfreedom около 12 150 из 25 094, в том числе Яндекс `2a02:6b8::` (/29 или /32 в зависимости от списка) и VK `2a00:bdc0::/29`. Значит, `geoip:ru` ловит IPv6. Утечка через IPv6 возможна только в узком случае: сплит построен по самодельному списку только из IPv4 (как снимок каскада на 8626 записей в Habr 1067230) или `AllowedIPs` у AWG, либо клиент или туннель не маршрутизирует IPv6 по правилам. Источники: [v2fly ru.txt](https://raw.githubusercontent.com/v2fly/geoip/release/text/ru.txt), [Loyalsoldier ru.txt](https://raw.githubusercontent.com/Loyalsoldier/geoip/release/text/ru.txt), [Habr 1067230](https://habr.com/ru/articles/1067230/).

### 3.3 Регуляторика

| Мера | Суть | Статус |
|---|---|---|
| Методичка Минцифры (04.2026) | Разослана 20+ компаниям (РБК, 05–06.04.2026), срок до 15.04. Самой методички в открытом доступе нет, всё по пересказам. Три этапа: 1) сверка IP с RU-диапазонами и списками РКН, плюс ASN хостинга и репутационные списки; 2) проверка через своё приложение, на Android — флаги IS_VPN/TRANSPORT_VPN (ConnectivityManager/NetworkCapabilities) и dumpsys; 3) прочие ОС и десктоп, где проверяют интерфейсы tun/tap/wg/utun/ppp. Списки портов (SOCKS 1080/9000/5555/16000–16100, HTTP 80/443/3128/8080/8888, Tor 9050/9051/9150) есть только в утёкшем PDF (Хакер 08.04, подлинность не подтверждена). Порты сверяют с **системными настройками прокси на устройстве**, а не сканируют на удалённых серверах, так что порты VPS эта проверка не трогает. Нужно несколько признаков, одного недостаточно. iOS, роутер, VM, split и резидентные прокси названы источниками ошибок детекции. Новые VPN передавать в РКН | 🟡 (l-mintsifry-method). Вывод для клиента: не включать системный прокси и не держать открытый локальный SOCKS. [Meduza](https://meduza.io/news/2026/04/06/mintsifry-razoslalo-rossiyskim-kompaniyam-metodichku-po-poisku-vpn-na-ustroystvah-polzovateley-v-vedomstve-zayavili-chto-vyyavlenie-takih-servisov-v-ayfonah-problematichno), [SecurityLab](https://www.securitylab.ru/news/571257.php), [Хакер 08.04](https://xakep.ru/2026/04/08/vpn-checks/), [ntc 23842](https://ntc.party/t/%D0%BC%D0%B5%D1%82%D0%BE%D0%B4%D0%B8%D1%87%D0%BA%D0%B0-%D0%BC%D0%B8%D0%BD%D1%86%D0%B8%D1%84%D1%80%D1%8B-%D0%BF%D0%BE-%D0%B2%D1%8B%D1%8F%D0%B2%D0%BB%D0%B5%D0%BD%D0%B8%D1%8E-vpn/23842) |
| Требование к 20+ платформам | На закрытом совещании в конце марта 2026 (РБК 02.04) Минцифры потребовало от 20+ компаний (Сбер, Яндекс, VK, WB, Ozon, Avito, X5 и др.) к 15.04 ограничить доступ пользователям с VPN. Санкции: отзыв IT-аккредитации и льгот, исключение из белых списков и предустановки. Закона об этом нет. WB и Ozon начали раньше, около 07.04. К 28.04 часть сервисов откатила ограничения (см. §3.1). 27.04 Минцифры объяснило требование «безопасностью данных» и анонсировало в Max кнопку жалоб на ложные срабатывания на Госуслугах; запуск не подтверждён | 🟡 (l-platform-demand: holds + partly). [Фонтанка](https://www.fontanka.ru/2026/04/02/76345065/), [CNews](https://www.cnews.ru/news/top/2026-04-06_rossijskim_it-kompaniyam), [anti-malware 27.04](https://www.anti-malware.ru/news/2026-04-27-111332/49839) |
| Сбор адресных пулов операторов | Приказ РКН №51 от 28.02.2025 (Минюст 31.03.2025 №81700, основание 216-ФЗ, п. 5.2-1 ст. 46 закона «О связи»; в силе с 11/12.04.2025). Операторы передают выделенные абонентам адреса и диапазоны IPv4/IPv6 с привязкой к региону/муниципалитету и номеру ТСПУ. Изменения — в течение 1 дня (рабочего или календарного, пересказы расходятся), по запросу РКН — за 1 час. Это **карта адресных пулов, а не «IP конкретного абонента»**: РКН это прямо опроверг 26.05.2026. Уведомлены 1359 операторов (март 2026), к 21.05 наказаны 85. Связь с блокировками VPN — оценки СМИ и экспертов. Нового детектора зарубежного VPS отсюда не следует | 🟡 (l-ip-map). [Ведомости 26.05](https://www.vedomosti.ru/technology/news/2026/05/26/1200076-roskomnadzor-oproverg-soobscheniya), [Теплица](https://te-st.org/2026/05/27/collectip/), [telecomika](https://www.telecomika.ru/prikaz_51_roskomnadzora_o_sredstvah_svyazi), [MT](https://ru.themoscowtimes.com/2026/05/26/roskomnadzor-nachal-trebovat-u-operatorov-svyazi-ip-adresa-rossiyan-dlya-blokirovok-vpn-a196222) |
| 210-ФЗ «Антифрод 2.0» | Хостерам запрещено давать мощности под VPN по ст. 15.8 | 🟡 (s-hosting-law) |
| Проект «Антифрод 3.0» | Реестр, KYC, годовой бан у всех RU-хостеров, с 01.03.2028 | 🟡 (s-hosting-law) |
| Мониторинг хостеров (08.2026) | **Предложение, а не действующая норма**: текста письма, НПА и сроков нет. 03–04.08.2026 РБК (4 источника) сообщил, что Минцифры обсуждает с хостерами мониторинг **только IP из перечня исключений ЦМУ ССОП** (белый список корпоративных VPN). Если VPN-инфраструктура видна на таком IP неделю, хостер получает запрос и за 24 ч должен подтвердить назначение, иначе IP выводят из перечня. Клиентов делят по уровню идентификации: ЕСИА/ЕБС/договор юрлица — уведомление, телефон или карта — приостановка за 30 мин. 31.08 Минцифры письмом **попросило** (а не обязало) хостеров, CDN и анти-DDoS вынести IP белого списка в отдельные подсети, ФСБ поддержала. Обычный арендованный RU-VPS не в перечне, напрямую мера его не задевает. Реальный риск для RU-входа — упрощённая идентификация у хостера | 🟡 (l-hosting-monitoring). [MT 03.08](https://ru.themoscowtimes.com/2026/08/03/mintsifri-velelo-internet-operatoram-usilit-borbu-s-vpn-a202553), [MT 04.08](https://ru.themoscowtimes.com/2026/08/04/desyatki-vpn-servisov-perestali-rabotat-v-rossii-posle-novih-trebovanii-mintsifri-k-provaideram-a202667), [Ведомости 31.08](https://www.vedomosti.ru/technology/news/2026/08/31/1224824-mintsifri-poprosilo), [Коммерсантъ](https://www.kommersant.ru/doc/8922142), [hightech.fm](https://hightech.fm/2026/08/31/fsb-vpn) |
| Белый список корпоративных VPN | Ведёт ЦМУ ССОП при РКН. Заявка — на white_list@cmu.gov.ru: организация, ИНН, протокол, IP источника и назначения, цель («личный кабинет» упоминают только СМИ). На 22.04.2026, по РКН, — более 57 тыс. адресов и подсетей 1730 организаций. 75 тыс. — данные «Коммерсанта» на апрель 2025 в других единицах, цифры не противоречат друг другу. Список снимает только протокольную фильтрацию ТСПУ и на проверки VPN внутри приложений не влияет. «Технически отличить корпоративный VPN нельзя» — мнение экспертов, а не позиция РКН. Частному лицу неприменимо: нужны ИНН и раскрытие IP | 🟡 (l-corp-whitelist). [Habr 1026710](https://habr.com/ru/news/1026710/), [Интерфакс](https://www.interfax.ru/russia/1085410), [Хакер 2025](https://xakep.ru/2025/04/17/rkn-white-lists/), [cisoclub](https://cisoclub.ru/roskomnadzor-rasshiril-spisok-razreshjonnyh-korporativnyh-vpn-do-57-tysjach-adresov/) |
| Плата за международный трафик | Весной 2026 Минцифры (поручение Шадаева от 28.03) обсуждало плату за мобильный международный трафик сверх 15 ГБ/мес, около 150 ₽/ГБ, к 01.05. Срок сдвигали на 01.06 и на осень. 07.07 замминистра Лебедев заявил в Думе, что плата «не рассматривается». 23.09 Би-би-си по анонимным источникам: идея вернулась для 5G с порогом **50 ГБ**, обсуждалась дата 01.10, не утверждена. Глава «Вымпелкома» Анохин 02.04 говорил, что VPN «очень сложно» выделить в международном трафике; к 5G это не относится. На 04.10.2026 плата не введена. Довод «трафик до RU-входа внутренний» — гипотеза: метод учёта не опубликован | 🟡 (l-intl-traffic-fee). [Meduza 23.09](https://meduza.io/news/2026/09/23/bi-bi-si-v-pravitelstve-vernulis-k-idee-platy-za-mezhdunarodnyy-trafik-no-teper-v-seti-5g-v-rossii-ona-dostupna-tolko-vladeltsam-androidov), [Коммерсантъ](https://www.kommersant.ru/doc/8625432), [Habr 1056566](https://habr.com/ru/news/1056566/) |
| Ответственность пользователя | Пользоваться VPN законно. С 01.09.2025 (281-ФЗ) ст. 13.53 КоАП: 3–5 тыс. ₽ за умышленный поиск заведомо экстремистских материалов из списка Минюста и доступ к ним, «в том числе через VPN». Здесь VPN входит в состав правонарушения, а **не отягчает** его. Отягчающим VPN является только для преступлений: п. «ф» ч. 1 ст. 63 УК (282-ФЗ), суды его уже применяют. Реклама средств обхода (ч. 18 ст. 14.3 КоАП): граждане 50–80 тыс. ₽, должностные лица 80–150 тыс. ₽, юрлица 200–500 тыс. ₽. Ст. 13.52 КоАП штрафует владельцев средств обхода, не исполняющих требования РКН | 🟡 (l-legal). Для сервера: только для себя и близких, ничего не публиковать и не рекламировать. [Контур.Норматив 282-ФЗ](https://www.kontur-extern.ru/info/normativ/document/1/500800-federalnyy-zakon-ot-31-07-2025-n-282-fz), [alta.ru 281-ФЗ](https://www.alta.ru/tamdoc/25fz0281/), [Ведомости](https://www.vedomosti.ru/press_releases/2025/08/27/v-rossii-vveli-otvetstvennost-za-poisk-ekstremistskih-materialov-i-reklamu-vpn) |

**Волны IP-банов 🟡 (l-ip-waves).** Волны: 04.08.2026, 7–8.09, после выборов (рост с 21–22.09, пик 25.09). Банили и отдельные IP, и целые подсети хостеров; Теплица (30.09) пишет о блоках целых AS и подсетей с августа. Сбой с 07.09 по симптомам («туннель рвётся через секунды») похож скорее на DPI, чем на баны IP. Попадание self-hosted VPS и случай «из двух одинаковых серверов в одной /24 забанили один» известны по единичному сообщению на ntc.party (05.08), это анекдот, а не установленный факт. Прямой связи «приложение сообщило IP → бан» публично **не доказано**, есть только гипотезы экспертов (Козлюк, Meduza). Методичка, по пересказу SecurityLab, обязывает искать VPN, но прямо не требует передавать в РКН IP серверов. Эффективность серверных мер против банов не подтверждена; реально работают клиентский сплит для RU, RU-вход и быстрая ротация IP. Источники: [ntc 25528](https://ntc.party/t/%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-ip-%D0%B0%D0%B4%D1%80%D0%B5%D1%81%D0%BE%D0%B2-vpn-%D1%81%D0%B5%D1%80%D0%B2%D0%B5%D1%80%D0%BE%D0%B2-%D1%80%D0%BA%D0%BD-04082026/25528), [Meduza 04.08](https://meduza.io/feature/2026/08/04/nekotorye-servisy-soobschili-o-novoy-volne-blokirovok-vpn-v-rossii-ee-nazyvayut-odnoy-iz-krupneyshih-za-poslednee-vremya), [MT 29.09](https://ru.themoscowtimes.com/2026/09/29/posle-viborov-v-dumu-v-rossii-nachalas-volna-blokirovok-vpn-a207251), [Теплица 30.09](https://te-st.org/2026/09/30/vlessmore/).

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
| RU-сервис через туннель видит IP exit | `geoip:ru` + `category-ru` + `tld-ru` → `blocked`/`warp` на exit (Xray; Hy2 ACL и AWG ipset — opt-in) | ✅ для RU-назначений. Без клиентского сплита ломает RU-сайты, а сплит из подписки получают только Happ/INCY | Сплит «РФ напрямую», per-app исключение RU-приложений |
| Echo-сервисы (ipify и т.п.) через туннель | `category-ip-geo-detect` → `warp`/`blocked` (📄 165 доменов в runetfreedom; все 6 echo-URL MAX в списке) | 🟡 только для доменов из списка и только в Xray/Hy2 со sniffing. Не закрывает echo вне списка, IP-литералы, собственный бэкенд и STUN приложения, AWG (L3). Факт VPN не скрывает | RU-приложения direct или исключить из VPN — это главная защита |
| Открытый локальный SOCKS у клиента | `subHappLocalProxyAuth=auto` + `subHappAutoDetect=true` (только Happ, проверено на Android 4.4.1; 📄 + l-3xui-socks-auth). JSON-подписку не включать. Привязка JSON-подписки к 127.0.0.1 (v3.8.0) закрывает доступ из LAN, но не от приложений на том же устройстве | 🟡 только Happ | Happ: не включать «Allow LAN». Клиенты без локального прокси (WG/AWG, sing-box TUN без mixed и clash_api, Amnezia ≥5.0.3.0). Husi/SFA — длинный случайный пароль. v2rayNG 2.1.0: auth и случайный порт есть, но ломают HEV TUN |
| Утёкший exit-IP банят | Второй IP через `sendThrough` / WARP / второй VPS | 🟡 спасает от точечного бана, не от /24 | — |
| RU-приложения видят зарубежный IP | RU-вход + гео-сплит на входе (RU и echo → `direct` со входа) | ✅ IP становится российским, но это RU-хостинг с hosting-флагом (в RKNHardering — NEEDS_REVIEW вместо DETECTED) | Сплит |
| TRANSPORT_VPN, tun0, MTU, маршруты | — | ❌ | Роутер (с RU напрямую, иначе IP VPS светится на этапе 1 методички; защищает только дома), второй телефон. Island/Shelter прячет VpnService-API и список пакетов, но не `tun0`, `/proc/net/route` и loopback/SOCKS. Anubis (Shizuku, `pm disable-user`) замораживает RU-приложения, но им не приходят пуши, Shizuku надо перезапускать после ребута, а сам он может считаться признаком модификации |
| Список установленных VPN-клиентов | — | ❌ | Рабочий профиль, другое устройство |
| WebRTC ICE (интерфейсы, srflx) | — | ❌. Сервер это не закрывает: STUN на не-RU серверы (Google) всё равно отдаст IP exit, host-кандидат с tun уходит через сигнализацию, а отказ UDP/STUN сам служит признаком | Сплит RU-приложений в обход туннеля или через RU-вход |
| `/proc/net/route`, Android 16 QPR1+ UDP | — | ❌ | GrapheneOS ≥2026050400, `adb shell device_config put tethering close_quic_connection -1`, роутер |
| Контрольные закупки / утечка конфига | Не раздавать публично; подписка на случайном пути; внешних держать на отдельной ноде | ✅ | Не пересылать подписку |
| Отпечаток AWG | Уникальные Jc/Jmin/Jmax, S1–S4, H1–H4, I1 на установку | ✅ | Клиент ≥5.0.1.5 для 3.x |
| Active probing / сканирование панели | REALITY (уже есть); панель на 127.0.0.1; Hy2 masquerade | ✅ | — |
| Заморозка 16 КБ | Хостер вне засвеченных ASN; RU-вход; Hy2 как UDP-резерв | 🟡 | Смена транспорта |
| БС на мобильном | RU-вход на **белом** IP | 🟡 только при белом IP | — |

**Главный вывод.** Сервер закрывает **утечку адреса сервера** (частично: echo из списка и RU-назначения, только для Xray/Hy2) и **отпечаток**, но не **факт VPN на устройстве**. Последнее решается только на клиенте или роутере, и в CREDENTIALS нужно честно об этом написать. Допроверка сместила акцент ещё сильнее на клиента: главная защита от утечки exit-IP — RU-приложения напрямую или вне VPN, а серверные правила — страховка.

---

## 5. Что заложить в инсталлер

Ниже — почищенный черновик конфига. Проверен 2026-10-04 по исходникам 3x-ui тега `v3.9.0`, документации Xray и Hysteria2 и по самим файлам runetfreedom: .dat скачаны, теги разобраны. Знаком ❓ помечено то, что не удалось подтвердить. Значения `${...}` подставляет инсталлер.

### 5.0 Проверенные факты, на которые опирается конфиг (📄)

- **3x-ui v3.9.0** собран с Xray **v26.9.30** (`release.yml`, строка 118). Тарбол уже содержит `bin/geoip_RU.dat` и `bin/geosite_RU.dat`, это снимок runetfreedom на момент сборки.
  - Автообновления geo в 3x-ui нет: в `internal/web/job/*` такой задачи нет. Обновить можно только через меню `x-ui` → `update_geofiles "RU"`.
- **Шаблон Xray по умолчанию** лежит в `internal/web/service/config.json`.
  - Там уже есть `freedom.finalRules` с block `geoip:private`, а routing блокирует `geoip:private` и `bittorrent`.
  - Шаблон хранится в настройке `xrayTemplateConfig`, через API это `POST /panel/xray/update` с form-полем `xraySetting`.
- **Встроенный WARP 3x-ui:** `POST /panel/xray/warp/:action`, где action — `data|del|config|reg|changeIp|license|interval`. `reg` принимает ключи клиента в полях `skey`/`pkey` (`xray_setting.go`).
  - **API только регистрирует аккаунт.** Outbound с tag `warp` собирает фронтенд по кнопке (`buildWarpOutbound` в `WarpModal.tsx`): протокол `wireguard`, `noKernelTun: true`, `sockopt.domainStrategy`/`targetStrategy: "ForceIPv4v6"` (смена формата с 26.9.30, issue #5205). Инсталлер должен собрать этот outbound сам (l-warp-egress).
  - Issue #5205 (11.06.2026): WARP-outbound не поднимался на 3x-ui v3.3.0 с Xray 26.6.1, закрыт без фикса. Для v3.9.0 нужен smoke-test (§7).
  - Плановая смена IP WARP — `warp_ip_job.go`, интервал в днях.
- **Теги runetfreedom:**
  - geoip: `ru`, `private`, `ru-blocked`, `ru-whitelist`, `yandex`, `telegram`;
  - geosite: `category-ru` (1103), `tld-ru`, `category-gov-ru`, `category-bank-ru`, `category-ip-geo-detect` (165: `ipify.org`, `ifconfig.me`, `checkip.amazonaws.com`, `ip.mail.ru`, `ipv4-internet.yandex.net`, `2ip.ru`, `2ip.ua`, `ip-api.com` …), `ru-blocked`, `category-ads-all`, `win-spy`, `private`.
  - **Тега `ru` в geosite нет вообще** (1543 тега, RU среди них нет). Поэтому `geosite:ru` не грузится: ошибка конфига, а не нормализации регистра. Xray и сборщик v2fly оба переводят имена в верхний регистр (l-geo-lists-ops).
  - В `category-ip-geo-detect` нет голого `internet.yandex.net`, только `ipv4-`/`ipv6-internet.yandex.net`. Все 6 echo-URL из реверса MAX покрыты.
  - Прежняя пометка «`category-ru` не включает зону `.ru` целиком» **скорее всего ошибочна**: в v2fly `data/category-ru` первая строка `include:tld-ru` (проверено по master 04.10.2026), так считает и адверсарий по l-geo-lists-ops. `tld-ru` в правилах всё равно оставить явно: это безвредно и страхует от расхождений сборки.
  - Релизы runetfreedom выходят по cron примерно каждые 4–10 ч (номинально 6 ч).
- **CIDR для ipset:** `runetfreedom/russia-blocked-geoip/release/text/ru.txt`, 25 094 строки, из них 12 150 IPv6. Рядом лежит `ru-whitelist.txt`.
- **Hysteria app/v2.12.3** (16.09.2026) исправляет перехват исходящего UDP правилами port hopping.
  - Если не задать `acl.geoip`/`acl.geosite`, Hysteria при старте сама качает файлы Loyalsoldier, где нужных тегов нет. Пути задавать явно.
  - При своём пути `geoUpdateInterval` не действует (`geoloader.go`): обновлять файлы должен наш таймер с рестартом. Грузится только один geoip и один geosite. `geosite:ru` роняет старт (#1298), нужен `category-ru` (l-hy2-acl).
- **JSON-подписка** (`internal/sub/default.json`) кладёт клиенту SOCKS `127.0.0.1:10808` с `auth: noauth`. Это тот самый вектор утечки (§3.1).
- **Заголовки локального прокси для Happ** (PR #6628, влит 26.09.2026): `subHappLocalProxyAuth` принимает `auto` (по умолчанию), `disable` или пусто. Режимов `manual`/`from-json` в панели нет, хотя Happ их понимает. Заголовки `Socks-Auth-Mode`/`Http-Auth-Mode` уходят только при `subHappAutoDetect=true` и UA Happ (l-3xui-socks-auth).
- **Заголовок `Routing`** (`internal/sub/controller.go`, v3.9.0): `ApplyCommonHeaders` берёт его из `subRoutingRules` (если поле пустое — из `subJsonRoutingRules`) **для любого клиента**, `Routing-Enable` — при `subEnableRouting` (по умолчанию `false`). Правила INCY (`subIncyRoutingRules`) попадают только в тело ответа, а INCY читает заголовок: баг #6273 не исправлен. Заголовок понимают только Happ и INCY (l-routing-cost).

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

**Geo-файлы: runetfreedom.** 3x-ui уже их поставляет, релизы выходят примерно каждые 4–10 ч, все нужные теги на месте. roscomvpn отдавать **клиенту** в routing-заголовке (§5.5), а на сервере держать один набор.

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
| `RU_EGRESS=warp` | RU-сайты, которые режут диапазоны хостеров, у любого клиента работают. IP VPS RU-сервисам не виден | IP Cloudflare AS13335 с геолокацией страны VPS: зарубежный и известный как анонимайзер. Сервисы, требующие RU-IP, и антифрод по VPN-базам всё равно режут или требуют капчу. Без фолбэка при падении WARP RU-сайты перестают открываться. Внешняя зависимость |
| `ip-echo → warp` | Exit-IP не утекает через ipify и подобные | Если WARP упал, эти домены не отвечают. Через WARP пойдут и browserleaks, maxmind, httpbin и т.п. Расхождение IP (Cloudflare против RU-IP у direct-трафика клиента) само может быть признаком |
| `IPIfNonMatch` | Ловит RU-IP за не-RU доменами | DNS-запрос на каждое несовпавшее соединение. При недоступном DoH соединения висят до таймаута |
| geosite_RU.dat ≈ 74 МБ | Полные списки | ❓ ОЗУ и время старта на VPS с 1 ГБ не измерены |
| `bittorrent → blocked` | Меньше abuse-жалоб хостеру | Работает только при sniffing. Шифрованный BT (MSE) не ловится |

**Не закрывает:** открытый локальный SOCKS в связке с echo вне списка, `/proc/net/route`, RU-сервисы на зарубежных IP с не-RU доменом, собственный бэкенд и STUN приложения в туннеле, IP-литералы, флаг TRANSPORT_VPN. Это страховка, а не гарантия. Эффективность правила против MAX не измерена.

### 5.2 Cloudflare WARP: новая фаза `08-warp.sh`, opt-in `WARP_ENABLE=1`

**Вариант A (по умолчанию): встроенный WARP 3x-ui, только для Xray.**
```bash
kp="$("$XUI_DIR/bin/xray-linux-$ARCH" wg)"          # PrivateKey / PublicKey
api POST /panel/xray/warp/reg -F skey=... -F pkey=...   # имена полей по xray_setting.go
cfg="$(api POST /panel/xray/warp/config)"           # interface.addresses, peers[0], client_id
# reserved = байты base64(client_id), endpoint = peers[0].endpoint.host
api POST /panel/xray/warp/interval -F interval=7     # смена IP WARP раз в 7 дней
```
После этого подставить значения в outbound `warp` из §5.1 и повторить `update`. Сам outbound панель через API не создаёт (это делает только кнопка во фронтенде), поэтому инсталлер собирает его из шаблона §5.1. Перед включением `RU_EGRESS=warp` — smoke-test: `curl` через outbound на `cloudflare.com/cdn-cgi/trace` должен дать `warp=on` (WARP-outbound на v3.3.0 не поднимался, #5205).

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
- WARP не делает трафик «российским»: RU-сервис видит IP Cloudflare с геолокацией страны VPS. Это средство против утечки exit-IP и против блоков по диапазонам хостеров, а не замена сплиту или RU-входу.
- Свидетельств, что хостеры банят WARP, не найдено. Проблемы с узлом Cloudflare DME касаются WARP-клиентов из РФ, а не зарубежного exit (l-warp-egress).
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

- `ECHO_EGRESS_HY` принимает `reject` или `warp` и включён по умолчанию: RU-сайты он не ломает.
- `RU_EGRESS_HY` принимает `direct`, `reject` или `warp`. **По умолчанию `direct` (opt-in), изменено после l-routing-cost:** Hy2-клиенты не получают routing-заголовок из подписки, так что `reject(geoip:ru)` у пользователя без ручного сплита ломает банки и Госуслуги. Включать флагом, когда сплит настроен на клиенте. Без WARP outbound `warp` из списка убрать.
- Синтаксис `outbound(address[, proto/port])`, матчеры `geoip:`/`geosite:`/`suffix:`/`all` — по документации v2.hysteria.network. Правила проверяются сверху вниз, срабатывает первое совпадение; домен резолвится, и к нему применяются и IP-правила. HTTP-outbound UDP не пропускает, поэтому для `warp` только socks5 или `bindDevice`.
- `suffix:ru/su/xn--p1ai` дублируют `category-ru` (в нём есть `tld-ru`), это безвредно. Кириллическое «рф» в правилах бесполезно: SNI приходит в punycode.
- ❓ UDP/QUIC через `warp` (вариант A) не проверен: Xray socks-inbound с `udp:true` UDP ASSOCIATE умеет, но прогнать QUIC-сайт на стенде. Если не пройдёт — `reject` для UDP RU-назначений.
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

**Статус режимов (l-awg-ipset 🟡).** Источник паттерна, Habr 1056220 (07.07.2026), описывает только **RU-вход**: `mangle PREROUTING -i awg0 -m set --match-set ru dst -j RETURN`, остальное `MARK 0x1` → `ip rule fwmark 0x1 table 100` → `default dev awg1`. Exit там ничего не фильтрует. Режимы `block` и `warp` ниже на **exit** — наша экстраполяция, источником не подтверждена. Технически с kernel AWG (`-i awg0`) они работают. Задокументированная альтернатива для WARP на exit — вариант bivlked: BGP-фид antifilter + BIRD, таблица 200, `ip rule iif awg0`, без ipset/fwmark, только IPv4. **По умолчанию оба режима выключены (opt-in):** AWG-клиенты не получают routing-заголовок, и без `AllowedIPs`-сплита или per-app на клиенте `block` просто ломает RU-сайты. REJECT лучше DROP: нет таймаутов.

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
- Ловится только IP-назначение. Домены RU-сервисов на зарубежных CDN проходят мимо. DNS идёт через exit, поэтому RU-CDN может отдать зарубежный IP, и такой трафик уйдёт мимо ipset.
- Режим `warp` выше размечает только IPv4. Для IPv6 нужен аналог с `ip6tables` и `vz-ru6`, иначе v6 в туннеле отключить.
- Echo-правило (`category-ip-geo-detect`) для AWG неприменимо: это L3, доменов нет.
- Клиент с `AllowedIPs = 0.0.0.0/0` в режиме `block` ломает себе RU-сайты. «0.0.0.0/0 минус ru.txt» — это ~13k v4-сетей, а у мобильных клиентов есть лимит на число маршрутов. Реалистичнее рекомендовать сплит по приложениям.
- ❓ `-m set` с iptables-nft на Ubuntu 22.04/24.04 не проверен. Запасной путь — нативный `nft` set с `flags interval`.
- Сейчас `PostUp` использует только `iptables`. IPv6 FORWARD/NAT для AWG не настроен, хотя `ipv6.forwarding=1` включён (дефект [репо]). Либо отключить v6 в туннеле, либо дублировать правила.

### 5.5 Подписка: новая фаза `09-subscription.sh`, пишет settings через API

| Ключ 3x-ui v3.9.0 (📄 `setting.go`) | Значение | Эффект |
|---|---|---|
| `subUpdates` | `2` | `Profile-Update-Interval: 2` (по умолчанию 12) |
| `subTitle` | имя | `Profile-Title` |
| `subHappAutoDetect` | `true` | **Без этого Happ-заголовки не отдаются вообще** |
| `subHappLocalProxyAuth` | `auto` | `Socks-Auth-Mode`/`Http-Auth-Mode`: Happ сам ставит случайный пароль на локальный SOCKS/HTTP. Без Provider ID. Работает только вместе с `subHappAutoDetect`, проверено лишь на Happ Android 4.4.1 и desktop 4.3.0. Если в Happ включён «Allow LAN», он снимает пароль. Другие клиенты заголовок игнорируют |
| `subEnableRouting` + `subRoutingRules` | `true` + профиль roscomvpn DEFAULT (URL) | `Routing-Enable` + `Routing`. `subEnableRouting` по умолчанию `false`. HTTPS-URL кешируется 10 мин. Лимит: 16 KiB по коду (📄), адверсарий по l-routing-cost называет 8 КиБ на заголовок, поэтому держать профиль меньше 8 КиБ. Понимают **только Happ и INCY** |
| `subIncyRoutingRules` | тот же профиль, что в `subRoutingRules` | Обход бага #6273 (не исправлен в v3.9.0): INCY читает заголовок `Routing`, который берётся из `subRoutingRules` для любого клиента. Одинаковый профиль в обоих полях снимает расхождение |
| `subJsonEnable` | **`false` по умолчанию** | См. ниже |
| `subHappPerAppMode`/`List` | `exclude` + банки, MAX | `Per-App-Proxy-Mode: bypass` (Android). ⚠️ Вероятно, требует Provider ID и будет проигнорирован |
| путь подписки | случайный (v3.8.0+) | Защита от угадывания |
| домен | свой (под)домен, TTL 60–300 | Ротация IP без перевыпуска |

- Для v2RayTun отдавать `update-always: true`. Hy2 класть в подписку через external links (`hysteria2://`). AWG отдавать отдельным файлом.
- **JSON-подписка и балансер (конфликт s-client-balancer ↔ утечка):** `default.json` открывает клиенту SOCKS без пароля. Варианты:
  - (а) **рекомендуется:** по умолчанию отдавать raw + Happ, JSON-подписку не включать;
  - ~~(б) переписать `noauth` на `password` через nginx `sub_filter` на `/json/`~~ — **снято после допроверки** (l-3xui-socks-auth, l-client-auth-ports). Пароль и порт получатся одинаковыми на всех, а не случайными на устройство, и RKNHardering подбирает слабые и известные пароли. Happ применит их только в режиме `from-json`, которого панель не отдаёт. tun2socks в v2rayNG ходит на локальный порт без auth (v2rayN #6981), так что своя socks-auth в JSON-шаблоне ломает VPN-режим v2rayNG;
  - (в) завести issue в 3x-ui: случайные логин, пароль и порт на выдачу.
  - Балансер (он есть только в JSON-подписке) не включать по умолчанию до фикса в апстриме. ❓ Перекрывает ли Happ в режиме `auto` значение `noauth` из JSON, не проверено.
- Значения `Socks-Auth-Mode`: в доках Happ `auto|manual|from-json|disable`, панель отдаёт `auto`, `disable` или пусто. URL Happ-профиля roscomvpn брать из README `hydraponique/roscomvpn-routing`; для INCY нужен отдельный deeplink.

**Текст для CREDENTIALS (`99-print-creds.sh`)**
1. Клиенты: Happ, v2RayTun, на iOS при отсутствии Happ — INCY/Karing, на Android — v2rayNG. Добавлять **подписку**, автообновление включить. Для XHTTP нужно ядро клиента ≤v26.7.28 или сборка с http2legacy.
2. **Сплит обязателен:** профиль «РФ напрямую». Без него RU-сайты не откроются (`blocked`) или пойдут через Cloudflare (`warp`). Happ и INCY получают профиль из подписки автоматически. В v2rayNG, Hy2-клиентах и AWG сплит настраивается вручную (geosite/geoip `category-ru`/`ru` → direct; для AWG — `AllowedIPs` или per-app), иначе серверная страховка для них по умолчанию выключена.
3. Android: исключить банки, Госуслуги, MAX, маркетплейсы. **Это прячет только IP, а не факт VPN.** Исключённое приложение не видит TRANSPORT_VPN своей сети, но видит `tun0`, VPN-сеть в списке сетей и установленные VPN-клиенты. Детект VPN есть у 22 из 30 топ-приложений, к 16.04 — у всех 30. Полностью прячет только второй телефон или роутер с RU напрямую. Рабочий профиль (Shelter/Island) не спасает.
4. Локальный прокси: в Happ не включать «Allow LAN» (иначе пароль снимается), сервер сам отдаёт `Socks-Auth-Mode: auto`. Лучше клиенты без локального прокси: WireGuard/AmneziaWG, Amnezia ≥5.0.3.0, sing-box TUN без mixed и clash_api. Где пароль задаётся вручную (Husi, SFA) — длинный случайный, UDP на локальном прокси выключить. В v2rayNG 2.1.0 auth и случайный порт есть, но с HEV TUN ломают трафик. Обновлять клиент.
5. Режим TUN, а не системный прокси.
6. Порядок транспортов при проблемах: Reality-TCP → XHTTP → Hy2 → AWG. «Wi-Fi не работает, LTE работает» значит менять транспорт, а не сервер.
7. Подписку и ключи никому не пересылать. Сервер — для себя и близких: не публиковать и не рекламировать (ч. 18 ст. 14.3 и ст. 13.52 КоАП).
8. Печатать ASN сервера (`curl ipinfo.io/org`) с предупреждением, если он из засвеченных (Hetzner AS24940, Contabo AS51167, DO, OVH, Amazon). Подавать как подсказку: список устаревает. Дать ссылку на чекер «TCP 16-20» и попросить прогнать его из РФ до раздачи.
9. Android 16 QPR1+ на стоковой прошивке: при желании закрыть UDP-утечку мимо VPN командой `adb shell device_config put tethering close_quic_connection -1` (GrapheneOS ≥2026050400 уже исправлен).

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
- Риски: ToS RUVDS/Beget/NTX, 210-ФЗ, проект «Антифрод 3.0», ТСПУ в ДЦ, KYC и СОРМ на входе. По обсуждаемой схеме Минцифры (08.2026) клиента с упрощённой идентификацией (телефон, карта) могут приостановить за 30 мин, поэтому RU-хостера выбирать, понимая риск привязки к личности.

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
| `05-hysteria2.sh` | `--version app/v2.12.3`, команда обновления, `sniff`/`acl`/`outbounds`, masquerade proxy; echo-правило по умолчанию, RU-egress opt-in | [версия]/[репо] |
| `06-amneziawg.sh` | Уникальные параметры 3.x, v6-правила; ipset + PostUp (RU-egress) только opt-in | [репо] |
| **`09-subscription.sh`** (новая) | Настройки §5.5 (включая `subEnableRouting=true` и одинаковый профиль в `subRoutingRules`/`subIncyRoutingRules`), external link Hy2 | новое |
| `99-print-creds.sh` | Текст §5.5, ASN, чекер, предупреждения | [репо] |
| `scripts/entry.sh`, `probe.sh`, `vpnzoo-export/import` | §5.6 | новое |

### 5.8 Приоритеты

**P0. Дефекты и безопасность (день-два)**
1. Панель на 127.0.0.1 + SSH-туннель. Сейчас она открыта по HTTP наружу.
2. Фаза 04: API вместо INSERT по старой схеме, `XRAY_BIN` по `$ARCH`, sniffing.
3. Пины: 3x-ui v3.9.0 (проверить на стенде, иначе v3.8.5), Hy2 app/v2.12.3 + отдельная команда обновления. Список клиентов в README.
4. Уникальные параметры AWG на установку. Если 3.x, то с предупреждением о клиенте ≥5.0.1.5.
5. Подписка: случайный путь, `subUpdates=2`, `subHappAutoDetect=true` (без него `subHappLocalProxyAuth` не работает), `subHappLocalProxyAuth=auto`, `subEnableRouting=true` + routing-профиль «РФ напрямую» в `subRoutingRules` **и** `subIncyRoutingRules` (баг #6273), **JSON-подписка выключена**, переписывание `noauth` через `sub_filter` не делать.
6. Серверный RU-egress (`07`) для Xray: `ru-blocked→direct`, `ip-echo→blocked|warp`, `RU→blocked|warp`. Идёт только вместе с п.5, а п.5 доставляет сплит **только Happ и INCY**. Поэтому для Hy2 по умолчанию включено только echo-правило, а `RU_EGRESS_HY` и AWG ipset — opt-in флагами, с инструкцией по ручному сплиту в CREDENTIALS.

**P1. Живучесть**
7. `vpnzoo-export`/`import` + автобэкап наружу.
8. Домен с TTL 60–300 для подписки и узлов + хук смены IP.
9. Второй inbound XHTTP+Reality на альтернативном порту с другим SNI.
10. RealiTLScanner по /24 при установке; предупреждать о .ru/Apple SNI.
11. Печать ASN + ссылка на чекер «TCP 16-20» в `99-print-creds.sh`.
12. Hy2: masquerade proxy, port hopping как опция.

**P2. Опции**
13. WARP (`08`) — вариант A. Outbound собирает инсталлер (API панели только регистрирует), включать `warp`-режимы после smoke-test на v3.9.0.
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

Целиком опровергнуто одно утверждение: **l-ipv6-leak** (оба адверсария, первая строка таблицы). Остальное ниже — **части** утверждений, которые адверсарии опровергли. Это то, что нельзя писать в README и CREDENTIALS.

| Миф | Почему неверно |
|---|---|
| «Ни одна крупная GeoIP-база не относит российский IPv6 к RU, `geoip:ru` не ловит IPv6 RU-сервисов» (l-ipv6-leak, **опровергнуто целиком**) | RU-списки v2fly, Loyalsoldier и runetfreedom содержат 10–12 тыс. IPv6-CIDR, включая Яндекс `2a02:6b8::` и VK `2a00:bdc0::/29`. В Habr 1067230 замера по GeoIP-базам нет: там самодельный снимок каскада без IPv6. Утечка возможна только при самодельном IPv4-списке, `AllowedIPs` у AWG или если IPv6 не маршрутизируется по правилам |
| «Сплит прячет VPN от банков» | Исключённое приложение не видит TRANSPORT_VPN своей сети, но видит `tun0`, VPN-сеть в `getAllNetworks()` и список VPN-клиентов (RKS Global, linkshield PR #60). Сплит прячет только IP |
| «Рабочий профиль (Shelter/Island) прячет VPN» | Прячет VpnService-API и список пакетов, но не `tun0`, `/proc/net/route` и loopback/SOCKS. VPN основного профиля виден из рабочего (GrapheneOS #7511) |
| «Почти все xray/sing-box-клиенты держат SOCKS без пароля» | Названы конкретные клиенты. Husi, SFA и Xray (saeeddev94) позволяли задать пароль ещё до раскрытия, HTTP-прокси в раскрытии не задокументирован |
| «Детекторы сканируют весь localhost» | Подтверждено только для открытого тестера RKNHardering. Методичка называет конкретные порты, и то для сверки с настройками прокси на устройстве, а не скана |
| «На iOS sandbox не даёт сканировать loopback» | iOS-приложения могут слушать локальные порты и подключаться к ним. Happ iOS, V2BOX, Karing в списке уязвимых |
| «IceCandidate — основной HIGH-признак у VK, Т-Банка, Сбера, Госуслуг, Avito» | HIGH — сумма 15+ совпадений по 8 литералам в smali, разбивки по приложениям нет. Литерал есть в любом SDK звонков |
| «Серверный блок STUN-назначений прикрывает WebRTC» | STUN на не-RU серверы отдаст IP exit, host-кандидат с tun уходит через сигнализацию, а отказ STUN — сам признак |
| «`geosite:ru` не грузится из-за нормализации тега» | Тега RU в geosite.dat нет вообще. Xray и v2fly оба приводят имена к верхнему регистру |
| «Баг INCY в 3x-ui был» | Есть и в v3.9.0: #6273 закрыт как not planned, заголовок `Routing` берётся из Happ-правил для любого клиента |
| «3x-ui сам создаёт outbound warp» | API только регистрирует аккаунт. Outbound строит фронтенд по кнопке, инсталлеру — собирать самому |
| «WARP на exit — RU-сайты работают у любого клиента» | IP Cloudflare с геолокацией страны VPS. Сервисы, требующие RU-IP, и антифрод по VPN-базам всё равно режут |
| «Хостеры банят WARP», «DME мешает exit» | Банов не найдено. Проблема DME касается WARP-клиентов из РФ |
| «Подписка с RU-direct снимает цену серверного RU-блока» | Заголовок `Routing` понимают только Happ и INCY. Клиенты v2rayNG, Hy2 и AWG его не получают |
| «На exit AWG рабочий паттерн ipset → DROP/WARP» (по Habr 1056220) | Статья описывает только RU-вход. Exit-режимы — наша экстраполяция |
| «19 приложений шлют статус, включая все банки», «TRANSPORT_VPN 17 по RKS» | Подтверждены три банка выборки (Т-Банк, Сбер, ВТБ). Цифра 17 — из Meduza, на странице RKS её нет. 19 — из 22 детектирующих, у The Bell 18 |
| «Банки не пускали пользователей с VPN» | Не подтверждено: ComNews у Т-Банка уведомлений не нашёл |
| «Минцифры потребовало от хостеров» (31.08), «мониторинг всех IP хостера» | Письмом «попросило». Недельный мониторинг касается только IP из перечня исключений ЦМУ ССОП, и это пока обсуждение |
| «РКН собирает карту IP абонентов» | Это адресные пулы с регионом и ID ТСПУ, без привязки к абоненту. РКН опроверг 26.05.2026 |
| «Методичка сканирует порты 1080/8080, не держать их на VPS» | Порты сверяют с системными настройками прокси на устройстве. Внешние порты VPS не проверяют |
| «VPN — отягчающее обстоятельство для штрафа 3–5 тыс. ₽» | В ст. 13.53 КоАП VPN входит в состав правонарушения. Отягчает только преступления (п. «ф» ч. 1 ст. 63 УК) |
| «Плата за международный трафик: 100–150 ₽/ГБ, для 5G тот же порог» | Около 150 ₽/ГБ. Для 5G порог 50 ГБ, по анонимным источникам. 07.07 Минцифры отрицало, что плата рассматривается |
| «Из двух одинаковых серверов в /24 забанили один» как установленный факт | Единичное сообщение на форуме. Сбой с 07.09 похож скорее на DPI, чем на баны IP |
| «Android 16 UDP-утечка — у всех Android 16, выдаёт только факт VPN» | Только QPR1+ и при включённом флаге DeviceConfig. Утечка связывает реальный IP с exit-IP. Есть ADB-митигация |
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
- [ ] Поднимается ли WARP-outbound на 3x-ui v3.9.0 / Xray v26.9.30 (на v3.3.0 не поднимался, #5205): `warp=on` в `cdn-cgi/trace`.
- [ ] UDP/QUIC из Hy2 через `warp` (socks5 → Xray `hy2-egress`).
- [ ] `*.dat.sha256sum` как release asset.

**Подписка и клиенты**
- [ ] `Socks-Auth-Mode: auto` на стенде: ставит ли Happ пароль без Provider ID, на iOS и на свежих версиях Android (проверено только 4.4.1), перекрывает ли он `noauth` из JSON-подписки.
- [ ] Реальный лимит заголовка `Routing` в 3x-ui v3.9.0: 16 KiB (📄) или 8 КиБ (голос по l-routing-cost).
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
- [ ] Видят ли исключённые из туннеля приложения `tun0` через `getNetworkInterfaces` на реальном устройстве (пока данные с эмулятора Android 15).

**RU-вход**
- [ ] Проходит ли UDP-проброс Hy2/AWG через RU-вход при БС.
- [ ] Белый ли IP у выбранного RU-хостера (`ru-whitelist.txt` + открыть :443 с мобильного без VPN).
- [ ] Дата вступления в силу пункта 210-ФЗ о хостинге по первичному тексту.

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
  - WARP: https://github.com/ViRb3/wgcf, https://github.com/bivlked/amneziawg-installer/blob/main/WARP-RU.md
  - прочее: https://github.com/legiz-ru/my-remnawave, https://github.com/petrochen/xray-double-hop, https://github.com/Leadaxe/sing-box-lx/issues/32

**Допроверка компрометаторов: ключевые новые источники**
- 3x-ui: PR #6628 https://github.com/MHSanaei/3x-ui/pull/6628, `internal/sub/controller.go` v3.9.0 https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/sub/controller.go, issue #5205 https://github.com/MHSanaei/3x-ui/issues/5205, `WarpModal.tsx` https://raw.githubusercontent.com/MHSanaei/3x-ui/main/frontend/src/pages/xray/overrides/WarpModal.tsx
- Клиенты: v2rayNG 2.1.0 https://github.com/2dust/v2rayNG/releases/tag/2.1.0, v2rayNG #5382 https://github.com/2dust/v2rayNG/issues/5382, v2rayN #6981 https://github.com/2dust/v2rayN/issues/6981, Amnezia PR #2456 https://github.com/amnezia-vpn/amnezia-client/pull/2456, #2457 https://github.com/amnezia-vpn/amnezia-client/issues/2457, PR #3199 https://github.com/amnezia-vpn/amnezia-client/pull/3199
- Android: linkshield PR #60 https://github.com/AlexMos555/linkshield/pull/60, GrapheneOS #7511 https://github.com/GrapheneOS/os-issue-tracker/issues/7511, Android 16 QUIC: https://github.com/0x33c0unt/quic-vpn-bypass, https://mullvad.net/en/blog/2026/5/12/any-app-on-recent-android-versions-can-leak-certain-traffic, https://leewoobin.com/posts/android-quic-vpn-lockdown-exception/
- Hysteria: issues #1298 https://github.com/apernet/hysteria/issues/1298, #1486 https://github.com/apernet/hysteria/issues/1486, `geoloader.go` https://raw.githubusercontent.com/apernet/hysteria/master/app/internal/utils/geoloader.go
- Geo: Xray `rule_parser.go` https://raw.githubusercontent.com/XTLS/Xray-core/main/common/geodata/rule_parser.go, v2fly `category-ru` https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/category-ru, v2fly/Loyalsoldier `ru.txt` https://raw.githubusercontent.com/v2fly/geoip/release/text/ru.txt, https://raw.githubusercontent.com/Loyalsoldier/geoip/release/text/ru.txt
- Регуляторика: Ведомости 26.05 https://www.vedomosti.ru/technology/news/2026/05/26/1200076-roskomnadzor-oproverg-soobscheniya, Ведомости 31.08 https://www.vedomosti.ru/technology/news/2026/08/31/1224824-mintsifri-poprosilo, Хакер 08.04 https://xakep.ru/2026/04/08/vpn-checks/, Интерфакс https://www.interfax.ru/russia/1085410, 282-ФЗ https://www.kontur-extern.ru/info/normativ/document/1/500800-federalnyy-zakon-ot-31-07-2025-n-282-fz
- Habr: 1056220 (AWG + ipset на RU-входе), 1021392, 1026710 (`/ru/news/`), 1056566 (`/ru/news/`)
