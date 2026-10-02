"""Invite your connections to follow a LinkedIn Page you manage (e.g. Atento).

Second, independent flow beside the personal Connect flow (``network.py``). It
shares the account, so it must never run at the same time as a connect run - the
``serve`` service serializes both on one lock.

Mechanic (verified against live LinkedIn, Oct 2026): the Page admin dashboard
exposes an **"Invite to follow"** link that opens a dialog. The dialog holds a
``ul[role=listbox]`` of candidate rows (20 at a time, with "Show more results"),
each row a checkbox; a "N selected" counter; and ONE bulk **"Invite N"** button
that sends the invitations (disabled until at least one row is selected). There
are NO per-row Invite buttons, and each invite spends a credit ("50/50 credits
available"). So this flow is **credit-driven**: it selects every not-yet-seen row
the credits allow, submits them, and reopens the dialog for the next rows until
the credits run out - it never clicks "Unselect all" or the filter controls.
"""

from __future__ import annotations

import re
import time

from . import db, monitor, scheduler

FEED_URL = "https://www.linkedin.com/feed/"
LISTBOX_SELECTOR = 'ul[role="listbox"]'
# The footer submit button: "Invite" before anything is selected, "Invite 3" after.
SUBMIT_NAME_RE = re.compile(r"^invite(\s+\d+)?$", re.IGNORECASE)
CREDITS_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*credits", re.IGNORECASE)
SELECTED_RE = re.compile(r"(\d+)\s+selected", re.IGNORECASE)
# A submitted row's label flips from "Select <Name>" to "Invited".
INVITED_RE = re.compile(r"\binvited\b", re.IGNORECASE)
# After a batch is submitted the same dialog flips in place to a confirmation
# ("Invitations sent") with no rows and no Show-more control. It does not close
# on Escape, so _dialog() still reports it open; it must be dismissed explicitly.
SENT_RE = re.compile(r"invitations?\s+sent", re.IGNORECASE)
DISMISS_NAME_RE = re.compile(r"dismiss|close", re.IGNORECASE)

OUTCOME_LABELS = {
    "SENT_PENDING": "invited",
    "UNKNOWN": "unknown (counted as sent)",
    "ERROR": "error",
    "LIMIT": "linkedin limit",
}


def _log(message: str) -> None:
    """Emit one action line to stdout (docker logs / `serve` console)."""
    print(f"[follow] {message}", flush=True)


def _dialog(page):
    """The open invite dialog, or ``None``."""
    try:
        dialog = page.get_by_role("dialog")
        return dialog.first if dialog.count() > 0 else None
    except Exception:
        return None


def _dialog_text(page) -> str:
    dialog = _dialog(page)
    if dialog is None:
        return ""
    try:
        return dialog.inner_text(timeout=2000) or ""
    except Exception:
        return ""


def invitations_sent(page) -> bool:
    """True when the dialog is the post-submit "Invitations sent" confirmation."""
    return bool(SENT_RE.search(_dialog_text(page)))


def credits_available(page) -> int | None:
    """Remaining invite credits parsed from "N/M credits available", or ``None``."""
    match = CREDITS_RE.search(_dialog_text(page))
    return int(match.group(1)) if match else None


def run_budget(
    available: int | None, cap_remaining: int, extra_limit: int | None = None
) -> tuple[int, str]:
    """This run's invite budget and the reason the run stops when it hits zero.

    The Page's remaining credits ("N/M credits available") are the real budget,
    so a run spends every credit instead of spreading invites across days.
    ``follow.caps.*`` (``cap_remaining``) is only a fallback for a dialog whose
    credit counter cannot be read; ``extra_limit`` (``--limit``) caps one run and
    binds first when it is the smaller of the two.
    """
    if available is not None:
        remaining, reason = available, "credits"
    else:
        remaining, reason = cap_remaining, "cap"
    if extra_limit is not None and extra_limit < remaining:
        remaining, reason = extra_limit, "limit"
    return remaining, reason


def selected_count(page) -> int | None:
    """"N selected" counter, or ``None`` if not shown."""
    match = SELECTED_RE.search(_dialog_text(page))
    return int(match.group(1)) if match else None


