"""Publish a post as a managed LinkedIn Page, through the browser session.

LinkedIn's Posts API can only author as the *member* (scope ``w_member_social``,
from the "Share on LinkedIn" product). Authoring as an organization
(``author = urn:li:organization:<id>``) needs scope ``w_organization_social``,
which LinkedIn only grants with the **Community Management API** product - which
this app does not have. So posting as the Page goes through the saved browser
session instead, the same way the connect (``network.py``) and follow
(``follow.py``) flows do, and must therefore never overlap them (``serve``
serializes everything on one lock).

The Page composer is opened from the posts admin (``post.page_url``, e.g.
``/company/<id>/admin/page-posts/published/``): a **Start a post** control opens
a dialog holding the Quill ``.ql-editor``, submitted with a **Post** button
(verified live, Oct 2026). The admin page pre-renders several inline editors
(comment boxes), so an editor merely existing does **not** mean the composer is
open - the trigger is clicked and the *dialog* is watched for.
"""

from __future__ import annotations

import re
import time

from . import monitor, scheduler, session

COMPANY_RE = re.compile(r"/company/(\d+)")
# A published post is identified by its activity URN ("urn:li:activity:<id>"),
# reachable either from a /feed/update/... permalink or a
# /posts/<slug>-activity-<id> link on the Page's published-posts admin list.
ACTIVITY_RE = re.compile(r"urn:li:activity:(\d+)")
POSTS_PERMALINK_RE = re.compile(r"/posts/[^/]*?activity-(\d+)")
# LinkedIn's share composer is a Quill editor inside a modal dialog.
EDITOR_SELECTOR = (
    "div.ql-editor[contenteditable='true'], [role='textbox'][contenteditable='true']"
)
FILE_INPUT_SELECTOR = "input[type='file']"
MAX_TEXT = 3000  # LinkedIn commentary limit
IMAGE_CONTENT_TYPE_DEFAULT = "image/jpeg"
_IMAGE_ATTACH_BUTTONS = ("Add a photo", "Add media", "Photo", "Media")
# After a photo is chosen LinkedIn shows an editing step whose primary action is
# "Next"; the "Post" submit only appears once that step is completed.
_MEDIA_NEXT_BUTTONS = ("Next", "Next step", "Continue")


class PublishError(RuntimeError):
    """A Page post could not be published (composer not found, submit failed, ...)."""


def _log(message: str) -> None:
    print(f"[post] {message}", flush=True)


def page_urn(page_url: str) -> str:
    """``urn:li:organization:<id>`` from a Page URL containing ``/company/<id>/``."""
    match = COMPANY_RE.search(str(page_url or ""))
    if not match:
        raise PublishError(f"post.page_url has no /company/<id>/ id: {page_url!r}")
    return f"urn:li:organization:{match.group(1)}"


def validate(text: str, image: bytes | None) -> str:
    """Trim *text* and reject an empty/oversized post; returns the trimmed text."""
    text = str(text or "").strip()
    if len(text) > MAX_TEXT:
        raise PublishError(f"post text is {len(text)} chars (LinkedIn max {MAX_TEXT})")
    if not text and image is None:
        raise PublishError("post needs text or an image")
    return text


def _modal(page):
    """The open composer dialog (the one holding the editor), or ``None``."""
    try:
        dialogs = page.get_by_role("dialog")
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if dialog.locator(EDITOR_SELECTOR).count() > 0:
                return dialog
    except Exception:
        pass
    return None


def _button_labels(scope) -> list[str]:
    """Visible button labels/aria-labels in *scope*, for diagnosis."""
    labels: list[str] = []
    try:
        for element in scope.get_by_role("button").all()[:40]:
            try:
                label = (element.get_attribute("aria-label") or element.inner_text() or "").strip()
            except Exception:
                label = ""
            label = " ".join(label.split())
            if label and label not in labels:
                labels.append(label)
    except Exception:
        pass
    return labels


def _submit_button(page, cfg):
    """The composer's submit control, searched permissively.

    The composer is a dialog, but LinkedIn nests dialogs and can render the
    submit outside the dialog we scoped to (or as a full-page composer), so try
    the composer dialog first and then the whole page. The configured label is
    matched exactly first, then case-insensitively as a substring, so a slightly
    reworded control ("Post now") still resolves. Returns the locator, or None.
    """
    name = str(cfg.post.submit_button_text)
    modal = _modal(page)
    scopes = [scope for scope in (modal, page) if scope is not None]
    for scope in scopes:
        for exact in (True, False):
            button = scope.get_by_role("button", name=name, exact=exact)
            if button.count() > 0:
                return button.first
    return None


