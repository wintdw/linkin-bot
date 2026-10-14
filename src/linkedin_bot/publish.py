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

An optional **comment** on the post just published can follow in the same
session (``comment=...``): LinkedIn has no Page-comment API here either, so the
comment is typed into the post's own inline comment box on that same admin list,
where the acting author is the Page. The box is scoped to the **card of the post
we just published** (located from the ``post_urn`` the read-back recovered), and a
comment box is recognised structurally (LinkedIn's ``...comment...`` markup, so
localized Pages still match) - a box that is not recognisably a comment box is
skipped rather than guessed at. A comment that cannot be posted is reported in
the result instead of failing the request - the post itself already went out, and
a retry would double-post it.

Every publish ends in one of three **statuses**, reported in the result and (on a
failure) in the HTTP body: ``confirmed`` (the composer closed, so the post is
out), ``refused`` (positively nothing was sent - e.g. the composer still holds the
caption - so a retry is safe), or ``unknown`` (the Post click was dispatched but
nothing confirmed it, so the post may be out: **never retry** an ``unknown``).
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
MAX_COMMENT = 1250  # LinkedIn comment limit
IMAGE_CONTENT_TYPE_DEFAULT = "image/jpeg"
_IMAGE_ATTACH_BUTTONS = ("Add a photo", "Add media", "Photo", "Media")
# After a photo is chosen LinkedIn shows an editing step whose primary action is
# "Next"; the "Post" submit only appears once that step is completed.
_MEDIA_NEXT_BUTTONS = ("Next", "Next step", "Continue")
# Comment boxes are Quill editors too, so the composer's selector matches them.
# They are recognised by LinkedIn's comment *markup* (a "...comment..." ancestor
# class, which survives localization) or by a "comment" placeholder/aria-label
# hint - never by position, so a box that is not recognisably a comment box is
# skipped instead of typed into.
COMMENT_EDITOR_SELECTOR = "div.ql-editor[contenteditable='true']"
_COMMENT_HINT = "comment"
# The card wrapping one post and its comment box, so the comment *submit* can be
# scoped to it: LinkedIn's post cards also carry a card-level "Comment" *action*
# button (same label) that only opens the box, and clicking that is not a submit.
_CARD_SELECTORS = ("div.feed-shared-update-v2", "article", "[data-urn]")
# A submitted comment empties its box; this bounds the wait for that.
COMMENT_CONFIRM_SECONDS = 8.0
# Outcome vocabulary reported to the caller (/post bodies, CLI, exceptions). Only
# a *refused* post is safe to retry: ``confirmed`` is already out, and ``unknown``
# may be out, so retrying either can duplicate it.
STATUS_CONFIRMED = "confirmed"
STATUS_REFUSED = "refused"
STATUS_UNKNOWN = "unknown"
# A comment box is a Quill editor inside LinkedIn's comment markup. Matching the
# *class* keeps the lookup working on localized Pages (Vietnamese), where the
# placeholder is not the English "comment" hint.
COMMENT_CONTAINER_SELECTOR = "[class*='comment']"


class PublishError(RuntimeError):
    """A Page post could not be published (composer not found, submit failed, ...).

    Nothing was sent, so the request is safe to retry (``status = "refused"``).
    """

    status = STATUS_REFUSED


class PublishUnconfirmed(PublishError):
    """The Post click was dispatched but nothing confirmed it published.

    The post may or may not have gone out, so the caller must **not** retry: a
    retry can duplicate it. Reported with ``status = "unknown"``.
    """

    status = STATUS_UNKNOWN


def is_retryable(status: str) -> bool:
    """Whether a request that ended with *status* may be retried without duplicating."""
    return status == STATUS_REFUSED


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


def _editor_text(editor, timeout_ms: int) -> str:
    """What *editor* currently holds, or "" when it cannot be read (detached)."""
    try:
        return (editor.inner_text(timeout=timeout_ms) or "").strip()
    except Exception:
        return ""


def _fill_editor(page, text: str, timeout_ms: int, editor=None) -> None:
    """Type *text* into a Quill editor (the composer's, or the comment box's).

    *editor* selects a specific box (comments); by default the composer's own
    editor in the open dialog is used. The value is read back afterwards: Quill
    can keep ``fill()``'s DOM change out of its own model (the post then submits
    empty), so real key events are used when the editor is still empty.
    """
    if editor is None:
        scope = _modal(page) or page
        editor = scope.locator(EDITOR_SELECTOR).first

    want = text.strip()
    try:
        editor.click(timeout=timeout_ms)
        editor.fill(text, timeout=timeout_ms)
    except Exception:
        pass
    if want and not _editor_text(editor, timeout_ms):
        try:
            editor.click(timeout=timeout_ms)
            page.keyboard.type(text)
        except Exception as exc:
            raise PublishError(f"could not type into the editor: {exc}") from None
    if want and not _editor_text(editor, timeout_ms):
        raise PublishError("typed the text but the editor stayed empty")


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


