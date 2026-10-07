"""Каталог клиентских приложений (zoo/data/clients.json) и последние версии из GitHub.

`zoo clients` — таблица клиентов и версий из кэша; `zoo clients --check-upstream` — опрос
GitHub releases/latest (10 с на запрос, без токена), результат — в кэш
/var/lib/vpn-zoo/clients-versions.json. Опрос запускает суточный таймер zoo-clients.timer
и заявка «Проверить сейчас» со страницы «Клиенты» (файл clients-req → zoo-clients.path);
страницы админки читают только кэш и в сеть не ходят. `--fetch-dist` — скачать дистрибутивы (APK, установщики)
клиентов, выбранных в группах «ставит ИТ» (zoolib/dist.py); его тоже запускает zoo-clients.service.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import output, paths
from .config import Config
from .fsutil import atomic_write_json

CATALOG_FILE = paths.ZOO_PKG_ROOT / "data" / "clients.json"
CACHE_NAME = "clients-versions.json"
API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 10
MAX_BODY = 1 << 20
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TAG_RE = re.compile(r"[A-Za-z0-9._+/-]{1,64}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
STATUSES = ("ok", "warn", "no", "unk")
STATUS_MARK = {"ok": "✓", "warn": "!", "no": "✕", "unk": "?"}
STATUS_TEXT = {"ok": "заявлено поддерживаемым", "warn": "с оговоркой", "no": "не работает", "unk": "не проверено"}
LINK_KINDS = {"github": "GitHub", "play": "Google Play", "appstore": "App Store", "fdroid": "F-Droid", "site": "сайт"}
IMPORT_METHODS = ("qr", "link", "file")
REQ_NAME = "clients-req"
RATE_LIMIT = 600   # «Проверить сейчас» — не чаще раза в 10 минут
REQ_STALE = 900    # заявку, которую никто не забрал за 15 минут, считаем потерянной


class ClientsError(Exception):
    pass


@dataclass
class Catalog:
    raw: dict[str, Any]

    @property
    def platforms(self) -> dict[str, str]:
        return self.raw["platforms"]

    @property
    def protocols(self) -> dict[str, dict[str, Any]]:
        return self.raw["protocols"]

    @property
    def clients(self) -> list[dict[str, Any]]:
        return self.raw["clients"]

    def client(self, cid: str) -> dict[str, Any] | None:
        return next((c for c in self.clients if c["id"] == cid), None)

    def recommended(self, platform: str, proto: str) -> dict[str, Any] | None:
        cid = self.raw["recommended"].get(platform, {}).get(proto)
        return self.client(cid) if cid else None

    def no_ru_store(self, client: dict[str, Any], platform: str) -> bool:
        """Клиента нет в российском магазине платформы: нужен аккаунт другой страны."""
        return platform in client.get("no_ru_store", [])

    def per_app_steps(self, client: dict[str, Any], platform: str) -> str | None:
        """Шаг «приложения через VPN» для платформы; нет шага — на ней клиент так не умеет."""
        return (client.get("per_app_steps") or {}).get(platform)

    def real_protocols(self) -> list[str]:
        return [p for p, d in self.protocols.items() if not d.get("pseudo")]

    def names_for(self, proto: str) -> str:
        """Клиенты протокола через запятую (подсказка на плитке): рекомендованные первыми."""
        rec = [plat[proto] for plat in self.raw["recommended"].values() if proto in plat]
        ok = [c["id"] for c in self.clients if c["protocols"].get(proto, {}).get("s") in ("ok", "warn")]
        order = list(dict.fromkeys(rec + ok))
        return ", ".join(self.client(i)["name"] for i in order)  # type: ignore[index]


def validate(raw: Any) -> None:
    """Целостность каталога: ссылки на клиентов, протоколы, платформы и формат ссылок."""
    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise ClientsError(f"clients.json: {msg}")

    need(isinstance(raw, dict), "корень — не объект")
    for key in ("platforms", "protocols", "clients", "recommended", "handoff"):
        need(key in raw, f"нет ключа {key}")
    plats, protos = raw["platforms"], raw["protocols"]
    ids: set[str] = set()
    for c in raw["clients"]:
        cid = c.get("id", "")
        need(bool(re.fullmatch(r"[a-z0-9-]+", cid)) and cid not in ids, f"id клиента: {cid!r}")
        ids.add(cid)
        need(bool(c.get("name")) and bool(c.get("platforms")), f"{cid}: нужны name и platforms")
        repo = c.get("repo")
        need(repo is None or bool(REPO_RE.fullmatch(repo)), f"{cid}: repo {repo!r}")
        for plat, links in c["platforms"].items():
            need(plat in plats, f"{cid}: платформа {plat}")
            need(bool(links), f"{cid}/{plat}: нет ссылок")
            for ln in links:
                need(ln.get("kind") in LINK_KINDS and str(ln.get("url", "")).startswith("https://")
                     and isinstance(ln.get("checked"), bool), f"{cid}/{plat}: ссылка {ln}")
        for pid, st in c["protocols"].items():
            need(pid in protos and st.get("s") in STATUSES, f"{cid}: протокол {pid} {st}")
        need(set(c.get("import", {})) <= set(IMPORT_METHODS), f"{cid}: import")
        steps = c.get("per_app_steps", {})
        need(isinstance(steps, dict) and set(steps) <= set(c["platforms"]) - {"ios"}
             and all(isinstance(v, str) and v for v in steps.values()),
             f"{cid}: per_app_steps — словарь платформа → текст, без iOS и только для платформ клиента")
        no_ru = c.get("no_ru_store", [])
        need(isinstance(no_ru, list) and set(no_ru) <= set(c["platforms"]), f"{cid}: no_ru_store")
        no_unify = c.get("no_unify", [])
        need(isinstance(no_unify, list) and set(no_unify) <= set(c["platforms"]), f"{cid}: no_unify")
        v = c.get("verified") or {}
        need(bool(DATE_RE.fullmatch(str(v.get("date", "")))) and isinstance(v.get("device"), bool),
             f"{cid}: verified")
    for plat, by_proto in raw["recommended"].items():
        need(plat in plats, f"recommended: платформа {plat}")
        for pid, cid in by_proto.items():
            c = next((x for x in raw["clients"] if x["id"] == cid), None)
            need(c is not None and plat in c["platforms"], f"recommended {plat}/{pid}: клиент {cid} без этой платформы")
            need(c["protocols"].get(pid, {}).get("s") in ("ok", "warn"), f"recommended {plat}/{pid}: {cid} не поддерживает")
    for plat, order in raw["handoff"].items():
        need(plat in plats and all(p in protos for p in order), f"handoff {plat}")
    need("*" in raw.get("check", {}), "check: нужен запасной текст «*»")
    need(all(c.get("per_app") in raw.get("per_app", {}) for c in raw["clients"]), "per_app клиента не из справочника")


def load(path: Path | None = None) -> Catalog:
    p = path or CATALOG_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ClientsError(f"каталог клиентов не прочитан ({p.name}): {e}") from e
    validate(raw)
    return Catalog(raw)


# ---------- версии из GitHub ----------

def cache_file() -> Path:
    return paths.state_dir() / CACHE_NAME


def load_cache() -> dict[str, Any]:
    """{checked: ts|None, versions: {id: {version, url, published, error?}}}; нет файла — пусто."""
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("versions"), dict):
            return {"checked": data.get("checked"), "versions": data["versions"]}
    except (OSError, ValueError):
        pass
    return {"checked": None, "versions": {}}


def req_file() -> Path:
    return paths.state_dir() / REQ_NAME


def check_state(now: float | None = None) -> tuple[bool, str]:
    """(можно ли заказать проверку версий, почему нет)."""
    now = time.time() if now is None else now
    try:
        if now - req_file().stat().st_mtime < REQ_STALE:
            return False, "проверка уже заказана: версии обновятся через минуту"
    except OSError:
        pass
    last = load_cache()["checked"]
    if isinstance(last, (int, float)) and 0 <= now - last < RATE_LIMIT:
        return False, f"версии проверяли меньше {RATE_LIMIT // 60} минут назад"
    return True, ""


def request_check(now: float | None = None) -> tuple[bool, str]:
    """Заказать опрос GitHub (вызывает админка): файл-заявку забирает zoo-clients.path → zoo-clients.service,
    у админки на это нет прав и сеть ей не нужна."""
    now = time.time() if now is None else now
    ok, why = check_state(now)
    if not ok:
        return False, why
    f = req_file()
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    f.write_text(str(int(now)), encoding="utf-8")
    os.chmod(f, 0o600)
    os.utime(f, (now, now))
    return True, "проверка заказана: версии обновятся через минуту"


def clear_request() -> None:
    try:
        req_file().unlink()
    except OSError:
        pass


def clean_tag(tag: str) -> str:
    """app/v2.12.3 → 2.12.3, v7.25.4 → 7.25.4."""
    tag = tag.rsplit("/", 1)[-1]
    return tag[1:] if re.fullmatch(r"[vV]\d.*", tag) else tag


def fetch_latest(repo: str, timeout: float = TIMEOUT) -> dict[str, str]:
    """Последний релиз репозитория (без пред-релизов). Исключения — ClientsError с причиной."""
    if not REPO_RE.fullmatch(repo):
        raise ClientsError(f"плохой репозиторий {repo!r}")
    req = urllib.request.Request(API.format(repo=repo), headers={
        "Accept": "application/vnd.github+json", "User-Agent": "vpn-zoo-clients"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(MAX_BODY).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise ClientsError(f"HTTP {e.code}" + (" (лимит GitHub)" if e.code == 403 else "")) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ClientsError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
    tag = str(data.get("tag_name", "")) if isinstance(data, dict) else ""
    if not TAG_RE.fullmatch(tag):
        raise ClientsError("в ответе нет tag_name")
    url = str(data.get("html_url", ""))
    return {"version": clean_tag(tag), "url": url if url.startswith("https://github.com/") else "",
            "published": str(data.get("published_at", ""))[:10]}


def check_upstream(cat: Catalog | None = None, fetch: Callable[[str], dict[str, str]] | None = None,
                   now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Опрос всех репозиториев каталога; запись в кэш. Ошибка клиента не затирает его прежнюю версию."""
    cat = cat or load()
    fetch = fetch or fetch_latest  # имя ищется при вызове: тесты подменяют fetch_latest
    old = load_cache()["versions"]
    versions: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for c in cat.clients:
        repo = c.get("repo")
        if not repo:
            continue
        try:
            versions[c["id"]] = fetch(repo)
        except ClientsError as e:
            errors[c["id"]] = str(e)
            versions[c["id"]] = {**old.get(c["id"], {}), "error": str(e)}
    result = {"checked": now(), "versions": versions}
    atomic_write_json(cache_file(), result, 0o644)
    return {**result, "errors": errors}


