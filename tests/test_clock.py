"""clock.py 测试：UTC 存储、时区显示、校时缓存、断网降级（全部注入假时间源，不联网）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tbrain import clock

UTC = timezone.utc
FIXED_TS = 1_700_000_000.0  # 2023-11-14T22:13:20Z
# FIXED_TS + 120s = 2023-11-14T22:15:20Z -> Asia/Shanghai 2023-11-15 06:15:20
FIXED_UTC = datetime.fromtimestamp(FIXED_TS, UTC)


def settings(**overrides):
    values = {"timezone": "Asia/Shanghai", "ntp_enabled": True}
    values.update(overrides)
    return values


def _no_network():
    raise AssertionError("测试不应发起真实网络请求")


def _cache_file(app_home):
    return app_home / "time.json"


# ---------------------------------------------------------------------------
# 时区与序列化


def test_validate_timezone_accepts_iana_and_rejects_bad_values():
    assert clock.validate_timezone("Asia/Shanghai") == "Asia/Shanghai"
    assert clock.validate_timezone("UTC") == "UTC"
    with pytest.raises(ValueError):
        clock.validate_timezone("Mars/Olympus")
    with pytest.raises(ValueError):
        clock.validate_timezone("")
    with pytest.raises(ValueError):
        clock.validate_timezone("../../etc/passwd")


def test_set_timezone_mutates_dict_and_object():
    values = settings()
    clock.set_timezone(values, "UTC")
    assert values["timezone"] == "UTC"

    class Dummy:
        timezone = "Asia/Shanghai"

    dummy = Dummy()
    clock.set_timezone(dummy, "Europe/Berlin")
    assert dummy.timezone == "Europe/Berlin"
    with pytest.raises(ValueError):
        clock.set_timezone(dummy, "Not/AZone")


def test_iso_utc_and_parse_utc_round_trip(tmp_path):
    assert clock.iso_utc(FIXED_UTC) == "2023-11-14T22:13:20Z"
    assert clock.iso_utc(FIXED_UTC.astimezone(timezone(timedelta(hours=8)))) == "2023-11-14T22:13:20Z"
    assert clock.parse_utc("2023-11-14T22:13:20Z") == FIXED_UTC
    assert clock.parse_utc("2023-11-15T06:13:20+08:00") == FIXED_UTC
    # 无时区信息按 UTC
    assert clock.parse_utc("2023-11-14T22:13:20") == FIXED_UTC
    with pytest.raises(ValueError):
        clock.parse_utc("")
    with pytest.raises(ValueError):
        clock.parse_utc("昨天下午")


def test_display_marks_calibrated_and_uncalibrated(tmp_path):
    calibrated = clock.Now(
        utc=FIXED_UTC + timedelta(seconds=120),
        local=(FIXED_UTC + timedelta(seconds=120)).astimezone(clock.ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        calibrated=True,
        source="ntp",
        offset_seconds=120.0,
    )
    assert clock.display(calibrated) == "2023-11-15 06:15:20 (Asia/Shanghai, 已校准)"
    assert clock.drift_warning(calibrated) == ""  # 正好 2 分钟不提醒
    uncalibrated = clock.Now(
        utc=FIXED_UTC,
        local=FIXED_UTC.astimezone(clock.ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        calibrated=False,
        source="system",
        offset_seconds=0.0,
    )
    assert "未校准" in clock.display(uncalibrated)
    assert clock.drift_warning(uncalibrated) == ""


def test_drift_warning_for_large_offset():
    now = clock.Now(
        utc=FIXED_UTC,
        local=FIXED_UTC.astimezone(clock.ZoneInfo("Asia/Shanghai")),
        timezone="Asia/Shanghai",
        calibrated=True,
        source="ntp",
        offset_seconds=300.0,
    )
    assert "5 分钟" in clock.drift_warning(now)


# ---------------------------------------------------------------------------
# 校时与缓存


def test_now_uses_ntp_and_converts_to_local_timezone(tmp_path):
    calls = []

    def fetcher():
        calls.append(1)
        return FIXED_TS + 120.0

    current = clock.now(
        tmp_path,
        settings(),
        ntp_fetcher=fetcher,
        http_fetcher=_no_network,
        system_time=FIXED_TS,
    )
    assert calls == [1]
    assert current.calibrated is True
    assert current.source == "ntp"
    assert current.offset_seconds == pytest.approx(120.0)
    assert current.utc == FIXED_UTC + timedelta(seconds=120)
    assert current.timezone == "Asia/Shanghai"
    assert current.local.strftime("%Y-%m-%d %H:%M:%S") == "2023-11-15 06:15:20"
    assert _cache_file(tmp_path).exists()


def test_now_respects_utc_setting(tmp_path):
    current = clock.now(
        tmp_path,
        settings(timezone="UTC"),
        ntp_fetcher=lambda: FIXED_TS,
        system_time=FIXED_TS,
    )
    assert current.local == current.utc
    assert current.local.strftime("%Y-%m-%d %H:%M:%S") == "2023-11-14 22:13:20"


def test_cache_valid_for_six_hours_and_avoids_network(tmp_path):
    calls = []

    def first_fetcher():
        calls.append(1)
        return FIXED_TS + 60.0

    clock.now(tmp_path, settings(), ntp_fetcher=first_fetcher, system_time=FIXED_TS)
    cache = json.loads(_cache_file(tmp_path).read_text(encoding="utf-8"))
    assert cache["calibrated"] is True
    assert cache["source"] == "ntp"
    assert cache["offset_seconds"] == pytest.approx(60.0)

    before_expiry = FIXED_TS + 6 * 3600 - 10

    def exploding_fetcher():
        raise AssertionError("缓存有效期内不应联网")

    current = clock.now(
        tmp_path,
        settings(),
        ntp_fetcher=exploding_fetcher,
        http_fetcher=_no_network,
        system_time=before_expiry,
    )
    assert len(calls) == 1
    assert current.calibrated is True
    assert current.offset_seconds == pytest.approx(60.0)


def test_cache_expires_after_six_hours_and_syncs_again(tmp_path):
    calls = []

    def fetcher():
        calls.append(1)
        return FIXED_TS + 60.0

    clock.sync(tmp_path, settings(), ntp_fetcher=fetcher, system_time=FIXED_TS)
    clock.sync(
        tmp_path,
        settings(),
        ntp_fetcher=fetcher,
        system_time=FIXED_TS + 6 * 3600 + 1,
    )
    assert len(calls) == 2


def test_https_date_fallback_when_ntp_fails(tmp_path):
    def broken_ntp():
        raise OSError("网络不可达")

    current = clock.now(
        tmp_path,
        settings(),
        ntp_fetcher=broken_ntp,
        http_fetcher=lambda: FIXED_TS - 30.0,
        system_time=FIXED_TS,
    )
    assert current.calibrated is True
    assert current.source == "https-date"
    assert current.offset_seconds == pytest.approx(-30.0)


def test_both_sources_fail_use_system_time_uncalibrated(tmp_path):
    def broken():
        raise OSError("断网")

    current = clock.now(
        tmp_path,
        settings(),
        ntp_fetcher=broken,
        http_fetcher=broken,
        system_time=FIXED_TS,
    )
    assert current.calibrated is False
    assert current.source == "system"
    assert current.offset_seconds == 0.0
    assert current.utc == FIXED_UTC
    assert "未校准" in clock.display(current)
    cache = json.loads(_cache_file(tmp_path).read_text(encoding="utf-8"))
    assert cache["calibrated"] is False
    assert cache["errors"]


def test_ntp_disabled_never_touches_network_or_cache(tmp_path):
    current = clock.now(
        tmp_path,
        settings(ntp_enabled=False),
        ntp_fetcher=_no_network,
        http_fetcher=_no_network,
        system_time=FIXED_TS,
    )
    assert current.calibrated is False
    assert current.source == "system"
    assert current.utc == FIXED_UTC
    assert not _cache_file(tmp_path).exists()
    # force 也不联网，只写未校准缓存
    state = clock.sync(
        tmp_path,
        settings(ntp_enabled=False),
        force=True,
        ntp_fetcher=_no_network,
        http_fetcher=_no_network,
        system_time=FIXED_TS,
    )
    assert state["calibrated"] is False
    assert state["source"] == "system"
    assert state["ntp_enabled"] is False
    assert _cache_file(tmp_path).exists()


def test_sync_force_bypasses_cache(tmp_path):
    calls = []

    def fetcher():
        calls.append(1)
        return FIXED_TS

    clock.sync(tmp_path, settings(), ntp_fetcher=fetcher, system_time=FIXED_TS)
    clock.sync(tmp_path, settings(), force=True, ntp_fetcher=fetcher, system_time=FIXED_TS + 10)
    assert len(calls) == 2


def test_sync_returns_payload_shape(tmp_path):
    payload = clock.sync(
        tmp_path,
        settings(),
        ntp_fetcher=lambda: FIXED_TS + 5.0,
        system_time=FIXED_TS,
    )
    for key in ("calibrated", "source", "offset_seconds", "checked_at", "expires_at", "ntp_enabled"):
        assert key in payload
    assert payload["checked_at"] == "2023-11-14T22:13:20Z"
    assert payload["expires_at"] == "2023-11-15T04:13:20Z"
    assert payload["cached"] is False


def test_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "time.json").write_text("{not json", encoding="utf-8")
    current = clock.now(
        tmp_path,
        settings(),
        ntp_fetcher=lambda: FIXED_TS,
        system_time=FIXED_TS,
    )
    assert current.calibrated is True


def test_now_without_app_home_stays_uncalibrated():
    current = clock.now(
        None,
        settings(),
        ntp_fetcher=_no_network,
        http_fetcher=_no_network,
        system_time=FIXED_TS,
    )
    assert current.calibrated is False
    assert current.utc == FIXED_UTC


def test_utc_now_applies_valid_cached_offset(tmp_path):
    real_now = datetime.now(UTC)
    cache = {
        "calibrated": True,
        "source": "ntp",
        "offset_seconds": 120.0,
        "checked_at": clock.iso_utc(real_now),
        "expires_at": clock.iso_utc(real_now + timedelta(hours=6)),
    }
    _cache_file(tmp_path).write_text(json.dumps(cache), encoding="utf-8")
    value = clock.utc_now(tmp_path)
    delta = (value - datetime.now(UTC)).total_seconds()
    assert 110 <= delta <= 130


def test_utc_now_ignores_expired_cache(tmp_path):
    real_now = datetime.now(UTC)
    cache = {
        "calibrated": True,
        "source": "ntp",
        "offset_seconds": 120.0,
        "checked_at": clock.iso_utc(real_now - timedelta(hours=7)),
        "expires_at": clock.iso_utc(real_now - timedelta(hours=1)),
    }
    _cache_file(tmp_path).write_text(json.dumps(cache), encoding="utf-8")
    delta = (clock.utc_now(tmp_path) - datetime.now(UTC)).total_seconds()
    assert -5 <= delta <= 5


def test_now_falls_back_to_system_timezone_when_setting_empty(tmp_path):
    current = clock.now(
        tmp_path,
        settings(timezone="", ntp_enabled=False),
        system_time=FIXED_TS,
    )
    assert current.timezone
    assert current.local.tzinfo is not None
    assert not current.calibrated


def test_now_tolerates_invalid_configured_timezone(tmp_path):
    current = clock.now(
        tmp_path,
        settings(timezone="Mars/Olympus", ntp_enabled=False),
        system_time=FIXED_TS,
    )
    assert current.timezone != "Mars/Olympus"