def _composer_holds_text(page) -> bool:
    """Whether the still-open composer's editor has content (a refused submit leaves it)."""
    modal = _modal(page)
    if modal is None:
        return False
    editors = modal.locator(EDITOR_SELECTOR)
    try:
        if editors.count() == 0:
            return False
    except Exception:
        return False
    return bool(_editor_text(editors.first, 2000))


def _submit(page, cfg, timeout_ms: int) -> str:
    """Click the composer's Post button; report how the publish ended.

    Returns :data:`STATUS_CONFIRMED` once the composer dialog closes (the
    live-verified success signal), :data:`STATUS_REFUSED` when the composer is
    still open holding the caption (the click never landed, so nothing was sent
    and a retry is safe) and :data:`STATUS_UNKNOWN` otherwise (the click was
    dispatched but nothing confirmed it, so the post may be out).
    """
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
        # A full-page composer has no modal to watch for closing, and the
        # live-verified shape is the dialog: give it time, then report the outcome
        # as unconfirmed rather than claiming a post we cannot see.
        page.wait_for_timeout(6000)
        if monitor.is_checkpoint_url(page.url):
            raise PublishError("checkpoint detected while publishing")
        return STATUS_UNKNOWN

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _modal(page) is None:
            return STATUS_CONFIRMED
        if monitor.is_checkpoint_url(page.url):
            raise PublishError("checkpoint detected while publishing")
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            body = ""
        if monitor.is_invitation_limit_text(body):
            raise PublishError("LinkedIn refused the post (limit notice)")
        page.wait_for_timeout(500)
    # Still open: it holds the caption only if the click never landed. An empty
    # editor (or an image-only post) tells us nothing, so that is not a refusal.
    return STATUS_REFUSED if _composer_holds_text(page) else STATUS_UNKNOWN


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


def _comment_probe(page, cfg) -> list[str]:
    """Dry-run report on the posts list's comment boxes (the comment path's selectors).

    Called while the composer is still closed - once it is open its editor is the
    one ``_comment_editors`` would see. Reports how many of the list's editors are
    recognised as comment boxes, so a tuning session can tell a wrong selector from
    a missing one.
    """
    editors = _comment_editors(page)
    try:
        total = page.locator(COMMENT_EDITOR_SELECTOR).count()
    except Exception:
        total = 0
    lines = [f"comment boxes on the posts list: {len(editors)} of {total} editor(s)"]
    if not editors:
        return lines
    editor = editors[0]
    scope = _comment_scope(page, editor)
    submit = _comment_submit(scope, cfg)
    lines += [
        f"comment submit {str(cfg.post.comment_button_text)!r} in the newest card: "
        f"{submit is not None}",
        "newest card buttons: " + ", ".join(_button_labels(scope)[:20]),
    ]
    return lines


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


def _post_card(page, post_urn: str):
    """The card of the post with *post_urn* on the posts admin, or ``None``.

    The admin list holds **every** post's card, so a page-wide comment-box search
    can land on the wrong post. The card is found from the post's own activity id
    (the same ids :func:`_activity_urn` accepts) and the comment box is then scoped
    to it. ``None`` means "not identifiable" - never "use any card".
    """
    match = ACTIVITY_RE.search(str(post_urn or "")) or POSTS_PERMALINK_RE.search(
        str(post_urn or "")
    )
    if not match:
        return None
    script = """([id, selectors]) => {
      const re = /urn:li:activity:([0-9]+)|activity-([0-9]+)/;
      for (const el of document.querySelectorAll('a[href], [data-urn]')) {
        const value = el.getAttribute('href') || el.getAttribute('data-urn') || '';
        const found = value.match(re);
        if (found && (found[1] || found[2]) === id) {
          return el.closest(selectors) || el;
        }
      }
      return null;
    }"""
    try:
        handle = page.evaluate_handle(script, [match.group(1), ", ".join(_CARD_SELECTORS)])
        return handle.as_element()
    except Exception:
        return None


