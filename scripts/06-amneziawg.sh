#!/usr/bin/env bash
# 06-amneziawg.sh — AmneziaWG (UDP/$AWG_PORT): движок kernel (DKMS из ppa:amnezia/ppa)
# или userspace (amneziawg-go и amneziawg-tools из закреплённых тегов), уникальные
# параметры обфускации на каждую установку (D4), peer на пользователя, NAT, манифест.
#
#   AWG_ENGINE=auto|kernel|userspace   auto → AWG_ENGINE_HINT от 02-kernel
#   AWG_PROFILE=v2|v3                  v2 — клиенты AWG 2.0; v3 — HeaderProtectionKey (клиенты 3.1)
#   AWG_RT=1                           (только v3) RandomTrailers, S1=S2=S3=S4
#   AWG_PORT, AWG_NETWORK, AWG_MTU, AWG_DNS, AWG_KEEPALIVE — см. config.env

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib/proto-amneziawg.sh"
config_load
versions_load
require_ubuntu
detect_arch >/dev/null

PHASE="${ZOO_PHASE:-06-amneziawg}"
CACHE="/var/cache/vpn-zoo/awg"
SYSCTL_FILE=/etc/sysctl.d/60-vpn-zoo-awg.conf
US_UNIT=/etc/systemd/system/awg-quick@.service
US_MARK="# vpn-zoo: userspace amneziawg-go"
STAMP_DIR=/var/lib/vpn-setup

if [ "${ENABLE_AWG:-1}" != "1" ]; then
    proto_amneziawg_disable
    log_ok "ENABLE_AWG=$ENABLE_AWG: AmneziaWG остановлен, порт закрыт"
    exit 0
fi

# ------------------------------------------------------------
# 0. Чужая установка
# ------------------------------------------------------------

guard_foreign_install amneziawg "$AWG_CONF" /usr/local/bin/amneziawg-go
# Сразу: упавшая на середине фаза не должна при повторе считаться чужой установкой
mark_owned amneziawg

# ------------------------------------------------------------
# 1. Параметры (config.env)
# ------------------------------------------------------------

config_default AWG_PROFILE v2
config_default AWG_RT 0
config_default AWG_NETWORK 10.66.66.0/24
config_default AWG_MTU 1280
config_default AWG_DNS "1.1.1.1, 1.0.0.1"
config_default AWG_KEEPALIVE 25
case "$AWG_PROFILE" in v2|v3) ;; *) die "AWG_PROFILE=$AWG_PROFILE: допустимо v2 или v3" ;; esac
[ "$AWG_PROFILE" = "v3" ] || [ "$AWG_RT" = "0" ] || { log_warn "AWG_RT=1 действует только с AWG_PROFILE=v3 — игнорирую"; config_set AWG_RT 0; }

if ! config_has AWG_PORT; then
    port="$(rand_port)"
    config_set AWG_PORT "$port"
fi
[[ "$AWG_PORT" =~ ^[0-9]+$ ]] && [ "$AWG_PORT" -ge 1 ] && [ "$AWG_PORT" -le 65535 ] || die "AWG_PORT=$AWG_PORT — не порт"
is_banned_port "$AWG_PORT" && die "AWG_PORT=$AWG_PORT в списке запрещённых ($BANNED_PORTS)"
# Порт слушает кто-то кроме нашего AWG (Hysteria на 443/udp, TUIC...) — до любых изменений:
# иначе интерфейс перезапустится без сокета и AWG ляжет целиком
awg_busy="$(ss -Hulnp "sport = :$AWG_PORT" 2>/dev/null || true)"
if [ -n "$awg_busy" ] && [[ "$awg_busy" != *'"amneziawg-go"'* ]] \
    && [ "$(awg show "$AWG_IFACE" listen-port 2>/dev/null || true)" != "$AWG_PORT" ]; then
    die "AWG_PORT=$AWG_PORT/udp уже занят: $(head -1 <<< "$awg_busy") — задайте другой AWG_PORT"
fi
_awg_net >/dev/null || die "неверный AWG_NETWORK=$AWG_NETWORK"

