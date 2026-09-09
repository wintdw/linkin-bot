from datetime import datetime, timezone
from pathlib import Path

from linkedin_bot import config, db, scheduler

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

BASE = (
    "caps:\n  daily: 15\n  weekly: 90\n"
    "warmup:\n  enabled: true\n  daily_start: 5\n  daily_increment: 2\n"
)


def _make_cfg(tmp_path: Path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return config.load(path)


def _seed(conn, day: str, count: int):
    for i in range(count):
        db.record_invite(conn, "SENT_PENDING", clicked_at=f"{day}T{i % 60:02d}:{i % 60:02d}:00Z")


def test_warmup_first_day_starts_low(tmp_path):
    cfg = _make_cfg(tmp_path, BASE)
    conn = db.connect(str(tmp_path / "b.db"))
    assert scheduler.warmup_day_cap(cfg, conn, now=NOW) == 5


def test_warmup_grows_with_active_days_and_caps(tmp_path):
    cfg = _make_cfg(tmp_path, BASE)
    conn = db.connect(str(tmp_path / "b.db"))
    _seed(conn, "2026-09-07", 2)
    _seed(conn, "2026-09-08", 2)
    # two prior active days -> 5 + 2*2 = 9
    assert scheduler.warmup_day_cap(cfg, conn, now=NOW) == 9


def test_warmup_disabled_uses_daily_cap(tmp_path):
    cfg = _make_cfg(
        tmp_path,
        "caps:\n  daily: 15\n  weekly: 90\nwarmup:\n  enabled: false\n",
    )
    conn = db.connect(str(tmp_path / "b.db"))
    assert scheduler.warmup_day_cap(cfg, conn, now=NOW) == 15


def test_remaining_counts_today(tmp_path):
    cfg = _make_cfg(tmp_path, BASE)
    conn = db.connect(str(tmp_path / "b.db"))
    for i in range(4):
        db.record_invite(conn, "SENT_PENDING", clicked_at=f"2026-09-09T09:0{i}:00Z")
    usage = scheduler.remaining_today(cfg, conn, now=NOW)
    assert usage["day_cap"] == 5
    assert usage["sent_today"] == 4
    assert usage["day_remaining"] == 1
    assert usage["remaining"] == 1


def test_weekly_cap_binds_before_daily(tmp_path):
    cfg = _make_cfg(tmp_path, BASE)
    conn = db.connect(str(tmp_path / "b.db"))
    _seed(conn, "2026-09-03", 88)  # inside the rolling 7-day window
    usage = scheduler.remaining_today(cfg, conn, now=NOW)
    assert usage["sent_week"] == 88
    assert usage["week_remaining"] == 2
    assert usage["remaining"] == 2  # weekly binds even though today has headroom
