"""Daily/weekly caps, warm-up ramp, and human-like pause helpers."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from . import db


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def warmup_day_cap(cfg, conn, now: datetime | None = None) -> int:
    """Today's ceiling: warm-up ramp capped by caps.daily."""
    now = now or now_utc()
    if not cfg.warmup.enabled:
        return int(cfg.caps.daily)
    today = now.strftime("%Y-%m-%d")
    prior_days = db.distinct_send_days_before(conn, today)
    cap = int(cfg.warmup.daily_start) + prior_days * int(cfg.warmup.daily_increment)
    return max(1, min(cap, int(cfg.caps.daily)))


def remaining_today(cfg, conn, now: datetime | None = None) -> dict:
    """Usage vs. caps for today and the rolling week, plus the binding minimum."""
    now = now or now_utc()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    week_cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    day_cap = warmup_day_cap(cfg, conn, now)
    sent_today = db.sent_since(conn, start_of_day)
    sent_week = db.sent_since(conn, week_cutoff)
    day_remaining = max(0, day_cap - sent_today)
    week_remaining = max(0, int(cfg.caps.weekly) - sent_week)

    return {
        "day_cap": day_cap,
        "sent_today": sent_today,
        "day_remaining": day_remaining,
        "sent_week": sent_week,
        "week_cap": int(cfg.caps.weekly),
        "week_remaining": week_remaining,
        "remaining": min(day_remaining, week_remaining),
    }


def sleep_between(cfg) -> None:
    time.sleep(random.uniform(float(cfg.delays.min_seconds), float(cfg.delays.max_seconds)))


def sleep_post_scroll(cfg) -> None:
    time.sleep(
        random.uniform(float(cfg.delays.post_scroll_min), float(cfg.delays.post_scroll_max))
    )
