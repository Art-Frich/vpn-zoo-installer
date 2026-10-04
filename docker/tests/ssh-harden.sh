#!/usr/bin/env bash
# docker/tests/ssh-harden.sh SERVER [--keep] — фаза 01b (SSH_HARDEN): перенос SSH без риска
# потерять доступ. Клиент — отдельный контейнер в zoo-net со своим ключом.
#
#   a) без ключей в authorized_keys фаза отказывается и ничего не меняет;
#   b) шаг 1 из SSH-сессии: оба порта пускают по ключу (root и пользователь с sudo), пароль
#      не предлагается, ssh -L к сервису на 127.0.0.1 через новый порт работает, ufw/fail2ban/
#      config.env на оба порта, сессия, запустившая шаг 1, не оборвалась;
#   c) без подтверждения таймер (SSH_REVERT_MIN=1) возвращает всё как было, включая вход по
#      паролю; пока занята блокировка install.sh, откат откладывается;
#   d) подтверждение отклоняется из сессии на старом порту, без SSH-сессии и из сессии на
#      новом порту с адреса самого сервера (ssh -J; так же выглядит вход через его VPN),
#      проходит из сессии на новом: старый порт закрыт (ufw и не слушается), новый работает;
#   e) SSH_HARDEN=0 в два запуска: первый возвращает вход как был, но оставляет открытым и
#      порт фазы; второй закрывает его только из сессии на исходном порту;
#   f) повторные запуски 01b и 01 ничего не меняют;
#   g) при полной установке (есть 07/09/99): шаг 1 внутри обычного install.sh — инструкция
#      последней, 07 пускает из туннеля на оба порта, 99 печатает старый порт; после
#      подтверждения 07, CREDENTIALS.md, zoo web --info и подсказка пробника — новый порт.
#
# Без установленной 01 тест сам ставит 00 и 01. В конце сервер возвращается к исходному SSH
# (SSH_HARDEN=0), ключ клиента и тестовый пользователь удаляются. Каждое SSH-подключение
# выдерживает паузу: ufw limit пропускает не больше 6 подключений за 30 с с одного адреса.
# Код выхода 0 — всё PASS.

# pass/fail возвращают 0: «A && pass || fail» безопасно; $ в одинарных кавычках — для сервера
# shellcheck disable=SC2015,SC2016

set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:?usage: ssh-harden.sh SERVER [--keep]}"
KEEP="${2:-}"
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
CLI="${SRV}-sshcli"
ST=/var/lib/vpn-setup/ssh
DROPIN=/etc/ssh/sshd_config.d/00-vpn-zoo.conf
TEST_USER=zoosshtest
WEB_TEST_PORT=24999
SSH_GAP=7

FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
info() { echo "      $*"; }
step() { echo; echo "=== $* ==="; }

sx() { docker exec -i -e ZOO_TEST_ENV=docker "$SRV" "$@"; }
cx() { docker exec -i "$CLI" "$@"; }
srv_ip() { docker container inspect -f '{{(index .NetworkSettings.Networks "zoo-net").IPAddress}}' "$1"; }

# Время последнего подключения — в файле: cssh вызывается и внутри $(…), то есть в подоболочке
GAP_FILE="$(mktemp)"
ssh_gap() {
    local now last
    now="$(date +%s)"
    last="$(cat "$GAP_FILE" 2>/dev/null)"; last="${last:-0}"
    [ $(( now - last )) -ge "$SSH_GAP" ] || sleep $(( SSH_GAP - (now - last) ))
    date +%s > "$GAP_FILE"
}
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8)
# cssh PORT USER команда… — вход по ключу клиента
cssh() {
    local port="$1" user="$2"
    shift 2
    ssh_gap
    cx ssh "${SSH_OPTS[@]}" -p "$port" "$user@$SIP" "$@"
}
# Методы входа, которые сервер предлагает до аутентификации (без ключа)
auth_methods() {
    ssh_gap
    cx bash -c "ssh -v -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 \
        -o PubkeyAuthentication=no -p $1 root@$SIP true 2>&1 | sed -n 's/.*Authentications that can continue: //p' | head -n 1" || true
}
# Запуск install.sh на сервере из SSH-сессии по ключу (как у пользователя)
remote_install() {
    local port="$1" envs="$2"
    shift 2
    cssh "$port" root "cd /repo && $envs ZOO_TEST_ENV=docker bash scripts/install.sh $*"
}
cfg() { sx bash -c ". /etc/vpn-setup/config.env; printf '%s' \"\${$1:-}\""; }
pending_val() { sx bash -c ". $ST/pending.env 2>/dev/null && printf '%s' \"\${$1:-}\""; }
listening() { [ -n "$(sx ss -Htln "sport = :$1" 2>/dev/null)" ]; }
ufw_has() { sx ufw status 2>/dev/null | grep -qE "^$1/tcp +LIMIT"; }
jail_ports() { sx awk -F'= *' '/^port/ {print $2}' /etc/fail2ban/jail.d/vpn-zoo.local; }
state_of() { sx bash -c "grep '^$1=' /var/lib/vpn-setup/state | tail -n 1 | cut -d= -f2"; }

