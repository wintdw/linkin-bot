from pathlib import Path

from linkedin_bot import monitor


def test_kill_switch(tmp_path: Path):
    marker = tmp_path / "STOP"
    assert not monitor.kill_switch_present(str(marker))
    marker.write_text("halt")
    assert monitor.kill_switch_present(str(marker))


def test_checkpoint_urls_detected():
    flagged = (
        "https://www.linkedin.com/checkpoint/challengesv2/ABC123",
        "https://www.linkedin.com/checkpoint/",
        "https://www.linkedin.com/authwall?trk=bfeed",
    )
    for url in flagged:
        assert monitor.is_checkpoint_url(url), url
    assert not monitor.is_checkpoint_url("https://www.linkedin.com/mynetwork/")
    assert not monitor.is_checkpoint_url(None)
    assert not monitor.is_checkpoint_url("")


def test_security_text_detected():
    assert monitor.is_security_text(
        "We noticed some unusual activity. Let's do a quick security check"
    )
    assert monitor.is_security_text("Verify it's you")
    assert not monitor.is_security_text("My Network")
    assert not monitor.is_security_text("")


def test_invitation_limit_toast_detected():
    # live wording (Sept 2026): a transient toast, not static page text
    assert monitor.is_invitation_limit_text(
        "Your invitation to Anh was not sent because you have reached the weekly "
        "limit for connection invitations. Please try again next week"
    )
    assert monitor.is_invitation_limit_text("weekly invitation limit")
    assert not monitor.is_invitation_limit_text("Invite Anh to connect")
    assert not monitor.is_invitation_limit_text("")


class _BlockPage:
    """Minimal page double: the Cloudflare block screen as the bot sees it."""

    def __init__(self, text, title="Attention Required! | Cloudflare"):
        self.url = "https://www.linkedin.com/uas/login?session_redirect=%2Ffeed%2F"
        self._text = text
        self._title = title

    def title(self):
        return self._title

    def locator(self, _selector):
        outer = self

        class _Loc:
            def inner_text(self, timeout=None):  # noqa: ARG002
                return outer._text

        return _Loc()


BLOCK_TEXT = (
    "Sorry, you have been blocked You are unable to access linkedin.com "
    "Why have I been blocked? This website is using a security service"
)


def test_block_text_detected():
    assert monitor.is_block_text(BLOCK_TEXT)
    assert monitor.is_block_text("Just a moment...")
    assert not monitor.is_block_text("Sign in to LinkedIn")
    assert not monitor.is_block_text("")


def test_looks_like_block_matches_live_wording():
    from types import SimpleNamespace

    page = _BlockPage(BLOCK_TEXT)
    assert monitor.looks_like_block(page, SimpleNamespace(status=403))
    # the block page is the evidence, not the status code alone
    assert monitor.looks_like_block(page, SimpleNamespace(status=200))


def test_looks_like_block_ignores_a_login_wall():
    """A plain /uas/login redirect (dead cookie) is NOT an edge block."""
    from types import SimpleNamespace

    page = _BlockPage("Sign in to your account Email or phone Password", title="LinkedIn Login, Sign in | LinkedIn")
    assert not monitor.looks_like_block(page, SimpleNamespace(status=200))


def test_looks_like_block_treats_bare_403_and_429_as_blocked():
    from types import SimpleNamespace

    page = _BlockPage("", title="")
    assert monitor.looks_like_block(page, SimpleNamespace(status=403))
    assert monitor.looks_like_block(page, SimpleNamespace(status=429))
    assert not monitor.looks_like_block(page, SimpleNamespace(status=302))
