"""LinkedIn OAuth 2.0 (authorization-code) helpers for the ``serve`` service.

LinkedIn refuses app-only tokens for this app ("This application is not allowed
to create application tokens"), so a member token can only come from the
three-legged authorization-code flow with a browser login. This module builds
the consent URL, exchanges the returned ``code`` for a member token, and keeps
the token next to the other runtime state.

The client id/secret live in a dedicated credentials file outside the repo
(``oauth.credentials_file``, default ``/var/secret/linkedin_credentials.json``),
never in the committed ``config.yaml``; the exchanged token lands in
``<data_dir>/oauth_token.json`` (gitignored). Only stdlib is used, so this
module stays importable without fastapi/uvicorn (see AGENTS.md).

This is a single-operator, single-use flow: the ``state`` value is the fixed
constant ``STATE`` rather than a per-request nonce, which survives a service
restart between the redirect and the callback. It is not real CSRF protection —
fine for a trusted deployment, called out here so it is a deliberate choice.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
CREDENTIALS_NAME = "linkedin_credentials.json"
CREDENTIALS_FILE_DEFAULT = "/var/secret/linkedin_credentials.json"
TOKEN_NAME = "oauth_token.json"
STATE = "linkin-bot"
HTTP_TIMEOUT_SECONDS = 30


class OAuthError(RuntimeError):
    """Any failure while loading credentials or talking to LinkedIn."""


def credentials_path(credentials_file: str | Path | None = None) -> Path:
    """Path of the client id/secret file (outside the repo by default)."""
    return Path(credentials_file or CREDENTIALS_FILE_DEFAULT)


def token_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / TOKEN_NAME


def load_credentials(credentials_file: str | Path | None = None) -> dict[str, Any]:
    """Read the client id/secret file, raising OAuthError if unusable."""
    path = credentials_path(credentials_file)
    if not path.exists():
        raise OAuthError(
            f"missing {path}: create it with {{\"client_id\": \"...\", \"client_secret\": \"...\"}}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OAuthError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict) or not data.get("client_id") or not data.get("client_secret"):
        raise OAuthError(f"{path} must contain non-empty client_id and client_secret")
    return data


def build_authorize_url(
    client_id: str, redirect_uri: str, scopes: str, state: str = STATE
) -> str:
    """The LinkedIn consent URL the operator opens in a browser."""
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise OAuthError(f"LinkedIn returned HTTP {exc.code}: {body}") from None
    except (urllib.error.URLError, ValueError) as exc:
        raise OAuthError(f"request to LinkedIn failed: {exc}") from None


def exchange_code(
    client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict[str, Any]:
    """Swap an authorization ``code`` for a member access token."""
    token = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if "access_token" not in token:
        raise OAuthError(f"no access_token in LinkedIn response: {token}")
    return token


def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    """Trade a refresh token for a new access token (when LinkedIn issues one)."""
    token = _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if "access_token" not in token:
        raise OAuthError(f"no access_token in LinkedIn response: {token}")
    return token


def save_token(data_dir: str | Path, token: dict[str, Any]) -> Path:
    """Persist the token with derived expiry timestamps; file mode 0600."""
    stored = dict(token)
    now = int(time.time())
    if "expires_in" in stored:
        try:
            stored["obtained_at"] = now
            stored["expires_at"] = now + int(stored["expires_in"])
        except (TypeError, ValueError):
            pass
    path = token_path(data_dir)
    path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_token(data_dir: str | Path) -> dict[str, Any] | None:
    """The stored token, or ``None`` when the operator has not connected yet."""
    path = token_path(data_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
