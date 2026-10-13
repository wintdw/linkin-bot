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
