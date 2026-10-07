"""Группы пользователей (docs/PLAN-builder.md, этап 3).

Реестр /etc/vpn-setup/groups.json (0600) рядом с users.json:

    {"schema": 1, "groups": [{"id": "main", "name": "Основная",
                              "protocols": ["*"] | [id, ...],          "*" — все включённые
                              "clients": {"android": ["happ", "amneziawg"], ...},   платформа → набор клиентов
                              "messages": {"android": {"text": "текст с {name}", "sig": "…"}, ...},   свой текст инструкции
                                                  (нет — по умолчанию; sig — подпись набора клиентов, строка — старый формат)
                              "allowlist": null | {"android": [...], "windows": [...]}}]}

allowlist null — группа на общем списке приложений (zoo allow), иначе свой список группы.
Клиенты — набор на платформу (по порядку): ни один клиент не умеет все протоколы, поэтому набор
вместе покрывает протоколы группы. Старый формат (строка — один клиент) читается как набор из одного.
В реестре пользователей у участника поле group (id) и, если его набор протоколов задан вручную,
custom: изменение группы его набор протоколов не трогает (zoo group move на ту же группу — вернуть).

Что у группы своё, а что нет: протоколы и список приложений применимы к каждому пользователю
(у пользователя уже есть и то и другое) — они и хранятся в группе. RU_EGRESS и DNS настраиваются на
весь сервер (фаза 07), у отдельного пользователя их нет — поэтому в группе их нет.

Список приложений группы зеркалится в allowlist.json (groups/members): так его читает lib.sh
(zoo_allowlist), которому groups.json не нужен. Порядок: свой список пользователя → группы → общий.

Изменение группы применяется к участникам один раз: протоколы (users._apply_protocols), файлы
v2rayN и Android-конфиги AmneziaWG (allowlist._apply); в отчёте — кому нужен новый QR.
"""

from __future__ import annotations

import argparse
import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import allowlist, clients as clientcat, output, paths, protolib, users
from .fsutil import atomic_write_json, read_json

SCHEMA = 1
MAIN_ID = "main"
MAIN_NAME = "Основная"
ALL = "*"
# порядок протоколов при раздаче: фиксированный, группа хранит только набор
PRIORITY = ("hysteria2", "vless-xhttp", "amneziawg", "hysteria2-obfs", "tuic", "vless-reality", "ss2022")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
NAME_MAX = 40
NEW_USERS_MAX = 20
NOTE_MAX = 200
CLIENTS_MAX = 5       # клиентов на платформу
MESSAGE_MAX = 3000    # знаков в тексте инструкции платформы
KEEP: Any = object()  # «не менять» для update (None у allowlist значит «общий список»)


class GroupError(users.UserError):
    """Неверный ввод или состояние групп."""


class MembersChanged(GroupError):
    """Состав группы не тот, что человек видел на странице подтверждения."""


def by_priority(ids: Any) -> list[str]:
    """Протоколы без повторов в порядке PRIORITY; неизвестные — в конце по алфавиту."""
    return sorted(dict.fromkeys(ids), key=lambda p: (PRIORITY.index(p) if p in PRIORITY else len(PRIORITY), p))


