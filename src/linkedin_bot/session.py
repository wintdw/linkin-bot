"""Login flow with manual checkpoint fallback; persists storage_state for later runs."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .monitor import looks_like_checkpoint

LOGGED_IN_MARKERS = (
    'a[href="/feed/"]',
    'a[href="/mynetwork/"]',
    'a[href*="/messaging/"]',
)


def session_exists(cfg) -> bool:
    return Path(cfg.paths.session_file).exists()


def session_status(cfg, now: datetime | None = None) -> dict:
    """Metadata about the saved browser session (the cookie store), never the values.

    This is the ``storage_state`` file of LinkedIn cookies (``li_at`` etc.)
    written by ``login`` — reported in ``/status``. It reports presence, when the
    session was saved and which cookies it holds; it deliberately does **not**
    hit LinkedIn, so it cannot prove the session is still accepted (only a run,
    or a fresh ``login``, can — a hard-invalidated session 302-loops).
    """
    path = Path(cfg.paths.session_file)
    if not path.exists():
        return {
            "present": False,
            "saved_at": None,
            "age_days": None,
            "cookies": [],
            "has_li_at": False,
            "summary": "session: none - run `linkedin-bot login`",
        }

    now = now or datetime.now()
    saved = datetime.fromtimestamp(path.stat().st_mtime)
    cookies: list[str] = []
    try:
        state = json.loads(path.read_text())
        cookies = [c.get("name") for c in state.get("cookies", []) if c.get("name")]
    except Exception:
        pass
    age_days = (now - saved).total_seconds() / 86400
    return {
        "present": True,
        "saved_at": saved.isoformat(timespec="seconds"),
        "age_days": round(age_days, 1),
        "cookies": cookies,
        "has_li_at": "li_at" in cookies,
        "summary": (
            f"session: saved {saved:%Y-%m-%d %H:%M} "
            f"({age_days:.1f} day(s) ago, {len(cookies)} cookie(s))"
        ),
    }



def is_logged_in(page) -> bool:
    """Logged-in pages always expose the global nav (feed/mynetwork/messaging links)."""
    try:
        for selector in LOGGED_IN_MARKERS:
            if page.locator(selector).count() > 0:
                return True
    except Exception:
        pass
    return False


class SessionInvalidError(RuntimeError):
    """The saved session no longer reaches LinkedIn (redirect loop / not logged in)."""


def _wait_logged_in(page, polls: int = 10, interval_ms: int = 2000) -> bool:
    """Poll for the global nav.

    A restored session can pass through LinkedIn's transient
    ssr-login/remember-me-auto-login redirect before the nav renders, so poll
    (bounded) before declaring the session invalid.
    """
    for _ in range(polls):
        if is_logged_in(page):
            return True
        page.wait_for_timeout(interval_ms)
    return False


def open_logged_in_page(playwright, cfg, *, headed: bool = False):
    """Launch chromium with the saved session; return ``(browser, context, page)`` on the feed.

    Shared by every browser entry point (the flows in ``cli.run_flow_once`` and
    the Page publisher in ``publish``). Raises :class:`SessionInvalidError` on a
    redirect loop (dead session) or when the global nav never appears; the
    caller maps that to the documented exit code 2 (re-run ``login``). On any
    failure the launched browser is closed before the error propagates.
    """
    from .flow import FEED_URL

    browser = playwright.chromium.launch(headless=bool(cfg.browser.headless) and not headed)
    try:
        context = browser.new_context(
            storage_state=str(Path(cfg.paths.session_file)),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(int(cfg.browser.timeout_ms))
        try:
            page.goto(FEED_URL, wait_until="domcontentloaded")
        except Exception as exc:
            detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            raise SessionInvalidError(f"could not load {FEED_URL} ({detail})") from None
        if not _wait_logged_in(page):
            raise SessionInvalidError("the saved session is no longer valid")
        return browser, context, page
    except BaseException:
        browser.close()
        raise


def login(cfg) -> bool:
    """Visible-browser manual login.

    Auto-fill of the login form is deliberately not attempted: localized button
    text, empty-field pages, and an evolving DOM make it unreliable, and
    LinkedIn almost always demands a manual security check (OTP / CAPTCHA) on
    fresh logins anyway. The operator types the credentials and completes any
    check by hand; the session is saved once the global nav is actually
    visible.
    """
    from playwright.sync_api import sync_playwright

    session_file = Path(cfg.paths.session_file)
    timeout_ms = int(cfg.browser.timeout_ms)
    deadline = time.monotonic() + int(cfg.login.checkpoint_wait_seconds)
    print("Sign in manually in the browser window that just opened.", flush=True)
    print(
        "Type your credentials and complete any OTP / CAPTCHA yourself - "
        "the bot only watches and saves the session.",
        flush=True,
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            print("Waiting for you to finish signing in ...", flush=True)

            checkpoint_notice = False
            last_heartbeat = 0.0
            try:
                while not is_logged_in(page):
                    now = time.monotonic()
                    if looks_like_checkpoint(page):
                        if not checkpoint_notice:
                            checkpoint_notice = True
                            print(
                                "Security check detected - finish it by hand "
                                "(OTP / email code / CAPTCHA).",
                                flush=True,
                            )
                    elif now - last_heartbeat > 30:
                        last_heartbeat = now
                        print(
                            "Still waiting for you to finish signing in in the "
                            "browser window ...",
                            flush=True,
                        )
                    if now > deadline:
                        print("Timed out waiting for login to complete.", flush=True)
                        return False
                    page.wait_for_timeout(2000)
            except Exception as exc:
                print(
                    f"Waiting for manual login failed ({exc.__class__.__name__}: {exc}). "
                    "If you closed the browser window, run `linkedin-bot login` again.",
                    flush=True,
                )
                return False

            try:
                session_file.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(session_file))
            except Exception as exc:
                print(f"Could not save the session: {exc}", flush=True)
                return False
            print(f"Logged in. Session saved to {session_file}", flush=True)
            return True
        finally:
            browser.close()
