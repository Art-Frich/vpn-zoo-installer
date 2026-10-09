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

from . import allowlist, clients as clientcat, manifests, output, paths, people, protolib, users
from .fsutil import atomic_write_json, read_json

SCHEMA = 1
MAIN_ID = "main"
MAIN_NAME = "Основная"
ALL = "*"
# порядок протоколов при раздаче: фиксированный, группа хранит только набор
PRIORITY = ("hysteria2", "vless-xhttp", "amneziawg", "hysteria2-obfs", "tuic", "vless-reality", "ss2022")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
NAME_MAX = 40
NEW_USERS_MAX = people.LINES_MAX
NOTE_MAX = people.NOTE_MAX
CLIENTS_MAX = 5       # клиентов на платформу
MESSAGE_MAX = 3000    # знаков в тексте инструкции платформы
KEEP: Any = object()  # «не менять» для update (None у allowlist значит «общий список»)
# кто ставит приложения: люди сами по инструкции / ИТ-администратор / телефоны — сами, компьютеры — ИТ (D61)
INSTALL_MODES = ("self", "admin", "mixed")
PHONES = ("android", "ios")
MAIN_DEVICES = ("android", "ios", "windows")
SHAKY = ("ss2022",)   # в полевом тесте терял данные (D37): в готовые варианты не берётся
MAX_APPS = 2          # приложений на устройство в наборе по умолчанию
STORE_KINDS = ("play", "appstore")
LINK_STORE_KINDS = STORE_KINDS + ("msstore",)   # магазины для порядка ссылок в тексте; в подборе — только STORE_KINDS
# транспорт протокола: «Надёжно» берёт пару из разных (TCP + UDP)
TRANSPORT = {"vless-reality": "tcp", "vless-xhttp": "tcp", "ss2022": "tcp", "hysteria2": "udp",
             "hysteria2-obfs": "udp", "amneziawg": "udp", "tuic": "udp"}


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


def clean_mode(v: Any, default: str = "self") -> str:
    return v if v in INSTALL_MODES else default


def mode_for(mode: str, plat: str) -> str:
    """Кто ставит на устройстве: «mixed» — телефоны люди ставят сами (личные), компьютеры — ИТ (рабочие)."""
    if mode == "mixed":
        return "self" if plat in PHONES else "admin"
    return mode if mode in INSTALL_MODES else "self"


def _norm_protocols(ids: list[str]) -> list[str]:
    return [ALL] if ALL in ids or not ids else by_priority(ids)


def offered(protocols: list[str], selectable: list[str]) -> list[str]:
    """Протоколы группы среди выбираемых, везде в порядке PRIORITY (у «всех включённых» — все выбираемые)."""
    return by_priority(selectable if ALL in protocols else [p for p in protocols if p in selectable])


@dataclass
class Group:
    id: str
    name: str
    protocols: list[str] = field(default_factory=lambda: [ALL])
    clients: dict[str, list[str]] = field(default_factory=dict)
    allowlist: dict[str, list[str]] | None = None
    messages: dict[str, str] = field(default_factory=dict)
    msg_sigs: dict[str, str] = field(default_factory=dict)   # подпись набора клиентов на момент сохранения текста
    install_mode: str = "self"   # «self» — люди ставят сами, «admin» — приложения ставит ИТ, «mixed» — телефоны сами, компьютеры ИТ
    extra: list[str] = field(default_factory=list)   # устройства с приложениями, но не по умолчанию (Mac у одного человека)

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
            messages=msgs, msg_sigs=sigs, install_mode=clean_mode(d.get("install_mode")),
            extra=[str(x)[:20] for x in d.get("extra_devices") or [] if isinstance(x, str)][:10])

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name, "protocols": list(self.protocols),
                               "clients": {k: list(v) for k, v in self.clients.items()}, "allowlist": self.allowlist}
        if extra := [p for p in self.extra if self.clients.get(p)]:
            out["extra_devices"] = extra
        if self.install_mode != "self":
            out["install_mode"] = self.install_mode
        if self.messages:
            out["messages"] = {k: {"text": m, "sig": self.msg_sigs.get(k)} for k, m in self.messages.items()}
        return out

    @property
    def all_protocols(self) -> bool:
        return ALL in self.protocols

    def mode(self, plat: str) -> str:
        """Кто ставит приложения на этом устройстве: «self» или «admin»."""
        return mode_for(self.install_mode, plat)

    @property
    def any_admin(self) -> bool:
        """Хоть на одном устройстве группы приложения ставит ИТ (нужны «Дистрибутивы» и памятка)."""
        return any(self.mode(p) == "admin" for p in (self.app_devices or MAIN_DEVICES))

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

    @property
    def devices(self) -> list[str]:
        """Устройства группы — платформы, для которых выбраны приложения; по умолчанию они же у каждого участника.
        Кроме extra: их приложения есть, но получают их только те, у кого это устройство указано."""
        return [p for p, ids in self.clients.items() if ids and p not in self.extra]

    @property
    def app_devices(self) -> list[str]:
        """Все платформы, для которых у группы есть приложения (и устройства по умолчанию, и extra)."""
        return [p for p, ids in self.clients.items() if ids]


def devices_of(u: users.User, g: Group | None) -> list[str] | None:
    """Устройства человека: свои, иначе группы; без группы и своих — None (все, для которых есть приложения)."""
    if u.devices:
        return list(u.devices)
    return g.devices if g is not None else None


def set_devices(names: list[str], devices: list[str]) -> dict[str, list[str]]:
    """Задать людям устройства. Совпали с устройствами группы (или пусто) — «как у группы»: дальше их меняет группа.
    Возвращает {логин: записанные устройства} (пусто — как у группы)."""
    plats = clientcat.load().platforms
    bad = [d for d in devices if d not in plats]
    if bad:
        raise GroupError(f"неизвестное устройство: {bad[0][:16]}")
    want = [p for p in plats if p in devices]
    gs, ureg = Groups.load(), users.list_users()
    out: dict[str, list[str]] = {}
    for n in names:
        u = ureg.get(n)
        if u is None or u.system:
            raise GroupError(f"пользователя «{n[:32]}» нет в реестре")
        g = gs.get(u.group)
        out[n] = [] if g is not None and set(want) == set(g.devices) else want
    users.set_devices(names, out)
    return out


