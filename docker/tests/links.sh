#!/usr/bin/env bash
# docker/tests/links.sh — импортируются ли выданные ссылки: каждая ссылка USER (owner) из манифестов
# (то, что пользователь вставит в приложение) разбирается так, как это делают клиенты
# (v2rayN/Happ для vless:// и ss://, sing-box-клиенты для tuic://, официальный клиент
# Hysteria берёт hysteria2:// как есть), и через построенный из неё клиент идёт трафик.
# Probe манифеста здесь не используется: тест ловит расхождения «probe работает, ссылка нет».
#
#   docker/tests/links.sh SERVER [USER]   (SERVER — контейнер стенда после установки, USER — owner)
#
# Клиентский контейнер в zoo-net, прямой выход закрыт iptables (только сервер).
# Для vpn:// (AmneziaVPN) проверяется только структура: last_config.config = .conf пользователя.
# LINKS_FILE=файл («id<TAB>uri» по строке) — ссылки не из манифестов, а из файла (сохранённые
# заранее); EXPECT_FAIL=1 — ожидается, что НИ ОДНА не работает (удалённый/выключенный пользователь).
# Код выхода 0 — всё PASS.

# ok/bad не падают; jq/python в одинарных кавычках намеренно; cleanup — из trap
# shellcheck disable=SC2015,SC2016,SC2329
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SRV="${1:-}"
[ -n "$SRV" ] || { echo "использование: $0 SERVER" >&2; exit 2; }
case "$SRV" in zoo-*) ;; *) SRV="zoo-$SRV" ;; esac
LU="${2:-owner}"
CLI="${SRV}-lnk"
NET="zoo-net"
SMALL_URL="https://www.gstatic.com/generate_204"
# ≥1 МБ; запасные зеркала — на случай 429 от speed.cloudflare.com при частых прогонах
BIG_URLS=("https://speed.cloudflare.com/__down?bytes=1500000" "https://proof.ovh.net/files/1Mb.dat"
          "http://speedtest.tele2.net/1MB.zip")
BIG_MIN=1000000
IP_URL="https://www.cloudflare.com/cdn-cgi/trace"

# shellcheck source=../../scripts/versions.env
. "$(dirname "${BASH_SOURCE[0]}")/../../scripts/versions.env"

fail=0
ok()   { echo "[PASS] $*"; }
bad()  { echo "[FAIL] $*"; fail=1; }
info() { echo "[i] $*"; }

sx() { docker exec -i "$SRV" "$@"; }
cx() { docker exec -i "$CLI" "$@"; }
cleanup() { docker rm -f "$CLI" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker container inspect "$SRV" >/dev/null 2>&1 || { echo "нет контейнера $SRV" >&2; exit 2; }
SRV_IP="$(docker container inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$SRV")"
EGRESS="$(sx curl -4 -sS --max-time 15 "$IP_URL" | sed -n 's/^ip=//p' || true)"
info "сервер $SRV ($SRV_IP), выход сервера ${EGRESS:-?}"

# Ссылки пользователя из включённых манифестов: «id<TAB>uri»
if [ -n "${LINKS_FILE:-}" ]; then
    LINKS="$(cat "$LINKS_FILE")"
else
    LINKS="$(sx env LU="$LU" bash -c 'for f in /etc/vpn-setup/protocols.d/*.json; do
        jq -r --arg u "$LU" "select(.enabled != false) | .id as \$i | .links[] | select(.user == \$u) | \"\(\$i)\t\(.uri)\"" "$f"; done')"
fi
[ -n "$LINKS" ] || { bad "в манифестах нет ссылок $LU"; exit 1; }
info "ссылки $LU: $(cut -f1 <<< "$LINKS" | tr '\n' ' ')"

# Разбор ссылки → конфиг клиента (stdout: «kind<TAB>json»). Логика клиентов, не модулей
# инсталлера: query через parse_qs, userinfo с %-декодированием, ss:// и в base64-виде
PARSER='
import base64, json, sys, urllib.parse as up
uri, port = sys.argv[1], int(sys.argv[2])
u = up.urlsplit(uri)
q = {k: v[0] for k, v in up.parse_qs(u.query, keep_blank_values=True).items()}
host = u.hostname
socks = {"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": True}}
def xray(ob):
    print("xray\t" + json.dumps({"log": {"loglevel": "warning"}, "inbounds": [socks], "outbounds": [ob]}))
