import json
from types import SimpleNamespace

from linkedin_bot import session


def _cfg(path):
    return SimpleNamespace(paths=SimpleNamespace(session_file=str(path)))


def test_session_status_missing(tmp_path):
    st = session.session_status(_cfg(tmp_path / "nope.json"))
    assert st["present"] is False
    assert st["saved_at"] is None
    assert st["age_days"] is None
    assert st["cookies"] == []
    assert st["has_li_at"] is False
    assert "login" in st["summary"]


def test_session_status_present_reports_cookies(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "li_at", "value": "secret"},
                    {"name": "JSESSIONID", "value": "secret"},
                ]
            }
        )
    )
    st = session.session_status(_cfg(path))
    assert st["present"] is True
    assert st["has_li_at"] is True
    assert set(st["cookies"]) == {"li_at", "JSESSIONID"}
    assert st["age_days"] is not None
    assert "saved" in st["summary"]
    # values must never be exposed
    assert "secret" not in json.dumps(st)


def test_session_status_tolerates_unreadable_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json")
    st = session.session_status(_cfg(path))
    assert st["present"] is True
    assert st["cookies"] == []
    assert st["has_li_at"] is False


def test_user_agent_hides_headless_token():
    """Cloudflare blocks ``HeadlessChrome/<ver>``; the UA must read as desktop Chrome."""
    browser = SimpleNamespace(version="151.0.7922.34")
    ua = session.user_agent(browser)
    assert "HeadlessChrome" not in ua
    assert "Chrome/151.0.7922.34" in ua
    assert ua.startswith("Mozilla/5.0 ")


def test_user_agent_works_without_a_version():
    ua = session.user_agent(SimpleNamespace())
    assert "HeadlessChrome" not in ua
    assert "Chrome/" in ua


def test_logged_in_markers_match_absolute_nav_hrefs():
    """The live nav renders absolute urls, so substring markers are required.

    Live, Oct 2026: ``a[href="/mynetwork/"]`` matched 0 elements on a logged-in
    feed page (``https://www.linkedin.com/mynetwork``, no trailing slash), so a
    valid session was misreported as dead.
    """
    markers = session.LOGGED_IN_MARKERS
    assert 'a[href="/mynetwork/"]' not in markers
    joined = " ".join(markers)
    for path in ("/feed", "/mynetwork", "/messaging/"):
        assert f'href*="{path}' in joined


def test_browser_proxy_none_when_unset():
    cfg = SimpleNamespace(browser=SimpleNamespace(proxy=""))
    assert session.browser_proxy(cfg) is None


def test_browser_proxy_parses_credentials():
    cfg = SimpleNamespace(browser=SimpleNamespace(proxy="socks5://u%40x:p%3Aw@10.0.0.5:1080"))
    assert session.browser_proxy(cfg) == {
        "server": "socks5://10.0.0.5:1080",
        "username": "u@x",
        "password": "p:w",
    }


def test_browser_proxy_without_credentials():
    cfg = SimpleNamespace(browser=SimpleNamespace(proxy="http://127.0.0.1:3128"))
    assert session.browser_proxy(cfg) == {"server": "http://127.0.0.1:3128"}
