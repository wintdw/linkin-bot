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
    details TEXT,
    kind TEXT NOT NULL DEFAULT 'connect'
);
CREATE INDEX IF NOT EXISTS idx_invitations_clicked_at ON invitations (clicked_at);
CREATE INDEX IF NOT EXISTS idx_invitations_outcome ON invitations (outcome);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT,
    flow TEXT NOT NULL DEFAULT ''
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a ledger already existed (kind, event flow)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(invitations)")}
    if "kind" not in columns:
        conn.execute(
            "ALTER TABLE invitations ADD COLUMN kind TEXT NOT NULL DEFAULT 'connect'"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invitations_kind ON invitations (kind)")

    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "flow" not in event_columns:
        conn.execute("ALTER TABLE events ADD COLUMN flow TEXT NOT NULL DEFAULT ''")
        # Backfill flow for rows written before the column existed.
        conn.execute(
            "UPDATE events SET flow='follow' WHERE flow='' AND "
            "(message LIKE 'follow:%' OR message='follow')"
        )
        conn.execute(
            "UPDATE events SET flow='connect' WHERE flow='' AND "
            "(message='connect' OR kind IN ('RUN_START', 'RUN_END'))"
        )


def _kind_filter(kind: str | None) -> tuple[str, tuple]:
    """SQL fragment + params to restrict a query to one invitation ``kind``."""
    return ("", ()) if kind is None else (" AND kind = ?", (kind,))


def connect(db_file: str) -> sqlite3.Connection:
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def record_invite(
    conn: sqlite3.Connection,
    outcome: str,
    profile_url: str | None = None,
    name: str | None = None,
    clicked_at: str | None = None,
    details: str | None = None,
    kind: str = "connect",
) -> None:
    conn.execute(
        "INSERT INTO invitations (profile_url, name, outcome, clicked_at, details, kind)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (profile_url, name, outcome, clicked_at or utc_now(), details, kind),
    )
    conn.commit()


def record_event(
    conn: sqlite3.Connection, kind: str, message: str = "", flow: str = ""
) -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, message, flow) VALUES (?, ?, ?, ?)",
        (utc_now(), kind, message, flow),
    )
    conn.commit()


def sent_since(conn: sqlite3.Connection, since_iso: str, kind: str | None = "connect") -> int:
    extra, params = _kind_filter(kind)
    placeholders = ",".join("?" * len(SEND_OUTCOMES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM invitations"
        f" WHERE outcome IN ({placeholders}) AND clicked_at >= ?{extra}",
        (*SEND_OUTCOMES, since_iso, *params),
    ).fetchone()
    return int(row["n"])


def distinct_send_days_before(
    conn: sqlite3.Connection, date_iso: str, kind: str | None = "connect"
) -> int:
    extra, params = _kind_filter(kind)
    placeholders = ",".join("?" * len(SEND_OUTCOMES))
    row = conn.execute(
        "SELECT COUNT(DISTINCT substr(clicked_at, 1, 10)) AS n FROM invitations"
        f" WHERE outcome IN ({placeholders}) AND substr(clicked_at, 1, 10) < ?{extra}",
        (*SEND_OUTCOMES, date_iso, *params),
    ).fetchone()
    return int(row["n"])


def outcome_counts(conn: sqlite3.Connection, kind: str | None = None) -> dict[str, int]:
    extra, params = _kind_filter(kind)
    rows = conn.execute(
        f"SELECT outcome, COUNT(*) AS n FROM invitations WHERE 1=1{extra} GROUP BY outcome",
        params,
    ).fetchall()
    return {row["outcome"]: int(row["n"]) for row in rows}


def last_days_summary(conn: sqlite3.Connection, days: int = 14, kind: str | None = None) -> list[dict]:
    extra, params = _kind_filter(kind)
    rows = conn.execute(
        f"""
        SELECT substr(clicked_at, 1, 10) AS day,
               SUM(CASE WHEN outcome IN ('SENT_PENDING', 'UNKNOWN') THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN outcome = 'ERROR' THEN 1 ELSE 0 END) AS errors
        FROM invitations
        WHERE 1=1{extra}
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
        """,
        (*params, days),
    ).fetchall()
    return [dict(row) for row in rows]


def recent_events(
    conn: sqlite3.Connection, limit: int = 12, flow: str | None = None
) -> list[dict]:
    """Most recent events (oldest first); optionally only one flow's events."""
    if flow is None:
        rows = conn.execute(
            "SELECT ts, kind, message, flow FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, kind, message, flow FROM events WHERE flow = ? "
            "ORDER BY id DESC LIMIT ?",
            (flow, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]
