# Сбой 23.10.2026: массовое или точечное?

> Срез 24.10.2026. Рой: 5 поисковиков + агент измерений (Globalping, check-host, RIPEstat) + сводка; сырые результаты — [raw/](raw/). Окно внешних замеров 20:07–21:12 UTC 23.10 совпало с установкой zoo на этот сервер (`install.sh --force`, фазы 03–99): x-ui забран, панель 2053 закрыта и переведена на 127.0.0.1, VLESS перенесён с 8443 на 443/tcp, accel-ppp (SSTP) отключён. Отсюда «странности» портов 8443/2053 после 20:35 UTC — это не сбой. Дополнительные замеры после роя — в конце документа.

## Вердикт

- **Заблокирован один конкретный IP <old-ip>, по всей России и у разных провайдеров.** Уверенность высокая. Его режут домашние линии в Екатеринбурге, Нижнем Тагиле, Нижнем Новгороде, Костроме, Москве, Уфе и Новосибирске. Операторы разные: Ростелеком, МТС, Билайн, Izet, GalaxyStar (сидит на ЭР-Телекоме). Пакеты пропадают на краю сети провайдера, там, где стоит ТСПУ. Соседний адрес <old-net>.150 из той же /24 с тех же проб и в ту же минуту открывается в 40 случаях из 40.
- **Это не блок подсети, не блок хостера, не Урал и не обновление hysteria.** Уверенность высокая.
- **«Поста» не было.** О новой волне против VPN за 22–24.10 не нашлось ничего: ни новостей, ни объявлений РКН, ни нового постановления, ни тредов на ntc.party, net4people или GitHub. Всё, что датировано этими днями, касается мобильного интернета, YouTube/GGC и Akamai. Оговорка: события вечера 23-го могли ещё не дойти до форумов, обычно это занимает 1–3 дня.
- **Скорее всего, это обычный бан IP в кампании, которая идёт с августа.** Основание у РКН такое: на адресе выявлено «использование средств обхода блокировок», п.15 Правил из ПП №127. Такие баны выходят пачками и без анонсов. На пачку, а не на персональный бан, указывают два факта: сервер подруги упал в тот же день, а за час замеров фильтр закрыл ещё 3 сети дата-центров. Уверенность средняя.

## Что показали измерения из РФ

Внешние пробы (Globalping, check-host) снимались 23.10 с 20:07 до 21:12 UTC. Это 22:07–23:12 по серверу и 01:07–02:12 24.10 по Екатеринбургу. Ниже `.107` — твой сервер, `.150` и `.100` — соседи из <old-net>.0/24.

### Основные замеры