def submit_locator(page):
    """The bulk "Invite [N]" submit button inside the dialog, or ``None``."""
    dialog = _dialog(page)
    if dialog is None:
        return None
    try:
        button = dialog.get_by_role("button", name=SUBMIT_NAME_RE)
        return button.first if button.count() > 0 else None
    except Exception:
        return None


def row_checkboxes(page) -> list:
    """Row checkboxes inside the dialog's listbox (empty if the dialog/list is absent)."""
    dialog = _dialog(page)
    scope = dialog if dialog is not None else page
    try:
        listbox = scope.locator(LISTBOX_SELECTOR)
        if listbox.count() == 0:
            return []
        return listbox.first.locator('input[type="checkbox"]').all()
    except Exception:
        return []


def _row_name(checkbox, timeout_ms: int) -> str | None:
    """Person name for a row checkbox.

    A row reads "Select <Name>" (checkbox label) when unselected and
    "Selected <Name>" once checked; the name is the first non-label line.
    """
    try:
        text = checkbox.evaluate(
            "(el) => { const li = el.closest('li') || el.parentElement;"
            " return li ? (li.innerText || '') : ''; }"
        )
    except Exception:
        return None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("selected "):
            name = line[len("selected "):].strip()
            if name:
                return name
            continue
        if lowered.startswith("select "):
            continue
        return line
    return None


def _select_up_to(page, cfg, seen: set[str], want: int) -> list[str]:
    """Check up to *want* not-yet-seen row checkboxes; return the names selected.

    Re-queries the list after each check (the DOM re-renders), de-duplicates by
    the row name, and never touches "Unselect all" or the filter controls.
    """
    timeout_ms = int(cfg.follow.click_timeout_ms)
    picked: list[str] = []
    while len(picked) < want:
        progressed = False
        for checkbox in row_checkboxes(page):
            name = _row_name(checkbox, timeout_ms)
            if not name or name in seen:
                continue
            if checkbox.is_checked():
                seen.add(name)
                continue
            try:
                checkbox.check(force=True, timeout=timeout_ms)
            except Exception:
                seen.add(name)  # unclickable row -> stop re-offering it
                continue
            seen.add(name)
            picked.append(name)
            progressed = True
            break
        if not progressed:
            break
    return picked


def _show_more(page, cfg) -> bool:
    """Load more candidate rows; return True if a control was clicked."""
    dialog = _dialog(page)
    scope = dialog if dialog is not None else page
    for name in ("Show more results", "Show more"):
        try:
            button = scope.get_by_role("button", name=name, exact=True)
            if button.count() > 0 and button.first.is_enabled():
                button.first.click(timeout=int(cfg.follow.click_timeout_ms))
                scheduler.sleep_post_scroll(cfg)
                return True
        except Exception:
            continue
    return False


def _submit(page, cfg) -> str:
    """Click the bulk "Invite N" button and classify the outcome.

    Returns SENT_PENDING, LIMIT, or UNKNOWN. A successful submit keeps the dialog
    open but spends a credit per invited row (the header drops from "N/M credits"
    to "(N-k)/M") and clears the selection, so success is read from a credit drop
    (or the dialog closing / the selection resetting to zero).
    """
    button = submit_locator(page)
    if button is None:
        return "ERROR"
    before_credits = credits_available(page)
    before_selected = selected_count(page)
    before_invited = len(INVITED_RE.findall(_dialog_text(page)))
    try:
        button.click(timeout=int(cfg.follow.click_timeout_ms))
    except Exception:
        return "ERROR"

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        text = _dialog_text(page)
        if monitor.is_invitation_limit_text(text):
            return "LIMIT"
        # The dialog flips to an explicit "Invitations sent" confirmation on success.
        if SENT_RE.search(text):
            return "SENT_PENDING"
        if _dialog(page) is None:
            return "SENT_PENDING"
        # Invited rows relabel to "Invited" (the open header does not live-update).
        if len(INVITED_RE.findall(text)) > before_invited:
            return "SENT_PENDING"
        after_credits = credits_available(page)
        if before_credits is not None and after_credits is not None and after_credits < before_credits:
            return "SENT_PENDING"
        if before_selected and selected_count(page) == 0:
            return "SENT_PENDING"
        page.wait_for_timeout(500)
    return "LIMIT" if monitor.is_invitation_limit_text(_dialog_text(page)) else "UNKNOWN"


