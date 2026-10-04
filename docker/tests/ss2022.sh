#!/usr/bin/env bash
# docker/tests/ss2022.sh SERVER [--keep] — сквозной тест протокола ss2022 (см. _xui-proto-e2e.sh)
exec bash "$(dirname "${BASH_SOURCE[0]}")/_xui-proto-e2e.sh" ss2022 "$@"
