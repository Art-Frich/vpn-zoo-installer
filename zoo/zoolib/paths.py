"""Пути инсталлера. Те же имена переменных окружения, что в scripts/lib.sh:
VPN_ETC, CONFIG_FILE, MANIFEST_DIR, PORTS_FILE — тесты и стенд подменяют их через окружение.
Функции, а не константы: окружение читается при каждом вызове."""

from __future__ import annotations

import os
from pathlib import Path

# Каталог пакета zoo/ (рядом с ним при установке лежит scripts/)
ZOO_PKG_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def etc() -> Path:
    return _env_path("VPN_ETC", Path("/etc/vpn-setup"))


def config_file() -> Path:
    return _env_path("CONFIG_FILE", etc() / "config.env")


def manifest_dir() -> Path:
    return _env_path("MANIFEST_DIR", etc() / "protocols.d")


def ports_file() -> Path:
    return _env_path("PORTS_FILE", etc() / "ports.tsv")


def users_file() -> Path:
    return _env_path("ZOO_USERS_FILE", etc() / "users.json")


def clients_dir() -> Path:
    """Файлы пользователей: /etc/vpn-setup/clients/<name>/ (.conf AWG и т. п.)."""
    return _env_path("ZOO_CLIENTS_DIR", etc() / "clients")


def probe_export_file() -> Path:
    return _env_path("ZOO_PROBE_EXPORT", etc() / "probe-export.json")


def xui_hdr_file() -> Path:
    return _env_path("XUI_HDR_FILE", etc() / "xui-auth.hdr")


def zoo_home() -> Path:
    """Каталог установки (фаза 09): /opt/vpn-zoo/{zoo,scripts}."""
    return _env_path("ZOO_HOME", Path("/opt/vpn-zoo"))


def state_dir() -> Path:
    """Данные zoo: история трафика (SQLite), токен веб-админки и т. п."""
    return _env_path("ZOO_STATE_DIR", Path("/var/lib/vpn-zoo"))


def lock_file() -> Path:
    """Блокировка изменяющих операций zoo (пользователи, конфиги протоколов)."""
    default = Path("/run/lock/vpn-zoo.lock")
    if not default.parent.is_dir():
        default = state_dir() / "zoo.lock"
    return _env_path("ZOO_LOCK_FILE", default)


def config_lock_file() -> Path:
    """Тот же путь, что _config_lock_path в lib.sh: config_set из bash и Python не пересекаются."""
    run_lock = Path("/run/lock")
    if run_lock.is_dir() and os.access(run_lock, os.W_OK):
        return run_lock / "vpn-setup-config.lock"
    return Path(str(config_file()) + ".lock")


def scripts_dir() -> Path:
    """Каталог scripts/ с lib.sh и lib/proto-<id>.sh.

    Порядок: ZOO_SCRIPTS_DIR → scripts/ рядом с пакетом (установка /opt/vpn-zoo
    или рабочая копия репо) → ZOO_HOME/scripts.
    """
    override = os.environ.get("ZOO_SCRIPTS_DIR")
    if override:
        return Path(override)
    sibling = ZOO_PKG_ROOT.parent / "scripts"
    if (sibling / "lib.sh").is_file():
        return sibling
    return zoo_home() / "scripts"
