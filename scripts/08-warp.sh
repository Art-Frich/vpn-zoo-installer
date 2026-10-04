#!/usr/bin/env bash
# 08-warp.sh — Cloudflare WARP как outbound Xray (opt-in, ENABLE_WARP=1).
#
# Вариант A (исследование §5.2): регистрация встроенным WARP 3x-ui (POST xray/warp/reg),
# outbound «warp» (wireguard, noKernelTun) собирает инсталлер — API панели его не создаёт.
# Для Hysteria — локальный socks-вход Xray «zoo-warp-in» (127.0.0.1, пароль), ACL шлёт
# в него echo (и RU при HY2_RU_EGRESS=warp). AWG через WARP не ходит (нужен ядерный warp0).
#
# Проверка: запрос через warp на cloudflare.com/cdn-cgi/trace должен дать warp=on.
# Не прошла — WARP_READY=0, echo-сервисы и RU_EGRESS=warp блокируются, фаза завершается
# с предупреждением (повтор: install.sh --phase 08; новая регистрация: WARP_REREGISTER=1).

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/routing.sh"
config_load
versions_load
detect_arch >/dev/null

if [ "${ENABLE_WARP:-0}" != "1" ]; then
    # выключили после установки: убрать outbound warp и вход zoo-warp-in, echo → блок
    if [ -n "${WARP_PRIV:-}" ]; then
        xui_wait_api 60 || die "API 3x-ui не отвечает — проверь фазу 03 (systemctl status x-ui)"
        routing_xray_apply || die "не удалось убрать warp из маршрутов Xray"
        routing_hy2_apply || die "не удалось обновить ACL Hysteria"
        log_ok "WARP выключен: echo-сервисы блокируются, RU_EGRESS=warp → block"
    else
        log_info "WARP выключен (ENABLE_WARP=0)"
    fi
    exit 0
fi

xui_wait_api 60 || die "API 3x-ui не отвечает — проверь фазу 03 (systemctl status x-ui)"
command -v python3 >/dev/null || die "нет python3 (ставит фаза 00)"

# Без 07 нет geo-файлов и правил, на которые опирается warp
if [ ! -s "$ROUTING_HY_GEOSITE" ]; then
    log_info "фаза 07 ещё не выполнялась — готовлю geo-файлы"
    routing_geo_ensure
fi

# ------------------------------------------------------------
# 1. Локальный socks-вход для Hysteria и самопроверки
# ------------------------------------------------------------

config_default WARP_SOCKS_USER zoo-warp
if [ -z "${WARP_SOCKS_PORT:-}" ] || ! [[ "$WARP_SOCKS_PORT" =~ ^[0-9]+$ ]]; then
    port="$(rand_port)" || die "не нашёл свободный порт для zoo-warp-in"
    config_set WARP_SOCKS_PORT "$port"
fi
if [ -z "${WARP_SOCKS_PASS:-}" ]; then
    pass="$(gen_random_alnum 24)" || die "не удалось сгенерировать пароль"
    config_set WARP_SOCKS_PASS "$pass"
fi

# ------------------------------------------------------------
# 2. Регистрация (один раз; ключи в config.env)
# ------------------------------------------------------------

fallback() {
    config_set WARP_READY 0
    config_set WARP_CHECKED_AT "$(date -Iseconds)"
    routing_xray_apply || log_err "не удалось переключить маршруты на блок"
    routing_hy2_apply || log_err "не удалось обновить ACL Hysteria"
    log_warn "WARP не работает: $1"
    log_warn "echo-сервисы и RU_EGRESS=warp теперь блокируются. Повтор: install.sh --phase 08 (новая регистрация: WARP_REREGISTER=1)"
    exit 0
}

registered_now=0
if [ -z "${WARP_PRIV:-}" ] || [ "${WARP_REREGISTER:-0}" = "1" ]; then
    log_info "регистрация WARP через 3x-ui (api.cloudflareclient.com)"
    retry 3 warp_register || fallback "регистрация в Cloudflare не удалась (429 с IP дата-центра? нет доступа к api.cloudflareclient.com?)"
    config_load
    registered_now=1
    log_ok "WARP зарегистрирован: адрес $WARP_V4, endpoint $WARP_ENDPOINT"
else
    log_info "WARP уже зарегистрирован ($WARP_V4) — новая регистрация: WARP_REREGISTER=1"
fi

# ------------------------------------------------------------
# 3. Outbound warp + проверка через локальный вход
# ------------------------------------------------------------

ROUTING_WARP_PROBE=1 routing_xray_apply || fallback "Xray не принял outbound warp"

if ! warp_smoke; then
    if [ "$registered_now" = "0" ]; then
        log_warn "старая регистрация WARP не работает — регистрирую заново"
        retry 3 warp_register || fallback "повторная регистрация не удалась"
        config_load
        ROUTING_WARP_PROBE=1 routing_xray_apply || fallback "Xray не принял outbound warp"
        warp_smoke || fallback "трафик через warp не выходит с адреса Cloudflare (UDP 2408 до engage.cloudflareclient.com закрыт?)"
    else
        fallback "трафик через warp не выходит с адреса Cloudflare (UDP 2408 до engage.cloudflareclient.com закрыт?)"
    fi
fi
config_set WARP_READY 1
config_set WARP_EXIT_IP "$WARP_EXIT_IP"
config_set WARP_CHECKED_AT "$(date -Iseconds)"
log_ok "WARP работает: выход через Cloudflare $WARP_EXIT_IP (warp=on)"

# ------------------------------------------------------------
# 4. Маршруты с warp: echo-сервисы (и RU_EGRESS=warp) → warp
# ------------------------------------------------------------

routing_xray_apply || die "не удалось применить маршруты с warp"
routing_hy2_apply || die "не удалось обновить ACL Hysteria"

got="$(routing_route_test domain=api.ipify.org)"
[ "$got" = "warp" ] || die "api.ipify.org уходит в «${got:-direct}», ожидался warp"
warp_smoke || die "WARP перестал отвечать после применения маршрутов"
log_ok "echo-сервисы → WARP ($WARP_EXIT_IP); RU_EGRESS=$RU_EGRESS → $(routing_effective "$RU_EGRESS")"
log_info "WARP не делает трафик российским: это IP Cloudflare (AS13335) со страной VPS"
