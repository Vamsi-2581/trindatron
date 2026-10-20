"""
Unit tests for src.timezones — IANA validation and the weekly reset window
(Sunday 12:00 local time, DST-safe).

All test cases pass now_utc explicitly (no mocking needed — week_window()
takes it as a parameter), so every test is fully deterministic regardless
of the real wall-clock date the suite runs on. This is what made the old
test_weekly_entitlement.py flaky: it relied on the real "now" instead of a
fixed instant.

Expected UTC values below were independently verified by manual calculation
(see conversation) before being encoded as assertions — not copied from
this module's own output.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.timezones import FALLBACK_TIMEZONE, is_valid_iana_timezone, resolve_user_timezone, week_window


# ── is_valid_iana_timezone / resolve_user_timezone ─────────────────────────


@pytest.mark.parametrize("tz", [
    "America/Sao_Paulo", "America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney", "UTC",
])
def test_is_valid_iana_timezone_accepts_real_zones(tz):
    assert is_valid_iana_timezone(tz) is True


@pytest.mark.parametrize("tz", ["Not/AZone", "", None, "GMT-3", "Brazil", "america/sao_paulo123"])
def test_is_valid_iana_timezone_rejects_invalid(tz):
    assert is_valid_iana_timezone(tz) is False


def test_resolve_user_timezone_passes_through_valid():
    assert resolve_user_timezone("Asia/Tokyo") == "Asia/Tokyo"


def test_resolve_user_timezone_falls_back_for_none():
    assert resolve_user_timezone(None) == FALLBACK_TIMEZONE


def test_resolve_user_timezone_falls_back_for_invalid():
    assert resolve_user_timezone("Not/AZone") == FALLBACK_TIMEZONE


# ── week_window — one case per target zone, before AND after Sunday 12:00 ──


def test_week_window_sao_paulo_before_sunday_noon():
    # 2026-08-30 14:00 UTC = Sunday 11:00 America/Sao_Paulo (UTC-3, no DST since 2019)
    now_utc = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "America/Sao_Paulo")
    assert start == datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)  # previous Sunday 12:00 -03:00
    assert end == datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)    # this Sunday 12:00 -03:00


def test_week_window_sao_paulo_after_sunday_noon():
    # 2026-08-30 16:00 UTC = Sunday 13:00 America/Sao_Paulo
    now_utc = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "America/Sao_Paulo")
    assert start == datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)  # this Sunday 12:00 -03:00
    assert end == datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)     # next Sunday 12:00 -03:00


def test_week_window_new_york_before_sunday_noon():
    # 2026-08-30 15:00 UTC = Sunday 11:00 America/New_York (EDT, UTC-4 in August)
    now_utc = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "America/New_York")
    assert start == datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)


def test_week_window_new_york_after_sunday_noon():
    # 2026-08-30 17:00 UTC = Sunday 13:00 America/New_York (EDT)
    now_utc = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "America/New_York")
    assert start == datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)


def test_week_window_london_before_sunday_noon():
    # 2026-08-30 10:00 UTC = Sunday 11:00 Europe/London (BST, UTC+1 in August)
    now_utc = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "Europe/London")
    assert start == datetime(2026, 8, 23, 11, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)


def test_week_window_london_after_sunday_noon():
    # 2026-08-30 13:00 UTC = Sunday 14:00 Europe/London (BST)
    now_utc = datetime(2026, 8, 30, 13, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "Europe/London")
    assert start == datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc)


def test_week_window_tokyo_before_sunday_noon():
    # 2026-08-30 02:00 UTC = Sunday 11:00 Asia/Tokyo (UTC+9, no DST)
    now_utc = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "Asia/Tokyo")
    assert start == datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)


def test_week_window_tokyo_after_sunday_noon():
    # 2026-08-30 04:00 UTC = Sunday 13:00 Asia/Tokyo
    now_utc = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "Asia/Tokyo")
    assert start == datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)


def test_week_window_sydney_before_sunday_noon():
    # 2026-08-30 00:00 UTC = Sunday 10:00 Australia/Sydney (AEST, UTC+10 in August — winter, no DST)
    now_utc = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "Australia/Sydney")
    assert start == datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)


def test_week_window_sydney_after_sunday_noon():
    # 2026-08-30 03:00 UTC = Sunday 13:00 Australia/Sydney (AEST)
    now_utc = datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, "Australia/Sydney")
    assert start == datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc)


# ── DST transitions — the reset boundary must not slide by an hour ─────────


def test_week_window_new_york_spring_forward_boundary():
    """US DST starts 2026-03-08 (2am EST -> 3am EDT). A Monday just after
    must resolve last Sunday's noon using the CORRECT (new, EDT) offset."""
    now_utc = datetime(2026, 3, 9, 18, 0, tzinfo=timezone.utc)  # Monday
    start, end = week_window(now_utc, "America/New_York")
    # 2026-03-08 12:00 local is already EDT (UTC-4) — transition was at 2am.
    assert start == datetime(2026, 3, 8, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 15, 16, 0, tzinfo=timezone.utc)