def version_of(cache: dict[str, Any], cid: str) -> str | None:
    v = cache["versions"].get(cid) or {}
    return v.get("version") or None


# ---------- команда ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--check-upstream", action="store_true",
                   help="спросить GitHub про последние версии (10 с на запрос, без токена) и записать в кэш")
    p.add_argument("--fetch-dist", action="store_true",
                   help="скачать дистрибутивы клиентов групп «ставит ИТ» в /var/lib/vpn-zoo/dist (две последние версии)")


def cmd_clients(args: argparse.Namespace, cfg: Config) -> int:
    if args.check_upstream:
        clear_request()  # до каталога: битый каталог не должен оставить заявку, которая гоняет юнит по кругу
    if args.fetch_dist:
        from . import dist
        dist.clear_request()
    try:
        cat = load()
    except ClientsError as e:
        output.error(str(e))
        return 1
    errors: dict[str, str] = {}
    if args.check_upstream:
        res = check_upstream(cat)
        errors = res["errors"]
        total = sum(1 for c in cat.clients if c.get("repo"))
        for cid, msg in errors.items():
            output.warn(f"{cid}: {msg}")
        (output.ok if not errors else output.warn)(f"версии: {total - len(errors)} из {total} обновлены")
    if args.fetch_dist and not args.check_upstream:
        return _fetch_dist(cat, args.json)
    cache = load_cache()
    rows = []
    for c in cat.clients:
        v = cache["versions"].get(c["id"]) or {}
        rows.append({"id": c["id"], "name": c["name"], "platforms": list(c["platforms"]),
                     "protocols": {p: s["s"] for p, s in c["protocols"].items() if p in cat.real_protocols()},
                     "version": v.get("version"), "error": v.get("error")})
    data = {"updated": cat.raw.get("updated"), "checked": cache["checked"], "clients": rows}
    output.emit(data, args.json, _render)
    total = sum(1 for c in cat.clients if c.get("repo"))
    code = 1 if args.check_upstream and total and len(errors) == total else 0
    if args.fetch_dist:   # сбой версий не мешает скачать файлы; итог юнита — худший из двух
        code = max(code, _fetch_dist(cat, False))
    return code


