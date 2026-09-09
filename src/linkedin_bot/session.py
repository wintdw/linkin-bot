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


def _attempt_autofill(page, email: str, password: str, timeout_ms: int) -> bool:
    """Best-effort credential fill. Returns True when the form was submitted.

    LinkedIn localizes button text (e.g. Vietnamese "Đăng nhập"), so submit via
    the type=submit button or Enter key rather than a localized label.
    """
    try:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        email_field = page.locator("#session_key")
        email_field.wait_for(state="visible", timeout=min(timeout_ms, 10000))
        email_field.fill(email)
        page.locator("#session_password").fill(password)
        submit = page.locator('button[type="submit"]')
        if submit.count() > 0:
            submit.first.click(timeout=min(timeout_ms, 10000))
        else:
            page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def login(cfg, email: str, password: str) -> bool:
    """Visible-browser login.

    Auto-fill is best-effort. LinkedIn frequently demands a manual security check
    (OTP / CAPTCHA / verification) on fresh automated logins, so after submitting
    we keep the window open and wait for the operator to finish by hand. The
    session is only saved once the global nav is actually visible.
    """
    from playwright.sync_api import sync_playwright

    session_file = Path(cfg.paths.session_file)
    timeout_ms = int(cfg.browser.timeout_ms)
    deadline = time.monotonic() + int(cfg.login.checkpoint_wait_seconds)
    print(f"Logging in as {email} ...", flush=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            submitted = _attempt_autofill(page, email, password, timeout_ms)
            if submitted:
                print("Credentials submitted.", flush=True)
            else:
                print(
                    "Auto-fill did not complete - please sign in manually in the "
                    "browser window.",
                    flush=True,
                )
            print(
                "If a security check / OTP prompt appears, complete it in the "
                "browser window. Waiting for you to finish ...",
                flush=True,
            )

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
