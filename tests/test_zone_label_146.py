"""The snapshot names the zone its days are bucketed in, never "" (#146)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import hub.localtime as lt


def test_configured_zone_is_its_key():
    assert lt.zone_label(ZoneInfo("America/Costa_Rica")) == "America/Costa_Rica"
    assert lt.zone_label(timezone.utc) == "UTC"


def test_unset_uses_the_system_iana_name(monkeypatch):
    monkeypatch.setattr(lt, "system_zone_name", lambda: "Europe/Madrid")
    assert lt.zone_label(None) == "Europe/Madrid"


def test_unnamed_system_zone_falls_back_to_offset(monkeypatch):
    monkeypatch.setattr(lt, "system_zone_name", lambda: "")
    now = datetime(2026, 11, 5, 12, tzinfo=timezone(timedelta(hours=-6)))
    assert lt.zone_label(None, now=now) == "UTC-06:00"
    now = datetime(2026, 11, 5, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert lt.zone_label(None, now=now) == "UTC+05:30"


def test_tz_env_wins_when_valid(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert lt.system_zone_name() == "Asia/Tokyo"
    monkeypatch.setenv("TZ", ":America/Mexico_City")
    assert lt.system_zone_name() == "America/Mexico_City"


def test_bogus_tz_env_is_ignored(monkeypatch):
    monkeypatch.setenv("TZ", "Not/AZone")
    assert lt.system_zone_name() != "Not/AZone"


def test_zone_key_stays_empty_for_the_system():
    """The stored key (zone-change detection) is unchanged."""
    assert lt.zone_key(None) == ""

