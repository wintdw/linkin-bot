"""Login flow with manual checkpoint fallback; persists storage_state for later runs."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .monitor import looks_like_checkpoint

# Matched with ``href*`` (substring), not an exact href: LinkedIn renders the
# global nav with ABSOLUTE urls now ("https://www.linkedin.com/mynetwork", note
# no trailing slash), so the old 'a[href="/mynetwork/"]' matched 0 elements and
# a perfectly valid session was reported invalid (live, Oct 2026).
LOGGED_IN_MARKERS = (
    'a[href*="/feed"]',
    'a[href*="/mynetwork"]',
    'a[href*="/messaging/"]',
)

# Cloudflare sits in front of LinkedIn and refuses the headless shell's UA
# (``HeadlessChrome/<version>``) with "Attention Required! ... you have been
# blocked", even for an anonymous, cookie-less context. Sending an ordinary
# desktop Chrome UA gets a clean 200 from the very same container (verified Oct
# 2026), so every context is opened with this one; the version is filled from the
# running browser so a Playwright upgrade cannot desync it.
USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version} Safari/537.36"
)


def user_agent(browser) -> str:
    """A normal desktop Chrome UA matching the launched browser's real version."""
    version = getattr(browser, "version", "") or "151.0.0.0"
    return USER_AGENT_TEMPLATE.format(version=version)


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


class BlockedError(SessionInvalidError):
    """LinkedIn's edge refused this browser — NOT a stale cookie.

    Subclasses :class:`SessionInvalidError` so any handler that does not know
    about edge blocks still treats it as "this run could not reach LinkedIn";
    the CLI catches it first and maps it to its own exit code, because the fix
    is waiting/rotating the egress, never ``login``.
    """



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


def _apply_stealth(context, page, browser) -> None:
    """Make the context read as ordinary desktop Chrome where the edge looks.

    Verified live (Oct 2026), in the bot's own container: passing ``user_agent=``
    rewrites the header *and* ``navigator.userAgent``, but leaves
    ``navigator.userAgentData.brands`` as ``HeadlessChrome/151`` and
    ``navigator.webdriver`` as ``true``. Cloudflare matched the UA header today;
    the same marker in Client Hints is one query away, so close both: the launch
    flag clears ``webdriver`` and this CDP override rewrites the brands.
    Best-effort — a CDP hiccup must not fail a run (the UA header still hides the
    headless token).
    """
    version = getattr(browser, "version", "") or "151.0.0.0"
    major = version.split(".")[0]
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not=A?Brand", "version": "99"},
    ]
    full = [{"brand": "Chromium", "version": version}, {"brand": "Google Chrome", "version": version}]
    full.append({"brand": "Not=A?Brand", "version": "99.0.0.0"})
    try:
        context.new_cdp_session(page).send(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": user_agent(browser),
                "acceptLanguage": "en-US,en;q=0.9",
                "platform": "Linux x86_64",
                "userAgentMetadata": {
                    "brands": brands,
                    "fullVersionList": full,
                    "fullVersion": version,
                    "platform": "Linux",
                    "platformVersion": "6.8.0",
                    "architecture": "x86",
                    "model": "",
                    "mobile": False,
                },
            },
        )
    except Exception:  # noqa: BLE001 - cosmetic; never break a run over it
        pass


def _new_page(browser, cfg):
    """A context + page that restores the saved session and looks like desktop Chrome."""
    context = browser.new_context(
        storage_state=str(Path(cfg.paths.session_file)),
        user_agent=user_agent(browser),
        viewport={"width": 1366, "height": 900},
    )
    page = context.new_page()
    _apply_stealth(context, page, browser)
    page.set_default_timeout(int(cfg.browser.timeout_ms))
    return context, page


def open_logged_in_page(playwright, cfg, *, headed: bool = False):
    """Launch chromium with the saved session; return ``(browser, context, page)`` on the feed.

    Shared by every browser entry point (the flows in ``cli.run_flow_once`` and
    the Page publisher in ``publish``). Raises :class:`SessionInvalidError` on a
    redirect loop (dead session) or when the global nav never appears, and the
    narrower :class:`BlockedError` when LinkedIn's edge refused the browser (a
    block is retried a few times first, since it is often transient). On any
    failure the launched browser is closed before the error propagates.
    """
    from .flow import FEED_URL

    browser = playwright.chromium.launch(
        headless=bool(cfg.browser.headless) and not headed,
        args=["--disable-blink-features=AutomationControlled"],
    )
    retries = max(0, int(getattr(cfg.browser, "block_retries", 1)))
    backoff = max(0, int(getattr(cfg.browser, "block_backoff_seconds", 120)))
    attempts = retries + 1
    try:
        last_block: str | None = None
        for attempt in range(1, attempts + 1):
            context, page = _new_page(browser, cfg)
            try:
                response = page.goto(FEED_URL, wait_until="domcontentloaded")
            except Exception as exc:
                context.close()
                detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                raise SessionInvalidError(f"could not load {FEED_URL} ({detail})") from None

            from .monitor import describe_page, looks_like_block

            if looks_like_block(page, response):
                last_block = describe_page(page)
                context.close()
                if attempt < attempts:
                    print(
                        f"[session] LinkedIn's edge refused this browser "
                        f"(attempt {attempt}/{attempts}); retrying in {backoff}s",
                        flush=True,
                    )
                    time.sleep(backoff)
                continue

            if not _wait_logged_in(page):
                context.close()
                raise SessionInvalidError(
                    f"the saved session is no longer valid ({describe_page(page)})"
                )
            return browser, context, page
        raise BlockedError(
            f"LinkedIn's edge is blocking this browser (not a stale cookie): {last_block}"
        )
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
