#!/usr/bin/env bash
# mini-server.sh — минимальный сервер для проверки пробника без фаз 04–06.
# Запускается ВНУТРИ контейнера стенда после фаз 00–03 (3x-ui с API):
#
#   docker/run-server.sh exec NAME bash /repo/docker/probe/mini-server.sh
#
# Поднимает и описывает манифестами (схема ARCHITECTURE §4, probe для owner):
#   vless-reality  VLESS+REALITY+Vision через lib/xui.sh, 443/tcp
#   ss2022         Shadowsocks-2022 через lib/xui.sh, MINI_SS_PORT/tcp
#   hysteria2      hysteria закреплённой версии (versions.env), userpass, 443/udp
#   amneziawg      amneziawg-go + awg (если есть в /usr/local/bin), MINI_AWG_PORT/udp
# Идемпотентно: повторный запуск пересоздаёт всё своё. Только для стенда.

set -euo pipefail
cd /repo
# shellcheck source=../../scripts/lib.sh
. scripts/lib.sh
config_load
# shellcheck source=../../scripts/lib/xui.sh
. scripts/lib/xui.sh
versions_load
detect_arch >/dev/null

MINI_SS_PORT="${MINI_SS_PORT:-23456}"
MINI_AWG_PORT="${MINI_AWG_PORT:-51888}"
MINI_DIR=/etc/zoo-mini
SERVER_IP="${SERVER_IP:?нет SERVER_IP в config.env}"
( umask 077; mkdir -p "$MINI_DIR" )

xui_wait_api 60 >/dev/null

# ---------- VLESS + REALITY ----------
xray="$(xui_xray_bin)"
keys="$("$xray" x25519)"
priv="$(awk -F': ' '/^PrivateKey/ {print $2}' <<< "$keys")"
pub="$(awk -F': ' '/^Password/ {print $2}' <<< "$keys")"
sid="$(gen_random_hex 8)"
uuid="$(cat /proc/sys/kernel/random/uuid)"
sni="www.microsoft.com"

for r in zoo-mini-vless zoo-mini-ss; do
    id="$(xui_inbound_find_by_remark "$r")"
    [ -z "$id" ] || xui_inbound_del "$id" >/dev/null
done
xui_client_del owner >/dev/null 2>&1 || true
xui_client_del_orphans >/dev/null 2>&1 || true

vless_id="$(xui_inbound_add "$(jq -cn --arg priv "$priv" --arg pub "$pub" --arg sid "$sid" --arg uuid "$uuid" --arg sni "$sni" '{
    remark: "zoo-mini-vless", enable: true, listen: "", port: 443, protocol: "vless", expiryTime: 0, total: 0,
    settings: {clients: [{id: $uuid, email: "owner", flow: "xtls-rprx-vision", enable: true, limitIp: 0,
                          totalGB: 0, expiryTime: 0, tgId: 0, subId: "zoominiowner", reset: 0}],
               decryption: "none", fallbacks: []},
    streamSettings: {network: "tcp", security: "reality", tcpSettings: {header: {type: "none"}},
        realitySettings: {show: false, xver: 0, target: ($sni + ":443"), serverNames: [$sni],
            privateKey: $priv, shortIds: [$sid], minClientVer: "", maxClientVer: "",
            settings: {publicKey: $pub, fingerprint: "chrome", spiderX: "/"}}},
    sniffing: {enabled: true, destOverride: ["http", "tls", "quic"]}}')")"
fw_allow 443/tcp "zoo-mini vless"
manifest_write vless-reality "$(jq -cn --arg ip "$SERVER_IP" --arg uuid "$uuid" --arg pub "$pub" --arg sid "$sid" --arg sni "$sni" '{
    id: "vless-reality", name: "VLESS + REALITY + Vision (mini)", layer: "tcp", port: 443, engine: "xray",
    service: "x-ui", enabled: true, users_backend: "none", links: [], files: [],
    probe: {kind: "xray", user: "owner", outbound: {protocol: "vless", tag: "proxy",
        settings: {vnext: [{address: $ip, port: 443, users: [{id: $uuid, encryption: "none", flow: "xtls-rprx-vision"}]}]},
        streamSettings: {network: "tcp", security: "reality",
            realitySettings: {serverName: $sni, fingerprint: "chrome", publicKey: $pub, shortId: $sid, spiderX: "/"}}}},
    notes: "стенд пробника"}')"
