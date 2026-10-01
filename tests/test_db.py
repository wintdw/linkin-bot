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


def test_event_flow_label_and_filter(tmp_path: Path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.record_event(conn, "RUN_START", "", "connect")
    db.record_event(conn, "RUN_END", "sent=1", "connect")
    db.record_event(conn, "RUN_END", "sent=2", "follow")

    assert [e["message"] for e in db.recent_events(conn, flow="follow")] == ["sent=2"]
    assert len(db.recent_events(conn, flow="connect")) == 2
    assert len(db.recent_events(conn)) == 3
    assert db.recent_events(conn, flow="follow")[0]["flow"] == "follow"


def test_migration_backfills_event_flow(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy_events.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT)"
    )
    raw.execute(
        "INSERT INTO events (ts, kind, message)"
        " VALUES ('2026-09-08T09:00:00Z', 'RUN_END', 'follow: sent=3 reason=cap')"
    )
    raw.execute(
        "INSERT INTO events (ts, kind, message)"
        " VALUES ('2026-09-08T09:01:00Z', 'RUN_END', 'sent=5 reason=cap')"
    )
    raw.commit()
    raw.close()

    conn = db.connect(str(path))
    assert len(db.recent_events(conn, flow="follow")) == 1
    assert len(db.recent_events(conn, flow="connect")) == 1


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


def test_kind_defaults_to_connect_and_filters(tmp_path: Path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.record_invite(conn, "SENT_PENDING", clicked_at="2026-09-08T09:00:00Z")
    db.record_invite(conn, "SENT_PENDING", clicked_at="2026-09-08T09:01:00Z", kind="follow")

    assert db.sent_since(conn, "2026-09-01T00:00:00Z") == 1  # connect only
    assert db.sent_since(conn, "2026-09-01T00:00:00Z", kind="follow") == 1
    assert db.sent_since(conn, "2026-09-01T00:00:00Z", kind=None) == 2
    assert db.outcome_counts(conn, kind="follow") == {"SENT_PENDING": 1}
    assert db.distinct_send_days_before(conn, "2026-09-09", kind="follow") == 1


def test_migration_adds_kind_to_legacy_ledger(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE invitations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, profile_url TEXT, name TEXT,"
        "outcome TEXT NOT NULL, clicked_at TEXT NOT NULL, details TEXT)"
    )
    raw.execute(
        "INSERT INTO invitations (outcome, clicked_at)"
        " VALUES ('SENT_PENDING', '2026-09-08T09:00:00Z')"
    )
    raw.commit()
    raw.close()

    conn = db.connect(str(path))  # opens + migrates the legacy table
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(invitations)")}
    assert "kind" in columns
    # pre-existing rows are treated as connect sends
    assert db.sent_since(conn, "2026-09-01T00:00:00Z") == 1
    assert db.sent_since(conn, "2026-09-01T00:00:00Z", kind="follow") == 0
