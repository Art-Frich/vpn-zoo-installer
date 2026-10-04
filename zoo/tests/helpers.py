"""Тестовое окружение: временные /etc/vpn-setup, scripts/ с настоящим lib.sh и фейковыми
proto-<id>.sh, которые хранят пользователей в файлах и умеют падать по заказу.

FAKE_FAIL="id:fn,id2:fn2" — функция fn протокола id вернёт ошибку.
FAKE_STATE/calls.log — журнал вызовов «id fn args».
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ZOO_DIR = REPO / "zoo"
BASH = shutil.which("bash")

needs_bash = unittest.skipUnless(BASH, "нет bash")

# {fn} заменяется на имя функции: proto_<id с _>_<fn> или, при dashed, proto_<id>_<fn>
FAKE_PROTO = r"""
# фейковый модуль протокола {id} для тестов zoo
_fake_db="$FAKE_STATE/{id}.users"
_fake_hook() {{
    echo "{id} $*" >> "$FAKE_STATE/calls.log"
    case ",${{FAKE_FAIL:-}}," in
        *",{id}:$1,"*) log_err "{id}: искусственная ошибка $1"; return 1 ;;
    esac
    case ",${{FAKE_SLOW:-}}," in
        *",{id}:$1,"*) sleep 5 ;;
    esac
    return 0
}}
_fake_has() {{ [ -f "$_fake_db" ] && grep -q "^$1 " "$_fake_db"; }}
{p}user_add() {{
    _fake_hook user_add "$@" || return 1
    if _fake_has "$1"; then log_err "{id}: $1 уже есть"; return 1; fi
    log_info "{id}: добавляю $1"
    echo "$1 true" >> "$_fake_db"
    case ",${{FAKE_HALF:-}}," in
        *",{id}:user_add,"*) log_err "{id}: упал после записи $1"; return 1 ;;
    esac
    mkdir -p "$VPN_ETC/clients/$1"
    if [ "{id}" = "amneziawg" ]; then
        echo "[Interface]" > "$VPN_ETC/clients/$1/amneziawg.conf"
        echo "секрет" > "$VPN_ETC/clients/$1/amneziawg.key"
    fi
}}
{p}user_del() {{
    _fake_hook user_del "$@" || return 1
    _fake_has "$1" || {{ log_err "{id}: нет $1"; return 1; }}
    grep -v "^$1 " "$_fake_db" > "$_fake_db.tmp" || true
    mv "$_fake_db.tmp" "$_fake_db"
    rm -f "$VPN_ETC/clients/$1/amneziawg.conf" "$VPN_ETC/clients/$1/amneziawg.key"
}}
{p}user_enable() {{
    _fake_hook user_enable "$@" || return 1
    _fake_has "$1" || {{ log_err "{id}: нет $1"; return 1; }}
    grep -v "^$1 " "$_fake_db" > "$_fake_db.tmp" || true
    echo "$1 $2" >> "$_fake_db.tmp"
    mv "$_fake_db.tmp" "$_fake_db"
}}
{p}user_list() {{
    _fake_hook user_list || return 1
    log_info "{id}: список"
    [ -f "$_fake_db" ] && cut -d' ' -f1 "$_fake_db"
    return 0
}}
{p}links() {{
    _fake_hook links "$@" || return 1
    _fake_has "$1" || {{ log_err "{id}: нет $1"; return 1; }}
    log_warn "{id}: шум в логе"
    printf '%s\t%s\n' "{id}-основная" "vless://$1@${{SERVER_IP:-0.0.0.0}}:443?type=tcp#{id}-$1"
    # как настоящий proto-amneziawg.sh: путь к клиентскому .conf отдельной строкой
    if [ "{id}" = "amneziawg" ]; then printf '%s\n' "$VPN_ETC/clients/$1/amneziawg.conf"; fi
}}
{p}probe() {{
    _fake_hook probe "$@" || return 1
    log_info "{id}: probe"
    printf '{{"kind":"xray","outbound":{{"protocol":"vless","tag":"{id}","user":"%s"}}}}\n' "$1"
}}
{p}traffic() {{
    _fake_hook traffic || return 1
    [ -f "$_fake_db" ] || return 0
    while read -r n _; do printf '{{"user":"%s","up":10,"down":20}}\n' "$n"; done < "$_fake_db"
}}
{p}manifest_refresh() {{
    _fake_hook manifest_refresh || return 1
    : > "$FAKE_STATE/{id}.refreshed"
}}
{p}sleepy() {{ sleep 5; }}
"""


def manifest(pid: str, **kw) -> dict:
    data = {
        "id": pid, "name": f"Протокол {pid}", "layer": "tcp", "port": 443, "engine": "xray",
        "service": "x-ui", "enabled": True, "users_backend": "xui",
        "links": [{"user": "owner", "uri": f"vless://owner@1.2.3.4:443#{pid}"}],
        "files": [], "probe": {"kind": "xray", "outbound": {"protocol": "vless"}}, "notes": "",
    }
    data.update(kw)
    return data


class ZooEnv:
    """Временное окружение zoo. Использование: with ZooEnv() as env: ..."""

    ENV_KEYS = ("VPN_ETC", "CONFIG_FILE", "MANIFEST_DIR", "PORTS_FILE", "ZOO_USERS_FILE", "ZOO_CLIENTS_DIR",
                "ZOO_PROBE_EXPORT", "XUI_HDR_FILE", "ZOO_HOME", "ZOO_STATE_DIR", "ZOO_LOCK_FILE",
                "ZOO_SCRIPTS_DIR", "FAKE_STATE", "FAKE_FAIL", "FAKE_SLOW", "FAKE_HALF", "ZOO_BASH")

    def __enter__(self) -> "ZooEnv":
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        self.root = Path(tempfile.mkdtemp(prefix="zoo-test-"))
        self.etc = self.root / "etc"
        self.scripts = self.root / "scripts"
        self.fake = self.root / "fake"
        for d in (self.etc / "protocols.d", self.scripts / "lib", self.fake, self.root / "state"):
            d.mkdir(parents=True)
        shutil.copy(REPO / "scripts" / "lib.sh", self.scripts / "lib.sh")
        shutil.copy(REPO / "scripts" / "lib" / "xui.sh", self.scripts / "lib" / "xui.sh")
        # тестам — свой короткий пресет: пресет владельца может меняться
        (self.scripts / "allowlist-default.json").write_text(
            '{"android": ["com.brave.browser", "org.telegram.messenger"], "windows": ["brave.exe", "Telegram.exe"]}\n',
            encoding="utf-8")
        if (REPO / "scripts" / "versions.env").exists():
            shutil.copy(REPO / "scripts" / "versions.env", self.scripts / "versions.env")
        env = {
            "VPN_ETC": self.etc, "CONFIG_FILE": self.etc / "config.env",
            "MANIFEST_DIR": self.etc / "protocols.d", "PORTS_FILE": self.etc / "ports.tsv",
            "ZOO_USERS_FILE": self.etc / "users.json", "ZOO_CLIENTS_DIR": self.etc / "clients",
            "ZOO_PROBE_EXPORT": self.etc / "probe-export.json", "XUI_HDR_FILE": self.etc / "xui-auth.hdr",
            "ZOO_HOME": self.root / "opt", "ZOO_STATE_DIR": self.root / "state",
            "ZOO_LOCK_FILE": self.root / "state" / "zoo.lock", "ZOO_SCRIPTS_DIR": self.scripts,
            "FAKE_STATE": self.fake,
        }
        for k, v in env.items():
            os.environ[k] = Path(v).as_posix()
        for k in ("FAKE_FAIL", "FAKE_SLOW", "FAKE_HALF"):
            os.environ.pop(k, None)
        if BASH:
            os.environ["ZOO_BASH"] = BASH
        self.write_config({"SERVER_IP": "10.0.0.1", "LABEL": "test", "PANEL_PORT": "", "PANEL_PATH": ""})
        return self

    def __exit__(self, *exc) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------- наполнение ----------

    def write_config(self, values: dict[str, str]) -> None:
        lines = ["# test"] + [f"{k}='" + v.replace("'", "'\\''") + "'" for k, v in values.items()]
        (self.etc / "config.env").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def add_manifest(self, pid: str, **kw) -> dict:
        data = manifest(pid, **kw)
        (self.etc / "protocols.d" / f"{pid}.json").write_text(json.dumps(data, ensure_ascii=False),
                                                              encoding="utf-8")
        return data

    def add_proto(self, pid: str, dashed: bool = False, users: tuple[str, ...] = ()) -> None:
        """Фейковый proto-<id>.sh; users — уже заведённые пользователи."""
        prefix = f"proto_{pid}_" if dashed else f"proto_{pid.replace('-', '_')}_"
        text = FAKE_PROTO.replace("{p}", prefix).replace("{id}", pid).replace("{{", "{").replace("}}", "}")
        (self.scripts / "lib" / f"proto-{pid}.sh").write_text(text, encoding="utf-8", newline="\n")
        if users:
            (self.fake / f"{pid}.users").write_text("".join(f"{u} true\n" for u in users),
                                                    encoding="utf-8", newline="\n")

    def add_protocol(self, pid: str, users: tuple[str, ...] = ("owner",), **kw) -> None:
        self.add_manifest(pid, **kw)
        self.add_proto(pid, users=users)

    # ---------- наблюдение ----------

    def proto_users(self, pid: str) -> dict[str, str]:
        f = self.fake / f"{pid}.users"
        if not f.exists():
            return {}
        return dict(line.split() for line in f.read_text(encoding="utf-8").splitlines() if line.strip())

    def calls(self) -> list[str]:
        f = self.fake / "calls.log"
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []

    def fail(self, *specs: str) -> None:
        os.environ["FAKE_FAIL"] = ",".join(specs)

    def users_json(self) -> dict:
        return json.loads((self.etc / "users.json").read_text(encoding="utf-8"))
