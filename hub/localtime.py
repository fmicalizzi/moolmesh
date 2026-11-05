"""The ONE definition of "local day" (issue #81).

Digests, the workspace rollup and the portfolio all answer "which day did this
happen?". They used to answer it three ways (UTC prefix, UTC ``date()``,
system-local ``fromtimestamp``), so the same PR could land on different days
in different views. Every caller now goes through :func:`local_day`.

The zone is ``[user] timezone`` in ``config.toml`` (an IANA name such as
``America/Costa_Rica``); empty means the system zone. An unknown name — or a
Windows machine without the tz database (``zoneinfo`` then has no data and
the ``tzdata`` package is not a dependency) — falls back to the system zone
with one warning in the log.

Inputs come in several clocks and all are accepted:

* aware ``datetime`` / ISO with ``Z`` or an offset — an exact instant;
* naive ``datetime`` / naive ISO — read as SYSTEM-local wall time, because
  that is what ``github.db`` stores (git_store migration 3 and the harvester
  write commits and issues as system-local naive);
* numbers or digit strings — epoch seconds, or milliseconds above ``1e11``
  (OpenCode).

The system zone is represented by ``None`` and resolved per instant with
``datetime.astimezone()``, so DST is applied at the instant, never with the
machine's *current* offset.
"""
from __future__ import annotations

import functools
import os
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from hub.log import get as get_logger

_log = get_logger("LocalTime")

# Default for the ``tz`` arguments below: "the zone configured in config.toml".
# A distinct sentinel because ``None`` already means "the system zone".
CONFIGURED: Any = object()

# Epoch values above this are milliseconds (1e11 s is the year 5138).
_MS_THRESHOLD = 1e11