def _fetch_dist(cat: Catalog, as_json: bool) -> int:
    from . import dist, groups
    try:
        res = dist.fetch_all(cat, groups.Groups.load())
    except (groups.GroupError, OSError) as e:
        output.error(f"дистрибутивы: {e}")
        return 1
    for key, msg in res["errors"].items():
        output.warn(f"{key}: {msg}")
    if as_json:
        output.print_json(res)
    else:
        output.ok(f"дистрибутивы: скачано {len(res['downloaded'])}, уже есть {len(res['kept'])}, "
                  f"не удалось {len(res['errors'])}")
    return 1 if res["errors"] and not res["downloaded"] and not res["kept"] else 0


def _render(d: dict[str, Any]) -> None:
    rows = [[r["name"], ", ".join(r["platforms"]), r["version"] or "—",
             ", ".join(f"{p}{STATUS_MARK[s]}" for p, s in r["protocols"].items())] for r in d["clients"]]
    print(output.table(rows, ["клиент", "платформы", "версия", "протоколы (✓ да, ! оговорка, ✕ нет, ? не проверено)"]))
    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(d["checked"])) if d["checked"] else "не проверялись"
    print(f"\nВерсии: {when}. Каталог от {d['updated']}. Проверить: sudo zoo clients --check-upstream")
