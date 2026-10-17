from types import SimpleNamespace

import pytest

from linkedin_bot import publish


class _Loc:
    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    def all(self):
        return list(self._items)

    def nth(self, index):
        return self._items[index]

    @property
    def first(self):
        return self._items[0]


class _Scope:
    """Fake locator scope matching by button label (exact equality / substring)."""

    def __init__(self, labels):
        self._labels = labels

    def get_by_role(self, role, name=None, exact=False):
        if name is None:
            return _Loc(list(self._labels))
        if exact:
            matched = [label for label in self._labels if label == name]
        else:
            matched = [label for label in self._labels if name.lower() in label.lower()]
        return _Loc(matched)


def _cfg(text="Post"):
    return SimpleNamespace(post=SimpleNamespace(submit_button_text=text))


def test_page_urn_from_company_url():
    urn = publish.page_urn("https://www.linkedin.com/company/125004230/admin/dashboard/")
    assert urn == "urn:li:organization:125004230"


def test_page_urn_rejects_url_without_id():
    with pytest.raises(publish.PublishError):
        publish.page_urn("https://www.linkedin.com/feed/")


def test_validate_trims_text():
    assert publish.validate("  hi  ", None) == "hi"


def test_validate_accepts_image_only():
    assert publish.validate("", b"x") == ""


def test_validate_rejects_empty_without_image():
    with pytest.raises(publish.PublishError):
        publish.validate("   ", None)


def test_validate_rejects_oversized_text():
    with pytest.raises(publish.PublishError):
        publish.validate("x" * (publish.MAX_TEXT + 1), None)


def test_submit_button_prefers_dialog_then_falls_back_to_page(monkeypatch):
    # Live failure: the submit lived outside the dialog _modal() scoped to, so an
    # exact match inside the dialog found nothing. It must fall back to the page.
    modal = _Scope(["Cancel"])
    page = _Scope(["Cancel", "Post"])
    monkeypatch.setattr(publish, "_modal", lambda _page: modal)
    assert publish._submit_button(page, _cfg()) == "Post"


def test_submit_button_matches_a_substring_label(monkeypatch):
    modal = _Scope(["Post now"])
    monkeypatch.setattr(publish, "_modal", lambda _page: modal)
    assert publish._submit_button(modal, _cfg()) == "Post now"


def test_submit_button_none_when_absent(monkeypatch):
    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    assert publish._submit_button(_Scope(["Cancel", "Back"]), _cfg()) is None


def test_button_labels_dedupes_and_trims():
    class Btn:
        def __init__(self, label):
            self._label = label

        def get_attribute(self, name):
            return None

        def inner_text(self):
            return self._label

    class Scope:
        def get_by_role(self, role, name=None, exact=False):
            return _Loc([Btn("Post"), Btn("  Post  "), Btn("Cancel")])

    assert publish._button_labels(Scope()) == ["Post", "Cancel"]


def test_advance_media_clicks_next_only_until_submit_appears(monkeypatch):
    clicked: list[str] = []

    class Btn:
        def __init__(self, label, scope):
            self._label = label
            self._scope = scope

        def is_enabled(self):
            return True

        def click(self, timeout=None):
            clicked.append(self._label)
            self._scope.submitted = True

    class Scope:
        def __init__(self):
            self.submitted = False

        def get_by_role(self, role, name=None, exact=False):
            if name == "Post":
                return _Loc([Btn("Post", self)] if self.submitted else [])
            if name == "Next":
                return _Loc([Btn("Next", self)])
            return _Loc([])

        def wait_for_timeout(self, ms):
            pass

    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    publish._advance_media(Scope(), _cfg(), 1000)
    assert clicked == ["Next"]


def test_advance_media_noop_when_submit_already_present(monkeypatch):
    clicked: list[str] = []

    class Btn:
        def is_enabled(self):
            return True

        def click(self, timeout=None):
            clicked.append("Next")

    class Scope:
        def get_by_role(self, role, name=None, exact=False):
            if name == "Post":
                return _Loc([Btn()])
            if name == "Next":
                return _Loc([Btn()])
            return _Loc([])

    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    publish._advance_media(Scope(), _cfg(), 1000)
    assert clicked == []


class _ClickButton:
    def click(self, timeout=None):
        pass


