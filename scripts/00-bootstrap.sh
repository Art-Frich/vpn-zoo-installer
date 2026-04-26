#!/usr/bin/env bash
# 00-bootstrap.sh — apt update, базовые пакеты, swap (1 GB), sysctl + BBR

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# ------------------------------------------------------------
# 1. apt update + базовые пакеты
# ------------------------------------------------------------

log_info "apt update + upgrade"
wait_for_apt
apt_update
DEBIAN_FRONTEND=noninteractive apt-get -yq \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    upgrade

log_info "установка базовых пакетов"
apt_install \
    curl wget ca-certificates gnupg lsb-release \
    sudo socat cron nano htop \
    jq qrencode sqlite3 \
    iptables iproute2 \
    openssl

# ------------------------------------------------------------
# 2. Swap 1 GB (если RAM < 2 GB и swap ещё нет)
# ------------------------------------------------------------

if is_container; then
    log_warn "контейнер — swap пропущен"
else
    if [ ! -f /swapfile ] && ! swapon --show | grep -q '^/'; then
        local_ram_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
        if [ "$local_ram_kb" -lt 2097152 ]; then
            log_info "RAM < 2 GB ($((local_ram_kb/1024)) MiB) — создаю swap 1 GB"
            fallocate -l 1G /swapfile
            chmod 600 /swapfile
            mkswap /swapfile
            swapon /swapfile
            grep -q '^/swapfile' /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
            log_ok "swap 1 GB активирован"
        else
            log_info "RAM >= 2 GB — swap не нужен"
        fi
    else
        log_info "swap уже настроен"
    fi
fi

# ------------------------------------------------------------
# 3. sysctl: swappiness, BBR + TCP tuning
# ------------------------------------------------------------

if is_container; then
    log_warn "контейнер — sysctl пропущен (нет доступа к kernel parameters)"
else
    log_info "sysctl: swappiness, BBR, TCP buffers"

    cat > /etc/sysctl.d/99-vpn-setup.conf <<EOF
# VPN setup — swappiness + BBR + TCP tuning
vm.swappiness = 10

# Включаем BBR + fq для эффективной congestion control
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# TCP buffers (помогает Hysteria2/QUIC и AWG на жирных каналах)
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.ipv4.tcp_rmem = 4096 87380 67108864
net.ipv4.tcp_wmem = 4096 65536 67108864

# IP forwarding (нужно для AWG MASQUERADE)
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1

# Меньшая агрессия на UDP-flood (но не нулевая, чтобы Hy2 не страдал)
net.ipv4.udp_mem = 8388608 12582912 16777216

# Увеличиваем conntrack table (на случай если много клиентов)
net.netfilter.nf_conntrack_max = 1048576
EOF

    if sysctl -p /etc/sysctl.d/99-vpn-setup.conf >/dev/null 2>&1; then
        log_ok "sysctl применён"
    else
        log_warn "часть sysctl-параметров не применена (вероятно nf_conntrack модуль не загружен) — это нормально"
        sysctl -p /etc/sysctl.d/99-vpn-setup.conf 2>&1 | grep -v "No such file" | head -5 || true
    fi

    # Проверяем что BBR реально активен
    cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo "?")
    log_info "tcp_congestion_control = $cc"
    if [ "$cc" = "bbr" ]; then
        log_ok "BBR активен"
    else
        log_warn "BBR не активен (cc=$cc) — возможно ядро без поддержки. На Ubuntu 22.04+ должно работать."
    fi
fi

log_ok "00-bootstrap завершён"