def _open_composer(page, cfg, timeout_ms: int) -> bool:
    """Navigate to the Page's post admin and open the composer dialog.

    The admin page already renders several inline Quill editors (comment boxes),
    so "an editor exists" does **not** mean the composer is open - the trigger
    must be clicked and the *dialog* watched for.
    """
    page.goto(str(cfg.post.page_url), wait_until="domcontentloaded")
    scheduler.sleep_post_scroll(cfg)

    trigger = str(cfg.post.composer_button_text)
    clicked = False
    for role in ("button", "link"):
        control = page.get_by_role(role, name=trigger, exact=False)
        try:
            if control.count() > 0:
                control.first.click(timeout=timeout_ms)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        try:
            page.get_by_text(trigger, exact=False).first.click(timeout=timeout_ms)
            clicked = True
        except Exception:
            clicked = False
    if not clicked:
        return False

    scheduler.sleep_post_scroll(cfg)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _modal(page) is not None:
            return True
        page.wait_for_timeout(300)
    return False


def _fill_editor(page, text: str, timeout_ms: int) -> None:
    """Type *text* into the composer's editor (the dialog's Quill `.ql-editor`).

    The value is read back afterwards: Quill can keep ``fill()``'s DOM change out
    of its own model (the post then submits empty), so real key events are used
    when the editor is still empty.
    """
    scope = _modal(page) or page
    editor = scope.locator(EDITOR_SELECTOR).first

    def _typed() -> str:
        try:
            return (editor.inner_text(timeout=timeout_ms) or "").strip()
        except Exception:
            return ""

    want = text.strip()
    try:
        editor.click(timeout=timeout_ms)
        editor.fill(text, timeout=timeout_ms)
    except Exception:
        pass
    if want and not _typed():
        try:
            editor.click(timeout=timeout_ms)
            page.keyboard.type(text)
        except Exception as exc:
            raise PublishError(f"could not type into the composer: {exc}") from None
    if want and not _typed():
        raise PublishError("typed the post text but the composer stayed empty")


def _attach_image(
    page, image: bytes, filename: str, content_type: str, timeout_ms: int
) -> None:
    """Attach *image* bytes to the composer via its (hidden) file input."""
    scope = _modal(page) or page
    inputs = scope.locator(FILE_INPUT_SELECTOR)
    if inputs.count() == 0:
        # The file input only appears after the media button is clicked.
        for name in _IMAGE_ATTACH_BUTTONS:
            button = scope.get_by_role("button", name=name)
            try:
                if button.count() > 0:
                    button.first.click(timeout=timeout_ms)
                    break
            except Exception:
                continue
        page.wait_for_timeout(1000)
        inputs = scope.locator(FILE_INPUT_SELECTOR)
    if inputs.count() == 0:
        raise PublishError("could not find the composer's image file input")
    payload = {"name": filename or "image", "mimeType": content_type, "buffer": image}
    try:
        inputs.first.set_input_files(payload, timeout=timeout_ms)
    except Exception as exc:
        raise PublishError(f"could not attach the image: {exc}") from None
    page.wait_for_timeout(4000)  # let the upload finish before submitting


def _advance_media(page, cfg, timeout_ms: int) -> None:
    """Complete LinkedIn's post-image editing step (its primary action is "Next").

    After a photo is attached the composer can show an editing step; the "Post"
    button only appears once it is completed. A Next-style control is clicked
    **only** when the submit button is absent and such a control is present, so a
    composer that already exposes "Post" (text posts, or a flow without the step)
    is left untouched.
    """
    deadline = time.monotonic() + 15.0
    while _submit_button(page, cfg) is None and time.monotonic() < deadline:
        scope = _modal(page) or page
        clicked = False
        for name in _MEDIA_NEXT_BUTTONS:
            for exact in (True, False):
                control = scope.get_by_role("button", name=name, exact=exact)
                try:
                    if control.count() > 0 and control.first.is_enabled():
                        control.first.click(timeout=timeout_ms)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            return
        _log(f"completed the composer's media step ({name!r})")
        page.wait_for_timeout(1500)


def _submit(page, cfg, timeout_ms: int) -> bool:
    """Click the composer's Post button; True once the composer closes (success)."""
    had_modal = _modal(page) is not None
    name = str(cfg.post.submit_button_text)
    button = _submit_button(page, cfg)
    if button is None:
        scope = _modal(page) or page
        seen = ", ".join(_button_labels(scope)) or "none"
        raise PublishError(f"no {name!r} button in the composer (buttons: {seen})")
    try:
        button.click(timeout=timeout_ms)
    except Exception as exc:
        raise PublishError(f"could not click {name!r}: {exc}") from None

    if not had_modal:
        # A full-page composer has no modal to watch for closing; give it time and
        # assume it posted (the caller re-checks the resulting page).
        page.wait_for_timeout(6000)
        if monitor.is_checkpoint_url(page.url):
            raise PublishError("checkpoint detected while publishing")
        return True

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _modal(page) is None:
            return True
        if monitor.is_checkpoint_url(page.url):
            raise PublishError("checkpoint detected while publishing")
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            body = ""
        if monitor.is_invitation_limit_text(body):
            raise PublishError("LinkedIn refused the post (limit notice)")
        page.wait_for_timeout(500)
    return False


