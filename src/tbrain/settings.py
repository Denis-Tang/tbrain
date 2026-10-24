"""Machine-local settings stored in ``app_home/settings.json``.

The file is plain JSON so users can inspect/edit it.  Reading is tolerant: unknown
keys (including fields from older versions) are preserved on disk but ignored when
building the :class:`Settings` object.  Writing is atomic (temporary sibling plus
``os.replace``).

Settings carry no secrets: model credentials belong to the OS credential store, a
different concern than this module.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

try:  # tzlocal is a declared dependency; degrade gracefully if it is absent.
    import tzlocal
except ImportError:  # pragma: no cover - defensive
    tzlocal = None  # type: ignore[assignment]


FALLBACK_TIMEZONE = "Asia/Shanghai"


def default_home() -> Path:
    """The application data directory (kept for backwards-compatible imports)."""
    from .paths import app_home

    return app_home()


def default_vault_path() -> Path:
    """Default knowledge-base location; the user can change it in settings."""
    configured = os.environ.get("TBRAIN_VAULT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "TbrainVault"


def available_timezones_list() -> list[str]:
    """Sorted IANA names for the settings page drop-down."""
    return sorted(available_timezones())


def system_timezone() -> str:
    """Best-effort name of the operating system time zone."""
    if tzlocal is not None:
        try:
            name = tzlocal.get_localzone_name()
        except Exception:  # pragma: no cover - platform dependent
            name = ""
        if name and valid_timezone(name):
            return name
    return FALLBACK_TIMEZONE


def default_timezone() -> str:
    return system_timezone()


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(str(name))
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def validate_timezone(name: str) -> str:
    """Return the IANA name unchanged or raise a user-facing ValueError."""
    text = str(name or "").strip()
    if not text:
        raise ValueError("请选择时区")
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError:
        raise ValueError(f"无效的时区名称：{text}") from None
    except (ValueError, OSError):
        raise ValueError(f"无效的时区名称：{text}") from None
    return text


@dataclass
class Settings:
    """The six fixed settings keys; an empty string means "not configured"."""

    vault_path: str = ""
    timezone: str = ""
    ntp_enabled: bool = True
    timeout_hours: float = 6.0
    reminder_context_switch: bool = True
    install_root: str = ""

    @property
    def resolved_timezone(self) -> str:
        return self.timezone or system_timezone()

    def as_dict(self) -> dict:
        return asdict(self)


KNOWN_KEYS = tuple(field.name for field in fields(Settings))
_BOOL_KEYS = {"ntp_enabled", "reminder_context_switch"}
_EXPAND_KEYS = {"vault_path", "install_root"}


def _coerce(key: str, value):
    """Validate one settings value and return the normalized form."""
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise ValueError("设置项必须是布尔值")
        return value
    if key == "timeout_hours":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("超时小时数必须是数字")
        number = float(value)
        if number < 0 or number > 24 * 30:
            raise ValueError("超时小时数须在 0 到 720 之间")
        return number
    if key == "timezone":
        text = str(value or "").strip()
        return validate_timezone(text) if text else ""
    if key in _EXPAND_KEYS:
        text = str(value or "").strip()
        if not text:
            return ""
        path = Path(text).expanduser()
        if key == "vault_path" and path.exists() and not path.is_dir():
            raise ValueError("请选择文件夹路径")
        return str(path)
    text = str(value or "")
    return text


def normalize(values: dict, base: dict | None = None) -> dict:
    """Merge ``values`` over ``base`` (a dict), validating only known keys."""
    merged = {key: (base or {}).get(key, getattr(Settings(), key)) for key in KNOWN_KEYS}
    unknown = [key for key in values if key not in KNOWN_KEYS]
    if unknown:
        raise ValueError("包含不支持的设置项：" + "、".join(sorted(unknown)))
    for key, value in values.items():
        merged[key] = _coerce(key, value)
    return merged


def settings_from(values: dict) -> Settings:
    """Build Settings from a raw JSON dict, ignoring unknown/legacy keys."""
    merged = normalize({key: values[key] for key in KNOWN_KEYS if key in values})
    return Settings(**merged)


def load_settings(home: Path | None = None) -> Settings:
    return SettingsStore(home).load()


def save_settings(home: Path | None, values: dict) -> Settings:
    return SettingsStore(home).save(values)


class SettingsStore:
    """Read/write ``<home>/settings.json``; the store keeps the raw dict for compat."""

    def __init__(self, home: Path | None = None):
        from .paths import app_home

        self.home = Path(home) if home is not None else app_home()
        self.path = self.home / "settings.json"

    # -- reading -----------------------------------------------------------
    def read_raw(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def load(self) -> Settings:
        return settings_from(self.read_raw())

    def as_dict(self, settings: Settings | None = None) -> dict:
        return (settings or self.load()).as_dict()

    # -- writing -----------------------------------------------------------
    def save(self, values: dict) -> Settings:
        raw = self.read_raw()
        merged = normalize(values, raw)
        record = dict(raw)  # preserve unknown/legacy keys on disk untouched
        record.update(merged)
        self._write(record)
        return Settings(**merged)

    # Older services called this ``update``; keep the name working.
    def update(self, values: dict, api_key: str | None = None) -> Settings:
        if api_key is not None:
            raise ValueError("此版本不再保存模型密钥，请使用系统凭据存储")
        return self.save(values)

    def _write(self, record: dict) -> None:
        path = self.path
        temporary = path.with_name(path.name + ".tmp")
        text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