# Исходный SSH без SSH-сессии: SSH_HARDEN=0 дважды (переходный шаг и закрытие порта фазы,
# второе — с SSH_CONFIRM_FORCE=1, как из консоли хостера)
restore_ssh() {
    sx bash -c 'cd /repo && SSH_HARDEN=0 bash scripts/install.sh --phase 01b' >/dev/null 2>&1 \
        && sx bash -c 'cd /repo && SSH_HARDEN=0 SSH_CONFIRM_FORCE=1 bash scripts/install.sh --phase 01b' >/dev/null 2>&1
}

# shellcheck disable=SC2329 # вызывается из trap
cleanup() {
    set +e
    if [ "${HARDENED_TOUCHED:-0}" = "1" ] && sx test -f "$ST/orig.env"; then
        info "возвращаю SSH (SSH_HARDEN=0)"
        restore_ssh
    fi
    sx bash -c "pkill -f '[z]oo-ssh-web-test'; pkill -u $TEST_USER; sleep 1; userdel -r $TEST_USER 2>/dev/null; \
        rm -f /etc/sudoers.d/$TEST_USER /etc/fail2ban/jail.d/zz-zoo-ssh-test.local; \
        if [ -f /root/.ssh/authorized_keys.zoo-test ]; then mv -f /root/.ssh/authorized_keys.zoo-test /root/.ssh/authorized_keys; \
        else rm -f /root/.ssh/authorized_keys; fi; fail2ban-client reload >/dev/null 2>&1" >/dev/null 2>&1
    rm -f "$GAP_FILE"
    [ "$KEEP" = "--keep" ] && { info "клиент оставлен: $CLI"; return 0; }
    docker rm -f "$CLI" >/dev/null 2>&1
}
trap cleanup EXIT

# ------------------------------------------------------------
step "подготовка"
# ------------------------------------------------------------

docker container inspect "$SRV" >/dev/null 2>&1 || { echo "нет контейнера $SRV"; exit 2; }
SIP="$(srv_ip "$SRV")"
if [ "$(state_of 01-firewall)" != "done" ]; then
    info "на сервере нет фазы 01 — ставлю 00 и 01"
    sx bash -c "cd /repo && ZOO_SKIP_UPGRADE=1 SERVER_IP=$SIP bash scripts/install.sh --only 00,01" >/dev/null 2>&1 \
        || { fail "установка 00,01 не удалась"; exit 1; }
fi
if sx test -f "$ST/orig.env"; then
    info "SSH уже закрыт фазой 01b — возвращаю исходное перед тестом"
    restore_ssh || { fail "SSH_HARDEN=0 перед тестом"; exit 1; }
fi
FULL=0
[ "$(state_of 99-print-creds)" = "done" ] && [ "$(state_of 07-routing)" = "done" ] && FULL=1
OLD="$(cfg SSH_PORTS)"; OLD="${OLD%%,*}"; OLD="${OLD:-22}"
DISTRO="$(sx bash -c '. /etc/os-release; echo $VERSION_ID')"
info "сервер $SRV ($SIP, Ubuntu $DISTRO), SSH-порт $OLD, полная установка: $FULL"

docker rm -f "$CLI" >/dev/null 2>&1 || true
docker run -d --name "$CLI" --hostname "$CLI" --label zoo.harness=1 --network zoo-net \
    --entrypoint sleep "zoo-test-server:24.04" infinity >/dev/null
