"""Login flow with manual checkpoint fallback; persists storage_state for later runs."""

from __future__ import annotations

import time
from pathlib import Path

from .monitor import looks_like_checkpoint

LOGGED_IN_MARKERS = (
    'a[href="/feed/"]',
    'a[href="/mynetwork/"]',
    'a[href*="/messaging/"]',
)


def session_exists(cfg) -> bool:
    return Path(cfg.paths.session_file).exists()


def is_logged_in(page) -> bool:
    """Logged-in pages always expose the global nav (feed/mynetwork/messaging links)."""
    try:
        for selector in LOGGED_IN_MARKERS:
            if page.locator(selector).count() > 0:
                return True
    except Exception:
        pass
    return False


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
