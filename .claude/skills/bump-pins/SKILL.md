---
name: bump-pins
description: Обновить закреплённые версии в scripts/versions.env — найти новые релизы upstream (3x-ui/Xray, Hysteria, amneziawg-go/tools, Go, sing-box, geo-файлы), проверить sha256 по опубликованным хешам, разобрать ломающие изменения, обновить пины и документацию, прогнать e2e.
argument-hint: "[компонент: xui|hy2|awg|go|singbox|geo|all]"
disable-model-invocation: true
allowed-tools: Bash(gh release view *) Bash(gh release list *) Bash(sha256sum *) Bash(bash docker/test.sh *) Bash(bash docker/run-server.sh *) Bash(bash docker/tests/*) Bash(python -m unittest *) Bash(bash docker/lint.sh *) Bash(git diff *) Read Grep Glob
---

# /bump-pins

Компонент: `$ARGUMENTS` (по умолчанию `all`). Правила — D1 и D19 в `docs/DECISIONS.md`: только стабильные релизы, никаких `latest`, хеш сверен с опубликованным upstream.

## 1. Что есть и что вышло

Прочитать `scripts/versions.env`. Для каждого компонента — последний **не pre-release** релиз (`gh release list -R <repo> --exclude-pre-releases -L 5`, или WebFetch `https://api.github.com/repos/<repo>/releases`):

| Компонент | Репозиторий | Источник хеша |
|---|---|---|
| 3x-ui (+ Xray внутри) | MHSanaei/3x-ui | `<архив>.sha256` рядом с архивом (с v3.8.0) |
| Hysteria2 | HyNetworks/hysteria, теги `app/vX` | `hashes.txt` релиза |
| amneziawg-go, amneziawg-tools | amnezia-vpn/* | бинарных релизов у go нет: sha256 архива тега + `*_COMMIT` тега (архивы GitHub не гарантированно стабильны) |
| Go | go.dev/dl | `https://go.dev/dl/?mode=json` (поле sha256) |
| sing-box | SagerNet/sing-box | digest ассета в API GitHub |
| geo | runetfreedom/russia-v2ray-rules-dat, Loyalsoldier/v2ray-rules-dat | `<файл>.sha256sum` релиза |

Ничего не изменилось — сообщить и закончить.

## 2. Ломающие изменения (до правки пинов)

Прочитать changelog между текущей и новой версией. Особое внимание:
- **3x-ui**: изменения API (сравнить эндпоинты, которые зовёт `scripts/lib/xui.sh`, с исходниками `internal/web/controller` нового тега; OpenAPI — `/panel/api/openapi.json` на живой панели стенда), схема БД, версия Xray внутри (`XUI_XRAY_VERSION`) — pre-release ли она, что меняется для клиентов REALITY (ML-KEM, отпечатки, sing-box-клиенты);
- **Hysteria**: формат конфига, `auth.type: command` (D17), trafficStats API;
- **AWG**: совместимость параметров с клиентами 2.0 (D4), требование версии Go в `go.mod`, список сломанных ядер `AWG_KMOD_BROKEN_KERNELS`;
- advisories: `github.com/<repo>/security/advisories`.

Если релиз ломает клиентов или контракт — не поднимать, доложить владельцу с выбором.

## 3. Хеши

Скачать каждый ассет (amd64 и arm64) во временный каталог, `sha256sum`, сверить с опубликованным хешем. Расхождение — стоп, доложить. В `versions.env`: версия, URL, хеши, комментарий с датой релиза и строкой «хеш совпадает с …». Только присваивания, без логики.

## 4. Хвосты по репозиторию

`git grep -n '<старая версия>'` и поправить упоминания: README (таблица протоколов, «Движки», клиенты), `docs/ARCHITECTURE.md`, `PVR_NOTES` и подобные заметки в `scripts/lib/proto-*.sh`, тесты, `docker/probe/install-clients.sh` (берёт пины из versions.env — проверить имена ассетов).

## 5. Проверка

`cd zoo && python -m unittest discover -s tests -t .`, `bash docker/lint.sh`, затем e2e на обоих дистрибутивах в фоне (как в `/e2e`, имена `pin22`/`pin24`, на 24.04 с `--env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1`).

Если менялись 3x-ui или Hysteria — ещё обновление поверх старой установки. Поднять контейнер **до** правки `versions.env` (шаг 3): `bash docker/run-server.sh up up1 && bash docker/run-server.sh install up1`. После правки: `bash docker/run-server.sh sync up1`, затем `bash docker/run-server.sh exec up1 zoo upgrade --apply --repo /repo` (план, smoke до и после) и `bash docker/tests/links.sh zoo-up1` — старые ссылки owner должны работать.

## 6. Итог

Коммит в `main`: `chore(pins): <компонент> X → Y`, тело — что изменилось у upstream и что проверено (e2e счёт). Push — после согласия владельца. Ответ: таблица компонент → было → стало → проверено.