# Параметры обфускации: генерируются один раз; заданные руками (AWG_S1=... в окружении)
# не трогаем. При смене профиля RandomTrailers S1..S4 пересоздаются (нужны равные)
sig="$AWG_PROFILE"; [ "$AWG_RT" = "1" ] && sig="$sig-rt"
old_sig="$(config_get AWG_PARAMS_SIG)"
regen_s=0
case "$old_sig:$sig" in *-rt:*-rt|:*) ;; *-rt:*|*:*-rt) regen_s=1 ;; esac
gen="$(awg_params_generate "$AWG_PROFILE" "$AWG_RT")"
while read -r k v; do
    [ -n "$k" ] || continue
    if [ "$regen_s" = "1" ] && [[ "$k" =~ ^AWG_S[1-4]$ ]]; then
        config_set "$k" "$v"
    elif ! config_has "$k" || [ -z "$(config_get "$k")" ]; then
        config_set "$k" "$v"
    else
        config_default "$k" "$v"
    fi
done <<< "$gen"
[ "$regen_s" = "1" ] && log_warn "профиль сменился ($old_sig → $sig): S1..S4 пересозданы, клиентам нужны новые конфиги"
awg_params_check || die "параметры AmneziaWG в $CONFIG_FILE нарушают ограничения (см. выше). Исправьте AWG_* или удалите их — пересоздадутся"
config_set AWG_PARAMS_SIG "$sig"
log_ok "параметры AWG ($sig): Jc=$AWG_JC Jmin=$AWG_JMIN Jmax=$AWG_JMAX S=$AWG_S1/$AWG_S2/$AWG_S3/$AWG_S4 H=$AWG_H1/$AWG_H2/$AWG_H3/$AWG_H4"

# ------------------------------------------------------------
# 2. Движок
# ------------------------------------------------------------

engine="${AWG_ENGINE:-auto}"
case "$engine" in
    auto) engine="${AWG_ENGINE_HINT:-userspace}"; reason="${AWG_ENGINE_REASON:-подсказка 02-kernel}" ;;
    kernel|userspace) reason="задано AWG_ENGINE=$engine" ;;
    *) die "AWG_ENGINE=$engine: допустимо auto, kernel, userspace" ;;
esac
if [ "$engine" = "kernel" ] && kernel_is_foreign; then
    [ "${AWG_ENGINE:-auto}" = "kernel" ] && die "AWG_ENGINE=kernel невозможен в контейнере/стенде"
    engine=userspace; reason="контейнер/стенд"
fi
log_info "движок AmneziaWG: $engine ($reason)"
if [ "$engine" = "kernel" ] && [ "$AWG_PROFILE" = "v3" ]; then
    log_warn "v3 (HeaderProtectionKey) на kmod: связка kmod-сервер ↔ go-клиенты не проверена (kmod#222). При сбоях — AWG_PROFILE=v2 или AWG_ENGINE=userspace"
fi

# Сборка в каталоге кэша; GOMAXPROCS=1 и -p 1 держат пик памяти в пределах 1 ГБ VPS со swap
build_amneziawg_go() {
    local out="$1" tmp gotgz srctgz goarch="$ZOO_ARCH"
    tmp="$(mktemp -d "$CACHE/build.XXXXXX")"
    gotgz="$CACHE/go$GO_VERSION.linux-$goarch.tar.gz"
    srctgz="$CACHE/amneziawg-go-$AWG_GO_REF.tar.gz"
    download_verified "$GO_URL_BASE/go$GO_VERSION.linux-$goarch.tar.gz" "$(version_for GO_SHA256 "$goarch")" "$gotgz"
    download_verified "$AWG_GO_SRC_URL" "$AWG_GO_SRC_SHA256" "$srctgz"
    tar -C "$tmp" -xzf "$gotgz"
    mkdir -p "$tmp/src"
    tar -C "$tmp/src" --strip-components=1 -xzf "$srctgz"
    log_info "сборка amneziawg-go $AWG_GO_REF (go $GO_VERSION), 1–3 минуты"
    if ! ( cd "$tmp/src" && env PATH="$tmp/go/bin:$PATH" HOME="$tmp" GOPATH="$tmp/gopath" \
            GOMODCACHE="$tmp/gomod" GOCACHE="$tmp/gocache" GOTOOLCHAIN=local GOFLAGS=-mod=readonly \
            GOPROXY="https://proxy.golang.org,direct" CGO_ENABLED=0 GOMAXPROCS=1 \
            go build -p 1 -trimpath -ldflags '-s -w' -o "$tmp/amneziawg-go" . ); then
        rm -rf "$tmp"
        die "сборка amneziawg-go не удалась (нужен доступ к proxy.golang.org для зависимостей)"
    fi
    install -m 0755 "$tmp/amneziawg-go" "$out"
    rm -rf "$tmp" "$gotgz"
}

