---
name: release-check
description: Проверка перед релизом или пушем крупного изменения — юнит-тесты zoo, shellcheck, полный e2e на Ubuntu 22.04 и 24.04, согласованность документации с кодом, целостность закреплённых версий. Итог — таблица готовности.
argument-hint: "[--quick — без e2e]"
allowed-tools: Bash(python -m unittest *) Bash(python3 -m unittest *) Bash(bash docker/lint.sh *) Bash(bash docker/test.sh *) Bash(git status *) Bash(git diff *) Bash(git log *) Read Grep Glob
---

# /release-check

Аргументы: `$ARGUMENTS` (`--quick` — пропустить e2e).

Ничего не коммитить и не пушить: только проверка и отчёт. Найденное чинить, только если владелец просил «проверь и поправь».

## Шаги (1–3 и 5–6 параллельно с 4)

1. **Состояние**: `git status --short`, `git log origin/main..HEAD --oneline`. Неотслеживаемые .sh — проверить, что будут добавлены с `--chmod=+x`.
2. **Юнит-тесты**: `cd zoo && python -m unittest discover -s tests -t .` (на Linux — `python3`). Ожидается OK, допустим 1 skip.
3. **shellcheck**: `bash docker/lint.sh` — замечаний уровня warning быть не должно.
4. **e2e** (если не `--quick`): два фоновых прогона одновременно, лучше через сабагентов `e2e-runner`:
   ```bash
   bash docker/test.sh --mode full --tests all --distro 24.04 --name rc24 --keep \
       --env ENABLE_TUIC=1 --env ENABLE_HY2_OBFS=1 --env HY2_HOP=1 --env ENABLE_WARP=1
   bash docker/test.sh --mode full --tests all --distro 22.04 --name rc22 --keep
   ```
   Пока идут — не трогать `docker/*.sh`. Итог — `docker/out/<ts>/summary.md` каждого; разбор как в `/e2e`.
5. **Документация** — сабагент `docs-consistency-checker`: README, ARCHITECTURE, DECISIONS, PROBE-SURFACE, docker/README против кода (фазы, ключи `CONFIG_ENV_KEYS_RE` и `phase_owns_key`, команды `zoo`, версии, ограничения).
6. **Пины**:
   - каждая версия в README/ARCHITECTURE/`PVR_NOTES` совпадает с `scripts/versions.env`;
   - в `scripts/` и `docker/` нет загрузок мимо `download_verified`/`fetch` с sha256: `grep -rnE 'curl|wget' scripts docker --include=*.sh`, просмотреть каждое совпадение; нет `latest`, кроме geo-таймера (D19);
   - все `*_SHA256*` — 64 hex; у AWG-исходников есть `*_COMMIT`.
7. **Секреты**: в диффе с прошлого релиза нет секретов в argv (`--password`, токены в командной строке), в логах (`log_*` с ключами), нет закоммиченных `docker/out/`, `probe/`, `*.conf`, `config.env`.

## Итог

Таблица по-русски: проверка → OK/FAIL → деталь (файл:строка, тест, фаза). Внизу одна строка: «готово к пушу» или список блокеров. Контейнеры `rc22`/`rc24` убрать (`bash docker/run-server.sh down rc22` …), если провалов нет.
