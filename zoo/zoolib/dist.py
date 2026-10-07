"""Дистрибутивы клиентов для групп, где приложения ставит ИТ (groups.install_mode = "admin").

`zoo clients --fetch-dist` (его запускает zoo-clients.service: раз в сутки и по заявке «Обновить» с
админки, файл dist-req → zoo-clients.path) берёт последний релиз GitHub каждого клиента, выбранного хотя бы
в одной такой группе, и кладёт подходящий файл в /var/lib/vpn-zoo/dist/<клиент>/<версия>/:
Android — APK (универсальный, иначе arm64), Windows — установщик x64 (иначе архив), macOS и Linux — если
устройство есть в группе. В каталоге версии лежит meta.json: имя, платформа, размер, sha256 (digest из
GitHub API, если он есть и сошёлся, иначе посчитанный при скачивании). Хранятся две последние версии
клиента; общий размер не больше cap(): CAP и доля раздела в бюджете данных (в бюджете данных это раздел «Дистрибутивы», storage.py).
Админка только читает каталог и отдаёт файлы залогиненному администратору (/dist/…), в сеть не ходит."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import clients as clientcat
from . import paths
from .fsutil import atomic_write_json

DIR_NAME = "dist"
META = "meta.json"
STATUS = "dist-status.json"
KEEP_VERSIONS = 2
CAP = 300 << 20
MAX_FILE = 250 << 20
TIMEOUT = 30
CHUNK = 1 << 20
MAX_BODY = 4 << 20
PLATFORMS = ("android", "windows", "macos", "linux")
REQ_NAME = "dist-req"
RATE_LIMIT = 120
REQ_STALE = 900
CID_RE = re.compile(r"[a-z0-9-]{1,40}")
VER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
SKIP_RE = re.compile(r"\.(sha\d*|sig|asc|txt|json|md5|pem|ya?ml|blockmap|sbom|zsync)$|checksum|sha256sum|dsym|symbols|debug",
                     re.I)
BAD_ARCH = re.compile(r"arm|aarch|x86(?!_?64)|i[3-6]86|[-_.]386|ia32|win32|mips|riscv|[-_.]32(?:[-_.]|bit|$)", re.I)
URL_PREFIX = "https://github.com/{repo}/releases/download/"


class DistError(Exception):
    pass


def root() -> Path:
    return paths.state_dir() / DIR_NAME


# ---------- выбор файла из релиза ----------

def _score(platform: str, name: str) -> int | None:
    """Насколько файл подходит платформе (меньше — лучше); None — не подходит."""
    if SKIP_RE.search(name):
        return None
    low = name.lower()
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    if platform == "android":
        if ext != "apk" or re.search(r"x86|armeabi|armv7|arm32|mips", low):
            return None
        base = 0 if "universal" in low else 1 if re.search(r"arm64|v8a", low) else 2
        # «android11+» не ставится на Android 9–10: при выборе — сборка без нижней планки версии
        return base * 2 + (1 if re.search(r"android[-_]?\d+\+", low) else 0)
    if platform == "windows":
        if ext not in ("exe", "msi", "zip") or not re.search(r"(?<![a-z])win", low) or BAD_ARCH.search(low):
            return None
        if ext == "zip":
            return 3
        return 0 if re.search(r"setup|install", low) else 1
    if platform == "macos":
        if ext not in ("dmg", "pkg") or not re.search(r"mac|osx|darwin", low):
            return None
        return 0 if "universal" in low else 1 if re.search(r"arm64|aarch|apple", low) else 2
    if platform == "linux":
        # v2rayN называет x64 просто «linux-64»
        if not re.search(r"amd64|x86_64|x64|[-_]64(?=[-_.])", low) or re.search(r"arm|aarch|loong|riscv", low):
            return None
        return {"appimage": 0, "deb": 1, "run": 2}.get(ext)
    return None


def pick_asset(platform: str, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Файл релиза под платформу: APK универсальный или arm64, установщик Windows x64 (иначе архив), dmg/pkg,
    AppImage/deb x64. Подходящего нет — None: админке покажут ссылку на GitHub."""
    scored = [(s, a["name"], a) for a in assets if isinstance(a.get("name"), str)
              and (s := _score(platform, a["name"])) is not None]
    return min(scored, key=lambda x: (x[0], len(x[1]), x[1]))[2] if scored else None


# ---------- GitHub ----------