cx bash -c 'install -d -m 700 /root/.ssh && ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519 <<< y >/dev/null 2>&1; command -v curl >/dev/null'
CIP="$(srv_ip "$CLI")"
PUB="$(cx cat /root/.ssh/id_ed25519.pub)"

# пользователь от прерванного прогона со своим ключом сорвал бы проверку a)
sx bash -c "pkill -u $TEST_USER; sleep 1; userdel -r $TEST_USER; rm -f /etc/sudoers.d/$TEST_USER" >/dev/null 2>&1 || true
# fail2ban не должен банить клиента за пробы «без ключа»; не входящих в sudo-пользователей
# pam_nologin стенда не пускает (systemd-user-sessions в контейнере не стартует)
sx bash -c "printf '[DEFAULT]\nignoreip = 127.0.0.1/8 ::1 $CIP\n' > /etc/fail2ban/jail.d/zz-zoo-ssh-test.local; \
    fail2ban-client reload >/dev/null 2>&1; rm -f /run/nologin; \
    if [ -f /root/.ssh/authorized_keys ]; then mv -f /root/.ssh/authorized_keys /root/.ssh/authorized_keys.zoo-test; fi"

# ------------------------------------------------------------
step "a) нет ключей — отказ без изменений"
# ------------------------------------------------------------

HARDENED_TOUCHED=1
out="$(sx bash -c 'cd /repo && SSH_HARDEN=1 bash scripts/install.sh --phase 01b' 2>&1)" && rc=0 || rc=$?
[ "$rc" != "0" ] && grep -q "нет ни одного ключа" <<< "$out" && pass "без ключей: отказ (rc=$rc)" || { fail "без ключей: rc=$rc"; tail -n 5 <<< "$out"; }
sx test ! -e "$DROPIN" && sx test ! -e "$ST/orig.env" && [ "$(cfg SSH_PORTS)" = "$OLD" ] \
    && pass "ничего не изменено (drop-in нет, SSH_PORTS=$OLD)" || fail "после отказа остались изменения"
# 443 по умолчанию у VLESS (фаза 04 идёт после 01b): явный SSH_PORT=0443 отклоняется
out="$(sx bash -c 'cd /repo && SSH_HARDEN=1 SSH_PORT=0443 bash scripts/install.sh --phase 01b' 2>&1)" && rc=0 || rc=$?
sx sed -i '/^SSH_PORT=/d' /etc/vpn-setup/config.env
[ "$rc" != "0" ] && grep -q "SSH_PORT=443: порт назначен VLESS" <<< "$out" && sx test ! -e "$DROPIN" \
    && pass "SSH_PORT=0443: отказ (порт VLESS), ничего не изменено" || { fail "SSH_PORT=0443: rc=$rc"; tail -n 5 <<< "$out"; }

# ключ root и пользователь с sudo со своим ключом
sx bash -c "install -d -m 700 /root/.ssh && echo '$PUB' > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys; \
    id $TEST_USER >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo $TEST_USER; \
    echo '$TEST_USER ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/$TEST_USER; chmod 440 /etc/sudoers.d/$TEST_USER; \
    install -d -m 700 -o $TEST_USER -g $TEST_USER /home/$TEST_USER/.ssh; \
    echo '$PUB' > /home/$TEST_USER/.ssh/authorized_keys; chown $TEST_USER: /home/$TEST_USER/.ssh/authorized_keys; chmod 600 /home/$TEST_USER/.ssh/authorized_keys"
m="$(auth_methods "$OLD")"
info "до шага 1 сервер предлагает: $m"
case "$m" in *password*) PASS_BEFORE=1 ;; *) PASS_BEFORE=0 ;; esac
if [ "$FULL" = "0" ]; then
    # сервис только на 127.0.0.1 — как админка zoo (при полной установке берётся она сама)
    docker exec -d "$SRV" bash -c "exec -a zoo-ssh-web-test python3 -m http.server --bind 127.0.0.1 $WEB_TEST_PORT --directory /tmp"
    sleep 1
fi

# ------------------------------------------------------------
step "b) шаг 1 из SSH-сессии на старом порту"
# ------------------------------------------------------------

