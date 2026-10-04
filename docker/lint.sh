#!/usr/bin/env bash
# lint.sh — shellcheck всех .sh репозитория в Docker (локально shellcheck не нужен).
#   docker/lint.sh [файл.sh ...]   без аргументов — все отслеживаемые и новые .sh
# Код выхода — код shellcheck. Уровень — warning, как в приёмке.
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"
cd "$ROOT"

if [ "$#" -gt 0 ]; then
    files=("$@")
else
    mapfile -t files < <(git ls-files -co --exclude-standard -- '*.sh')
fi
[ "${#files[@]}" -gt 0 ] || { echo "нет .sh-файлов"; exit 0; }

host_root="$ROOT"
command -v cygpath >/dev/null 2>&1 && host_root="$(cygpath -m "$ROOT")"

docker run --rm -v "$host_root:/mnt:ro" -w /mnt "$IMAGE" -x -S warning "${files[@]}"
echo "shellcheck: ${#files[@]} файлов, замечаний нет"
