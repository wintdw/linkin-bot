"""Flow registry and shared loop scaffolding.

A *flow* is one browser automation pass against the shared LinkedIn account:
``connect`` (personal My Network requests, ``network.py``) and ``follow``
(invite connections to follow a managed Page, ``follow.py``). They share one
account and must never run concurrently (``serve`` serializes them on one
lock), and they share a lot of scaffolding: kill-switch / checkpoint stop
detection, per-run stats, stdout ``[flow]`` logging, and the ``RUN_END`` ledger
event. That scaffolding lives here so a new flow only has to supply its loop
body plus a few descriptors.

``SPECS`` / ``NAMES`` are the single source of truth for the set of flows: the
CLI, the web service and the scheduler iterate them instead of hard-coding
``("connect", "follow")``. Loop runners live in their own modules and are
resolved lazily, so importing this module never pulls in Playwright.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

from . import db, monitor, scheduler

FEED_URL = "https://www.linkedin.com/feed/"

CONNECT_OUTCOME_LABELS = {
    "SENT_PENDING": "sent",
    "UNKNOWN": "unknown (counted as sent)",
    "ERROR": "error",
    "LIMIT": "linkedin limit",
}
FOLLOW_OUTCOME_LABELS = {
    "SENT_PENDING": "invited",
    "UNKNOWN": "unknown (counted as sent)",
    "ERROR": "error",
    "LIMIT": "linkedin limit",
}


@dataclass(frozen=True)
class FlowSpec:
    """Descriptor for one flow: identity, schedule default and code entry points.

    ``name`` is canonical: it is the ledger ``kind``, the web schedule key and
    the ``[name]`` log prefix. ``loop`` / ``dry_run`` name functions in the
    sibling ``module`` and are resolved lazily by :meth:`loop_fn` /
    :meth:`dry_run_fn`.
    """

    name: str
    module: str
    loop: str
    dry_run: str
    default_at: tuple[str, ...]
    outcome_labels: dict[str, str]

    def loop_fn(self) -> Callable:
        return _resolve(self.module, self.loop)

    def dry_run_fn(self) -> Callable:
        return _resolve(self.module, self.dry_run)


def _resolve(module_name: str, attr: str) -> Callable:
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, attr)


SPECS: dict[str, FlowSpec] = {
    "connect": FlowSpec(
        name="connect",
        module="network",
        loop="run_connect_loop",
        dry_run="dry_run",
        default_at=("09:30",),
        outcome_labels=CONNECT_OUTCOME_LABELS,
    ),
    "follow": FlowSpec(
        name="follow",
        module="follow",
        loop="run_follow_loop",
        dry_run="dry_run",
        default_at=("10:00",),
        outcome_labels=FOLLOW_OUTCOME_LABELS,
    ),
}

NAMES: tuple[str, ...] = tuple(SPECS)


def spec(name: str) -> FlowSpec:
    """Look up a flow descriptor, raising ``ValueError`` for an unknown name."""
    try:
        return SPECS[name]
    except KeyError:
        raise ValueError(f"unknown flow {name!r}; known: {', '.join(NAMES)}") from None


def logger(name: str) -> Callable[[str], None]:
    """A ``[<flow>] message`` stdout logger (docker logs / ``serve`` console)."""
    prefix = spec(name).name

    def _log(message: str) -> None:
        print(f"[{prefix}] {message}", flush=True)

    return _log


def stop_reason(page, cfg) -> str | None:
    """Shared health stop: the kill-switch file or a checkpoint URL, else ``None``."""
    if monitor.kill_switch_present(cfg.paths.kill_file):
        return "kill-switch"
    if monitor.is_checkpoint_url(page.url):
        return "checkpoint"
    return None


def new_stats() -> dict:
    """A fresh per-run outcome tally; ``reason`` is filled in when the run ends."""
    return {"sent": 0, "errors": 0, "unknown": 0, "refreshed": 0, "reason": "end"}


def finish(conn, name: str, stats: dict) -> None:
    """Record the per-run ``RUN_END`` event (counts + stop reason) for ``name``."""
    db.record_event(
        conn,
        "RUN_END",
        f"sent={stats['sent']} errors={stats['errors']} "
        f"unknown={stats['unknown']} reason={stats['reason']}",
        name,
    )


def usage(cfg, conn) -> dict[str, dict]:
    """Caps/usage for every flow, keyed by flow name (for status + dashboard)."""
    return {name: scheduler.remaining_today(cfg, conn, kind=name) for name in NAMES}


def caps_lines(usage_by_flow: dict[str, dict]) -> list[str]:
    """Human cap-usage lines shared by the ``status`` command and the dashboard."""
    lines: list[str] = []
    for name in NAMES:
        u = usage_by_flow[name]
        lines.append(
            f"{name} today: {u['sent_today']}/{u['day_cap']} sent, "
            f"{u['day_remaining']} remaining"
        )
        lines.append(
            f"{name} week : {u['sent_week']}/{u['week_cap']} sent, "
            f"{u['week_remaining']} remaining"
        )
    return lines
