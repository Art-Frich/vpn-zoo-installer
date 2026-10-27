# Tailscale и Headscale как ещё один протокол в зоопарке (срез 27.10.2026)

Повод: 27.10.2026 у знакомого владельца с сервера в Риге из РФ пропал AmneziaWG, а Tailscale продолжил работать. Вопрос: стоит ли добавить Tailscale или Headscale в зоопарк для офиса на 33 человека.

Метки в документе:
- **[факт]** — первоисточник (документация, релиз, issue, наш замер с датой);
- **[наблюдение]** — отчёты пользователей и блогов (такие отчёты обычно смещены в негатив, и среда автора неизвестна);
- **[гипотеза]** — наш вывод, не проверенный на стенде.

Если вывод зависит от оператора или региона, это сказано рядом.

## Коротко

- **Облачный Tailscale (tailscale.com) для офиса из РФ не подходит.** С октября 2024 года сама компания отвечает российским IP кодом 451: вход, панель, координационный сервер, по отчётам также DERP и раздача пакетов. Свидетельств, что это отменили, за 2025–2026 нет. Это ограничение со стороны Tailscale, а не РКН. [наблюдение, согласованное из нескольких источников]
- **iOS-приложения Tailscale нет в российском App Store.** Наш замер 27.10.2026: `itunes.apple.com/lookup?id=1470499037&country=ru` вернул 0 результатов, для US и KZ приложение есть. Для сравнения, AmneziaWG и Telegram в RU-витрине есть. [факт]
- **Headscale** — свой координационный сервер для официальных клиентов Tailscale. Проект живой: v0.29.6 вышла 24.10.2026. Но он остаётся в ветке 0.x и сам себя называет решением «для личного использования и небольших организаций». Поддерживает Android, iOS, Windows и macOS, есть встроенный DERP. [факт]
- **Что значит «Tailscale работал, когда AWG упал».** Tailscale сначала пробует прямой UDP с обычным WireGuard. Если прямой путь не проходит, трафик идёт через DERP-реле: TLS поверх TCP 443. Скорее всего, у знакомого трафик шёл именно через DERP, то есть по TCP. Но конфигурация у него неизвестна: облако или Headscale, где стоят exit node и DERP. [гипотеза]
- **Для нашей модели «только выбранные приложения через VPN» Tailscale подходит хуже текущих протоколов:**
  - режим «только эти приложения» есть только на Android и только через MDM;
  - на Windows и iOS выбора по приложениям нет вообще;
  - путь через DERP медленнее прямого.

  [факт] по документации, скорость — [наблюдение].
- **Рекомендация.** Как основной протокол для 33 человек не добавлять. Если нужен ещё один резервный путь по TCP, можно сделать **экспериментальный модуль Headscale со встроенным DERP и exit node на тестовом VPS** и прогнать короткий тест. Тест описан в §5. Решение принимать по замерам, а не по одному случаю в Риге.

## 1. Как Tailscale соединяет устройства

**Три компонента** [факт]:

| Компонент | Что делает | Транспорт |
|---|---|---|
| Координационный сервер (control plane: `login.tailscale.com`, `controlplane.tailscale.com`, либо свой Headscale) | Вход, выдача ключей, список узлов, ACL, карта DERP | HTTPS (TCP 443) |
| DERP-реле | Помогает установить связь и служит «последним» маршрутом, когда прямой не получается. Пересылает уже зашифрованные WireGuard-пакеты и не может их расшифровать | HTTPS (TCP 443), плюс HTTP 80 и STUN UDP 3478 |
| Узлы (клиенты) | Сеть WireGuard между собой | Прямой UDP; при неудаче — peer relay или DERP |