def _stop_reason_for(page, cfg) -> str | None:
    if monitor.kill_switch_present(cfg.paths.kill_file):
        return "kill-switch"
    if monitor.is_checkpoint_url(page.url):
        return "checkpoint"
    return None


def goto_page(page, cfg, timeout_ms: int) -> None:
    """Warm up on the feed (completes the remember-me handshake), then open the Page.

    Deep-linking straight to the Page admin from a restored session hits
    LinkedIn's ``/authwall``; going through ``/feed/`` first avoids it. With
    ``follow.page_url`` set we navigate there directly, else pick the Page's
    ``/company/...`` admin link from the left rail by ``follow.page_name``.
    """
    page.goto(FEED_URL, wait_until="domcontentloaded")
    scheduler.sleep_post_scroll(cfg)

    url = str(cfg.follow.page_url or "").strip()
    if url:
        page.goto(url, wait_until="domcontentloaded")
        scheduler.sleep_post_scroll(cfg)
        return

    name = str(cfg.follow.page_name).strip().lower()
    for link in page.locator('a[href*="/company/"]').all():
        try:
            text = (link.inner_text(timeout=500) or "").strip()
        except Exception:
            continue
        if text.lower() == name:
            link.click(timeout=timeout_ms)
            scheduler.sleep_post_scroll(cfg)
            return
    # Fall back to a role-based match if the text walk found nothing.
    fallback = page.get_by_role("link", name=cfg.follow.page_name, exact=True)
    if fallback.count() > 0:
        fallback.first.click(timeout=timeout_ms)
        scheduler.sleep_post_scroll(cfg)


def close_invite_modal(page, cfg) -> bool:
    """Dismiss the invite dialog, including its post-submit confirmation.

    Escape does **not** close this dialog; the artdeco close button does.
    Returns True once no dialog remains.
    """
    dialog = _dialog(page)
    if dialog is None:
        return True
    timeout_ms = int(cfg.follow.click_timeout_ms)
    for selector in ("button.artdeco-modal__dismiss", "[data-test-modal-close-btn]"):
        try:
            control = dialog.locator(selector)
            if control.count() > 0:
                control.first.click(timeout=timeout_ms)
                scheduler.sleep_post_scroll(cfg)
                if _dialog(page) is None:
                    return True
        except Exception:
            continue
    try:
        control = dialog.get_by_role("button", name=DISMISS_NAME_RE)
        if control.count() > 0:
            control.first.click(timeout=timeout_ms)
            scheduler.sleep_post_scroll(cfg)
    except Exception:
        pass
    return _dialog(page) is None


def open_invite_modal(page, cfg, timeout_ms: int) -> bool:
    """Click the "Invite to follow" control; return True once the dialog is open."""
    dialog = _dialog(page)
    if dialog is not None and not invitations_sent(page):
        return True
    if dialog is not None:
        close_invite_modal(page, cfg)
    text = str(cfg.follow.invite_button_text)
    for role in ("link", "button"):
        try:
            control = page.get_by_role(role, name=text, exact=False)
            if control.count() > 0:
                control.first.click(timeout=timeout_ms)
                scheduler.sleep_post_scroll(cfg)
                return _dialog(page) is not None
        except Exception:
            continue
    return False


def count_invitable_rows(page, cfg) -> tuple[int | None, int | None]:
    """Dry-run helper: open the dialog and report (row count, credits), clicking nothing."""
    timeout_ms = int(cfg.follow.click_timeout_ms)
    goto_page(page, cfg, timeout_ms)
    if not open_invite_modal(page, cfg, timeout_ms):
        return None, None
    return len(row_checkboxes(page)), credits_available(page)