class Groups:
    def __init__(self, path: Path, groups: list[Group] | None = None, exists: bool = False) -> None:
        self.path = path
        self.groups = groups or []
        self.exists = exists
        self.custom_recomputed = 0   # метка разового пересчёта custom у «Основной» (_migrate)
        self.obfs_split = 0          # метка: Salamander выбирается отдельно, прежним группам с Hysteria2 он добавлен (_migrate)
        self.clients_filled = 0      # метка: группам без набора приложений один раз подобран набор (_migrate)
        self.main_preset = 0         # метка: «Основной» один раз дан набор рекомендованного варианта (_migrate)

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
        gs.clients_filled = 1 if data.get("clients_filled") == 1 else 0
        gs.main_preset = 1 if data.get("main_preset") == 1 else 0
        return gs

    def save(self) -> None:
        data: dict[str, Any] = {"schema": SCHEMA, "groups": [g.to_dict() for g in self.groups]}
        if self.custom_recomputed:
            data["custom_recomputed"] = self.custom_recomputed
        if self.obfs_split:
            data["obfs_split"] = self.obfs_split
        if self.clients_filled:
            data["clients_filled"] = self.clients_filled
        if self.main_preset:
            data["main_preset"] = self.main_preset
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
        raise GroupError("назовите группу, например «Бухгалтерия»")
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


def parse_new_users(text: str) -> list[tuple[str, str, str, tuple[str, ...]]]:
    """Список людей (people.build) → [(id, заметка, имя, устройства)]; негодная строка или больше people.LINES_MAX — отказ."""
    plan = people.plan_for_registry(text)
    if not plan.ok:
        raise GroupError(plan.error)
    return plan.entries()


def check_members(new: list[tuple[str, ...]], existing: list[str]) -> None:
    """Имена новых годны и свободны, существующие есть в реестре и не служебные. new — (id, заметка[, имя])."""
    reg = users.list_users()
    seen: set[str] = set()
    for name, *_ in new:
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
        if platform not in c["platforms"] or not c.get("import"):   # без import ссылок и QR не берёт: людям его не отдать
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
UNIFY_SLACK = 2      # на сколько приложений больше минимума ещё ищется набор с меньшим числом иностранных и оговорок


def _covered(opts: list[dict[str, Any]]) -> set[str]:
    return set().union(*(o["covers"] for o in opts)) if opts else set()


def in_store(client: dict[str, Any], plat: str) -> bool:
    """У клиента на платформе есть страница в магазине приложений (Google Play, App Store)."""
    return any(ln["kind"] in STORE_KINDS for ln in client["platforms"].get(plat, []))


def has_store_link(client: dict[str, Any], plat: str) -> bool:
    """Есть ссылка на любой магазин (Google Play, App Store, Microsoft Store): для подсказки «ставится файлом»."""
    return any(ln["kind"] in LINK_STORE_KINDS for ln in client["platforms"].get(plat, []))


def _store_bound(plat: str, mode: str) -> bool:
    """Магазин решает, что можно поставить: людям, которые ставят сами, и на iPhone (мимо App Store не
    поставить даже админу). Админ на Android и компьютерах берёт APK и установщики с GitHub."""
    return mode_for(mode, plat) == "self" or plat == "ios"


def _platform_pool(cat: clientcat.Catalog, plat: str, protocols: list[str],
                   mode: str = "self") -> tuple[list[dict[str, Any]], set[str]] | None:
    """Клиенты платформы, из которых собирается общий набор, и протоколы, которые на ней можно покрыть.
    Приложения только из магазина и российский магазин берутся, пока ими покрыты все протоколы платформы
    (иначе — любые); клиент с no_unify для этой платформы идёт в набор, только если без него протокол не покрыть."""
    opts = client_options(cat, plat, protocols)
    if not opts:
        return None
    target = _covered(opts)
    base = opts
    if mode_for(mode, plat) == "self":   # люди ставят сами с российским Apple ID: есть замена из РФ-магазина — иностранные не предлагаем
        ru = [o for o in opts if not o["no_ru_store"]]
        if ru:
            base, target = ru, _covered(ru)
    if _store_bound(plat, mode):
        stored = [o for o in base if in_store(o["client"], plat)]
        if _covered(stored) == target:
            base = stored
        ru = [o for o in base if not o["no_ru_store"]]
        if _covered(ru) == target:
            base = ru
    pool = [o for o in base if plat not in o["client"].get("no_unify", [])]
    return (pool if _covered(pool) == target else base), target


def _warn_count(cat: clientcat.Catalog, plat: str, chosen: list[dict[str, Any]]) -> int:
    """Протоколы, которые набор покрывает только с оговоркой (ни у одного приложения нет статуса ok)."""
    have = set().union(*(o["covers"] for o in chosen)) if chosen else set()
    return sum(1 for p in have if not any(cat.status(o["client"], p, plat) == "ok" for o in chosen))


