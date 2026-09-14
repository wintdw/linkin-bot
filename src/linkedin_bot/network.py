"""Scan the My Network page and click Connect on suggestion cards."""

from __future__ import annotations

import re
import time

from . import db, monitor, scheduler

CONNECT_LABEL = "Connect"

OUTCOME_LABELS = {
    "SENT_PENDING": "sent",
    "UNKNOWN": "unknown (counted as sent)",
    "ERROR": "error",
    "LIMIT": "linkedin limit",
}

# Suggestion cards no longer expose the profile link next to the button, but the
# Connect button still carries a stable member id:
#   componentkey="ConnectButtonstate:invitation:urn:li:member:<id>_connect"
_MEMBER_URN_RE = re.compile(r"urn:li:member:(\d+)")


def _log(message: str) -> None:
    """Emit one action line to stdout (docker logs / `serve` console)."""
    print(f"[run] {message}", flush=True)


def connect_locator(page):
    """Suggestion-card Connect buttons.

    Linked pages compute accessible names that do not match exactly (0 hits with
    exact=True, 8 with substring match), so match loosely, case-insensitively.
    """
    return page.get_by_role("button", name=CONNECT_LABEL)


def _card_meta(button, timeout_ms: int) -> tuple[str | None, str | None, str | None]:
    """Best-effort ``(profile_url, name, member_urn)`` for the card owning this button.

    The grow feed no longer keeps the profile link in the button's immediate
    parent, so walk a few ancestors up to the card that owns the ``/in/`` anchor.
    The member URN comes from the button's ``componentkey`` and is the stable
    identity used for de-duplication.
    """
    member = None
    try:
        key = button.get_attribute("componentkey", timeout=timeout_ms)
        match = _MEMBER_URN_RE.search(key or "")
        if match:
            member = f"urn:li:member:{match.group(1)}"
    except Exception:
        pass

    try:
        data = button.evaluate(
            "(el) => {"
            " let card = el;"
            " for (let i = 0; i < 6 && card.parentElement; i++) {"
            "   card = card.parentElement;"
            "   if (card.querySelector('a[href*=\"/in/\"]')) break;"
            " }"
            " const a = card.querySelector('a[href*=\"/in/\"]');"
            " const label = el.getAttribute('aria-label') || '';"
            " const m = label.match(/^invite (.+) to connect$/i);"
            " let name = m ? m[1] : null;"
            " if (!name && a) {"
            "   const alt = a.getAttribute('aria-label') || a.innerText || a.textContent || '';"
            "   name = (alt.split('\\n')[0] || '').trim() || null;"
            " }"
            " return { href: a ? a.href : null, name: name };"
            "}",
            timeout=timeout_ms,
        )
        return data.get("href"), (data.get("name") or None), member
    except Exception:
        return None, None, member


def _invitation_limit_shown(page) -> bool:
    """LinkedIn's transient rejection toast (see ``monitor.INVITATION_LIMIT_RE``).

    The click still fires the server action; when the weekly/daily invitation
    limit is hit the only signal is this toast — the Connect button itself never
    changes state (it stays "Connect"), so it must be checked before declaring a
    send.
    """
    try:
        return page.get_by_text(monitor.INVITATION_LIMIT_RE).count() > 0
    except Exception:
        return False


def _invite_label(button, timeout_ms: int) -> str | None:
    try:
        return button.get_attribute("aria-label", timeout=timeout_ms)
    except Exception:
        return None


def _visible_connects(page) -> list:
    """Connect-button locators currently visible, in page order.

    Re-queried before every click: LinkedIn swaps a sent card's button to
    "Pending", so a list collected once goes stale and the tail indices stop
    resolving (each miss then waits out the page timeout).
    """
    return [button for button in connect_locator(page).all() if button.is_visible()]


def _label_name(label: str | None) -> str:
    """Readable person name from an "Invite <Name> to connect" aria-label."""
    if not label:
        return "?"
    return re.sub(r"^invite (.+) to connect$", r"\1", label, flags=re.IGNORECASE)


