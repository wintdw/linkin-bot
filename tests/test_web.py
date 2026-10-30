from datetime import datetime

import pytest

from linkedin_bot.web import (
    _due_slot,
    _status_dict,
    current_slot,
    health_verdict,
    next_run_at,
    seconds_until,
    seconds_until_any,
    session_check,
    session_check_line,
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


def test_session_check_from_exit_code():
    ok = session_check("connect", 0, NOW)
    assert ok == {
        "at": NOW.isoformat(timespec="seconds"),
        "ok": True,
        "kind": "ok",
        "detail": "connect run reached LinkedIn",
    }
    bad = session_check("follow", 2, NOW)
    assert bad["ok"] is False and "invalid" in bad["detail"]
    # exit 4 is an edge block: not the same verdict as a dead cookie
    blocked = session_check("connect", 4, NOW)
    assert blocked["ok"] is False
    assert blocked["kind"] == "blocked"
    assert "not a login problem" in blocked["detail"]
    # exit 1 (kill switch / missing session) and 3 (crash) say nothing about validity
    assert session_check("connect", 1, NOW) is None
    assert session_check("connect", 3, NOW) is None


def test_health_verdict_escalates():
    assert health_verdict(0, None) == "ok"
    assert health_verdict(0, {"ok": True, "kind": "ok"}) == "ok"
    # repeated failures are degraded even without a session verdict
    assert health_verdict(2, None) == "degraded"
    assert health_verdict(7, {"ok": True, "kind": "ok"}) == "degraded"
    # a rejected cookie is degraded, an edge block is its own state
    assert health_verdict(0, {"ok": False, "kind": "invalid"}) == "degraded"
    assert health_verdict(0, {"ok": False, "kind": "blocked"}) == "blocked"
    # a block never hides behind success counters
    assert health_verdict(0, {"ok": False, "kind": "blocked"}) == "blocked"


def test_session_check_line_variants():
    assert session_check_line(None) == "session check: not run yet"
    assert session_check_line({"at": None, "ok": None, "detail": None}) == "session check: not run yet"
    assert session_check_line(
        {"at": "2026-09-09T09:30:05", "ok": True, "detail": "connect run reached LinkedIn"}
    ).startswith("session check: ok")
    assert "FAILED" in session_check_line(
        {"at": "2026-09-09T09:30:05", "ok": False, "detail": "connect run: session invalid or blocked"}
    )


def test_status_dict_has_machine_readable_shape(tmp_path):
    from types import SimpleNamespace

    from linkedin_bot import db as dbmod

    db_file = str(tmp_path / "bot.db")
    cfg = SimpleNamespace(
        account="dung",
        paths=SimpleNamespace(
            db_file=db_file,
            kill_file=str(tmp_path / "STOP"),
            data_dir=str(tmp_path),
            session_file=str(tmp_path / "linkedin_storage_state.json"),
        ),
        caps=SimpleNamespace(daily=10, weekly=100),
        follow=SimpleNamespace(caps=SimpleNamespace(daily=10, weekly=100)),
        warmup=SimpleNamespace(enabled=False),
        post=SimpleNamespace(page_url=""),
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
    assert status["posts"]["page_url"] == ""
    assert status["session"]["present"] is False  # no saved cookie store yet
    assert status["session"]["last_check"] is None  # no run has reported a verdict yet
    assert len(status["events"]["connect"]) == 1
    assert status["events"]["follow"] == []


def _post_cfg(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        account="",
        paths=SimpleNamespace(
            data_dir=str(tmp_path),
            db_file=str(tmp_path / "bot.db"),
            kill_file=str(tmp_path / "STOP"),
            session_file=str(tmp_path / "session.json"),
        ),
        post=SimpleNamespace(
            page_url="https://www.linkedin.com/company/1/admin/page-posts/published/",
            composer_button_text="Start a post",
            submit_button_text="Post",
            comment_button_text="Comment",
        ),
        caps=SimpleNamespace(daily=1, weekly=1),
        follow=SimpleNamespace(caps=SimpleNamespace(daily=1, weekly=1)),
        warmup=SimpleNamespace(enabled=False),
    )


def test_post_failure_body_carries_status_and_retryable(tmp_path, monkeypatch):
    # A 502 alone cannot say whether the post went out, so the body must: only a
    # "refused" failure may be retried, an "unknown" one must not (a retry can
    # duplicate the post).
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from linkedin_bot import publish, web

    monkeypatch.setattr(web, "load", lambda *a, **k: _post_cfg(tmp_path))
    app = web.create_app()
    failures = {
        publish.STATUS_UNKNOWN: publish.PublishUnconfirmed("could not confirm it published"),
        publish.STATUS_REFUSED: publish.PublishError("could not open the Page composer"),
    }
    for status, error in failures.items():

        def _raise(*_args, _error=error, **_kwargs):
            raise _error

        monkeypatch.setattr(publish, "publish_to_page", _raise)
        with TestClient(app) as client:
            response = client.post("/post", json={"text": "hi"})
        assert response.status_code == 502
        body = response.json()
        assert body["ok"] is False
        assert body["status"] == status
        assert body["retryable"] is (status == publish.STATUS_REFUSED)