def _cheapest_cover(cat: clientcat.Catalog, plat: str, pool: list[dict[str, Any]], target: set[str],
                    allowed: frozenset[str], max_k: int = CLIENTS_MAX,
                    reach: dict[str, int] | None = None) -> list[dict[str, Any]] | None:
    """Наименьший набор из allowed, покрывающий target и не длиннее max_k: меньше приложений, меньше
    иностранных, меньше протоколов «с оговоркой» там, где есть клиент без неё, приложения, доступные на большем числе устройств (reach), больше рекомендованных каталогом,
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
            key = (sum(o["no_ru_store"] for o in combo), _warn_count(cat, plat, list(combo)),
                   -sum(reach.get(o["client"]["id"], 0) for o in combo),
                   -sum(rec(o) for o in combo), [pool.index(o) for o in combo])
            if best is None or key < best[0]:
                best = (key, list(combo))
        if best:
            return best[1]
    return None


def suggest_set(cat: clientcat.Catalog, platforms: Any, protocols: list[str],
                mode: str = "self") -> dict[str, list[str]]:
    """Клиенты на каждую платформу так, чтобы на всех устройствах группы было как можно меньше РАЗНЫХ приложений
    (одно кросс-платформенное, где оно покрывает выбранные протоколы). Охват протоколов и число приложений на
    устройстве — как минимум у suggest_clients: ради общего приложения набор на устройстве не растёт. Из наборов с
    равным числом разных приложений берётся тот, где меньше иностранных магазинов, приложения стоят на
    большем числе устройств (сначала самое распространённое), больше рекомендованных каталогом, раньше по
    порядку каталога. Порядок внутри платформы — как у suggest_clients (по PRIORITY). Больше MAX_APPS на
    устройстве не бывает: тогда берётся лучший набор из двух (client_sets) и строка устройства честно
    говорит «без X». mode — кто ставит приложения (см. _platform_pool)."""
    real = _real(cat, protocols)
    pools: dict[str, tuple[list[dict[str, Any]], set[str]]] = {}
    min_k: dict[str, int] = {}
    for plat in platforms:
        got = _platform_pool(cat, plat, protocols, mode)
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
    found_at = 0
    for k in range(1, len(universe) + 1):
        if found_at and k > found_at + UNIFY_SLACK:
            break
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
                key = (sum(o["no_ru_store"] for c in chosen.values() for o in c),
                       sum(_warn_count(cat, plat, c) for plat, c in chosen.items()),
                       len(apps),
                       tuple(-n for n in sorted((used.count(a) for a in apps), reverse=True)),
                       -sum(1 for plat, c in chosen.items() for o in c for p in o["covers"]
                            if (cat.recommended(plat, p) or {}).get("id") == o["client"]["id"]),
                       sorted(universe.index(a) for a in apps))
                if best is None or key < best[0]:
                    best = (key, chosen)
        if best and not found_at:
            found_at = k   # меньше разных приложений уже есть; ещё UNIFY_SLACK размеров ищем набор без иностранных и оговорок
    out: dict[str, list[str]] = {}
    for plat, cover in (best[1] if best else {}).items():
        owner: dict[str, str] = {}
        for o in cover:
            for p in o["covers"]:
                owner.setdefault(p, o["client"]["id"])
        first = {o["client"]["id"]: min((real.index(p) for p, c in owner.items() if c == o["client"]["id"]),
                                        default=len(real)) for o in cover}
        out[plat] = sorted(first, key=lambda i: first[i])
    plan = {plat: ids for plat in platforms if (ids := out.get(plat) or suggest_clients(cat, plat, protocols))}
    for plat in [p for p, ids in plan.items() if len(ids) > MAX_APPS]:
        used = {a for p, ids in plan.items() if p != plat for a in ids}
        top = client_sets(cat, plat, protocols, mode, prefer=used)
        if top:
            plan[plat] = top[0]["ids"]
    return plan


def default_clients(cat: clientcat.Catalog, protocols: list[str], mode: str = "self") -> dict[str, list[str]]:
    return suggest_set(cat, cat.platforms, protocols, mode)


def _by_first_protocol(real: list[str], opts: list[dict[str, Any]]) -> list[str]:
    """id клиентов набора: первым — тот, кто отвечает за самый приоритетный протокол."""
    owner: dict[str, str] = {}
    for o in opts:
        for p in o["covers"]:
            owner.setdefault(p, o["client"]["id"])
    first = {o["client"]["id"]: min((real.index(p) for p, c in owner.items() if c == o["client"]["id"]),
                                    default=len(real)) for o in opts}
    return sorted(first, key=lambda i: first[i])


def client_sets(cat: clientcat.Catalog, plat: str, protocols: list[str], mode: str = "self",
                max_apps: int = MAX_APPS, prefer: Any = ()) -> list[dict[str, Any]]:
    """Наборы из 1–max_apps приложений платформы, покрывающие хотя бы один протокол, лучшие первыми. Поля: ids
    (по PRIORITY), covers, missing (протоколы без клиента), foreign (не из российского магазина), nostore
    (не из магазина вообще), rec (рекомендовано каталогом). Порядок: меньше непокрытых, меньше «не из магазина»
    и «не из РФ-магазина» (там, где магазин решает, см. _store_bound), меньше протоколов «с оговоркой», меньше приложений, больше
    рекомендованных, уже выбранные на других устройствах (prefer), порядок каталога. Набор, где приложение
    ничего не добавляет, и набор, который строго хуже другого (то же покрытие при большем числе приложений),
    отбрасываются."""
    real = _real(cat, protocols)
    opts = client_options(cat, plat, protocols)
    bound = _store_bound(plat, mode)
    any_store = bound and any(in_store(o["client"], plat) for o in opts)
    prefer = frozenset(prefer)
    rows: list[dict[str, Any]] = []
    for k in range(1, min(max_apps, len(opts)) + 1):
        for combo in itertools.combinations(range(len(opts)), k):
            chosen = [opts[i] for i in combo]
            covers = [set(o["covers"]) for o in chosen]
            if any(not (covers[j] - set().union(*(c for m, c in enumerate(covers) if m != j))) for j in range(k)):
                continue
            ids = _by_first_protocol(real, chosen)
            have = set().union(*covers)
            rec = sum(1 for o in chosen for p in o["covers"] if (cat.recommended(plat, p) or {}).get("id") == o["client"]["id"])
            rows.append({"ids": ids, "covers": [p for p in real if p in have], "missing": [p for p in real if p not in have],
                         "foreign": sum(o["no_ru_store"] for o in chosen) if bound else 0,
                         "nostore": sum(not in_store(o["client"], plat) for o in chosen) if any_store else 0,
                         "warns": _warn_count(cat, plat, chosen), "rec": rec, "order": combo})
    rows = [r for r in rows if not any(set(s["covers"]) >= set(r["covers"]) and len(s["ids"]) < len(r["ids"]) for s in rows)]
    rows.sort(key=lambda r: (len(r["missing"]), r["nostore"], r["foreign"], r["warns"], len(r["ids"]), -r["rec"],
                             -len(set(r["ids"]) & prefer), r["order"]))
    return rows


def easy_protocols(cat: clientcat.Catalog) -> set[str]:
    """Протоколы, которые человек ставит сам без ручных правок: на телефоне есть приложение из магазина,
    принимающее их из одного QR."""
    return {p for c in cat.clients for plat in ("android", "ios") if plat in c["platforms"]
            and in_store(c, plat) and "qr" in c.get("import", {})
            for p, st in c["protocols"].items() if st.get("s") in ("ok", "warn") and p in cat.protocols
            and not cat.protocols[p].get("pseudo")}


# Готовые варианты — presets каталога (zoo/data/clients.json), одни для обоих режимов «кто ставит»: через VPN только
# приложения из списка там, где клиент это умеет (D55). «Просто»: Android — AmneziaWG (список внутри QR), iPhone — INCY
# с «РФ напрямую», Windows — v2rayN с правилами, Mac и Linux — v2rayN с VPN только в Brave; «Надёжно» — то же и Happ
# на Android на случай, если режут UDP (советуется он: у «Просто» Android только на UDP, D59).
# Протокола варианта нет на сервере или на устройстве не осталось приложения — подбор по общим правилам.


def recommended_preset(prs: list[dict[str, Any]]) -> str | None:
    """Какой вариант советовать: первый без оговорок (clean), где ни одно устройство не осталось только на UDP (мобильные
    сети и офисный Wi-Fi режут его чаще TCP) — сейчас «Надёжно»; таких нет — первый без оговорок; у всех оговорки — никакой."""
    clean = [p for p in prs if p.get("clean")]
    both = [p["id"] for p in clean if not p.get("udp_only")]
    return both[0] if both else (clean[0]["id"] if clean else None)


def recommended(cat: clientcat.Catalog, available: list[str], mode: str = "self",
                devices: Any = MAIN_DEVICES) -> dict[str, Any] | None:
    """Рекомендованный вариант целиком (протоколы и приложения); не советуется ни один — первый из вариантов."""
    prs = presets(cat, available, mode, devices)
    best = recommended_preset(prs)
    return next((p for p in prs if p["id"] == best), prs[0] if prs else None)


def device_plan(cat: clientcat.Catalog, protocols: list[str], plat: str, mode: str = "self") -> list[str]:
    """Приложения для устройства, которого у группы ещё нет (человек с Mac в группе без macOS): как в рекомендованном
    варианте каталога, если они берут протоколы группы; иначе — общий подбор."""
    specs = sorted(cat.raw.get("presets", []), key=lambda x: x["id"] != "reliable")
    for spec in specs:
        ids = [i for i in spec["plan"].get(plat, []) if coverage(cat, plat, protocols, [i])[0]]
        if ids:
            return ids
    return suggest_set(cat, [plat], protocols, mode).get(plat, [])


def _pinned(cat: clientcat.Catalog, pid: str, cands: list[str], devices: list[str]) -> tuple[list[str], dict[str, list[str]]] | None:
    """Вариант каталога под включённые протоколы: протоколы — те из варианта, что есть среди cands; приложения устройства —
    те из варианта, что умеют хоть один из них. Не осталось протоколов или устройство без приложения — None."""
    spec = next((x for x in cat.raw.get("presets", []) if x["id"] == pid), None)
    if spec is None:
        return None
    protos = by_priority(p for p in spec["protocols"] if p in cands)
    plan: dict[str, list[str]] = {}
    for plat in devices:
        ids = [i for i in spec["plan"].get(plat, []) if coverage(cat, plat, protos, [i])[0]]
        if not ids:
            return None
        plan[plat] = ids
    return (protos, plan) if protos else None


def presets(cat: clientcat.Catalog, available: list[str], mode: str = "self",
            devices: Any = MAIN_DEVICES) -> list[dict[str, Any]]:
    """Готовые варианты первого экрана мастера: «simple» — одно приложение на устройство, «reliable» — не больше MAX_APPS.
    Сначала — presets каталога (_pinned); не подошёл — подбор: «simple» — один протокол, «reliable» — два (TCP + UDP),
    меньше иностранных магазинов и оговорок, затем меньше разных приложений. Поля: id, protocols, plan {платформа:
    [клиенты]}, apps (число разных), per_device (наибольшее число на устройстве), gaps (протоколов, которых нет на
    устройствах), complete (на каждом устройстве есть приложение хоть для одного протокола), foreign (приложений не из
    РФ-магазина), warns (протоколов «с оговоркой»), clean (complete и ничего из остального). Протоколы людям, которые
    ставят сами, — только из easy_protocols. Нет подходящего — варианта нет."""
    devices = list(devices)
    easy = easy_protocols(cat)
    cands = [p for p in by_priority(available) if p in cat.protocols and not cat.protocols[p].get("pseudo")
             and p not in SHAKY and (not any(mode_for(mode, ph) == "self" for ph in PHONES if ph in devices) or p in easy)]

    def make(pid: str, protos: list[str], plan: dict[str, list[str]] | None = None) -> dict[str, Any]:
        plan = suggest_set(cat, devices, protos, mode) if plan is None else plan
        cover = {plat: coverage(cat, plat, protos, ids) for plat, ids in plan.items()}
        out = {"id": pid, "protocols": protos, "plan": plan, "apps": len({a for v in plan.values() for a in v}),
               "per_device": max((len(v) for v in plan.values()), default=0),
               "gaps": sum(len(miss) for _, miss in cover.values()),
               "complete": set(plan) == set(devices) and all(done for done, _ in cover.values()),
               "foreign": sum(1 for plat, ids in plan.items() for i in ids if cat.no_ru_store(cat.client(i) or {}, plat)),
               "warns": sum(len(caveats(cat, plat, protos, ids)) for plat, ids in plan.items()),
               # устройства, где все протоколы его приложений — UDP: где режут UDP, не подключатся
               "udp_only": [plat for plat, (done, _) in cover.items()
                            if done and all(TRANSPORT.get(p) == "udp" for p in done)]}
        out["clean"] = out["complete"] and not out["foreign"] and not out["warns"]
        return out

    out: list[dict[str, Any]] = []
    simple = None
    if got := _pinned(cat, "simple", cands, devices):
        pinned = make("simple", *got)
        if pinned["complete"] and pinned["per_device"] == 1:
            simple = pinned
    if simple is None:
        auto = [x for x in (make("simple", [p]) for p in cands) if x["complete"] and not x["gaps"] and x["per_device"] == 1]
        simple = min(auto, key=lambda x: (x["foreign"], x["warns"], x["apps"], cands.index(x["protocols"][0])),
                     default=None)
    if simple:
        out.append(simple)
    reliable = None
    if got := _pinned(cat, "reliable", cands, devices):
        pinned = make("reliable", *got)
        if pinned["complete"] and pinned["per_device"] <= MAX_APPS and (simple is None or pinned["plan"] != simple["plan"]):
            reliable = pinned
    if reliable is None:
        pairs = [x for x in (make("reliable", list(by_priority(pair))) for pair in itertools.combinations(cands, 2))
                 if x["plan"]]
        reliable = min(pairs, key=lambda x: (x["gaps"] > 0 or not x["complete"], x["per_device"] > MAX_APPS,
                                             TRANSPORT.get(x["protocols"][0]) == TRANSPORT.get(x["protocols"][1]),
                                             x["foreign"], x["warns"], x["apps"],
                                             sum(cands.index(p) for p in x["protocols"])), default=None)
    if reliable:
        out.append(reliable)
    return out


def via_line(cat: clientcat.Catalog, chosen: dict[str, list[str]]) -> str:
    """Что идёт через VPN у главного (первого) приложения каждого устройства: «Через VPN: Android, Windows — только
    приложения из списка · iPhone — всё, кроме российских сайтов, список не действует · macOS — только браузер Brave,
    список не действует». Не описано ни у кого — пусто."""
    where: dict[str, list[str]] = {}
    for plat in cat.platforms:
        ids = chosen.get(plat) or []
        c = cat.client(ids[0]) if ids else None
        mode = cat.via(c, plat) if c else ""
        if mode:
            text = cat.raw["via"][mode] + ("" if mode == "apps" else ", список не действует")
            where.setdefault(text, []).append(cat.platforms[plat])
    return "Через VPN: " + " · ".join(f"{', '.join(v)} — {text}" for text, v in where.items()) if where else ""


def apps_line(cat: clientcat.Catalog, chosen: dict[str, list[str]]) -> str:
    """Набор приложений группы одной строкой: «Happ — Android, iPhone · v2rayN — Windows». Нет приложений — пусто."""
    where: dict[str, list[str]] = {}
    for plat in cat.platforms:
        for cid in chosen.get(plat) or []:
            where.setdefault(cid, []).append(cat.platforms[plat])
    return " · ".join(f"{(cat.client(cid) or {}).get('name', cid)} — {', '.join(v)}" for cid, v in where.items())


def coverage(cat: clientcat.Catalog, platform: str, protocols: list[str],
             ids: list[str]) -> tuple[list[str], list[str]]:
    """(покрытые, не покрытые) протоколы группы набором клиентов ids."""
    real = _real(cat, protocols)
    have = [c for c in (cat.client(i) for i in ids) if c and platform in c["platforms"]]
    done = [p for p in real if any(c["protocols"].get(p, {}).get("s") in ("ok", "warn") for c in have)]
    return done, [p for p in real if p not in done]


def caveats(cat: clientcat.Catalog, platform: str, protocols: list[str],
            ids: list[str]) -> list[tuple[str, str, str, str]]:
    """Протоколы, которые набор ids покрывает только «с оговоркой» (warn): [(протокол, приложение, оговорка, заметка)];
    оговорка — short статуса («без проверки сертификата»), заметка — полный текст каталога."""
    have = [c for c in (cat.client(i) for i in ids) if c and platform in c["platforms"]]
    out = []
    for p in _real(cat, protocols):
        sts = [(c, c["protocols"].get(p, {}), cat.status(c, p, platform)) for c in have]
        if not any(s == "ok" for _, _, s in sts):
            warn = next(((c, st) for c, st, s in sts if s == "warn"), None)
            if warn:
                out.append((p, warn[0]["name"], warn[1].get("short") or "с оговоркой", warn[1].get("note", "")))
    return out


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
    resend: list[str] = field(default_factory=list)    # кому отмечено «переслать» (resend.mark)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"group": self.group.to_dict(), "ok": self.ok, "message": self.message, "created": self.created,
                "moved": self.moved, "needs_qr": self.needs_qr, "skipped": self.skipped,
                "errors": self.errors, "allow": self.allow, "removed": self.removed,
                "deleted": self.deleted, "resend": self.resend}


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


def _main_preset() -> dict[str, Any] | None:
    """Рекомендованный вариант для «Основной» (людям, которые ставят сами); каталога или протоколов нет — None."""
    try:
        cat = clientcat.load()
    except clientcat.ClientsError:
        return None
    return recommended(cat, users.selectable_protocols(), "self")


def _foreign_set(cat: clientcat.Catalog, g: Group) -> bool:
    """В наборе группы есть приложение, которого нет ни в одном варианте каталога на этом устройстве (прежний
    автоподбор «Основной» ставил Hiddify, который не берёт VLESS и AmneziaWG)."""
    known = {plat: {i for spec in cat.raw.get("presets", []) for i in spec["plan"].get(plat, [])} for plat in cat.platforms}
    return any(i not in known.get(plat, set()) for plat, ids in g.clients.items() for i in ids)


def _main_pending(gs: Groups) -> bool:
    return gs.exists and not gs.main_preset and gs.get(MAIN_ID) is not None


def _fix_main(gs: Groups, ureg: users.Registry) -> None:
    """Один раз (метка main_preset): набор «Основной» с приложением не из вариантов каталога заменяется набором
    рекомендованного варианта; участникам — отметка «переслать сообщение целиком». Протоколы не трогаются: у людей
    уже есть ключи."""
    main = gs.get(MAIN_ID)
    pr = _main_preset()
    try:
        cat = clientcat.load()
    except clientcat.ClientsError:
        return
    if main is not None and pr is not None and pr["plan"] and _foreign_set(cat, main):
        main.clients = {plat: list(ids) for plat, ids in pr["plan"].items()}
        if ureg.exists:
            from . import resend
            names = [u.name for u in members_of(gs, ureg, MAIN_ID) if u.name != users.OWNER]
            if resend.mark(ureg, names):
                ureg.save()
    gs.main_preset = 1


def _fill_pending(gs: Groups) -> bool:
    """Есть ли группы без набора приложений, которым его ещё не подбирали (метка clients_filled) и есть из чего."""
    return (gs.exists and not gs.clients_filled and any(not g.clients for g in gs.groups)
            and bool(users.selectable_protocols()))


def _fill_clients(gs: Groups) -> None:
    """Группам без набора приложений — подбор под их протоколы и режим «кто ставит» (как в мастере): без него
    инструкции и карточки не знали бы, что раздавать. Дальше пустой набор — осознанный выбор администратора."""
    try:
        cat = clientcat.load()
    except clientcat.ClientsError:
        return
    selectable = users.selectable_protocols()
    for g in gs.groups:
        if not g.clients and (protos := g.offered(selectable)):
            pr = _main_preset() if g.id == MAIN_ID and g.install_mode == "self" else None
            g.clients = ({plat: list(ids) for plat, ids in pr["plan"].items()} if pr and pr["plan"]
                         else suggest_set(cat, MAIN_DEVICES, protos, g.install_mode))
    gs.clients_filled = 1


def _migrate(gs: Groups, ureg: users.Registry) -> bool:
    """«Основная» и все пользователи без группы — в неё, с текущими настройками: протоколы «*»
    (как раньше: всё включённое), у кого набор меньше — custom (группа его не трогает).
    Один раз (метка obfs_split) группам с Hysteria2 добавляется Salamander (_obfs_pending).
    Один раз (метка clients_filled) группам без набора приложений подбирается набор (_fill_clients).
    Один раз (метка custom_recomputed в groups.json) custom у участников «Основной» пересчитывается по
    тому же правилу: серверы, перенесённые прежним кодом, получили неверные флаги. Идемпотентно.
    True — что-то записано."""
    changed = False
    if not gs.exists:
        main = Group(MAIN_ID, MAIN_NAME, [ALL])
        if pr := _main_preset():
            # приложения «Основной» — как у рекомендованного варианта мастера (D59); протоколы — «все включённые»: она
            # следует за включением протоколов, и у людей, заведённых до групп, есть ключи всех
            main.clients = {plat: list(ids) for plat, ids in pr["plan"].items()}
            gs.clients_filled = 1
        gs.groups.append(main)
        gs.main_preset = 1
        gs.save()
        changed = True
    if _main_pending(gs):
        _fix_main(gs, ureg)
        gs.save()
        changed = True
    if _obfs_pending(gs):
        for g in gs.groups:
            if not g.all_protocols and "hysteria2" in g.protocols:
                g.protocols = by_priority([*g.protocols, OBFS])
        gs.obfs_split = 1
        gs.save()
        changed = True
    if _fill_pending(gs):
        _fill_clients(gs)
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
    if _obfs_pending(gs) or _fill_pending(gs) or _main_pending(gs):
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
           allow: dict[str, list[str]] | None = None, install_mode: str = "self") -> Group:
    with users._lock():
        gs, ureg = _open()
        g = Group(gs.free_id(), clean_name(name, gs), clean_protocols(protocols),
                  clean_clients(clients or {}, protocols), clean_allow(allow), install_mode=clean_mode(install_mode))
        gs.groups.append(g)
        gs.save()
        refresh_mirror(gs, ureg)
        return g


def _settle(rep: GroupReport, gs: Groups, ureg: users.Registry, names: list[str],
            before_protos: dict[str, list[str]], before_allow: dict[str, Any], protocols: bool,
            kinds: dict[str, list[str]] | None = None) -> None:
    """Применить настройки групп к участникам names один раз (под блокировкой, реестры сохранены). Кому что переслать —
    отметкой в реестре: kinds — что поменялось на его устройствах (resend.view_kinds; считает вызывающий: только он
    знает, какой набор был до правки), список — по приложениям."""
    managed, _ = users.managed_protocols()
    variants = users.variant_modules()
    if protocols:
        wanted: dict[str, list[str]] = {}
        for n in names:
            u = ureg.require(n)
            if n == users.OWNER:   # как в sync_users: владельцу всё включённое, иначе sync пересоздаст учётки с новыми ключами
                wanted[n] = list(managed)
                continue
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
    from . import resend
    protos_now = {n: set(ureg.require(n).protocols) for n in names}
    rep.needs_qr = [n for n in names if protos_now[n] != set(before_protos[n]) or n in changed]
    _mark(rep, ureg, [], resend.list_changes({n: before_allow.get(n, {}) for n in changed},
                                             {n: after.get(n, {}) for n in changed}), gs, kinds)


def _mark(rep: GroupReport, ureg: users.Registry, full: list[str], lists: dict[str, set[str]] | None = None,
          gs: Groups | None = None, kinds: dict[str, list[str]] | None = None) -> None:
    from . import resend
    if marked := resend.mark(ureg, full, lists, gs, kinds):
        ureg.save()
        rep.resend += [n for n in marked if n not in rep.resend]


def member_view(cat: clientcat.Catalog, clients: dict[str, list[str]], devices: list[str],
                protocols: list[str]) -> dict[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Что человек получает на каждом своём устройстве: (приложение, протоколы, которые достаются ему) по порядку набора —
    как в инструкции (протокол берёт первое приложение, которое его умеет). Сравнение до и после правки группы говорит,
    кому слать новое сообщение."""
    out: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {}
    for plat in devices:
        taken: set[str] = set()
        row = []
        for cid in clients.get(plat) or []:
            c = cat.client(cid)
            mine = tuple(p for p in protocols if p not in taken and c is not None
                         and cat.status(c, p, plat) in ("ok", "warn"))
            taken.update(mine)
            row.append((cid, mine))
        out[plat] = tuple(row)
    return out