def _probe(page, cfg, opened: bool) -> str:
    """Dry-run report: what the composer exposes once opened, so selectors can be tuned."""
    trigger = str(cfg.post.composer_button_text)
    name = str(cfg.post.submit_button_text)
    modal = _modal(page)
    scope = modal or page
    submit = _submit_button(page, cfg)
    names = _button_labels(scope)
    return "\n".join(
        [
            f"opened {page.url}",
            f"composer trigger {trigger!r} clicked: {opened}",
            f"composer dialog open: {modal is not None}",
            f"submit {name!r} button present: {submit is not None}",
            f"file input present: {page.locator(FILE_INPUT_SELECTOR).count() > 0}",
            "composer buttons: " + ", ".join(names[:20]),
        ]
    )


def _first_post_link(page) -> str:
    """The newest post's permalink/urn on the Page's published-posts list, or "".

    LinkedIn lists the newest post first. Its link holds the activity URN
    (``/feed/update/urn:li:activity:<id>/``) or a ``/posts/<slug>-activity-<id>``
    permalink; a ``data-urn`` attribute is accepted too.
    """
    try:
        candidates = page.evaluate(
            "() => ["
            "  ...document.querySelectorAll('a[href]')].map(e => e.href).concat("
            "  [...document.querySelectorAll('[data-urn]')].map(e => e.getAttribute('data-urn')))"
        )
    except Exception:
        return ""
    for value in candidates or []:
        if value and (ACTIVITY_RE.search(value) or POSTS_PERMALINK_RE.search(value)):
            return value
    return ""


def _activity_urn(link: str) -> str:
    """``urn:li:activity:<id>`` from a post permalink, or "" if it has none."""
    match = ACTIVITY_RE.search(link) or POSTS_PERMALINK_RE.search(link)
    return f"urn:li:activity:{match.group(1)}" if match else ""


def _latest_post(page, cfg) -> tuple[str, str]:
    """Best-effort ``(post_urn, permalink)`` for the post just published.

    LinkedIn's composer does not surface the created post's id, so the Page's
    published-posts admin list is re-read for the newest link. Returns empty
    strings when it cannot be found (the post itself still succeeded).
    """
    try:
        page.goto(str(cfg.post.page_url), wait_until="domcontentloaded")
    except Exception:
        return "", ""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        link = _first_post_link(page)
        if link:
            return _activity_urn(link), link
        page.wait_for_timeout(500)
    return "", ""


def publish_to_page(
    cfg,
    *,
    text: str,
    image: bytes | None = None,
    image_filename: str | None = None,
    image_content_type: str = IMAGE_CONTENT_TYPE_DEFAULT,
    dry_run: bool = False,
    headed: bool = False,
) -> dict:
    """Publish *text* (+ optional *image*) as the configured Page.

    Returns ``{"ok": True, "as": "page", "author": "urn:li:organization:<id>"}``,
    plus ``post_urn``/``permalink`` of the published post when recoverable.
    Raises :class:`PublishError` on any failure (including an invalid session).
    With ``dry_run`` it opens the composer page and reports what it finds,
    posting nothing.
    """
    org = page_urn(cfg.post.page_url)
    # A dry-run only probes the composer page, so an empty body is fine.
    text = str(text or "").strip() if dry_run else validate(text, image)
    if monitor.kill_switch_present(cfg.paths.kill_file):
        raise PublishError("data/STOP present - refusing to post")

    from playwright.sync_api import sync_playwright

    timeout_ms = int(cfg.browser.timeout_ms)
    _log(f"{'probing' if dry_run else 'posting as ' + org}: {cfg.post.page_url}")

    with sync_playwright() as playwright:
        try:
            browser, _context, page = session.open_logged_in_page(playwright, cfg, headed=headed)
        except session.SessionInvalidError as exc:
            raise PublishError(f"{exc} - run `linkedin-bot login`") from None
        try:
            if dry_run:
                opened = _open_composer(page, cfg, timeout_ms)
                return {
                    "ok": True,
                    "as": "page",
                    "author": org,
                    "dry_run": True,
                    "detail": _probe(page, cfg, opened),
                }

            if not _open_composer(page, cfg, timeout_ms):
                raise PublishError(
                    "could not open the Page composer "
                    "(check post.page_url / post.composer_button_text)"
                )
            # Attach the image first: choosing a photo can open a media step that
            # replaces the composer, so a caption typed before it would be lost
            # (live: the post went out image-only). The caption goes into the
            # final composer, after that step is cleared.
            if image is not None:
                _attach_image(page, image, image_filename or "image", image_content_type, timeout_ms)
                _advance_media(page, cfg, timeout_ms)
            if text:
                _fill_editor(page, text, timeout_ms)
            if not _submit(page, cfg, timeout_ms):
                raise PublishError("clicked Post but could not confirm it published")
            post_urn, permalink = _latest_post(page, cfg)
            _log(f"published as {org}" + (f" ({permalink})" if permalink else ""))
            return {
                "ok": True,
                "as": "page",
                "author": org,
                "post_urn": post_urn,
                "permalink": permalink,
            }
        finally:
            browser.close()