build_awg_tools_src() {
    local outdir="$1" tmp srctgz
    log_info "сборка amneziawg-tools $AWG_TOOLS_REF из исходников"
    apt_install gcc make libc6-dev >/dev/null
    tmp="$(mktemp -d "$CACHE/build.XXXXXX")"
    srctgz="$CACHE/amneziawg-tools-$AWG_TOOLS_REF.tar.gz"
    download_verified "$AWG_TOOLS_SRC_URL" "$AWG_TOOLS_SRC_SHA256" "$srctgz"
    tar -C "$tmp" --strip-components=1 -xzf "$srctgz"
    make -s -C "$tmp/src" wg >/dev/null || { rm -rf "$tmp"; die "сборка amneziawg-tools не удалась"; }
    install -m 0755 "$tmp/src/wg" "$outdir/awg"
    install -m 0755 "$tmp/src/wg-quick/linux.bash" "$outdir/awg-quick"
    rm -rf "$tmp" "$srctgz"
}

# amneziawg-go в /usr/local/bin (сборка кэшируется в $CACHE)
install_go_binary() {
    local go_bin
    ( umask 022; mkdir -p "$CACHE" )
    go_bin="$CACHE/amneziawg-go-$AWG_GO_REF-$ZOO_ARCH"
    [ -x "$go_bin" ] || build_amneziawg_go "$go_bin"
    install -m 0755 "$go_bin" /usr/local/bin/amneziawg-go
}

install_userspace() {
    local tools_dir sum tmp
    install_go_binary
    tools_dir="$CACHE/amneziawg-tools-$AWG_TOOLS_REF-$ZOO_ARCH"
    if [ ! -x "$tools_dir/awg" ] || [ ! -x "$tools_dir/awg-quick" ]; then
        mkdir -p "$tools_dir"
        sum="AWG_TOOLS_BIN_SHA256_$ZOO_ARCH"
        if [ -n "${!sum:-}" ] && [ "${ZOO_AWG_TOOLS_SRC:-0}" != "1" ]; then
            tmp="$(mktemp -d "$CACHE/build.XXXXXX")"
            download_verified "$AWG_TOOLS_BIN_URL" "${!sum}" "$tmp/tools.zip"
            command -v unzip >/dev/null || apt_install unzip >/dev/null
            unzip -q -o "$tmp/tools.zip" -d "$tmp"
            install -m 0755 "$tmp"/*/awg "$tools_dir/awg"
            install -m 0755 "$tmp"/*/awg-quick "$tools_dir/awg-quick"
            rm -rf "$tmp"
        else
            build_awg_tools_src "$tools_dir"
        fi
    fi
    install -m 0755 "$tools_dir/awg" /usr/local/bin/awg
    install -m 0755 "$tools_dir/awg-quick" /usr/local/bin/awg-quick
    ( umask 077; mkdir -p "$AWG_CONF_DIR" )
    # awg-quick из PPA/upstream вызывает /usr/bin/awg-quick; наш юнит — /usr/local/bin и amneziawg-go
    cat > "$US_UNIT.tmp" <<EOF
$US_MARK (06-amneziawg.sh), не править
[Unit]
Description=AmneziaWG (amneziawg-go) via awg-quick for %I
After=network-online.target nss-lookup.target
Wants=network-online.target nss-lookup.target
PartOf=awg-quick.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/local/bin/amneziawg-go
Environment=WG_ENDPOINT_RESOLUTION_RETRIES=infinity
ExecStart=/usr/local/bin/awg-quick up %i
ExecStop=/usr/local/bin/awg-quick down %i
ExecReload=/bin/bash -c 'exec /usr/local/bin/awg syncconf %i <(exec /usr/local/bin/awg-quick strip %i)'

[Install]
WantedBy=multi-user.target
EOF
    mv -f "$US_UNIT.tmp" "$US_UNIT"
    systemctl daemon-reload
}

