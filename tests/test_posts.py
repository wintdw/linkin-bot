import pytest

from linkedin_bot import posts


def test_create_post_rejects_empty_text():
    with pytest.raises(posts.PostError):
        posts.create_post("tok", "   ")


def test_create_post_rejects_bad_visibility():
    with pytest.raises(posts.PostError):
        posts.create_post("tok", "hello", visibility="FRIENDS")


def test_visibilities_are_public_and_connections():
    assert posts.VISIBILITIES == ("PUBLIC", "CONNECTIONS")