def client_ids(v: Any) -> list[str]:
    """Набор клиентов платформы из файла или формы: список id или (старый формат) одна строка;
    порядок сохраняется, пустые и повторы убираются."""
    items = [v] if isinstance(v, str) else (list(v) if isinstance(v, (list, tuple)) else [])
    out: list[str] = []
    for x in items:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def clean_message(text: Any) -> str:
    """Текст инструкции: переводы строк LF, без управляющих знаков и хвостовых пробелов."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def _norm_protocols(ids: list[str]) -> list[str]:
    return [ALL] if ALL in ids or not ids else by_priority(ids)


def offered(protocols: list[str], selectable: list[str]) -> list[str]:
    return list(selectable) if ALL in protocols else [p for p in protocols if p in selectable]


@dataclass
class Group:
    id: str
    name: str
    protocols: list[str] = field(default_factory=lambda: [ALL])
    clients: dict[str, list[str]] = field(default_factory=dict)
    allowlist: dict[str, list[str]] | None = None
    messages: dict[str, str] = field(default_factory=dict)
    msg_sigs: dict[str, str] = field(default_factory=dict)   # подпись набора клиентов на момент сохранения текста

    def __post_init__(self) -> None:
        self.protocols = _norm_protocols(self.protocols)   # набор, не очередь: порядок — PRIORITY

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Group":
        raw_al = d.get("allowlist")
        own = ({p: allowlist._clean(raw_al.get(p), p) for p in allowlist.PLATFORMS}
               if isinstance(raw_al, dict) else None)
        raw_cl = d.get("clients")
        msgs: dict[str, str] = {}
        sigs: dict[str, str] = {}
        raw_msg = d.get("messages")
        for k, v in (raw_msg.items() if isinstance(raw_msg, dict) else ()):
            body, sig = (v.get("text"), v.get("sig")) if isinstance(v, dict) else (v, None)  # строка — прежний формат
            if isinstance(body, str) and (m := clean_message(body)[:MESSAGE_MAX]):
                msgs[str(k)[:20]] = m
                if isinstance(sig, str) and sig:
                    sigs[str(k)[:20]] = sig[:40]
        return cls(
            id=str(d["id"]), name=str(d.get("name") or d["id"]),
            protocols=[str(x) for x in d.get("protocols", [ALL])],
            clients={str(k): ids for k, v in (raw_cl.items() if isinstance(raw_cl, dict) else ())
                     if (ids := client_ids(v))},
            allowlist=own if own and all(own.values()) else None,
            messages=msgs, msg_sigs=sigs)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name, "protocols": list(self.protocols),
                               "clients": {k: list(v) for k, v in self.clients.items()}, "allowlist": self.allowlist}
        if self.messages:
            out["messages"] = {k: {"text": m, "sig": self.msg_sigs.get(k)} for k, m in self.messages.items()}
        return out

    @property
    def all_protocols(self) -> bool:
        return ALL in self.protocols

    def resolve(self, managed: list[str], variants: dict[str, str] | None = None) -> list[str]:
        """Модули, где участник получает учётки: протоколы группы среди включённых; вариант (hysteria2-obfs)
        даёт учётку своего модуля. variants — users.variant_modules(); не задан — читается здесь."""
        if self.all_protocols:
            return list(managed)
        variants = users.variant_modules() if variants is None else variants
        return [m for m in by_priority(variants.get(p, p) for p in self.protocols) if m in managed]

    def offered(self, selectable: list[str]) -> list[str]:
        """Протоколы группы, которые видит человек (ссылки, QR, плитки): выбранные среди выбираемых,
        в порядке PRIORITY; у «всех включённых» — все выбираемые."""
        return offered(self.protocols, selectable)


class Groups:
    def __init__(self, path: Path, groups: list[Group] | None = None, exists: bool = False) -> None:
        self.path = path
        self.groups = groups or []
        self.exists = exists
        self.custom_recomputed = 0   # метка разового пересчёта custom у «Основной» (_migrate)
        self.obfs_split = 0          # метка: Salamander выбирается отдельно, прежним группам с Hysteria2 он добавлен (_migrate)

    @classmethod
    def load(cls) -> "Groups":
        path = paths.groups_file()
        if not path.exists():
            return cls(path)
        try:
            data = read_json(path)
        except (OSError, ValueError) as e:
            raise GroupError(f"{path}: {e}") from None
        if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
            raise GroupError(f"{path}: ожидается объект с массивом groups")
        out = []
        for d in data["groups"]:
            if isinstance(d, dict) and ID_RE.match(str(d.get("id", ""))):
                out.append(Group.from_dict(d))
        gs = cls(path, out, exists=True)
        gs.custom_recomputed = 1 if data.get("custom_recomputed") == 1 else 0
        gs.obfs_split = 1 if data.get("obfs_split") == 1 else 0
        return gs

    def save(self) -> None:
        data: dict[str, Any] = {"schema": SCHEMA, "groups": [g.to_dict() for g in self.groups]}
        if self.custom_recomputed:
            data["custom_recomputed"] = self.custom_recomputed
        if self.obfs_split:
            data["obfs_split"] = self.obfs_split
        atomic_write_json(self.path, data)
        self.exists = True

    def get(self, ref: str | None) -> Group | None:
        """По id, затем по имени без учёта регистра."""
        ref = (ref or "").strip()
        if not ref:
            return None
        return (next((g for g in self.groups if g.id == ref), None)
                or next((g for g in self.groups if g.name.casefold() == ref.casefold()), None))

    def require(self, ref: str | None) -> Group:
        g = self.get(ref)
        if g is None:
            known = ", ".join(f"{x.name} ({x.id})" for x in self.groups) or "групп нет"
            raise GroupError(f"группы «{ref}» нет ({known})")
        return g

    def free_id(self) -> str:
        n = 1
        while any(g.id == f"g{n}" for g in self.groups):
            n += 1
        return f"g{n}"

    def next_name(self) -> str:
        """«Группа N» — по умолчанию для мастера."""
        n = len(self.groups) + 1
        while self.get(f"Группа {n}"):
            n += 1
        return f"Группа {n}"


# ---------- проверка ввода ----------

def clean_name(name: str, gs: Groups, ignore: str | None = None) -> str:
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", name or "")).strip()
    if not name:
        raise GroupError("у группы должно быть название")
    if len(name) > NAME_MAX:
        raise GroupError(f"название длиннее {NAME_MAX} знаков")
    other = gs.get(name)
    if other is not None and other.id != ignore:
        raise GroupError(f"группа «{name}» уже есть")
    return name


def clean_protocols(raw: list[str]) -> list[str]:
    """Только включённые протоколы (модули и их варианты, например hysteria2-obfs) или «*»;
    без повторов, в порядке PRIORITY."""
    managed = users.selectable_protocols()
    out: list[str] = []
    for p in raw:
        p = (p or "").strip()
        if p == ALL:
            return [ALL]
        if p and p not in out:
            if p not in managed:
                raise GroupError(f"протокол «{p[:40]}» не включён (доступны: {', '.join(managed) or 'нет'})")
            out.append(p)
    if not out:
        raise GroupError("выберите хотя бы один протокол")
    return by_priority(out)


def clean_clients(raw: dict[str, Any], protocols: list[str] | None = None) -> dict[str, list[str]]:
    """{платформа: [id клиента, ...]}: каждый клиент есть в каталоге, на этой платформе и подходит хотя бы
    к одному из выбранных протоколов. Пустое значение — «платформа не нужна» (в итог не попадает)."""
    wanted = {p: ids for p, v in raw.items() if (ids := client_ids(v))}
    if not wanted:
        return {}
    try:
        cat = clientcat.load()
    except clientcat.ClientsError as e:
        raise GroupError(str(e)) from None
    protos = users.selectable_protocols() if not protocols or ALL in protocols else protocols
    out: dict[str, list[str]] = {}
    for plat, ids in wanted.items():
        if plat not in cat.platforms:
            raise GroupError(f"неизвестная платформа «{plat[:20]}»")
        if len(ids) > CLIENTS_MAX:
            raise GroupError(f"на платформу {cat.platforms[plat]} — не больше {CLIENTS_MAX} клиентов")
        for cid in ids:
            c = cat.client(cid)
            if c is None or plat not in c["platforms"]:
                raise GroupError(f"клиента «{cid[:30]}» для платформы {cat.platforms[plat]} нет в каталоге")
            if not any(c["protocols"].get(p, {}).get("s") in ("ok", "warn") for p in protos):
                raise GroupError(f"{c['name']} не поддерживает выбранные протоколы")
        out[plat] = ids
    return out


def clean_allow(lists: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
    """None — общий список; иначе обе платформы непустые и допустимые (как у zoo allow)."""
    if lists is None:
        return None
    try:
        out = {p: allowlist.normalize(p, lists.get(p) or []) for p in allowlist.PLATFORMS}
        for p in allowlist.PLATFORMS:
            if not out[p]:
                raise allowlist.AllowlistError(allowlist._empty_msg(p))
    except allowlist.AllowlistError as e:
        raise GroupError(str(e)) from None
    return out


def parse_new_users(text: str) -> list[tuple[str, str]]:
    """Строки «имя» или «имя заметка…» → [(имя, заметка)]; имя проверяет users.validate_name."""
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, note = line.partition(" ")
        out.append((name.lower(), note.strip()[:NOTE_MAX]))
    if len(out) > NEW_USERS_MAX:
        raise GroupError(f"за раз — не больше {NEW_USERS_MAX} новых пользователей")
    return out


def check_members(new: list[tuple[str, str]], existing: list[str]) -> None:
    """Имена новых годны и свободны, существующие есть в реестре и не служебные."""
    reg = users.list_users()
    seen: set[str] = set()
    for name, _ in new:
        try:
            users.validate_name(name)
        except users.UserError as e:
            raise GroupError(str(e)) from None
        if name in users.SYSTEM_USERS:
            raise GroupError(f"имя «{name}» зарезервировано за служебным пользователем")
        if reg.get(name) or name in seen:
            raise GroupError(f"пользователь «{name}» уже есть" if reg.get(name) else f"имя «{name}» повторяется")
        seen.add(name)
    for name in existing:
        u = reg.get(name)
        if u is None or u.system:
            raise GroupError(f"пользователя «{name[:32]}» нет в реестре")
        if name in seen:
            raise GroupError(f"имя «{name}» повторяется")
        seen.add(name)


# ---------- каталог клиентов под протоколы группы ----------

def client_options(cat: clientcat.Catalog, platform: str, protocols: list[str],
                   prefer: Any = ()) -> list[dict[str, Any]]:
    """Клиенты платформы, которые заявлены для выбранных протоколов (ok/warn). Порядок: клиенты, которых нет
    в российском магазине платформы, — в конце (первым их не предлагаем, пока есть другие); затем охватившие
    больше протоколов; затем уже выбранные на других устройствах группы (prefer: одно приложение на
    несколько устройств); при равенстве — рекомендованный каталогом для первого протокола группы по порядку
    раздачи, затем остальные рекомендованные. recommended=True — у первого, если его рекомендует каталог."""
    real = [p for p in protocols if p in cat.protocols and not cat.protocols[p].get("pseudo")]
    rec = [c["id"] for c in (cat.recommended(platform, p) for p in real) if c]
    lead = next((p for p in cat.raw["handoff"].get(platform, []) if p in real), None)
    lead_rec = cat.recommended(platform, lead) if lead else None
    lead_id = lead_rec["id"] if lead_rec else None
    opts = []
    for c in cat.clients:
        if platform not in c["platforms"]:
            continue
        covers = [p for p in real if c["protocols"].get(p, {}).get("s") in ("ok", "warn")]
        if covers:
            opts.append({"client": c, "covers": covers, "recommended": False,
                         "no_ru_store": cat.no_ru_store(c, platform)})
    opts.sort(key=lambda o: (o["no_ru_store"], -len(o["covers"]), o["client"]["id"] not in prefer,
                             o["client"]["id"] != lead_id,
                             rec.index(o["client"]["id"]) if o["client"]["id"] in rec else 99))
    if opts and opts[0]["client"]["id"] in rec:
        opts[0]["recommended"] = True
    return opts


def _real(cat: clientcat.Catalog, protocols: list[str]) -> list[str]:
    return by_priority(p for p in protocols if p in cat.protocols and not cat.protocols[p].get("pseudo"))


def suggest_clients(cat: clientcat.Catalog, platform: str, protocols: list[str], prefer: Any = ()) -> list[str]:
    """Минимальный набор клиентов платформы, вместе покрывающий протоколы (жадно). На каждом шаге: клиент не из
    российского магазина — только если иначе протокол не покрыть; затем покрывающий больше ещё не покрытых
    протоколов; затем уже выбранный на других устройствах (prefer); затем рекомендованный каталогом для них;
    затем порядок client_options. Результат — по порядку протоколов по PRIORITY: первым идёт клиент самого
    приоритетного протокола. Для всех устройств группы сразу — suggest_set (меньше разных приложений)."""
    real = _real(cat, protocols)
    opts = client_options(cat, platform, protocols, prefer)
    todo = list(real)
    picked: list[tuple[int, str]] = []
    while todo:
        best: tuple[tuple[Any, ...], str, list[str]] | None = None
        for pos, o in enumerate(opts):
            cid = o["client"]["id"]
            new = [p for p in todo if p in o["covers"]]
            if not new or any(cid == x for _, x in picked):
                continue
            rec = sum(1 for p in new if (cat.recommended(platform, p) or {}).get("id") == cid)
            key = (o["no_ru_store"], -len(new), cid not in prefer, -rec, pos)
            if best is None or key < best[0]:
                best = (key, cid, new)
        if best is None:
            break
        picked.append((min(real.index(p) for p in best[2]), best[1]))
        todo = [p for p in todo if p not in best[2]]
    return [cid for _, cid in sorted(picked, key=lambda x: x[0])]


UNIFY_MAX_APPS = 14  # разных приложений в каталоге больше — перебор дорог, остаётся подбор по платформам


def _covered(opts: list[dict[str, Any]]) -> set[str]:
    return set().union(*(o["covers"] for o in opts)) if opts else set()


def _platform_pool(cat: clientcat.Catalog, plat: str,
                   protocols: list[str]) -> tuple[list[dict[str, Any]], set[str]] | None:
    """Клиенты платформы, из которых собирается общий набор, и протоколы, которые на ней можно покрыть.
    Иностранные магазины берутся, только если без них протокол не покрыть (как в suggest_clients);
    клиент с no_unify для этой платформы идёт в набор, только если без него протокол не покрыть."""
    opts = client_options(cat, plat, protocols)
    if not opts:
        return None
    target = _covered(opts)
    ru = [o for o in opts if not o["no_ru_store"]]
    base = ru if _covered(ru) == target else opts
    pool = [o for o in base if plat not in o["client"].get("no_unify", [])]
    return (pool if _covered(pool) == target else base), target


def _cheapest_cover(cat: clientcat.Catalog, plat: str, pool: list[dict[str, Any]], target: set[str],
                    allowed: frozenset[str], max_k: int = CLIENTS_MAX,
                    reach: dict[str, int] | None = None) -> list[dict[str, Any]] | None:
    """Наименьший набор из allowed, покрывающий target и не длиннее max_k: меньше приложений, меньше
    иностранных, приложения, доступные на большем числе устройств (reach), больше рекомендованных каталогом,
    раньше в порядке client_options. Нет такого — None."""
    reach = reach or {}
    mine = [o for o in pool if o["client"]["id"] in allowed]
    if _covered(mine) != target:
        return None

    def rec(o: dict[str, Any]) -> int:
        return sum(1 for p in o["covers"] if (cat.recommended(plat, p) or {}).get("id") == o["client"]["id"])

    for k in range(1, min(len(mine), max_k) + 1):
        best: tuple[tuple[Any, ...], list[dict[str, Any]]] | None = None
        for combo in itertools.combinations(mine, k):
            if _covered(list(combo)) != target:
                continue
            key = (sum(o["no_ru_store"] for o in combo), -sum(reach.get(o["client"]["id"], 0) for o in combo),
                   -sum(rec(o) for o in combo), [pool.index(o) for o in combo])
            if best is None or key < best[0]:
                best = (key, list(combo))
        if best:
            return best[1]
    return None


def suggest_set(cat: clientcat.Catalog, platforms: Any, protocols: list[str]) -> dict[str, list[str]]:
    """Клиенты на каждую платформу так, чтобы на всех устройствах группы было как можно меньше РАЗНЫХ приложений
    (одно кросс-платформенное, где оно покрывает выбранные протоколы). Охват протоколов и число приложений на
    устройстве — как минимум у suggest_clients: ради общего приложения набор на устройстве не растёт. Из наборов с
    равным числом разных приложений берётся тот, где меньше иностранных магазинов, приложения стоят на
    большем числе устройств (сначала самое распространённое), больше рекомендованных каталогом, раньше по
    порядку каталога. Порядок внутри платформы — как у suggest_clients (по PRIORITY)."""
    real = _real(cat, protocols)
    pools: dict[str, tuple[list[dict[str, Any]], set[str]]] = {}
    min_k: dict[str, int] = {}
    for plat in platforms:
        got = _platform_pool(cat, plat, protocols)
        if got:
            pool, target = got
            cover = _cheapest_cover(cat, plat, pool, target, frozenset(o["client"]["id"] for o in pool))
            if cover:
                pools[plat] = got
                min_k[plat] = len(cover)
    universe = list(dict.fromkeys(o["client"]["id"] for pool, _ in pools.values() for o in pool))
    reach = {a: sum(1 for pool, _ in pools.values() if any(o["client"]["id"] == a for o in pool)) for a in universe}
    if len(universe) > UNIFY_MAX_APPS:
        return {plat: ids for plat in pools if (ids := suggest_clients(cat, plat, protocols))}
    best: tuple[tuple[Any, ...], dict[str, list[dict[str, Any]]]] | None = None
    for k in range(1, len(universe) + 1):
        for combo in itertools.combinations(universe, k):
            allowed = frozenset(combo)
            chosen: dict[str, list[dict[str, Any]]] = {}
            for plat, (pool, target) in pools.items():
                cover = _cheapest_cover(cat, plat, pool, target, allowed, min_k[plat], reach)
                if cover is None:
                    break
                chosen[plat] = cover
            else:
                used = [o["client"]["id"] for cover in chosen.values() for o in cover]
                apps = set(used)
                key = (len(apps),
                       sum(o["no_ru_store"] for c in chosen.values() for o in c),
                       tuple(-n for n in sorted((used.count(a) for a in apps), reverse=True)),
                       -sum(1 for plat, c in chosen.items() for o in c for p in o["covers"]
                            if (cat.recommended(plat, p) or {}).get("id") == o["client"]["id"]),
                       sorted(universe.index(a) for a in apps))
                if best is None or key < best[0]:
                    best = (key, chosen)
        if best:
            break
    out: dict[str, list[str]] = {}
    for plat, cover in (best[1] if best else {}).items():
        owner: dict[str, str] = {}
        for o in cover:
            for p in o["covers"]:
                owner.setdefault(p, o["client"]["id"])
        first = {o["client"]["id"]: min((real.index(p) for p, c in owner.items() if c == o["client"]["id"]),
                                        default=len(real)) for o in cover}
        out[plat] = sorted(first, key=lambda i: first[i])
    return {plat: ids for plat in platforms if (ids := out.get(plat) or suggest_clients(cat, plat, protocols))}


def default_clients(cat: clientcat.Catalog, protocols: list[str]) -> dict[str, list[str]]:
    return suggest_set(cat, cat.platforms, protocols)


def apps_summary(cat: clientcat.Catalog, chosen: dict[str, list[str]]) -> tuple[str, str]:
    """Итог выбора по устройствам: (заголовок, строка приложений). Заголовок «Одно приложение на всех
    устройствах: X» — когда на каждом нужном устройстве стоит одно и то же единственное приложение; строка
    «Приложений всего N: X — Android, Windows; Y — iPhone» — когда какое-то приложение стоит на нескольких
    устройствах. Одно устройство или нечего объединять — ('', '')."""
    sets = {plat: ids for plat, ids in chosen.items() if ids}
    if len(sets) < 2:
        return "", ""
    where: dict[str, list[str]] = {}
    for plat, ids in sets.items():
        for cid in ids:
            where.setdefault(cid, []).append(cat.platforms.get(plat, plat))

    def name(cid: str) -> str:
        return (cat.client(cid) or {}).get("name", cid)

    if len(where) == 1:
        return f"Одно приложение на всех устройствах: {name(next(iter(where)))}", ""
    if all(len(v) == 1 for v in where.values()):
        return "", ""
    return "", f"Приложений всего {len(where)}: " + "; ".join(f"{name(c)} — {', '.join(v)}" for c, v in where.items())


def coverage(cat: clientcat.Catalog, platform: str, protocols: list[str],
             ids: list[str]) -> tuple[list[str], list[str]]:
    """(покрытые, не покрытые) протоколы группы набором клиентов ids."""
    real = _real(cat, protocols)
    have = [c for c in (cat.client(i) for i in ids) if c and platform in c["platforms"]]
    done = [p for p in real if any(c["protocols"].get(p, {}).get("s") in ("ok", "warn") for c in have)]
    return done, [p for p in real if p not in done]


# ---------- зеркало списка приложений ----------

def refresh_mirror(gs: Groups | None = None, ureg: users.Registry | None = None,
                   extra: dict[str, str] | None = None) -> None:
    """allowlist.json: списки групп со своим списком и их участники (extra — ещё не записанные в реестр)."""
    gs = gs or Groups.load()
    ureg = ureg or users.Registry.load()
    lists = {g.id: g.allowlist for g in gs.groups if g.allowlist}
    members = {u.name: u.group for u in ureg.visible() if u.group in lists}
    members.update({n: gid for n, gid in (extra or {}).items() if gid in lists})
    try:
        al = allowlist.Allowlist.load()
    except allowlist.AllowlistError:
        return
    if (al.groups, al.members) != (lists, members):
        al.groups, al.members = lists, members
        al.save()


# ---------- отчёт ----------

@dataclass
class GroupReport:
    group: Group
    message: str = ""
    created: list[str] = field(default_factory=list)   # новые пользователи
    moved: list[str] = field(default_factory=list)     # перешли в группу
    needs_qr: list[str] = field(default_factory=list)  # кому нужны новые QR и файлы
    skipped: list[str] = field(default_factory=list)   # «свой набор»: протоколы не тронуты
    errors: list[str] = field(default_factory=list)
    allow: dict[str, Any] = field(default_factory=dict)
    crashed: bool = False   # добавление людей упало исключением: часть могла примениться
    removed: bool = False   # группа удалена (мастер убрал созданную пустую; delete_group)
    deleted: list[str] = field(default_factory=list)   # пользователи, удалённые вместе с группой

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"group": self.group.to_dict(), "ok": self.ok, "message": self.message, "created": self.created,
                "moved": self.moved, "needs_qr": self.needs_qr, "skipped": self.skipped,
                "errors": self.errors, "allow": self.allow, "removed": self.removed,
                "deleted": self.deleted}


def _errors(reports: list[users.OpReport]) -> list[str]:
    out = []
    for r in reports:
        if not r.ok and not r.steps:
            out.append(f"{r.user}: {r.message}")
        out += [f"{r.user}: {s.proto_id}: {s.error}" for s in r.failed]
    return out


# ---------- миграция ----------

OBFS = "hysteria2-obfs"


def _obfs_pending(gs: Groups) -> bool:
    """Salamander стал отдельным выбором: один раз группам с Hysteria2 добавляется он (как было, пока галочка была
    общей), если включён на сервере. Метка obfs_split ставится при добавлении; пока Salamander выключен — не ставится."""
    return gs.exists and not gs.obfs_split and OBFS in users.variant_modules()


def _migrate(gs: Groups, ureg: users.Registry) -> bool:
    """«Основная» и все пользователи без группы — в неё, с текущими настройками: протоколы «*»
    (как раньше: всё включённое), у кого набор меньше — custom (группа его не трогает).
    Один раз (метка obfs_split) группам с Hysteria2 добавляется Salamander (_obfs_pending).
    Один раз (метка custom_recomputed в groups.json) custom у участников «Основной» пересчитывается по
    тому же правилу: серверы, перенесённые прежним кодом, получили неверные флаги. Идемпотентно.
    True — что-то записано."""
    changed = False
    if not gs.exists:
        gs.groups.append(Group(MAIN_ID, MAIN_NAME, [ALL]))
        gs.save()
        changed = True
    if _obfs_pending(gs):
        for g in gs.groups:
            if not g.all_protocols and "hysteria2" in g.protocols:
                g.protocols = by_priority([*g.protocols, OBFS])
        gs.obfs_split = 1
        gs.save()
        changed = True
    main = gs.get(MAIN_ID)
    if main is not None and ureg.exists:
        managed, _ = users.managed_protocols()
        vis = ureg.visible()
        # протокол, заведённый только у owner, — новый: фаза включила его владельцу, остальным его
        # доведёт zoo user sync; отсутствие такого протокола не делает набор «своим»
        fresh = {p for p in managed if {u.name for u in vis if p in u.protocols} <= {users.OWNER}}
        recompute = not gs.custom_recomputed
        assigned = False
        for u in vis:
            if not u.group or gs.get(u.group) is None:
                u.group = main.id
                u.custom = bool(u.protocols) and not (set(managed) - fresh) <= set(u.protocols)
                assigned = True
            elif recompute and u.group == main.id:
                custom = bool(u.protocols) and not (set(managed) - fresh) <= set(u.protocols)
                assigned = assigned or custom != u.custom
                u.custom = custom
        if assigned:
            ureg.save()
            changed = True
        if recompute:
            gs.custom_recomputed = 1
            gs.save()
            changed = True
    if changed:
        refresh_mirror(gs, ureg)
    return changed


def _pending(gs: Groups, ureg: users.Registry) -> bool:
    """Нужна ли запись миграции (проверка без блокировки: страницы читают её на каждом показе)."""
    if not gs.exists:
        return ureg.exists
    if _obfs_pending(gs):
        return True
    if gs.get(MAIN_ID) is None:
        return False
    return (ureg.exists and not gs.custom_recomputed) or any(not u.group or gs.get(u.group) is None
                                                             for u in ureg.visible())


def ensure() -> Groups:
    """Группы после миграции (создаёт groups.json, если его нет и реестр пользователей есть)."""
    gs, ureg = Groups.load(), users.Registry.load()
    if not _pending(gs, ureg):
        return gs
    with users._lock():
        ureg = users.Registry.load()
        gs = Groups.load()
        if ureg.exists or gs.exists:
            _migrate(gs, ureg)
        return gs


def _open() -> tuple[Groups, users.Registry]:
    """Под блокировкой: реестры после миграции."""
    ureg = users._load_registry()
    gs = Groups.load()
    _migrate(gs, ureg)
    return gs, ureg


def members_of(gs: Groups, ureg: users.Registry, gid: str) -> list[users.User]:
    return [u for u in ureg.visible() if u.group == gid]


# ---------- изменения ----------

def create(name: str, protocols: list[str], clients: dict[str, Any] | None = None,
           allow: dict[str, list[str]] | None = None) -> Group:
    with users._lock():
        gs, ureg = _open()
        g = Group(gs.free_id(), clean_name(name, gs), clean_protocols(protocols),
                  clean_clients(clients or {}, protocols), clean_allow(allow))
        gs.groups.append(g)
        gs.save()
        refresh_mirror(gs, ureg)
        return g


def _settle(rep: GroupReport, gs: Groups, ureg: users.Registry, names: list[str],
            before_protos: dict[str, list[str]], before_allow: dict[str, Any], protocols: bool) -> None:
    """Применить настройки групп к участникам names один раз (под блокировкой, реестры сохранены)."""
    managed, _ = users.managed_protocols()
    variants = users.variant_modules()
    if protocols:
        wanted: dict[str, list[str]] = {}
        for n in names:
            u = ureg.require(n)
            if u.custom:
                rep.skipped.append(n)
                continue
            want = gs.require(u.group).resolve(managed, variants)
            if want:
                wanted[n] = want
            else:
                rep.errors.append(f"{n}: в группе нет включённых протоколов")
        rep.errors += _errors(users._apply_protocols(ureg, wanted))
    after = allowlist._snapshot(allowlist.Allowlist.load())
    changed = [n for n in names if after.get(n) != before_allow.get(n)]
    if changed:
        try:
            rep.allow = allowlist._apply(changed)
        except allowlist.AllowlistError as e:
            rep.errors.append(f"файлы приложений: {e}")
        else:
            for n, e in rep.allow.get("errors", {}).items():
                rep.errors.append(f"{n}: {e}")
            if str(rep.allow.get("amneziawg", "")).startswith("ошибка"):
                rep.errors.append(f"AmneziaWG: {rep.allow['amneziawg']} (повторить: sudo zoo allow apply)")
    protos_now = {n: set(ureg.require(n).protocols) for n in names}
    rep.needs_qr = [n for n in names if protos_now[n] != set(before_protos[n]) or n in changed]


def _snapshot(ureg: users.Registry, names: list[str]) -> tuple[dict[str, list[str]], dict[str, Any]]:
    return ({n: list(ureg.require(n).protocols) for n in names},
            allowlist._snapshot(allowlist.Allowlist.load()))


def update(ref: str, name: str | None = None, protocols: list[str] | None = None,
           clients: dict[str, Any] | None = None, allow: Any = KEEP) -> GroupReport:
    """Изменить группу и применить к участникам один раз. allow: KEEP — не менять, None — общий
    список, словарь — свой список группы."""
    with users._lock():
        gs, ureg = _open()
        g = gs.require(ref)
        names = [u.name for u in members_of(gs, ureg, g.id)]
        before = _snapshot(ureg, names)
        new_name = clean_name(name, gs, g.id) if name is not None else g.name
        new_protocols = clean_protocols(protocols) if protocols is not None else g.protocols
        new_clients = clean_clients(clients, new_protocols) if clients is not None else g.clients
        new_allow = clean_allow(allow) if allow is not KEEP else g.allowlist
        protos_changed = new_protocols != g.protocols
        sel = users.selectable_protocols()
        offer_changed = offered(new_protocols, sel) != offered(g.protocols, sel)
        allow_changed = new_allow != g.allowlist
        g.name, g.protocols, g.clients, g.allowlist = new_name, new_protocols, new_clients, new_allow
        gs.save()
        rep = GroupReport(g, "сохранено")
        refresh_mirror(gs, ureg)
        if names and (protos_changed or allow_changed):
            _settle(rep, gs, ureg, names, before[0], before[1], protos_changed)
            if offer_changed:   # учётки те же, но набор ссылок (Salamander) другой
                rep.needs_qr += [n for n in names if n not in rep.needs_qr and n not in rep.skipped]
        return rep


def set_message(ref: str, platform: str, text: str | None, sig: str | None = None) -> Group:
    """Свой текст инструкции группы для платформы; пустой или None — вернуть текст по умолчанию.
    sig — подпись набора клиентов группы на эту платформу: по ней страница заметит, что набор сменился
    и текст мог устареть. Участников не трогает: текст читают страницы при показе."""
    try:
        cat = clientcat.load()
    except clientcat.ClientsError as e:
        raise GroupError(str(e)) from None
    if platform not in cat.platforms:
        raise GroupError(f"неизвестная платформа «{platform[:20]}»")
    msg = clean_message(text)
    if len(msg) > MESSAGE_MAX:
        raise GroupError(f"текст длиннее {MESSAGE_MAX} знаков")
    with users._lock():
        gs, _ = _open()
        g = gs.require(ref)
        if msg:
            g.messages[platform] = msg
            if sig:
                g.msg_sigs[platform] = sig[:40]
            else:
                g.msg_sigs.pop(platform, None)
        else:
            g.messages.pop(platform, None)
            g.msg_sigs.pop(platform, None)
        gs.save()
        return g


def _move_in(gs: Groups, ureg: users.Registry, names: list[str], g: Group, keep_custom: bool = False) -> GroupReport:
    """Перевод в группу под блокировкой: реестры сохраняются, настройки группы применяются один раз.
    keep_custom — «свой набор протоколов» остаётся (меняется только группа); иначе набор возвращается к группе."""
    before = _snapshot(ureg, names)
    for n in names:
        u = ureg.require(n)
        u.group = g.id
        if not keep_custom:
            u.custom = False
    ureg.save()
    refresh_mirror(gs, ureg)
    rep = GroupReport(g, "готово")
    rep.moved = list(names)
    _settle(rep, gs, ureg, names, before[0], before[1], True)
    return rep


def move_many(names: list[str], ref: str) -> GroupReport:
    """Перевести пользователей в группу: у них её протоколы и её список приложений
    (в том числе ставший «своим» набор протоколов возвращается к группе)."""
    with users._lock():
        gs, ureg = _open()
        g = gs.require(ref)
        for n in names:
            u = ureg.get(n)
            if u is None or u.system:
                raise GroupError(f"пользователя «{n[:32]}» нет в реестре")
        return _move_in(gs, ureg, names, g)


MAIN_KEEP = f"«{MAIN_NAME}» не удаляется: в неё переходят участники удалённых групп и попадают новые пользователи"


def delete_group(ref: str, members: str = "move", expected: list[str] | None = None) -> GroupReport:
    """Удалить группу и решить судьбу участников: «move» — перевести в «Основную», «delete» — удалить
    пользователей (owner не удаляется никогда — он переводится). Всё под одной блокировкой, настройки
    «Основной» применяются к переведённым один раз. Кто-то не удалился — группа остаётся, в отчёте ошибки.
    Переведённые со «своим набором протоколов» его сохраняют (меняется только группа).
    expected — состав группы, который видел человек на странице подтверждения: изменился — отказ, ничего не тронуто."""
    if members not in ("move", "delete"):
        raise GroupError(f"участники: «move» или «delete», получено «{members[:20]}»")
    with users._lock():
        gs, ureg = _open()
        g = gs.require(ref)
        main = gs.get(MAIN_ID)
        if g.id == MAIN_ID:
            raise GroupError(MAIN_KEEP)
        if main is None:
            raise GroupError(f"группы «{MAIN_NAME}» нет: переводить участников некуда")
        mem = [u.name for u in members_of(gs, ureg, g.id)]
        if expected is not None and set(expected) != set(mem):
            was = ", ".join(sorted(expected)) or "никого"
            now = ", ".join(sorted(mem)) or "никого"
            raise MembersChanged(f"состав группы «{g.name}» изменился, пока вы подтверждали удаление (было: {was}; стало: {now}). "
                             "Ничего не удалено и не переведено — откройте страницу удаления заново")
        doomed = [n for n in mem if n != users.OWNER] if members == "delete" else []
        rep = GroupReport(main, "группа удалена")
        for n in doomed:
            r = users._delete_in(ureg, n)
            if r.ok:
                rep.deleted.append(n)
            else:
                rep.errors.append(f"{n}: {r.message}" + "".join(f" ({s.proto_id}: {s.error})" for s in r.failed))
        if rep.errors:
            refresh_mirror(gs, ureg)
            rep.message = f"группа «{g.name}» не удалена: не все участники удалены"
            return rep
        keep = [n for n in mem if n not in doomed]
        if keep:
            mv = _move_in(gs, ureg, keep, main, keep_custom=True)
            rep.moved, rep.skipped, rep.allow, rep.needs_qr = mv.moved, mv.skipped, mv.allow, mv.needs_qr
            rep.errors += mv.errors
        gs.groups.remove(g)
        gs.save()
        refresh_mirror(gs, ureg)
        rep.message = f"группа «{g.name}» удалена"
        rep.removed = True
        return rep


def merge_groups(src: str, dst: str) -> GroupReport:
    """Объединить: участники src переходят в dst (протоколы и приложения — как у dst), src удаляется,
    настройки dst не меняются; «свой набор протоколов» у переведённых сохраняется. Одна блокировка, одно применение.
    «Основная» как src не годится."""
    with users._lock():
        gs, ureg = _open()
        s, d = gs.require(src), gs.require(dst)
        if s.id == d.id:
            raise GroupError("выберите другую группу: сама с собой не объединяется")
        if s.id == MAIN_ID:
            raise GroupError(MAIN_KEEP + ". Объедините другую группу с ней")
        names = [u.name for u in members_of(gs, ureg, s.id)]
        rep = _move_in(gs, ureg, names, d, keep_custom=True) if names else GroupReport(d)
        gs.groups.remove(s)
        gs.save()
        refresh_mirror(gs, ureg)
        rep.group = d
        rep.message = f"«{s.name}» объединена с «{d.name}»"
        return rep


def remove(ref: str) -> Group:
    with users._lock():
        gs, ureg = _open()
        g = gs.require(ref)
        members = members_of(gs, ureg, g.id)
        if members:
            raise GroupError(f"в группе «{g.name}» есть пользователи ({', '.join(u.name for u in members)}): "
                             "сначала переведите их в другую группу")
        gs.groups.remove(g)
        gs.save()
        refresh_mirror(gs, ureg)
        return g


def add_members(ref: str, new: list[tuple[str, str]], existing: list[str]) -> GroupReport:
    """Новые пользователи (создаются сразу в группе) и существующие (переводятся). Ввод проверяется
    до первого изменения."""
    check_members(new, existing)
    g = Groups.load().require(ref)
    rep = GroupReport(g, "готово")
    for name, note in new:
        try:
            r = users.add_user(name, note=note, group=g.id)
        except (users.UserError, protolib.ProtoError) as e:
            rep.errors.append(f"{name}: {e}")
            continue
        if r.ok:
            rep.created.append(name)
            rep.needs_qr.append(name)
        else:
            rep.errors += [f"{name}: {r.message}"] + [f"{name}: {s.proto_id}: {s.error}" for s in r.failed]
    if existing:
        mv = move_many(existing, g.id)
        rep.moved, rep.skipped, rep.allow = mv.moved, mv.skipped, mv.allow
        rep.errors += mv.errors
        rep.needs_qr += [n for n in mv.needs_qr if n not in rep.needs_qr]
    return rep


def connect(name: str, protocols: list[str], clients: dict[str, Any] | None,
            allow: dict[str, list[str]] | None, new: list[tuple[str, str]], existing: list[str]) -> GroupReport:
    """Мастер «Новая группа»: группа + пользователи, всё проверено до первого изменения.
    Исключение при добавлении людей не бросается наружу (rep.crashed): отчёт с ошибками и тем, что
    успело примениться. Если не добавлен никто, созданная пустая группа удаляется (rep.removed):
    повтор мастера с тем же названием не должен упереться в «уже есть». Обычные отказы по отдельным
    людям (rep.errors без исключения) группу оставляют — её видно на странице группы."""
    if not new and not existing:
        raise GroupError("добавьте хотя бы одного пользователя")
    check_members(new, existing)
    g = create(name, protocols, clients, allow)
    try:
        rep = add_members(g.id, new, existing)
    except Exception as e:  # noqa: BLE001 — отчёт важнее причины: частичное состояние нужно показать
        rep = GroupReport(g, errors=[f"{type(e).__name__}: {e}"])
        rep.crashed = True
    try:
        have = {u.name for u in members_of(Groups.load(), users.list_users(), g.id)}
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"реестр не прочитан: {e}")
        have = set()
    if rep.crashed:
        rep.created = [n for n, _ in new if n in have]
        rep.moved = [n for n in existing if n in have]
        rep.needs_qr = list(rep.created + rep.moved)
    if rep.crashed and not have:
        try:
            remove(g.id)
            rep.removed = True
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"пустая группа «{g.name}» не удалена: {e}")
    rep.message = ("группа создана" if rep.ok else
                   "группа не создана" if rep.removed else "группа создана с ошибками")
    return rep


# ---------- CLI ----------

def _fmt_protocols(g: Group) -> str:
    return "все включённые" if g.all_protocols else ", ".join(g.protocols)


def _print_report(rep: GroupReport, as_json: bool) -> int:
    if as_json:
        output.print_json(rep.to_dict())
        return 0 if rep.ok else 1
    for n in rep.created:
        output.ok(f"{n}: создан в группе «{rep.group.name}»")
    for n in rep.deleted:
        output.ok(f"{n}: удалён")
    for n in rep.moved:
        output.ok(f"{n}: в группе «{rep.group.name}»")
    for n in rep.skipped:
        output.info(f"{n}: свой набор протоколов, группа его не тронула (zoo group move {n} {rep.group.id} — вернуть)")
    for e in rep.errors:
        output.error(e)
    (output.ok if rep.ok else output.warn)(f"{rep.group.name}: {rep.message}")
    if rep.needs_qr:
        output.info("новые QR и файлы нужны: " + ", ".join(rep.needs_qr)
                    + " (zoo links ИМЯ --qr)")
    return 0 if rep.ok else 1


def _parse_clients(items: list[str] | None) -> dict[str, list[str]]:
    """--client android=happ,amneziawg: набор платформы (повтор платформы дописывает клиентов)."""
    out: dict[str, list[str]] = {}
    for it in items or []:
        plat, sep, cids = it.partition("=")
        if not sep:
            raise GroupError(f"--client ждёт ПЛАТФОРМА=КЛИЕНТ[,КЛИЕНТ] (android=happ,amneziawg), получено «{it[:40]}»")
        out.setdefault(plat.strip(), []).extend(client_ids(cids.split(",")))
    return out


def _allow_lists(apps: list[str], base: allowlist.Allowlist) -> dict[str, list[str]]:
    """--allow: приложения указанных платформ заменяют список; остальная платформа — как сейчас."""
    got: dict[str, list[str]] = {}
    for p, ident in allowlist.resolve(apps):
        got.setdefault(p, []).append(ident)
    return {p: got.get(p) or base.baseline(p) for p in allowlist.PLATFORMS}


def cmd_group_list(args: argparse.Namespace, cfg: Any) -> int:
    gs = ensure()
    ureg = users.list_users()
    rows, data = [], []
    for g in gs.groups:
        names = [u.name for u in members_of(gs, ureg, g.id)]
        data.append({**g.to_dict(), "members": names})
        rows.append([g.id, g.name, _fmt_protocols(g), ", ".join(f"{p}={'+'.join(c)}" for p, c in g.clients.items()) or "—",
                     "свой" if g.allowlist else "общий", ", ".join(names) or "—"])
    if args.json:
        output.print_json({"path": str(gs.path), "groups": data})
        return 0
    if not rows:
        output.warn("групп нет: реестр пользователей ещё не создан (zoo setup)")
        return 0
    print(output.table(rows, ["id", "название", "протоколы", "клиенты", "приложения", "участники"]))
    return 0


def cmd_group_add(args: argparse.Namespace, cfg: Any) -> int:
    allow = None
    if args.allow:
        allow = _allow_lists(args.allow, allowlist.Allowlist.load())
    g = create(args.name, [ALL] if args.all_protocols or not args.proto else args.proto,
               _parse_clients(args.client), allow)
    if args.json:
        output.print_json(g.to_dict())
    else:
        output.ok(f"группа «{g.name}» ({g.id}): протоколы — {_fmt_protocols(g)}")
        output.info(f"пользователь в группу: zoo user add ИМЯ --group {g.id}")
    return 0


def cmd_group_set(args: argparse.Namespace, cfg: Any) -> int:
    allow: Any = KEEP
    if args.allow_common:
        allow = None
    elif args.allow:
        allow = _allow_lists(args.allow, allowlist.Allowlist.load())
    clients = _parse_clients(args.client) if args.client else None
    if args.no_client:
        clients = {**(clients or {}), **{p: [] for p in args.no_client}}
    if clients is not None:
        # изменение — поверх текущих клиентов группы
        cur = Groups.load().require(args.group).clients
        clients = {**cur, **clients}
    protos = [ALL] if args.all_protocols else args.proto
    rep = update(args.group, args.name, protos, clients, allow)
    return _print_report(rep, args.json)


def cmd_group_move(args: argparse.Namespace, cfg: Any) -> int:
    return _print_report(move_many([args.user], args.group), args.json)


def cmd_group_merge(args: argparse.Namespace, cfg: Any) -> int:
    return _print_report(merge_groups(args.src, args.dst), args.json)


def cmd_group_rm(args: argparse.Namespace, cfg: Any) -> int:
    if args.move_members or args.delete_members:
        return _print_report(delete_group(args.group, "delete" if args.delete_members else "move"), args.json)
    if Groups.load().require(args.group).id == MAIN_ID:
        raise GroupError(MAIN_KEEP)
    g = remove(args.group)
    if args.json:
        output.print_json(g.to_dict())
    else:
        output.ok(f"группа «{g.name}» удалена")
    return 0
