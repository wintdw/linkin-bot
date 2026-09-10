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