def run_follow_loop(page, cfg, conn, extra_limit: int | None = None) -> dict:
    """Select candidates in the Page's "Invite to follow" dialog and submit them.

    Credit-driven: selects every not-yet-seen row the available credits allow,
    submits them with the dialog's bulk Invite button, and reopens for the next
    rows until the credits run out (``--limit`` caps one run; ``follow.caps.*``
    is only a fallback when the credit counter can't be read). Every selected
    person is recorded with ``kind='follow'``.
    """
    stats = {"sent": 0, "errors": 0, "unknown": 0, "refreshed": 0, "reason": "end"}
    if not bool(cfg.follow.enabled):
        _log("follow flow disabled in config (follow.enabled=false)")
        stats["reason"] = "disabled"
        return stats

    click_timeout_ms = int(cfg.follow.click_timeout_ms)
    max_empty = max(1, int(getattr(cfg.follow, "max_empty_refreshes", 3) or 3))
    budget = extra_limit

    goto_page(page, cfg, click_timeout_ms)
    if not open_invite_modal(page, cfg, click_timeout_ms):
        _log("could not open the Invite to follow dialog")
        stats["reason"] = "no-modal"
        db.record_event(conn, "RUN_END", "sent=0 reason=no-modal", "follow")
        return stats

    available = credits_available(page)
    if available is None:
        _log("credit counter unreadable - falling back to follow.caps.*")
    cap_remaining = scheduler.remaining_today(cfg, conn, kind="follow")["remaining"]
    remaining, exhausted_reason = run_budget(available, cap_remaining, budget)
    _log(
        f"page {cfg.follow.page_name or cfg.follow.page_url}: "
        f"{len(row_checkboxes(page))} candidates, {available} credit(s) available, "
        f"{remaining} allowed this run"
    )

    seen: set[str] = set()
    empty = 0
    stop_reason = None

    while stop_reason is None:
        stop_reason = _stop_reason_for(page, cfg)
        if stop_reason:
            _log(f"stopping: {stop_reason}")
            break

        if remaining <= 0:
            stop_reason = exhausted_reason
            _log(f"stopping: {stop_reason} reached (0 remaining)")
            break

        picked = _select_up_to(page, cfg, seen, remaining)
        if not picked:
            empty += 1
            _log(f"no new rows (dry pass {empty}/{max_empty})")
            if empty >= max_empty:
                stop_reason = "exhausted"
                _log("stopping: no new rows after loading more")
                break
            if _show_more(page, cfg):
                stats["refreshed"] += 1
                continue
            scheduler.sleep_refresh(cfg)
            continue

        empty = 0
        outcome = _submit(page, cfg)
        for name in picked:
            db.record_invite(conn, outcome=outcome, name=name, kind="follow")
        if outcome == "SENT_PENDING":
            stats["sent"] += len(picked)
        elif outcome == "LIMIT":
            stats["errors"] += 1
            stop_reason = "linkedin-limit"
            _log("stopping: LinkedIn invitation-limit notice")
            break
        else:
            stats["unknown"] += len(picked)
        _log(f"invited {len(picked)} -> {OUTCOME_LABELS.get(outcome, outcome)}: {', '.join(picked)}")

        remaining -= len(picked)
        if budget is not None:
            budget -= len(picked)

        if monitor.looks_like_checkpoint(page):
            stop_reason = "checkpoint"
            _log("stopping: security text / checkpoint detected on page")
            break

        # A submit flips the dialog in place to an "Invitations sent" confirmation
        # (no rows, no Show-more) that stays open, so _dialog() alone still says
        # "open". Dismiss it and reopen to get a fresh batch of candidates.
        if invitations_sent(page) or _dialog(page) is None:
            close_invite_modal(page, cfg)
            if remaining > 0 and not open_invite_modal(page, cfg, click_timeout_ms):
                stop_reason = "no-modal"
                _log("stopping: invite dialog closed and could not be reopened")
                break

        scheduler.sleep_between(cfg)

    stats["reason"] = stop_reason or "end"
    db.record_event(
        conn,
        "RUN_END",
        f"sent={stats['sent']} errors={stats['errors']} "
        f"unknown={stats['unknown']} reason={stats['reason']}",
        "follow",
    )
    return stats
