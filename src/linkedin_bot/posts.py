"""LinkedIn member posting — the gateway a peer app calls to publish.

linkin-bot owns the OAuth token, so it is the single place that talks to
LinkedIn's Posts API. ``POST /post`` (web.py) is a thin HTTP shim over
``create_post`` so another local app (social-bot) can publish as the member
without holding the token or knowing anything about OAuth. Stdlib only, so it
imports without fastapi/uvicorn (see AGENTS.md).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

API_BASE = "https://api.linkedin.com"
POSTS_URL = f"{API_BASE}/rest/posts"
USERINFO_URL = f"{API_BASE}/v2/userinfo"
LINKEDIN_VERSION_DEFAULT = "202601"
HTTP_TIMEOUT_SECONDS = 30
VISIBILITIES = ("PUBLIC", "CONNECTIONS")


class PostError(RuntimeError):
    """A post could not be created, or the member could not be resolved."""


def _request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict | None = None,
    version: str | None = None,
    timeout: int = HTTP_TIMEOUT_SECONDS,
):
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if version:
        headers["LinkedIn-Version"] = version
        headers["X-Restli-Protocol-Version"] = "2.0.0"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise PostError(f"LinkedIn returned HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, ValueError) as exc:
        raise PostError(f"request to LinkedIn failed: {exc}") from None


def member_urn(token: str, *, timeout: int = HTTP_TIMEOUT_SECONDS) -> str:
    """``urn:li:person:<sub>`` for the token's member (via OpenID userinfo)."""
    with _request(USERINFO_URL, token=token, timeout=timeout) as response:
        payload = json.load(response)
    sub = payload.get("sub")
    if not sub:
        raise PostError(f"no sub in /v2/userinfo response: {payload}")
    return f"urn:li:person:{sub}"


def create_post(
    token: str,
    text: str,
    *,
    visibility: str = "PUBLIC",
    author_urn: str | None = None,
    version: str = LINKEDIN_VERSION_DEFAULT,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> dict:
    """Publish ``text`` as the member; return ``{"post_urn", "permalink"}``."""
    visibility = str(visibility or "PUBLIC").upper()
    if visibility not in VISIBILITIES:
        raise PostError(f"visibility must be one of {VISIBILITIES}")
    text = str(text or "").strip()
    if not text:
        raise PostError("post text is empty")
    author = author_urn or member_urn(token, timeout=timeout)
    body = {
        "author": author,
        "commentary": text,
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    with _request(
        POSTS_URL, token=token, method="POST", body=body, version=version, timeout=timeout
    ) as response:
        urn = response.headers.get("x-restli-id") or response.headers.get("X-Restli-Id") or ""
    return {
        "post_urn": urn,
        "permalink": f"https://www.linkedin.com/feed/update/{urn}/" if urn else "",
    }