def _looks_like_comment_box(editor) -> bool:
    """Whether *editor* is a comment box, by its comment markup or its placeholder.

    A "comment" placeholder/aria-label identifies one outright; the localized
    (Vietnamese) Pages this runs against are covered by LinkedIn's
    ``...comment...`` ancestor class instead, which does not depend on the language.
    """
    try:
        hint = (
            editor.get_attribute("data-placeholder")
            or editor.get_attribute("aria-label")
            or ""
        )
    except Exception:
        hint = ""
    if _COMMENT_HINT in hint.lower():
        return True
    try:
        container = editor.evaluate_handle(
            f"el => el.closest({COMMENT_CONTAINER_SELECTOR!r})"
        ).as_element()
    except Exception:
        container = None
    return container is not None


def _comment_editors(page, card=None) -> list:
    """The comment boxes to use on the posts list, or ``[]``.

    With *card* only that post's box is considered; without it the whole list is
    searched (when the new post's card could not be identified). Only boxes that
    are recognisably comment boxes are returned: the old "no placeholder, take
    every editor" fallback is gone, because typing a comment into the wrong box (a
    composer, a search field) is worse than reporting the comment as failed.
    """
    scope = card if card is not None else page
    editors = scope.locator(COMMENT_EDITOR_SELECTOR)
    try:
        count = editors.count()
    except Exception:
        return []
    boxes = []
    for index in range(count):
        editor = editors.nth(index)
        if _looks_like_comment_box(editor):
            boxes.append(editor)
    if count and not boxes:
        _log(f"no comment box among {count} editor(s) - not guessing (see `post --dry-run`)")
    return boxes


def _comment_scope(page, editor):
    """The post card wrapping *editor*, so the comment submit is scoped to it.

    The page-level "Comment" control is the card's *action* button (it only opens
    the box), so a submit looked up there could click the wrong thing; the card
    is found by walking up from the editor itself, falling back to the page.
    """
    for selector in _CARD_SELECTORS:
        try:
            element = editor.evaluate_handle(f"el => el.closest({selector!r})").as_element()
        except Exception:
            element = None
        if element is not None:
            return element
    return page


def _comment_submit(scope, cfg):
    """An enabled Comment button inside *scope*, or ``None``."""
    name = str(cfg.post.comment_button_text)
    for exact in (True, False):
        buttons = scope.get_by_role("button", name=name, exact=exact)
        try:
            count = buttons.count()
        except Exception:
            return None
        for index in range(count):
            candidate = buttons.nth(index)
            try:
                if candidate.is_enabled():
                    return candidate
            except Exception:
                continue
    return None


def _submit_comment(page, editor, cfg, timeout_ms: int) -> bool:
    """Submit the comment in *editor*; True once its box empties.

    Enter submits a Quill comment box (Shift+Enter is a line break) and, unlike
    the card's Comment button, cannot be confused with the action button of the
    same label - so it is tried first, with that button as the fallback. The box
    clearing is the success signal: a refused submit leaves the text in place.
    """
    try:
        editor.click(timeout=timeout_ms)
        page.keyboard.press("Enter")
    except Exception as exc:
        raise PublishError(f"could not submit the comment: {exc}") from None

    def _cleared() -> bool:
        deadline = time.monotonic() + COMMENT_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            if not _editor_text(editor, 1000):
                return True
            page.wait_for_timeout(300)
        return False

    if _cleared():
        return True
    button = _comment_submit(_comment_scope(page, editor), cfg)
    if button is None:
        return False
    try:
        button.click(timeout=timeout_ms)
    except Exception as exc:
        raise PublishError(f"could not click the comment button: {exc}") from None
    return _cleared()


def _comment_on_newest_post(page, cfg, text: str, timeout_ms: int, post_urn: str = "") -> str:
    """Comment *text* on the post we just published, as the Page; returns the text.

    Runs on the Page's posts admin (``post.page_url``), where the acting author is
    the Page itself. The box is scoped to the card of *post_urn* - the post just
    published; when that cannot be identified the list is searched instead, but
    still only for boxes that are recognisably comment boxes. Raises
    :class:`PublishError` when there is no such box or the submit cannot be
    confirmed, so the caller reports the comment as failed rather than commenting
    somewhere else.
    """
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) > MAX_COMMENT:
        raise PublishError(f"comment is {len(text)} chars (LinkedIn max {MAX_COMMENT})")
    if str(cfg.post.page_url) not in str(getattr(page, "url", "")):
        page.goto(str(cfg.post.page_url), wait_until="domcontentloaded")
        scheduler.sleep_post_scroll(cfg)
    card = _post_card(page, post_urn)
    if card is None:
        _log("could not identify the new post's card; searching the list for a comment box")
    editors = _comment_editors(page, card)
    if not editors:
        where = "the new post's card" if card is not None else "the Page posts list"
        raise PublishError(
            f"no comment box in {where} (check post.page_url, or that the list has rendered)"
        )
    editor = editors[0]
    _fill_editor(page, text, timeout_ms, editor=editor)
    if not _submit_comment(page, editor, cfg, timeout_ms):
        scope = _comment_scope(page, editor)
        seen = ", ".join(_button_labels(scope)) or "none"
        raise PublishError(f"typed the comment but could not submit it (buttons: {seen})")
    _log(f"commented as the page: {text[:60]!r}")
    return text


