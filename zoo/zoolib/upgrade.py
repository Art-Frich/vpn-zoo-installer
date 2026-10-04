"""Смоук-проверка и обновление закреплённых версий (ARCHITECTURE §6, D1).

`zoo smoke` — быстрая проверка здоровья: сервисы протоколов активны, порты слушают,
API 3x-ui и Xray живы, UFW включён, нет лишних listen, коллектор трафика свежий,
сводка `zoo probe --local` (если пробник есть).

`zoo upgrade` — план: что устарело относительно versions.env рабочей копии репо
(установленные бинарники и разница versions.env рабочей копии и копии в /opt/vpn-zoo),
какие фазы install.sh перезапустить. `zoo upgrade --apply [--pull]` — git pull (по
желанию), smoke до, `install.sh --phase` по плану, smoke после и подсказка отката.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import output, paths, protolib, status, system, traffic
from . import probe as probe_mod
from .config import Config

# префикс ключа versions.env → фаза, которая ставит компонент
VERSION_PHASES = (("XUI_", "03-3xui"), ("HY2_", "05-hysteria2"), ("AWG_", "06-amneziawg"),
                  ("GO_", "06-amneziawg"), ("GEO_", "07-routing"))
PHASE_ORDER = ("03-3xui", "04-vless-reality", "04b-vless-xhttp", "04c-ss2022", "04d-tuic",
               "05-hysteria2", "06-amneziawg", "07-routing", "08-warp", "09-zoo", "99-print-creds")


# ---------- smoke ----------

def _check(name: str, ok: bool | None, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def probe_summary(results: list[dict[str, Any]]) -> tuple[bool, str]:
    """Провал — протокол не работает на сервере; SLOW и «не проверялось» (нет клиента
    на сервере) — не провал, но видны в подробностях."""
    v = probe_mod.verdicts
    bad = [f"{r.get('id')}: {r.get('verdict')}" for r in results
           if r.get("verdict") not in v.WORKING and r.get("verdict") not in v.NOT_TESTED]
    other = [f"{r.get('id')}: {r.get('verdict')}" for r in results if r.get("verdict") not in (v.OK,)
             and f"{r.get('id')}: {r.get('verdict')}" not in bad]
    good = sum(1 for r in results if r.get("verdict") in v.WORKING)
    detail = f"работает {good}/{len(results)}" + "".join(f"; {x}" for x in bad + other)
    return not bad, detail


def smoke(cfg: Config, with_probe: bool = True, probe_timeout: float = 10.0) -> dict[str, Any]:
    """{ok, checks: [{name, ok: true|false|null, detail}], probe, ts}; ok=None — проверка пропущена."""
    st = status.collect(cfg, cpu_interval=0.05)
    checks: list[dict[str, Any]] = []
    if not st["protocols"]:
        checks.append(_check("протоколы", False, "нет манифестов (фазы 04–06 не выполнены?)"))
    for p in st["protocols"]:
        if not p["enabled"]:
            checks.append(_check(f"протокол {p['id']}", None, "выключен в манифесте"))
            continue
        svc = ", ".join(f"{u} {s}" for u, s in p["services"].items()) or "без сервиса"
        listen = ", ".join(f"{p['port']}/{k} {'слушает' if v else 'НЕ слушает'}" for k, v in p["listening"].items())
        checks.append(_check(f"протокол {p['id']}", p["ok"], f"{svc}; {listen}"))
    for unit in status.BASE_UNITS:
        s = st["services"].get(unit, {})
        if s.get("load") == "loaded":
            checks.append(_check(f"сервис {unit}", s.get("active") == "active", s.get("active", "")))
    fw = st["firewall"]["ufw_active"]
    checks.append(_check("UFW", fw, "не установлен" if fw is None else ("включён" if fw else "выключен")))
    if st["xray"]:
        x = st["xray"]
        ok = x.get("state") == "running"
        checks.append(_check("Xray (API 3x-ui)", ok, (f"{x.get('state', '')} {x.get('version', '')} "
                                                        f"{x.get('error', '')}").strip()))
    exposed = [f"{e['port']}/{e['proto']} ({e['process'] or '?'})" for e in st["exposed"]]
    checks.append(_check("лишние listen на всех адресах", not exposed, ", ".join(exposed) or "нет"))
    for c in st["certs"]:
        if c["days_left"] is not None:
            checks.append(_check(f"сертификат {c['path']}", c["days_left"] >= status.CERT_WARN_DAYS,
                                 f"осталось {c['days_left']} дн."))
    for unit in status.ZOO_UNITS:
        s = st["services"].get(unit, {})
        if s.get("load") == "loaded" and s.get("enabled") == "enabled":
            checks.append(_check(f"zoo: {unit}", s.get("active") == "active", s.get("active", "")))
    run = traffic.last_run()
    if run:
        checks.append(_check("коллектор трафика", not run["stale"],
                             f"последнее снятие {output.human_duration(run['age'])} назад"
                             + (f", ошибки: {', '.join(run['errors'])}" if run["errors"] else "")))
    probe_res = None
    if with_probe:
        try:
            st_ = probe_mod.engine.Settings(timeout=probe_timeout)
            st_.progress = None
            rep = probe_mod.run_local(cfg, None, "owner", st_)
            probe_res = rep.get("results", [])
            ok, detail = probe_summary(probe_res)
            checks.append(_check("zoo probe --local", ok if probe_res else None, detail if probe_res else
                                 "нет протоколов с probe"))
        except NotImplementedError:
            checks.append(_check("zoo probe --local", None, "пробник ещё не реализован"))
        except Exception as e:  # пробник не должен ронять smoke
            checks.append(_check("zoo probe --local", False, f"ошибка пробника: {e}"))
    ok = all(c["ok"] is not False for c in checks)
    return {"ok": ok, "checks": checks, "probe": probe_res, "ts": int(time.time())}


def _render_smoke(data: dict[str, Any]) -> None:
    for c in data["checks"]:
        mark = output.color("—", "dim") if c["ok"] is None else output.mark(c["ok"])
        print(f"  {mark:>4}  {c['name']}" + (output.color(f"  {c['detail']}", "dim") if c["detail"] else ""))
    print()
    if data["ok"]:
        output.ok("smoke: всё в порядке")
    else:
        output.error("smoke: есть проблемы (" + ", ".join(c["name"] for c in data["checks"] if c["ok"] is False) + ")")


def add_smoke_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-probe", action="store_true", help="без zoo probe --local")
    p.add_argument("--timeout", type=float, default=10.0, help="таймаут рукопожатия пробника, с")


def cmd_smoke(args: argparse.Namespace, cfg: Config) -> int:
    data = smoke(cfg, with_probe=not args.no_probe, probe_timeout=args.timeout)
    output.emit(data, args.json, _render_smoke)
    return 0 if data["ok"] else 1


# ---------- upgrade: что устарело ----------

def install_info() -> dict[str, Any]:
    try:
        return json.loads((paths.zoo_home() / "INSTALL.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def repo_dir(explicit: str | None = None) -> Path | None:
    """Рабочая копия репо, из которой ставили: --repo, ZOO_REPO или source из INSTALL.json."""
    for cand in (explicit, os.environ.get("ZOO_REPO"), install_info().get("source")):
        if cand and (Path(cand) / "scripts" / "install.sh").is_file():
            return Path(cand)
    return None


def _git(repo: Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    rc, out, err = system.run(["git", "-C", str(repo), *args], timeout=timeout)
    return rc, (out if rc == 0 else err).strip()


def git_state(repo: Path, fetch: bool = False) -> dict[str, Any]:
    rc, rev = _git(repo, "rev-parse", "--short", "HEAD")
    if rc != 0:
        return {"git": False}
    _, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _, dirty = _git(repo, "status", "--porcelain")
    st: dict[str, Any] = {"git": True, "rev": rev, "branch": branch, "dirty": bool(dirty)}
    if fetch:
        rc, err = _git(repo, "fetch", "--quiet", timeout=120)
        st["fetch_error"] = "" if rc == 0 else err
    rc, behind = _git(repo, "rev-list", "--count", "HEAD..@{upstream}")
    st["behind"] = int(behind) if rc == 0 and behind.isdigit() else None
    return st


def read_versions(f: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def _norm(v: str) -> str:
    return v.strip().lstrip("vV")


def components(installed: dict[str, str], pinned: dict[str, str]) -> dict[str, dict[str, Any]]:
    """{компонент: {installed, pinned, outdated, phase}}; outdated=None — не сравнить."""
    pairs = (("x-ui", "XUI_VERSION", "03-3xui"), ("xray", "XUI_XRAY_VERSION", "03-3xui"),
             ("hysteria", "HY2_VERSION", "05-hysteria2"))
    out: dict[str, dict[str, Any]] = {}
    for name, key, phase in pairs:
        inst, pin = installed.get(name, ""), pinned.get(key, "")
        outdated = None if not inst or not pin else _norm(inst) != _norm(pin)
        out[name] = {"installed": inst, "pinned": pin, "outdated": outdated, "phase": phase}
    # `awg --version` — версия amneziawg-tools («amneziawg-tools v3.1.20260812 - https://…»):
    # сравниваем с AWG_TOOLS_REF; amneziawg-go свою версию не сообщает — только versions.env
    m = re.search(r"\bv?\d+(?:\.\d+)+\b", installed.get("awg", ""))
    tools, tools_pin = (m.group(0) if m else installed.get("awg", "")), pinned.get("AWG_TOOLS_REF", "")
    if tools or tools_pin:
        out["amneziawg-tools"] = {"installed": tools, "pinned": tools_pin, "phase": "06-amneziawg",
                                  "outdated": None if not tools or not tools_pin else _norm(tools) != _norm(tools_pin)}
    out["amneziawg-go"] = {"installed": "", "pinned": pinned.get("AWG_GO_REF", ""), "outdated": None,
                           "phase": "06-amneziawg"}
    return out


def _tree_digest(root: Path, subdirs: tuple[str, ...] = ("zoo", "scripts")) -> dict[str, str]:
    out: dict[str, str] = {}
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(root).as_posix()
            if not f.is_file() or "__pycache__" in rel or rel.endswith(".pyc") or rel.startswith("zoo/tests/"):
                continue
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def changed_files(repo: Path, installed_home: Path) -> list[str]:
    a, b = _tree_digest(repo), _tree_digest(installed_home)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def plan_phases(comp: dict[str, dict[str, Any]], version_diff: list[str], changed: list[str]) -> list[str]:
    phases = {c["phase"] for c in comp.values() if c["outdated"]}
    for key in version_diff:
        phases |= {ph for prefix, ph in VERSION_PHASES if key.startswith(prefix)}
    if phases or changed:
        phases.add("09-zoo")  # обновить копию /opt/vpn-zoo
    return [p for p in PHASE_ORDER if p in phases]


def check(cfg: Config, repo: str | None = None, fetch: bool = False) -> dict[str, Any]:
    """Сводка для `zoo upgrade`: компоненты, репо, план фаз."""
    rdir = repo_dir(repo)
    installed_scripts = paths.zoo_home() / "scripts"
    pinned_file = (rdir / "scripts" / "versions.env") if rdir else installed_scripts / "versions.env"
    pinned = read_versions(pinned_file)
    comp = components(system.component_versions(), pinned)
    data: dict[str, Any] = {"repo": str(rdir) if rdir else None, "versions_file": str(pinned_file),
                            "install": install_info(), "components": comp, "version_diff": [], "changed": []}
    if rdir:
        data["git"] = git_state(rdir, fetch)
        old = read_versions(installed_scripts / "versions.env")
        if old:
            data["version_diff"] = sorted(k for k in set(old) | set(pinned) if old.get(k) != pinned.get(k))
        if paths.zoo_home().is_dir():
            data["changed"] = changed_files(rdir, paths.zoo_home())
    data["phases"] = plan_phases(comp, data["version_diff"], data["changed"])
    return data


def _render_check(d: dict[str, Any]) -> None:
    rows = []
    for name, c in d["components"].items():
        state = "—" if c["outdated"] is None else ("УСТАРЕЛ" if c["outdated"] else "ok")
        rows.append([name, c["installed"] or "—", c["pinned"] or "—", state])
    print(output.table(rows, ["компонент", "установлен", "закреплён", ""]))
    print(f"\nversions.env: {d['versions_file']}")
    if d["repo"]:
        g = d.get("git") or {}
        if g.get("git"):
            behind = g.get("behind")
            extra = "" if behind is None else (f", отстаёт от upstream на {behind}" if behind else ", актуальна")
            print(f"репо: {d['repo']} ({g['branch']} {g['rev']}{', есть правки' if g['dirty'] else ''}{extra})")
            if g.get("fetch_error"):
                output.warn(f"git fetch: {g['fetch_error']}")
        else:
            print(f"репо: {d['repo']} (не git)")
    else:
        output.warn("рабочая копия репо не найдена (INSTALL.json → source); укажи --repo DIR")
    if d["version_diff"]:
        print("versions.env отличается от установленной копии: " + ", ".join(d["version_diff"]))
    if d["changed"]:
        print(f"файлов zoo/scripts изменилось с установки: {len(d['changed'])}")
    if d["phases"]:
        print("\nПлан: " + " → ".join(d["phases"]))
        print("Применить: zoo upgrade --apply" + ("" if d["repo"] else " --repo DIR"))
    else:
        print()
        output.ok("всё актуально")


# ---------- upgrade: применение ----------

def _run_phase(repo: Path, phase: str) -> int:
    """install.sh --phase: вывод идёт прямо в терминал (и в лог /var/log/vpn-zoo)."""
    sys.stdout.flush()
    env = dict(os.environ, ZOO_CALLER="zoo-upgrade")
    try:
        return subprocess.run([protolib.bash_path(), str(repo / "scripts" / "install.sh"), "--phase", phase],
                              cwd=str(repo), env=env, stdin=subprocess.DEVNULL).returncode
    except OSError as e:
        output.error(f"{phase}: {e}")
        return 127


def _latest_backup() -> str | None:
    root = Path("/var/backups/vpn-setup")
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    except OSError:
        return None
    return str(dirs[-1]) if dirs else None


def rollback_hint(repo: Path, old_rev: str | None, phases: list[str]) -> list[str]:
    hints = []
    if old_rev:
        hints.append(f"git -C {repo} checkout {old_rev}   # вернуть прежние versions.env и скрипты")
    for ph in phases:
        hints.append(f"sudo bash {repo}/scripts/install.sh --phase {ph}")
    b = _latest_backup()
    if b:
        hints.append(f"бэкапы перезаписанных файлов: {b}")
    return hints


def apply(cfg: Config, repo: str | None = None, pull: bool = False, phases: list[str] | None = None,
          with_smoke: bool = True, verbose: bool = True) -> dict[str, Any]:
    show = _render_smoke if verbose else (lambda _d: None)
    rdir = repo_dir(repo)
    if rdir is None:
        raise RuntimeError("рабочая копия репо не найдена: укажи --repo DIR (каталог с scripts/install.sh)")
    result: dict[str, Any] = {"repo": str(rdir), "pulled": False, "phases": [], "ok": True}
    g = git_state(rdir)
    old_rev = g.get("rev") if g.get("git") else None
    result["old_rev"] = old_rev
    if pull:
        if not g.get("git"):
            raise RuntimeError(f"{rdir} — не git-репозиторий, --pull невозможен")
        if g.get("dirty"):
            raise RuntimeError(f"в {rdir} есть незакоммиченные правки — git pull не делаю")
        output.info(f"git pull в {rdir}")
        rc, msg = _git(rdir, "pull", "--ff-only", timeout=300)
        if rc != 0:
            raise RuntimeError(f"git pull: {msg}")
        result["pulled"] = True
        result["new_rev"] = _git(rdir, "rev-parse", "--short", "HEAD")[1]
    plan = phases or check(cfg, str(rdir))["phases"]
    result["plan"] = plan
    if not plan:
        output.ok("обновлять нечего")
        return result
    if with_smoke:
        output.info("smoke до обновления")
        result["smoke_before"] = smoke(cfg, with_probe=False)
        show(result["smoke_before"])
    for ph in plan:
        output.info(f"фаза {ph}")
        rc = _run_phase(rdir, ph)
        result["phases"].append({"phase": ph, "rc": rc})
        if rc != 0:
            result["ok"] = False
            output.error(f"{ph}: ошибка (rc={rc}), дальше не иду")
            break
    if with_smoke:
        output.info("smoke после обновления")
        result["smoke_after"] = smoke(cfg)
        show(result["smoke_after"])
        before = {c["name"] for c in result["smoke_before"]["checks"] if c["ok"] is False}
        after = {c["name"] for c in result["smoke_after"]["checks"] if c["ok"] is False}
        result["regressions"] = sorted(after - before)
        if result["regressions"]:
            result["ok"] = False
    if not result["ok"]:
        result["rollback"] = rollback_hint(rdir, old_rev if result["pulled"] else None,
                                           [p["phase"] for p in result["phases"]])
    return result


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--check", action="store_true", help="только показать план (по умолчанию)")
    p.add_argument("--apply", action="store_true", help="выполнить план: фазы install.sh, smoke до и после")
    p.add_argument("--pull", action="store_true", help="перед планом: git pull --ff-only в рабочей копии")
    p.add_argument("--fetch", action="store_true", help="git fetch, чтобы показать отставание от upstream")
    p.add_argument("--phase", action="append", metavar="ФАЗА", help="перезапустить именно эту фазу")
    p.add_argument("--repo", metavar="DIR", help="рабочая копия репо (по умолчанию из INSTALL.json)")
    p.add_argument("--no-smoke", action="store_true", help="без smoke до и после")


def cmd_upgrade(args: argparse.Namespace, cfg: Config) -> int:
    if not args.apply:
        if args.pull:
            output.error("--pull меняет рабочую копию: используй вместе с --apply")
            return 2
        data = check(cfg, args.repo, fetch=args.fetch)
        output.emit(data, args.json, _render_check)
        return 0
    try:
        res = apply(cfg, args.repo, pull=args.pull, phases=args.phase, with_smoke=not args.no_smoke,
                    verbose=not args.json)
    except RuntimeError as e:
        output.error(str(e))
        return 1
    if args.json:
        output.print_json(res)
    elif res.get("plan"):
        if res["ok"]:
            output.ok("обновление завершено: " + " → ".join(res["plan"]))
        else:
            if res.get("regressions"):
                output.error("после обновления сломалось: " + ", ".join(res["regressions"]))
            output.warn("откат:")
            for h in res.get("rollback", []):
                print(f"  {h}")
    return 0 if res["ok"] else 1
