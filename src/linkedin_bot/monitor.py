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