Источники: [DERP servers](https://tailscale.com/kb/1232/derp-servers), [Connection types](https://tailscale.com/kb/1257/connection-types), [Custom DERP servers](https://tailscale.com/kb/1118/custom-derp-servers).

**Порядок выбора пути** [факт, Connection types]:
1. Любое соединение начинается через DERP.
2. Затем клиент пытается перейти на прямой UDP.
3. Если прямой путь не работает, используется peer relay (если он настроен; он тоже UDP).
4. Последний вариант — DERP.

По скорости: прямой путь лучше peer relay, peer relay лучше DERP. Шифрование одинаковое во всех трёх случаях (WireGuard из конца в конец).

**Если прямой UDP заблокирован.** Документация Tailscale говорит прямо: без UDP прямого соединения не будет, а связь пойдёт через peer relay или DERP [факт]. Значит, при полностью закрытом UDP Tailscale продолжает работать, только медленнее, через TCP 443. Это и делает его кандидатом в «резервный TCP-путь».

**Оговорка про прямой путь** [гипотеза]. Прямой путь — это обычный WireGuard. По отчётам, в РФ рукопожатие обычного WireGuard блокируется по сигнатуре:
- на мобильных сетях часто;
- на домашних проводных — по регионам и дням.

Источники: [vpnstatus.site](https://vpnstatus.site/protocols/wireguard), страница обновлена 27.10.2026, методика не раскрыта; [ntc.party: блокировка WireGuard между проводными операторами](https://ntc.party/t/блокировка-wireguard-внутри-рф-на-проводных-операторах/25350). [наблюдение]

Возможен такой сценарий: служебные проверочные пакеты Tailscale по UDP проходят, клиент решает, что прямой путь рабочий, а сам WireGuard на нём режется. Похожие жалобы на то, что клиент «залипает» на неработающем пути, есть в трекере Tailscale в другом контексте ([#9524](https://github.com/tailscale/tailscale/issues/9524), [#21666](https://github.com/tailscale/tailscale/issues/21666)). Это нужно проверить на стенде (§5), а не предполагать.

**Exit node** [факт, [Exit nodes](https://tailscale.com/kb/1103/exit-nodes)]:
- Узел объявляет маршруты `0.0.0.0/0` и `::/0`, администратор их одобряет, клиент выбирает exit node.
- Без выбранного exit node через туннель идёт только трафик внутри tailnet (100.64.0.0/10). Интернет при этом идёт напрямую.
- Exit node может работать на Linux, то есть на нашем VPS.
- Через политику `ExitNodeID` exit node можно задать принудительно на Windows, macOS, iOS и Android ([system policies](https://tailscale.com/docs/features/tailscale-system-policies.md)).

## 2. Tailscale из России в 2024–2026

| Что | Состояние | Класс | Источник и дата |
|---|---|---|---|
| Координационный сервер и вход (облако Tailscale) | Код **451 Unavailable For Legal Reasons** для российских IP с 01.10.2024. Раньше, с конца августа 2023, 451 отдавали только загрузки | [наблюдение], совпадает в 4+ источниках | [HN, 02.10.2024](https://news.ycombinator.com/item?id=41717465); [bugtraq.ru, 02.10.2024](https://bugtraq.ru/cgi-bin/forum.mcgi?type=sb&b=17&m=169444); [ntc.party](https://ntc.party/t/tailscale-больше-не-работает-в-россии/10977); [vpsindex, 02–09.2026](https://vpsindex.ru/blog/headscale-na-vps) |
| Публичные DERP Tailscale | В 2024 году отвечали 451 российским IP. В 2026 году описаны как «тоже geo-blocked» | [наблюдение] | HN (2024); vpsindex (2026) |
| Пакеты и сайт загрузки (`pkgs.tailscale.com`) | С российских IP не отвечают (MSI, APK, pkg, Linux-репозиторий) | [наблюдение] | [SSD Nodes, 21.10.2026](https://www.ssdnodes.com/learn/lang/ru/where-to-download-tailscale-apk-msi-linux); [Wiren Board, 451 при apt](https://support.wirenboard.com/t/tailscale-ne-ustanavlivaetsya-oshibka-451/16513) |
| App Store, российская витрина | Приложения нет | **[факт]**, наш замер 27.10.2026 (iTunes lookup country=ru → 0; us и kz → есть) | — |
| Google Play, российский аккаунт | Карточка не открывается | [наблюдение] | SSD Nodes, 21.10.2026. Есть F-Droid, но сборка отстаёт и подписана F-Droid |
| Блокировка доменов tailscale.com со стороны РКН | Подтверждений не нашли. 451 отдаёт сам Tailscale | [факт] по коду ответа, [гипотеза] насчёт отсутствия блокировки РКН | — |
| Почему Tailscale закрылся | Официального заявления не нашли. Сроки совпадают с запретом OFAC на ИТ-услуги и облачное ПО для управления предприятием в РФ, действует с 12.09.2024 | [факт] о самом запрете, [гипотеза] о связи | [Miller & Chevalier](https://www.millerchevalier.com/publication/trade-compliance-flash-ofac-announces-prohibitions-provision-certain-it-and-software); [Arnold & Porter](https://www.arnoldporter.com/en/perspectives/advisories/2024/06/russias-access-to-it-services-and-software-restricted) |
| Оплата | Бесплатный план Personal — до 6 пользователей; Standard — $8, Premium — $18 за пользователя в месяц ([pricing](https://tailscale.com/pricing)). Российской картой не оплатить. Для 33 человек без 451 это было бы около $264 в месяц | [факт] по ценам, [гипотеза] по оплате | — |

**Различия по операторам и регионам**: это не про само облако Tailscale, оно закрыто для всех российских IP, а про Headscale и любой свой DERP.
- **Мобильные сети** [наблюдение]:
  - обычный WireGuard (прямой путь Tailscale) часто режется;
  - на МегаФоне пользователи жаловались, что не работают Tailscale и ZeroTier ([Хабр Q&A, >1 года назад](https://qna.habr.com/q/1401938));
  - в том же треде, по словам одного пользователя, на МТС и МегаФоне работает схема «Android-клиент плюс свой координационный сервер».
- **В режиме белых списков** на мобильном интернете иностранный VPS недоступен целиком, на любом протоколе. Tailscale здесь ничего не меняет (см. [white-ips_25-10-26.md](../2026-10-25/white-ips_25-10-26.md)). [гипотеза, следует из механики]
- **Домашний проводной интернет** [наблюдение]: обычный WireGuard фильтруется в части регионов и дней (Ростелеком, Дом.ру, МГТС), где-то проходит. Значит, доля трафика по прямому пути будет зависеть от провайдера.
- **Свой DERP на TCP 443 в РФ.** Прямых измерений за 2026 год по операторам мы не нашли. Есть только описания в гайдах. [нет данных]

**Случай с Ригой 27.10** [гипотеза]. Если облачный Tailscale отвечает 451 российским IP, то «Tailscale работал» означает одно из трёх:
1. у знакомого Headscale или свой DERP;
2. клиент вошёл в сеть раньше и жил на закэшированной сетевой карте, а связь шла по прямому UDP или через доступный DERP;
3. трафик Tailscale шёл не с российского адреса.

Вывод «Tailscale устойчивее AWG» из одного случая сделать нельзя. Корректно только одно: в этой сети в этот день TCP-путь (или другой UDP-путь) пережил то, что убило AWG.

## 3. Headscale: свой координационный сервер

**Зрелость** [факт, [releases](https://github.com/juanfont/headscale/releases), [README](https://github.com/juanfont/headscale)]:
- Последние релизы:
  - **v0.29.6 (24.10.2026)** — пересборка из-за безопасности;
  - v0.29.5 (13.10) — исправления exit node, SSH, устаревших узлов в Android;
  - v0.29.4 (23.09) — исправлено падение Android-клиента, когда узел видел сам себя;
  - v0.29.0 (17.06) — grants, ломающие изменения в ACL.
- Ритм — примерно релиз в месяц. Ветка 0.x: ломающие изменения бывают даже в минорных версиях.
- Масштаб по README: **один tailnet**, «для личного использования или небольшой open-source организации». Около 45 тыс. звёзд.
- Один из мейнтейнеров работает в Tailscale, но проект с Tailscale Inc. формально не связан.
- Docker и обратные прокси авторы «не поддерживают и не поощряют».

**Клиенты** [факт, [clients](https://headscale.net/stable/about/clients/)]. Поддерживаются последние 10 релизов официальных клиентов Tailscale на Linux, Windows, macOS, iOS, tvOS и Android. Отдельного клиента нет, официальное приложение переключают на свой сервер:

| Платформа | Как переключить | Сложность для нетехнического человека |
|---|---|---|
| Android | Настройки → Accounts → ⋮ → «Use an alternate server», затем ⋮ → «Use an auth key» ([doc](https://headscale.net/stable/usage/connect/android/)) | Средняя: скрытое меню, но без командной строки |
| iOS | Аккаунт → Log in → ⋮ → «Use custom coordination server» ([doc](https://headscale.net/stable/usage/connect/apple/)) | Средняя. **Нужен Apple ID не российского региона**: в RU App Store приложения нет |
| macOS | Option-клик по иконке → Debug → Custom Login Server, или `tailscale login --login-server` | Высокая: отладочное меню |
| Windows | Только `tailscale login --login-server <URL>` в PowerShell, либо политика `LoginURL` в реестре ([doc](https://headscale.net/stable/usage/connect/windows/)) | Высокая без готового `.reg` или скрипта |

Политики `LoginURL` и `AuthKey` есть на всех четырёх платформах. Через них можно заранее задать сервер и ключ входа ([system policies](https://tailscale.com/docs/features/tailscale-system-policies.md)) [факт]:
- на Windows — значения в реестре (можно выдавать `.reg` или скрипт из админки);
- на iOS и macOS — профиль конфигурации (MDM или Apple Configurator);
- на Android — managed configuration (нужен MDM).

**Регистрация 33 человек** [факт, [getting started](https://headscale.net/stable/usage/getting-started/)]:
- пользователи Headscale создаются локально (`headscale users create`), Google или Microsoft не нужны;
- устройство входит по pre-auth key (по умолчанию ключ одноразовый и живёт 1 час, есть варианты многоразовых) или через интерактивное одобрение администратором;
- OIDC — по желанию.

Для зоопарка это ложится на «одна учётка — один человек — ключ из админки». [гипотеза]

**Свой DERP** [факт, [headscale DERP](https://headscale.net/stable/ref/derp/), [custom DERP](https://tailscale.com/kb/1118/custom-derp-servers)]:
- Встроенный DERP: `derp.server.enabled: true`. Ему нужны TCP 443 (общий с Headscale), UDP 3478 (STUN) и TCP 80 для Let's Encrypt.
- Проверка клиентов (`verify_clients`) включена по умолчанию: реле обслуживает только свои узлы.
- `urls: []` отключает публичные DERP Tailscale. Для российских IP они всё равно отвечают 451.
- Документация предупреждает о двух вещах:
  - у встроенного DERP нет оптимизаций скорости;
  - без публичных DERP он становится единственной точкой отказа.
- Отдельный `derper` нужно периодически пересобирать под новые клиенты.

**Ресурсы** [наблюдение, [vpsindex](https://vpsindex.ru/blog/headscale-na-vps)]:
- Headscale в простое занимает 20–50 МБ ОЗУ, рекомендуют 512 МБ и 1 vCPU;
- база SQLite;
- для DERP важны канал и квота трафика, а не CPU.

Нужны домен (или имя вида sslip.io) и действующий TLS-сертификат. Работать не на 443 порту можно, но документация против: часть клиентов рассчитывает на 443 ([requirements](https://headscale.net/stable/setup/requirements/)).

**Конфликт с нашим стендом** [факт, по конфигу инсталлера]. TCP 443 у нас занят VLESS-REALITY, UDP 443 — Hysteria2. Headscale с DERP хотят TCP 443. Есть три варианта:
1. другой порт (против рекомендаций документации);
2. делить 443 по SNI (отдельная инженерная задача, затрагивает REALITY);
3. отдельный IP или VPS.

Это главный инженерный вопрос модуля.

**Веб-интерфейс.** В Headscale его нет. Сторонний Headplane существует (0.7.x по vpsindex), но нам он не нужен: управление удобнее встроить в zoo через CLI или gRPC Headscale. [гипотеза]

## 4. Подходит ли это нашей модели

**4.1. Белый список приложений** (через VPN только выбранные, банки и MAX напрямую):

| Платформа | Что умеет Tailscale | Против нашего AWG `IncludedApplications` и sing-box |
|---|---|---|
| Android | Split tunneling по приложениям с v1.70. Режим «исключить» пользователь настраивает руками в приложении. Режим **«только эти приложения» — только через MDM** (`IncludedPackageNames`) ([doc](https://tailscale.com/docs/features/client/android-app-split-tunneling)) [факт] | Хуже: белый список нельзя выдать файлом или ссылкой, а без MDM возможен только чёрный список руками на каждом телефоне |
| Windows | Выбора по приложениям нет. Только «exit node включён или выключен» плюс доступ к LAN [факт: в документации и трекере такой функции нет] | Хуже |
| iOS / macOS | Выбора по приложениям нет. Per-app VPN на iOS возможен только через MDM, и в документации Tailscale его нет [факт по документации] | Так же или хуже |

Флаг VPN в системе при этом поднят, как и у любого другого протокола. Для MAX и банков ничего не меняется: детект через `NetworkCapabilities` срабатывает так же (RISK-REDUCTION §7). [гипотеза, следует из механики Android VpnService]

**4.2. Exit node на нашем VPS.** Технически просто [факт]:
- `tailscale up --advertise-exit-node` на VPS, одобрение в Headscale (или autoApprovers), разрешение `autogroup:internet` в политике;
- на клиенте выбрать exit node или задать его политикой `ExitNodeID`.

Если exit node и DERP стоят на одном VPS, лишнего плеча нет: клиент → VPS (DERP) → тот же VPS (выход). [гипотеза]

**4.3. Учётки.**
- Облако Tailscale требует внешнего провайдера входа (Google, Microsoft, GitHub, Apple, OIDC), бесплатно до 6 человек. Из РФ отвечает 451, так что вариант отпадает. [факт / наблюдение]
- Headscale: локальные пользователи и ключи. Это ложится на модель zoo. [факт]

**4.4. Простота для нетехнических сотрудников.** Хуже, чем «отсканируй QR или открой ссылку» в наших текущих клиентах [гипотеза]:
- на iOS нужен иностранный Apple ID;
- сервер вводится через скрытые меню;
- на Windows нужна командная строка или `.reg`;
- на Android без MDM нет белого списка;
- выбор exit node — отдельный шаг (или политика).

**4.5. Скорость через DERP** [наблюдение]:
- Облачный DERP Tailscale ограничен ради «справедливости» между пользователями. В тесте Tailscale на длинном маршруте получилось около 2,2 Мбит/с ([блог Tailscale, 26.01.2026](https://tailscale.com/blog/peer-relays-international-networks)).
- Свой DERP таких ограничений не имеет, но:
  - встроенный DERP Headscale «без оптимизаций скорости» [факт];
  - на Windows открыт баг: около 10 Мбит/с через DERP против 40–60 Мбит/с из WSL на том же ПК ([#18017](https://github.com/tailscale/tailscale/issues/18017), с 21.11.2025, ответа нет).
- Путь через DERP — это WireGuard внутри TCP. На мобильных линиях с потерями стоит ждать заметной просадки по сравнению с UDP-протоколами. [гипотеза]

Ориентир: «хватит на мессенджеры и браузер, видео под вопросом». Цифры нужно получить замером.

## 5. Рекомендация и минимальный тест

**Вывод** [гипотеза, основанная на фактах выше]:
- **Облачный Tailscale не добавлять**: 451 для российских IP, нет приложения в RU App Store, нельзя оплатить.
- **Headscale как основной протокол для 33 человек не добавлять.** Белый список приложений не выдать с сервера (Android — только через MDM, на Windows и iOS его нет). Подключение для нетехнических людей сложнее. Порт 443 конфликтует с REALITY.
- **Как экспериментальный резервный путь по TCP 443** — допустимо, если тест покажет пользу. Основной плюс — автоматический откат с UDP на TCP внутри одного клиента. Но у нас уже есть TCP-путь (VLESS-REALITY, XHTTP), и случай в Риге сам по себе не доказывает, что Tailscale переживёт то, что не переживает REALITY.
- **Приоритет низкий.** Сначала стоит понять, почему у знакомого упал AWG: бан IP, UDP-фильтр или сигнатура. Это видно по журналу и пробнику, как в разборе 24.10.

**Минимальный тест на тестовом VPS**, около 2 часов, без изменений в инсталлере:
1. Поставить Headscale (последний 0.29.x, пакет `.deb` с GitHub) на свободный порт или на отдельный IP тестового стенда. Включить встроенный DERP, задать `urls: []` (без публичных DERP Tailscale) и `verify_clients: true`. Поднять `tailscale` на том же VPS как exit node.
2. Подключить 3 устройства с 3 разных сетей: Android на мобильном (МТС или МегаФон), Windows на домашнем проводном (Ростелеком или другой), iPhone с не российским Apple ID. Для каждого записать:
   - сколько минут занял вход нетехническим способом;
   - `tailscale status` и `tailscale netcheck`: какой путь, direct или DERP.
3. Для каждого устройства проверить два режима: UDP-порт tailscaled на VPS открыт и закрыт (во втором случае весь трафик идёт через DERP). Цель — увидеть, не залипает ли клиент на прямом UDP, когда обычный WireGuard режется.
4. Замерить скорость `iperf3` или speedtest через exit node в обоих режимах. Сравнить с AWG и VLESS-REALITY с того же устройства в тот же час.
5. На Android проверить split tunneling в режиме «исключить» (банк и MAX вне туннеля). Отдельно записать: можно ли получить режим «только эти приложения» без MDM (по документации нельзя).
6. Повторить шаг 2 через сутки и после ближайшего сбоя AWG у кого-то из своих.

**Критерий «добавлять экспериментальный модуль»**: на мобильном и на проводном Headscale через DERP работает там, где AWG не работает, и даёт не меньше 10–20 Мбит/с. Если везде, где работает Headscale, работает и VLESS-REALITY, модуль не нужен.

## Источники

Tailscale:
- [DERP servers](https://tailscale.com/kb/1232/derp-servers)
- [Connection types](https://tailscale.com/kb/1257/connection-types)
- [Custom DERP](https://tailscale.com/kb/1118/custom-derp-servers)
- [Exit nodes](https://tailscale.com/kb/1103/exit-nodes)
- [Android app split tunneling](https://tailscale.com/docs/features/client/android-app-split-tunneling)
- [System policies](https://tailscale.com/docs/features/tailscale-system-policies.md)
- [Pricing](https://tailscale.com/pricing)
- [Peer relays blog, 26.01.2026](https://tailscale.com/blog/peer-relays-international-networks)
- Issues: [#6912](https://github.com/tailscale/tailscale/issues/6912), [#9524](https://github.com/tailscale/tailscale/issues/9524), [#21666](https://github.com/tailscale/tailscale/issues/21666), [#18017](https://github.com/tailscale/tailscale/issues/18017)

Headscale:
- [releases](https://github.com/juanfont/headscale/releases)
- [README](https://github.com/juanfont/headscale)
- [clients](https://headscale.net/stable/about/clients/)
- [DERP](https://headscale.net/stable/ref/derp/)
- [requirements](https://headscale.net/stable/setup/requirements/)
- подключение: [Windows](https://headscale.net/stable/usage/connect/windows/), [Apple](https://headscale.net/stable/usage/connect/apple/), [Android](https://headscale.net/stable/usage/connect/android/)

Россия:
- [HN 02.10.2024](https://news.ycombinator.com/item?id=41717465)
- [bugtraq.ru 02.10.2024](https://bugtraq.ru/cgi-bin/forum.mcgi?type=sb&b=17&m=169444)
- [ntc.party #10977](https://ntc.party/t/tailscale-больше-не-работает-в-россии/10977)
- [Хабр Q&A](https://qna.habr.com/q/1401938)
- [vpsindex 2026](https://vpsindex.ru/blog/headscale-na-vps)
- [SSD Nodes 21.10.2026](https://www.ssdnodes.com/learn/lang/ru/where-to-download-tailscale-apk-msi-linux)
- [vpnstatus.site](https://vpnstatus.site/protocols/wireguard)

Санкционный фон:
- [Miller & Chevalier](https://www.millerchevalier.com/publication/trade-compliance-flash-ofac-announces-prohibitions-provision-certain-it-and-software)
- [Arnold & Porter](https://www.arnoldporter.com/en/perspectives/advisories/2024/06/russias-access-to-it-services-and-software-restricted)