log_ok "vless-reality: inbound $vless_id, 443/tcp"

# ---------- Shadowsocks-2022 ----------
ss_id="$(xui_inbound_add "$(jq -cn --argjson port "$MINI_SS_PORT" --arg pw "$(head -c16 /dev/urandom | base64)" '{
    remark: "zoo-mini-ss", enable: true, listen: "", port: $port, protocol: "shadowsocks", expiryTime: 0, total: 0,
    settings: {method: "2022-blake3-aes-128-gcm", password: $pw, network: "tcp,udp", clients: []},
    streamSettings: {network: "tcp", security: "none"}, sniffing: {enabled: true, destOverride: ["http", "tls"]}}')")"
xui_client_attach owner "$ss_id" >/dev/null
fw_allow "$MINI_SS_PORT/tcp" "zoo-mini ss"
fw_allow "$MINI_SS_PORT/udp" "zoo-mini ss"
ss_ob="$(xui_inbound_get "$ss_id" | jq -c --arg host "$SERVER_IP" '
    ((.settings.clients // [])[] | select(.email == "owner") | .password) as $k
    | {tag: "proxy", protocol: "shadowsocks",
       settings: {servers: [{address: $host, port: .port, method: .settings.method, password: "\(.settings.password):\($k)"}]},
       streamSettings: {network: "tcp"}}')"
manifest_write ss2022 "$(jq -cn --argjson ob "$ss_ob" --argjson port "$MINI_SS_PORT" '{
    id: "ss2022", name: "Shadowsocks-2022 (mini)", layer: "tcp", port: $port, engine: "xray", service: "x-ui",
    enabled: true, users_backend: "none", links: [], files: [], probe: {kind: "xray", user: "owner", outbound: $ob},
    notes: "стенд пробника"}')"
log_ok "ss2022: inbound $ss_id, $MINI_SS_PORT/tcp"

# ---------- Hysteria2 (standalone) ----------
download_verified "$HY2_URL_BASE/hysteria-linux-$ZOO_ARCH" "$(version_for HY2_SHA256 "$ZOO_ARCH")" /usr/local/bin/hysteria
chmod 755 /usr/local/bin/hysteria
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 -subj "/CN=bing.com" \
    -keyout "$MINI_DIR/hy.key" -out "$MINI_DIR/hy.crt" >/dev/null 2>&1
pin="$(openssl x509 -in "$MINI_DIR/hy.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
hypass="$(gen_random_alnum 24)"
cat > "$MINI_DIR/hy.yaml" <<EOF
listen: :443
tls:
  cert: $MINI_DIR/hy.crt
  key: $MINI_DIR/hy.key
auth:
  type: userpass
  userpass:
    owner: $hypass
EOF
chmod 600 "$MINI_DIR"/hy.*
systemctl stop zoo-mini-hy 2>/dev/null || true
systemctl reset-failed zoo-mini-hy 2>/dev/null || true
systemd-run --unit zoo-mini-hy -E HYSTERIA_DISABLE_UPDATE_CHECK=1 /usr/local/bin/hysteria server -c "$MINI_DIR/hy.yaml" >/dev/null
fw_allow 443/udp "zoo-mini hysteria"
wait_port 443 udp 15 || die "hysteria не слушает 443/udp"
manifest_write hysteria2 "$(jq -cn --arg srv "$SERVER_IP:443" --arg auth "owner:$hypass" --arg pin "$pin" --arg v "$HY2_VERSION" '{
    id: "hysteria2", name: "Hysteria2 (mini)", layer: "udp", port: 443, engine: "hysteria", service: "zoo-mini-hy",
    enabled: true, users_backend: "none", links: [], files: [],
    probe: {kind: "hysteria", user: "owner", version: $v,
            client: {server: $srv, auth: $auth, tls: {sni: "bing.com", insecure: true, pinSHA256: $pin}}},
    notes: "стенд пробника"}')"
