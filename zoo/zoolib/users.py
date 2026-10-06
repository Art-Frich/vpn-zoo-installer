"""Пользователи зоопарка (ARCHITECTURE §5).

Реестр /etc/vpn-setup/users.json — источник правды zoo:
    {"schema": 1, "users": [{"name", "created", "enabled", "note", "protocols": [id, ...],
                             "group": id, "custom": true}]}
group — id группы (groups.json); custom — набор протоколов задан вручную, группа его не трогает.

Операции расходятся по всем включённым протоколам с пользователями через protolib.
При ошибке в одном протоколе изменения в остальных откатываются (partial=True — оставить
то, что получилось, и записать в реестр только успешные протоколы).

Служебный пользователь zoo-probe (system=true) — креды пробника: `zoo probe --local` и
`zoo export-probe` проверяют протоколы им, а не owner. Так проба не выбивает сессию
AmneziaWG у телефона владельца (роуминг WireGuard) и не попадает в трафик owner.
Он скрыт из списков и отчётов трафика, не удаляется и не отключается без --force.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import manifests, paths, protolib
from .fsutil import atomic_write_json, file_lock, read_json

SCHEMA = 1
OWNER = "owner"
PROBE_USER = "zoo-probe"
SYSTEM_USERS = frozenset({PROBE_USER})
PROBE_NOTE = "служебный: пробник (zoo probe --local, export-probe)"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class UserError(Exception):
    """Неверный ввод или состояние (нет пользователя, уже есть, нельзя удалить owner)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise UserError(
            f"недопустимое имя «{name}»: латиница в нижнем регистре, цифры, - и _, до 32 символов, "
            "начинается с буквы или цифры"
        )
    return name


