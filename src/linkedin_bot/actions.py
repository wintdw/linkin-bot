"""DOM-agnostic click primitives shared by the connect and follow flows.

These were lifted out of ``network.py`` (the connect flow keeps working
unchanged) so the page "invite to follow" flow reuses the exact same
hard-won rules: pin the element handle before clicking, dispatch the click
directly on the node, and read detachment from ``isConnected`` rather than
from an exception (Playwright hands back stale text for a detached handle).
"""

from __future__ import annotations

import time

from . import monitor

# Button texts that mean the action landed: the node re-rendered as pending /
# sent / withdrawable. Flows can extend this (e.g. "invited" for page follows).
DEFAULT_SUCCESS_WORDS = ("pending", "sent", "withdraw")


def button_state(handle) -> dict | None:
    """Current DOM state of a pinned button handle, or ``None`` if unusable."""
    try:
        return handle.evaluate(
            "el => ({connected: el.isConnected, text: (el.innerText || ''),"
            " label: el.getAttribute('aria-label')})"
        )
    except Exception:
        return None


def classify_button_state(
    state: dict | None,
    label_before: str | None,
    success_words: tuple[str, ...] = DEFAULT_SUCCESS_WORDS,
) -> str | None:
    """Outcome implied by the button state, or ``None`` when nothing changed yet.

    A detached node (``connected`` false) means the row was removed after a
    successful action. Playwright does *not* raise on a detached element handle -
    it hands back stale text/label - so detachment must be read explicitly.
    """
    if state is None or not state.get("connected"):
        return "SENT_PENDING"  # node removed -> action accepted
    text = (state.get("text") or "").lower()
    if any(word in text for word in success_words):
        return "SENT_PENDING"
    label = state.get("label")
    if label is not None and label_before is not None and label != label_before:
        return "SENT_PENDING"  # node recycled -> action happened
    return None


def invitation_limit_shown(page) -> bool:
    """LinkedIn's transient rejection toast (see ``monitor.INVITATION_LIMIT_RE``).

    The click still fires the server action; when the limit is hit the only
    signal is this toast - the button itself never changes state - so it must be
    checked before declaring a success.
    """
    try:
        return page.get_by_text(monitor.INVITATION_LIMIT_RE).count() > 0
    except Exception:
        return False


def click_pinned_and_classify(
    page,
    button,
    *,
    timeout_ms: int,
    success_words: tuple[str, ...] = DEFAULT_SUCCESS_WORDS,
    modal_names: tuple[str, ...] = (),
    poll_seconds: float = 3.0,
) -> str:
    """Click *button* and classify the outcome.

    Returns SENT_PENDING, ERROR, UNKNOWN, or LIMIT.

    The button is pinned to an element handle first: a locator re-resolves by
    index, and the list shifts as rows are sent, so comparing a re-resolved
    locator's label would report a send even when the same row was rejected.
    Physical (mouse) clicks can be swallowed by transient overlays, so the click
    is dispatched directly on the node. Optional ``modal_names`` are
    confirmation buttons (e.g. "Send without a note") whose appearance also
    counts as a success.
    """
    try:
        handle = button.element_handle(timeout=timeout_ms)
    except Exception:
        return "ERROR"
    if handle is None:
        return "ERROR"

    try:
        label_before = handle.get_attribute("aria-label")
    except Exception:
        label_before = None
    try:
        handle.evaluate("(el) => el.click()")
    except Exception:
        return "ERROR"

    deadline = time.monotonic() + poll_seconds
    while time.monotonic() < deadline:
        # A rejected click (limit reached) shows only a transient toast; the
        # button stays unchanged, so check this before anything else.
        if invitation_limit_shown(page):
            return "LIMIT"

        for name in modal_names:
            try:
                modal = page.get_by_role("button", name=name, exact=True)
                if modal.is_visible():
                    modal.click(timeout=timeout_ms)
                    return "SENT_PENDING"
            except Exception:
                pass

        verdict = classify_button_state(button_state(handle), label_before, success_words)
        if verdict:
            return verdict
        page.wait_for_timeout(200)

    return "LIMIT" if invitation_limit_shown(page) else "UNKNOWN"


def next_unseen(buttons, seen: set[str], timeout_ms: int, identity_fn):
    """First button whose identity is not already in *seen*.

    ``identity_fn(button, timeout_ms)`` returns a stable per-row key (member URN,
    else a label) or ``None``. Buttons with no resolvable identity are skipped
    rather than retried - clicking them has only ever produced no-op sends. Adds
    the chosen identity to *seen* and returns the button, or ``None`` when every
    row has been handled.
    """
    for button in buttons:
        identity = identity_fn(button, timeout_ms)
        if identity is None:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        return button
    return None