class _DispatchButton:
    """A button whose click is dispatched on the pinned node (the overlay-safe path)."""

    def __init__(self, enabled=True, visible=True):
        self._enabled = enabled
        self._visible = visible
        self.dispatched = 0
        self.clicked = 0

    def is_enabled(self):
        return self._enabled

    def is_visible(self):
        return self._visible

    def element_handle(self, timeout=None):
        if not self._enabled or not self._visible:
            raise RuntimeError("not actionable")
        return self

    def evaluate(self, script):
        self.dispatched += 1

    def click(self, timeout=None):
        self.clicked += 1


def test_click_dispatches_on_the_node_when_it_is_actionable():
    button = _DispatchButton()
    publish._click(button, 1000)
    assert button.dispatched == 1
    assert button.clicked == 0  # no physical click to swallow


def test_click_falls_back_to_a_real_click_when_not_actionable():
    # A disabled/overlaid control must not be "clicked" through the DOM: the normal
    # click waits for actionability and fails loudly instead.
    button = _DispatchButton(enabled=False)
    publish._click(button, 1000)
    assert button.dispatched == 0
    assert button.clicked == 1


class _Txt:
    def __init__(self, text, connected=True):
        self._text = text
        self._connected = connected

    def inner_text(self, timeout=None):
        return self._text

    def evaluate(self, script):
        return {"connected": self._connected, "text": self._text}


class _ModalScope:
    def __init__(self, text, connected=True):
        self._txt = _Txt(text, connected)

    def locator(self, selector):
        return _Loc([self._txt])


class _StubPage:
    url = "https://www.linkedin.com/company/1/admin/page-posts/published/"

    def wait_for_timeout(self, ms):
        pass


def _submit_cfg():
    return SimpleNamespace(post=SimpleNamespace(submit_button_text="Post"))


def test_submit_confirms_when_the_composer_dialog_closes(monkeypatch):
    seen = {"calls": 0}

    def _modal(_page):
        seen["calls"] += 1
        return object() if seen["calls"] == 1 else None

    monkeypatch.setattr(publish, "_modal", _modal)
    monkeypatch.setattr(publish, "_submit_button", lambda *a, **k: _ClickButton())
    assert publish._submit(_StubPage(), _submit_cfg(), 1000) == publish.STATUS_CONFIRMED


def test_submit_refuses_when_the_composer_still_holds_the_caption(monkeypatch):
    # The click never landed: the caption is still in the composer, so nothing was
    # sent and the caller may retry safely.
    monkeypatch.setattr(publish, "_modal", lambda _page: object())
    monkeypatch.setattr(publish, "_submit_button", lambda *a, **k: _ClickButton())
    monkeypatch.setattr(publish, "_composer_holds_text", lambda _page: True)
    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(publish.time, "monotonic", lambda: next(ticks))
    assert publish._submit(_StubPage(), _submit_cfg(), 1000) == publish.STATUS_REFUSED


def test_submit_reports_unknown_when_an_open_composer_is_empty(monkeypatch):
    # An empty composer tells us nothing (an image-only post, or a post that went
    # out with the dialog stuck), so it is *not* reported as a refusal: the caller
    # must not retry.
    monkeypatch.setattr(publish, "_modal", lambda _page: object())
    monkeypatch.setattr(publish, "_submit_button", lambda *a, **k: _ClickButton())
    monkeypatch.setattr(publish, "_composer_holds_text", lambda _page: False)
    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(publish.time, "monotonic", lambda: next(ticks))
    assert publish._submit(_StubPage(), _submit_cfg(), 1000) == publish.STATUS_UNKNOWN


def test_submit_reports_unknown_for_a_full_page_composer(monkeypatch):
    # No dialog to watch closing, and the live-verified shape *is* the dialog: the
    # outcome is unconfirmed rather than claimed.
    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    monkeypatch.setattr(publish, "_submit_button", lambda *a, **k: _ClickButton())
    assert publish._submit(_StubPage(), _submit_cfg(), 1000) == publish.STATUS_UNKNOWN


def test_composer_holds_text_reads_the_composer_editor(monkeypatch):
    monkeypatch.setattr(publish, "_modal", lambda _page: _ModalScope("a caption"))
    assert publish._composer_holds_text(_StubPage()) is True
    monkeypatch.setattr(publish, "_modal", lambda _page: _ModalScope(""))
    assert publish._composer_holds_text(_StubPage()) is False


def test_composer_holds_text_false_without_a_composer(monkeypatch):
    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    assert publish._composer_holds_text(_StubPage()) is False