out="$(remote_install "$OLD" "SSH_HARDEN=1 SSH_REVERT_MIN=5" --phase 01b 2>&1)" && rc=0 || rc=$?
NEW="$(pending_val NEW_PORT)"
if [ "$rc" = "0" ] && [ -n "$NEW" ] && grep -q "01b-ssh            OK" <<< "$out"; then
    pass "шаг 1 применён, новый порт $NEW; сессия, запустившая его, дожила до конца"
else
    fail "шаг 1: rc=$rc, NEW=${NEW:-?}"; tail -n 15 <<< "$out"; exit 1
fi
grep -q "ssh -p $NEW " <<< "$out" && grep -q "SSH_CONFIRM=1" <<< "$out" && pass "инструкция: ssh -p $NEW и SSH_CONFIRM=1" || fail "нет инструкции в выводе"
[ "$NEW" -ge 20000 ] && ! grep -qw "$NEW" <<< "1080 3128 8080 9050 2053 54321" && pass "порт случайный высокий" || fail "порт $NEW"
for p in "$OLD" "$NEW"; do
    listening "$p" && pass "порт $p слушается" || fail "порт $p не слушается"
    ufw_has "$p" && pass "ufw limit $p/tcp" || fail "нет ufw limit $p/tcp"
    r="$(cssh "$p" root 'echo ok-$(id -un)' 2>&1 || true)";[ "$r" = "ok-root" ] && pass "порт $p: root по ключу" || fail "порт $p: root по ключу: $r"
    m="$(auth_methods "$p")"; [ "$m" = "publickey" ] && pass "порт $p: предлагается только publickey" || fail "порт $p: методы «$m»"
done
r="$(cssh "$NEW" "$TEST_USER" 'sudo -n id -un' 2>&1 || true)"; [ "$r" = "root" ] && pass "пользователь с sudo по ключу на новом порту" || fail "пользователь $TEST_USER: $r"
[ "$(jail_ports)" = "$(printf '%s\n' "$OLD" "$NEW" | sort -n | paste -sd, -)" ] && pass "fail2ban sshd: порты $(jail_ports)" || fail "fail2ban: $(jail_ports)"
[ "$(cfg SSH_LOGIN_PORT)" = "$OLD" ] && pass "SSH_LOGIN_PORT=$OLD до подтверждения" || fail "SSH_LOGIN_PORT=$(cfg SSH_LOGIN_PORT)"
sx systemctl is-active --quiet vpn-zoo-ssh-revert.timer && pass "таймер отката взведён" || fail "таймер отката не активен"
sx systemctl is-enabled --quiet vpn-zoo-ssh-revert.timer && pass "таймер включён (переживёт reboot)" || fail "таймер не enabled"
eff="$(sx sshd -T 2>/dev/null)"
grep -qx "passwordauthentication no" <<< "$eff" && grep -qx "kbdinteractiveauthentication no" <<< "$eff" \
    && grep -qE "^permitrootlogin (without-password|prohibit-password)$" <<< "$eff" && grep -qx "maxauthtries 3" <<< "$eff" \
    && grep -qx "allowtcpforwarding yes" <<< "$eff" && pass "sshd -T: пароли выключены, root по ключу, проброс портов оставлен" \
    || fail "sshd -T: $(grep -E '^(passwordauth|kbdinter|permitroot|maxauth|allowtcp)' <<< "$eff" | paste -sd' ' -)"

WEBP="$WEB_TEST_PORT"
[ "$FULL" = "1" ] && WEBP="$(cfg ZOO_WEB_PORT)"
ssh_gap
code="$(cx bash -c "ssh ${SSH_OPTS[*]} -o ExitOnForwardFailure=yes -f -N -p $NEW -L 127.0.0.1:18080:127.0.0.1:$WEBP root@$SIP \
    && sleep 1 && curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18080/; pkill -x ssh || true")"
case "$code" in 2??|3??|401|403) pass "ssh -L через новый порт к 127.0.0.1:$WEBP (HTTP $code)" ;; *) fail "ssh -L через новый порт: HTTP «$code»" ;; esac

# ------------------------------------------------------------
step "c) без подтверждения — автооткат (SSH_REVERT_MIN=1)"
# ------------------------------------------------------------

