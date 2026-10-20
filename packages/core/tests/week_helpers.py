"""An instant that is "earlier this week, but not today" — for weekly-pool tests.

The weekly window resets on Sunday 12:00 local, the daily one at local
midnight (src/timezones.py). A record placed at week_start is therefore
*today* on a Sunday afternoon, and counts in the daily pool too. The last
instant before today's local midnight is inside this week on every other
day; on a Sunday afternoon no such instant exists, so the test is skipped.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.services import entitlement_service


def earlier_this_week_not_today(user_timezone: str | None) -> datetime:
    day_start, _ = entitlement_service._day_window(user_timezone)
    week_start, _ = entitlement_service._week_window(user_timezone)
    if day_start <= week_start:
        pytest.skip("the weekly window started today: there is no earlier day this week")
    return day_start - timedelta(microseconds=1)
