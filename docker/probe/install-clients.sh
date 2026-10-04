#!/usr/bin/env bash
# install-clients.sh OUT — клиенты пробника по закреплённым версиям с проверкой sha256.
# Запускается в сборочной стадии docker/probe.Dockerfile; результат — бинари в OUT:
#   xray (из архива 3x-ui, как на сервере), hysteria, sing-box, amneziawg-go, awg.
# Версии и sha256 — scripts/versions.env (в образе /build/versions.env), как у сервера.

set -euo pipefail

OUT="${1:-/out}"
BUILD="$(mktemp -d)"
mkdir -p "$OUT"

# shellcheck source=../../scripts/versions.env
. /build/versions.env

case "$(uname -m)" in
    x86_64)  ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) echo "[x] архитектура $(uname -m) не поддерживается" >&2; exit 1 ;;
esac

pin() { local v="$1_$ARCH"; [ -n "${!v:-}" ] || { echo "[x] нет $v в versions.env" >&2; exit 1; }; printf '%s\n' "${!v}"; }

fetch() {
    local url="$1" sum="$2" dest="$3"
    echo "[i] $url"
    curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
    echo "$sum  $dest" | sha256sum -c --quiet - \
        || { echo "[x] sha256 не совпал: $url" >&2; exit 1; }
}

# Xray — из того же архива 3x-ui, что ставит фаза 03
fetch "$XUI_URL_BASE/x-ui-linux-$ARCH.tar.gz" "$(pin XUI_SHA256)" "$BUILD/x-ui.tgz"
tar -xzf "$BUILD/x-ui.tgz" -C "$BUILD" "x-ui/bin/xray-linux-$ARCH"
install -m 0755 "$BUILD/x-ui/bin/xray-linux-$ARCH" "$OUT/xray"

fetch "$HY2_URL_BASE/hysteria-linux-$ARCH" "$(pin HY2_SHA256)" "$BUILD/hysteria"
install -m 0755 "$BUILD/hysteria" "$OUT/hysteria"

fetch "$SINGBOX_URL_BASE/sing-box-${SINGBOX_VERSION#v}-linux-$ARCH.tar.gz" "$(pin SINGBOX_SHA256)" "$BUILD/sb.tgz"
tar -xzf "$BUILD/sb.tgz" -C "$BUILD"
install -m 0755 "$BUILD/sing-box-${SINGBOX_VERSION#v}-linux-$ARCH/sing-box" "$OUT/sing-box"

# amneziawg-go и awg — из исходников (бинарных релизов под все архитектуры нет)
fetch "$GO_URL_BASE/go$GO_VERSION.linux-$ARCH.tar.gz" "$(pin GO_SHA256)" "$BUILD/go.tgz"
tar -C "$BUILD" -xzf "$BUILD/go.tgz"
fetch "$AWG_GO_SRC_URL" "$AWG_GO_SRC_SHA256" "$BUILD/awg-go.tgz"
mkdir -p "$BUILD/awg-go"
tar -C "$BUILD/awg-go" --strip-components=1 -xzf "$BUILD/awg-go.tgz"
( cd "$BUILD/awg-go" && env PATH="$BUILD/go/bin:$PATH" HOME="$BUILD" GOPATH="$BUILD/gopath" \
    GOCACHE="$BUILD/gocache" GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0 \
    go build -trimpath -ldflags '-s -w' -o "$OUT/amneziawg-go" . )

fetch "$AWG_TOOLS_SRC_URL" "$AWG_TOOLS_SRC_SHA256" "$BUILD/awg-tools.tgz"
mkdir -p "$BUILD/awg-tools"
tar -C "$BUILD/awg-tools" --strip-components=1 -xzf "$BUILD/awg-tools.tgz"
make -s -C "$BUILD/awg-tools/src" wg >/dev/null
install -m 0755 "$BUILD/awg-tools/src/wg" "$OUT/awg"

rm -rf "$BUILD"
{
    echo "xray=$("$OUT/xray" version | head -1)"
    echo "hysteria=$HY2_VERSION"
    echo "sing-box=$SINGBOX_VERSION"
    echo "amneziawg-go=$AWG_GO_REF"
    echo "amneziawg-tools=$AWG_TOOLS_REF"
} | tee "$OUT/VERSIONS"
