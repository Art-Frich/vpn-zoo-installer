# vpn-zoo-installer — карта для Claude

Bash-инсталлер «зоопарка» VPN-протоколов на одном VPS (Ubuntu 22.04/24.04) для небольшого частного круга пользователей в РФ + инструмент `zoo` (Python 3.10+ stdlib: CLI, веб-админка на 127.0.0.1, пробник, коллектор трафика). Если ТСПУ режет один протокол, остаются другие: VLESS+REALITY (443/tcp), VLESS XHTTP+REALITY, Shadowsocks-2022, Hysteria2 (443/udp), AmneziaWG 2.0; по флагам TUIC v5, Hysteria2+Salamander, WARP. Владелец — Art-Frich, MIT.

## Где правда (читать перед изменениями)

| Файл | Что |
|---|---|
| `docs/ARCHITECTURE.md` | контракт разработки: фазы, ключи config.env и их фазы-владельцы, манифест, пользователи, zoo, пробник, стенд, правила кода |
| `docs/DECISIONS.md` | решения D1–D30 с «почему» и «как поменять». Новое решение без владельца → новая строка D31… |
| `docs/RISK-REDUCTION.md` | детект VPN, утечки IP, MAX/банки, пошаговые настройки клиентов, §7 поправки к исследованиям |
| `docs/PROBE-SURFACE.md` | что видит активный сканер снаружи (порты, баннеры, неотличимость REALITY) |
| `research/README.md` | индекс исследований по датам; срезы не переписываются, поправки — в RISK-REDUCTION §7 |
| `scripts/versions.env` | закреплённые версии и sha256 всего скачиваемого (D1). Только присваивания |
| `README.md` | пользовательская документация (русский): установка, zoo, флаги, ограничения, FAQ |
| `docker/README.md` | стенд: ключи test.sh, контракт `ZOO_TEST_ENV=docker`, отчёты, тесты |

При расхождении кода и документа правда — код; документ поправить в том же коммите.

## Архитектура

**Фазы** (`scripts/NN-*.sh`, порядок — массив `PHASES` в `install.sh`; state в `/var/lib/vpn-setup/state`: done/failed/disabled):

| Фаза | Что |
|---|---|
| 00-bootstrap | apt, swap, sysctl, unattended-upgrades (AUTO_REBOOT) |
| 01-firewall | UFW default deny, SSH-порты определяются, fail2ban; порты протоколов открывают сами фазы через `fw_allow` |
| 01b-ssh | opt-in `SSH_HARDEN=1` (D30): новый порт + только ключ, два шага, таймер отката |
| 02-kernel | совместимость ядра с DKMS amneziawg, HWE-ядро (22.04) или `AWG_NO_HWE=1` |
| 03-3xui | 3x-ui v-пин, панель на 127.0.0.1, API-токен |
| 04 / 04b / 04c / 04d | VLESS REALITY / XHTTP / SS-2022 / TUIC — inbound'ы 3x-ui через API |
| 05-hysteria2 | standalone HyNetworks/hysteria, `auth.type: command` (D17), pinSHA256, Salamander, hopping |
| 06-amneziawg | kernel (DKMS) или `amneziawg-go`, уникальные параметры обфускации |
| 07-routing | анти-утечки: echo-сервисы → WARP/блок, RU_EGRESS, блок адресов сервера из туннеля (D25), geo-таймер |
| 08-warp | Cloudflare WARP outbound (opt-in) |
| 09-zoo | копия `zoo/`+`scripts/` в `/opt/vpn-zoo`, юниты, служебный пользователь `zoo-probe` |
| 99-print-creds | ссылки/QR/CREDENTIALS.md/probe-export.json из манифестов; затем `install.sh` запускает самопроверку |

Повторный запуск: изменённый ключ перезапускает фазу-владельца (`phase_owns_key` в install.sh), `ENABLE_X=0` выключает протокол (D24), после фазы протокола переприменяется 07.