if u.scheme == "vless":
    net = q.get("type", "tcp")
    net = "tcp" if net == "raw" else net
    st = {"network": net, "security": q.get("security", "none")}
    if st["security"] == "reality":
        st["realitySettings"] = {"serverName": q.get("sni", ""), "fingerprint": q.get("fp") or "chrome",
                                 "publicKey": q.get("pbk", ""), "shortId": q.get("sid", ""),
                                 "spiderX": q.get("spx", "")}
    if net == "xhttp":
        xs = {"path": q.get("path", "/"), "host": q.get("host", ""), "mode": q.get("mode") or "auto"}
        if q.get("extra"):
            xs["extra"] = json.loads(q["extra"])
        st["xhttpSettings"] = xs
    xray({"tag": "proxy", "protocol": "vless", "streamSettings": st,
          "settings": {"vnext": [{"address": host, "port": u.port, "users": [
              {"id": up.unquote(u.username), "encryption": q.get("encryption", "none"),
               "flow": q.get("flow", "")}]}]}})
elif u.scheme == "ss":
    ui = up.unquote(uri[5:].split("@", 1)[0])
    if ":" not in ui:
        ui = base64.urlsafe_b64decode(ui + "=" * (-len(ui) % 4)).decode()
    method, pw = ui.split(":", 1)
    xray({"tag": "proxy", "protocol": "shadowsocks", "settings": {"servers": [
        {"address": host, "port": u.port, "method": method, "password": pw}]}})
elif u.scheme == "tuic":
    ob = {"type": "tuic", "tag": "proxy", "server": host, "server_port": u.port,
          "uuid": up.unquote(u.username), "password": up.unquote(u.password or ""),
          "congestion_control": q.get("congestion_control", "cubic"),
          "udp_relay_mode": q.get("udp_relay_mode", "native"),
          "tls": {"enabled": True, "server_name": q.get("sni") or host,
                  "insecure": q.get("allow_insecure") == "1",
                  "alpn": [a for a in q.get("alpn", "").split(",") if a]}}
    print("sing-box\t" + json.dumps({"log": {"level": "warn"}, "outbounds": [ob],
        "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": port}]}))
elif u.scheme in ("hysteria2", "hy2"):
    print("hysteria\t" + json.dumps({"server": uri, "socks5": {"listen": "127.0.0.1:%d" % port}}))
elif u.scheme == "vpn":
    import struct, zlib
    s = uri[6:]
    blob = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    n = struct.unpack(">I", blob[:4])[0]
    raw = zlib.decompress(blob[4:])
    assert len(raw) == n, "qCompress: длина не совпала"
    outer = json.loads(raw)
    inner = json.loads(outer["containers"][0]["awg"]["last_config"])
    print("vpn\t" + json.dumps({"config": inner["config"], "psk": inner.get("psk_key", ""),
                                 "container": outer["containers"][0]["container"]}))
elif u.scheme == "tg" and u.netloc == "proxy":
    print("tg\t" + json.dumps({"server": q["server"], "port": int(q["port"]), "secret": q["secret"]}))
else:
    print("unknown\t{}")
'

# ---------- клиентский контейнер ----------
IMAGE="$(docker container inspect -f '{{.Config.Image}}' "$SRV")"
docker rm -f "$CLI" >/dev/null 2>&1 || true
docker run -d --name "$CLI" --label zoo.harness=1 --label zoo.role=client --network "$NET" \
    --cap-add NET_ADMIN --entrypoint sleep "$IMAGE" infinity >/dev/null
arch="$(cx uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
sx cat "/usr/local/x-ui/bin/xray-linux-$arch" | cx sh -c 'cat > /usr/local/bin/xray && chmod 755 /usr/local/bin/xray'
if sx test -x /usr/local/bin/hysteria; then
    sx cat /usr/local/bin/hysteria | cx sh -c 'cat > /usr/local/bin/hysteria && chmod 755 /usr/local/bin/hysteria'
fi
if grep -q '^tuic	' <<< "$LINKS"; then
    sum_var="SINGBOX_SHA256_$arch"
    cx sh -c "set -e; cd /tmp; curl -fsSL --max-time 120 -o sb.tgz \
        $SINGBOX_URL_BASE/sing-box-$SINGBOX_VERSION-linux-$arch.tar.gz
        echo '${!sum_var}  sb.tgz' | sha256sum -c - >/dev/null
        tar -xzf sb.tgz; install -m 755 sing-box-$SINGBOX_VERSION-linux-$arch/sing-box /usr/local/bin/sing-box" \
        || bad "sing-box не поставился"
fi
cx sh -c "iptables -A OUTPUT -o lo -j ACCEPT && iptables -A OUTPUT -d $SRV_IP -j ACCEPT \
    && iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT && iptables -A OUTPUT -j REJECT"
cx curl -s -o /dev/null --max-time 5 "$SMALL_URL" && bad "прямой выход клиента не закрыт" || ok "прямой выход клиента закрыт"

fetch() { cx curl -sS --max-time "${3:-20}" -x "socks5h://127.0.0.1:$1" -o /dev/null -w '%{http_code} %{size_download}' "$2" 2>/dev/null || echo "000 0"; }

