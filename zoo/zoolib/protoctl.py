"""Включить или выключить протокол из админки (D42).

Фаза протокола пишет за пределы песочницы админки (ufw, юниты, /etc), поэтому админка её не запускает: кладёт
заявку `jobs/<uuid>.json` в каталог состояния. `zoo-job.path` видит файл и запускает
`zoo-job.service` (root, без песочницы) → `zoo job run`: ключ ENABLE_* в config.env и
`install.sh --phase <фаза протокола>` — тот же путь, что у владельца в консоли.

    jobs/<uuid>.json          заявка (пока не выполнена): {id, proto, action, created}
    jobs/state/<uuid>.json    состояние: running | ok | fail, время, код возврата
    jobs/state/<uuid>.log     вывод install.sh (0600; в браузер — только через logs.sanitize)

Что можно трогать, решает таблица STATIC, а не запрос и не манифест: фаза и ENABLE_* берутся только
из неё. Манифест пишет proto-<id>.sh, но его каталог читает и веб-процесс, поэтому phase/enable_var
оттуда принимаются, лишь когда пара равна STATIC[id], — то есть ничего нового не дают; из манифеста
берутся имя и enabled. Фаза проверяется по файлу в установленной копии (zoo_home()/scripts/<фаза>.sh),
оттуда же запускается install.sh. Перед фазой config.env приводится к формату config_set (KEY='значение'):
исполнитель — root, и install.sh делает source этого файла, так что любая строка, кроме простого
присваивания, — отказ. Одна задача за раз
(flock), последний включённый протокол выключить нельзя.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, manifests, output, paths, protolib, users
from .fsutil import (NOFOLLOW, LockTimeout, UnsafePath, atomic_write_json, check_real_dirs, file_lock,
                     open_append_nofollow, read_text_nofollow)

JOB_TIMEOUT = 30 * 60
QUEUE_STALE = 10 * 60   # заявку не забрали: исполнитель не работает — она не держит переключатели и не выполняется позже
KEEP_STATES = 20
ID_RE = re.compile(r"^[0-9a-f]{32}$")
PHASE_RE = re.compile(r"^\d{2}[a-z]?-[a-z0-9-]+$")
VAR_RE = re.compile(r"^ENABLE_[A-Z0-9_]+$")
ACTIONS = ("enable", "disable")


class JobError(Exception):
    """Заявку нельзя принять или выполнить; текст — по-русски, для человека."""


@dataclass
class Ctl:
    id: str
    name: str
    phase: str
    var: str
    requires: str | None = None   # протокол, без которого этот не включается (Salamander → Hysteria2)
    enabled: bool = False
    installed: bool = False       # есть манифест (протокол когда-то ставился)


# единственный источник фазы и ключа: что можно включать и выключать, решает эта таблица
STATIC: dict[str, Ctl] = {c.id: c for c in (
    Ctl("vless-reality", "VLESS + REALITY", "04-vless-reality", "ENABLE_VLESS"),
    Ctl("vless-xhttp", "VLESS + XHTTP + REALITY", "04b-vless-xhttp", "ENABLE_XHTTP"),
    Ctl("ss2022", "Shadowsocks-2022", "04c-ss2022", "ENABLE_SS"),
    Ctl("tuic", "TUIC v5", "04d-tuic", "ENABLE_TUIC"),
    Ctl("hysteria2", "Hysteria2", "05-hysteria2", "ENABLE_HY2"),
    Ctl("hysteria2-obfs", "Hysteria2 + Salamander", "05-hysteria2", "ENABLE_HY2_OBFS", requires="hysteria2"),
    Ctl("amneziawg", "AmneziaWG", "06-amneziawg", "ENABLE_AWG"),
)}


def jobs_dir() -> Path:
    return paths.state_dir() / "jobs"


def state_dir() -> Path:
    return jobs_dir() / "state"


def installed_scripts() -> Path:
    """scripts/ установленной копии: исполнитель берёт install.sh и проверяет фазы только здесь."""
    return paths.zoo_home() / "scripts"


def _phase_ok(phase: str) -> bool:
    return bool(PHASE_RE.match(phase)) and (installed_scripts() / f"{phase}.sh").is_file()


def controls() -> dict[str, Ctl]:
    """Протоколы, которыми можно управлять: {id: Ctl}. Только id из STATIC с существующей фазой;
    phase/enable_var манифеста не используются (пара не из STATIC никогда не принимается)."""
    good, _ = manifests.load_all()
    by_id = {m.id: m for m in good}
    try:
        fallback = config.load().get("XHTTP_PLACEMENT") == "fallback"
    except config.ConfigError:
        fallback = False
    out: dict[str, Ctl] = {}
    for sid, st in STATIC.items():
        if not (_phase_ok(st.phase) and VAR_RE.match(st.var)):
            continue
        requires = st.requires
        if sid == "vless-xhttp" and fallback:
            requires = "vless-reality"   # child за inbound'ом фазы 04: без него XHTTP не принимает
        m = by_id.get(sid)
        if m is not None:
            out[sid] = Ctl(st.id, m.name, st.phase, st.var, requires, m.enabled, True)
        elif not requires or (requires in out and out[requires].enabled):
            out[sid] = Ctl(st.id, st.name, st.phase, st.var, requires)
    return out


def _dependents(ctls: dict[str, Ctl], proto: str) -> set[str]:
    return {c.id for c in ctls.values() if c.requires == proto}


def check(proto: str, action: str, ctls: dict[str, Ctl] | None = None) -> Ctl:
    """Допустима ли заявка прямо сейчас; иначе JobError."""
    ctls = ctls if ctls is not None else controls()
    if action not in ACTIONS:
        raise JobError("неизвестное действие")
    ctl = ctls.get(proto)
    if ctl is None:
        raise JobError("этим протоколом нельзя управлять отсюда")
    if action == "enable":
        if ctl.enabled:
            raise JobError(f"{ctl.name} уже включён")
        dep = ctls.get(ctl.requires) if ctl.requires else None
        if ctl.requires and not (dep and dep.enabled):
            raise JobError(f"{ctl.name} без {dep.name if dep else ctl.requires} не включить: сначала включите его")
        return ctl
    if not ctl.enabled:
        raise JobError(f"{ctl.name} уже выключен")
    if proto == "vless-reality":
        child = next((c for c in ctls.values() if c.id == "vless-xhttp" and c.requires == proto and c.enabled), None)
        if child:
            raise JobError(f"Выключить {ctls[proto].name} нельзя: сначала выключите VLESS XHTTP (он работает через VLESS).")
    gone = {proto} | _dependents(ctls, proto)
    if not any(c.enabled and c.id not in gone for c in ctls.values()):
        raise JobError("Это последний включённый протокол: выключив его, вы потеряете доступ. Сначала включите другой.")
    return ctl


# ---------- заявки и состояния (читает и пишет админка) ----------

def submit(proto: str, action: str) -> str:
    """Принять заявку: проверка, одна задача за раз, файл jobs/<uuid>.json. → id задачи."""
    jobs_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # проверка, active() и запись — под одним замком: два одновременных POST не пройдут оба
        with file_lock(jobs_dir() / "submit.lock", timeout=10):
            ctl = check(proto, action)
            busy = active()
            if busy:
                raise JobError(f"Сейчас идёт: {busy['verb']} {busy['name']}. Дождитесь окончания.")
            jid = uuid.uuid4().hex
            atomic_write_json(jobs_dir() / f"{jid}.json",
                              {"id": jid, "proto": ctl.id, "action": action, "created": int(time.time())}, 0o600)
    except LockTimeout:
        raise JobError("Заявки принимаются по одной, повторите через несколько секунд.") from None
    return jid


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(read_text_nofollow(path))   # заявку кладёт админка: ссылку на чужой файл не читаем
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


VERB = {"enable": "включение", "disable": "выключение"}


def _describe(d: dict[str, Any], status: str) -> dict[str, Any]:
    proto = str(d.get("proto") or "")
    name = d.get("name") or (STATIC[proto].name if proto in STATIC else proto)
    action = d.get("action") if d.get("action") in ACTIONS else "enable"
    return {"id": d.get("id"), "proto": proto, "action": action, "name": name, "status": status,
            "verb": VERB[action], "started": d.get("started"), "finished": d.get("finished"),
            "rc": d.get("rc"), "error": d.get("error") or "", "created": d.get("created")}


def _fresh(j: dict[str, Any], now: float) -> bool:
    """В очереди меньше QUEUE_STALE (время — из заявки; нет времени — считаем свежей)."""
    created = j.get("created")
    return not isinstance(created, (int, float)) or now - created < QUEUE_STALE


def queued() -> list[dict[str, Any]]:
    out = []
    try:
        names = sorted(os.listdir(jobs_dir()))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and ID_RE.match(n[:-5]):
            d = _read(jobs_dir() / n)
            if d and d.get("id") == n[:-5]:
                out.append(_describe(d, "queued"))
    return out


def states() -> list[dict[str, Any]]:
    """Состояния задач, новые первыми."""
    out = []
    try:
        files = [f for f in state_dir().iterdir() if f.suffix == ".json" and ID_RE.match(f.stem)]
    except OSError:
        return out
    for f in sorted(files, key=lambda f: -f.stat().st_mtime):
        d = _read(f)
        if d and d.get("id") == f.stem:
            out.append(_describe(d, d.get("status") if d.get("status") in ("running", "ok", "fail") else "fail"))
    return out


def active() -> dict[str, Any] | None:
    """Задача в очереди или выполняется (зависшая «выполняется» старше таймаута не считается)."""
    now = time.time()
    for j in states():
        if j["status"] == "running" and now - (j["started"] or 0) < JOB_TIMEOUT + 120:
            return j
    q = [j for j in queued() if _fresh(j, now)]
    return q[0] if q else None


def active_by_proto() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    now = time.time()
    for j in [q for q in queued() if _fresh(q, now)] + [s for s in states() if s["status"] == "running" and now - (s["started"] or 0) < JOB_TIMEOUT + 120]:
        out.setdefault(j["proto"], j)
    return out


def get_state(jid: str) -> dict[str, Any] | None:
    if not ID_RE.match(jid):
        return None
    d = _read(state_dir() / f"{jid}.json")
    if d and d.get("id") == jid:
        return _describe(d, d.get("status") if d.get("status") in ("running", "ok", "fail") else "fail")
    for q in queued():
        if q["id"] == jid:
            return q
    return None


def log_tail(jid: str, lines: int = 40) -> str:
    """Хвост вывода install.sh; секреты не вырезаны — перед показом пропускать через logs.sanitize."""
    if not ID_RE.match(jid):
        return ""
    try:
        with open(state_dir() / f"{jid}.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - 64 * 1024, 0))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


# ---------- исполнитель (zoo job run, root, вне песочницы) ----------

def install_script() -> Path:
    return installed_scripts() / "install.sh"


def _write_state(jid: str, data: dict[str, Any]) -> None:
    state_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(state_dir() / f"{jid}.json", data, 0o600)


def _prune() -> None:
    for f in sorted(state_dir().glob("*.json"), key=lambda f: -f.stat().st_mtime)[KEEP_STATES:]:
        for g in (f, f.with_suffix(".log")):
            try:
                g.unlink()
            except OSError:
                pass


def _bash_value(rest: str) -> str:
    r"""Значение присваивания KEY=<rest> так, как его прочтёт bash при source. ValueError — если это не
    одно простое слово: подстановка ($, `, $( ), метасимвол (; & | < > ( )), тильда, команда после
    значения, незакрытая кавычка. Ни shlex, ни parse_line тут не годятся: они иначе режут слова
    (\r, \x0c — не пробел для bash, а `a;id` — две команды, не одно слово)."""
    out: list[str] = []
    i, n = 0, len(rest)
    while i < n:
        c = rest[i]
        if c == "'":
            j = rest.find("'", i + 1)
            if j < 0:
                raise ValueError("незакрытая кавычка")
            out.append(rest[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise ValueError("незакрытая кавычка")
                c = rest[i]
                if c == '"':
                    i += 1
                    break
                if c in "$`":
                    raise ValueError("подстановка команды или переменной")
                if c == "\\" and i + 1 < n and rest[i + 1] in ('$', '`', '"', "\\"):
                    out.append(rest[i + 1])
                    i += 2
                    continue
                out.append(c)
                i += 1
        elif c == "\\":
            if i + 1 >= n:
                raise ValueError("перенос строки через \\")
            out.append(rest[i + 1])
            i += 2
        elif c in " \t":
            tail = rest[i:].lstrip(" \t")
            if tail and not tail.startswith("#"):
                raise ValueError("после значения что-то ещё (команда?)")
            break
        elif c in "$`;&|<>()~":
            raise ValueError(f"спецсимвол {c!r} вне кавычек")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _normalise_line(line: str) -> str:
    """Строка config.env → строка в формате config_set. Комментарии и пустые — как есть."""
    s = line.lstrip(" \t")
    if not s or s.startswith("#"):
        return line
    if "\0" in line:
        raise ValueError("нулевой байт")
    if s.startswith(("export ", "export\t")):
        s = s[len("export"):].lstrip(" \t")
    key, sep, rest = s.partition("=")
    if not sep or not config.KEY_RE.match(key):
        raise ValueError("не KEY=значение")
    return f"{key}={config.quote(_bash_value(rest))}"


def normalise_config_file() -> None:
    r"""config.env читает root-овый install.sh через source, а файл можно править руками (v1: SERVER_IP="1.2.3.4",
    ENABLE_SS=1). Перед фазой каждая строка приводится к формату config_set (KEY='значение') под той же
    блокировкой, что у config_set в lib.sh; значение — ровно то, что дал бы bash. Строка, которая не простое
    присваивание или содержит подстановку, — JobError, файл не меняется, ничего не запускается.
    Строки делятся только по \n: \r, \x0c, U+0085, U+2028 внутри значения не разрывают строку."""
    path = paths.config_file()
    try:
        with file_lock(paths.config_lock_file()):
            try:
                with open(path, encoding="utf-8", newline="") as f:
                    text = f.read()
            except FileNotFoundError:
                return
            lines = text.split("\n")
            out: list[str] = []
            for n, line in enumerate(lines, 1):
                try:
                    out.append(_normalise_line(line))
                except ValueError as e:
                    raise JobError(f"config.env, строка {n}: {e} — это не простое присваивание KEY='значение', "
                                   "задачу не запускаю. Поправьте файл руками на сервере.") from None
            new = "\n".join(out)
            if new != text:
                fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                        f.write(new)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
    except LockTimeout as e:
        raise JobError(f"config.env занят: {e}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise JobError(f"config.env не прочитать: {e}") from None


def _run_phase(phase: str, log: Path, extra_env: dict[str, str] | None = None) -> int:
    env = dict(os.environ, ZOO_CALLER="zoo-job", NO_COLOR="1", ZOO_COLOR="0", **(extra_env or {}))
    script = install_script()
    with open_append_nofollow(log) as f:
        f.write(f"$ install.sh --phase {phase}\n".encode())
        f.flush()
        try:
            return subprocess.run([protolib.bash_path(), str(script), "--phase", phase], cwd=str(script.parent.parent),
                                  env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                  timeout=JOB_TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            f.write(f"\nтаймаут {JOB_TIMEOUT // 60} мин\n".encode())
            return 124
        except OSError as e:
            f.write(f"\n{e}\n".encode())
            return 127


def _sync_users(log: Path) -> None:
    """Протокол включили (возможно, впервые): завести в нём креды существующих пользователей.
    Сбой не проваливает задачу — пользователей можно досинхронизировать кнопкой на странице «Пользователи»."""
    try:
        reps = users.sync_users()
        bad = [f"{r.user}: {r.message}" for r in reps if not r.ok]
        note = "zoo user sync: " + ("; ".join(bad) if bad else f"пользователей: {len(reps)}, готово")
    except Exception as e:  # sync не должен ронять исполнитель
        note = f"zoo user sync не выполнен: {e}"
    with open_append_nofollow(log) as f:
        f.write((note + "\n").encode())


def _run_one(req: Path) -> None:
    jid = req.stem
    data = _read(req) or {}
    proto, action = str(data.get("proto") or ""), str(data.get("action") or "")
    st: dict[str, Any] = {"id": jid, "proto": proto, "action": action if action in ACTIONS else "enable",
                          "status": "running", "started": int(time.time())}
    log = state_dir() / f"{jid}.log"
    try:
        _run_checked(jid, data, proto, action, st, log)
    finally:
        try:   # заявка уходит всегда, даже если упала запись состояния: иначе path-юнит крутит её до упора
            req.unlink()
        except OSError:
            pass
    _prune()


def _run_checked(jid: str, data: dict[str, Any], proto: str, action: str, st: dict[str, Any],
                 log: Path) -> None:
    try:
        if data.get("id") != jid:
            raise JobError("заявка повреждена")
        created = data.get("created")
        if isinstance(created, (int, float)) and time.time() - created > QUEUE_STALE:
            raise JobError("заявка пролежала дольше 10 минут (исполнитель не работал) — повторите из админки")
        ctl = check(proto, action)
        st["name"] = ctl.name
        _write_state(jid, st)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW, 0o600)
        os.close(fd)
        normalise_config_file()
        # ключ едет в окружении: install.sh сам сохранит его в config.env под своей блокировкой;
        # при сбое фазы возвращаем прежнее значение, чтобы файл не расходился с реальным состоянием
        value = "1" if action == "enable" else "0"
        old = config.load().values.get(ctl.var)
        rc = _run_phase(ctl.phase, log, {ctl.var: value})
        if rc != 0 and old is not None:
            config.config_set(ctl.var, old)
        st.update(rc=rc, status="ok" if rc == 0 else "fail")
        if rc == 0 and action == "enable":
            _sync_users(log)
        if rc != 0:
            st["error"] = f"install.sh --phase {ctl.phase} завершился с кодом {rc}"
    except (JobError, OSError, config.ConfigError) as e:
        st.update(status="fail", rc=st.get("rc", 1), error=str(e))
    st["finished"] = int(time.time())
    _write_state(jid, st)


def _drain() -> None:
    # «выполняется» без живого исполнителя (мы держим блокировку) — след оборванной задачи; её заявку убираем,
    # иначе она молча выполнится ещё раз (после перезагрузки, раньше x-ui) и затрёт «оборвана»
    for f in state_dir().glob("*.json") if state_dir().is_dir() else []:
        d = _read(f)
        if d and d.get("status") == "running":
            d.update(status="fail", error="оборвана (перезагрузка или остановка юнита) — повторите из админки",
                     finished=int(time.time()))
            atomic_write_json(f, d, 0o600)
            try:
                (jobs_dir() / f"{f.stem}.json").unlink()
            except OSError:
                pass
    for _ in range(50):
        _check_dirs()
        reqs = sorted((f for f in jobs_dir().glob("*.json") if ID_RE.match(f.stem)), key=lambda f: f.stat().st_mtime)
        if not reqs:
            return
        _run_one(reqs[0])


def _check_dirs() -> None:
    check_real_dirs(paths.state_dir(), jobs_dir(), state_dir())


def run_queue() -> int:
    """Выполнить заявки по порядку; уже работающий исполнитель — выйти (он дочитает очередь).
    Это root в каталоге, куда пишет админка: каталог заявок — ссылка → отказ, ничего не трогаем."""
    try:
        _check_dirs()
        jobs_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        with file_lock(jobs_dir() / "runner.lock", timeout=0, nofollow=True):
            _drain()
    except LockTimeout:
        pass
    except UnsafePath as e:
        output.error(str(e))
        return 1
    return 0


# ---------- CLI ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", nargs="?", choices=("run", "list"), default="list",
                   help="run — выполнить очередь заявок (юнит zoo-job.service), list — состояния задач")


def cmd_job(args: argparse.Namespace, cfg: Any) -> int:
    if args.action == "run":
        return run_queue()
    rows = queued() + states()
    if args.json:
        output.print_json(rows)
    elif rows:
        print(output.table([[j["id"][:8], j["name"], j["verb"], j["status"],
                             "" if not j["started"] else time.strftime("%d.%m %H:%M", time.localtime(j["started"]))]
                            for j in rows], ["задача", "протокол", "действие", "итог", "начата"]))
    else:
        output.info("задач не было")
    return 0
