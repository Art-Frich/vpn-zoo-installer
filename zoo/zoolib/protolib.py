"""Вызов bash-функций протоколов из scripts/lib/proto-<id>.sh.

Контракт модуля протокола (функции; имя id с «-» → «_», например proto_vless_reality_links,
вариант proto_vless-reality_links тоже находится):
    proto_<id>_user_add NAME            завести креды пользователя
    proto_<id>_user_del NAME            удалить
    proto_<id>_user_enable NAME true|false
    proto_<id>_user_list                имена, по одному в строке (или JSON-массив)
    proto_<id>_links NAME               строка — «uri», «метка<TAB>uri» или абсолютный путь
                                        к клиентскому файлу (.conf); либо JSON-массив
    proto_<id>_probe NAME               JSON {kind: xray|hysteria|awg, ...}
    proto_<id>_manifest_refresh         переписать манифест (links/files/probe)
    proto_<id>_traffic                  JSON-строки {user, up, down, ...}
Ненулевой код — ошибка, последняя строка stderr — её текст.

Обёртка: lib.sh → config_load → lib/xui.sh (если есть) → proto-<id>.sh → функция.
Логи lib.sh (log_info/ok/warn/step) уходят в stderr: stdout — только данные.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_TIMEOUT = 60.0
FN_TIMEOUTS = {
    "user_add": 120.0,
    "user_del": 120.0,
    "user_enable": 120.0,
    "manifest_refresh": 180.0,
}
RC_NO_FUNCTION = 127
# id протокола — часть пути к модулю и имени функции; имя пользователя модули кладут в пути
# (clients/<имя>/) и в jq/API: «../x», «-rf», пробелы и управляющие символы не пропускаем.
# Имя — правило модулей (шире NAME_RE реестра zoo: там ещё заглавные и точки).
PROTO_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
USER_ARG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")  # zoo_user_valid в lib.sh
NAME_FNS = {"user_add", "user_del", "user_enable", "links", "probe"}

_WRAPPER = r"""
set -euo pipefail
zoo_dir="$1" zoo_id="$2" zoo_fn="$3"
shift 3
zoo_quiet_logs() {
    log_info() { printf '[i] %s\n' "$*" >&2; }
    log_ok()   { printf '[+] %s\n' "$*" >&2; }
    log_warn() { printf '[!] %s\n' "$*" >&2; }
    log_step() { printf '=== %s ===\n' "$*" >&2; }
}
. "$zoo_dir/lib.sh"
zoo_quiet_logs
config_load
if [ -f "$zoo_dir/lib/xui.sh" ]; then . "$zoo_dir/lib/xui.sh"; fi
. "$zoo_dir/lib/proto-$zoo_id.sh"
zoo_quiet_logs
for zoo_f in "proto_${zoo_id//-/_}_${zoo_fn}" "proto_${zoo_id}_${zoo_fn}"; do
    if declare -F "$zoo_f" >/dev/null; then
        "$zoo_f" "$@"
        exit 0
    fi
