#!/usr/bin/env bash
# docker/tests/tuic.sh SERVER [--keep] — сквозной тест протокола tuic (см. _xui-proto-e2e.sh)
exec bash "$(dirname "${BASH_SOURCE[0]}")/_xui-proto-e2e.sh" tuic "$@"
