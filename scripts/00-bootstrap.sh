#!/usr/bin/env bash
# 00-bootstrap.sh — apt, базовые пакеты, swap, sysctl, conntrack, unattended-upgrades

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
config_load

# ------------------------------------------------------------
# 1. apt + базовые пакеты
# ------------------------------------------------------------

wait_for_apt
log_info "apt update"
apt_update

if [ "${ZOO_SKIP_UPGRADE:-0}" = "1" ]; then
    log_warn "ZOO_SKIP_UPGRADE=1 — apt upgrade пропущен"
else
    # upgrade, не dist-upgrade: новые пакеты ядра не ставим (ядро — забота 02-kernel)
    log_info "apt upgrade"
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get -yq \
        -o DPkg::Lock::Timeout="$APT_LOCK_TIMEOUT" \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        upgrade
fi

log_info "установка базовых пакетов"
apt_install \
    curl wget ca-certificates gnupg lsb-release \
    sudo socat cron nano htop \
    jq qrencode sqlite3 python3 \
    iptables iproute2 kmod procps psmisc \
    openssl tar gzip unzip \
    unattended-upgrades

for c in jq curl openssl sqlite3 python3 flock ss; do
    command -v "$c" >/dev/null || die "после установки нет команды $c"
done

# ------------------------------------------------------------
# 2. Swap 1 GB (RAM < 2 GB и swap ещё нет)
# ------------------------------------------------------------

if kernel_is_foreign; then
    log_warn "контейнер/стенд — swap пропущен (swapon затронул бы хост)"
elif [ -f /swapfile ] || swapon --show --noheadings | grep -q .; then
    log_info "swap уже настроен"
else
    ram_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
    if [ "$ram_kb" -lt 2097152 ]; then
        log_info "RAM $((ram_kb/1024)) МБ < 2 ГБ — создаю swap 1 ГБ"
        fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        grep -q '^/swapfile' /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
        log_ok "swap 1 ГБ включён"
    else
        log_info "RAM ≥ 2 ГБ — swap не нужен"
    fi
fi

# ------------------------------------------------------------
# 3. Модули: conntrack (для nf_conntrack_max) и BBR — при каждой загрузке
# ------------------------------------------------------------

cat > /etc/modules-load.d/vpn-zoo.conf <<'EOF'
# vpn-zoo: нужны до применения sysctl (nf_conntrack_max, tcp_congestion_control=bbr)
nf_conntrack
tcp_bbr
EOF

if kernel_is_foreign; then
    log_info "контейнер/стенд — modprobe пропущен (модули грузит ядро хоста)"
else
    modprobe nf_conntrack || log_warn "modprobe nf_conntrack не удался — nf_conntrack_max не применится"
    modprobe tcp_bbr || log_warn "modprobe tcp_bbr не удался — BBR недоступен в этом ядре"
fi

# ------------------------------------------------------------
# 4. sysctl
# ------------------------------------------------------------

SYSCTL_FILE=/etc/sysctl.d/99-vpn-setup.conf

# Старая версия ставила ipv6 forwarding глобально — откатываем, если это было наше
had_v6_fwd=0
if [ -f "$SYSCTL_FILE" ] && grep -q '^net.ipv6.conf.all.forwarding *= *1' "$SYSCTL_FILE"; then
    had_v6_fwd=1
fi

cat > "$SYSCTL_FILE" <<'EOF'
# vpn-zoo: sysctl. Без net.ipv4.udp_mem (значение в страницах, старое = десятки ГБ)
# и без глобального ipv6 forwarding (AWG работает по IPv4; forwarding ломает приём RA).
vm.swappiness = 10

# BBR + fq: влияет на TCP (VLESS/XHTTP); на QUIC/Hy2 и AWG не влияет
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Буферы сокетов: quic-go просит ≥ 7.5 МБ, документация Hysteria — 16 МБ
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# NAT для AWG
net.ipv4.ip_forward = 1

# Таблица conntrack (модуль nf_conntrack грузится через modules-load.d)
net.netfilter.nf_conntrack_max = 262144
EOF