def _card_identity(button, timeout_ms: int) -> str | None:
    """Stable per-person identity for a Connect button.

    Prefers the member URN from ``componentkey`` (survives re-renders and label
    variants); falls back to the aria-label. ``None`` means the button could not
    be identified.
    """
    try:
        key = button.get_attribute("componentkey", timeout=timeout_ms)
    except Exception:
        key = None
    match = _MEMBER_URN_RE.search(key or "")
    if match:
        return f"urn:li:member:{match.group(1)}"
    return _invite_label(button, timeout_ms)


def _next_unseen(buttons, seen: set[str], timeout_ms: int):
    """First Connect button whose card identity is not already in *seen*.

    Identity is the member URN (``componentkey``), else the aria-label. Buttons
    with no resolvable identity are skipped rather than retried: clicking them has
    only ever produced no-op sends, and re-offering them on every pass used to
    spam the log with "unidentified card -> unknown". Adds the chosen identity to
    *seen* and returns the button, or ``None`` when every card has been handled.
    """
    for button in buttons:
        identity = _card_identity(button, timeout_ms)
        if identity is None:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        return button
    return None


def _button_state(handle) -> dict | None:
    """Current DOM state of a pinned Connect button, or ``None`` if unusable."""
    try:
        return handle.evaluate(
            "el => ({connected: el.isConnected, text: (el.innerText || ''),"
            " label: el.getAttribute('aria-label')})"
        )
    except Exception:
        return None


def _classify_button_state(state: dict | None, label_before: str | None) -> str | None:
    """Outcome implied by the button state, or ``None`` when nothing changed yet.

    A detached node (``connected`` false) means the card was removed after a
    successful send. Playwright does *not* raise on a detached element handle — it
    hands back stale text/label — so detachment must be read explicitly.
    """
    if state is None or not state.get("connected"):
        return "SENT_PENDING"  # node removed -> invitation accepted
    text = (state.get("text") or "").lower()
    if "pending" in text or "sent" in text or "withdraw" in text:
        return "SENT_PENDING"
    label = state.get("label")
    if label is not None and label_before is not None and label != label_before:
        return "SENT_PENDING"  # node recycled for a new suggestion -> send happened
    return None


