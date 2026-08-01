"""Small, typed TikTok Login Kit, Content Posting, and Display API client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

_API = "https://open.tiktokapis.com"
_AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class TikTokAPIError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class TokenPayload:
    access_token: str
    refresh_token: str | None
    open_id: str
    scopes: list[str]
    expires_in: int
    refresh_expires_in: int | None


def authorization_url(state: str, scopes: list[str]) -> str:
    if not settings.tiktok_client_key or not settings.tiktok_redirect_uri:
        raise TikTokAPIError("not_configured", "TikTok OAuth is not configured", status_code=503)
    query = urlencode(
        {
            "client_key": settings.tiktok_client_key,
            "scope": ",".join(scopes),
            "response_type": "code",
            "redirect_uri": settings.tiktok_redirect_uri,
            "state": state,
        }
    )
    return f"{_AUTHORIZE}?{query}"


def _token_payload(data: dict[str, Any]) -> TokenPayload:
    if not data.get("access_token") or not data.get("open_id"):
        raise TikTokAPIError("invalid_token_response", "TikTok returned an invalid token response")
    scope = data.get("scope") or ""
    scopes = [part.strip() for part in str(scope).split(",") if part.strip()]
    return TokenPayload(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else None,
        open_id=str(data["open_id"]),
        scopes=scopes,
        expires_in=int(data.get("expires_in") or 86400),
        refresh_expires_in=(
            int(data["refresh_expires_in"]) if data.get("refresh_expires_in") else None
        ),
    )


def exchange_code(code: str) -> TokenPayload:
    return _token_payload(
        _form(
            "/v2/oauth/token/",
            {
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.tiktok_redirect_uri,
            },
        )
    )


def refresh_access_token(refresh_token: str) -> TokenPayload:
    return _token_payload(
        _form(
            "/v2/oauth/token/",
            {
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    )


def revoke_access(access_token: str) -> None:
    _form(
        "/v2/oauth/revoke/",
        {
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "token": access_token,
        },
    )


def creator_info(access_token: str) -> dict[str, Any]:
    return _json("POST", "/v2/post/publish/creator_info/query/", access_token, {})


def initialize_direct_post(
    access_token: str,
    *,
    post_info: dict[str, Any],
    media_url: str,
) -> str:
    data = _json(
        "POST",
        "/v2/post/publish/video/init/",
        access_token,
        {
            "post_info": post_info,
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": media_url,
            },
        },
        ambiguous_timeout=True,
    )
    publish_id = data.get("publish_id")
    if not publish_id:
        raise TikTokAPIError("missing_publish_id", "TikTok did not return a publish id")
    return str(publish_id)


def fetch_publish_status(access_token: str, publish_id: str) -> dict[str, Any]:
    return _json("POST", "/v2/post/publish/status/fetch/", access_token, {"publish_id": publish_id})


def user_info(access_token: str) -> dict[str, Any]:
    fields = (
        "open_id,union_id,avatar_url,display_name,bio_description,profile_deep_link,"
        "is_verified,follower_count,following_count,likes_count,video_count"
    )
    return _json("GET", f"/v2/user/info/?fields={fields}", access_token).get("user", {})


def list_videos(access_token: str, *, limit: int = 30) -> list[dict[str, Any]]:
    fields = (
        "id,create_time,cover_image_url,share_url,video_description,duration,height,"
        "width,title,embed_html,embed_link,like_count,comment_count,share_count,view_count"
    )
    videos: list[dict[str, Any]] = []
    cursor: int | None = None
    while len(videos) < limit:
        body: dict[str, Any] = {"max_count": min(20, limit - len(videos))}
        if cursor is not None:
            body["cursor"] = cursor
        data = _json("POST", f"/v2/video/list/?fields={fields}", access_token, body)
        videos.extend(data.get("videos") or [])
        if not data.get("has_more") or data.get("cursor") is None:
            break
        cursor = int(data["cursor"])
    return videos[:limit]


def _form(path: str, form: dict[str, Any]) -> dict[str, Any]:
    if not settings.tiktok_client_key or not settings.tiktok_client_secret:
        raise TikTokAPIError(
            "not_configured", "TikTok credentials are not configured", status_code=503
        )
    try:
        response = httpx.post(f"{_API}{path}", data=form, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise TikTokAPIError("network_error", "TikTok is unavailable", retryable=True) from exc
    return _decode(response)


def _json(
    method: str,
    path: str,
    access_token: str,
    body: dict[str, Any] | None = None,
    *,
    ambiguous_timeout: bool = False,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        response = httpx.request(
            method,
            f"{_API}{path}",
            headers=headers,
            json=body if method != "GET" else None,
            timeout=_TIMEOUT,
        )
    except httpx.ConnectTimeout as exc:
        raise TikTokAPIError(
            "network_timeout", "TikTok connection timed out", retryable=True
        ) from exc
    except httpx.TimeoutException as exc:
        raise TikTokAPIError(
            "submission_timeout" if ambiguous_timeout else "network_timeout",
            (
                "TikTok submission timed out after the request may have been sent"
                if ambiguous_timeout
                else "TikTok request timed out"
            ),
            retryable=not ambiguous_timeout,
            ambiguous=ambiguous_timeout,
        ) from exc
    except httpx.HTTPError as exc:
        raise TikTokAPIError("network_error", "TikTok is unavailable", retryable=True) from exc
    return _decode(response)


def _decode(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TikTokAPIError("invalid_response", "TikTok returned an invalid response") from exc
    error = payload.get("error") or {}
    code = str(error.get("code") or "")
    if response.is_error or (code and code != "ok"):
        retryable = response.status_code == 429 or response.status_code >= 500 or code == "internal"
        raise TikTokAPIError(
            code or f"http_{response.status_code}",
            str(error.get("message") or "TikTok request failed"),
            status_code=429 if response.status_code == 429 else 502,
            retryable=retryable,
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else payload