def test_composer_holds_text_ignores_a_detached_editor(monkeypatch):
    # "Refused" tells the caller a retry is safe, so it must not come from a box that
    # cannot be read (a detached handle hands back stale text - AGENTS.md).
    modal = _ModalScope("still our caption", connected=False)
    monkeypatch.setattr(publish, "_modal", lambda _page: modal)
    assert publish._composer_holds_text(_StubPage()) is False


def test_advance_media_keeps_waiting_for_a_slow_upload(monkeypatch):
    # While the image is still uploading the composer shows neither Post nor Next.
    # The old code returned at once, so the caption/submit raced the upload; now it
    # keeps polling until a control appears.
    class Btn:
        def is_enabled(self):
            return True

        def click(self, timeout=None):
            pass

    class Scope:
        def __init__(self):
            self.post_lookups = 0

        def get_by_role(self, role, name=None, exact=False):
            if name == "Post":
                self.post_lookups += 1
                return _Loc([Btn()] if self.post_lookups >= 3 else [])
            return _Loc([])

        def wait_for_timeout(self, ms):
            pass

    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    scope = Scope()
    publish._advance_media(scope, _cfg(), 1000)
    assert scope.post_lookups >= 3  # polled rather than giving up on the first look


def test_advance_media_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(publish, "MEDIA_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(publish, "_modal", lambda _page: None)
    monkeypatch.setattr(publish, "_submit_button", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_click_media_next", lambda *a, **k: False)
    publish._advance_media(_StubPage(), _cfg(), 1000)  # returns instead of hanging


class _TypingKeyboard:
    """Keyboard that records what was typed/pressed (Enter would submit a comment)."""

    def __init__(self, editor):
        self._editor = editor
        self.presses: list[str] = []
        self.typed: list[str] = []

    def press(self, key):
        self.presses.append(key)
        self._editor.text += "\n"

    def type(self, text):
        self.typed.append(text)
        self._editor.text += text


def test_type_text_uses_shift_enter_for_line_breaks():
    # A bare Enter submits a Quill *comment* box, so a multi-line comment typed with
    # real key events must break lines with Shift+Enter.
    editor = _Editor(fill_sticks=False)
    page = SimpleNamespace(keyboard=_TypingKeyboard(editor))
    publish._type_text(page, "first line\nsecond line")
    assert page.keyboard.presses == ["Shift+Enter"]
    assert page.keyboard.typed == ["first line", "second line"]
    assert editor.text == "first line\nsecond line"


def test_fill_editor_types_multiline_without_a_bare_enter(monkeypatch):
    editor = _Editor(fill_sticks=False)
    page = _Page(editor, type_sticks=False)
    page.keyboard = _TypingKeyboard(editor)
    monkeypatch.setattr(publish, "_modal", lambda _page: page)
    publish._fill_editor(page, "one\ntwo", 1000)
    assert "Enter" not in page.keyboard.presses
    assert editor.text == "one\ntwo"


class _Editor:
    """Fake Quill editor: ``fill`` only sticks when *fill_sticks* is set."""

    def __init__(self, fill_sticks):
        self.text = ""
        self.fill_sticks = fill_sticks

    def click(self, timeout=None):
        pass

    def fill(self, text, timeout=None):
        if self.fill_sticks:
            self.text = text

    def inner_text(self, timeout=None):
        return self.text


class _Keyboard:
    def __init__(self, editor, sticks):
        self._editor = editor
        self._sticks = sticks

    def type(self, text):
        if self._sticks:
            self._editor.text = text


class _Page:
    def __init__(self, editor, type_sticks):
        self._editor = editor
        self.keyboard = _Keyboard(editor, type_sticks)

    def locator(self, selector):
        return _Loc([self._editor])


def _editor_page(fill_sticks, type_sticks):
    editor = _Editor(fill_sticks)
    return editor, _Page(editor, type_sticks)


def test_fill_editor_keeps_fill_when_it_sticks(monkeypatch):
    editor, page = _editor_page(fill_sticks=True, type_sticks=False)
    monkeypatch.setattr(publish, "_modal", lambda _page: page)
    publish._fill_editor(page, "hi caption", 1000)
    assert editor.text == "hi caption"


def test_fill_editor_types_when_quill_drops_fill(monkeypatch):
    # fill() sets the DOM but Quill keeps an empty model, so the post would go out
    # with no caption; the read-back catches it and real key events are used.
    editor, page = _editor_page(fill_sticks=False, type_sticks=True)
    monkeypatch.setattr(publish, "_modal", lambda _page: page)
    publish._fill_editor(page, "hello caption", 1000)
    assert editor.text == "hello caption"


def test_fill_editor_raises_when_caption_never_lands(monkeypatch):
    editor, page = _editor_page(fill_sticks=False, type_sticks=False)
    monkeypatch.setattr(publish, "_modal", lambda _page: page)
    with pytest.raises(publish.PublishError):
        publish._fill_editor(page, "hello caption", 1000)


class _EntriesPage:
    """Fake posts admin whose successive reads return canned ``_post_entries`` payloads.

    *reads* is a list of payloads (each a list of entry dicts); a single flat payload
    is accepted too and then returned for every read. The last payload repeats, so a
    test can say "the first read misses it, then it is there".
    """

    url = "https://www.linkedin.com/company/1/admin/page-posts/published/"

    def __init__(self, reads):
        if reads and isinstance(reads[0], dict):
            reads = [reads]
        self.reads = list(reads)
        self.calls = 0

    def goto(self, url, wait_until=None):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, arg=None):
        self.calls += 1
        if not self.reads:
            return []
        return self.reads[min(self.calls - 1, len(self.reads) - 1)]


def _entry(activity, text="", href=""):
    return {
        "id": activity,
        "href": href,
        "urn": f"urn:li:activity:{activity}",
        "text": text,
    }


POSTS_CFG = SimpleNamespace(
    post=SimpleNamespace(page_url="https://www.linkedin.com/company/1/admin/page-posts/published/")
)


def test_caption_key_ignores_whitespace_and_case():
    assert publish._caption_key("  Hello   World\nAnd more text here  ") == (
        "hello world and more text here"
    )


def test_caption_key_empty_when_too_short_to_match():
    # A caption this short could match any card, so it is not used as a key.
    assert publish._caption_key("hi") == ""


def test_caption_key_is_bounded_and_nfc_normalized():
    # Vietnamese diacritics are compared in NFC, and only the start of a long
    # caption is used (LinkedIn truncates the card with "see more").
    text = "Đọc bài gốc ở comment " + "x" * 200
    key = publish._caption_key(text)
    assert key.startswith("đọc bài gốc ở comment")
    assert len(key) == publish._CAPTION_KEY_CHARS


def test_entry_matches_normalizes_both_sides():
    entry = _entry("1", "Page name\n\n  Đọc   bài gốc ở comment\n\n2h •")
    assert publish._entry_matches(entry, publish._caption_key("đọc bài gốc ở comment"))
    assert not publish._entry_matches(entry, publish._caption_key("a different caption here"))


def test_post_entries_keeps_the_newest_first_and_drops_bad_rows():
    page = _EntriesPage(
        [
            _entry("22", "newest", href="https://www.linkedin.com/feed/update/urn:li:activity:22/"),
            {"id": "", "href": "", "urn": "", "text": "no id"},
            _entry("11", "older"),
        ]
    )
    entries = publish._post_entries(page)
    assert [entry["urn"] for entry in entries] == ["urn:li:activity:22", "urn:li:activity:11"]


def test_post_entries_empty_when_the_page_errors():
    class _Broken:
        def evaluate(self, script, arg=None):
            raise RuntimeError("no dom")

    assert publish._post_entries(_Broken()) == []


def test_latest_post_prefers_the_entry_matching_our_caption():
    # The list's first entry is not necessarily ours (a pinned/older post), so the
    # caption decides; matching also reports the read-back as verified.
    page = _EntriesPage(
        [
            _entry("22", "Some other Page post that is pinned"),
            _entry("11", "Đọc bài gốc ở comment — our brand new post"),
        ]
    )
    urn, permalink, verified = publish._latest_post(
        page, POSTS_CFG, caption="Đọc bài gốc ở comment"
    )
    assert urn == "urn:li:activity:11"
    assert permalink == "https://www.linkedin.com/feed/update/urn:li:activity:11/"
    assert verified is True


def test_latest_post_polls_until_our_post_appears(monkeypatch):
    # The list lags the post by a moment: the first read misses it, the second has it.
    page = _EntriesPage(
        [
            [_entry("22", "an older post")],
            [_entry("33", "Đọc bài gốc ở comment")],
        ]
    )
    urn, _permalink_value, verified = publish._latest_post(
        page, POSTS_CFG, caption="Đọc bài gốc ở comment"
    )
    assert page.calls == 2  # the first read missed it, the second found it
    assert urn == "urn:li:activity:33"
    assert verified is True


def test_latest_post_falls_back_to_the_newest_entry_unverified(monkeypatch):
    page = _EntriesPage(
        [
            _entry("22", "not our caption at all", href="https://x/feed/update/urn:li:activity:22/"),
        ]
    )
    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(publish.time, "monotonic", lambda: next(ticks))
    urn, permalink, verified = publish._latest_post(page, POSTS_CFG, caption="our caption here")
    assert urn == "urn:li:activity:22"
    assert permalink == "https://x/feed/update/urn:li:activity:22/"
    assert verified is False


def test_latest_post_skips_the_poll_when_there_is_no_caption():
    # An image-only post has nothing to match, so one read is enough.
    page = _EntriesPage([_entry("22", "")])
    urn, permalink, verified = publish._latest_post(page, POSTS_CFG, caption="")
    assert page.calls == 1
    assert urn == "urn:li:activity:22"
    assert permalink == "https://www.linkedin.com/feed/update/urn:li:activity:22/"
    assert verified is False


def test_latest_post_empty_when_nothing_can_be_read(monkeypatch):
    # With nothing on the list it keeps polling until the deadline (the list can
    # still be rendering), then reports nothing found.
    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(publish.time, "monotonic", lambda: next(ticks))
    assert publish._latest_post(_EntriesPage([]), POSTS_CFG, caption="x" * 30) == ("", "", False)


def test_permalink_falls_back_to_a_feed_url_from_the_urn():
    assert publish._permalink(_entry("9")) == (
        "https://www.linkedin.com/feed/update/urn:li:activity:9/"
    )
    assert publish._permalink(_entry("9", href="https://x/posts/a-activity-9-b")) == (
        "https://x/posts/a-activity-9-b"
    )


# --- comments on the post just published -------------------------------------


class _CommentEditor:
    """Fake Quill comment box: it holds what was typed until it is submitted."""

    def __init__(self, placeholder="Add a comment...", container=None, connected=True):
        self.placeholder = placeholder
        self.container = container  # truthy = sits in LinkedIn's comment markup
        self.connected = connected
        self.text = ""
        self.card = None
        self.clicks = 0

    def get_attribute(self, name):
        return self.placeholder if name == "data-placeholder" else None

    def click(self, timeout=None):
        self.clicks += 1

    def fill(self, text, timeout=None):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text

    def element_handle(self, timeout=None):
        return self

    def evaluate(self, script):
        return {"connected": self.connected, "text": self.text}

    def evaluate_handle(self, script):
        # `_looks_like_comment_box` looks for the comment markup; `_comment_scope`
        # walks up to the post card.
        return _Handle(self.container if "comment" in script else self.card)


class _Handle:
    def __init__(self, element):
        self._element = element

    def as_element(self):
        return self._element


class _Editors:
    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    def nth(self, index):
        return self._items[index]


class _PostsPage:
    """Fake Page posts admin: a list of comment boxes and Enter that submits."""

    url = "https://www.linkedin.com/company/1/admin/page-posts/published/"

    def __init__(self, editors):
        self._editors = editors
        self.keyboard = self

    def press(self, key):
        # Enter submits a Quill comment box, which empties it.
        if key == "Enter":
            for editor in self._editors:
                editor.text = ""

    def goto(self, url, wait_until=None):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        return _Editors(self._editors)


class _LabeledButton:
    def __init__(self, label, enabled=True):
        self.label = label
        self.enabled = enabled
        self.clicked = False

    def is_enabled(self):
        return self.enabled

    def click(self, timeout=None):
        self.clicked = True


class _LabeledScope:
    def __init__(self, buttons):
        self._buttons = buttons

    def get_by_role(self, role, name=None, exact=False):
        if name is None:
            return _Loc(self._buttons)
        if exact:
            return _Loc([b for b in self._buttons if b.label == name])
        return _Loc([b for b in self._buttons if name.lower() in b.label.lower()])


class _ClearingButton(_LabeledButton):
    """Fake Comment submit whose click empties the box it belongs to."""

    def __init__(self, label, editor):
        super().__init__(label)
        self._editor = editor

    def click(self, timeout=None):
        self.clicked = True
        self._editor.text = ""


def _post_cfg(**post):
    values = {"page_url": _PostsPage.url, "comment_button_text": "Comment"}
    values.update(post)
    return SimpleNamespace(
        post=SimpleNamespace(**values),
        browser=SimpleNamespace(timeout_ms=1000, headless=True, slow_mo_ms=0),
        paths=SimpleNamespace(kill_file="/nonexistent/STOP"),
    )


def test_comment_editors_prefers_boxes_that_say_comment():
    box = _CommentEditor()
    other = _CommentEditor(placeholder="What do you want to talk about?")
    page = _PostsPage([other, box])
    assert publish._comment_editors(page) == [box]


def test_comment_editors_accepts_localized_boxes_by_their_comment_markup():
    # The Pages are Vietnamese, so the placeholder is not the English hint; the
    # "...comment..." ancestor class identifies the box instead.
    box = _CommentEditor(placeholder="Viết bình luận...", container=object())
    other = _CommentEditor(placeholder="Bạn muốn nói về điều gì?")
    page = _PostsPage([other, box])
    assert publish._comment_editors(page) == [box]


def test_comment_editors_skips_boxes_that_are_not_comment_boxes():
    # The old "no placeholder, take every editor" fallback is gone: a box that is
    # not recognisably a comment box (a composer, a search field) is skipped, so
    # the comment is reported failed instead of typed into the wrong box.
    page = _PostsPage([_CommentEditor(placeholder=""), _CommentEditor(placeholder="")])
    assert publish._comment_editors(page) == []


def test_comment_editors_scopes_to_the_given_card():
    mine = _CommentEditor(placeholder="Bình luận...", container=object())
    theirs = _CommentEditor(placeholder="Bình luận...", container=object())

    class _Card:
        def locator(self, selector):
            return _Editors([mine])

    page = _PostsPage([theirs])
    assert publish._comment_editors(page, _Card()) == [mine]


class _CardPage:
    def __init__(self, element):
        self._element = element
        self.calls = []

    def evaluate_handle(self, script, arg=None):
        self.calls.append(arg)
        return _Handle(self._element)


def test_post_card_finds_the_card_by_activity_id():
    element = object()
    page = _CardPage(element)
    assert publish._post_card(page, "urn:li:activity:4242") is element
    assert page.calls == [["4242", "div.feed-shared-update-v2, article, [data-urn]"]]


def test_post_card_accepts_a_posts_permalink():
    element = object()
    page = _CardPage(element)
    assert publish._post_card(page, "https://x/posts/atento_x-activity-99-abc") is element


def test_post_card_none_without_an_activity_id():
    page = _CardPage(object())
    assert publish._post_card(page, "") is None
    assert publish._post_card(page, "https://www.linkedin.com/feed/") is None
    assert page.calls == []  # never queried without an id to look for


def test_comment_scope_walks_up_to_the_post_card():
    card = object()
    editor = _CommentEditor()
    editor.card = card
    assert publish._comment_scope(_PostsPage([]), editor) is card


def test_comment_scope_falls_back_to_the_page():
    editor = _CommentEditor()  # card is None -> no ancestor matched
    page = _PostsPage([])
    assert publish._comment_scope(page, editor) is page


def test_comment_submit_skips_a_disabled_button():
    disabled = _LabeledButton("Comment", enabled=False)
    enabled = _LabeledButton("Comment", enabled=True)
    scope = _LabeledScope([disabled, enabled])
    assert publish._comment_submit(scope, _post_cfg()) is enabled


def test_comment_submit_none_without_a_comment_button():
    scope = _LabeledScope([_LabeledButton("Like"), _LabeledButton("Repost")])
    assert publish._comment_submit(scope, _post_cfg()) is None


def test_comment_on_newest_post_types_and_submits_with_enter():
    editor = _CommentEditor()
    page = _PostsPage([editor])
    assert publish._comment_on_newest_post(page, _post_cfg(), "  a first comment  ", 1000) == (
        "a first comment"
    )
    assert editor.text == ""  # Enter submitted it
    assert editor.clicks >= 1


def test_comment_on_newest_post_uses_the_comment_button_when_enter_does_not_submit(monkeypatch):
    class _NoEnterPage(_PostsPage):
        def press(self, key):
            pass

    # The confirm wait is real seconds; shrink it so the fallback path is quick.
    monkeypatch.setattr(publish, "COMMENT_CONFIRM_SECONDS", 0.05)
    editor = _CommentEditor()
    editor.card = _LabeledScope([_ClearingButton("Comment", editor)])
    page = _NoEnterPage([editor])
    assert publish._comment_on_newest_post(page, _post_cfg(), "hello", 1000) == "hello"


def test_comment_on_newest_post_raises_without_a_comment_box(monkeypatch):
    monkeypatch.setattr(publish, "_comment_editors", lambda _page, _card=None: [])
    with pytest.raises(publish.PublishError):
        publish._comment_on_newest_post(_PostsPage([]), _post_cfg(), "hello", 1000)


def test_pin_editor_returns_none_when_the_box_vanished():
    class _Gone:
        def element_handle(self, timeout=None):
            raise RuntimeError("no node")

    assert publish._pin_editor(_Gone(), 1000) is None


def test_comment_on_newest_post_raises_when_the_box_vanishes(monkeypatch):
    class _Gone(_CommentEditor):
        def element_handle(self, timeout=None):
            raise RuntimeError("no node")

    editor = _Gone()
    monkeypatch.setattr(publish, "_comment_editors", lambda _page, _card=None: [editor])
    with pytest.raises(publish.PublishError):
        publish._comment_on_newest_post(_PostsPage([editor]), _post_cfg(), "hello", 1000)


def test_submit_comment_reports_failure_when_the_box_detaches(monkeypatch):
    # A detached box hands back stale text (AGENTS.md) and its locator would be
    # re-resolved to another post's box, so that is "not confirmed" - never a
    # success. The page-wide Comment *button* fallback must not be used either
    # (_PostsPage has no get_by_role, so reaching it would raise).
    monkeypatch.setattr(publish, "COMMENT_CONFIRM_SECONDS", 0.05)
    editor = _CommentEditor()
    editor.text = "hello"
    editor.connected = False
    assert publish._submit_comment(_PostsPage([editor]), editor, _post_cfg(), 1000) is False


def test_comment_on_newest_post_uses_the_card_of_the_given_urn(monkeypatch):
    # The admin list holds every post's card: with the published post's urn the box
    # must come from *that* card, not the first editor on the page.
    mine = _CommentEditor(placeholder="Bình luận...", container=object())
    other = _CommentEditor(placeholder="Bình luận...", container=object())
    page = _PostsPage([other, mine])

    class _Card:
        def locator(self, selector):
            return _Editors([mine])

    monkeypatch.setattr(publish, "_post_card", lambda _page, _urn: _Card())
    # Enter submits the box the flow typed into, which empties *mine*.
    monkeypatch.setattr(_PostsPage, "press", lambda _self, key: setattr(mine, "text", ""))
    assert publish._comment_on_newest_post(
        page, _post_cfg(), "hello", 1000, post_urn="urn:li:activity:5"
    ) == "hello"
    assert mine.clicks >= 1
    assert other.clicks == 0  # the other post's box was never touched


def test_comment_on_newest_post_rejects_oversized_text():
    too_long = "x" * (publish.MAX_COMMENT + 1)
    with pytest.raises(publish.PublishError):
        publish._comment_on_newest_post(_PostsPage([]), _post_cfg(), too_long, 1000)


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakePlaywright:
    """Stands in for sync_playwright(): a context manager yielding anything."""

    def __init__(self, browser):
        self._browser = browser

    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def _patch_post_flow(monkeypatch, browser, page):
    import playwright.sync_api as playwright_api

    monkeypatch.setattr(playwright_api, "sync_playwright", lambda: _FakePlaywright(browser))
    monkeypatch.setattr(
        publish.session, "open_logged_in_page", lambda *a, **k: (browser, None, page)
    )
    monkeypatch.setattr(publish.monitor, "kill_switch_present", lambda _path: False)
    monkeypatch.setattr(publish, "_open_composer", lambda *a, **k: True)
    monkeypatch.setattr(publish, "_fill_editor", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_submit", lambda *a, **k: publish.STATUS_CONFIRMED)
    monkeypatch.setattr(
        publish,
        "_latest_post",
        lambda *a, **k: (
            "urn:li:activity:1",
            "https://x/feed/update/urn:li:activity:1/",
            True,
        ),
    )


def test_publish_never_fails_when_the_comment_does(monkeypatch):
    # The post already went out: a retry would double-post it, so a failed
    # comment must come back as data, not as an error - whatever kind it is.
    browser = _FakeBrowser()
    _patch_post_flow(monkeypatch, browser, SimpleNamespace())

    def _boom(*args, **kwargs):
        raise publish.PublishError("no comment box")

    monkeypatch.setattr(publish, "_comment_on_newest_post", _boom)
    result = publish.publish_to_page(_post_cfg(), text="hi", comment="first comment")
    assert result["ok"] is True
    assert result["commented"] is False
    assert result["comment_error"] == "no comment box"
    assert browser.closed


def test_publish_reports_an_unexpected_comment_failure(monkeypatch):
    def _timeout(*args, **kwargs):
        raise TimeoutError("waiting for the comment box")

    monkeypatch.setattr(publish, "_comment_on_newest_post", _timeout)
    _patch_post_flow(monkeypatch, _FakeBrowser(), SimpleNamespace())
    result = publish.publish_to_page(_post_cfg(), text="hi", comment="first comment")
    assert result["ok"] is True
    assert result["commented"] is False
    assert result["comment_error"] == "TimeoutError: waiting for the comment box"


def test_publish_reports_a_posted_comment(monkeypatch):
    browser = _FakeBrowser()
    _patch_post_flow(monkeypatch, browser, SimpleNamespace())
    monkeypatch.setattr(publish, "_comment_on_newest_post", lambda *a, **k: "first comment")
    result = publish.publish_to_page(_post_cfg(), text="hi", comment="first comment")
    assert result["commented"] is True
    assert result["comment_error"] is None


def test_publish_omits_comment_fields_when_none_requested(monkeypatch):
    browser = _FakeBrowser()
    _patch_post_flow(monkeypatch, browser, SimpleNamespace())
    result = publish.publish_to_page(_post_cfg(), text="hi")
    assert "commented" not in result


def test_publish_rejects_an_oversized_comment_before_posting():
    with pytest.raises(publish.PublishError):
        publish.publish_to_page(_post_cfg(), text="hi", comment="x" * (publish.MAX_COMMENT + 1))


def test_publish_result_carries_the_confirmed_status(monkeypatch):
    _patch_post_flow(monkeypatch, _FakeBrowser(), SimpleNamespace())
    result = publish.publish_to_page(_post_cfg(), text="hi")
    assert result["status"] == publish.STATUS_CONFIRMED
    assert result["retryable"] is False
    assert result["post_verified"] is True  # _patch_post_flow's read-back matched


def test_publish_passes_the_caption_to_the_read_back(monkeypatch):
    _patch_post_flow(monkeypatch, _FakeBrowser(), SimpleNamespace())
    seen = {}

    def _capture(_page, _cfg, caption=""):
        seen["caption"] = caption
        return "urn:li:activity:1", "https://x/1", True

    monkeypatch.setattr(publish, "_latest_post", _capture)
    publish.publish_to_page(_post_cfg(), text="  a caption to match  ")
    assert seen["caption"] == "a caption to match"


def test_publish_reports_an_unconfirmed_post(monkeypatch):
    # The click went out but nothing confirmed it: the post may be live, so the
    # caller must not retry - a retry can double-post it.
    _patch_post_flow(monkeypatch, _FakeBrowser(), SimpleNamespace())
    monkeypatch.setattr(publish, "_submit", lambda *a, **k: publish.STATUS_UNKNOWN)
    with pytest.raises(publish.PublishUnconfirmed) as excinfo:
        publish.publish_to_page(_post_cfg(), text="hi")
    assert excinfo.value.status == publish.STATUS_UNKNOWN
    assert publish.is_retryable(excinfo.value.status) is False


def test_publish_reports_a_refused_post_as_retryable(monkeypatch):
    _patch_post_flow(monkeypatch, _FakeBrowser(), SimpleNamespace())
    monkeypatch.setattr(publish, "_submit", lambda *a, **k: publish.STATUS_REFUSED)
    with pytest.raises(publish.PublishError) as excinfo:
        publish.publish_to_page(_post_cfg(), text="hi")
    assert not isinstance(excinfo.value, publish.PublishUnconfirmed)
    assert excinfo.value.status == publish.STATUS_REFUSED
    assert publish.is_retryable(excinfo.value.status) is True


def test_publish_scopes_the_comment_to_the_published_post(monkeypatch):
    _patch_post_flow(monkeypatch, _FakeBrowser(), SimpleNamespace())
    seen = {}

    def _capture(_page, _cfg, text, _timeout_ms, post_urn=""):
        seen["urn"] = post_urn
        return text

    monkeypatch.setattr(publish, "_comment_on_newest_post", _capture)
    publish.publish_to_page(_post_cfg(), text="hi", comment="first comment")
    # _patch_post_flow's _latest_post reports urn:li:activity:1.
    assert seen["urn"] == "urn:li:activity:1"
