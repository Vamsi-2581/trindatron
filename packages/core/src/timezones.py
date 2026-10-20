"""
IANA timezone validation and the TRINITY weekly-reset window calculation.

TRINITY's weekly quota resets every SUNDAY AT 12:00 in the user's *local*
IANA timezone — never a fixed UTC offset, never the server's timezone, never
manual "+/- N hours" arithmetic. This module is the single source of truth
for that rule; every caller (entitlement_service.check_and_reserve,
get_user_week_usage, get_user_period_usage) must go through week_window()
here so the rule stays identical everywhere.

DST correctness
----------------
week_window() never adds a fixed timedelta across a timezone boundary. It
builds *local* wall-clock datetimes (Sunday, 12:00, in the user's zone) via
zoneinfo.ZoneInfo, and only converts to UTC as the final step. zoneinfo (not
pytz, no manual offset math) resolves the correct UTC offset for that local
instant using the zone's real transition rules, so a DST change anywhere
between "now" and the target Sunday cannot shift the boundary by an hour.

Requires the `tzdata` package on platforms without an OS-level IANA
timezone database (Windows has none — see pyproject.toml). Do not remove
that dependency; zoneinfo silently has nothing to load without it on
Windows, which would break this calculation invisibly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Computational fallback used ONLY when a user's timezone is not yet known
# (NULL in the database — see models/user.py and alembic/versions/0014_*).
# This is never written to the database as if it were the user's real,
# detected timezone; it exists purely so week_window() can still return a
# deterministic answer for a user who hasn't been through timezone
# detection yet (see PARTE 5 / frontend detection-on-login).
FALLBACK_TIMEZONE = "UTC"

# Sunday 12:00 local — the fixed point of the weekly reset rule.
_RESET_WEEKDAY = 6  # Python: Monday=0 ... Sunday=6
_RESET_HOUR = 12


def is_valid_iana_timezone(tz_name: str | None) -> bool:
    """True if tz_name is a real IANA zone zoneinfo can load.

    Used to validate user-submitted timezone strings — never accept an
    arbitrary string as a timezone (see PATCH /users/me/timezone).
    """
    if not tz_name:
        return False
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # OSError covers legacy IANA directory aliases (e.g. "Brazil",
        # "America") that resolve to a directory instead of a leaf zone
        # file — PermissionError/IsADirectoryError on different platforms.
        # Those are not valid, selectable timezones either way.
        return False


def resolve_user_timezone(tz_name: str | None) -> str:
    """Return tz_name if it's a valid IANA zone, else FALLBACK_TIMEZONE.

    Never invents a *different* real timezone for the user — the fallback is
    a neutral computational default, documented as such everywhere it's used.
    """
    if tz_name and is_valid_iana_timezone(tz_name):
        return tz_name
    return FALLBACK_TIMEZONE


def week_window(now_utc: datetime, tz_name: str | None) -> tuple[datetime, datetime]:
    """Return (week_start_utc, week_end_utc) for the weekly reset rule:
    resets every Sunday at 12:00 in tz_name (an IANA zone).

    now_utc must be timezone-aware (UTC). tz_name may be None or invalid —
    resolve_user_timezone() falls back to FALLBACK_TIMEZONE in that case.

    Algorithm — never manual offset arithmetic:
      1. Convert now_utc to the user's local timezone.
      2. Find *this* calendar week's Sunday (local date) and build the
         local wall-clock instant "that Sunday, 12:00" directly with
         tzinfo=ZoneInfo(...) — zoneinfo resolves the correct UTC offset
         for that specific local instant, DST-transition-aware.
      3. If that instant is still in the future, the current window
         actually started the PREVIOUS Sunday at 12:00 local.
      4. week_end is computed the same way — by building "start + 7 days"
         as a fresh local wall-clock instant, not by adding a UTC
         timedelta — so a DST change inside the window doesn't shift it.
      5. Convert both endpoints to UTC as the final step.

    Returns both endpoints tz-aware in UTC — the database stays UTC-only,
    no schema change needed to compare against UsageRecord.created_at.
    """
    tz = ZoneInfo(resolve_user_timezone(tz_name))
    now_local = now_utc.astimezone(tz)

    # Days since the most recent Sunday (0 if today IS Sunday).
    days_since_sunday = (now_local.weekday() - _RESET_WEEKDAY) % 7
    this_sunday: date = (now_local - timedelta(days=days_since_sunday)).date()
    this_sunday_noon = datetime(
        this_sunday.year, this_sunday.month, this_sunday.day,
        _RESET_HOUR, 0, 0, tzinfo=tz,
    )

    if this_sunday_noon > now_local:
        # This week's reset instant hasn't happened yet — the active
        # window started the Sunday before.
        start_date = this_sunday - timedelta(days=7)
    else:
        start_date = this_sunday

    week_start_local = datetime(
        start_date.year, start_date.month, start_date.day,
        _RESET_HOUR, 0, 0, tzinfo=tz,
    )
    end_date = start_date + timedelta(days=7)
    week_end_local = datetime(
        end_date.year, end_date.month, end_date.day,
        _RESET_HOUR, 0, 0, tzinfo=tz,
    )

    return week_start_local.astimezone(timezone.utc), week_end_local.astimezone(timezone.utc)


def day_window(now_utc: datetime, tz_name: str | None) -> tuple[datetime, datetime]:
    """Return (day_start_utc, day_end_utc) for the daily reset rule:
    resets at LOCAL midnight in tz_name.

    Added 2026-09-11. The daily window used to be computed as UTC midnight
    while the weekly one has always been Sunday 12:00 *local* — so the same
    product had two different ideas of when a period ends, and a user in
    São Paulo (UTC-3) watched their day roll over at 21:00 with no
    explanation available anywhere in the interface. One rule now: every
    period boundary a user can observe is expressed in their own time.

    Built the same way as week_window() and for the same reason: a local
    wall-clock midnight constructed with tzinfo=ZoneInfo, converted to UTC
    only as the final step, so a DST transition inside the day cannot shift
    the boundary by an hour. Never `now.replace(hour=0)` on a UTC instant.

    The 5-hour session window is deliberately NOT affected — it is a
    duration from a starting instant, not a calendar boundary, so it has no
    local-midnight to be wrong about.
    """
    tz = ZoneInfo(resolve_user_timezone(tz_name))
    now_local = now_utc.astimezone(tz)

    start_local = datetime(
        now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=tz,
    )
    next_day = (start_local + timedelta(days=1)).date()
    end_local = datetime(
        next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=tz,
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
