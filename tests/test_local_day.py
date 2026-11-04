"""One definition of "local day" (issue #81): config, helper, digests, rollup,
portfolio window. Fixtures are synthetic (acme); every GitHub timestamp in a
fixture carries an explicit offset unless the test pins the system zone."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hub import localtime
from hub.localtime import local_day

zoneinfo = pytest.importorskip("zoneinfo")
try:
    CR = zoneinfo.ZoneInfo("America/Costa_Rica")    # UTC-6, no DST
    NY = zoneinfo.ZoneInfo("America/New_York")      # DST: 2nd Sun Mar, 1st Sun Nov
except zoneinfo.ZoneInfoNotFoundError:              # Windows without tzdata
    pytest.skip("no tz database on this machine", allow_module_level=True)


@pytest.fixture
def system_tz(monkeypatch):
    """Pin the SYSTEM zone (POSIX TZ + tzset) and restore it afterwards."""
    prior = os.environ.get("TZ")

    def _set(posix: str) -> None:
        monkeypatch.setenv("TZ", posix)
        time.tzset()

    yield _set
    if prior is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prior
    time.tzset()


@pytest.fixture
def config_tz(tmp_path, monkeypatch):
    """Write ``[user] timezone`` to an isolated config.toml."""
    import hub.config as config_mod
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)

    def _set(name: str | None) -> None:
        if name is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f'[user]\ngithub_handle = ""\ntimezone = "{name}"\n',
                            encoding="utf-8")
    return _set


# ── config ──────────────────────────────────────────────────────────


class TestConfigTimezone:
    def test_parsed_from_user_section(self, config_tz):
        from hub.config import load_config
        config_tz("America/Costa_Rica")
        assert load_config().timezone == "America/Costa_Rica"

    def test_default_is_system_zone(self, config_tz):
        from hub.config import load_config
        config_tz(None)
        assert load_config().timezone == ""
        assert localtime.user_timezone() is None   # None = system zone

    def test_round_trip_and_omitted_when_unset(self, tmp_path, monkeypatch):
        import hub.config as config_mod
        from hub.config import HubConfig, _serialize_toml, load_config, save_config
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
        assert "timezone" not in _serialize_toml(HubConfig())
        save_config(HubConfig(timezone="America/New_York"))
        assert load_config().timezone == "America/New_York"

    def test_non_string_value_falls_back_with_warning(self, tmp_path, monkeypatch, caplog):
        import hub.config as config_mod
        from hub.config import load_config
        path = tmp_path / "config.toml"
        path.write_text("[user]\ntimezone = 5\n", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        with caplog.at_level(logging.WARNING, logger="hub"):
            assert load_config().timezone == ""
        assert any("timezone" in r.getMessage() for r in caplog.records)

    def test_unknown_name_falls_back_to_system_with_warning(self, config_tz, caplog):
        config_tz("Mars/Olympus_Mons")
        localtime.resolve_timezone.cache_clear()
        with caplog.at_level(logging.WARNING, logger="hub"):
            assert localtime.user_timezone() is None
        assert any("Mars/Olympus_Mons" in r.getMessage() for r in caplog.records)

    def test_no_tz_database_falls_back_to_system(self, monkeypatch, caplog):
        """Windows without tzdata: ZoneInfo raises for every key → system zone;
        "UTC" still resolves (it never needs the database)."""
        def _missing(key):
            raise zoneinfo.ZoneInfoNotFoundError(key)
        monkeypatch.setattr(zoneinfo, "ZoneInfo", _missing)
        localtime.resolve_timezone.cache_clear()
        try:
            with caplog.at_level(logging.WARNING, logger="hub"):
                assert localtime.resolve_timezone("America/New_York") is None
            assert caplog.records
            assert localtime.resolve_timezone("UTC") is timezone.utc
            # Bucketing still works on the system zone.
            assert local_day("2026-05-05T12:00:00+00:00", None)
        finally:
            localtime.resolve_timezone.cache_clear()


# ── the helper ──────────────────────────────────────────────────────


class TestLocalDay:
    def test_merged_0100_utc_is_previous_day_at_utc_minus_6(self):
        assert local_day("2026-05-05T01:00:00Z", CR) == "2026-05-04"
        assert local_day("2026-05-05T01:00:00+00:00", CR) == "2026-05-04"
        assert local_day("2026-05-05T01:00:00Z", timezone.utc) == "2026-05-05"

    def test_accepts_every_stored_clock(self, system_tz):
        system_tz("UTC")
        inst = datetime(2026, 5, 5, 1, 0, tzinfo=timezone.utc)
        ms = int(inst.timestamp() * 1000)
        for value in (inst, inst.timestamp(), ms, str(ms),
                      "2026-05-05T01:00:00",            # naive = system (UTC)
                      "2026-05-04T19:00:00-06:00"):
            assert local_day(value, CR) == "2026-05-04", value
        for bad in (None, "", "garbage", True):
            assert local_day(bad, CR) is None

    def test_naive_values_are_system_local(self, system_tz):
        """github.db stores system-local naive: at a UTC-6 system, a naive
        20:00 is 02:00 UTC the next day."""
        system_tz("UTC+06")   # POSIX sign inverted → UTC-6
        assert local_day("2026-01-01T20:00:00", timezone.utc) == "2026-01-02"
        assert local_day("2026-01-01T20:00:00", None) == "2026-01-01"

    def test_dst_spring_forward_new_york(self):
        # 2026-03-08: 02:00 EST → 03:00 EDT (07:00 UTC). A 23-hour day.
        assert local_day("2026-03-08T04:30:00Z", NY) == "2026-03-07"   # 23:30 EST
        assert local_day("2026-03-08T05:30:00Z", NY) == "2026-03-08"   # 00:30 EST
        assert local_day("2026-03-09T03:30:00Z", NY) == "2026-03-08"   # 23:30 EDT
        assert local_day("2026-03-09T04:30:00Z", NY) == "2026-03-09"   # 00:30 EDT
        start, end = localtime.day_bounds("2026-03-08", tz=NY)
        assert end - start == timedelta(hours=23)
        assert start == datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)

    def test_dst_fall_back_new_york(self):
        # 2026-11-01: 02:00 EDT → 01:00 EST (06:00 UTC). A 25-hour day.
        assert local_day("2026-11-01T03:30:00Z", NY) == "2026-10-31"   # 23:30 EDT
        assert local_day("2026-11-01T04:30:00Z", NY) == "2026-11-01"   # 00:30 EDT
        assert local_day("2026-11-02T04:30:00Z", NY) == "2026-11-01"   # 23:30 EST
        assert local_day("2026-11-02T05:30:00Z", NY) == "2026-11-02"   # 00:30 EST
        start, end = localtime.day_bounds("2026-11-01", tz=NY)
        assert end - start == timedelta(hours=25)

    def test_system_naive_range_matches_legacy_strings_without_zone(self, system_tz):
        system_tz("UTC+06")
        assert localtime.system_naive_range("2026-04-16", tz=None) == (
            "2026-04-16T00:00:00", "2026-04-16T23:59:59")

    def test_system_naive_range_converts_configured_day_to_system_clock(self, system_tz):
        system_tz("UTC")
        # CR day 2026-05-04 = 06:00Z on the 4th .. 05:59:59Z on the 5th.
        assert localtime.system_naive_range("2026-05-04", tz=CR) == (
            "2026-05-04T06:00:00", "2026-05-05T05:59:59")

    def test_window_and_today(self):
        assert localtime.window_start("2026-05-07", 7) == "2026-05-01"
        assert localtime.window_start("2026-05-07", 1) == "2026-05-07"
        now = datetime(2026, 5, 5, 1, 0, tzinfo=timezone.utc)
        assert localtime.today(CR, now=now) == "2026-05-04"

    def test_zone_key(self):
        assert localtime.zone_key(None) == ""
        assert localtime.zone_key(timezone.utc) == "UTC"
        assert localtime.zone_key(NY) == "America/New_York"
