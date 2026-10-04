---
name: e2e
description: Прогнать e2e-стенд vpn-zoo-installer (docker/test.sh, systemd-контейнер Ubuntu, настоящие клиенты, эмулятор ТСПУ) и разобрать итог summary.md. Использовать после изменений в scripts/, zoo/ или docker/, когда нужно проверить установку и протоколы.
argument-hint: "[22.04|24.04|both] [доп. ключи test.sh, напр. --env ENABLE_TUIC=1 --tests hysteria2]"
allowed-tools: Bash(bash docker/test.sh *) Bash(bash docker/run-server.sh *) Bash(bash docker/tests/*) Bash(docker ps *) Read Grep Glob
---

# /e2e — прогон стенда

Аргументы: `$ARGUMENTS`

## Что запустить

1. Дистрибутив: из первого аргумента (`22.04`, `24.04` или `both`), по умолчанию `24.04`. Остальные аргументы — дополнительные ключи `docker/test.sh` (полный список: `bash docker/test.sh --help`).
2. **По умолчанию — точечно:** только тесты, которые задевают изменения (`git diff --name-only` → `docker/tests/<id>.sh` по затронутым протоколам/модулям), без матрицы цензора:
   ```bash
   bash docker/test.sh --mode full --tests allowlist,amneziawg --distro 24.04 --name dev24 --probe-profiles none
   ```
   Установка ≈1.5 мин (93 с), один тест — 10–75 с, ssh-harden ≈4 мин. **Полный** прогон (`--tests all`, матрица цензора, оба дистрибутива) — только если пользователь просит `all`/`both` или это `/release-check`:
   ```bash
   bash docker/test.sh --mode full --tests all --distro 24.04 --name e2e24 --keep
   ```
   `--mode full` — один `install.sh`, как у пользователя; `--tests all` — тесты протоколов, links, routing, security, web, collector и последним ssh-harden (≈4 мин).
3. Перед запуском назвать пользователю ожидаемое время. Полный прогон (≈14 мин на дистрибутив, замер 05.10.2026) — через Bash с `run_in_background: true`, ждать уведомления, не опрашивать. Для `both` — два фоновых прогона с разными `--name` (`e2e22`, `e2e24`) или сабагент `e2e-runner`.
4. Пока идёт прогон, **не править `docker/*.sh`** (bash читает скрипт по ходу). Чтобы править репо параллельно, гонять из замороженной копии: `rm -rf /tmp/zoo-frozen && mkdir -p /tmp/zoo-frozen && git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf - | tar -C /tmp/zoo-frozen -xf - && cd /tmp/zoo-frozen && bash docker/test.sh …`.
5. На Windows/Git Bash скрипты стенда сами ставят `MSYS_NO_PATHCONV=1`; для ручных `docker`-команд ставить его самому.

## Как разобрать итог

1. Каталог отчёта — последний `docker/out/<ts>/` с `meta.txt`, где `container=zoo-<имя>` совпадает с прогоном.
2. Прочитать `summary.md`. Провал — любые `FAIL`, `TIMEOUT`, `NOEXEC`, `NOT_RUN`, `REBOOT` или `SKIPPED` без «выключена (ENABLE_…=0)». `probe-client: SKIPPED` при `--tests` без пробника — норма.
3. Для каждого провала открыть лог из колонки «Причина» (`phases/<фаза>.log` или `phases/full.log`, `tests/<тест>.log`, `probe/<профиль>/`), найти первую строку `[x]` и контекст до неё; при сервисных ошибках — `diag.txt` (systemd, `ss -tulpn`, ufw, журналы).
4. `NOEXEC` = у нового .sh нет +x в индексе git: `git add --chmod=+x <файл>`.
5. Если test.sh оборвался без `summary.md` (бывает на Git Bash), контейнер остался (`--keep`): дотестировать руками `bash docker/tests/<тест>.sh zoo-<имя>`.

## Ответ пользователю

Коротко по-русски: дистрибутив, флаги, счёт `PASS n/m`, по каждому провалу — фаза/тест, причина одной строкой, файл:строка в коде, если найдена. Путь к `summary.md`.

После разбора убрать контейнеры, если отладка не нужна: `bash docker/run-server.sh down <имя>` (только контейнеры стенда с меткой `zoo.harness=1`).