out="$(sx bash -c 'cd /repo && SSH_HARDEN=1 SSH_REVERT_MIN=1 bash scripts/install.sh --phase 01b' 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && [ "$(pending_val NEW_PORT)" = "$NEW" ] && grep -q "уже применён" <<< "$out" \
    && pass "повтор шага 1: тот же порт $NEW, таймер заново" || fail "повтор шага 1: rc=$rc"
# занятая блокировка install.sh: откат должен отложиться, а не идти параллельно
sx bash -c 'flock /run/vpn-setup.lock sleep 75' >/dev/null 2>&1 &
LOCK_PID=$!
sleep 70
sx test -f "$ST/pending.env" && sx test -f "$DROPIN" && pass "пока install.sh держит блокировку, откат отложен" || fail "откат прошёл при занятой блокировке"
wait "$LOCK_PID" 2>/dev/null || true
for _ in $(seq 1 24); do sx test -f "$ST/pending.env" || break; sleep 5; done
if sx test ! -f "$ST/pending.env"; then pass "таймер откатил шаг 1"; else fail "отката нет за 2 мин"; sx journalctl -u vpn-zoo-ssh-revert --no-pager -n 20; fi
sx test ! -e "$DROPIN" && sx test ! -e "$ST/orig.env" && pass "drop-in и состояние фазы убраны" || fail "после отката остались файлы фазы"
listening "$NEW" && fail "после отката порт $NEW слушается" || pass "порт $NEW не слушается"
ufw_has "$NEW" && fail "после отката ufw $NEW открыт" || pass "ufw: $NEW закрыт"
ufw_has "$OLD" && pass "ufw: $OLD открыт" || fail "ufw: нет $OLD"
[ "$(jail_ports)" = "$OLD" ] && [ "$(cfg SSH_PORTS)" = "$OLD" ] && [ "$(cfg SSH_LOGIN_PORT)" = "$OLD" ] \
    && pass "fail2ban, SSH_PORTS, SSH_LOGIN_PORT = $OLD" || fail "после отката: jail=$(jail_ports) SSH_PORTS=$(cfg SSH_PORTS)"
[ "$(cfg SSH_HARDEN)" = "0" ] && [ "$(state_of 01b-ssh)" = "disabled" ] && pass "SSH_HARDEN=0, state disabled — повтора без запроса не будет" \
    || fail "SSH_HARDEN=$(cfg SSH_HARDEN), state=$(state_of 01b-ssh)"
m="$(auth_methods "$OLD")"
if [ "$PASS_BEFORE" = "1" ]; then
    case "$m" in *password*) pass "вход по паролю снова предлагается ($m)" ;; *) fail "пароль не вернулся: $m" ;; esac
else
    info "пароль не предлагался и до шага 1 ($m)"
fi
r="$(cssh "$OLD" root 'echo ok' 2>&1 || true)"; [ "$r" = "ok" ] && pass "вход на $OLD работает" || fail "вход на $OLD: $r"
sx systemctl cat vpn-zoo-ssh-revert.timer >/dev/null 2>&1 && fail "юнит таймера остался" || pass "юнит таймера удалён"

# ------------------------------------------------------------
step "d) шаг 1 и подтверждение"
# ------------------------------------------------------------

if [ "$FULL" = "1" ]; then
    out="$(remote_install "$OLD" "SSH_HARDEN=1 SSH_REVERT_MIN=10 ZOO_SELFTEST=0" --rerun --only 01,01b,07,09,99 2>&1)" && rc=0 || rc=$?
else
    out="$(remote_install "$OLD" "SSH_HARDEN=1 SSH_REVERT_MIN=10" --phase 01b 2>&1)" && rc=0 || rc=$?
fi
NEW="$(pending_val NEW_PORT)"
[ "$rc" = "0" ] && [ -n "$NEW" ] && pass "шаг 1 снова, порт $NEW" || { fail "шаг 1 (d): rc=$rc"; tail -n 20 <<< "$out"; exit 1; }

