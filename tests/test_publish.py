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


def test_activity_urn_from_feed_update_permalink():
    link = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"
    assert publish._activity_urn(link) == "urn:li:activity:7123456789"


def test_activity_urn_from_posts_permalink():
    link = "https://www.linkedin.com/posts/atento_x-activity-7123456789-abcd"
    assert publish._activity_urn(link) == "urn:li:activity:7123456789"


def test_activity_urn_none_when_absent():
    assert publish._activity_urn("https://www.linkedin.com/feed/") == ""


class _LinkPage:
    def __init__(self, values):
        self._values = values

    def evaluate(self, script):
        return self._values


def test_first_post_link_prefers_activity_urn():
    page = _LinkPage(
        [
            "https://www.linkedin.com/company/125004230/admin/dashboard/",
            "https://www.linkedin.com/feed/update/urn:li:activity:7654321/",
        ]
    )
    assert (
        publish._first_post_link(page)
        == "https://www.linkedin.com/feed/update/urn:li:activity:7654321/"
    )


def test_first_post_link_accepts_posts_permalink():
    page = _LinkPage(["https://www.linkedin.com/posts/atento_x-activity-99-abc"])
    assert publish._first_post_link(page) == "https://www.linkedin.com/posts/atento_x-activity-99-abc"


def test_first_post_link_none_without_a_post_link():
    page = _LinkPage(["https://www.linkedin.com/feed/", "https://www.linkedin.com/mynetwork/"])
    assert publish._first_post_link(page) == ""


def test_latest_post_returns_urn_and_permalink(monkeypatch):
    class Page:
        def goto(self, url, wait_until=None):
            pass

        def wait_for_timeout(self, ms):
            pass

    cfg = SimpleNamespace(
        post=SimpleNamespace(
            page_url="https://www.linkedin.com/company/1/admin/page-posts/published/"
        )
    )
    link = "https://www.linkedin.com/feed/update/urn:li:activity:42/"
    monkeypatch.setattr(publish, "_first_post_link", lambda _page: link)
    urn, permalink = publish._latest_post(Page(), cfg)
    assert urn == "urn:li:activity:42"
    assert permalink == link


def test_latest_post_empty_when_not_found(monkeypatch):
    class Page:
        def goto(self, url, wait_until=None):
            pass

        def wait_for_timeout(self, ms):
            pass

    cfg = SimpleNamespace(post=SimpleNamespace(page_url="https://x/company/1/page-posts/"))
    monkeypatch.setattr(publish, "_first_post_link", lambda _page: "")
    # deadline = monotonic()+15, then the while re-checks -> jump past it.
    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(publish.time, "monotonic", lambda: next(ticks))
    assert publish._latest_post(Page(), cfg) == ("", "")