log_ok "hysteria2: 443/udp"

# ---------- AmneziaWG (amneziawg-go) ----------
if command -v amneziawg-go >/dev/null && command -v awg >/dev/null; then
    systemctl stop zoo-mini-awg 2>/dev/null || true
    systemctl reset-failed zoo-mini-awg 2>/dev/null || true
    ip link del awgt0 2>/dev/null || true
    spriv="$(awg genkey)"; spub="$(awg pubkey <<< "$spriv")"
    cpriv="$(awg genkey)"; cpub="$(awg pubkey <<< "$cpriv")"
    psk="$(awg genpsk)"
    obf=$'Jc = 4\nJmin = 40\nJmax = 70\nS1 = 52\nS2 = 97\nH1 = 1381287421\nH2 = 1904416243\nH3 = 2096330818\nH4 = 726183920'
    printf '[Interface]\nPrivateKey = %s\nListenPort = %s\n%s\n\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = 10.77.0.2/32\n' \
        "$spriv" "$MINI_AWG_PORT" "$obf" "$cpub" "$psk" > "$MINI_DIR/awgt0.conf"
    chmod 600 "$MINI_DIR/awgt0.conf"
    systemd-run --unit zoo-mini-awg -E WG_PROCESS_FOREGROUND=1 /usr/local/bin/amneziawg-go -f awgt0 >/dev/null
    for _ in $(seq 1 50); do [ -S /var/run/amneziawg/awgt0.sock ] && break; sleep 0.1; done
    awg setconf awgt0 "$MINI_DIR/awgt0.conf"
    ip addr add 10.77.0.1/24 dev awgt0
    ip link set awgt0 mtu 1280 up
    sysctl -qw net.ipv4.ip_forward=1
    egress="$(ip route show default | awk '{print $5; exit}')"
    iptables -t nat -C POSTROUTING -s 10.77.0.0/24 -o "$egress" -j MASQUERADE 2>/dev/null \
        || iptables -t nat -A POSTROUTING -s 10.77.0.0/24 -o "$egress" -j MASQUERADE
    iptables -C FORWARD -i awgt0 -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -i awgt0 -j ACCEPT
    iptables -C FORWARD -o awgt0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \
        || iptables -I FORWARD 1 -o awgt0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    fw_allow "$MINI_AWG_PORT/udp" "zoo-mini awg"
    conf="$(printf '[Interface]\nPrivateKey = %s\nAddress = 10.77.0.2/32\nDNS = 1.1.1.1\nMTU = 1280\n%s\n\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nEndpoint = %s:%s\nAllowedIPs = 0.0.0.0/0, ::/0\nPersistentKeepalive = 25\n' \
        "$cpriv" "$obf" "$spub" "$psk" "$SERVER_IP" "$MINI_AWG_PORT")"
    manifest_write amneziawg "$(jq -cn --arg conf "$conf" --arg ep "$SERVER_IP:$MINI_AWG_PORT" --argjson port "$MINI_AWG_PORT" '{
        id: "amneziawg", name: "AmneziaWG (mini, amneziawg-go)", layer: "udp", port: $port, engine: "amneziawg-go",
        service: "zoo-mini-awg", enabled: true, users_backend: "none", links: [], files: [],
        probe: {kind: "awg", user: "owner", conf: $conf, endpoint: $ep, address: "10.77.0.2", mtu: 1280},
        notes: "стенд пробника"}')"
    log_ok "amneziawg: $MINI_AWG_PORT/udp"
else
    log_warn "amneziawg: нет amneziawg-go/awg в PATH — пропуск (скопируйте из образа zoo-probe)"
fi

# клиент, привязанный к SS-inbound, Xray видит только после перегенерации конфига
xui_xray_restart >/dev/null
ls -1 "$MANIFEST_DIR"