# DKMS-модуль из PPA. Возвращает 1 (с диагностикой), если модуль не собрался/не грузится
install_kernel() {
    local kernel ver log
    kernel="$(uname -r)"
    wait_for_apt
    if ! grep -rqs 'amnezia/ppa' /etc/apt/sources.list /etc/apt/sources.list.d/; then
        apt_install software-properties-common >/dev/null
        log_info "добавляю ppa:amnezia/ppa"
        add-apt-repository -y ppa:amnezia/ppa >/dev/null || { log_err "add-apt-repository ppa:amnezia/ppa не удался"; return 1; }
    fi
    apt_update
    if ! apt_install "linux-headers-$kernel" amneziawg-dkms amneziawg-tools; then
        log_err "apt не поставил amneziawg-dkms/amneziawg-tools"
        return 1
    fi
    if ! dkms status -m amneziawg -k "$kernel" 2>/dev/null | grep -q 'installed'; then
        log_err "DKMS-модуль amneziawg не собрался под $kernel"
        log="$(find /var/lib/dkms/amneziawg -name make.log 2>/dev/null | head -1)"
        [ -z "$log" ] || { log_err "хвост $log:"; tail -n 20 "$log" >&2; }
        return 1
    fi
    # другие установленные ядра (загрузятся после reboot): без модуля сработает запасной amneziawg-go
    local k
    for k in /lib/modules/*/build; do
        [ -e "$k" ] || continue
        k="$(basename "$(dirname "$k")")"
        [ "$k" = "$kernel" ] && continue
        dkms status -m amneziawg -k "$k" 2>/dev/null | grep -q 'installed' \
            || log_warn "DKMS amneziawg не собран под ядро $k — после загрузки в него AWG пойдёт через amneziawg-go"
    done
    modprobe amneziawg || { log_err "modprobe amneziawg не удался"; return 1; }
    echo amneziawg > /etc/modules-load.d/amneziawg.conf
    ver="$(cat /sys/module/amneziawg/version 2>/dev/null || echo '?')"
    log_ok "модуль amneziawg загружен, версия $ver ($(dpkg-query -W -f='${Version}' amneziawg-dkms 2>/dev/null || true))"
    [[ "$ver" =~ ^[3-9]\. ]] || log_warn "версия модуля $ver < 3 — параметры 2.0 работают, 3.x нет"
    # userspace-юнит от прошлого движка перекрыл бы юнит пакета
    if [ -f "$US_UNIT" ] && head -1 "$US_UNIT" | grep -qF "$US_MARK"; then
        rm -f "$US_UNIT" /usr/local/bin/awg /usr/local/bin/awg-quick
        systemctl daemon-reload
    fi
    [ -x /usr/bin/awg-quick ] || { log_err "нет /usr/bin/awg-quick из amneziawg-tools"; return 1; }
}

if [ "$engine" = "kernel" ]; then
    if ! install_kernel; then
        [ "${AWG_ENGINE:-auto}" = "kernel" ] && die "AWG_ENGINE=kernel: модуль не готов (см. выше). Варианты: AWG_ENGINE=userspace или ядро 6.8"
        log_warn "kernel-движок не готов — перехожу на userspace (amneziawg-go)"
        engine=userspace
        config_set AWG_ENGINE_REASON "DKMS/modprobe не удался на $(uname -r) — userspace"
    fi
fi
[ "$engine" = "userspace" ] && install_userspace
# Запасной движок для kernel: если после обновления ядра DKMS не соберётся, модуля не будет,
# и awg-quick сам поднимет интерфейс через amneziawg-go (add_if в linux.bash)
if [ "$engine" = "kernel" ] && ! ( install_go_binary ); then
    log_warn "amneziawg-go не собрался — запасного userspace-движка не будет"
fi
mkdir -p "/etc/systemd/system/$AWG_SERVICE.service.d"
cat > "/etc/systemd/system/$AWG_SERVICE.service.d/10-vpn-zoo.conf" <<'EOF'
# vpn-zoo (06-amneziawg): без модуля ядра awg-quick поднимает интерфейс через amneziawg-go
[Service]
Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/local/bin/amneziawg-go
EOF
hash -r
if ! command -v awg >/dev/null || ! command -v awg-quick >/dev/null; then die "после установки нет awg/awg-quick"; fi
prev_engine="$(config_get AWG_ENGINE_ACTIVE)"
config_set AWG_ENGINE_ACTIVE "$engine"

# ------------------------------------------------------------
# 3. Ключ сервера (v1 хранил его в config.env — сохраняем)
# ------------------------------------------------------------

if [ -z "${AWG_SERVER_KEY:-}" ]; then
    key="$(awg genkey)"
    config_set AWG_SERVER_KEY "$key"
fi
pub="$(printf '%s\n' "$AWG_SERVER_KEY" | awg pubkey)" || die "AWG_SERVER_KEY в config.env испорчен"
[ "$(config_get AWG_SERVER_PUB)" = "$pub" ] || config_set AWG_SERVER_PUB "$pub"

# ------------------------------------------------------------
# 4. Forwarding, NAT, firewall
# ------------------------------------------------------------

cat > "$SYSCTL_FILE" <<'EOF'
# vpn-zoo (06-amneziawg): маршрутизация IPv4 для NAT клиентов AmneziaWG
net.ipv4.ip_forward = 1
EOF
sysctl -q -w net.ipv4.ip_forward=1 >/dev/null || true
[ "$(sysctl -n net.ipv4.ip_forward)" = "1" ] || die "net.ipv4.ip_forward не включился — NAT для AWG не заработает"

mkdir -p "$(dirname "$AWG_NAT_HELPER")"
cat > "$AWG_NAT_HELPER.tmp" <<'EOF'
#!/bin/bash
# vpn-zoo: forward/NAT/MSS для AmneziaWG — PostUp/PostDown awg-quick.
#   awg-nat.sh up|down IFACE NETWORK
# Правила помечены комментарием vpn-zoo-awg:IFACE; up сначала снимает старые (идемпотентно).
set -u
act="$1" ifc="$2" net="$3"
tag="vpn-zoo-awg:$ifc"
purge() {
    local t r
    for t in filter nat mangle; do
        # iptables-nft печатает комментарий в кавычках, legacy — без
        iptables -t "$t" -S 2>/dev/null | grep -E -- "--comment \"?$tag\"?( |\$)" \
        | sed -e 's/^-A /-D /' -e 's/"//g' \
        | while read -r r; do
            # shellcheck disable=SC2086 # правило из iptables -S, без пробелов в аргументах
            iptables -t "$t" $r 2>/dev/null || true
        done
    done
}
purge
[ "$act" = "up" ] || exit 0
c=(-m comment --comment "$tag")
set -e
# клиенты не видят друг друга и частные сети хоста (как geoip:private в Xray)
iptables -I FORWARD 1 -i "$ifc" "${c[@]}" -j ACCEPT
iptables -I FORWARD 1 -i "$ifc" -d 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,169.254.0.0/16 "${c[@]}" -j REJECT
iptables -I FORWARD 1 -i "$ifc" -o "$ifc" "${c[@]}" -j DROP
iptables -I FORWARD 1 -o "$ifc" -m conntrack --ctstate RELATED,ESTABLISHED "${c[@]}" -j ACCEPT
iptables -t nat -A POSTROUTING -s "$net" ! -o "$ifc" "${c[@]}" -j MASQUERADE
iptables -t mangle -A FORWARD -o "$ifc" -p tcp --tcp-flags SYN,RST SYN "${c[@]}" -j TCPMSS --clamp-mss-to-pmtu
iptables -t mangle -A FORWARD -i "$ifc" -p tcp --tcp-flags SYN,RST SYN "${c[@]}" -j TCPMSS --clamp-mss-to-pmtu
EOF
chmod 0755 "$AWG_NAT_HELPER.tmp"
mv -f "$AWG_NAT_HELPER.tmp" "$AWG_NAT_HELPER"

# Порт сменился — закрываем старый
if [ -f "$PORTS_FILE" ]; then
    while IFS=$'\t' read -r spec _ owner _; do
        [ "$owner" = "$PHASE" ] && [ "$spec" != "$AWG_PORT/udp" ] && { log_info "закрываю старый порт AWG $spec"; fw_revoke "$spec"; }
    done < <(cat "$PORTS_FILE")
fi
fw_allow "$AWG_PORT/udp" "AmneziaWG"

# ------------------------------------------------------------
# 5. Пользователь owner (v1: ключи клиента из config.env переносятся)
# ------------------------------------------------------------

if ! _awg_has_user owner && [ -n "${AWG_CLIENT_KEY:-}" ] && [ -n "${AWG_CLIENT_PSK:-}" ]; then
    log_info "перенос клиента v1 в пользователя owner"
    tip="$(_awg_server_tip)"
    printf '%s\n' "$AWG_CLIENT_KEY" | _awg_ufile owner amneziawg.key
    printf '%s\n' "$AWG_CLIENT_KEY" | awg pubkey | _awg_ufile owner amneziawg.pub
    printf '%s\n' "$AWG_CLIENT_PSK" | _awg_ufile owner amneziawg.psk
    _awg_int2ip $(( $(_awg_ip2int "$tip") + 1 )) | _awg_ufile owner amneziawg.ip
fi
PROTO_AWG_NO_REFRESH=1 proto_amneziawg_user_add owner >/dev/null || die "не удалось создать пользователя owner"
_awg_render_server || die "не удалось записать $AWG_CONF"

# ------------------------------------------------------------
# 6. Запуск: рестарт, если менялся [Interface] или движок; иначе syncconf
# ------------------------------------------------------------

iface_hash="$( { _awg_server_iface; echo "engine=$engine"; } | sha256sum | awk '{print $1}')"
hash_file="$STAMP_DIR/awg-iface.sha256"
mkdir -p "$STAMP_DIR"
systemctl daemon-reload
systemctl enable "$AWG_SERVICE" >/dev/null 2>&1 || die "не удалось включить $AWG_SERVICE"
if systemctl is-active --quiet "$AWG_SERVICE" && _awg_iface_up \
   && [ "$(cat "$hash_file" 2>/dev/null)" = "$iface_hash" ] && [ "$prev_engine" = "$engine" ]; then
    log_info "интерфейс не менялся — применяю peer'ов через syncconf"
    _awg_apply || die "awg syncconf не применился"
else
    # v1 мог оставить интерфейс, поднятый awg-quick вне нашего юнита
    if ! systemctl is-active --quiet "$AWG_SERVICE" && _awg_iface_up; then
        awg-quick down "$AWG_IFACE" >/dev/null 2>&1 || ip link del "$AWG_IFACE" 2>/dev/null || true
    fi
    log_info "перезапуск $AWG_SERVICE"
    if ! systemctl restart "$AWG_SERVICE"; then
        journalctl -u "$AWG_SERVICE" -n 30 --no-pager >&2 || true
        die "$AWG_SERVICE не запустился (журнал выше)"
    fi
fi
printf '%s\n' "$iface_hash" > "$hash_file"

# ------------------------------------------------------------
# 7. Самопроверка
# ------------------------------------------------------------

systemctl is-active --quiet "$AWG_SERVICE" || die "$AWG_SERVICE не активен"
_awg_iface_up || die "интерфейс $AWG_IFACE не поднят"
wait_port "$AWG_PORT" udp 10 || die "AmneziaWG не слушает $AWG_PORT/udp"
dump1="$(awg show "$AWG_IFACE" dump | head -1)"
IFS=$'\t' read -r _ _ d_port d_jc _ _ d_s1 d_s2 d_s3 d_s4 d_h1 d_h2 d_h3 d_h4 _ <<< "$dump1"
[ "$d_port/$d_jc/$d_s1/$d_s2/$d_s3/$d_s4/$d_h1/$d_h2/$d_h3/$d_h4" = \
  "$AWG_PORT/$AWG_JC/$AWG_S1/$AWG_S2/$AWG_S3/$AWG_S4/$AWG_H1/$AWG_H2/$AWG_H3/$AWG_H4" ] \
    || die "параметры на интерфейсе не совпадают с config.env (порт/Jc/S/H): $d_port/$d_jc/$d_s1/$d_s2/$d_s3/$d_s4/$d_h1/$d_h2/$d_h3/$d_h4"
# поля dump: 20 — HeaderProtectionKey, 27 — RandomTrailers
want_rt=off; [ "$AWG_PROFILE" = "v3" ] && [ "$AWG_RT" = "1" ] && want_rt=on
d_rt="$(cut -f27 <<< "$dump1")"
[ "$d_rt" = "$want_rt" ] || die "RandomTrailers на интерфейсе: $d_rt, ожидалось $want_rt"
if [ "$AWG_PROFILE" = "v3" ] && [ "$(cut -f20 <<< "$dump1")" != "$AWG_HPK" ]; then
    die "HeaderProtectionKey на интерфейсе не совпадает с AWG_HPK"
fi
iptables -t nat -S POSTROUTING 2>/dev/null | grep -qF "vpn-zoo-awg:$AWG_IFACE" || die "нет NAT-правила AmneziaWG (PostUp не отработал?)"
peers="$(awg show "$AWG_IFACE" peers | wc -l)"
log_ok "AmneziaWG: $AWG_SERVICE активен ($engine), $AWG_PORT/udp, peer'ов: $peers"

proto_amneziawg_manifest_refresh || die "не удалось записать манифест amneziawg"
log_ok "манифест: $(manifest_path amneziawg)"
log_ok "конфиг owner: $(_awg_udir owner)/amneziawg.conf"