**Контракты:**
- `scripts/lib.sh` — лог (`log_info/ok/warn/err`, `die`), state (`state_get/set`, `is_done`), config (`config_set/get/default`, список ключей — `CONFIG_ENV_KEYS_RE`), манифесты (`manifest_write/list/get/del`), firewall (`fw_allow/fw_revoke`, реестр портов), `backup_path`, `guard_foreign_install`/`mark_owned`, `download_verified` (sha256), `rand_port` (запрещённые: 1080 3128 8080 9050 2053 54321), `is_test_docker`.
- `scripts/lib/xui.sh` — **единственный** адаптер API 3x-ui (`xui_api`, `xui_inbound_*`, `xui_client_*`, пользователи `xui_user_attach/detach/set_enable`). Один пользователь = один клиент 3x-ui на все Xray-inbound (D18).
- `scripts/lib/proto-<id>.sh` — модуль протокола: `proto_<id>_user_add|user_del|user_enable|user_list|links|probe|manifest_refresh|traffic|disable`. stdout — только данные, логи в stderr. Контракт описан в `zoo/zoolib/protolib.py`; zoo зовёт эти функции из копии в `/opt/vpn-zoo`.
- Манифест `/etc/vpn-setup/protocols.d/<id>.json`: `id, layer, port, engine, service, enabled, users_backend, links[], files[], probe{kind: xray|hysteria|awg|sing-box}` (ARCHITECTURE §4). Пробник строит клиента только из `probe`.
- Пользователи: `/etc/vpn-setup/users.json` — источник правды; `owner` создаётся при установке; `zoo-probe` — скрытый служебный (D26).
- Серверные файлы: `/etc/vpn-setup/config.env` (0600, секреты), `clients/<имя>/`, `/var/lib/vpn-zoo/`, `/var/log/vpn-zoo/install-*.log` и `/var/backups/vpn-setup/` (содержат ключи, ротация D29).

**zoo** (`zoo/zoo` → `zoo/zoolib/`): `cli.py` (argparse), `config.py`, `manifests.py`, `users.py`, `protolib.py` (мост в bash), `xui.py`, `traffic.py` (SQLite), `status.py`, `upgrade.py` (`zoo upgrade/smoke`), `probe/` (`engine.py`, `clients.py`, `verdicts.py`, `report.py`), `web/` (сервер, auth, CSRF/CSP, views). Юниты — `zoo/systemd/`. Тесты — `zoo/tests/` (unittest) + `zoo/tests/web_smoke.sh`.

**Стенд** (`docker/`): `server.Dockerfile` (Ubuntu + systemd PID 1), `run-server.sh` (up/sync/install/shell/exec/down), `test.sh` (e2e), `tests/<id>.sh` (трафик настоящими клиентами, links, routing, security, collector, ssh-harden), `probe/` (образ `zoo-probe`, ожидаемые вердикты — `expect.py`), `censor/` (эмулятор ТСПУ: clean, drop-udp, ip-block, freeze-16k, rst-tls), `lint.sh` (shellcheck). Отчёты — `docker/out/<ts>/` (в .gitignore, внутри секреты стенда).

## Как проверять

```bash
# юнит-тесты zoo (на Windows — python, на Linux — python3), ~30 с
cd zoo && python -m unittest discover -s tests -t .

# shellcheck всех .sh в Docker (образ закреплён по digest), ~10 с
bash docker/lint.sh

# полный e2e как у пользователя + все тесты, 15–30 мин на дистрибутив
bash docker/test.sh --mode full --tests all --distro 24.04 --name e2e24 --keep
bash docker/test.sh --mode full --tests all --distro 22.04 --name e2e22 --keep
# с флагами: --env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1 --env HY2_HOP=1 --env ENABLE_WARP=1
# одна фаза/отладка: docker/run-server.sh up dev; sync dev; install dev -- --phase 05-hysteria2; shell dev
# отдельный тест против живого контейнера: bash docker/tests/hysteria2.sh zoo-e2e24
```

