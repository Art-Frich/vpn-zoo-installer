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
LINK_KINDS = {"github": "GitHub", "play": "Google Play", "appstore": "App Store", "msstore": "Microsoft Store",
              "fdroid": "F-Droid", "site": "сайт"}
IMPORT_METHODS = ("qr", "link", "file")
REQ_NAME = "clients-req"
RATE_LIMIT = 600   # «Проверить сейчас» — не чаще раза в 10 минут
REQ_STALE = 900    # заявку, которую никто не забрал за 15 минут, считаем потерянной


class ClientsError(Exception):
    pass


KEY_NOUNS = {"link": ("ссылку", "ссылки"), "file": ("файл", "файлы"), "qr": ("QR", "QR")}


def key_phrase(method: str, titles: list[str] | tuple[str, ...] = (), named: bool = True) -> str:
    """«ссылку «VLESS XHTTP»», «ссылки «VLESS XHTTP» и «VLESS Vision»»; без названий (named=False) — «ссылку», «ссылки»."""
    noun = KEY_NOUNS[method][len(titles) > 1]
    if not titles or not named:
        return noun
    quoted = [f"«{x}»" for x in titles]
    return noun + " " + (quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " и " + quoted[-1])


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

    def status(self, client: dict[str, Any], proto: str, platform: str) -> str:
        """Статус протокола у клиента на платформе: ok, warn, no, unk; нет записи — пусто. warn_on — платформы, где
        заявленное «ok» пока с оговоркой (не проверено на стенде)."""
        st = client["protocols"].get(proto) or {}
        s = st.get("s", "")
        return "warn" if s == "ok" and platform in st.get("warn_on", []) else s

    def per_app_steps(self, client: dict[str, Any], platform: str) -> str | None:
        """Шаг «приложения через VPN» для платформы; нет шага — на ней клиент так не умеет."""
        return (client.get("per_app_steps") or {}).get(platform)

    def via(self, client: dict[str, Any], platform: str) -> str:
        """Что идёт через VPN у клиента на платформе: apps, ru-direct, all; не описано — пусто."""
        return (client.get("via") or {}).get(platform, "")

    def admin_setup(self, client: dict[str, Any], platform: str) -> list[str]:
        """Шаги настройки, которым нужны права администратора компьютера (TUN в v2rayN)."""
        return list((client.get("admin_setup") or {}).get(platform, []))

    def config(self, client: dict[str, Any], platform: str) -> list[str]:
        """Разовая настройка приложения после импорта (режим, «РФ напрямую», ярлык): при «Ставит ИТ» её делает ИТ."""
        return list((client.get("config") or {}).get(platform, []))

    def setup(self, client: dict[str, Any], platform: str, admin: bool = False) -> list[str]:
        """Шаги настройки после импорта (TUN, маршрутизация, что знать). admin — приложения ставит ИТ: шаги с правами
        администратора и разовую настройку делает он (памятка у дистрибутивов), человеку их нет; иначе первый шаг с
        правами — с предупреждением."""
        rights = [] if admin else self.admin_setup(client, platform)
        if rights:
            rights[0] = self.raw["admin_rights"] + rights[0]
        return rights + ([] if admin else self.config(client, platform)) + list((client.get("setup") or {}).get(platform, []))

    def it_steps(self, client: dict[str, Any], platform: str, apps: str = "") -> list[str]:
        """Что делает ИТ при «Ставит ИТ» после установки: шаги с правами администратора, выбор приложений «через VPN» в
        самом приложении (apps — их названия) и разовая настройка."""
        out = self.admin_setup(client, platform)
        if step := self.per_app_steps(client, platform):
            out.append(f"Приложения через VPN в «{client['name']}»: " + step.replace("{apps}", apps or "приложения из списка"))
        return out + self.config(client, platform)

    def report(self, speed: str = "brave") -> str:
        """Что прислать администратору; speed — как мерить «медленно»: brave (VPN только в Brave), device (через VPN
        идёт всё), apps (Brave в списке нет), guide (общий документ)."""
        return self.raw["report"].replace("{speed}", self.raw["speed"][speed])

    def install_note(self, client: dict[str, Any], platform: str) -> str:
        """Что делать, если система не открывает приложение после установки (Gatekeeper на Mac); нет — пусто."""
        return (client.get("install_note") or {}).get(platform, "")

    def import_step(self, client: dict[str, Any], method: str, titles: list[str] | tuple[str, ...] = (),
                    other: bool = False, named: bool = True) -> str:
        """Шаг импорта: «Скопируйте ссылку «VLESS XHTTP» из сообщения…»; titles — названия ключей, как они подписаны
        у человека (named=False — без названий: «ссылку»); other — QR с другого экрана («Открываете сообщение на другом
        экране — …»)."""
        text = client["import"][method].replace("{key}", key_phrase(method, titles, named))
        return self.raw["other_screen"] + text[:1].lower() + text[1:] if other else text

    def both_step(self, titles: list[str]) -> str:
        """Ключей у приложения несколько: какой включать, какой запасной (названия — как подписаны у человека)."""
        q = [f"«{x}»" for x in titles]
        return self.raw["both"].replace("{first}", q[0]).replace("{rest}", " или ".join(q[1:]))

    def steps(self, client: dict[str, Any], platform: str, imports: list[tuple[str, list[str]]], apps: str = "",
              extras: Callable[[str], bool] = lambda proto: True, alt_qr: list[str] | None = None,
              named: bool = True, one_way: bool = True, admin: bool = False) -> list[str]:
        """Шаги одного приложения после установки: импорт ключа ([(способ, названия ключей)]) и в том же шаге — QR с
        другого экрана как другой способ (alt_qr — названия; None — не предлагать), файлы-дополнения (extras — какие
        протоколы-файлы есть у человека), выбор приложений «через VPN» (apps — их названия), настройка. named —
        называть ключи (на устройстве два приложения: какой ключ в какое); ключей у приложения несколько — названы
        всегда, и в том же шаге — какой включать (отдельным шагом он читался как повтор импорта). one_way — сказать
        «один способ из двух» (у второго приложения не повторяется). admin — ставит ИТ: шагов с правами администратора нет."""
        keys = list(dict.fromkeys(x for _, ts in imports for x in ts))
        named = named or len(keys) > 1
        out = [self.import_step(client, m, ts, named=named) for m, ts in imports]
        if alt_qr is not None:
            alt = self.import_step(client, "qr", alt_qr, other=True, named=named)
            if out:   # другой способ, а не следующий шаг: иначе человек сделает оба и получит два подключения
                out[-1] = f"{out[-1]} {alt}" + (f" {self.raw['one_way']}" if one_way else "")
            else:
                out.append(alt)
        if len(keys) > 1 and out:
            out[-1] = f"{out[-1]} {self.both_step(keys)}"
        out += [ex["text"] for ex in client.get("extra", []) if ex["platform"] == platform and extras(ex["proto"])]
        if (step := self.per_app_steps(client, platform)) and not admin:   # при «Ставит ИТ» выбирает ИТ (it_steps)
            out.append(f"Приложения через VPN в «{client['name']}»: " + step.replace("{apps}", apps or "нужные приложения"))
        return out + self.setup(client, platform, admin)

    def check(self, via: str, brave: bool, first_app: str = "") -> str:
        """Шаг проверки: в Brave (он в списке), в первом приложении списка или на любом сайте (через VPN идёт всё)."""
        chk = self.raw["check"]
        if via == "brave":
            return chk["brave"]
        if via == "apps" and (brave or not first_app):
            return chk["brave"]
        if via == "apps":
            return chk["apps"].replace("{app}", first_app)
        return chk["device"]

    def real_protocols(self) -> list[str]:
        return [p for p, d in self.protocols.items() if not d.get("pseudo")]

    def names_for(self, proto: str) -> str:
        """Приложения протокола через запятую (подсказка на плитке): рекомендованные первыми; без import (ссылок не берут) — нет."""
        rec = [plat[proto] for plat in self.raw["recommended"].values() if proto in plat]
        ok = [c["id"] for c in self.clients if c.get("import") and c["protocols"].get(proto, {}).get("s") in ("ok", "warn")]
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
            need(set(st.get("warn_on", [])) <= set(c["platforms"]), f"{cid}: протокол {pid}: warn_on")
        need(set(c.get("import", {})) <= set(IMPORT_METHODS) and all("{key}" in v for v in c.get("import", {}).values()),
             f"{cid}: import — способы {IMPORT_METHODS}, в тексте {{key}}")
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
        via = c.get("via", {})
        need(isinstance(via, dict) and set(via) <= set(c["platforms"]) and all(m in raw.get("via", {}) for m in via.values()),
             f"{cid}: via — платформа клиента → ключ справочника via")
        for key in ("setup", "config"):
            steps_of = c.get(key, {})
            need(isinstance(steps_of, dict) and set(steps_of) <= set(c["platforms"])
                 and all(isinstance(v, list) and all(isinstance(s, str) and s for s in v) for v in steps_of.values()),
                 f"{cid}: {key} — платформа клиента → список шагов")
        asset = c.get("asset", {})
        need(isinstance(asset, dict) and set(asset) <= set(c["platforms"]), f"{cid}: asset")
        rights = c.get("admin_setup", {})
        need(isinstance(rights, dict) and set(rights) <= set(c["platforms"])
             and all(isinstance(v, list) and all(isinstance(s, str) and s for s in v) for v in rights.values()),
             f"{cid}: admin_setup — платформа клиента → список шагов")
        note = c.get("install_note", {})
        need(isinstance(note, dict) and set(note) <= set(c["platforms"]) and all(isinstance(v, str) and v for v in note.values()),
             f"{cid}: install_note — платформа клиента → текст")
    for plat, by_proto in raw["recommended"].items():
        need(plat in plats, f"recommended: платформа {plat}")
        for pid, cid in by_proto.items():
            c = next((x for x in raw["clients"] if x["id"] == cid), None)
            need(c is not None and plat in c["platforms"], f"recommended {plat}/{pid}: клиент {cid} без этой платформы")
            need(c["protocols"].get(pid, {}).get("s") in ("ok", "warn"), f"recommended {plat}/{pid}: {cid} не поддерживает")
    for plat, order in raw["handoff"].items():
        need(plat in plats and all(p in protos for p in order), f"handoff {plat}")
    need(set(raw.get("check", {})) >= {"brave", "apps", "device"} and "{app}" in raw["check"]["apps"],
         "check: нужны brave, apps (с {app}) и device")
    need(isinstance(raw.get("report"), str) and "{speed}" in raw["report"], "report: что прислать, если не работает, с {speed}")
    need(isinstance(raw.get("speed"), dict) and set(raw["speed"]) >= {"brave", "device", "apps", "guide"},
         "speed: brave, device, apps, guide")
    need(isinstance(raw.get("other_screen"), str) and bool(raw["other_screen"]), "other_screen: QR с другого экрана")
    need(isinstance(raw.get("one_way"), str) and bool(raw["one_way"]), "one_way: один способ импорта из двух")
    need(isinstance(raw.get("both"), str) and "{first}" in raw["both"] and "{rest}" in raw["both"],
         "both: нужны {first} и {rest}")
    need(isinstance(raw.get("one_on"), str) and "{first}" in raw["one_on"] and "{rest}" in raw["one_on"],
         "one_on: нужны {first} и {rest}")
    need(isinstance(raw.get("fallback"), str) and "{first}" in raw["fallback"] and "{rest}" in raw["fallback"],
         "fallback: нужны {first} и {rest}")
    need(isinstance(raw.get("admin_rights"), str) and bool(raw["admin_rights"]), "admin_rights: нужны права администратора")
    upd = raw.get("update")
    need(isinstance(upd, dict) and set(upd) == {"keys", "all", "lost", "add", "drop", "app", "switch"}
         and all("{apps}" in upd[k] for k in ("keys", "all", "add")) and "{keys}" in upd["add"]
         and "{device}" in upd["lost"] and "{old}" in upd["drop"] and "{new}" in upd["app"]
         and "{old}" in upd["switch"] and "{new}" in upd["switch"],
         "update: keys, all, add с {apps}; add с {keys}; lost с {device}; drop, app, switch с {old}/{new}")
    lists = raw.get("lists")
    need(isinstance(lists, dict) and set(lists) == {"head", "file", "rules", "manual"} and "{name}" in lists["head"]
         and "{file}" in lists["file"] and "{step}" in lists["rules"] and "{step}" in lists["manual"],
         "lists: head ({name}), file ({file}), rules и manual ({step})")
    for key in ("brave", "rules"):
        need(isinstance(raw.get(key, {}), dict) and set(raw.get(key, {})) <= set(plats), f"{key}: платформа → текст")
    need(all(c.get("per_app") in raw.get("per_app", {}) for c in raw["clients"]), "per_app клиента не из справочника")
    for plat, g in raw.get("guide", {}).items():
        need(plat in plats and isinstance(g, dict) and bool(g.get("apps"))
             and all(any(x["id"] == i and plat in x["platforms"] and x.get("import") for x in raw["clients"]) for i in g["apps"]),
             f"guide {plat}: нужен список apps из клиентов этой платформы, которые берут ключи")
    for pr in raw.get("presets", []):
        need(pr.get("id") in ("simple", "reliable") and pr.get("protocols") and all(p in protos for p in pr["protocols"]),
             f"presets: {pr.get('id')}")
        for plat, ids in pr.get("plan", {}).items():
            need(plat in plats and all(any(x["id"] == i and plat in x["platforms"] for x in raw["clients"]) for i in ids),
                 f"presets {pr['id']}/{plat}: клиент без этой платформы")


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