def fetch_release(repo: str, timeout: float = clientcat.TIMEOUT) -> dict[str, Any]:
    """Последний релиз с файлами: {tag, version, published, assets: [{name, size, url, digest}]}."""
    if not clientcat.REPO_RE.fullmatch(repo):
        raise DistError(f"плохой репозиторий {repo!r}")
    req = urllib.request.Request(clientcat.API.format(repo=repo), headers={
        "Accept": "application/vnd.github+json", "User-Agent": "vpn-zoo-clients"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(MAX_BODY).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise DistError(f"HTTP {e.code}" + (" (лимит GitHub)" if e.code == 403 else "")) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DistError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
    tag = str(data.get("tag_name", "")) if isinstance(data, dict) else ""
    if not clientcat.TAG_RE.fullmatch(tag):
        raise DistError("в ответе нет tag_name")
    prefix = URL_PREFIX.format(repo=repo)
    assets = []
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        url = str(a.get("browser_download_url", ""))
        size = a.get("size")
        if url.startswith(prefix) and isinstance(size, int) and size > 0:
            m = DIGEST_RE.fullmatch(str(a.get("digest") or ""))
            assets.append({"name": str(a.get("name", "")), "size": size, "url": url, "digest": m.group(1) if m else ""})
    return {"tag": tag, "version": clientcat.clean_tag(tag), "published": str(data.get("published_at", ""))[:10],
            "assets": assets}


def download(url: str, dest: Path, size: int, timeout: float = TIMEOUT) -> str:
    """Скачать url в dest (через .part), → sha256. Размер должен сойтись с объявленным и не превышать MAX_FILE."""
    if size > MAX_FILE:
        raise DistError(f"файл {size >> 20} МБ больше лимита {MAX_FILE >> 20} МБ")
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": "vpn-zoo-clients"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as out:
            while chunk := r.read(CHUNK):
                got += len(chunk)
                if got > size or got > MAX_FILE:
                    raise DistError("файл больше объявленного размера")
                h.update(chunk)
                out.write(chunk)
        if got != size:
            raise DistError(f"скачано {got} из {size} байт")
        os.replace(part, dest)
    except (urllib.error.URLError, OSError) as e:
        raise DistError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
    finally:
        try:
            part.unlink()
        except OSError:
            pass
    return h.hexdigest()


# ---------- что нужно скачать ----------

def needed(gs: Any, cat: clientcat.Catalog) -> dict[str, list[str]]:
    """{клиент: [платформы]} по группам с install_mode = admin: клиент из набора устройства группы, у которого
    есть репозиторий GitHub и ссылка на GitHub для этой платформы."""
    out: dict[str, list[str]] = {}
    for g in gs.groups:
        if g.install_mode != "admin":
            continue
        for plat, ids in g.clients.items():
            for cid in ids:
                c = cat.client(cid)
                if c and plat in PLATFORMS and github_link(c, plat) and plat not in out.setdefault(cid, []):
                    out[cid].append(plat)
    return {cid: plats for cid, plats in out.items() if plats}


def github_link(client: dict[str, Any], plat: str) -> bool:
    return bool(client.get("repo")) and any(ln["kind"] == "github" for ln in client["platforms"].get(plat, []))


# ---------- каталог на диске ----------

def _clean_version(v: str) -> str:
    v = re.sub(r"[^A-Za-z0-9._+-]", "_", v)[:64]
    return v if VER_RE.fullmatch(v) else "v" + v.lstrip("._+-")[:60]


def load_meta(vdir: Path) -> dict[str, Any] | None:
    try:
        d = json.loads((vdir / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("files"), list):
        return None
    files = [f for f in d["files"] if isinstance(f, dict) and NAME_RE.fullmatch(str(f.get("name", "")))
             and isinstance(f.get("size"), int) and re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", "")))
             and f.get("platform") in PLATFORMS]
    return {**d, "files": files}


def versions(cid: str) -> list[tuple[Path, dict[str, Any]]]:
    """Версии клиента на диске, свежие первыми (по времени скачивания): каталог и meta."""
    base = root() / cid
    out = []
    try:
        for d in base.iterdir():
            if d.is_dir() and not d.is_symlink() and VER_RE.fullmatch(d.name):
                m = load_meta(d)
                if m is not None:
                    out.append((d, m))
    except OSError:
        return []
    return sorted(out, key=lambda x: (x[1].get("fetched") or 0, x[0].name), reverse=True)


def cap() -> int:
    """Сколько держать: CAP, но не больше доли «Дистрибутивы» в бюджете данных (storage). Иначе чистка по бюджету
    удаляла бы скачанное, а zoo-clients качал бы его снова — каждый день."""
    from . import config, storage
    try:
        share = storage.budget(config.load())["limit"] * storage.SHARES["dist"] // 100
    except (OSError, config.ConfigError):
        return CAP
    return min(CAP, share)


def total_size() -> int:
    n = 0
    try:
        for f in root().rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    n += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return n


def newest_per_platform(have: list[tuple[Path, dict[str, Any]]]) -> set[Path]:
    """Версии (из versions(), свежие первыми), где лежит самый свежий файл хоть одной платформы: в новой версии мог
    не скачаться файл Windows — тогда он есть только в прежней. Самая свежая версия входит всегда."""
    out: set[Path] = set()
    seen: set[str] = set()
    for d, meta in have:
        plats = {str(f.get("platform")) for f in meta.get("files", []) if isinstance(f, dict)}
        if plats - seen or not out:
            out.add(d)
        seen |= plats
    return out


def prune(cid: str, keep: int = KEEP_VERSIONS) -> int:
    """Оставить keep последних версий клиента и последние по каждой платформе, остальное (и каталоги без
    meta.json) удалить."""
    base = root() / cid
    have = versions(cid)
    kept = {d for d, _ in have[:keep]} | newest_per_platform(have)
    drop = [d for d, _ in have if d not in kept]
    try:
        keep_names = {d.name for d in kept}
        drop += [d for d in base.iterdir() if d.is_dir() and not d.is_symlink() and d.name not in keep_names
                 and d not in drop]
    except OSError:
        pass
    for d in drop:
        shutil.rmtree(d, ignore_errors=True)
    return len(drop)


def _prune_all(keep: int) -> None:
    try:
        for d in root().iterdir():
            if d.is_dir() and not d.is_symlink() and CID_RE.fullmatch(d.name):
                prune(d.name, keep)
    except OSError:
        pass


def status() -> dict[str, Any]:
    try:
        d = json.loads((paths.state_dir() / STATUS).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("errors"), dict):
            return {"checked": d.get("checked"), "errors": {str(k)[:60]: str(v)[:200] for k, v in d["errors"].items()}}
    except (OSError, ValueError):
        pass
    return {"checked": None, "errors": {}}


def fetch_all(cat: clientcat.Catalog | None = None, gs: Any = None,
              fetch: Callable[[str], dict[str, Any]] | None = None,
              dl: Callable[[str, Path, int], str] | None = None,
              now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Скачать то, что нужно группам «ставит ИТ». Ошибка клиента или файла не мешает остальным: попадает в
    errors (ключ «клиент» или «клиент/платформа»), прежние версии остаются. → {downloaded, kept, errors}."""
    from . import groups
    cat = cat or clientcat.load()
    gs = gs or groups.Groups.load()
    fetch = fetch or fetch_release
    dl = dl or download
    want = needed(gs, cat)
    limit = cap()
    res: dict[str, Any] = {"downloaded": [], "kept": [], "errors": {}}
    if want:
        root().mkdir(mode=0o755, parents=True, exist_ok=True)
    for cid, plats in want.items():
        c = cat.client(cid)
        if c is None:
            continue
        try:
            rel = fetch(c["repo"])
        except DistError as e:
            res["errors"][cid] = str(e)
            continue
        ver = _clean_version(rel["version"])
        vdir = root() / cid / ver
        meta = load_meta(vdir) or {"client": cid, "version": rel["version"], "published": rel.get("published", ""),
                                   "files": []}
        for plat in plats:
            key = f"{cid}/{plat}"
            asset = pick_asset(plat, rel["assets"])
            if asset is None:
                res["errors"][key] = "в релизе нет подходящего файла"
                continue
            name = asset["name"]
            if not NAME_RE.fullmatch(name):
                res["errors"][key] = f"странное имя файла {name[:40]!r}"
                continue
            if any(f["name"] == name and (vdir / name).is_file() for f in meta["files"]):
                res["kept"].append(key)
                continue
            if total_size() + asset["size"] > limit:
                _prune_all(1)
            if total_size() + asset["size"] > limit:
                res["errors"][key] = (f"не помещается в лимит {limit >> 20} МБ"
                                      + (" (доля дистрибутивов в бюджете данных, ZOO_DATA_LIMIT)" if limit < CAP else ""))
                continue
            vdir.mkdir(mode=0o755, parents=True, exist_ok=True)
            try:
                digest = dl(asset["url"], vdir / name, asset["size"])
            except DistError as e:
                res["errors"][key] = str(e)
                continue
            if asset["digest"] and asset["digest"] != digest:
                try:
                    (vdir / name).unlink()
                except OSError:
                    pass
                res["errors"][key] = "sha256 не совпал с указанным GitHub"
                continue
            meta["files"] = [f for f in meta["files"] if f["name"] != name] + [
                {"name": name, "platform": plat, "size": asset["size"], "sha256": digest,
                 "verified": bool(asset["digest"])}]
            meta["fetched"] = int(now())
            atomic_write_json(vdir / META, meta, 0o644)
            res["downloaded"].append(key)
        if (vdir / META).exists():
            prune(cid)
        else:
            shutil.rmtree(vdir, ignore_errors=True)
    atomic_write_json(paths.state_dir() / STATUS, {"checked": int(now()), "errors": res["errors"]}, 0o644)
    return res


# ---------- что видит админка ----------

def listing(gs: Any, cat: clientcat.Catalog, group_id: str | None = None) -> list[dict[str, Any]]:
    """Строки блока «Скачать дистрибутивы»: по клиенту и платформе групп «ставит ИТ» (group_id — одной группы).
    file — последняя версия с файлом под платформу ({name, size, sha256, verified, version, path}); нет файла —
    links (GitHub и магазин из каталога) и error (из последнего прогона)."""
    st = status()["errors"]
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for g in gs.groups:
        if g.install_mode != "admin" or (group_id and g.id != group_id):
            continue
        for plat, ids in g.clients.items():
            for cid in ids:
                c = cat.client(cid)
                if c is None or (cid, plat) in seen:
                    continue
                seen.add((cid, plat))
                found = None
                for _, m in versions(cid):
                    f = next((x for x in m["files"] if x["platform"] == plat), None)
                    if f:
                        found = {**f, "version": m.get("version", ""),
                                 "path": f"/dist/{cid}/{_clean_version(str(m.get('version', '')))}/{f['name']}"}
                        break
                rows.append({"client": c, "platform": plat, "file": found,
                             "links": c["platforms"].get(plat, []), "store": not github_link(c, plat),
                             "error": st.get(f"{cid}/{plat}") or st.get(cid)})
    return rows


def file_path(cid: str, ver: str, name: str) -> Path | None:
    """Файл для скачивания: только перечисленный в meta.json версии, обычный файл внутри каталога дистрибутивов
    (ни .., ни ссылок); иначе None."""
    if not (CID_RE.fullmatch(cid) and VER_RE.fullmatch(ver) and NAME_RE.fullmatch(name)):
        return None
    base = root()
    vdir = base / cid / ver
    try:
        if any(p.is_symlink() for p in (base, base / cid, vdir)):
            return None
        meta = load_meta(vdir)
        f = vdir / name
        if meta is None or name not in {x["name"] for x in meta["files"]} or f.is_symlink() or not f.is_file():
            return None
        if f.resolve().parent != vdir.resolve():
            return None
    except OSError:
        return None
    return f


# ---------- заявка «Обновить» ----------

def req_file() -> Path:
    return paths.state_dir() / REQ_NAME


def request_state(now: float | None = None) -> tuple[bool, str]:
    now = time.time() if now is None else now
    try:
        if now - req_file().stat().st_mtime < REQ_STALE:
            return False, "обновление уже заказано: файлы появятся через несколько минут"
    except OSError:
        pass
    last = status()["checked"]
    if isinstance(last, (int, float)) and 0 <= now - last < RATE_LIMIT:
        return False, f"обновляли меньше {RATE_LIMIT // 60} минут назад"
    return True, ""


def request_fetch(now: float | None = None) -> tuple[bool, str]:
    """Заказать скачивание (вызывает админка): файл-заявку забирает zoo-clients.path → zoo-clients.service."""
    now = time.time() if now is None else now
    ok, why = request_state(now)
    if not ok:
        return False, why
    f = req_file()
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    f.write_text(str(int(now)), encoding="utf-8")
    os.chmod(f, 0o600)
    os.utime(f, (now, now))
    return True, "обновление заказано: файлы появятся через несколько минут"


def clear_request() -> None:
    try:
        req_file().unlink()
    except OSError:
        pass
