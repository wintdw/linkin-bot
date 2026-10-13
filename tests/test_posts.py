import json

import pytest

from linkedin_bot import posts


class _Resp:
    """Minimal stand-in for the urlopen response used by posts._send."""

    def __init__(self, body=b"", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, responses):
    """Replace urlopen with a scripted fake; return the captured requests."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return responses.pop(0)

    monkeypatch.setattr(posts.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_create_post_rejects_empty_text():
    with pytest.raises(posts.PostError):
        posts.create_post("tok", "   ")


def test_create_post_rejects_bad_visibility():
    with pytest.raises(posts.PostError):
        posts.create_post("tok", "hello", visibility="FRIENDS")


def test_visibilities_are_public_and_connections():
    assert posts.VISIBILITIES == ("PUBLIC", "CONNECTIONS")


def test_upload_image_registers_then_puts(monkeypatch):
    calls = _patch_urlopen(
        monkeypatch,
        [
            _Resp(
                json.dumps(
                    {"value": {"uploadUrl": "https://upload/1", "image": "urn:li:image:abc"}}
                ).encode()
            ),
            _Resp(),
        ],
    )
    urn = posts.upload_image(
        "tok", b"IMGBYTES", owner_urn="urn:li:person:42", content_type="image/png"
    )
    assert urn == "urn:li:image:abc"

    init, put = calls
    assert init.full_url == posts.IMAGES_URL + "?action=initializeUpload"
    assert init.get_method() == "POST"
    assert json.loads(init.data) == {"initializeUploadRequest": {"owner": "urn:li:person:42"}}
    assert "Bearer tok" in init.headers.values()
    assert "202601" in init.headers.values()  # LinkedIn-Version carried

    assert put.full_url == "https://upload/1"
    assert put.get_method() == "PUT"
    assert put.data == b"IMGBYTES"
    assert "image/png" in put.headers.values()
    assert "Bearer tok" not in put.headers.values()  # pre-signed URL: no token


def test_upload_image_rejects_empty_data():
    with pytest.raises(posts.PostError):
        posts.upload_image("tok", b"", owner_urn="urn:li:person:1")


def test_upload_image_errors_without_upload_fields(monkeypatch):
    _patch_urlopen(monkeypatch, [_Resp(json.dumps({"value": {}}).encode())])
    with pytest.raises(posts.PostError):
        posts.upload_image("tok", b"x", owner_urn="urn:li:person:1")


def test_create_post_with_image_attaches_media(monkeypatch):
    calls = _patch_urlopen(
        monkeypatch,
        [
            _Resp(
                json.dumps(
                    {"value": {"uploadUrl": "https://upload/1", "image": "urn:li:image:abc"}}
                ).encode()
            ),
            _Resp(),
            _Resp(headers={"x-restli-id": "urn:li:share:999"}),
        ],
    )
    result = posts.create_post(
        "tok", "hello", author_urn="urn:li:person:42", image=b"IMGBYTES", image_title="Caption"
    )
    assert result["post_urn"] == "urn:li:share:999"
    assert result["permalink"] == "https://www.linkedin.com/feed/update/urn:li:share:999/"

    post = calls[-1]
    assert post.full_url == posts.POSTS_URL
    body = json.loads(post.data)
    assert body["commentary"] == "hello"
    assert body["content"] == {"media": {"id": "urn:li:image:abc", "title": "Caption"}}


def test_create_post_with_image_omits_blank_title(monkeypatch):
    calls = _patch_urlopen(
        monkeypatch,
        [
            _Resp(
                json.dumps(
                    {"value": {"uploadUrl": "https://upload/1", "image": "urn:li:image:abc"}}
                ).encode()
            ),
            _Resp(),
            _Resp(headers={"x-restli-id": "urn:li:share:1"}),
        ],
    )
    posts.create_post("tok", "hi", author_urn="urn:li:person:1", image=b"IMG")
    body = json.loads(calls[-1].data)
    assert body["content"] == {"media": {"id": "urn:li:image:abc"}}


def test_create_post_text_only_has_no_content(monkeypatch):
    calls = _patch_urlopen(monkeypatch, [_Resp(headers={"x-restli-id": "urn:li:share:1"})])
    posts.create_post("tok", "hi", author_urn="urn:li:person:1")
    body = json.loads(calls[0].data)
    assert "content" not in body
