#!/usr/bin/env bash
# 01-firewall.sh — UFW + fail2ban
# Открывает: 22/tcp (SSH), $PANEL_PORT/tcp (3x-ui), $VLESS_PORT/tcp,
#            $HY2_PORT/udp, $AWG_PORT/udp

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
config_load

# ------------------------------------------------------------
# 1. Установка UFW + fail2ban
# ------------------------------------------------------------

log_info "установка ufw + fail2ban"
wait_for_apt
apt_install ufw fail2ban

# ------------------------------------------------------------
# 2. UFW правила
# ------------------------------------------------------------

if is_container; then
    log_warn "контейнер — ufw активация пропущена (нет cap_net_admin), правила запишутся, но enable пропустим"
fi

# Сбрасываем только если ещё не настраивали (idempotent)
if ! ufw status 2>/dev/null | grep -q "Status: active"; then
    log_info "сброс UFW в дефолт"
    ufw --force reset >/dev/null 2>&1 || true
fi

# Дефолтные политики
ufw default deny incoming  >/dev/null 2>&1 || true
ufw default allow outgoing >/dev/null 2>&1 || true

# SSH — обязательно ДО enable, иначе закроем себе доступ
log_info "разрешаю SSH (22/tcp)"
ufw allow 22/tcp comment "SSH" >/dev/null 2>&1 || true

# Панель 3x-ui
log_info "разрешаю 3x-ui панель (${PANEL_PORT}/tcp)"
ufw allow "${PANEL_PORT}/tcp" comment "3x-ui panel" >/dev/null 2>&1 || true

# VLESS+Reality
log_info "разрешаю VLESS+Reality (${VLESS_PORT}/tcp)"
ufw allow "${VLESS_PORT}/tcp" comment "VLESS+Reality" >/dev/null 2>&1 || true

# Hysteria2
log_info "разрешаю Hysteria2 (${HY2_PORT}/udp)"
ufw allow "${HY2_PORT}/udp" comment "Hysteria2" >/dev/null 2>&1 || true

# AmneziaWG v2
log_info "разрешаю AmneziaWG v2 (${AWG_PORT}/udp)"
ufw allow "${AWG_PORT}/udp" comment "AmneziaWG v2" >/dev/null 2>&1 || true

# Активируем UFW
if ! is_container; then
    log_info "активирую UFW"
    ufw --force enable >/dev/null 2>&1 || log_warn "ufw enable не удалось (возможно нет cap_net_admin)"
    log_info "ufw status:"
    ufw status verbose 2>/dev/null | head -20 || true
fi

# ------------------------------------------------------------
# 3. fail2ban для sshd
# ------------------------------------------------------------

log_info "настройка fail2ban для sshd"
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
port = ssh
filter = sshd
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF

if ! is_container; then
    systemctl enable fail2ban >/dev/null 2>&1 || true
    systemctl restart fail2ban >/dev/null 2>&1 || log_warn "fail2ban не стартует (возможно нет systemd в окружении)"
    if systemctl is-active --quiet fail2ban; then
        log_ok "fail2ban активен"
        fail2ban-client status sshd 2>/dev/null | head -5 || true
    fi
else
    log_warn "контейнер — fail2ban systemd-сервис пропущен, конфиг записан"
fi

log_ok "01-firewall завершён"
