import json
import stat
import urllib.parse

import pytest

from linkedin_bot import oauth


def test_build_authorize_url_carries_all_params():
    url = oauth.build_authorize_url("cid", "https://x/cb", "openid profile", "st8")
    parsed = urllib.parse.urlparse(url)
    assert url.startswith(oauth.AUTHORIZE_URL + "?")
    query = urllib.parse.parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["cid"]
    assert query["redirect_uri"] == ["https://x/cb"]
    assert query["scope"] == ["openid profile"]
    assert query["state"] == ["st8"]


def test_load_credentials_missing(tmp_path):
    with pytest.raises(oauth.OAuthError):
        oauth.load_credentials(tmp_path / oauth.CREDENTIALS_NAME)


def test_load_credentials_rejects_incomplete(tmp_path):
    path = tmp_path / oauth.CREDENTIALS_NAME
    path.write_text('{"client_id": "x"}')
    with pytest.raises(oauth.OAuthError):
        oauth.load_credentials(path)


def test_load_credentials_reads_values(tmp_path):
    path = tmp_path / oauth.CREDENTIALS_NAME
    path.write_text(json.dumps({"client_id": "cid", "client_secret": "sec"}))
    creds = oauth.load_credentials(path)
    assert creds["client_id"] == "cid"
    assert creds["client_secret"] == "sec"


def test_credentials_path_default_is_outside_repo():
    assert str(oauth.credentials_path()).startswith("/var/secret/")


def test_save_token_derives_expiry_and_locks_mode(tmp_path):
    path = oauth.save_token(tmp_path, {"access_token": "tok", "expires_in": 3600})
    stored = json.loads(path.read_text())
    assert stored["access_token"] == "tok"
    assert stored["expires_at"] - stored["obtained_at"] == 3600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_token_without_expires_in(tmp_path):
    path = oauth.save_token(tmp_path, {"access_token": "tok"})
    stored = json.loads(path.read_text())
    assert "expires_at" not in stored


def test_load_token_roundtrip_and_absent(tmp_path):
    assert oauth.load_token(tmp_path) is None
    oauth.save_token(tmp_path, {"access_token": "tok", "expires_in": 60})
    assert oauth.load_token(tmp_path)["access_token"] == "tok"
