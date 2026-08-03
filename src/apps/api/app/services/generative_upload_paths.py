"""Shared object-key contract for browser-direct generative uploads."""

import re

DIRECT_CLIP_PREFIX = "dev-user/"
DIRECT_VOICEOVER_PREFIX = "voiceover-uploads/direct/"

_DIRECT_CLIP_RE = re.compile(
    r"^dev-user/([^/]+)/generative/[0-9a-f]{12}(?:[0-9a-f]{20})?/clip\.[a-z0-9]+$"
)


def direct_clip_owner(path: str) -> str | None:
    match = _DIRECT_CLIP_RE.fullmatch(path)
    return match.group(1) if match else None


def direct_clip_path(user_id: str, upload_id: str, extension: str) -> str:
    return f"{DIRECT_CLIP_PREFIX}{user_id}/generative/{upload_id}/clip{extension}"


def direct_voiceover_path(user_id: str, upload_id: str, extension: str) -> str:
    return f"{DIRECT_VOICEOVER_PREFIX}{user_id}/{upload_id}/voice{extension}"
