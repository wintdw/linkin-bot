from datetime import datetime

import pytest

from linkedin_bot.web import (
    current_slot,
    next_run_at,
    seconds_until,
    seconds_until_any,
)

NOW = datetime(2026, 9, 9, 12, 0)  # 12:00 local


def test_seconds_until_today_or_tomorrow():
    assert seconds_until("14:00", NOW) == 2 * 3600
    # 09:30 today already passed -> next occurrence is tomorrow 09:30 (21.5 h)
    assert seconds_until("09:30", NOW) == 21 * 3600 + 30 * 60


def test_seconds_until_any_takes_earliest():
    assert seconds_until_any(["09:30", "14:00"], NOW) == 2 * 3600
    assert seconds_until_any(["23:00"], NOW) == 11 * 3600


def test_next_run_at_wraps_to_tomorrow():
    assert next_run_at(["09:30"], NOW) == datetime(2026, 9, 10, 9, 30)


def test_current_slot_most_recent_occurrence():
    assert current_slot(["09:30"], NOW) == "2026-09-09 09:30"  # passed today
    assert current_slot(["20:00"], NOW) == "2026-09-08 20:00"  # yesterday's slot


def test_current_slot_exact_boundary():
    assert current_slot(["12:00"], NOW) == "2026-09-09 12:00"


def test_invalid_hhmm_rejected():
    with pytest.raises(ValueError):
        seconds_until("25:00")
    with pytest.raises(ValueError):
        seconds_until("09:xx")
    with pytest.raises(ValueError):
        seconds_until("0930")
