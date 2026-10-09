#!/usr/bin/env bash
# 07-routing.sh — анти-утечки (D9, RISK-REDUCTION §5): правила Xray через шаблон 3x-ui,
# ACL Hysteria, opt-in RU-фильтр AmneziaWG, проверенные geo-файлы runetfreedom и
# ежедневный таймер их обновления с откатом.
#
#   echo-сервисы («мой IP»)       → warp (ENABLE_WARP=1 и WARP проверен) или блок — всегда
#   RU-назначения Xray            → RU_EGRESS=direct|block|warp (по умолчанию direct)
#   RU-назначения Hysteria / AWG  → HY2_RU_EGRESS / AWG_RU_EGRESS (opt-in, по умолчанию direct)
#   bittorrent                    → блок (ENABLE_BITTORRENT=1 — пропускать; только Xray)
#
# Повторный запуск безопасен: правила с ruleTag zoo-* пересобираются, свои правила
# владельца в шаблоне Xray сохраняются ниже наших.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/xui.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/routing.sh"
config_load
versions_load
detect_arch >/dev/null

# ------------------------------------------------------------
# 1. Параметры
# ------------------------------------------------------------

config_default RU_EGRESS direct
config_default HY2_RU_EGRESS direct
config_default AWG_RU_EGRESS direct
config_default ENABLE_BITTORRENT 0

for key in RU_EGRESS HY2_RU_EGRESS AWG_RU_EGRESS; do
    _routing_norm "${!key}" >/dev/null || die "$key=${!key}: допустимо direct, block или warp"
done
case "$ENABLE_BITTORRENT" in 0|1) ;; *) die "ENABLE_BITTORRENT=$ENABLE_BITTORRENT: допустимо 0 или 1" ;; esac
# BitTorrent блокируется ради хостера: жалобы правообладателей приходят на IP сервера.
# Ловится только по sniffing (рукопожатие BT); шифрованный BT (MSE) и Hysteria/AWG — нет
[ "$ENABLE_BITTORRENT" = "1" ] && log_warn "ENABLE_BITTORRENT=1: торренты через сервер разрешены (риск жалоб хостеру)"

for key in RU_EGRESS HY2_RU_EGRESS; do
    if [ "$(_routing_norm "${!key}")" = "warp" ] && ! routing_warp_ready; then
        if [ "${ENABLE_WARP:-0}" = "1" ]; then
            log_info "$key=warp: WARP настроит фаза 08, до этого RU-назначения блокируются"
        else
            log_warn "$key=warp, но WARP выключен (ENABLE_WARP=0) — RU-назначения будут блокироваться"
        fi
    fi
done
if [ "$(_routing_norm "$RU_EGRESS")" != "direct" ]; then
    log_warn "RU_EGRESS=$RU_EGRESS: у клиентов без сплита «РФ напрямую» перестанут нормально работать банки и Госуслуги"
fi

command -v python3 >/dev/null || die "нет python3 (ставит фаза 00)"
[ -x "$(xui_xray_bin)" ] || die "нет Xray 3x-ui ($(xui_xray_bin)) — сначала фаза 03"
xui_wait_api 60 || die "API 3x-ui не отвечает — проверь фазу 03 (systemctl status x-ui)"

# ------------------------------------------------------------
# 2. Geo-файлы runetfreedom
# ------------------------------------------------------------

log_info "проверка geo-файлов (runetfreedom, закреплён $GEO_RU_TAG)"
routing_geo_ensure

# ------------------------------------------------------------
# 3. Xray: sniffing на inbound + шаблон маршрутизации
# ------------------------------------------------------------

fixed="$(routing_sniffing_fix)" || die "не удалось проверить sniffing inbound"
[ "$fixed" = "0" ] || log_ok "sniffing routeOnly включён на $fixed inbound"

routing_xray_apply || die "не удалось применить маршрутизацию Xray (шаблон не изменён)"

# ------------------------------------------------------------
# 4. Hysteria ACL, AWG RU-фильтр
# ------------------------------------------------------------

routing_hy2_apply || die "не удалось применить ACL Hysteria (конфиг восстановлен из бэкапа)"
routing_awg_apply || die "не удалось применить RU-фильтр AWG"
log_info "AWG: echo-правило на L3 невозможно (доменов нет) — защищает только сплит на клиенте"

# ------------------------------------------------------------
# 5. Таймер обновления geo
# ------------------------------------------------------------

routing_timer_install || die "не удалось установить таймер обновления geo"

# ------------------------------------------------------------
# 6. Самопроверка: Xray работает, маршруты те, что ожидаем
# ------------------------------------------------------------

routing_xray_wait_running 30 || die "Xray не работает после применения маршрутизации"

want_echo="$(routing_echo_egress)"; [ "$want_echo" = "block" ] && want_echo=blocked
got="$(routing_route_test domain=api.ipify.org)" || die "routeTest API не ответил"
[ "$got" = "$want_echo" ] || die "api.ipify.org уходит в «${got:-direct}», ожидался $want_echo"
got="$(routing_route_test ip=192.168.1.1)"
[ "$got" = "blocked" ] || die "приватные адреса не блокируются (outbound «${got:-direct}»)"
got="$(routing_route_test domain=example.com)"
case "$got" in ""|direct) ;; *) die "обычный домен example.com уходит в «$got», ожидался direct" ;; esac
ru_eff="$(routing_effective "$RU_EGRESS")"
want_ru="$(case "$ru_eff" in block) echo blocked ;; warp) echo warp ;; *) echo direct ;; esac)"
got="$(routing_route_test domain=gosuslugi.ru)"
[ "${got:-direct}" = "$want_ru" ] || die "gosuslugi.ru уходит в «${got:-direct}», ожидался $want_ru (RU_EGRESS=$RU_EGRESS)"
log_ok "маршруты: echo → $want_echo, приватные → blocked, RU → $want_ru, остальное → direct"

if [ "$(xui_inbound_list | jq '[.[] | select(.protocol != "wireguard" and .protocol != "tunnel" and .protocol != "mtproto") | select((.sniffing.enabled and .sniffing.routeOnly) | not)] | length')" != "0" ]; then
    die "есть inbound без sniffing — правила по доменам для них не работают"
fi
systemctl is-active --quiet vpn-zoo-geo-update.timer || die "таймер vpn-zoo-geo-update не активен"

log_ok "анти-утечки применены. Статус: bash -c '. $ROUTING_SHARE/scripts/lib.sh; . $ROUTING_SHARE/scripts/lib/xui.sh; . $ROUTING_SHARE/scripts/lib/routing.sh; config_load; routing_status'"
log_info "обновление geo: $ROUTING_UPDATER (таймер vpn-zoo-geo-update.timer, ежедневно ночью)"
