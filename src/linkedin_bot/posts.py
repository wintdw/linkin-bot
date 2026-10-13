"""LinkedIn member posting — the gateway a peer app calls to publish.

linkin-bot owns the OAuth token, so it is the single place that talks to
LinkedIn's Posts API. ``POST /post`` (web.py) is a thin HTTP shim over
``create_post`` so another local app (social-bot) can publish as the member
without holding the token or knowing anything about OAuth. A post may carry an
optional image: it is uploaded through the ``/rest/images`` register-then-PUT
flow and attached as the post's media. Stdlib only, so it imports without
fastapi/uvicorn (see AGENTS.md).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

API_BASE = "https://api.linkedin.com"
POSTS_URL = f"{API_BASE}/rest/posts"
IMAGES_URL = f"{API_BASE}/rest/images"
USERINFO_URL = f"{API_BASE}/v2/userinfo"
LINKEDIN_VERSION_DEFAULT = "202601"
HTTP_TIMEOUT_SECONDS = 30
VISIBILITIES = ("PUBLIC", "CONNECTIONS")
IMAGE_CONTENT_TYPE_DEFAULT = "image/jpeg"


class PostError(RuntimeError):
    """A post could not be created, or the member/image could not be resolved."""


def _send(request: urllib.request.Request, timeout: int):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise PostError(f"LinkedIn returned HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, ValueError) as exc:
        raise PostError(f"request to LinkedIn failed: {exc}") from None


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
    return _send(urllib.request.Request(url, data=data, headers=headers, method=method), timeout)


def upload_image(
    token: str,
    data: bytes,
    *,
    owner_urn: str,
    content_type: str = IMAGE_CONTENT_TYPE_DEFAULT,
    version: str = LINKEDIN_VERSION_DEFAULT,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> str:
    """Upload image *data* owned by *owner_urn*; return its ``urn:li:image:...``.

    Two steps per LinkedIn's Posts API: register the upload (which returns a
    one-time URL + the image URN), then PUT the raw bytes to that URL. The
    upload URL is pre-signed, so it does **not** carry the bearer token.
    """
    if not data:
        raise PostError("image data is empty")
    body = {"initializeUploadRequest": {"owner": owner_urn}}
    with _request(
        f"{IMAGES_URL}?action=initializeUpload",
        token=token,
        method="POST",
        body=body,
        version=version,
        timeout=timeout,
    ) as response:
        payload = json.load(response)
    value = payload.get("value") or {}
    upload_url = value.get("uploadUrl")
    image_urn = value.get("image")
    if not upload_url or not image_urn:
        raise PostError(f"no uploadUrl/image in image initializeUpload response: {payload}")
    request = urllib.request.Request(
        upload_url, data=data, headers={"Content-Type": content_type}, method="PUT"
    )
    with _send(request, timeout):
        pass
    return image_urn


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
    image: bytes | None = None,
    image_title: str | None = None,
    image_content_type: str = IMAGE_CONTENT_TYPE_DEFAULT,
    version: str = LINKEDIN_VERSION_DEFAULT,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> dict:
    """Publish ``text`` (with optional ``image`` bytes) as the member.

    Returns ``{"post_urn", "permalink"}``. When ``image`` is given it is uploaded
    first and attached as the post's media; the text stays the post's commentary.
    """
    visibility = str(visibility or "PUBLIC").upper()
    if visibility not in VISIBILITIES:
        raise PostError(f"visibility must be one of {VISIBILITIES}")
    text = str(text or "").strip()
    if not text and image is None:
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
    if image is not None:
        image_urn = upload_image(
            token,
            image,
            owner_urn=author,
            content_type=image_content_type,
            version=version,
            timeout=timeout,
        )
        media = {"id": image_urn}
        if image_title:
            media["title"] = str(image_title)
        body["content"] = {"media": media}
    with _request(
        POSTS_URL, token=token, method="POST", body=body, version=version, timeout=timeout
    ) as response:
        urn = response.headers.get("x-restli-id") or response.headers.get("X-Restli-Id") or ""
    return {
        "post_urn": urn,
        "permalink": f"https://www.linkedin.com/feed/update/{urn}/" if urn else "",
    }