| Точка | ASN | Цель | Тест | Результат |
|---|---|---|---|---|
| Екб, твоя домашняя линия (~20:00 UTC) | AS12389 Ростелеком | .107 | TCP 22/443/8443/2053, UDP, ICMP, tcpdump на сервере, traceroute | Все таймауты. На сервер пакеты не приходят. Трасса обрывается после 94.51.110.254 (узел РТК в Екб) |
| Нижний Тагил, твоя линия | AS31499 Планета | .107 | подключение клиентом | Не работает (с твоих слов, деталей нет) |
| Сервер, входящий UDP | все сети РФ | udp/* | что приходит на сервер | С 13:17 CEST (11:17 UTC, 16:17 Екб) из РФ не пришло ни одного пакета |
| Сервер, SSTP | AS31499 Планета, 213.151.31.252 | tcp/443 | активная сессия | Сессия жива, трафик идёт |
| Екб | AS41925 Izet Telecom Ural | .107 | ICMP, TCP 443, TCP 22, mtr | Везде 0/3. mtr: 147.45.113.1 → 87.249.216.165 → обрыв |
| Нижний Тагил | AS206873 GalaxyStar (апстрим ЭР-Телеком) | .107 | ICMP, TCP 443, TCP 22, mtr | Везде 0/3. mtr: 185.173.176.1 → обрыв |
| Н. Новгород, Кострома | AS12389 Ростелеком | .107 | ICMP, TCP 443, mtr | 100% потерь, трасса обрывается на 1–2 хопе внутри РТК |
| Новосибирск, 2 пробы | AS8359 МТС | .107 | ICMP, TCP 443 | 100% потерь |
| Уфа | AS3216 Билайн | .107 | ICMP, TCP 443 | 100% потерь |
| Москва | AS42610 НКС | .107 | ICMP, TCP 443 | 100% потерь |
| Москва | AS25513 МГТС | .107 | TCP 8443 (~20:16 UTC) | 100% потерь |
| Москва | AS3216 Билайн | .107 | ICMP, TCP 443 | **Исключение:** ICMP 3/3, TCP 2/3 |
| 40 дата-центров РФ (Мск, СПб, Нск, Томск, Кемерово, Хабаровск, Владивосток, Тамбов, Таганрог, Обнинск) | разные | .107 | TCP 443 (21:09 UTC) | 35 из 40 со 100% потерь. Проходят TIME-HOST Мск 3/3, Adman Нск 2/3, Happy Tech Мск 1/3, Beget Мск 1/3 |
| Те же дата-центры, 20:08 и 21:09 UTC | Selectel СПб AS49505, GIS Нск AS207713, Misaka Хабаровск | .107 | TCP 443 | В 20:08 проходили, в 21:09 уже 0/3: фильтр расширялся прямо во время замеров |
| 20 дата-центров РФ | разные | .107 | ICMP | 12 из 20 проходят: на путях дата-центров ICMP часто пропускают, а режут только TCP |
| check-host ru1/ru2, Москва | AS14576, AS210644 | .107 | TCP 22/443/8443/2053; ICMP | TCP таймаут в обоих раундах, ICMP 4/4 |
| check-host ru3, СПб | AS210644 | .107 | TCP 22/443/8443 | **Исключение:** соединяется в обоих раундах |

### Контроли

| Точка | ASN | Цель | Тест | Результат |
|---|---|---|---|---|
| Те же пробы РФ (40, 20, 7 и 2 штуки) | разные | .150, тоже VM u1host | TCP 443, ICMP, mtr | TCP 443: 40/40, 20/20, 7/7, 2/2. ICMP: 20/20, 7/7, 2/2. mtr доходит за 9–10 хопов через те же РТК и ЭР-Телеком до AS49418 |
| check-host ru1/ru2 | AS14576, AS210644 | 16 живых соседей в /24 | TCP 443/22 | 14 из 16 открываются, таймаут только у .107 и .100 |
| 7 домашних проб, Екб, Тагил | разные | .100, другая VM u1host | TCP 443 | 100% потерь, включая Билайн Мск; из Варшавы 3/3. Это ещё один забаненный адрес, когда его забанили, неизвестно |
| Те же домашние пробы | разные | 1.1.1.1 | ICMP | 0% потерь, сами пробы исправны |
| check-host ru1–ru3 | разные | 1.1.1.1:443, 9.9.9.9:443 | TCP | Проходят, зарубежный трафик в целом не режут |
| NL, DE, PL, KZ, BY, TR, US | Hetzner, LeaseWeb, Oracle, G-Core, Beltelecom и др. | .107 | TCP 22/443 | 0% потерь (5–150 мс), сервер жив |
| Ростелеком, Кострома | AS12389 | u1host.com, 64.188.114.188:443 (AS49418) | TCP | 0% потерь, апстрим хостера не режут |
| RIPE RIS, 163 пира, включая Москву через AS12389 | разные | <old-net>.0/24 | видимость в BGP | Префикс виден 163 пирам из 163, маршрут цел |
| antifilter.download (выгрузка от 22.10) | — | .107 и его /24 | реестр РКН | Не найдены. Так и должно быть: блокировки VPN на ТСПУ в публичный реестр не попадают |

### Что пришлось выкинуть

- **Порт 8443 после 20:35 UTC.** На сервере в это время менялось состояние портов. 443 какое-то время отвечал refused, 2053 открывался, а после 20:50 8443 и 2053 молчали даже из NL и DE. Поэтому данные по 8443 после 20:35 негодны. Замеры по 443 и 22 в 21:07–21:12 годны: из-за рубежа в те же минуты всё открывалось.
- **Проверка UDP через check-host.** Hysteria не отвечает на случайный UDP, поэтому таймаут везде, включая NL. О блокировке это ничего не говорит.
- **Контрольные серверы** 72.4.71.232 (u1host FI) и 185.121.12.185 (твой второй VPS) не отвечали ниоткуда, включая NL и DE.
- **Контроль 193.0.6.139:443** в одном из прогонов дал 100% потерь со всех дата-центров РФ. TCP с дата-центров из того прогона я не учитываю. Вместо него работает парный контроль через .150.
- **ping-admin.ru** не дал данных: форма требует JavaScript и не отработала.
- **Планету снаружи проверить нельзя:** в AS31499, у ЭР-Телекома и у МегаФона нет проб Globalping.

### Про живую сессию с Планеты

Её видно с самого сервера: в соединениях на tcp/443 висит адрес 213.151.31.252, и по нему идёт трафик. Внешними пробами это не проверить. Твоя линия в Тагиле тоже на Планете, и новые соединения с неё не проходят. Значит, вероятнее всего, эта сессия поднялась до бана, и фильтр её не трогает. Есть и второй вариант: этот узел Планеты ещё не получил адрес в список. Различить их можно только переподключением, но тогда сессию, скорее всего, потеряешь. Картину это не меняет. Неполное покрытие видно и по другим исключениям: check-host ru3 СПб, TIME-HOST Мск, Билайн Мск.

## Что пишут в сети (по датам)

### Фон: август–сентябрь

- **04.08.** Хостер VirtualDC официально заявил о массовой блокировке IP через ТСПУ; пострадали несколько крупных хостеров [офиц./новости]. Видел только сниппет из поиска. [vdc.ru](https://news.vdc.ru/2026/08/04/o-massovoj-blokirovke-ip-adresov-tspurkn-oficzialnaya-pozicziya-kompanii/)
- **04.08 и 21.09.** Две волны массовых банов IP у популярных VPS-хостеров, в том числе серверов с Reality и даже с одним AmneziaWG. У Firstbyte заблокировано около 3 тыс. адресов из 29 тыс., у Veesp 5 тыс. из 30 тыс., у Play2go 2 тыс. из 37 тыс. [github] [net4people #671](https://github.com/net4people/bbs/issues/671)
- **18.08.** Сервис Xeovo пишет, что серверы Hysteria2 в РФ снова блокируют и что это «targeted IP blocking», а не детект протокола [форум, блог сервиса]. [xeovo](https://hub.xeovo.com/posts/211-hysteria2-servers-are-blocked-in-russia-again)
- **21.09.** Статья на Хабре: адреса сканируют (active probing) и банят IP, на которых нашли средства обхода. Трассировка до такого адреса обрывается внутри сети провайдера, как у тебя. Среди примет назван веб-сервер на нестандартном порту вроде 2096, это порты панелей семейства x-ui [офиц./новости; это наблюдения автора, а не регламент]. [habr](https://habr.com/ru/articles/1084862/)
- **29.09.** SecurityLab: публичных данных о едином механизме ограничений для всех операторов нет [офиц./новости]. [securitylab](https://www.securitylab.ru/news/578067.php)
- **30.09.** Разбор «Теплицы»: с августа блокируют целые подсети хостеров, раскатывают неравномерно по провайдерам. Собственные тесты авторов с российских узлов IP-блоков не нашли [офиц./новости]. [te-st.org](https://te-st.org/2026/09/30/vlessmore/)
- **30.09.** Реестр Минцифры с запретом хостинга для тех, кто размещает VPN, пока только проект [офиц./новости]. [habr](https://habr.com/ru/news/1088742/)

### 1–19 октября

- **02.10.** На тестовом VPS исследователя TCP заблокирован по всей РФ, а UDP и ICMP проходят [github]. [net4people #671](https://github.com/net4people/bbs/issues/671)
- **06.10.** Ответ ДЦОА (мониторинг РКН) на запрос об адресе: ограничение «в связи с выявлением использования средств обхода блокировок» по п.15 Правил из ПП №127 от 2020 года. Нового постановления для этого не понадобилось [форум, цитата официального ответа]. [ntc 17013](https://ntc.party/t/%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-cloudflare-ovh-hetzner-digitalocean-09062025-xxxxxxxx/17013)
- **~10.10.** В Xray-core как общее знание: QUIC к зарубежным IP в РФ обычно режется. Это не новость [github]; видел только сниппет. [Xray #7115](https://github.com/XTLS/Xray-core/issues/7115)
- **13.10.** ТСПУ таргетит uTLS-отпечаток Chrome 133. Соединения с ним виснут, а после 3–4 параллельных соединений включается заморозка на 120 с. Это DPI по отпечатку, не бан IP [github]. [Xray #7131](https://github.com/XTLS/Xray-core/issues/7131)
- **14.10.** «Массовые перебои VPN» по данным Downdetector, без протоколов и провайдеров [офиц./новости]. [rambler](https://news.rambler.ru/tech/57235541-masshtabnyy-sboy-v-rabote-vpn-servisov-proizoshel-v-rossii/)
- **14.10.** В Свердловской области мобильный интернет работал только по белым спискам, плюс жалобы на домашний интернет с 01:00 [офиц./новости]. [e1](https://www.e1.ru/text/gorod/2026/10/14/76699344/), [66.ru](https://66.ru/society/6518152-v-sverdlovskoy-oblasti-upal-mobilnyy-internet/), [realtagil](https://realtagil.ru/news/sverdlovchane-zhaluyutsya-na-sboj-interneta-14-oktyabrya/)
- **15–16.10.** «Новая волна, блокируют целые подсети хостеров» [офиц./новости, вторичка]. Первоисточник — Telegram-каналы techmedia и «Кровавая барыня», хостеры не названы. [vgtimes](https://vgtimes.ru/tech-and-hardware/171165-v-rossii-novaya-volna-blokirovok-vpn-pod-udar-popali-celye-podseti-hosting-provayderov.html), [mentoday](https://www.mentoday.ru/life/experience/vpn-v-rossii-teryayut-effektivnost-novaya-volna-blokirovok-nakryla-stranu/)
- **15.10.** VPS из #671 разблокировался сам примерно через 2 недели после того, как владелец перестал к нему ходить [github]. [net4people #671](https://github.com/net4people/bbs/issues/671)
- **17–18.10.** На Firstbyte «не менее трети IP наглухо забанены». VLESS Reality у знакомого на 62yun (DE): сначала закрыли 443, потом 22, при этом «на Ростелеком Мск не попала». Ещё один бан держался 3+ недели и снялся только сменой IP [форум]. [ntc 25993](https://ntc.party/t/25993)
- **19–20.10.** Recorded Future предупреждает о новых ограничениях. Это прогноз, а не факт [офиц./новости]. [mentoday](https://www.mentoday.ru/technics/news/20-10-2026/poka-rabotaet-no-nadolgo-li-rossiyan-predupredili-o-novoi-volne-blokirovok-vpn/)
- **19–23.10.** В Екб у абонентов Планеты, РТК и МТС ломался YouTube, 23.10 сам восстановился («на ТСПУ что-то протестили и откатили»). VPN это не касается [github]. [Flowseal #18292](https://github.com/Flowseal/zapret-discord-youtube/issues/18292)

### 20–24 октября

- **21.10.** **Одиночный отчёт:** неделю дропаются крупные UDP-пакеты AWG при росте трафика, автор подозревает фильтрацию всего UDP. Провайдер не указан, ему возразили, что это может быть перегрузка [форум]. [ntc 25993](https://ntc.party/t/25993)
- **21.10.** Mentoday: 20.10 VPN не работали 2–8 часов, и РКН якобы «массово блокирует диапазоны IP» по данным «Коммерсанта». Первоисточник (Коммерсант от 16.10) о диапазонах не пишет, **не подтверждено** [офиц./новости]. [mentoday](https://www.mentoday.ru/technics/news/21-10-2026/v-rossii-nachinaetsya-totalnaya-zachistka-vpn-blokirovkoi-zaimetsya-iskusstvennyi-intellekt/)
- **21–22.10.** Akamai режут после 16–20 КБ, украинские диапазоны Akamai/Arelion закрыты на ТТК и ростовском Дом.ру, а на региональном РТК нет. Только TCP, VPS-хостеров не касается [форум]. [ntc 17013/1403](https://ntc.party/t/17013/1403)
- **22.10.** Сайт IONOS не открывается, автор сам не уверен, что это РКН [форум]. [ntc 4625](https://ntc.party/t/%D1%87%D0%B8%D1%82%D0%B0%D0%B9%D1%82%D0%B5-1-%D1%81%D0%BE%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D0%B5-%D1%81%D0%BE%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D1%8F-%D0%BE%D0%B1-%D0%BE%D1%82%D0%B4%D0%B5%D0%BB%D1%8C%D0%BD%D1%8B%D1%85-%D0%BF%D0%BE%D0%B4%D1%82%D0%B2%D0%B5%D1%80%D0%B6%D0%B4%D1%91%D0%BD%D0%BD%D1%8B%D1%85-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0%D1%85-%D1%82%D1%81%D0%BF%D1%83/4625?page=51)
- **22.10.** На Сбой.рф жалоб на AdGuard VPN 108 против 47 накануне, 65% из Москвы, Свердловская область 3%. Краудсорс, причина неизвестна, сигнал слабый [измерение]. [сбой.рф](https://xn--90aqok.xn--p1ai/adguard-vpn-com)
- **22.10.** Сбои мобильного интернета в десятках регионов, включая Свердловскую область, работают белые списки. Только мобильная связь [офиц./новости]. [info24](https://info24.ru/news/pochemu-ne-rabotaet-mobilnyj-internet-segodnya-22-oktyabrya-gde-sboi-chto-delat.html)
- **22.10.** Hysteria #1710 с тем же симптомом (клиентских пакетов в логе сервера нет), но это Китай (+08:00), а не РФ [github]. [hysteria #1710](https://github.com/HyNetworks/hysteria/issues/1710)
- **22–23.10.** Режут Google GGC, в том числе на узле МегаФона svx2. svx — вероятно, Екатеринбург, но в посте город не назван. К VPN отношения не имеет [форум]. [ntc 26445](https://ntc.party/t/%D1%81-%D0%BF%D0%BE%D0%BD%D0%B5%D0%B4%D0%B5%D0%BB%D1%8C%D0%BD%D0%B8%D0%BA%D0%B0-2026-%D0%BE%D0%BA%D1%82-19-%D0%BD%D0%B0-%D1%82%D1%82%D0%BA-%D0%BF%D0%BE%D0%BB%D0%BE%D0%B2%D0%B8%D0%BD%D0%B0-%D1%80%D0%BE%D0%BB%D0%B8%D0%BA%D0%BE%D0%B2-%D0%BD%D0%B0-%D1%8E%D1%82%D1%83%D0%B1%D0%B5-%D0%BE%D1%82%D0%B2%D0%B0%D0%BB%D0%B8%D0%B2%D0%B0%D0%B5%D1%82%D1%81%D1%8F%E2%80%A6/26445)
- **23.10.** На мобильном интернете перебор SNI и параллельные запросы приводят к тому, что все соединения перестают отвечать до перезапуска сети. Это поведение мобильного ТСПУ [форум]. [ntc 26426](https://ntc.party/t/%D0%BE%D0%B1%D1%8B%D1%87%D0%BD%D1%8B%D0%B5-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B8-%D0%BD%D0%B0-%D0%BC%D0%BE%D0%B1%D0%B8%D0%BB%D1%8C%D0%BD%D0%BE%D0%BC-%D0%B8%D0%BD%D1%82%D0%B5%D1%80%D0%BD%D0%B5%D1%82%D0%B5-%D0%BD%D0%B5-%D0%B1%D0%B5%D0%BB%D1%8B%D0%B5-%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B8/26426)
- **23.10.** Мобильный интернет ограничен примерно в 40 регионах (по другой сводке в 25), эпицентр в Москве и СПб. Свердловская область не названа, проводной интернет работал [офиц./новости]. [mentoday](https://www.mentoday.ru/life/news/23-10-2026/moskva-i-peterburg-v-liderah-po-otklyucheniyu-mobilnogo-interneta-chto-izvestno-o-massovyh-sboyah-seti-23-oktyabrya/), [hi-tech.mail](https://hi-tech.mail.ru/news/145610-sboi-interneta-v-rossii/)
- **23.10.** На detector404 по Свердловской области есть жалобы на мобильный и домашний интернет, но без провайдеров и без VPN [измерение, краудсорс]. [detector404](https://detector404.ru/66-sverdlovskaya-oblast)
- **23.10.** Разговоры про «новую волну РКН» в zapret-трекере: Telegram на Дом.ру. Сосед на том же операторе возражает [github, спекуляция]. [Flowseal #18321](https://github.com/Flowseal/zapret-discord-youtube/issues/18321)
- **23.10.** «VLESS Reality отваливается всё чаще» — это предложение фичи, а не отчёт о сбое [github]. [igareck #389](https://github.com/igareck/vpn-configs-for-russia/issues/389)
- **23.10.** Рейтинг доступности хостингов за 30 дней: у u1host (AS213877) из РФ недоступны 19 адресов из 42. Банят поштучно, AS целиком не закрыт. У Aeza 65 из 109, у VDSINA 60 из 106 [измерение]. [roskompozor](https://roskompozor.ru/hosting-rating/)
- **23–24.10.** Массового сбоя Ростелекома в Екб нет: на Сбой.рф 55 жалоб 23.10 против 305 накануне, rt-podkluchit пишет «Работает стабильно». Это ожидаемо, если режут один зарубежный IP [измерение]. [rt-podkluchit](https://rt-podkluchit.ru/ekb/downdetector/)

### Чего не нашли (22–24.10)

- **ntc.party:** за 20–24.10 нет новых тем о массовом отказе VPN, UDP, QUIC или Hysteria. Поиск «hysteria» после 19.10 даёт ноль постов, «vless» и «заблокировали» после 21.10 тоже ноль. За 24.10 постов в индексе пока нет. [ntc latest](https://ntc.party/latest), [поиск](https://ntc.party/search.json?q=hysteria%20after%3A2026-10-19)
- **net4people:** с 16 по 24.10 нет ни одного нового или обновлённого issue с меткой Russia. [label:Russia](https://github.com/net4people/bbs/issues?q=label%3ARussia%20sort%3Aupdated-desc)
- **Трекеры** hysteria, Xray-core, 3x-ui, amnezia-client, hiddify, sing-box: с 15 по 24.10 нет отчётов о блокировках в РФ, только обычные баги. [3x-ui](https://github.com/MHSanaei/3x-ui/issues?q=is%3Aissue%20sort%3Acreated-desc)
- **Федеральные Telegram-каналы** (Коммерсант, РБК, Ведомости, ТАСС, Медуза, Код Дурова, ЗаТелеком, Кровавая барыня, Baza и др.) с 20.10 до ~20:00 UTC 23.10 о новой волне не писали. Оговорка: полный бан вечером случился позже их последних постов. [kommersant](https://t.me/s/kommersant), [zatelecom](https://t.me/s/zatelecom)
- **Законодательство:** постановлений, вступающих в силу 22–24.10, нет, есть только проекты.
- **u1host:** в Telegram-канале с 04.07 тишина, за октябрь ничего. В итогах 2025 года хостер сам упоминал «блокировки подсетей и отдельных IP». [t.me/s/u1host](https://t.me/s/u1host)
- **Местные СМИ:** E1, URA.RU, «Екатеринбург ЧП», 4 канал и «Новости Хакасии» за 20–23.10 ничего не писали о домашнем интернете, Ростелекоме, Планете или VPN. [e1_news](https://t.me/s/e1_news), [uranews](https://t.me/uranews/130034), [abakan_news](https://t.me/s/abakan_news)

### Отчёты «работает»

- Hysteria #1683 (UDP-блок на мобильном, хостер Aeza) закрыт как not planned. Единственный ответ: «hy2 работает и на мобильном, и на домашнем, смените хостинг» [github, 27.09]. [hysteria #1683](https://github.com/HyNetworks/hysteria/issues/1683)
- На ntc за 22–24.10 жалоб на Hysteria и VLESS нет совсем. Это слабый аргумент против волны, но отсутствие жалоб на главном форуме кое-что значит [форум].
- 23.10 на ntc одни пишут, что «внезапно заработал дефолтный фейк», другие — что блокировки «наоборот усилили». Это про zapret, а не про VPN, и сигнал противоречивый [форум]. [ntc 726/9861](https://ntc.party/t/726/9861)
- Агрегаторы показывают, что Ростелеком в Екб работает штатно [измерение].

## Версии и их вес

**(а) Массовая волна ТСПУ против UDP/QUIC: вес низкий.**
- За: с 13:17 по серверу из РФ не приходит UDP; 21.10 был один отчёт про дропы AWG; QUIC к зарубежным IP давно режется.
- Против: вечерний бан закрыл ещё TCP 22/443/8443 и ICMP. При этом у соседнего .150 TCP с тех же проб проходит, и UDP-волна такое не объясняет. Если бы 23.10 массово отвалились Hysteria и AWG, ntc и трекер hysteria были бы завалены жалобами, а там пусто.
- Что остаётся: дневная фаза «только UDP» могла быть первой стадией того же бана IP, когда сначала закрыли UDP, а вечером всё остальное. Снаружи это не различить, проверка UDP неинформативна.

**(б) Блок IP, подсети или хостера: IP подтверждён, подсеть и хостер опровергнуты.**
- IP: уверенность высокая, это видно по всем замерам выше.
- /24: опровергнуто. .150 и 14 из 16 живых соседей открываются, кроме тебя забанен только .100.
- Хостер AS213877: опровергнуто. Соседи живы, сайт u1host и апстрим AS49418 доступны. Рейтинг показывает поштучные баны: 19 адресов из 42 за 30 дней.
- Неравномерность по провайдерам — нормальная картина. Так же было с 62yun (бан на одних сетях, но не на РТК Мск) и с Akamai (ТТК режет, а региональный РТК нет).

**(в) Региональные ограничения на Урале и белые списки: вес очень низкий.**
- Бан одинаковый в Н. Новгороде, Костроме, Москве, Уфе и Новосибирске, а не только на Урале.
- Белые списки 14.10 и 22–23.10 касались мобильного интернета, а твои линии проводные.
- Местные СМИ и агрегаторы по РТК Екб молчат.
- ТСПУ в Екб в эти дни трогали YouTube и откатили это 23.10, к VPN это не относится.
- Важная деталь: Планета с 2020 года — дочка Ростелекома ([comnews](https://www.comnews.ru/content/212262/2020-12-17/2020-w51/rostelekom-priobrel-veduschego-internet-provaydera-ekaterinburga-i-sverdlovskoy-oblasti)). Поэтому Тагил на Планете не был независимой проверкой. Независимую проверку дали Izet в Екб и GalaxyStar в Тагиле, и обе сети режут.

**(г) «Спалил коллега из Хакасии»: вес низкий, но исключить нельзя.**
- Бан ставят за то, что на IP выявили средства обхода, а детект идёт по трафику любого пользователя. Мобильный ТСПУ агрессивнее проводного (ntc, 23.10), так что небольшая вероятность есть.
- Против: после его последнего захода (23.10, 03:41 по серверу) всё работало ещё около 9,5 часа. Сервер подруги он не видел, а тот упал в тот же день. По Хакасии за 17–23.10 нет ни одного сигнала.
- Привязать бан к конкретному клиенту снаружи нельзя, и таких данных не будет.

**(д) Обновление hysteria: ноль.**
- Пакеты не доходят до сервера, значит софт на сервере ни на что не влияет.
- Флаг `urgent` только пишется в лог, по коду [update.go](https://raw.githubusercontent.com/apernet/hysteria/master/app/cmd/update.go) от него ничего не зависит.
- В [v2.13.0](https://github.com/HyNetworks/hysteria/releases/tag/app/v2.13.0) нет security-фиксов и изменений протокола.
- Отдельно, к бану не относится: 2.8.1 старше security-релиза 2.9.2 от 23.05 (обход UDP ACL, OOM в sniff). Обновиться стоит.

## Что делать дальше

1. **Переехать на новый IP: это и есть лечение, на сервере чинить нечего.** Можно остаться у u1host: соседи по /24 живы, хостер целиком не забанен. Но учти, что их адреса банят часто (19 из 42 за 30 дней), так что переезды, вероятно, будут повторяться.
2. **Старый IP не выбрасывать сразу.** Неделю-три не подключайся к нему из РФ и проверяй. Иногда бан снимается сам примерно через 2 недели ([net4people #671](https://github.com/net4people/bbs/issues/671)), иногда держится больше 3 недель ([ntc 25993](https://ntc.party/t/25993)). Если оживёт, будет запасным.
3. **Убрать поводы для бана на новом IP:**
   - Панель 3x-ui (2053) и подписки не открывать наружу. Панель держать на localhost, ходить через SSH-туннель. Открытые порты панелей на форумах и на Хабре называют триггером.
   - Основной протокол — VLESS Reality на tcp/443. Для SNI не брать google, apple или yandex: на форуме это называют возможным триггером, но это не подтверждено. Xray и клиенты должны быть свежими, без закреплённого старого отпечатка chrome133 ([Xray #7131](https://github.com/XTLS/Xray-core/issues/7131)).
   - Hysteria2, AWG и SSTP держать запасными и по возможности на отдельном IP, чтобы детект одного протокола не утянул основной. На этом сервере первым умер UDP, а у подруги тоже упала именно Hysteria.
   - Hysteria обновить до 2.13 вместе с клиентами.
4. **Выдавать ключи поштучно**, отдельного клиента в 3x-ui на каждого человека. Заодно по логам будет видно, кто и из каких сетей ходил перед баном.
5. **Проверить сейчас:**
   - живы ли 8443 и 2053 на текущем сервере (`systemctl status x-ui`). После 20:50 UTC они не отвечали даже из NL и DE. Возможно, это последствие перенастройки в тот момент, но проверь;
   - второй VPS 185.121.12.185, он не отвечает ниоткуда.
6. **Спросить у подруги хостер и IP** и прогнать её адрес тем же парным тестом: её IP, сосед из той же /24 и 1.1.1.1 с российских домашних проб. Если её адрес забанен так же, а соседи живы, это та же пачка.
7. **Через 1–3 дня перепроверить** ntc ([4625](https://ntc.party/t/%D1%87%D0%B8%D1%82%D0%B0%D0%B9%D1%82%D0%B5-1-%D1%81%D0%BE%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D0%B5-%D1%81%D0%BE%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D1%8F-%D0%BE%D0%B1-%D0%BE%D1%82%D0%B4%D0%B5%D0%BB%D1%8C%D0%BD%D1%8B%D1%85-%D0%BF%D0%BE%D0%B4%D1%82%D0%B2%D0%B5%D1%80%D0%B6%D0%B4%D1%91%D0%BD%D0%BD%D1%8B%D1%85-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0%D1%85-%D1%82%D1%81%D0%BF%D1%83/4625?page=51), [25993](https://ntc.party/t/25993)) и [net4people #671](https://github.com/net4people/bbs/issues/671). Если там появятся массовые жалобы за 23.10, версия про пачку укрепится.
8. **В следующий раз проверять так же:** Globalping с российских домашних проб, всегда парой «свой IP и сосед из той же /24», плюс 1.1.1.1. check-host ru3 (СПб) не показатель: он пробивал даже сейчас.

## Источники

**Собственные измерения**
- Globalping: [2VsOByBHo2detEV8100021N5s](https://api.globalping.io/v1/measurements/2VsOByBHo2detEV8100021N5s), [2giv075ul8jNrDjfX00021N5q](https://api.globalping.io/v1/measurements/2giv075ul8jNrDjfX00021N5q), [2FsCbdiwttDbcPTjZ00021N5q](https://api.globalping.io/v1/measurements/2FsCbdiwttDbcPTjZ00021N5q), [2NP3paoC0z5xPb1RM00021N5j](https://api.globalping.io/v1/measurements/2NP3paoC0z5xPb1RM00021N5j), [2dSarUTDlO7f5w8xV00021N5k](https://api.globalping.io/v1/measurements/2dSarUTDlO7f5w8xV00021N5k), [2oYwRvM36ASPVhJfI00021N6i](https://api.globalping.io/v1/measurements/2oYwRvM36ASPVhJfI00021N6i)
- check-host.net (результаты в API, ссылок нет)
- RIPEstat: [prefix-overview](https://stat.ripe.net/data/prefix-overview/data.json?resource=<old-ip>), [routing-status](https://stat.ripe.net/data/routing-status/data.json?resource=<old-net>.0/24)
- [antifilter.download ip.lst](https://antifilter.download/list/ip.lst)

**Агрегаторы**
- [roskompozor: рейтинг хостингов](https://roskompozor.ru/hosting-rating/)
- [rt-podkluchit Екб](https://rt-podkluchit.ru/ekb/downdetector/)
- [Сбой.рф, AdGuard VPN](https://xn--90aqok.xn--p1ai/adguard-vpn-com)
- [detector404, Свердловская обл.](https://detector404.ru/66-sverdlovskaya-oblast)

**Форумы**
- ntc.party: [latest](https://ntc.party/latest), [поиск hysteria](https://ntc.party/search.json?q=hysteria%20after%3A2026-10-19), [25993](https://ntc.party/t/25993), [17013, ответ ДЦОА](https://ntc.party/t/%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-cloudflare-ovh-hetzner-digitalocean-09062025-xxxxxxxx/17013), [17013/1403](https://ntc.party/t/17013/1403), [4625](https://ntc.party/t/%D1%87%D0%B8%D1%82%D0%B0%D0%B9%D1%82%D0%B5-1-%D1%81%D0%BE%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D0%B5-%D1%81%D0%BE%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D1%8F-%D0%BE%D0%B1-%D0%BE%D1%82%D0%B4%D0%B5%D0%BB%D1%8C%D0%BD%D1%8B%D1%85-%D0%BF%D0%BE%D0%B4%D1%82%D0%B2%D0%B5%D1%80%D0%B6%D0%B4%D1%91%D0%BD%D0%BD%D1%8B%D1%85-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0%D1%85-%D1%82%D1%81%D0%BF%D1%83/4625?page=51), [26445](https://ntc.party/t/%D1%81-%D0%BF%D0%BE%D0%BD%D0%B5%D0%B4%D0%B5%D0%BB%D1%8C%D0%BD%D0%B8%D0%BA%D0%B0-2026-%D0%BE%D0%BA%D1%82-19-%D0%BD%D0%B0-%D1%82%D1%82%D0%BA-%D0%BF%D0%BE%D0%BB%D0%BE%D0%B2%D0%B8%D0%BD%D0%B0-%D1%80%D0%BE%D0%BB%D0%B8%D0%BA%D0%BE%D0%B2-%D0%BD%D0%B0-%D1%8E%D1%82%D1%83%D0%B1%D0%B5-%D0%BE%D1%82%D0%B2%D0%B0%D0%BB%D0%B8%D0%B2%D0%B0%D0%B5%D1%82%D1%81%D1%8F%E2%80%A6/26445), [26426](https://ntc.party/t/%D0%BE%D0%B1%D1%8B%D1%87%D0%BD%D1%8B%D0%B5-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B8-%D0%BD%D0%B0-%D0%BC%D0%BE%D0%B1%D0%B8%D0%BB%D1%8C%D0%BD%D0%BE%D0%BC-%D0%B8%D0%BD%D1%82%D0%B5%D1%80%D0%BD%D0%B5%D1%82%D0%B5-%D0%BD%D0%B5-%D0%B1%D0%B5%D0%BB%D1%8B%D0%B5-%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B8/26426), [726/9861](https://ntc.party/t/726/9861)
- [Xeovo: Hysteria2 blocked again](https://hub.xeovo.com/posts/211-hysteria2-servers-are-blocked-in-russia-again)

**GitHub**
- [net4people #671](https://github.com/net4people/bbs/issues/671), [net4people label:Russia](https://github.com/net4people/bbs/issues?q=label%3ARussia%20sort%3Aupdated-desc)
- hysteria: [#1683](https://github.com/HyNetworks/hysteria/issues/1683), [#1710](https://github.com/HyNetworks/hysteria/issues/1710), [v2.13.0](https://github.com/HyNetworks/hysteria/releases/tag/app/v2.13.0), [update.go](https://raw.githubusercontent.com/apernet/hysteria/master/app/cmd/update.go)
- Xray-core: [#7131](https://github.com/XTLS/Xray-core/issues/7131), [#7115](https://github.com/XTLS/Xray-core/issues/7115)
- [3x-ui issues](https://github.com/MHSanaei/3x-ui/issues?q=is%3Aissue%20sort%3Acreated-desc)
- igareck: [#388](https://github.com/igareck/vpn-configs-for-russia/issues/388), [#389](https://github.com/igareck/vpn-configs-for-russia/issues/389)
- Flowseal: [#18292](https://github.com/Flowseal/zapret-discord-youtube/issues/18292), [#18321](https://github.com/Flowseal/zapret-discord-youtube/issues/18321)
- [ClusterM/vpn-how-to #20](https://github.com/ClusterM/vpn-how-to/issues/20)

**Официальное и новости**
- [VirtualDC, 04.08](https://news.vdc.ru/2026/08/04/o-massovoj-blokirovke-ip-adresov-tspurkn-oficzialnaya-pozicziya-kompanii/), [te-st.org](https://te-st.org/2026/09/30/vlessmore/), [Хабр: active probing](https://habr.com/ru/articles/1084862/), [Хабр: реестр Минцифры](https://habr.com/ru/news/1088742/), [SecurityLab](https://www.securitylab.ru/news/578067.php)
- [Рамблер 14.10](https://news.rambler.ru/tech/57235541-masshtabnyy-sboy-v-rabote-vpn-servisov-proizoshel-v-rossii/), [VGTimes 15.10](https://vgtimes.ru/tech-and-hardware/171165-v-rossii-novaya-volna-blokirovok-vpn-pod-udar-popali-celye-podseti-hosting-provayderov.html)
- Mentoday: [16.10](https://www.mentoday.ru/life/experience/vpn-v-rossii-teryayut-effektivnost-novaya-volna-blokirovok-nakryla-stranu/), [20.10](https://www.mentoday.ru/technics/news/20-10-2026/poka-rabotaet-no-nadolgo-li-rossiyan-predupredili-o-novoi-volne-blokirovok-vpn/), [21.10](https://www.mentoday.ru/technics/news/21-10-2026/v-rossii-nachinaetsya-totalnaya-zachistka-vpn-blokirovkoi-zaimetsya-iskusstvennyi-intellekt/), [23.10](https://www.mentoday.ru/life/news/23-10-2026/moskva-i-peterburg-v-liderah-po-otklyucheniyu-mobilnogo-interneta-chto-izvestno-o-massovyh-sboyah-seti-23-oktyabrya/)
- [hi-tech.mail 23.10](https://hi-tech.mail.ru/news/145610-sboi-interneta-v-rossii/), [info24 22.10](https://info24.ru/news/pochemu-ne-rabotaet-mobilnyj-internet-segodnya-22-oktyabrya-gde-sboi-chto-delat.html)
- Свердловская область, 14.10: [e1](https://www.e1.ru/text/gorod/2026/10/14/76699344/), [66.ru](https://66.ru/society/6518152-v-sverdlovskoy-oblasti-upal-mobilnyy-internet/), [realtagil](https://realtagil.ru/news/sverdlovchane-zhaluyutsya-na-sboj-interneta-14-oktyabrya/)
- [ComNews: РТК купил Планету](https://www.comnews.ru/content/212262/2020-12-17/2020-w51/rostelekom-priobrel-veduschego-internet-provaydera-ekaterinburga-i-sverdlovskoy-oblasti)

**Telegram**
- [u1host](https://t.me/s/u1host), [kommersant](https://t.me/s/kommersant), [zatelecom](https://t.me/s/zatelecom), [e1_news](https://t.me/s/e1_news), [uranews](https://t.me/uranews/130034), [abakan_news](https://t.me/s/abakan_news)

## Дополнительные замеры после роя (24.10, ~21:00–21:30 UTC 23.10)

| Точка | Цель | Тест | Результат |
|---|---|---|---|
| RUVDS Екб (AS214822), tcpdump на сервере | <old-ip> | TCP SYN 22/80/443/1194/5000/8080/26440/28737/31880/40000/51820/60000, UDP 53/123/443/500/1194/4500/26440/28737/29724/51820/60000, ICMP | До сервера доходит **только ICMP** (сервер отвечает). Ни одного TCP/UDP-пакета |
| RUVDS Екб | <old-net>.2, 88.198.248.254 (Hetzner) | TCP 443 | Открыты |
| RUVDS Екб | <old-ip> | TCP traceroute :443 | Обрыв после 139.45.243.4 (3-й хоп) |
| Globalping, 8 узлов РФ (Timeweb, Aeza, Misaka, GIS и др.) | **<srv-v6>** (IPv6 сервера) | TCP 443, ICMPv6 | **0% потерь**: IPv6 не заблокирован, в т.ч. из Timeweb, где IPv4 режется |
| Globalping NL/DE | IPv6 сервера | TCP 443 | 0% потерь (контроль) |
| Домашний Wi-Fi владельца (Ростелеком Екб) | — | глобальный IPv6 | Нет (только fe80::), воспользоваться лазейкой нельзя |

Вывод: бан — на IPv4-адрес, все TCP/UDP-порты, ICMP пропускается из ДЦ-сетей. IPv6 того же сервера доступен из РФ [измерение]; это временная лазейка, а не решение.

Отдельно [среда]: на старом сервере x-ui слушал 2053 и 2096 наружу — именно порты панелей x-ui Хабр (21.09) называет приметой при active probing. В zoo панель только на 127.0.0.1.
