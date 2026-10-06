"""Манифесты протоколов /etc/vpn-setup/protocols.d/<id>.json (ARCHITECTURE §4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LAYERS = ("tcp", "udp", "tcp+udp")
PROBE_KINDS = ("xray", "hysteria", "awg", "sing-box")
# users_backend: кто хранит пользователей (xui | hysteria | awg | ...); none — протокол без
# пользователей. Значение не проверяется строго: zoo работает через proto-<id>.sh
LAYER_ALIASES = {"tcp,udp": "tcp+udp", "udp+tcp": "tcp+udp", "tcp/udp": "tcp+udp", "both": "tcp+udp"}


def norm_layer(value: Any) -> Any:
    return LAYER_ALIASES.get(value, value) if isinstance(value, str) else value


@dataclass
class Manifest:
    id: str
    name: str
    layer: str
    port: int
    engine: str = ""
    service: str = ""
    enabled: bool = True
    users_backend: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    probe: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def short(self) -> str:
        """Короткое имя для карточек: поле short, иначе name до « (»."""
        s = self.raw.get("short")
        return s if isinstance(s, str) and s.strip() else self.name.partition(" (")[0]

    @property
    def services(self) -> list[str]:
        """service может быть строкой или списком юнитов."""
        s = self.raw.get("service", self.service)
        if isinstance(s, list):
            return [str(x) for x in s if x]
        return [s] if s else []

    @property
    def protos(self) -> list[str]:
        """Транспорты порта для проверки listen: tcp+udp → [tcp, udp]."""
        return self.layer.split("+")

    @property
    def phase(self) -> str:
        """Фаза install.sh, которой принадлежит протокол (вкл/выкл с карточки). Пусто — не задана."""
        v = self.raw.get("phase")
        return v if isinstance(v, str) else ""

    @property
    def enable_var(self) -> str:
        """Ключ config.env, включающий протокол (ENABLE_*). Пусто — не задан."""
        v = self.raw.get("enable_var")
        return v if isinstance(v, str) else ""

    @property
    def has_users(self) -> bool:
        return self.users_backend != "none"

    def links_for(self, user: str, enabled_only: bool = True) -> list[str]:
        """Ссылки пользователя; запись с enabled=false (пользователь отключён) — только при
        enabled_only=False (проверка «есть ли пользователь в протоколе»)."""
        return [str(x.get("uri", "")) for x in self.links if x.get("user") == user and x.get("uri")
                and (not enabled_only or x.get("enabled") is not False)]

    def files_for(self, user: str, enabled_only: bool = True) -> list[str]:
        return [str(x.get("path", "")) for x in self.files if x.get("user") == user and x.get("path")
                and (not enabled_only or x.get("enabled") is not False)]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass
class ManifestError:
    id: str
    path: str
    errors: list[str]


def validate(data: Any, file_id: str | None = None) -> list[str]:
    """Список ошибок схемы; пустой — манифест годен."""
    if not isinstance(data, dict):
        return ["манифест — не JSON-объект"]
    errs: list[str] = []
    mid = data.get("id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        errs.append(f"id: ожидается [a-z0-9-], получено {mid!r}")
    elif file_id is not None and mid != file_id:
        errs.append(f"id {mid!r} не совпадает с именем файла {file_id!r}")
    if not isinstance(data.get("name"), str) or not data.get("name"):
        errs.append("name: нужна непустая строка")
    if "short" in data and not isinstance(data["short"], str):
        errs.append("short: ожидается строка")
    if norm_layer(data.get("layer")) not in LAYERS:
        errs.append(f"layer: одно из {', '.join(LAYERS)}, получено {data.get('layer')!r}")
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        errs.append(f"port: целое 1..65535, получено {port!r}")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        errs.append("enabled: ожидается true/false")
    if not isinstance(data.get("users_backend", ""), str):
        errs.append("users_backend: ожидается строка")
    service = data.get("service", "")
    if not isinstance(service, (str, list)):
        errs.append("service: строка или список")
    for key, inner in (("links", ("user", "uri")), ("files", ("user", "path"))):
        items = data.get(key, [])
        if not isinstance(items, list) or not all(
            isinstance(x, dict) and all(isinstance(x.get(k), str) for k in inner) for x in items
        ):
            errs.append(f"{key}: список объектов {{{', '.join(inner)}}}")
    # probe: null или {} — пока нет пользователя, для которого его собрать
    probe = data.get("probe")
    if probe:
        if not isinstance(probe, dict) or probe.get("kind") not in PROBE_KINDS:
            errs.append(f"probe.kind: одно из {', '.join(PROBE_KINDS)}")
    return errs


def from_dict(data: dict[str, Any], path: Path | None = None) -> Manifest:
    service = data.get("service", "")
    return Manifest(
        id=data["id"],
        name=data["name"],
        layer=norm_layer(data["layer"]),
        port=data["port"],
        engine=str(data.get("engine", "")),
        service=service if isinstance(service, str) else ",".join(service),
        enabled=data.get("enabled", True),
        users_backend=data.get("users_backend", ""),
        links=list(data.get("links", [])),
        files=list(data.get("files", [])),
        probe=dict(data.get("probe") or {}),
        notes=str(data.get("notes", "")),
        raw=data,
        path=path,
    )


def load_file(path: Path) -> Manifest:
    """Прочитать и проверить один файл. ValueError с перечнем ошибок, если негоден."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"невалидный JSON: {e}") from e
    errs = validate(data, path.stem)
    if errs:
        raise ValueError("; ".join(errs))
    return from_dict(data, path)


def load_all(directory: Path | None = None) -> tuple[list[Manifest], list[ManifestError]]:
    """Все манифесты по имени файла. Негодные не роняют загрузку — уходят во второй список."""
    directory = Path(directory) if directory else paths.manifest_dir()
    good: list[Manifest] = []
    bad: list[ManifestError] = []
    if not directory.is_dir():
        return good, bad
    for f in sorted(directory.glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            good.append(load_file(f))
        except (OSError, ValueError) as e:
            bad.append(ManifestError(id=f.stem, path=str(f), errors=[str(e)]))
    return good, bad


def enabled(directory: Path | None = None) -> list[Manifest]:
    return [m for m in load_all(directory)[0] if m.enabled]


def get(proto_id: str, directory: Path | None = None) -> Manifest | None:
    if not ID_RE.match(proto_id):
        return None
    f = (Path(directory) if directory else paths.manifest_dir()) / f"{proto_id}.json"
    if not f.is_file():
        return None
    return load_file(f)