done
echo "zoo: в proto-$zoo_id.sh нет функции proto_${zoo_id//-/_}_${zoo_fn}" >&2
exit 127
"""


class ProtoError(Exception):
    """Ошибка вызова функции протокола. kind: missing_lib | missing_fn | timeout | failed | bad_output."""

    def __init__(self, proto_id: str, fn: str, message: str, kind: str = "failed",
                 rc: int | None = None, stderr: str = "") -> None:
        super().__init__(f"{proto_id}: {fn}: {message}")
        self.proto_id = proto_id
        self.fn = fn
        self.kind = kind
        self.rc = rc
        self.stderr = stderr

    def short(self) -> str:
        """Последняя содержательная строка stderr или сообщение — для отчётов."""
        lines = [x.strip() for x in self.stderr.splitlines() if x.strip()]
        if not lines:
            return str(self)
        return re.sub(r"^\[[xi!+]\]\s*", "", lines[-1])


@dataclass
class ProtoResult:
    proto_id: str
    fn: str
    args: list[str]
    rc: int
    stdout: str
    stderr: str
    duration: float


@dataclass
class Link:
    """Ссылка (kind=uri) или путь к клиентскому файлу (kind=file, например AWG .conf)."""
    uri: str
    label: str = ""
    proto_id: str = ""
    kind: str = "uri"

    def to_dict(self) -> dict[str, str]:
        return {"proto": self.proto_id, "kind": self.kind, "label": self.label, "uri": self.uri}


@dataclass
class TrafficRow:
    user: str
    up: int
    down: int
    extra: dict[str, Any] = field(default_factory=dict)


def bash_path() -> str:
    """Путь к bash. На Windows (юнит-тесты) — bash из PATH (Git Bash), а не WSL из System32."""
    return os.environ.get("ZOO_BASH") or shutil.which("bash") or "/bin/bash"


def lib_path(proto_id: str, scripts: Path | None = None) -> Path:
    return (scripts or paths.scripts_dir()) / "lib" / f"proto-{proto_id}.sh"


def available(proto_id: str, scripts: Path | None = None) -> bool:
    return lib_path(proto_id, scripts).is_file()


def list_libs(scripts: Path | None = None) -> list[str]:
    """id протоколов, для которых есть proto-<id>.sh."""
    d = (scripts or paths.scripts_dir()) / "lib"
    return sorted(p.stem[len("proto-"):] for p in d.glob("proto-*.sh")) if d.is_dir() else []


def call(proto_id: str, fn: str, *args: str, timeout: float | None = None,
         check: bool = True, input_text: str | None = None) -> ProtoResult:
    """Вызвать proto_<id>_<fn> args... и вернуть результат. При check=True и rc != 0 — ProtoError."""
    if not PROTO_ID_RE.fullmatch(proto_id or "") or not re.fullmatch(r"[a-z_]+", fn or ""):
        raise ProtoError(str(proto_id), str(fn), "недопустимый id протокола или функция", kind="missing_lib")
    if fn in NAME_FNS and not (args and USER_ARG_RE.fullmatch(str(args[0]))):
        raise ProtoError(proto_id, fn, f"недопустимое имя пользователя «{args[0] if args else ''}»",
                         kind="failed", stderr="недопустимое имя пользователя")
    scripts = paths.scripts_dir()
    if not available(proto_id, scripts):
        raise ProtoError(proto_id, fn, f"нет {lib_path(proto_id, scripts)}", kind="missing_lib")
    timeout = timeout if timeout is not None else FN_TIMEOUTS.get(fn, DEFAULT_TIMEOUT)
    env = dict(os.environ)
    env.update({
        "SCRIPTS_DIR": scripts.as_posix(),
        "REPO_ROOT": scripts.parent.as_posix(),
        "ZOO_PHASE": env.get("ZOO_PHASE", "zoo"),
        "ZOO_CALLER": "zoo",
    })
    argv = [bash_path(), "-c", _WRAPPER, "zoo-proto", scripts.as_posix(), proto_id, fn, *map(str, args)]
    started = time.monotonic()
    try:
        cp = subprocess.run(
            argv, input=input_text, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, cwd=scripts.parent.as_posix(),
            stdin=None if input_text is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise ProtoError(proto_id, fn, f"таймаут {timeout:.0f} с", kind="timeout", stderr=stderr) from None
    except OSError as e:
        raise ProtoError(proto_id, fn, f"не удалось запустить bash: {e}", kind="failed") from None
    res = ProtoResult(proto_id, fn, list(map(str, args)), cp.returncode, cp.stdout, cp.stderr,
                      time.monotonic() - started)
    if check and cp.returncode != 0:
        kind = "missing_fn" if cp.returncode == RC_NO_FUNCTION and "zoo: в proto-" in cp.stderr else "failed"
        raise ProtoError(proto_id, fn, f"код {cp.returncode}", kind=kind, rc=cp.returncode, stderr=cp.stderr)
    return res


# ---------- функции контракта с разбором вывода ----------

def user_add(proto_id: str, name: str) -> None:
    call(proto_id, "user_add", name)


def user_del(proto_id: str, name: str) -> None:
    call(proto_id, "user_del", name)


def user_enable(proto_id: str, name: str, enabled: bool) -> None:
    call(proto_id, "user_enable", name, "true" if enabled else "false")


def user_list(proto_id: str) -> list[str]:
    out = call(proto_id, "user_list").stdout
    data = _try_json(out)
    if isinstance(data, list):
        names = []
        for x in data:
            if isinstance(x, str):
                names.append(x)
            elif isinstance(x, dict):
                n = x.get("name") or x.get("user") or x.get("email")
                if n:
                    names.append(str(n))
        return names
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def links(proto_id: str, name: str) -> list[Link]:
    out = call(proto_id, "links", name).stdout
    data = _try_json(out)
    result: list[Link] = []
    if isinstance(data, list):
        for x in data:
            if isinstance(x, str):
                x = {"uri": x}
            if isinstance(x, dict) and x.get("uri"):
                uri = str(x["uri"])
                result.append(Link(uri, str(x.get("label", "")), proto_id, _kind(uri)))
        return result
    for line in out.splitlines():
        line = line.strip()
        label, _, value = line.rpartition("\t")
        value = value.strip()
        if "://" in value or _is_path(value):
            result.append(Link(value, label.strip(), proto_id, _kind(value)))
    return result


def _is_path(value: str) -> bool:
    # «/…» — путь на сервере; isabs — ещё и C:/… в юнит-тестах на Windows
    return value.startswith("/") or os.path.isabs(value)


def _kind(value: str) -> str:
    return "file" if _is_path(value) and "://" not in value else "uri"


def probe(proto_id: str, name: str = "owner") -> dict[str, Any]:
    out = call(proto_id, "probe", name).stdout
    data = _try_json(out, embedded=True)
    if not isinstance(data, dict) or "kind" not in data:
        raise ProtoError(proto_id, "probe", "вывод — не JSON-объект с полем kind", kind="bad_output",
                         stderr=out[-500:])
    return data


def manifest_refresh(proto_id: str) -> None:
    call(proto_id, "manifest_refresh")


def traffic(proto_id: str) -> list[TrafficRow]:
    out = call(proto_id, "traffic").stdout
    data = _try_json(out)
    items = data if isinstance(data, list) else [_try_json(x) for x in out.splitlines() if x.strip()]
    rows = []
    for x in items:
        if not isinstance(x, dict) or "user" not in x:
            continue
        try:
            up, down = int(x.get("up", 0) or 0), int(x.get("down", 0) or 0)
        except (TypeError, ValueError):
            continue
        extra = {k: v for k, v in x.items() if k not in ("user", "up", "down")}
        # user=null — итог без пользователя (например, scope=inbound у vless-xhttp) → ""
        rows.append(TrafficRow("" if x["user"] is None else str(x["user"]), up, down, extra))
    return rows


def _try_json(text: str, embedded: bool = False) -> Any:
    """JSON из всего вывода; embedded=True — первый JSON-объект внутри вывода."""
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if embedded:
        start = s.find("{")
        while start != -1:
            try:
                return json.JSONDecoder().raw_decode(s, start)[0]
            except json.JSONDecodeError:
                start = s.find("{", start + 1)
    return None
