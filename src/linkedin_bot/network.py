"""Scan the My Network page and click Connect on suggestion cards."""

from __future__ import annotations

import time

from . import db, monitor, scheduler

CONNECT_LABEL = "Connect"


def connect_locator(page):
    """Suggestion-card Connect buttons.

    Linked pages compute accessible names that do not match exactly (0 hits with
    exact=True, 8 with substring match), so match loosely, case-insensitively.
    """
    return page.get_by_role("button", name=CONNECT_LABEL)


def _card_meta(button) -> tuple[str | None, str | None]:
    """Best-effort profile link + name for the card owning this button."""
    try:
        data = button.evaluate(
            "(el) => {"
            " const card = el.closest('li') || el.parentElement || el;"
            " const a = card.querySelector('a[href*=\"/in/\"]');"
            " const label = el.getAttribute('aria-label') || '';"
            " let name = a ? (a.getAttribute('aria-label') || a.textContent.trim().slice(0, 80)) : null;"
            " if (!name && /^invite (.+) to connect$/i.test(label)) name = label.replace(/^invite (.+) to connect$/i, '$1');"
            " return { href: a ? a.href : null, name: name };"
            "}"
        )
        return data.get("href"), data.get("name")
    except Exception:
        return None, None


def _invitation_limit_shown(page) -> bool:
    try:
        marker = page.locator("text=/weekly invitation limit|daily invitation limit/i")
        return marker.count() > 0
    except Exception:
        return False


def _invite_label(button) -> str | None:
    try:
        return button.get_attribute("aria-label")
    except Exception:
        return None


def _click_connect(page, button) -> str:
    """Send the invitation and classify the outcome.

    Returns SENT_PENDING, ERROR, UNKNOWN, or LIMIT.

    Physical (mouse) clicks occasionally get swallowed by transient ad overlays
    on the grow feed, so we dispatch the click directly on the button element.
    LinkedIn replaces a sent card with a fresh suggestion, so success is detected
    by the original node detaching from the DOM.
    """
    label_before = _invite_label(button)
    try:
        button.evaluate("(el) => el.click()")
    except Exception:
        return "ERROR"

    # Some layouts open a modal asking whether to add a note; send without one.
    try:
        send_without_note = page.get_by_role("button", name="Send without a note", exact=True)
        send_without_note.wait_for(state="visible", timeout=1500)
        send_without_note.click(timeout=5000)
        return "SENT_PENDING"
    except Exception:
        pass

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _invitation_limit_shown(page):
            return "LIMIT"
        try:
            text = button.inner_text(timeout=1500)
        except Exception:
            return "SENT_PENDING"  # original card detached -> replaced after sending
        current = _invite_label(button)
        if current is not None and current != label_before:
            return "SENT_PENDING"  # node recycled for a new suggestion -> send happened
        if "pending" in text.lower() or "sent" in text.lower():
            return "SENT_PENDING"
        page.wait_for_timeout(500)
    return "UNKNOWN"


def _stop_reason_for(page, cfg) -> str | None:
    if monitor.kill_switch_present(cfg.paths.kill_file):
        return "kill-switch"
    if monitor.is_checkpoint_url(page.url):
        return "checkpoint"
    return None


def run_connect_loop(page, cfg, conn, extra_limit: int | None = None) -> dict:
    """Open My Network and click Connect until a cap, the list, or a health stop."""
    stats = {"sent": 0, "errors": 0, "unknown": 0, "scrolled": 0, "reason": "end"}
    scan_every = int(cfg.monitor.security_scan_every)
    max_scrolls = int(cfg.network.max_scrolls)
    scroll_px = int(cfg.network.scroll_px)
    budget = extra_limit

    page.goto(cfg.network.url, wait_until="domcontentloaded")
    scheduler.sleep_post_scroll(cfg)

    scroll_cycles = 0
    stop_reason = None

    while stop_reason is None:
        stop_reason = _stop_reason_for(page, cfg)
        if stop_reason:
            break

        remaining = scheduler.remaining_today(cfg, conn)["remaining"]
        if budget is not None:
            remaining = min(remaining, budget)
        if remaining <= 0:
            stop_reason = "limit" if budget is not None else "cap"
            break

        buttons = connect_locator(page).all()
        actionable = [b for b in buttons if b.is_visible()]

        if not actionable:
            if cfg.network.scroll_to_load and scroll_cycles < max_scrolls:
                page.mouse.wheel(0, scroll_px)
                scheduler.sleep_post_scroll(cfg)
                scroll_cycles += 1
                stats["scrolled"] += 1
                continue
            stop_reason = "exhausted"
            break

        for button in actionable[:remaining]:
            stop_reason = _stop_reason_for(page, cfg)
            if stop_reason:
                break

            href, name = _card_meta(button)
            outcome = _click_connect(page, button)
            db.record_invite(conn, outcome=outcome, profile_url=href, name=name)

            if outcome == "SENT_PENDING":
                stats["sent"] += 1
            elif outcome == "ERROR":
                stats["errors"] += 1
            elif outcome == "UNKNOWN":
                stats["unknown"] += 1
            elif outcome == "LIMIT":
                stop_reason = "linkedin-limit"
                break

            if budget is not None:
                budget -= 1
                if budget <= 0:
                    stop_reason = "limit"
                    break

            if scan_every and stats["sent"] > 0 and stats["sent"] % scan_every == 0:
                if monitor.looks_like_checkpoint(page):
                    stop_reason = "checkpoint"
                    break

            scheduler.sleep_between(cfg)

    stats["reason"] = stop_reason or "end"
    db.record_event(
        conn,
        "RUN_END",
        f"sent={stats['sent']} errors={stats['errors']} "
        f"unknown={stats['unknown']} reason={stats['reason']}",
    )
    return stats
