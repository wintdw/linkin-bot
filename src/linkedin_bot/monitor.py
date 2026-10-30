"""Health checks that can halt a run: kill switch, checkpoints, security text."""

from __future__ import annotations

import re
from pathlib import Path

CHECKPOINT_PATTERNS = (
    re.compile(r"/checkpoint/"),
    re.compile(r"/authwall"),
    re.compile(r"challenge"),
)

SECURITY_MARKERS = (
    "unusual activity",
    "quick security check",
    "security verification",
    "verify it's you",
    "we noticed some unusual activity",
)

# LinkedIn rejects a Connect click with a transient error toast when the
# invitation limit is hit, e.g. "Your invitation to X was not sent because you
# have reached the weekly limit for connection invitations. Please try again
# next week." The wording drifts, so match the stable fragments.
INVITATION_LIMIT_RE = re.compile(
    r"was not sent because"
    r"|limit for connection invitations"
    r"|(weekly|daily) invitation limit",
    re.IGNORECASE,
)


def is_invitation_limit_text(text: str) -> bool:
    return bool(INVITATION_LIMIT_RE.search(text or ""))


def kill_switch_present(path: str) -> bool:
    return Path(path).exists()


def is_checkpoint_url(url: str | None) -> bool:
    if not url:
        return False
    return any(pattern.search(url) for pattern in CHECKPOINT_PATTERNS)


def is_security_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in SECURITY_MARKERS)


def looks_like_checkpoint(page) -> bool:
    """Cheap screen for checkpoint pages, meant to run every few actions."""
    if is_checkpoint_url(page.url):
        return True
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    return is_security_text(body_text)


# LinkedIn's edge (Cloudflare) refuses a browser outright: HTTP 403 with
# "Attention Required!" / "Sorry, you have been blocked". That is NOT a stale
# cookie, but the flows used to report it as one ("run `linkedin-bot login`"),
# which is a dead end - logging in again cannot clear an edge block. Live, Oct
# 2026: this cost four days of runs that all looked like a dead session.
BLOCK_MARKERS = (
    "attention required",
    "you have been blocked",
    "unable to access linkedin.com",
    "checking your browser",
    "just a moment",
    "cf-error",
)
BLOCK_STATUSES = (403, 429)


def is_block_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def looks_like_block(page, response=None) -> bool:
    """True when the edge refused this browser (block/rate limit), not a bad cookie.

    Matches the block page's own words, so a normal login wall (which also
    redirects to /uas/login) is not mistaken for a block; a 403/429 with no body
    read is still treated as a block, because that is what the edge returns.
    """
    try:
        status = getattr(response, "status", None)
    except Exception:
        status = None
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = page.locator("body").inner_text(timeout=2000)
    except Exception:
        body = ""
    if is_block_text(f"{title} {body}"):
        return True
    return status in BLOCK_STATUSES



def describe_page(page, limit: int = 200) -> str:
    """One-line "where are we" for logs: URL, title and a text snippet.

    Used when a run finds nothing or the session is rejected, so the log says
    what LinkedIn actually showed (limit notice, restriction, login wall).
    """
    try:
        title = page.title()
    except Exception:
        title = "?"
    try:
        text = " ".join(page.locator("body").inner_text(timeout=2000).split())[:limit]
    except Exception:
        text = ""
    note = " [LinkedIn limit notice]" if is_invitation_limit_text(text) else ""
    return f"url={page.url} title={title!r} text={text!r}{note}"