- Итог — `docker/out/<ts>/summary.md` (+ `phases/*.log`, `tests/*.log`, `diag.txt`). Код 0 = все PASS.
- Windows/Git Bash: ручные `docker`-команды только с `MSYS_NO_PATHCONV=1` (скрипты стенда ставят его сами); пути для `-v` — `$(pwd -W)`.
- Долгий прогон — в фоне и с `--keep`; на Git Bash он иногда обрывается без `summary.md`, тогда дотестировать руками `docker/tests/<тест>.sh zoo-NAME`.
- **Не править `docker/*.sh` во время прогона** — bash читает скрипт по ходу, test.sh упадёт.
- Параллельные прогоны — только с разными `--name`. Уборка: `docker/run-server.sh down NAME` или `down --all`.
- `--modes git` (по умолчанию) берёт права из индекса git: новый .sh без `git add --chmod=+x` даст `NOEXEC`.

## Правила кода и репозитория

- bash: `set -euo pipefail`, идемпотентность, каждая фаза проверяет себя (сервис active, порт слушает) и падает с понятным `[x]`.
- Секреты никогда в argv и в логах там, где есть альтернатива (тело запроса, файл 0600, stdin); `umask 077`. Исключения задокументированы (D13, D17).
- Всё скачиваемое — только через `versions.env` + `download_verified` (sha256). Никаких `latest`, `curl | bash`, незакреплённых образов.
- API 3x-ui — только через `scripts/lib/xui.sh`. Тела запросов брать из `/panel/api/openapi.json` закреплённой версии.
- Перед перезаписью чужого конфига — бэкап; чужая установка — отказ без `--force`.
- Новый параметр: `CONFIG_ENV_KEYS_RE` в lib.sh, фаза-владелец в `phase_owns_key`, таблица ARCHITECTURE §3, README «Параметры».
- Новый протокол: фаза + `lib/proto-<id>.sh` по контракту + манифест с `probe` + `docker/tests/<id>.sh` + при особом поведении под цензором — `docker/probe/expect.py` + README/ARCHITECTURE.
- LF везде (`.gitattributes`: `* text=auto eol=lf`). Новый скрипт: `git add --chmod=+x файл.sh` (Windows не хранит +x).
- Комментарии — по-русски и редко, только «почему». Весь вывод для пользователя и документация — по-русски, коротко, без канцелярита.
- Не публиковать адрес сервера и ссылки; в `docker/out/` и `probe/` — ключи, не коммитить.
- Коммиты прямо в `main`, стиль `feat(scope): …` / `fix: …` / `docs: …` / `research(YYYY-MM-DD): …`, тело — список по-русски, в конце строка `Co-Authored-By`. Push — `git push origin main` (remote по SSH `git@github.com:Art-Frich/vpn-zoo-installer.git`); перед коммитом — юнит-тесты, `docker/lint.sh`, e2e затронутого.

## Исследования (процесс)

- Срез — `research/YYYY-MM-DD/<тема>_DD-MM-YY.md`, сырьё роя — `research/YYYY-MM-DD/raw/<тема>.json` (`[{id, research, verify}]`). Шапка: Дата, Для кого, Контекст; §0 «Как собрано»; TL;DR; «Мифы»; источники ссылками.
- Рой: несколько потоков по темам, у каждого свой фактчекер, сверка с первоисточником (исходники на закреплённом теге, документация, GitHub API), затем адверсарная допроверка спорного (агенты пытаются опровергнуть).
- Статусы: ✅ подтверждено, ✏️ исправлено фактчекером, ❓ не проверено, 📄 по коду/документации без устройства.
- Калибровка утверждений о блокировках: **[репо]** — дефект инсталлера, чинить; **[версия]** — лечится закреплением; **[среда]** — ландшафт ТСПУ, операционные издержки. Форумы перекошены в негатив: искать и позитивные свидетельства (что работает у коммерческих сервисов и у владельца: его Hysteria2 стабильно работает на мобильном).
- После среза: строка в `research/README.md`, выводы → DECISIONS/RISK-REDUCTION/код. Скилл `/research`.