if [ "$FULL" = "1" ]; then
    step "g) полная установка: шаг 1"
    last="$(grep -n 'ГОТОВО' <<< "$out" | tail -n 1 | cut -d: -f1)"
    instr="$(grep -n "ssh -p $NEW " <<< "$out" | tail -n 1 | cut -d: -f1)"
    [ -n "$last" ] && [ -n "$instr" ] && [ "$instr" -gt "$last" ] && pass "инструкция подтверждения — после «ГОТОВО» (последней)" || fail "инструкция не в конце вывода"
    grep -q "перенос SSH ждёт подтверждения" <<< "$out" && pass "99 предупреждает о неподтверждённом переносе" || fail "99 не предупредил"
    grep -qE "ssh -N( -p $OLD)? -L .*root@$SIP" <<< "$out" && ! grep -qE "ssh -N -p $NEW " <<< "$out" \
        && pass "99 печатает туннель на текущий порт $OLD" || fail "99: туннель не на $OLD"
    acl="$(sx cat /etc/hysteria/acl.txt 2>/dev/null || true)"
    grep -q "tcp/$NEW)" <<< "$acl" && grep -q "tcp/$OLD)" <<< "$acl" && pass "07: Hysteria ACL пускает на оба SSH-порта" || fail "07: ACL без $OLD/$NEW"
    xp="$(sx grep -o '"port": *"[0-9,]*"' /usr/local/x-ui/bin/config.json 2>/dev/null | head -n 1 || true)"
    grep -q "$NEW" <<< "$xp" && grep -q "\"$OLD\|$OLD," <<< "$xp" && pass "07: Xray finalRules пускают на оба порта ($xp)" || fail "07: Xray finalRules: $xp"
fi

out="$(remote_install "$OLD" "SSH_CONFIRM=1" --phase 01b 2>&1)" && rc=0 || rc=$?
[ "$rc" != "0" ] && grep -q "а не на $NEW" <<< "$out" && pass "подтверждение из сессии на старом порту отклонено" || { fail "подтверждение со старого порта: rc=$rc"; tail -n 5 <<< "$out"; }
out="$(sx bash -c 'cd /repo && SSH_CONFIRM=1 bash scripts/install.sh --phase 01b' 2>&1)" && rc=0 || rc=$?
[ "$rc" != "0" ] && grep -q "из SSH-сессии на порту $NEW" <<< "$out" && pass "подтверждение без SSH-сессии отклонено" || { fail "подтверждение без сессии: rc=$rc"; tail -n 5 <<< "$out"; }
# на новый порт с адреса самого сервера (ssh -J через старый порт; так же выглядит вход через
# VPN этого сервера): файрвол хостера не пройден — не доказательство
ssh_gap
out="$(cx ssh "${SSH_OPTS[@]}" -o "ProxyCommand=ssh ${SSH_OPTS[*]} -p $OLD -W %h:%p root@$SIP" -p "$NEW" root@127.0.0.1 \
    "cd /repo && SSH_CONFIRM=1 ZOO_TEST_ENV=docker bash scripts/install.sh --phase 01b" 2>&1)" && rc=0 || rc=$?
[ "$rc" != "0" ] && grep -q "хостера она не проходила" <<< "$out" && pass "подтверждение через сам сервер (127.0.0.1:$NEW) отклонено" || { fail "подтверждение через loopback: rc=$rc"; tail -n 5 <<< "$out"; }
ssh_gap
sx test -f "$ST/pending.env" && listening "$OLD" && pass "после отказов шаг 1 на месте" || fail "отказ изменил состояние"

