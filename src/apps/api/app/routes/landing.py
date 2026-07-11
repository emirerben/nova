"""Public landing-page endpoints.

GET /landing-clips?keys[]=landing/a.mp4  — return short-lived signed URLs for
curated showcase clips.

Background
----------
The landing marquee displays ~6 Kria outputs.  Clips live under the permanent
``landing/`` prefix in the storage bucket (not touched by the 24-h lifecycle
delete rule — only ``dev-user/``, ``music-jobs/``, ``music-lyrics-previews/``,
and ``voiceover-uploads/`` are purged).  The bucket runs uniform-bucket-level
access (UBLA), so objects can *not* be made public-read via ACLs.  Instead,
``page.tsx`` (a force-dynamic server component) calls this endpoint at render
time, gets fresh signed URLs (TTL = PLAYBACK_URL_TTL_MIN), and passes them to
the ShowcaseMarquee client component.  The same signed-URL-on-read pattern used
by ``routes/generative_jobs.py::_variants_for_response``.

Upload path (one-off per clip)
--------------------------------
  gsutil cp <local.mp4> gs://$STORAGE_BUCKET/landing/<slug>.mp4
  # No ACL grant needed — UBLA + this endpoint handles auth.

Then add the ``key`` field to ``SHOWCASE_CLIPS`` in
``src/apps/web/src/app/page.tsx``.
"""

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.storage import signed_get_url

log = structlog.get_logger()
router = APIRouter()

# Match the playback TTL used for generative-job variants (6 hours).
# Longer than a typical user session so clips don't expire mid-browse.
_CLIP_URL_TTL_MIN = 360

# Only objects under this prefix are eligible — prevents signing arbitrary paths.
_ALLOWED_PREFIX = "landing/"


class LandingClipUrl(BaseModel):
    key: str
    src: str | None  # None when signing fails (best-effort)


@router.get("", response_model=list[LandingClipUrl])
async def get_landing_clip_urls(
    keys: list[str] = Query(default=[]),
) -> list[LandingClipUrl]:
    """Return freshly-signed GET URLs for the given GCS object keys.

    - Only keys that start with ``landing/`` are signed (others return src=None).
    - Signing failures are best-effort: a failed key returns src=None and the
      marquee gracefully falls back to its CSS gradient.
    - No authentication required — this is a public page endpoint.
    """
    results: list[LandingClipUrl] = []
    for key in keys:
        if not key.startswith(_ALLOWED_PREFIX):
            log.warning("landing_clip_invalid_prefix", key=key)
            results.append(LandingClipUrl(key=key, src=None))
            continue
        try:
            src = signed_get_url(key, expiration_minutes=_CLIP_URL_TTL_MIN)
            results.append(LandingClipUrl(key=key, src=src))
        except Exception:  # noqa: BLE001
            log.warning("landing_clip_sign_failed", key=key)
            results.append(LandingClipUrl(key=key, src=None))
    return results
