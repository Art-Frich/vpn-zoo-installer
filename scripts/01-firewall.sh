#!/usr/bin/env bash
# 01-firewall.sh — UFW (default deny, SSH с limit) + fail2ban для sshd.
# Порты протоколов здесь не хардкодятся: их открывает фаза-владелец через fw_allow,
# реестр — /etc/vpn-setup/ports.tsv. Эта фаза переприменяет реестр.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
config_load

# ------------------------------------------------------------
# 1. Пакеты
# ------------------------------------------------------------

log_info "установка ufw, fail2ban, python3-systemd"
wait_for_apt
apt_install ufw fail2ban python3-systemd

# ------------------------------------------------------------
# 2. SSH-порты
# ------------------------------------------------------------

mapfile -t ssh_ports < <(detect_ssh_ports)
[ "${#ssh_ports[@]}" -gt 0 ] || die "не удалось определить SSH-порт — задайте SSH_PORTS=22"
log_info "SSH-порты: ${ssh_ports[*]}"
config_set SSH_PORTS "$(IFS=,; echo "${ssh_ports[*]}")"

# ------------------------------------------------------------
# 3. UFW: без reset — чужие правила на живой системе сохраняются
# ------------------------------------------------------------

# Сначала правила SSH, потом default deny: на уже активном UFW с default allow
# обратный порядок сразу отрезал бы текущую SSH-сессию
# Старое правило allow 22/tcp удалять не нужно: ufw limit на тот же порт заменяет его
# на месте («Rule updated»), без окна, когда SSH не разрешён
for p in "${ssh_ports[@]}"; do
    fw_allow "$p/tcp" "SSH" limit
done

ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null

# Наследие старой версии: панель 3x-ui наружу (2053/PANEL_PORT) больше не открываем
legacy_rules="$(ufw show added 2>/dev/null | grep -E "comment '3x-ui panel'" || true)"
if [ -n "$legacy_rules" ]; then
    while read -r rule; do
        spec="$(awk '{print $3}' <<< "$rule")"
        _fw_valid "$spec" || continue
        ufw --force delete allow "$spec" >/dev/null && log_info "закрыт порт старой панели: $spec"
    done <<< "$legacy_rules"
fi

fw_apply_registry

# Перед enable правило SSH обязано быть в списке, иначе отрежем себе доступ
added="$(ufw show added 2>/dev/null)"
for p in "${ssh_ports[@]}"; do
    grep -qE "ufw limit ${p}/tcp" <<< "$added" || die "правило SSH ${p}/tcp не добавилось — UFW не включаю"
done

if _fw_ufw_active; then
    ufw reload >/dev/null
    log_ok "UFW уже был включён — правила обновлены"
else
    ufw --force enable >/dev/null || die "ufw enable не удался"
    log_ok "UFW включён"
fi
# Журнал атак (zoo journal) берёт блокировки закрытых портов из журнала ядра. ufw сам включает
# уровень low (не чаще 3 записей в минуту на весь сервер), поэтому трогаем только выключенный лог;
# уровень, выбранный владельцем, не меняем. Через переменную: grep -q в пайпе при pipefail падает по SIGPIPE
ufw_state="$(ufw status verbose 2>/dev/null || true)"
if grep -q '^Logging: off' <<< "$ufw_state"; then
    ufw logging low >/dev/null || die "ufw logging low не включился"
    log_info "ufw: лог блокировок был выключен — включён low (для zoo journal)"
fi
ufw status verbose | sed 's/^/    /'

# ------------------------------------------------------------
# 4. fail2ban: sshd (journal) + recidive, бан через ufw
# ------------------------------------------------------------

# старый файл первой версии — заменяем своим
if [ -f /etc/fail2ban/jail.d/sshd.local ] && grep -q '^\[sshd\]' /etc/fail2ban/jail.d/sshd.local \
   && grep -q 'backend = systemd' /etc/fail2ban/jail.d/sshd.local; then
    rm -f /etc/fail2ban/jail.d/sshd.local
fi

ssh_port_list="$(IFS=,; echo "${ssh_ports[*]}")"
cat > /etc/fail2ban/jail.d/vpn-zoo.local <<EOF
# vpn-zoo: fail2ban для SSH
[DEFAULT]
banaction = ufw
banaction_allports = ufw

[sshd]
enabled  = true
port     = ${ssh_port_list}
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h

# повторные нарушители: бан на неделю по всем портам
[recidive]
enabled  = true
bantime  = 1w
findtime = 1d
maxretry = 5
EOF

# recidive читает /var/log/fail2ban.log — файл должен существовать
touch /var/log/fail2ban.log

fail2ban-client -t >/dev/null 2>&1 || { fail2ban-client -t || true; die "конфиг fail2ban не прошёл проверку"; }
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban
for _ in $(seq 1 15); do
    fail2ban-client ping >/dev/null 2>&1 && break
    sleep 1
done
fail2ban-client status sshd >/dev/null 2>&1 || { journalctl -u fail2ban -n 30 --no-pager || true; die "fail2ban: jail sshd не поднялся"; }
log_ok "fail2ban активен: $(fail2ban-client status | awk -F': *' '/Jail list/ {print $2}')"

log_ok "01-firewall завершён"