@functools.lru_cache(maxsize=32)
def resolve_timezone(name: str | None) -> tzinfo | None:
    """IANA name → tzinfo; ``""``/``None`` → ``None`` (the system zone).

    ``"UTC"`` maps to ``datetime.timezone.utc`` so it works without a tz
    database. An unknown name, or a valid one this machine has no data for
    (Windows without tzdata), falls back to the system zone and logs one
    warning per name (the result is cached).
    """
    name = (name or "").strip()
    if not name:
        return None
    if name.upper() in ("UTC", "Z", "ETC/UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - stdlib since 3.9
        _log.warning("zoneinfo no disponible; uso la zona del sistema en vez de %r", name)
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _log.warning(
            "[user] timezone = %r no es una zona IANA conocida en esta máquina; "
            "uso la zona del sistema", name)
        return None


def configured_timezone_name() -> str:
    """The raw ``[user] timezone`` value ("" when unset or unreadable)."""
    from hub.config import load_config
    try:
        return load_config().timezone
    except (OSError, ValueError):
        _log.warning("no pude leer config.toml para la zona horaria; "
                     "uso la zona del sistema", exc_info=True)
        return ""


def user_timezone() -> tzinfo | None:
    """The configured zone, resolved (``None`` = system zone)."""
    return resolve_timezone(configured_timezone_name())


def _tz(tz: Any) -> tzinfo | None:
    return user_timezone() if tz is CONFIGURED else tz


def zone_key(tz: Any = CONFIGURED) -> str:
    """Stable name of a zone: IANA key, ``"UTC"``, or ``""`` for the system."""
    tz = _tz(tz)
    if tz is None:
        return ""
    if tz is timezone.utc:
        return "UTC"
    return str(getattr(tz, "key", "") or tz)


def system_zone_name() -> str:
    """IANA name of the system zone, or ``""`` when it cannot be named (#146).

    Tried in order: ``$TZ`` (a plain IANA name), ``/etc/timezone`` (Debian),
    and the ``/etc/localtime`` symlink's path after ``zoneinfo/`` (Linux and
    macOS). Each candidate must load as a ``ZoneInfo``; nothing is guessed.
    """
    from pathlib import Path
    from zoneinfo import ZoneInfo

    candidates: list[str] = []
    env = os.environ.get("TZ", "").strip().lstrip(":")
    if env and not env.startswith("/"):
        candidates.append(env)
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            ZoneInfo(name)
        except Exception:  # noqa: BLE001 — unknown or malformed key
            continue
        return name
    return ""


def zone_label(tz: Any = CONFIGURED, now: datetime | None = None) -> str:
    """The zone days are bucketed in, as a name a consumer can compare (#146).

    Like :func:`zone_key`, but never ``""``: an unset ``[user] timezone`` (the
    system zone) is named by :func:`system_zone_name`, else written as its
    current UTC offset (``"UTC-06:00"``). For output only — ``zone_key``
    stays the stored key (``""`` = system) used to detect zone changes.
    """
    key = zone_key(tz)
    if key:
        return key
    name = system_zone_name()
    if name:
        return name
    off = (now or datetime.now().astimezone()).utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hh, mm = divmod(abs(minutes), 60)
    return f"UTC{sign}{hh:02d}:{mm:02d}"


def to_aware(value: Any) -> datetime | None:
    """Any supported clock → aware ``datetime`` (``None`` if unreadable)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.astimezone()
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _from_epoch(float(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def _from_epoch(n: float) -> datetime | None:
    if n > _MS_THRESHOLD:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def local_day(value: Any, tz: Any = CONFIGURED) -> str | None:
    """The local calendar day (``YYYY-MM-DD``) of an instant, in ``tz``.

    Resolve ``tz`` once (``user_timezone()``) and pass it when bucketing many
    rows: the default re-reads ``config.toml`` on every call.
    """
    dt = to_aware(value)
    if dt is None:
        return None
    zone = _tz(tz)
    local = dt.astimezone(zone) if zone is not None else dt.astimezone()
    return local.date().isoformat()


def today(tz: Any = CONFIGURED, now: datetime | None = None) -> str:
    """Today's local day in ``tz``."""
    return local_day(now or datetime.now(timezone.utc), tz) or ""


def shift_day(day: str, days: int) -> str:
    """``day`` plus ``days`` calendar days (``YYYY-MM-DD``)."""
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def window_start(end_day: str, days: int) -> str:
    """First day of the inclusive ``days``-day window ending on ``end_day``."""
    return shift_day(end_day, -(max(int(days), 1) - 1))


def day_start(day: str, tz: Any = CONFIGURED) -> datetime:
    """Aware instant of local midnight starting ``day`` in ``tz``.

    Correct on DST days: the day after is computed as a calendar day, so a
    23- or 25-hour day keeps its true length.
    """
    zone = _tz(tz)
    midnight = datetime.combine(date.fromisoformat(day), time.min)
    if zone is None:
        return midnight.astimezone()
    return midnight.replace(tzinfo=zone)


def day_bounds(first_day: str, last_day: str | None = None,
               tz: Any = CONFIGURED) -> tuple[datetime, datetime]:
    """``[start, end)`` instants (aware UTC) covering local days first..last.

    UTC on purpose: subtracting two datetimes that share one ``ZoneInfo`` is
    wall-clock arithmetic in Python, which would hide a DST day's 23 or 25
    hours.
    """
    zone = _tz(tz)
    last_day = last_day or first_day
    return (day_start(first_day, zone).astimezone(timezone.utc),
            day_start(shift_day(last_day, 1), zone).astimezone(timezone.utc))


def system_naive_range(first_day: str, last_day: str | None = None,
                       tz: Any = CONFIGURED) -> tuple[str, str]:
    """Inclusive ``(since, until)`` strings for ``github.db`` range queries.

    ``git_commits.timestamp`` is stored as SYSTEM-local naive with second
    precision, so the configured local days are converted to that clock:
    ``since`` is the first second, ``until`` the last second of the range.
    With the zone unset this is exactly ``dayT00:00:00 .. dayT23:59:59``.
    """
    start, end = day_bounds(first_day, last_day, tz)

    def _naive(dt: datetime) -> str:
        return dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    return _naive(start), _naive(end - timedelta(seconds=1))


def utc_iso_from_epoch(value: Any) -> str | None:
    """Epoch (s or ms, number or digit string) → fixed-width UTC ISO.

    ``2026-03-08T07:30:00.000+00:00``: independent of the machine's zone and
    of DST (#79), millisecond precision like OpenCode's source clock, and a
    fixed width so string MIN/MAX order chronologically.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.strip().isdigit():
            return None
        value = float(value.strip())
    if not isinstance(value, (int, float)) or not value:
        return None
    dt = _from_epoch(float(value))
    return dt.isoformat(timespec="milliseconds") if dt is not None else None