check() {
    local id="$1" kind="$2" port="$3" r ip u n=8
    r=""
    [ "${EXPECT_FAIL:-0}" = "1" ] && n=3
    for _ in $(seq 1 "$n"); do
        r="$(fetch "$port" "$SMALL_URL" 10)"
        [ "${r%% *}" = "204" ] && break
        sleep 3
    done
    if [ "${EXPECT_FAIL:-0}" = "1" ]; then
        [ "${r%% *}" = "204" ] && bad "$id: ссылка $LU всё ещё работает" || ok "$id: ссылка $LU не пускает ($r)"
        return
    fi
    if [ "${r%% *}" != "204" ]; then
        bad "$id: ссылка → $kind: малый запрос $r ($(cx tail -3 "/tmp/$id.log" | tr '\n' ' '))"
        return
    fi
    for u in "${BIG_URLS[@]}"; do
        r="$(fetch "$port" "$u" 90)"
        [ "${r%% *}" = "200" ] && [ "${r#* }" -ge "$BIG_MIN" ] && break
    done
    ip="$(cx curl -sS --max-time 15 -x "socks5h://127.0.0.1:$port" "$IP_URL" 2>/dev/null | sed -n 's/^ip=//p' || true)"
    if [ "${r#* }" -ge "$BIG_MIN" ] && [ "$ip" = "$EGRESS" ]; then
        ok "$id: ссылка импортирована в $kind: 204, ${r#* } Б, выход $ip"
    else
        bad "$id: ссылка → $kind: большой «$r», выход «$ip» (сервер $EGRESS)"
    fi
}

port=11000
while IFS=$'\t' read -r -u 3 id uri; do
    [ -n "$uri" ] || continue
    port=$((port + 1))
    out="$(sx python3 -c "$PARSER" "$uri" "$port" 2>&1)" || { bad "$id: ссылка не разбирается: $out"; continue; }
    kind="${out%%$'\t'*}"; cfg="${out#*$'\t'}"
    case "$kind" in
        xray)
            printf '%s' "$cfg" | cx sh -c "cat > /tmp/$id.json"
            cx xray run -test -c "/tmp/$id.json" >/dev/null 2>&1 || { bad "$id: xray -test отверг конфиг из ссылки: $(cx xray run -test -c "/tmp/$id.json" 2>&1 | tail -2)"; continue; }
            docker exec -d "$CLI" sh -c "exec xray run -c /tmp/$id.json > /tmp/$id.log 2>&1" ;;
        sing-box)
            printf '%s' "$cfg" | cx sh -c "cat > /tmp/$id.json"
            cx sing-box check -c "/tmp/$id.json" >/dev/null 2>&1 || { bad "$id: sing-box check отверг конфиг из ссылки"; continue; }
            docker exec -d "$CLI" sh -c "exec sing-box run -c /tmp/$id.json > /tmp/$id.log 2>&1" ;;
        hysteria)
            printf '%s' "$cfg" | cx sh -c "cat > /tmp/$id.json"
            docker exec -d -e HYSTERIA_DISABLE_UPDATE_CHECK=1 "$CLI" sh -c "exec hysteria client -c /tmp/$id.json > /tmp/$id.log 2>&1" ;;
        vpn)
            conf="$(sx cat "/etc/vpn-setup/clients/$LU/amneziawg.conf")"
            [ "${EXPECT_FAIL:-0}" != "1" ] || { info "$id: vpn:// без проверки (EXPECT_FAIL)"; continue; }
            if [ "$(sx python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["config"])' <<< "$cfg")" = "$conf" ] \
                && [ "$(sx python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["container"])' <<< "$cfg")" = "amnezia-awg" ]; then
                ok "$id: vpn:// раскодирован, last_config.config = .conf $LU"
            else
                bad "$id: vpn:// не совпадает с .conf $LU"
            fi
            continue ;;
        tg)
            # MTProxy: клиента Telegram на стенде нет — рукопожатие Fake-TLS с секретом из ссылки (подпись mtg)
            # shellcheck disable=SC2016 # код Python, $ здесь не нужен
            r="$(sx python3 -c 'import json, sys; sys.path.insert(0, "/repo/zoo")
from zoolib.probe import mtproto
c = json.loads(sys.argv[1]); print(mtproto.handshake(c["server"], c["port"], c["secret"], 8)[0])' "$cfg" 2>&1 || true)"
            if [ "$r" = ok ]; then ok "$id: tg://proxy — секрет из ссылки принят (рукопожатие Fake-TLS)"
            else bad "$id: tg://proxy — рукопожатие: $r"; fi
            continue ;;
        *) bad "$id: неизвестная схема ссылки: ${uri%%:*}"; continue ;;
    esac
    sleep 2
    check "$id" "$kind" "$port"
    cx sh -c "pkill -f '/tmp/$id.json'" >/dev/null 2>&1 || true
done 3<<< "$LINKS"

echo
[ "$fail" = "0" ] && echo "ИТОГ: PASS" || echo "ИТОГ: FAIL"
exit "$fail"