# подтверждение из сессии на новом порту, через sudo (как у пользователя без root)
out="$(cssh "$NEW" "$TEST_USER" "cd /repo && SSH_CONFIRM=1 ZOO_TEST_ENV=docker sudo -E bash scripts/install.sh --phase 01b" 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && grep -q "SSH закрыт: только порт $NEW" <<< "$out" && pass "подтверждение из сессии на новом порту (sudo)" || { fail "подтверждение: rc=$rc"; tail -n 15 <<< "$out"; }
listening "$OLD" && fail "порт $OLD всё ещё слушается" || pass "порт $OLD не слушается"
ufw_has "$OLD" && fail "ufw: $OLD открыт" || pass "ufw: $OLD закрыт"
sx ufw status | grep -qE "^$OLD/tcp " && fail "в ufw осталось правило $OLD/tcp" || pass "в ufw нет правил $OLD/tcp"
ufw_has "$NEW" && listening "$NEW" && pass "порт $NEW: ufw limit и слушается" || fail "порт $NEW не готов"
r="$(cssh "$NEW" root 'echo ok' 2>&1 || true)"; [ "$r" = "ok" ] && pass "вход root по ключу на $NEW" || fail "вход на $NEW: $r"
ssh_gap
r="$(cx ssh "${SSH_OPTS[@]}" -p "$OLD" "root@$SIP" true 2>&1 || true)"
[ -n "$r" ] && pass "подключение к $OLD не проходит (${r##*: })" || fail "к $OLD подключились"
m="$(auth_methods "$NEW")"; [ "$m" = "publickey" ] && pass "порт $NEW: только publickey" || fail "порт $NEW: «$m»"
[ "$(jail_ports)" = "$NEW" ] && [ "$(cfg SSH_PORTS)" = "$NEW" ] && [ "$(cfg SSH_LOGIN_PORT)" = "$NEW" ] \
    && pass "fail2ban, SSH_PORTS, SSH_LOGIN_PORT = $NEW" || fail "jail=$(jail_ports) SSH_PORTS=$(cfg SSH_PORTS) LOGIN=$(cfg SSH_LOGIN_PORT)"
sx test ! -e "$ST/pending.env" && ! sx systemctl cat vpn-zoo-ssh-revert.timer >/dev/null 2>&1 && pass "таймер отката снят" || fail "таймер или pending остались"
[ "$(sx bash -c "sshd -T | awk '\$1==\"port\"' | paste -sd' ' -")" = "port $NEW" ] && pass "sshd -T: только Port $NEW" || fail "sshd -T: другие порты"