def test_week_window_new_york_fall_back_boundary():
    """US DST ends 2026-11-01 (2am EDT -> 1am EST). A Monday just after
    must resolve last Sunday's noon using the CORRECT (new, EST) offset."""
    now_utc = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)  # Monday
    start, end = week_window(now_utc, "America/New_York")
    # 2026-11-01 12:00 local is already EST (UTC-5) — transition was at 2am.
    assert start == datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)


def test_week_window_london_dst_end_boundary():
    """UK clocks go back 2026-10-25 (2am BST -> 1am GMT)."""
    now_utc = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)  # Monday
    start, end = week_window(now_utc, "Europe/London")
    # 2026-10-25 12:00 local is already GMT (UTC+0).
    assert start == datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_week_window_sydney_dst_start_boundary():
    """Australia DST starts 2026-10-04 (2am AEST -> 3am AEDT) — Southern
    Hemisphere, opposite season from the Northern Hemisphere zones above."""
    now_utc = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)  # Monday local
    start, end = week_window(now_utc, "Australia/Sydney")
    # 2026-10-04 12:00 local is already AEDT (UTC+11) — transition was at 2am.
    assert start == datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)


def test_week_window_sydney_dst_end_boundary():
    """Australia DST ends 2026-04-05 (3am AEDT -> 2am AEST)."""
    now_utc = datetime(2026, 4, 5, 15, 0, tzinfo=timezone.utc)  # Sunday, after the local transition
    start, end = week_window(now_utc, "Australia/Sydney")
    # 2026-04-05 12:00 local is already AEST (UTC+10) — transition was earlier that morning.
    assert start == datetime(2026, 4, 5, 2, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 12, 2, 0, tzinfo=timezone.utc)


# ── Fallback behavior — unknown/invalid timezone never crashes ─────────────


def test_week_window_falls_back_to_utc_for_invalid_timezone():
    now_utc = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)  # Sunday
    start, end = week_window(now_utc, "Not/AZone")
    assert start == datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def test_week_window_falls_back_to_utc_for_none():
    now_utc = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)  # Sunday
    start, end = week_window(now_utc, None)
    assert start == datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


# ── Structural invariants ───────────────────────────────────────────────


@pytest.mark.parametrize("tz", [
    "America/Sao_Paulo", "America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney",
])
def test_week_window_start_is_always_before_now_and_end_after(tz):
    now_utc = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    start, end = week_window(now_utc, tz)
    assert start <= now_utc < end


@pytest.mark.parametrize("tz", [
    "America/Sao_Paulo", "America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney",
])
def test_week_window_start_local_weekday_is_always_sunday(tz):
    now_utc = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    start, _ = week_window(now_utc, tz)
    from zoneinfo import ZoneInfo
    assert start.astimezone(ZoneInfo(tz)).weekday() == 6  # Sunday