@dataclass
class User:
    name: str
    created: str = ""
    enabled: bool = True
    note: str = ""
    protocols: list[str] = field(default_factory=list)
    system: bool = False
    group: str = ""
    custom: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "User":
        return cls(
            name=str(d["name"]),
            created=str(d.get("created", "")),
            enabled=bool(d.get("enabled", True)),
            note=str(d.get("note", "")),
            protocols=[str(x) for x in d.get("protocols", [])],
            system=bool(d.get("system", False)),
            group=str(d.get("group") or ""),
            custom=bool(d.get("custom", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        d = {"name": self.name, "created": self.created, "enabled": self.enabled,
             "note": self.note, "protocols": list(self.protocols)}
        if self.system:
            d["system"] = True
        if self.group:
            d["group"] = self.group
        if self.custom:
            d["custom"] = True
        return d


class Registry:
    def __init__(self, path: Path, users: list[User] | None = None, exists: bool = False) -> None:
        self.path = path
        self.users = users or []
        self.exists = exists

    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        path = Path(path) if path else paths.users_file()
        if not path.exists():
            return cls(path)
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise UserError(f"{path}: ожидается объект с массивом users")
        return cls(path, [User.from_dict(u) for u in data["users"]], exists=True)

    def save(self) -> None:
        atomic_write_json(self.path, {"schema": SCHEMA, "users": [u.to_dict() for u in self.users]})
        self.exists = True

    def get(self, name: str) -> User | None:
        return next((u for u in self.users if u.name == name), None)

    def require(self, name: str) -> User:
        u = self.get(name)
        if u is None:
            raise UserError(f"пользователя «{name}» нет (список: zoo user list)")
        return u

    def remove(self, name: str) -> None:
        self.users = [u for u in self.users if u.name != name]

    def names(self) -> list[str]:
        return [u.name for u in self.users]

    def visible(self) -> list[User]:
        """Пользователи без служебных (их не показывают списки и отчёты)."""
        return [u for u in self.users if not u.system]


@dataclass
class Step:
    proto_id: str
    action: str  # add | adopt | del | enable | disable | rollback | forget | skip
    ok: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"proto": self.proto_id, "action": self.action, "ok": self.ok, "error": self.error}


@dataclass
class OpReport:
    op: str
    user: str
    ok: bool = True
    steps: list[Step] = field(default_factory=list)
    rolled_back: bool = False
    message: str = ""
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "user": self.user, "ok": self.ok, "rolled_back": self.rolled_back,
                "message": self.message, "steps": [s.to_dict() for s in self.steps],
                "skipped": dict(self.skipped)}


# ---------- протоколы ----------

def shared_module(m: manifests.Manifest, libs: set[str]) -> str | None:
    """Модуль, с которым манифест делит пользователей: поле users_module или id-префикс
    (hysteria2-obfs → hysteria2: второй инстанс с тем же списком пользователей)."""
    explicit = m.raw.get("users_module")
    if isinstance(explicit, str) and explicit and explicit != m.id:
        return explicit
    for other in sorted(libs, key=len, reverse=True):
        if m.id.startswith(other + "-"):
            return other
    return None


def users_module(m: manifests.Manifest, libs: set[str]) -> str:
    """id, под которым пользователь записан в реестре: свой модуль или общий (hysteria2-obfs → hysteria2)."""
    return m.id if m.id in libs else (shared_module(m, libs) or m.id)


def managed_protocols(only: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Протоколы, в которых живут пользователи: включённый манифест + есть proto-<id>.sh.
    Манифесты, делящие пользователей с другим модулем, не считаются отдельно.

    Возвращает (ids, {id: причина пропуска}).
    """
    good, bad = manifests.load_all()
    libs = set(protolib.list_libs())
    ids: list[str] = []
    skipped: dict[str, str] = {b.id: "манифест негоден: " + "; ".join(b.errors) for b in bad}
    for m in good:
        if only and m.id not in only:
            continue
        if not m.enabled:
            skipped[m.id] = "выключен в манифесте"
        elif not m.has_users:
            skipped[m.id] = "без пользователей (users_backend=none)"
        elif m.id in libs:
            ids.append(m.id)
        elif shared_module(m, libs):
            continue
        else:
            skipped[m.id] = f"нет модуля {protolib.lib_path(m.id).name}"
    if only:
        for pid in only:
            if pid not in ids and pid not in skipped:
                skipped[pid] = "нет манифеста"
    return ids, skipped


def _present(pid: str, name: str) -> bool | None:
    """Есть ли пользователь в протоколе; None — модуль не смог ответить."""
    try:
        return name in protolib.user_list(pid)
    except protolib.ProtoError:
        return None


def _err(e: protolib.ProtoError) -> str:
    return e.short()


# ---------- операции ----------

def _lock():
    return file_lock(paths.lock_file(), timeout=120)


def _load_registry() -> Registry:
    reg = Registry.load()
    if not reg.exists:
        _bootstrap_into(reg, OWNER)
    return reg


def add_user(name: str, note: str = "", only: list[str] | None = None,
             partial: bool = False, system: bool = False, group: str | None = None) -> OpReport:
    """group: id или имя группы; None — «Основная», если она есть. Протоколы — группы, если
    only не задан; свой only делает набор «своим» (группа его не трогает)."""
    validate_name(name)
    if name in SYSTEM_USERS and not system:
        raise UserError(f"имя «{name}» зарезервировано за служебным пользователем пробника")
    with _lock():
        reg = _load_registry()
        if reg.get(name):
            raise UserError(f"пользователь «{name}» уже есть")
        grp, custom = None, False
        if not system:
            from . import groups
            gs = groups.Groups.load()
            grp = gs.require(group) if group else (gs.get(groups.MAIN_ID) if group is None else None)
            if grp is not None:
                if only:
                    custom = True
                else:
                    managed_all, _ = managed_protocols()
                    want = grp.resolve(managed_all)
                    if not want:
                        raise UserError(f"в группе «{grp.name}» нет включённых протоколов")
                    only = None if set(want) >= set(managed_all) else want
                groups.refresh_mirror(extra={name: grp.id})
        targets, skipped = managed_protocols(only)
        if not targets:
            raise UserError("нет протоколов, куда можно добавить пользователя: "
                            + ("; ".join(f"{k}: {v}" for k, v in skipped.items()) or "манифестов нет"))
        rep = OpReport("add", name, skipped=skipped)
        added: list[str] = []
        failed: list[str] = []
        for pid in targets:
            before = _present(pid, name)
            if before:
                rep.steps.append(Step(pid, "adopt", True, "уже был в протоколе"))
                continue
            try:
                protolib.user_add(pid, name)
                rep.steps.append(Step(pid, "add", True))
                added.append(pid)
            except protolib.ProtoError as e:
                rep.steps.append(Step(pid, "add", False, _err(e)))
                failed.append(pid)
                # модуль мог успеть завести пользователя до ошибки — убираем недоделанное
                if before is False and _present(pid, name):
                    _rollback(rep, [pid], lambda p: protolib.user_del(p, name))
                if not partial:
                    break
        if failed and not partial:
            _rollback(rep, added, lambda pid: protolib.user_del(pid, name))
            _cleanup_client_dir(name)
            _drop_mirror(grp)
            rep.ok = False
            rep.message = "пользователь не создан: ошибка в " + ", ".join(failed)
            return rep
        ok_ids = [s.proto_id for s in rep.steps if s.ok and s.action in ("add", "adopt")]
        if not ok_ids:
            _drop_mirror(grp)
            rep.ok = False
            rep.message = "пользователь не создан ни в одном протоколе"
            return rep
        reg.users.append(User(name, now_iso(), True, note, ok_ids, system=system,
                              group=grp.id if grp else "", custom=custom))
        reg.save()
        if not system:
            _write_allowlist_files(name)
        rep.ok = not failed
        rep.message = "пользователь создан" if rep.ok else "создан частично, без: " + ", ".join(failed)
        return rep


def delete_user(name: str, force: bool = False) -> OpReport:
    with _lock():
        reg = _load_registry()
        user = reg.require(name)
        if name == OWNER and not force:
            raise UserError("owner нельзя удалить: на нём ссылки по умолчанию (--force, если очень нужно)")
        if user.system and not force:
            raise UserError(f"{name} — служебный пользователь пробника, его креды нужны zoo probe и "
                            f"export-probe. Удалить: zoo user del {name} --force (пробник перейдёт на owner; "
                            "завести заново: sudo bash scripts/install.sh --phase 09 из каталога клона)")
        rep = OpReport("del", name)
        left: list[str] = []
        for pid in user.protocols:
            if not protolib.available(pid):
                rep.steps.append(Step(pid, "del", False, "нет модуля протокола"))
                left.append(pid)
                continue
            try:
                protolib.user_del(pid, name)
                rep.steps.append(Step(pid, "del", True))
            except protolib.ProtoError as e:
                if _present(pid, name) is False:
                    rep.steps.append(Step(pid, "del", True, "уже отсутствовал"))
                else:
                    rep.steps.append(Step(pid, "del", False, _err(e)))
                    left.append(pid)
        if left and not force:
            user.protocols = left
            reg.save()
            rep.ok = False
            rep.message = "удалён не везде, остался в: " + ", ".join(left)
            return rep
        reg.remove(name)
        reg.save()
        from . import allowlist
        allowlist.forget_user(name)
        _cleanup_client_dir(name)
        rep.ok = not left
        rep.message = "пользователь удалён" if rep.ok else "удалён из реестра (--force), ошибки: " + ", ".join(left)
        if _drop_probe_export(name):
            rep.message += (f"; пакет пробника {paths.probe_export_file()} с его ключами удалён "
                            "(новый: sudo zoo probe --local --summary --export "
                            f"{paths.probe_export_file()})")
        return rep


def set_enabled(name: str, enabled: bool, partial: bool = False) -> OpReport:
    action = "enable" if enabled else "disable"
    with _lock():
        reg = _load_registry()
        user = reg.require(name)
        if user.system and not enabled:
            raise UserError(f"{name} — служебный пользователь пробника, его не отключают: "
                            "без него самопроверка перейдёт на креды owner")
        rep = OpReport(action, name)
        done: list[str] = []
        failed: list[str] = []
        for pid in user.protocols:
            try:
                protolib.user_enable(pid, name, enabled)
                rep.steps.append(Step(pid, action, True))
                done.append(pid)
            except protolib.ProtoError as e:
                rep.steps.append(Step(pid, action, False, _err(e)))
                failed.append(pid)
                if not partial:
                    break
        if failed and not partial:
            prev = user.enabled
            _rollback(rep, done, lambda pid: protolib.user_enable(pid, name, prev))
            rep.ok = False
            rep.message = "состояние не изменено: ошибка в " + ", ".join(failed)
            return rep
        user.enabled = enabled
        reg.save()
        rep.ok = not failed
        verb = "включён" if enabled else "отключён"
        rep.message = f"пользователь {verb}" if rep.ok else f"{verb} частично, ошибки: " + ", ".join(failed)
        return rep


def _attach(rep: OpReport, user: User, pid: str) -> bool:
    """Завести пользователя в протоколе (или признать уже заведённого) и записать в user.protocols.
    Отключённый остаётся отключённым и в новом протоколе. False — не удалось (шаги в rep)."""
    adopted = False
    try:
        if _present(pid, user.name):
            adopted = True
            rep.steps.append(Step(pid, "adopt", True, "уже был в протоколе"))
        else:
            protolib.user_add(pid, user.name)
            rep.steps.append(Step(pid, "add", True))
    except protolib.ProtoError as e:
        rep.steps.append(Step(pid, "add", False, _err(e)))
        return False
    if not user.enabled:
        try:
            protolib.user_enable(pid, user.name, False)
            rep.steps.append(Step(pid, "disable", True))
        except protolib.ProtoError as e:
            rep.steps.append(Step(pid, "disable", False, _err(e)))
            # отключённый пользователь не должен получить доступ через новый протокол
            if not adopted:
                _rollback(rep, [pid], lambda p: protolib.user_del(p, user.name))
                return False
    user.protocols.append(pid)
    return True


def sync_users(names: list[str] | None = None, include_custom: bool = False) -> list[OpReport]:
    """Довести пользователей до текущего набора протоколов: завести креды в новых
    протоколах (например, включили TUIC после установки), забыть удалённые. Участнику группы
    (без «своего» набора) — только протоколы группы; «свой» набор (custom) не расширяется,
    пока не передан include_custom (иначе протоколы, которых владелец ему не давал, вернулись бы)."""
    from . import groups
    with _lock():
        reg = _load_registry()
        targets, _ = managed_protocols()
        known = {m.id for m in manifests.load_all()[0]}
        gs = groups.Groups.load()
        reports = []
        for user in reg.users:
            if names and user.name not in names:
                continue
            rep = OpReport("sync", user.name)
            for pid in [p for p in user.protocols if p not in known]:
                user.protocols.remove(pid)
                rep.steps.append(Step(pid, "forget", True, "манифеста больше нет"))
            grp = gs.get(user.group) if user.group and not user.custom else None
            lacking = [p for p in (grp.resolve(targets) if grp else targets) if p not in user.protocols]
            if user.custom and not include_custom:
                rep.skipped.update({p: "свой набор протоколов (--include-custom, чтобы добавить)" for p in lacking})
                lacking = []
            for pid in lacking:
                _attach(rep, user, pid)
            rep.ok = not rep.failed
            rep.message = "без изменений" if not rep.steps else ("готово" if rep.ok else "есть ошибки")
            reports.append(rep)
        reg.save()
        return reports


def apply_protocols(wanted: dict[str, list[str]]) -> list[OpReport]:
    """Привести протоколы пользователей к списку: недостающие завести, лишние (из включённых) убрать.
    {имя: [id протоколов]}; ошибка в одном протоколе не откатывает остальные (partial)."""
    with _lock():
        return _apply_protocols(_load_registry(), wanted)


def _apply_protocols(reg: Registry, wanted: dict[str, list[str]]) -> list[OpReport]:
    """То же под уже взятой блокировкой; реестр сохраняется здесь."""
    targets, _ = managed_protocols()
    reports = []
    for name, want_raw in wanted.items():
        user = reg.require(name)
        want = [p for p in want_raw if p in targets]
        rep = OpReport("group", name)
        if not want:
            rep.ok = False
            rep.message = "нет ни одного включённого протокола из списка"
            reports.append(rep)
            continue
        for pid in [p for p in want if p not in user.protocols]:
            _attach(rep, user, pid)
        for pid in [p for p in user.protocols if p in targets and p not in want]:
            try:
                protolib.user_del(pid, name)
                rep.steps.append(Step(pid, "del", True))
                user.protocols.remove(pid)
            except protolib.ProtoError as e:
                if _present(pid, name) is False:
                    rep.steps.append(Step(pid, "del", True, "уже отсутствовал"))
                    user.protocols.remove(pid)
                else:
                    rep.steps.append(Step(pid, "del", False, _err(e)))
        rep.ok = not rep.failed
        rep.message = "без изменений" if not rep.steps else ("готово" if rep.ok else "есть ошибки")
        reports.append(rep)
    reg.save()
    return reports


def bootstrap(owner: str = OWNER) -> OpReport:
    """Создать реестр с owner (фаза 09 / zoo setup). Креды owner заводят фазы протоколов,
    здесь — только учёт: протокол записывается, если owner в нём есть."""
    with _lock():
        reg = Registry.load()
        rep = _bootstrap_into(reg, owner)
        return rep


def ensure_probe_user() -> OpReport | None:
    """Завести служебного пользователя пробника во всех протоколах (фаза 09 / zoo setup).
    Уже есть — None: новые протоколы ему добавит `zoo user sync` (фаза 99). Ошибка в
    части протоколов не мешает остальным (partial)."""
    reg = Registry.load()
    if reg.get(PROBE_USER):
        return None
    try:
        return add_user(PROBE_USER, note=PROBE_NOTE, partial=True, system=True)
    except UserError as e:
        return OpReport("add", PROBE_USER, ok=False, message=str(e))


def probe_user() -> str:
    """Чьими кредами проверять протоколы: zoo-probe, если он заведён и включён, иначе owner."""
    try:
        u = Registry.load().get(PROBE_USER)
    except (UserError, OSError, ValueError):
        return OWNER
    return PROBE_USER if u and u.enabled and u.protocols else OWNER


def hidden_names() -> set[str]:
    """Имена, скрытые из списков и отчётов: служебные пользователи реестра (и zoo-probe всегда)."""
    names = set(SYSTEM_USERS)
    try:
        names |= {u.name for u in Registry.load().users if u.system}
    except (UserError, OSError, ValueError):
        pass
    return names


def _bootstrap_into(reg: Registry, owner: str) -> OpReport:
    rep = OpReport("bootstrap", owner)
    user = reg.get(owner)
    if user is None:
        user = User(owner, now_iso(), True, "владелец сервера", [])
        reg.users.insert(0, user)
        rep.message = "реестр создан"
    targets, rep.skipped = managed_protocols()
    by_id = {m.id: m for m in manifests.load_all()[0]}
    for pid in targets:
        if pid in user.protocols:
            continue
        present = _present(pid, owner)
        if present is None:
            present = bool(by_id[pid].links_for(owner, False) or by_id[pid].files_for(owner, False))
        if present:
            user.protocols.append(pid)
            rep.steps.append(Step(pid, "adopt", True))
        else:
            rep.steps.append(Step(pid, "adopt", False, f"{owner} нет в протоколе (zoo user sync заведёт)"))
    reg.save()
    rep.ok = not rep.failed
    rep.message = rep.message or "реестр обновлён"
    return rep


def _rollback(rep: OpReport, pids: list[str], undo) -> None:
    for pid in reversed(pids):
        try:
            undo(pid)
            rep.steps.append(Step(pid, "rollback", True))
        except protolib.ProtoError as e:
            rep.steps.append(Step(pid, "rollback", False, _err(e)))
    rep.rolled_back = True


def _drop_probe_export(name: str) -> bool:
    """Пакет клиентского пробника с ключами удалённого пользователя бесполезен и вводит в
    заблуждение (пробник у пользователя получит HANDSHAKE_FAIL везде) — удаляем его."""
    f = paths.probe_export_file()
    try:
        if read_json(f).get("user") != name:
            return False
        f.unlink()
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _drop_mirror(grp: Any) -> None:
    """Пользователь не создан: убрать его из зеркала групп (оно писалось до модулей протоколов)."""
    if grp is not None:
        from . import groups
        groups.refresh_mirror()


def _write_allowlist_files(name: str) -> None:
    """Правила v2rayN нового пользователя (D31); сбой не мешает созданию пользователя."""
    from . import allowlist
    try:
        allowlist.write_user_files(name)
    except (allowlist.AllowlistError, OSError):
        pass


def _cleanup_client_dir(name: str) -> None:
    """Каталог клиентских файлов удаляем, только если модули протоколов его уже опустошили."""
    d = paths.clients_dir() / name
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass


# ---------- чтение ----------

def list_users() -> Registry:
    return Registry.load()


def verify() -> dict[str, dict[str, Any]]:
    """Расхождения реестра и протоколов: {id: {missing: [...], extra: [...], error: str}}."""
    reg = Registry.load()
    targets, _ = managed_protocols()
    out: dict[str, dict[str, Any]] = {}
    for pid in targets:
        try:
            actual = set(protolib.user_list(pid))
        except protolib.ProtoError as e:
            out[pid] = {"missing": [], "extra": [], "error": _err(e)}
            continue
        expected = {u.name for u in reg.users if pid in u.protocols}
        out[pid] = {"missing": sorted(expected - actual),
                    "extra": sorted(actual - set(reg.names())), "error": ""}
    return out


def user_links(name: str, protocols: list[str] | None = None) -> tuple[list[protolib.Link], dict[str, str]]:
    """Ссылки и клиентские файлы (Link.kind = uri | file) пользователя по протоколам.

    Каталог clients/<name>/ не сканируется: там и секреты модулей (ключи, пароли).
    Модуль не ответил → ссылки и файлы из манифеста (если есть). Без реестра —
    протоколы, в манифестах которых есть этот пользователь. Без фильтра protocols (или с
    allowlist в нём) в конце — правила v2rayN пользователя (proto_id allowlist).
    Возвращает (ссылки, {id: ошибка}).
    """
    from . import allowlist
    protocols_arg = protocols
    by_id = {m.id: m for m in manifests.load_all()[0]}
    libs = set(protolib.list_libs())
    user = Registry.load().get(name)
    if protocols is not None:
        protocols = [p for p in protocols if p != allowlist.V2RAYN_PROTO]
    if protocols is None:
        if user:
            protocols = list(user.protocols)
        else:
            protocols = [m.id for m in by_id.values() if m.links_for(name, False) or m.files_for(name, False)]
            # второй инстанс модуля (hysteria2-obfs): его ссылки отдаёт сам модуль
            protocols = [p for p in protocols
                         if not (p in by_id and p not in libs and shared_module(by_id[p], libs) in protocols)]

    def one(pid: str) -> tuple[list[protolib.Link], str]:
        try:
            return protolib.links(pid, name), ""
        except protolib.ProtoError as e:
            err = _err(e)
        m = by_id.get(pid)
        fallback = [protolib.Link(uri, "", pid) for uri in (m.links_for(name) if m else [])]
        fallback += [protolib.Link(f, "", pid, "file") for f in (m.files_for(name) if m else [])]
        return fallback, ("" if fallback else err)

    # модули — отдельные bash-процессы (≈50 мс каждый): параллельно, порядок ссылок прежний.
    # Первым — один Xray-модуль в одиночку: xui_hdr создаёт файл заголовка без блокировки,
    # две параллельные первые попытки перетёрли бы друг друга
    done: dict[str, tuple[list[protolib.Link], str]] = {}
    first = next((p for p in protocols if by_id.get(p) is not None and by_id[p].users_backend == "xui"), None)
    if first is not None:
        done[first] = one(first)
    rest = [p for p in protocols if p not in done]
    if len(rest) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(rest))) as pool:
            done.update(zip(rest, pool.map(one, rest)))
    else:
        done.update((p, one(p)) for p in rest)
    result: list[protolib.Link] = []
    errors: dict[str, str] = {}
    for pid in protocols:
        found, err = done[pid]
        result += found
        if err:
            errors[pid] = err
    if protocols_arg is None or allowlist.V2RAYN_PROTO in protocols_arg:
        result += allowlist.links_for(name)
    return result, errors