if [ "$FULL" = "1" ]; then
    step "g) полная установка: после подтверждения"
    acl="$(sx cat /etc/hysteria/acl.txt 2>/dev/null || true)"
    grep -q "tcp/$NEW)" <<< "$acl" && ! grep -q "tcp/$OLD)" <<< "$acl" && pass "07 переприменена: ACL Hysteria только на $NEW" || fail "07: ACL после подтверждения"
    xp="$(sx grep -o '"port": *"[0-9,]*"' /usr/local/x-ui/bin/config.json 2>/dev/null | head -n 1 || true)"
    [ "$(tr -dc '0-9,' <<< "${xp#*:}")" = "$NEW" ] && pass "07: Xray finalRules только $NEW" || fail "07: Xray finalRules: $xp"
    sx grep -q "ssh -N -p $NEW -L" /root/CREDENTIALS.md && sx grep -q "scp -P $NEW " /root/CREDENTIALS.md \
        && pass "CREDENTIALS.md: ssh -p $NEW и scp -P $NEW" || fail "CREDENTIALS.md без нового порта"
    wi="$(sx zoo web --info 2>&1 || true)"
    grep -q -- "-p $NEW root@$SIP" <<< "$wi" && pass "zoo web --info: -p $NEW" || { fail "zoo web --info"; info "$wi"; }
    hint="$(sx bash -c "cd /opt/vpn-zoo/zoo && python3 -c 'from zoolib import config; from zoolib.probe import _ssh_port; print(_ssh_port(config.load()))'" 2>&1 || true)"
    [ "$hint" = "$NEW" ] && pass "подсказка пробника (scp -P) берёт порт $NEW" || fail "подсказка пробника: «$hint»"
    out="$(remote_install "$NEW" "ZOO_CREDS_NO_QR=1" --phase 99 2>&1)" && rc=0 || rc=$?
    grep -qE "ssh -N -p $NEW -L .*root@$SIP" <<< "$out" && pass "99: туннель на порт $NEW" || fail "99 после подтверждения"
fi

# ------------------------------------------------------------
step "f) повторные запуски"
# ------------------------------------------------------------

sum_before="$(sx md5sum "$DROPIN" | cut -d' ' -f1)"
out="$(remote_install "$NEW" "SSH_HARDEN=1" --phase 01b 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && grep -q "SSH уже закрыт: порт $NEW" <<< "$out" && [ "$(sx md5sum "$DROPIN" | cut -d' ' -f1)" = "$sum_before" ] \
    && sx test ! -e "$ST/pending.env" && pass "повтор 01b: без изменений" || fail "повтор 01b: rc=$rc"
out="$(remote_install "$NEW" "" --phase 01 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && [ "$(cfg SSH_PORTS)" = "$NEW" ] && ! ufw_has "$OLD" && [ "$(jail_ports)" = "$NEW" ] \
    && pass "повтор 01: SSH-порт $NEW, $OLD не вернулся" || fail "повтор 01: rc=$rc SSH_PORTS=$(cfg SSH_PORTS)"

# ------------------------------------------------------------
step "e) SSH_HARDEN=0 — исходное состояние"
# ------------------------------------------------------------

out="$(remote_install "$NEW" "SSH_HARDEN=0" --phase 01b 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && grep -q "порт $NEW пока тоже открыт" <<< "$out" && grep -q "ssh -p $OLD " <<< "$out" \
    && pass "SSH_HARDEN=0 из сессии на $NEW: вход исходный, $NEW пока открыт, инструкция на $OLD" || { fail "SSH_HARDEN=0 (шаг 1): rc=$rc"; tail -n 10 <<< "$out"; }
listening "$OLD" && listening "$NEW" && ufw_has "$OLD" && ufw_has "$NEW" && pass "переходный шаг: слушаются и открыты $OLD и $NEW" || fail "переходный шаг: порты"
m="$(auth_methods "$NEW")"
[ "$PASS_BEFORE" = "0" ] || case "$m" in *password*) pass "переходный шаг: пароль снова предлагается" ;; *) fail "переходный шаг: пароль не вернулся: $m" ;; esac
out="$(remote_install "$NEW" "SSH_HARDEN=0" --phase 01b 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && grep -q "порт $NEW оставлен открытым: .*а не на $OLD" <<< "$out" && listening "$NEW" && pass "из сессии на $NEW порт $NEW не закрывается (rc=0, предупреждение)" || { fail "SSH_HARDEN=0 (шаг 2) с $NEW: rc=$rc"; tail -n 5 <<< "$out"; }
out="$(remote_install "$OLD" "SSH_HARDEN=0" --phase 01b 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && grep -q "SSH возвращён" <<< "$out" && pass "SSH_HARDEN=0 из сессии на $OLD: порт $NEW закрыт" || { fail "SSH_HARDEN=0 (шаг 2): rc=$rc"; tail -n 10 <<< "$out"; }
sx test ! -e "$DROPIN" && sx test ! -e "$ST/orig.env" && ! sx grep -rq '^#vpn-zoo# ' /etc/ssh && pass "drop-in и метки убраны" || fail "остались файлы фазы"
listening "$OLD" && ! listening "$NEW" && pass "слушается $OLD, $NEW — нет" || fail "порты после возврата"
ufw_has "$OLD" && ! ufw_has "$NEW" && pass "ufw: $OLD открыт, $NEW закрыт" || fail "ufw после возврата"
[ "$(jail_ports)" = "$OLD" ] && [ "$(cfg SSH_PORTS)" = "$OLD" ] && [ "$(cfg SSH_LOGIN_PORT)" = "$OLD" ] && pass "fail2ban и config.env = $OLD" || fail "jail/config после возврата"
m="$(auth_methods "$OLD")"
[ "$PASS_BEFORE" = "0" ] || case "$m" in *password*) pass "пароль снова предлагается" ;; *) fail "пароль не вернулся: $m" ;; esac
r="$(cssh "$OLD" root 'echo ok' 2>&1 || true)"; [ "$r" = "ok" ] && pass "вход на $OLD работает" || fail "вход на $OLD: $r"
if [ "$FULL" = "1" ]; then
    sx grep -q "ssh -N -p $NEW" /root/CREDENTIALS.md && fail "CREDENTIALS.md: остался порт $NEW" || pass "CREDENTIALS.md: снова порт $OLD"
fi
out="$(sx bash -c 'cd /repo && SSH_HARDEN=0 bash scripts/install.sh --phase 01b' 2>&1)" && rc=0 || rc=$?
[ "$rc" = "0" ] && grep -q "менять нечего" <<< "$out" && pass "повтор SSH_HARDEN=0: менять нечего" || fail "повтор SSH_HARDEN=0: rc=$rc"

echo
if [ "$FAILS" -eq 0 ]; then echo "ИТОГ: PASS"; exit 0; fi
echo "ИТОГ: FAIL ($FAILS)"
exit 1
