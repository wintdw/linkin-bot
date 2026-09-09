"""SQLite state ledger: every click outcome and run event."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Outcomes that count as "an invitation was sent" for cap accounting.
# UNKNOWN means the click went through but we could not confirm state; it may
# have sent, so it is counted against the caps to stay conservative.
SEND_OUTCOMES = ("SENT_PENDING", "UNKNOWN")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_url TEXT,
    name TEXT,
    outcome TEXT NOT NULL,
    clicked_at TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_invitations_clicked_at ON invitations (clicked_at);
CREATE INDEX IF NOT EXISTS idx_invitations_outcome ON invitations (outcome);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_file: str) -> sqlite3.Connection:
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_invite(
    conn: sqlite3.Connection,
    outcome: str,
    profile_url: str | None = None,
    name: str | None = None,
    clicked_at: str | None = None,
    details: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO invitations (profile_url, name, outcome, clicked_at, details)"
        " VALUES (?, ?, ?, ?, ?)",
        (profile_url, name, outcome, clicked_at or utc_now(), details),
    )
    conn.commit()


def record_event(conn: sqlite3.Connection, kind: str, message: str = "") -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, message) VALUES (?, ?, ?)",
        (utc_now(), kind, message),
    )
    conn.commit()


def sent_since(conn: sqlite3.Connection, since_iso: str) -> int:
    placeholders = ",".join("?" * len(SEND_OUTCOMES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM invitations"
        f" WHERE outcome IN ({placeholders}) AND clicked_at >= ?",
        (*SEND_OUTCOMES, since_iso),
    ).fetchone()
    return int(row["n"])


def distinct_send_days_before(conn: sqlite3.Connection, date_iso: str) -> int:
    placeholders = ",".join("?" * len(SEND_OUTCOMES))
    row = conn.execute(
        "SELECT COUNT(DISTINCT substr(clicked_at, 1, 10)) AS n FROM invitations"
        f" WHERE outcome IN ({placeholders}) AND substr(clicked_at, 1, 10) < ?",
        (*SEND_OUTCOMES, date_iso),
    ).fetchone()
    return int(row["n"])


def outcome_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM invitations GROUP BY outcome"
    ).fetchall()
    return {row["outcome"]: int(row["n"]) for row in rows}


def last_days_summary(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    rows = conn.execute(
        """
        SELECT substr(clicked_at, 1, 10) AS day,
               SUM(CASE WHEN outcome IN ('SENT_PENDING', 'UNKNOWN') THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN outcome = 'ERROR' THEN 1 ELSE 0 END) AS errors
        FROM invitations
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
        """,
        (days,),
    ).fetchall()
    return [dict(row) for row in rows]


def recent_events(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, kind, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in reversed(rows)]