def publish_to_page(
    cfg,
    *,
    text: str,
    image: bytes | None = None,
    image_filename: str | None = None,
    image_content_type: str = IMAGE_CONTENT_TYPE_DEFAULT,
    comment: str | None = None,
    dry_run: bool = False,
    headed: bool = False,
) -> dict:
    """Publish *text* (+ optional *image*) as the configured Page.

    Returns ``{"ok": True, "as": "page", "author": "urn:li:organization:<id>",
    "status": "confirmed", "retryable": False}``, plus ``post_urn``/``permalink``
    of the published post when recoverable. With *comment* the post is then
    commented on **as the Page** in the same session, and
    ``commented``/``comment_error`` report that part; a failed comment does
    **not** fail the call (the post is already out - a retry would double-post),
    it is logged and reported so the caller can decide.

    Raises :class:`PublishError` (``status = "refused"``) when nothing was sent -
    safe to retry - and :class:`PublishUnconfirmed` (``status = "unknown"``) when
    the post may have gone out, which the caller must **not** retry. With
    ``dry_run`` it opens the composer page and reports what it finds, posting
    nothing.
    """
    org = page_urn(cfg.post.page_url)
    # A dry-run only probes the composer page, so an empty body is fine.
    text = str(text or "").strip() if dry_run else validate(text, image)
    if comment and len(str(comment).strip()) > MAX_COMMENT:
        length = len(str(comment).strip())
        raise PublishError(f"comment is {length} chars (LinkedIn max {MAX_COMMENT})")
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
                # The comment boxes are probed on the posts list *before* the
                # composer opens: once it is up, its editor is the one the comment
                # lookup sees, and the boxes would be invisible behind the dialog.
                try:
                    page.goto(str(cfg.post.page_url), wait_until="domcontentloaded")
                    scheduler.sleep_post_scroll(cfg)
                    comment_lines = _comment_probe(page, cfg)
                except Exception as exc:  # noqa: BLE001 - a probe reports, never fails
                    comment_lines = [f"comment probe failed: {exc}"]
                opened = _open_composer(page, cfg, timeout_ms)
                return {
                    "ok": True,
                    "as": "page",
                    "author": org,
                    "dry_run": True,
                    "detail": "\n".join([*comment_lines, _probe(page, cfg, opened)]),
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
            status = _submit(page, cfg, timeout_ms)
            if status == STATUS_REFUSED:
                # The composer still holds the caption: the click never landed, so
                # nothing was sent and the caller may retry safely.
                raise PublishError(
                    "clicked Post but the composer still holds the text - nothing was sent"
                )
            if status != STATUS_CONFIRMED:
                raise PublishUnconfirmed(
                    "clicked Post but could not confirm it published - the post may have "
                    "gone out; do NOT retry, check the Page's posts list"
                )
            post_urn, permalink = _latest_post(page, cfg)
            _log(f"published as {org}" + (f" ({permalink})" if permalink else ""))
            result = {
                "ok": True,
                "as": "page",
                "author": org,
                "status": STATUS_CONFIRMED,
                "retryable": False,  # the post is out: a retry would duplicate it
                "post_urn": post_urn,
                "permalink": permalink,
            }
            if comment:
                # The post is out: a comment failure is reported, never raised, so
                # the caller does not retry /post and duplicate the post. Every
                # failure counts, not just PublishError - a Playwright timeout in
                # the comment path must not turn a published post into a 502.
                try:
                    _comment_on_newest_post(page, cfg, comment, timeout_ms, post_urn=post_urn)
                    result["commented"] = True
                    result["comment_error"] = None
                except Exception as exc:  # noqa: BLE001 - reported, never re-raised
                    detail = (
                        str(exc)
                        if isinstance(exc, PublishError)
                        else f"{type(exc).__name__}: {exc}"
                    )
                    _log(f"comment failed: {detail}")
                    result["commented"] = False
                    result["comment_error"] = detail
            return result
        finally:
            browser.close()
