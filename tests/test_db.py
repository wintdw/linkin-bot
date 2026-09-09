from pathlib import Path

from linkedin_bot import db


def test_record_and_queries(tmp_path: Path):
    conn = db.connect(str(tmp_path / "t.db"))

    db.record_invite(conn, "SENT_PENDING", profile_url="https://www.linkedin.com/in/alice",
                     name="Alice", clicked_at="2026-09-07T09:00:00Z")
    db.record_invite(conn, "SENT_PENDING", clicked_at="2026-09-08T09:00:00Z")
    db.record_invite(conn, "ERROR", clicked_at="2026-09-08T10:00:00Z")

    # sent_since counts SENT_PENDING (and UNKNOWN) only, never ERROR
    assert db.sent_since(conn, "2026-09-08T00:00:00Z") == 1
    assert db.sent_since(conn, "2026-09-07T00:00:00Z") == 2

    assert db.distinct_send_days_before(conn, "2026-09-09") == 2
    assert db.distinct_send_days_before(conn, "2026-09-08") == 1
    assert db.distinct_send_days_before(conn, "2026-09-07") == 0

    counts = db.outcome_counts(conn)
    assert counts == {"SENT_PENDING": 2, "ERROR": 1}

    db.record_event(conn, "RUN_START", "warmup run")
    events = db.recent_events(conn)
    assert len(events) == 1
    assert events[0]["kind"] == "RUN_START"


def test_unknown_counts_as_sent(tmp_path: Path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.record_invite(conn, "UNKNOWN", clicked_at="2026-09-08T09:00:00Z")
    assert db.sent_since(conn, "2026-09-01T00:00:00Z") == 1


def test_last_days_summary(tmp_path: Path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.record_invite(conn, "SENT_PENDING", clicked_at="2026-09-08T09:00:00Z")
    db.record_invite(conn, "ERROR", clicked_at="2026-09-08T10:00:00Z")
    rows = db.last_days_summary(conn, days=14)
    assert rows[0]["day"] == "2026-09-08"
    assert rows[0]["sent"] == 1
    assert rows[0]["errors"] == 1