# Применяем по одному ключу: ошибки видны, а не глушатся
failed_keys=()
while IFS='=' read -r key value; do
    key="$(echo "$key" | xargs)"; value="$(echo "$value" | xargs)"
    case "$key" in ''|'#'*) continue ;; esac
    if kernel_is_foreign; then
        # Глобальные (не привязанные к netns) параметры в стенде меняли бы хост
        case "$key" in
            net.ipv4.ip_forward|net.ipv4.tcp_*) ;;
            *) log_info "стенд: $key не применяю (глобальный параметр)"; continue ;;
        esac
    fi
    if ! sysctl -q -w "$key=$value" >/dev/null 2>&1; then
        failed_keys+=("$key")
    fi
done < "$SYSCTL_FILE"

if [ "$had_v6_fwd" = "1" ] && ! kernel_is_foreign; then
    sysctl -q -w net.ipv6.conf.all.forwarding=0 && log_info "ipv6 forwarding (от старой версии) выключен"
fi

if [ "${#failed_keys[@]}" -gt 0 ]; then
    log_warn "sysctl не применились: ${failed_keys[*]}"
fi
[ "$(sysctl -n net.ipv4.ip_forward)" = "1" ] || die "net.ipv4.ip_forward не включился — NAT для AWG не заработает"

cc="$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo '?')"
if [ "$cc" = "bbr" ]; then
    log_ok "BBR активен"
else
    log_warn "BBR не активен (cc=$cc, доступны: $(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || echo '?')). Работать будет, TCP чуть медленнее на потерях"
fi

# ------------------------------------------------------------
# 4b. journald: потолок объёма системного журнала (по умолчанию — 10 % диска, до 4 ГБ).
# Бюджет данных zoo (ZOO_DATA_LIMIT) journald не включает: он ограничен отдельно
# ------------------------------------------------------------

JOURNALD_DROPIN=/etc/systemd/journald.conf.d/50-vpn-zoo.conf
journald_new="$(mktemp)"
cat > "$journald_new" <<'EOF'
# vpn-zoo: потолок объёма системного журнала
[Journal]
SystemMaxUse=500M
EOF
if cmp -s "$journald_new" "$JOURNALD_DROPIN" 2>/dev/null; then
    log_info "journald: SystemMaxUse=500M уже задан"
else
    mkdir -p "$(dirname "$JOURNALD_DROPIN")"
    install -m 0644 "$journald_new" "$JOURNALD_DROPIN"
    if systemctl restart systemd-journald >/dev/null 2>&1; then
        log_ok "journald: SystemMaxUse=500M"
    else
        log_warn "journald: лимит записан в $JOURNALD_DROPIN, но systemd-journald не перезапустился (подействует после перезагрузки)"
    fi
fi
rm -f "$journald_new"

# ------------------------------------------------------------
# 5. unattended-upgrades: security-обновления + перезагрузка ночью
# ------------------------------------------------------------

AUTO_REBOOT="${AUTO_REBOOT:-1}"
AUTO_REBOOT_TIME="${AUTO_REBOOT_TIME:-04:00}"
[[ "$AUTO_REBOOT_TIME" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] || die "AUTO_REBOOT_TIME: ожидаю ЧЧ:ММ, получено $AUTO_REBOOT_TIME"

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

if [ "$AUTO_REBOOT" = "1" ]; then reboot_flag=true; else reboot_flag=false; fi
cat > /etc/apt/apt.conf.d/52vpn-zoo-unattended <<EOF
// vpn-zoo: новые ядра и libc применяются только после reboot
Unattended-Upgrade::Automatic-Reboot "${reboot_flag}";
Unattended-Upgrade::Automatic-Reboot-Time "${AUTO_REBOOT_TIME}";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
EOF
config_default AUTO_REBOOT "$AUTO_REBOOT"
config_default AUTO_REBOOT_TIME "$AUTO_REBOOT_TIME"
log_ok "unattended-upgrades: включены, автоперезагрузка=${reboot_flag} в ${AUTO_REBOOT_TIME}"

if [ -f /var/run/reboot-required ]; then
    log_warn "система просит перезагрузку (/var/run/reboot-required): $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null | head -c 200)"
fi

log_ok "00-bootstrap завершён"
