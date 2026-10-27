"""时间工具：UTC 为唯一真实时间，按用户时区显示，可选联网校时。

契约见 ``docs/ARCHITECTURE.md`` 第 3 节：

- 持久化一律 UTC；显示按 ``settings.timezone``（空则用系统时区）。
- 联网校时用 socket 实现 SNTP，失败退回 HTTPS 响应头的 ``Date``。
- 成功校时的偏差缓存到 ``<app_home>/time.json``，有效期 6 小时。
- 断网或关闭校时：用系统时间并标记 ``calibrated=False``。

测试通过 keyword-only 的 ``ntp_fetcher`` / ``http_fetcher`` / ``system_time``
注入假数据，不会真实联网。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time as _time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

UTC = timezone.utc

CACHE_FILENAME = "time.json"
CACHE_TTL_SECONDS = 6 * 3600
FAILURE_RETRY_SECONDS = 60
NTP_TIMEOUT_SECONDS = 1.5
HTTPS_TIMEOUT_SECONDS = 3.0
NTP_SERVERS = ("ntp.aliyun.com", "pool.ntp.org", "time.windows.com")
HTTPS_DATE_URLS = ("https://www.baidu.com", "https://www.cloudflare.com")
NTP_EPOCH_DELTA = 2_208_988_800  # 1900-01-01 到 1970-01-01 的秒数
CACHE_SCHEMA_VERSION = 1
DRIFT_WARNING_SECONDS = 120.0

TimeFetcher = Callable[[], float]


@dataclass
class Now:
    """一次“当前时间”查询的结果。"""

    utc: datetime
    local: datetime
    timezone: str
    calibrated: bool
    source: str
    offset_seconds: float

    def as_dict(self) -> dict:
        """供桌面/CLI 序列化的稳定结构。"""
        return {
            "utc": iso_utc(self.utc),
            "local": self.local.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "display": display(self),
            "timezone": self.timezone,
            "calibrated": self.calibrated,
            "source": self.source,
            "offset_seconds": self.offset_seconds,
        }


# ---------------------------------------------------------------------------
# 设置与时间源


def _setting(settings: Any, key: str, default: Any = None) -> Any:
    """设置可能是 dataclass 也可能是 dict（流程层两者都会出现）。"""
    if settings is None:
        return default
    if isinstance(settings, dict):
        value = settings.get(key, default)
    else:
        value = getattr(settings, key, default)
    return default if value is None else value


def system_timezone() -> str:
    """系统 IANA 时区；探测失败时退回 UTC。"""
    try:
        from tzlocal import get_localzone_name

        name = get_localzone_name()
        if name:
            return name
    except Exception:
        pass
    name = (_time.tzname[0] or "").strip()
    return name or "UTC"


def validate_timezone(tz: str) -> str:
    """返回合法的 IANA 时区名；非法时抛 :class:`ValueError`。"""
    name = (tz or "").strip()
    if not name:
        raise ValueError("时区不能为空")
    try:
        ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError
        raise ValueError(f"无效的时区：{name}") from exc
    return name


def resolve_timezone(name: str = "") -> tuple[str, ZoneInfo]:
    """时区名 -> (名称, ZoneInfo)；空或非法名退回系统时区。"""
    candidate = (name or "").strip()
    if candidate:
        try:
            return candidate, ZoneInfo(candidate)
        except Exception:
            pass
    fallback = system_timezone()
    try:
        return fallback, ZoneInfo(fallback)
    except Exception:
        return "UTC", ZoneInfo("UTC")


def set_timezone(settings: Any, tz: str) -> None:
    """校验并写入设置对象（dataclass 或 dict）。持久化由设置存储层负责。"""
    name = validate_timezone(tz)
    if isinstance(settings, dict):
        settings["timezone"] = name
        return
    setattr(settings, "timezone", name)


# ---------------------------------------------------------------------------
# 序列化


def iso_utc(dt: datetime) -> str:
    """UTC ISO-8601（秒精度，``Z`` 后缀），用于所有落盘时间。"""
    if not isinstance(dt, datetime):
        raise TypeError("iso_utc 需要 datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    """解析 ISO-8601 时间并转为 aware UTC；无时区信息按 UTC 处理。"""
    value = (text or "").strip()
    if not value:
        raise ValueError("时间文本不能为空")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"无法解析的时间：{text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _dt_to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _epoch_to_utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(float(seconds), UTC)


def display(now: Now) -> str:
    """中文可读串，例：``2026-10-24 21:55:22 (Asia/Shanghai, 已校准)``。"""
    stamp = now.local.strftime("%Y-%m-%d %H:%M:%S")
    state = "已校准" if now.calibrated else "未校准"
    return f"{stamp} ({now.timezone}, {state})"


def drift_warning(now: Now) -> str:
    """系统时间与网络时间偏差超过 2 分钟时返回提醒，否则返回空串。"""
    if not now.calibrated:
        return ""
    drift = abs(float(now.offset_seconds or 0.0))
    if drift <= DRIFT_WARNING_SECONDS:
        return ""
    minutes = int(round(drift / 60))
    return f"系统时间与网络时间相差约 {minutes} 分钟，请检查系统时间设置。"


# ---------------------------------------------------------------------------
# 校时缓存


def _cache_path(app_home: Path | None) -> Path | None:
    if app_home is None:
        return None
    return Path(app_home) / CACHE_FILENAME


def read_cache(app_home: Path | None) -> dict | None:
    """读取 time.json 缓存；文件缺失或损坏返回 None。"""
    path = _cache_path(app_home)
    if path is None or not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(app_home: Path | None, payload: dict) -> None:
    path = _cache_path(app_home)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _cache_time(cache: dict | None, key: str) -> float | None:
    if not cache:
        return None
    raw = cache.get(key)
    if not raw:
        return None
    try:
        return _dt_to_epoch(parse_utc(str(raw)))
    except ValueError:
        return None


def _cache_is_valid(cache: dict | None, sys_ts: float) -> bool:
    if not cache or not cache.get("calibrated"):
        return False
    expires = _cache_time(cache, "expires_at")
    if expires is None:
        checked = _cache_time(cache, "checked_at")
        if checked is None:
            return False
        expires = checked + CACHE_TTL_SECONDS
    return sys_ts < expires


def _recent_attempt(cache: dict | None, sys_ts: float) -> bool:
    """失败后 60 秒内不重复联网，避免每次 now() 都卡在网络超时上。"""
    if not cache:
        return False
    checked = _cache_time(cache, "checked_at")
    if checked is None:
        return False
    return 0 <= sys_ts - checked < FAILURE_RETRY_SECONDS


def _build_payload(
    *,
    sys_ts: float,
    calibrated: bool,
    source: str,
    offset: float,
    ntp_enabled: bool,
    cached: bool = False,
    errors: list[str] | None = None,
    previous: dict | None = None,
) -> dict:
    checked = _epoch_to_utc(sys_ts)
    last_offset = float((previous or {}).get("offset_seconds") or 0.0)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "checked_at": iso_utc(checked),
        "expires_at": iso_utc(checked + timedelta(seconds=CACHE_TTL_SECONDS)),
        "calibrated": bool(calibrated),
        "source": source,
        "offset_seconds": float(offset) if calibrated else 0.0,
        "last_offset_seconds": float(offset) if calibrated else last_offset,
        "ntp_enabled": bool(ntp_enabled),
        "cached": bool(cached),
        "errors": list(errors or []),
    }


# ---------------------------------------------------------------------------
# 网络时间源（默认实现；测试注入替代）


def _default_ntp_fetch() -> float:
    """SNTP（UDP 123）轮询多个服务器，返回网络 Unix 时间戳。"""
    request = b"\x1b" + b"\x00" * 47  # LI=0, VN=3, Mode=3(client)
    errors: list[str] = []
    for server in NTP_SERVERS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(NTP_TIMEOUT_SECONDS)
                sock.sendto(request, (server, 123))
                data, _ = sock.recvfrom(48)
            if len(data) < 48:
                errors.append(f"{server}: 响应过短")
                continue
            seconds, fraction = struct.unpack("!II", data[40:48])
            if seconds == 0:
                errors.append(f"{server}: 时间为 0")
                continue
            return float(seconds - NTP_EPOCH_DELTA) + float(fraction) / 2**32
        except OSError as exc:
            errors.append(f"{server}: {exc}")
    raise OSError("NTP 校时失败：" + "；".join(errors or ["无可用服务器"]))


def _default_http_fetch() -> float:
    """HTTPS 响应头 ``Date`` 兜底，返回网络 Unix 时间戳。"""
    import httpx

    errors: list[str] = []
    for url in HTTPS_DATE_URLS:
        try:
            with httpx.Client(timeout=HTTPS_TIMEOUT_SECONDS, follow_redirects=True) as client:
                response = client.get(url)
            raw = response.headers.get("date")
            if not raw:
                errors.append(f"{url}: 无 Date 响应头")
                continue
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except Exception as exc:  # 网络/解析错误都退回下一个源
            errors.append(f"{url}: {exc}")
    raise OSError("HTTPS Date 校时失败：" + "；".join(errors or ["无可用地址"]))


# ---------------------------------------------------------------------------
# 校时与当前时间


def sync(
    app_home: Path | None,
    settings: Any = None,
    force: bool = False,
    *,
    ntp_fetcher: TimeFetcher | None = None,
    http_fetcher: TimeFetcher | None = None,
    system_time: float | None = None,
) -> dict:
    """校时：SNTP -> HTTPS Date。成功写缓存；失败/关闭标未校准。"""
    sys_ts = float(system_time if system_time is not None else _time.time())
    ntp_enabled = bool(_setting(settings, "ntp_enabled", True))
    cache = read_cache(app_home)

    if not force and _cache_is_valid(cache, sys_ts):
        payload = dict(cache)  # type: ignore[arg-type]
        payload["cached"] = True
        return payload

    if not ntp_enabled:
        payload = _build_payload(
            sys_ts=sys_ts,
            calibrated=False,
            source="system",
            offset=0.0,
            ntp_enabled=False,
            errors=["联网校时已关闭"],
            previous=cache,
        )
        _write_cache(app_home, payload)
        return payload

    errors: list[str] = []
    fetch_ntp = ntp_fetcher or _default_ntp_fetch
    fetch_http = http_fetcher or _default_http_fetch
    try:
        network_ts = float(fetch_ntp())
        offset = network_ts - sys_ts
        payload = _build_payload(
            sys_ts=sys_ts,
            calibrated=True,
            source="ntp",
            offset=offset,
            ntp_enabled=True,
            errors=errors,
            previous=cache,
        )
        _write_cache(app_home, payload)
        return payload
    except Exception as exc:
        errors.append(f"NTP 失败：{exc}")

    try:
        network_ts = float(fetch_http())
        offset = network_ts - sys_ts
        payload = _build_payload(
            sys_ts=sys_ts,
            calibrated=True,
            source="https-date",
            offset=offset,
            ntp_enabled=True,
            errors=errors,
            previous=cache,
        )
        _write_cache(app_home, payload)
        return payload
    except Exception as exc:
        errors.append(f"HTTPS Date 失败：{exc}")

    payload = _build_payload(
        sys_ts=sys_ts,
        calibrated=False,
        source="system",
        offset=0.0,
        ntp_enabled=True,
        errors=errors,
        previous=cache,
    )
    _write_cache(app_home, payload)
    return payload


def now(
    app_home: Path | None,
    settings: Any = None,
    *,
    ntp_fetcher: TimeFetcher | None = None,
    http_fetcher: TimeFetcher | None = None,
    system_time: float | None = None,
) -> Now:
    """返回“当前时间”（校正后的 UTC + 用户时区显示）。"""
    sys_ts = float(system_time if system_time is not None else _time.time())
    ntp_enabled = bool(_setting(settings, "ntp_enabled", True))
    cache = read_cache(app_home)

    if app_home is None:
        payload: dict = {
            "calibrated": False,
            "source": "system",
            "offset_seconds": 0.0,
        }
    elif ntp_enabled and not _cache_is_valid(cache, sys_ts) and not _recent_attempt(cache, sys_ts):
        payload = sync(
            app_home,
            settings,
            force=False,
            ntp_fetcher=ntp_fetcher,
            http_fetcher=http_fetcher,
            system_time=sys_ts,
        )
    elif not ntp_enabled:
        payload = {"calibrated": False, "source": "system", "offset_seconds": 0.0}
    else:
        payload = dict(cache or {"calibrated": False, "source": "system", "offset_seconds": 0.0})

    calibrated = bool(payload.get("calibrated"))
    offset = float(payload.get("offset_seconds") or 0.0) if calibrated else 0.0
    utc_dt = _epoch_to_utc(sys_ts + offset)

    tz_name, zone = resolve_timezone(str(_setting(settings, "timezone", "") or ""))
    local_dt = utc_dt.astimezone(zone)
    return Now(
        utc=utc_dt,
        local=local_dt,
        timezone=tz_name,
        calibrated=calibrated,
        source=str(payload.get("source") or "system"),
        offset_seconds=offset,
    )


def utc_now(app_home: Path | None = None) -> datetime:
    """只要有可信缓存就用校正后的当前 UTC（不联网），供写文件/索引时间戳使用。"""
    sys_ts = _time.time()
    cache = read_cache(app_home)
    if _cache_is_valid(cache, sys_ts):
        offset = float(cache.get("offset_seconds") or 0.0)  # type: ignore[union-attr]
        return _epoch_to_utc(sys_ts + offset)
    return _epoch_to_utc(sys_ts)
