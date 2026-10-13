from datetime import datetime

import pytest

from linkedin_bot.web import (
    _due_slot,
    _status_dict,
    current_slot,
    next_run_at,
    oauth_token_status,
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


def test_due_slot_fires_only_for_the_fresh_occurrence():
    # Just after 09:30: connect is due, follow's 10:00 occurrence is stale (yesterday).
    at_0930 = datetime(2026, 9, 9, 9, 30, 5)
    assert _due_slot(["09:30"], None, at_0930) == "2026-09-09 09:30"
    assert _due_slot(["10:00"], None, at_0930) is None
    assert _due_slot(["09:30"], "2026-09-09 09:30", at_0930) is None  # already ran

    # Just after 10:00: follow is due, and connect's 09:30 is now stale.
    at_1000 = datetime(2026, 9, 9, 10, 0, 30)
    assert _due_slot(["10:00"], None, at_1000) == "2026-09-09 10:00"
    assert _due_slot(["09:30"], None, at_1000) is None


def test_oauth_token_status_variants():
    assert "not connected" in oauth_token_status(None)
    assert "connected (no expiry" in oauth_token_status({"access_token": "x"})
    # 10 days out -> "10 day(s) left"
    far = int(NOW.timestamp()) + 10 * 86400
    assert "10 day(s) left" in oauth_token_status({"expires_at": far}, NOW)
    # 2 hours out, same day -> hours form
    soon = int(NOW.timestamp()) + 2 * 3600
    assert "expires in 2 h" in oauth_token_status({"expires_at": soon}, NOW)
    # already past -> EXPIRED
    past = int(NOW.timestamp()) - 60
    assert "EXPIRED" in oauth_token_status({"expires_at": past}, NOW)


def test_status_dict_has_machine_readable_shape(tmp_path):
    from types import SimpleNamespace

    from linkedin_bot import db as dbmod

    db_file = str(tmp_path / "bot.db")
    cfg = SimpleNamespace(
        paths=SimpleNamespace(
            db_file=db_file,
            kill_file=str(tmp_path / "STOP"),
            data_dir=str(tmp_path),
        ),
        caps=SimpleNamespace(daily=10, weekly=100),
        follow=SimpleNamespace(caps=SimpleNamespace(daily=10, weekly=100)),
        warmup=SimpleNamespace(enabled=False),
    )
    conn = dbmod.connect(db_file)
    dbmod.record_event(conn, "RUN_START", "", "connect")
    conn.close()
    state = {
        "busy": False,
        "flows": {
            "connect": {
                "last_started": None,
                "last_finished": None,
                "last_ok": None,
                "last_error": None,
                "last_slot": None,
            },
            "follow": {
                "last_started": None,
                "last_finished": None,
                "last_ok": None,
                "last_error": None,
                "last_slot": None,
            },
        },
    }
    status = _status_dict(cfg, state, {"connect": ["09:30"], "follow": ["10:00"]})
    assert status["status"] == "ok"
    assert set(status["caps"]) == {"connect", "follow"}
    assert status["caps"]["connect"]["day_cap"] == 10
    assert status["kill_switch"] is False
    assert status["oauth"]["connected"] is False
    assert "access_token" not in status["oauth"]  # never leak the token here
    assert len(status["events"]["connect"]) == 1
    assert status["events"]["follow"] == []