def _click_connect(page, button, timeout_ms: int = 2000) -> str:
    """Send the invitation and classify the outcome.

    Returns SENT_PENDING, ERROR, UNKNOWN, or LIMIT.

    Physical (mouse) clicks occasionally get swallowed by transient ad overlays
    on the grow feed, so we dispatch the click directly on the button element.
    LinkedIn removes a sent card from the DOM, so success is detected by the
    pinned node detaching (``isConnected``) — not by an exception, since a
    detached handle still answers with stale text and label. Every element op
    carries a short timeout so a stale card fails fast instead of waiting out the
    page default.

    The button is pinned to an element handle first: the locator returned by
    ``connect_locator`` re-resolves by index, and the list shifts as cards are
    sent, so comparing a re-resolved locator's label would report a send even
    when the same card is rejected.
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

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        # A rejected click (invitation limit) shows only a transient toast; the
        # button stays "Connect", so check this before anything else.
        if _invitation_limit_shown(page):
            return "LIMIT"

        # Some layouts open a modal asking whether to add a note; send without one.
        try:
            modal = page.get_by_role("button", name="Send without a note", exact=True)
            if modal.is_visible():
                modal.click(timeout=timeout_ms)
                return "SENT_PENDING"
        except Exception:
            pass

        verdict = _classify_button_state(_button_state(handle), label_before)
        if verdict:
            return verdict
        page.wait_for_timeout(200)

    return "LIMIT" if _invitation_limit_shown(page) else "UNKNOWN"


def _stop_reason_for(page, cfg) -> str | None:
    if monitor.kill_switch_present(cfg.paths.kill_file):
        return "kill-switch"
    if monitor.is_checkpoint_url(page.url):
        return "checkpoint"
    return None


def run_connect_loop(page, cfg, conn, extra_limit: int | None = None) -> dict:
    """Click every visible Connect, then wait and reload for fresh suggestions.

    One cycle clicks all Connect buttons on the page (never past the caps),
    pauses ``network.refresh_wait_seconds``, reloads My Network, and repeats
    until a cap, a health stop, or ``network.max_empty_refreshes`` reloads that
    surface no cards at all.
    """
    stats = {"sent": 0, "errors": 0, "unknown": 0, "refreshed": 0, "reason": "end"}
    scan_every = int(cfg.monitor.security_scan_every)
    refresh_wait = float(cfg.network.refresh_wait_seconds)
    max_empty_refreshes = int(cfg.network.max_empty_refreshes)
    click_timeout_ms = int(cfg.network.click_timeout_ms)
    budget = extra_limit

    page.goto(cfg.network.url, wait_until="domcontentloaded")
    scheduler.sleep_post_scroll(cfg)
    _log("scanning My Network")

    empty_refreshes = 0
    stop_reason = None
    sent_labels: set[str] = set()  # card identities already clicked this run

    while stop_reason is None:
        stop_reason = _stop_reason_for(page, cfg)
        if stop_reason:
            _log(f"stopping: {stop_reason}")
            break

        visible = _visible_connects(page)
        batch_size = len(visible)
        names = ", ".join(_label_name(_invite_label(b, click_timeout_ms)) for b in visible)
        _log(f"found {batch_size} Connect button(s)" + (f": {names}" if names else ""))

        # Click unseen cards one at a time, re-querying each time: a sent card's
        # button turns "Pending", so the list shrinks. Skipping by aria-label
        # identity avoids re-clicking a card whose Connect button lingers.
        clicked_this_batch = 0
        while clicked_this_batch < batch_size:
            stop_reason = _stop_reason_for(page, cfg)
            if stop_reason:
                _log(f"stopping: {stop_reason}")
                break

            remaining = scheduler.remaining_today(cfg, conn)["remaining"]
            if budget is not None:
                remaining = min(remaining, budget)
            if remaining <= 0:
                stop_reason = "limit" if budget is not None else "cap"
                _log(f"stopping: {stop_reason} reached (0 remaining)")
                break

            button = _next_unseen(_visible_connects(page), sent_labels, click_timeout_ms)
            if button is None:
                break  # every visible card already sent -> refresh for more

            href, name, member = _card_meta(button, click_timeout_ms)
            outcome = _click_connect(page, button, click_timeout_ms)
            db.record_invite(
                conn, outcome=outcome, profile_url=href, name=name, details=member
            )
            who = name or member or "unidentified card"
            _log(f"clicked {who} -> {OUTCOME_LABELS[outcome]}")

            if outcome == "SENT_PENDING":
                stats["sent"] += 1
            elif outcome == "ERROR":
                stats["errors"] += 1
            elif outcome == "UNKNOWN":
                stats["unknown"] += 1
            elif outcome == "LIMIT":
                stop_reason = "linkedin-limit"
                _log("stopping: LinkedIn invitation-limit notice")
                break

            clicked_this_batch += 1

            if budget is not None:
                budget -= 1

            if scan_every and stats["sent"] > 0 and stats["sent"] % scan_every == 0:
                if monitor.looks_like_checkpoint(page):
                    stop_reason = "checkpoint"
                    _log("stopping: security text / checkpoint detected on page")
                    break

            scheduler.sleep_between(cfg)

        if stop_reason:
            break

        if clicked_this_batch == 0:
            # No cards, or every visible card already sent: a feed that keeps
            # re-serving sent cards must stop, not reload forever.
            empty_refreshes += 1
            _log(f"no new cards (dry refresh {empty_refreshes}/{max_empty_refreshes})")
            if empty_refreshes > max_empty_refreshes:
                stop_reason = "exhausted"
                _log("stopping: no new cards after refreshing")
                break
        else:
            empty_refreshes = 0

        _log(f"batch done - waiting {refresh_wait:.0f}s then refreshing")
        scheduler.sleep_refresh(cfg)
        stop_reason = _stop_reason_for(page, cfg)
        if stop_reason:
            _log(f"stopping: {stop_reason}")
            break
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception as exc:
            stop_reason = "refresh-failed"
            _log(f"stopping: reload failed ({exc.__class__.__name__})")
            break
        stats["refreshed"] += 1
        scheduler.sleep_post_scroll(cfg)

    stats["reason"] = stop_reason or "end"
    db.record_event(
        conn,
        "RUN_END",
        f"sent={stats['sent']} errors={stats['errors']} "
        f"unknown={stats['unknown']} reason={stats['reason']}",
    )
    return stats