## Внешние референсы

| Что | Где |
|---|---|
| Xray (конфиг, REALITY, XHTTP, routing) | https://xtls.github.io/en/config/ · github.com/XTLS/Xray-core (релизы, pre-release!) · github.com/XTLS/REALITY |
| 3x-ui | github.com/MHSanaei/3x-ui, исходники на теге `v3.9.0` (`internal/web/...`), OpenAPI на живой панели `/panel/api/openapi.json` (после входа), advisories `/security/advisories` |
| Hysteria2 | https://v2.hysteria.network/docs/ (Full-Server-Config, URI-Scheme, Port-Hopping), github.com/HyNetworks/hysteria (теги `app/vX`) |
| AmneziaWG | github.com/amnezia-vpn: `amneziawg-go`, `amneziawg-tools`, `amneziawg-linux-kernel-module` (сломанные ядра #259), `amnezia-client`; https://docs.amnezia.org/documentation/amnezia-wg/ |
| sing-box | github.com/SagerNet/sing-box (клиент TUIC, совместимость REALITY #4520) |
| Geo-списки | github.com/runetfreedom/russia-v2ray-rules-dat, github.com/Loyalsoldier/v2ray-rules-dat; runetfreedom/per-app-split-bypass-poc |
| Блокировки, ТСПУ | github.com/net4people/bbs (issues), https://ntc.party/ |

## Известные ограничения (не выдавать за проверенное)

- Проверено только на Docker-стенде (systemd в контейнере, 22.04/24.04) настоящими клиентами Xray, sing-box, hysteria, amneziawg-go и эмулятором ТСПУ.
- Не проверено: боевой VPS через российского провайдера и настоящий ТСПУ; модуль ядра AmneziaWG (DKMS) и цикл HWE→reboot; arm64; хостер без hairpin; `SSH_HARDEN` на облачных образах с чужими drop-in и файрволом хостера.
- Клиентские приложения (Happ, v2rayN/NG, Hiddify, AmneziaVPN, mihomo…) — ссылки проверены разбором ядрами, не импортом на устройстве.
- Не прогонялись: `XHTTP_PLACEMENT=fallback`, `AWG_PROFILE=v3`, смена `PANEL_*`, `ENABLE_ZOO=0`, `SUB_PUBLIC=1` (фазы подписки нет).
- Открытые продуктовые темы: allowlist приложений вместо «исключить RU» и Brave под VPN (`research/2026-10-04/clients-and-allowlist_04-10-26.md`), свой Android-клиент (`own-android-client_04-10-26.md`) — решения за владельцем.

## Владелец: как работать

- Отвечать по-русски, коротко, по делу.
- Тяжёлое и параллельное (e2e на двух дистрибутивах, рой исследований, ревью) — в сабагентов/workflow, не занимать основной тред.
- Не дёргать вопросами по мелочам: принять разумное решение, записать в DECISIONS, сделать и отчитаться. Спрашивать только о необратимом (push, удаление, боевой сервер) и о продуктовом выборе.
- Не усложнять: минимальное решение, которое закрывает задачу; без лишних абстракций и зависимостей (zoo — только stdlib).

## Claude-инфраструктура

- Скиллы `.claude/skills/`: `/e2e` (прогон стенда и разбор итога), `/research` (срез по процессу выше), `/release-check` (юнит, shellcheck, e2e 22.04+24.04, сверка документов и пинов), `/bump-pins` (новые версии upstream → sha256 → versions.env → e2e).
- Сабагенты `.claude/agents/`: `docs-consistency-checker` (сверка README/ARCHITECTURE/DECISIONS с кодом), `e2e-runner` (долгий прогон стенда в фоне с разбором отчёта).
- Права — `.claude/settings.json`; личное — `.claude/settings.local.json` (в .gitignore).