def view_of(cat: clientcat.Catalog, u: users.User, g: Group, sel: list[str]) -> dict[str, Any]:
    """member_view человека в группе g: его устройства, протоколы, которые он видит (свой набор и owner — все)."""
    sees_all = u.custom or u.name == users.OWNER
    return member_view(cat, g.clients, devices_of(u, g) or [], sel if sees_all else g.offered(sel))


def _touched(names: list[str], ureg: users.Registry, old: Group, new: Group, sel: list[str]) -> dict[str, list[str]]:
    """{участник: что поменялось на его устройствах} — только у кого на его устройствах сменились приложения или ключи:
    правка Windows не задевает тех, у кого только телефон; ещё один ключ — «добавьте», а не «удалите всё». Каталог не
    читается — все, сообщение целиком."""
    from . import resend
    try:
        cat = clientcat.load()
    except clientcat.ClientsError:
        return {n: ["all"] for n in names}
    out = {}
    for n in names:
        u = ureg.require(n)
        if kinds := resend.view_kinds(view_of(cat, u, old, sel), view_of(cat, u, new, sel)):
            out[n] = kinds
    return out


def preview_change(g: Group, members: list[users.User], sel: list[str], add: Any = (),
                   clients: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Кому и что пришлось бы переслать, если группе добавить протоколы add и/или задать наборы приложений clients
    ({устройство: приложения}) — тем же сравнением, что update(): подсказки «не работает» называют число людей,
    которое потом покажет «Кому переслать»."""
    new = Group(g.id, g.name, list(g.protocols) if g.all_protocols or not add else [*g.protocols, *add],
                {**{p: list(v) for p, v in g.clients.items()}, **(clients or {})}, extra=list(g.extra))
    reg = users.Registry(paths.users_file(), list(members))
    return _touched([u.name for u in members], reg, g, new, sel)


def _snapshot(ureg: users.Registry, names: list[str]) -> tuple[dict[str, list[str]], dict[str, Any]]:
    return ({n: list(ureg.require(n).protocols) for n in names},
            allowlist._snapshot(allowlist.Allowlist.load()))


def update(ref: str, name: str | None = None, protocols: list[str] | None = None,
           clients: dict[str, Any] | None = None, allow: Any = KEEP, install_mode: str | None = None,
           drop: list[str] | None = None) -> GroupReport:
    """Изменить группу и применить к участникам один раз. allow: KEEP — не менять, None — общий
    список, словарь — свой список группы. install_mode: кто ставит приложения (участников не затрагивает).
    drop: убрать из группы выключенные на сервере протоколы (включённые убираются через protocols)."""
    with users._lock():
        gs, ureg = _open()
        g = gs.require(ref)
        names = [u.name for u in members_of(gs, ureg, g.id)]
        before = _snapshot(ureg, names)
        new_name = clean_name(name, gs, g.id) if name is not None else g.name
        sel = users.selectable_protocols()
        drop = [p for p in (drop or []) if p in g.protocols and p not in sel]
        new_protocols = clean_protocols(protocols) if protocols is not None else g.protocols
        if protocols is not None and new_protocols != [ALL]:
            # выключенный сейчас протокол (манифест есть, enabled=false) в форме не выбрать: он остаётся в группе и
            # вернётся участникам при включении, пока его не убрали явно (drop); неизвестные id отпадают
            off = {m.id for m in manifests.load_all()[0] if not m.enabled}
            new_protocols = by_priority(new_protocols + [p for p in g.protocols
                                                         if p != ALL and p not in sel and p in off and p not in drop])
        elif drop:
            new_protocols = [p for p in g.protocols if p not in drop]
            if not new_protocols:
                raise GroupError("выберите хотя бы один протокол")
        new_clients = clean_clients(clients, new_protocols) if clients is not None else g.clients
        new_allow = clean_allow(allow) if allow is not KEEP else g.allowlist
        protos_changed = new_protocols != g.protocols
        offer_changed = offered(new_protocols, sel) != offered(g.protocols, sel)
        allow_changed = new_allow != g.allowlist
        clients_changed = new_clients != g.clients
        old = Group(g.id, g.name, list(g.protocols), {p: list(v) for p, v in g.clients.items()}, extra=list(g.extra))
        g.name, g.protocols, g.clients, g.allowlist = new_name, new_protocols, new_clients, new_allow
        if install_mode is not None:
            g.install_mode = clean_mode(install_mode)
        gs.save()
        rep = GroupReport(g, "сохранено")
        refresh_mirror(gs, ureg)
        if names and (protos_changed or allow_changed):
            _settle(rep, gs, ureg, names, before[0], before[1], protos_changed)
            if offer_changed:   # учётки те же, но набор ссылок (Salamander) другой
                rep.needs_qr += [n for n in names if n not in rep.needs_qr and n not in rep.skipped]
        if names and (offer_changed or clients_changed):   # другие ключи или приложения на его устройствах
            _mark(rep, ureg, [], gs=gs, kinds=_touched(names, ureg, old, g, sel))
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
    kinds = _move_kinds(gs, ureg, names, g)   # другая группа — другие приложения и инструкция: что именно, по устройствам
    for n in names:
        u = ureg.require(n)
        u.group = g.id
        if not keep_custom:
            u.custom = False
    ureg.save()
    refresh_mirror(gs, ureg)
    rep = GroupReport(g, "готово")
    rep.moved = list(names)
    _settle(rep, gs, ureg, names, before[0], before[1], True, kinds)
    return rep


def _move_kinds(gs: Groups, ureg: users.Registry, names: list[str], g: Group) -> dict[str, list[str]]:
    """Что поменяется на устройствах у переводимых в g (до перевода: старая группа ещё известна). Без старой группы или
    каталога — сообщение целиком."""
    from . import resend
    try:
        cat: clientcat.Catalog | None = clientcat.load()
    except clientcat.ClientsError:
        cat = None
    sel = users.selectable_protocols()
    out: dict[str, list[str]] = {}
    for n in names:
        u = ureg.require(n)
        if u.group == g.id:
            continue
        old = gs.get(u.group)
        if cat is None or old is None:
            out[n] = ["all"]
            continue
        after = users.User(u.name, protocols=list(u.protocols), group=g.id, devices=list(u.devices))
        if kinds := resend.view_kinds(view_of(cat, u, old, sel), view_of(cat, after, g, sel)):
            out[n] = kinds
    return out


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
            rep.moved, rep.skipped, rep.allow, rep.needs_qr, rep.resend = mv.moved, mv.skipped, mv.allow, mv.needs_qr, mv.resend
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


def add_devices(ref: str, plans: dict[str, list[str]]) -> Group:
    """Дать группе приложения для устройств, которых у неё нет (человек с Mac): устройства по умолчанию у остальных не
    меняются (extra), пересылать им нечего."""
    with users._lock():
        gs, _ = _open()
        g = gs.require(ref)
        new = {p: ids for p, ids in clean_clients(plans, g.protocols).items() if not g.clients.get(p)}
        for plat, ids in new.items():
            g.clients[plat] = ids
            if plat not in g.extra:
                g.extra.append(plat)
        if new:
            gs.save()
        return g


def lacking_plans(have: Any, protocols: list[str], wanted: list[str], mode: str = "self") -> dict[str, list[str]]:
    """{устройство: приложения} для устройств из списка людей (wanted), для которых у группы или черновика (have —
    устройства с приложениями) приложений нет."""
    try:
        cat = clientcat.load()
    except clientcat.ClientsError:
        return {}
    have = set(have)
    protos = users.selectable_protocols() if not protocols or ALL in protocols else protocols
    return {p: ids for p in cat.platforms if p in wanted and p not in have
            and (ids := device_plan(cat, protos, p, mode))}


def add_members(ref: str, new: list[tuple[str, ...]], existing: list[str]) -> GroupReport:
    """Новые пользователи (создаются сразу в группе) и существующие (переводятся). Ввод проверяется
    до первого изменения."""
    check_members(new, existing)
    g = Groups.load().require(ref)
    # устройства как у группы не записываются: человек следует за группой
    new = [(*x[:3], () if set(x[3]) == set(g.devices) else x[3]) if len(x) > 3 else x for x in new]
    rep = GroupReport(g, "готово")
    for r, (name, *_) in zip(users.add_many(new, g.id) if new else [], new):
        if r.ok:
            rep.created.append(name)
            rep.needs_qr.append(name)
        else:
            rep.errors += [f"{name}: {r.message}"] + [f"{name}: {s.proto_id}: {s.error}" for s in r.failed]
    if existing:
        mv = move_many(existing, g.id)
        rep.moved, rep.skipped, rep.allow, rep.resend = mv.moved, mv.skipped, mv.allow, mv.resend
        rep.errors += mv.errors
        rep.needs_qr += [n for n in mv.needs_qr if n not in rep.needs_qr]
    return rep


def connect(name: str, protocols: list[str], clients: dict[str, Any] | None,
            allow: dict[str, list[str]] | None, new: list[tuple[str, ...]], existing: list[str],
            install_mode: str = "self") -> GroupReport:
    """Мастер «Подключить людей»: группа + пользователи, всё проверено до первого изменения.
    Исключение при добавлении людей не бросается наружу (rep.crashed): отчёт с ошибками и тем, что
    успело примениться. Если не добавлен никто, созданная пустая группа удаляется (rep.removed):
    повтор мастера с тем же названием не должен упереться в «уже есть». Обычные отказы по отдельным
    людям (rep.errors без исключения) группу оставляют — её видно на странице группы."""
    if not new and not existing:
        raise GroupError("добавьте хотя бы одного пользователя")
    check_members(new, existing)
    g = create(name, protocols, clients, allow, install_mode)
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
        rep.created = [n for n, *_ in new if n in have]
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
                     "свой" if g.allowlist else "общий", g.install_mode, ", ".join(names) or "—"])
    if args.json:
        output.print_json({"path": str(gs.path), "groups": data})
        return 0
    if not rows:
        output.warn("групп нет: реестр пользователей ещё не создан (zoo setup)")
        return 0
    print(output.table(rows, ["id", "название", "протоколы", "клиенты", "приложения", "ставит", "участники"]))
    return 0


def cmd_group_add(args: argparse.Namespace, cfg: Any) -> int:
    allow = None
    if args.allow:
        allow = _allow_lists(args.allow, allowlist.Allowlist.load())
    g = create(args.name, [ALL] if args.all_protocols or not args.proto else args.proto,
               _parse_clients(args.client), allow, args.install or "self")
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
    rep = update(args.group, args.name, protos, clients, allow, args.install, drop=args.drop_proto)
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
