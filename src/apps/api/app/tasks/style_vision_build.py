"""Explicitly vision-analyze a creator's TikTok videos → StyleObservation aggregate.

Downloads each enriched video MP4 to a TemporaryDirectory, uploads to the Gemini
File API, runs StyleObservationAgent per video, aggregates deterministically
(mode + ≥0.5 agreement), and persists to persona.tiktok_profile["style_observations"].

On success, chains derive_user_style when user_style_enabled and status != "edited".
Any per-video failure drops that video (best-effort). Total failure leaves the key
absent — the style_build task falls back to the metadata-only path.

Dark: gated by settings.tiktok_style_vision_enabled (default False).
No GCS — all video data lives in a TemporaryDirectory and the Gemini File API.
"""

from __future__ import annotations

import concurrent.futures
import statistics
import tempfile
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.models import Persona
from app.services.tiktok_style_analysis import TIKTOK_STYLE_ANALYSIS_CLAIM_LEASE
from app.services.tiktok_style_observations import style_observations_are_fresh
from app.worker import celery_app

log = structlog.get_logger()

_CONCURRENT_DOWNLOADS = 4  # yt-dlp parallelism cap
_MAX_VIDEOS = 8  # hard paid-analysis ceiling per explicit review
_AGREEMENT_THRESHOLD = 0.5  # min fraction agreeing for a field to be emitted


# ── Deterministic aggregator ──────────────────────────────────────────────────


def _mode_or_none(values: list[str]) -> str | None:
    """Return the most common value, or None when the list is empty."""
    if not values:
        return None
    counter = Counter(values)
    return counter.most_common(1)[0][0]


def _aggregate_observations(observations: list[dict]) -> dict[str, Any]:
    """Deterministic aggregate: mode + agreement ≥ 0.5 per categorical field.

    Returns a dict suitable for persona.tiktok_profile["style_observations"]["aggregate"].
    Fields with low agreement (< 0.5) are emitted as None so low-confidence guesses
    never override curated style-set values downstream.
    """
    n = len(observations)
    if n == 0:
        return {}

    result: dict[str, Any] = {"videos_seen": n}

    # Boolean: majority vote.
    has_text_votes = [o.get("has_on_screen_text") for o in observations]
    has_text_count = sum(1 for v in has_text_votes if v is True)
    result["has_on_screen_text"] = has_text_count >= (n / 2)

    # For categorical fields, only consider observations where text was present.
    text_obs = [o for o in observations if o.get("has_on_screen_text") is True]
    n_text = len(text_obs)

    categorical_fields = [
        "font_feel",
        "position",
        "size_class",
        "layout",
        "stroke",
        "text_anchor",
    ]
    confidence_per_field: dict[str, float] = {}

    for field in categorical_fields:
        values = [o[field] for o in text_obs if o.get(field) and o[field] != "none"]
        if not values or n_text == 0:
            result[field] = None
            confidence_per_field[field] = 0.0
            continue
        mode_val = _mode_or_none(values)
        agreement = values.count(mode_val) / n_text
        if agreement >= _AGREEMENT_THRESHOLD:
            result[field] = mode_val
            confidence_per_field[field] = round(agreement, 3)
        else:
            result[field] = None
            confidence_per_field[field] = round(agreement, 3)

    # Hex colors: median-luminance approach is impractical; use mode with agreement.
    for color_field in ("text_color_hex", "highlight_color_hex"):
        colors = [o[color_field] for o in text_obs if o.get(color_field)]
        if colors and n_text > 0:
            mode_color = _mode_or_none(colors)
            agreement = colors.count(mode_color) / n_text
            result[color_field] = mode_color if agreement >= _AGREEMENT_THRESHOLD else None
            confidence_per_field[color_field] = round(agreement, 3)
        else:
            result[color_field] = None
            confidence_per_field[color_field] = 0.0

    # Overall confidence: mean of individual observation confidences.
    confs = [o.get("confidence", 0.5) for o in observations]
    result["mean_confidence"] = round(statistics.mean(confs), 3) if confs else 0.5
    result["confidence_per_field"] = confidence_per_field

    return result


def _representative_videos(videos: list[dict], *, limit: int = _MAX_VIDEOS) -> list[dict]:
    """Choose stable performance quantiles instead of whichever rows arrive first."""

    downloadable = [video for video in videos if video.get("webpage_url")]
    ranked = sorted(
        downloadable,
        key=lambda video: (
            -float(video.get("view_index") or 0.0),
            str(video.get("upload_date") or ""),
            str(video.get("video_id") or video.get("webpage_url") or ""),
        ),
    )
    if len(ranked) <= limit:
        return ranked
    # Evenly sample the complete performance distribution, always including
    # both the strongest and weakest observed videos.
    indices = [round(index * (len(ranked) - 1) / (limit - 1)) for index in range(limit)]
    return [ranked[index] for index in indices]


# ── Download helper (runs in a thread pool) ───────────────────────────────────


def _download_video(webpage_url: str, dest_dir: str) -> str | None:
    """Download one TikTok MP4 with yt-dlp. Returns local path or None on failure."""
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError:
        log.warning("style_vision.yt_dlp_missing")
        return None

    from app.services.yt_dlp_options import with_yt_dlp_cookiefile  # noqa: PLC0415

    output_template = f"{dest_dir}/%(id)s.%(ext)s"
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": output_template,
        "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "socket_timeout": 30,
        "max_filesize": 150 * 1024 * 1024,  # 150MB hard cap
    }

    try:
        with with_yt_dlp_cookiefile() as cookie_file:
            if cookie_file is not None:
                opts["cookiefile"] = str(cookie_file.path)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(webpage_url, download=True)
                if not info:
                    return None
                import os  # noqa: PLC0415

                video_id = info.get("id", "video")
                ext = info.get("ext", "mp4")
                path = f"{dest_dir}/{video_id}.{ext}"
                return path if os.path.exists(path) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("style_vision.download_failed", url=webpage_url[:80], error=str(exc)[:200])
        return None


# ── Main task ─────────────────────────────────────────────────────────────────


def _run_tiktok_style_analysis(
    self,
    persona_id: str,
    handle: str,
    *,
    explicit: bool = False,
    usage_purpose: str | None = None,
    test_run_id: str | None = None,
    estimated_max_cost_usd: float | None = None,
    reservation_approved: bool = False,
    release_canary_id: str | None = None,
) -> str | None:  # noqa: ANN001
    """Download + vision-analyze a creator's TikTok videos → style_observations.

    Best-effort: any failure (TikTok blocked, Gemini quota, timeout) logs and returns
    silently. The persona is NEVER marked failed by this task.

    time_limit=1800 < worker visibility_timeout (1900s) invariant holds.
    Dark: gated by settings.tiktok_style_vision_enabled and an explicit request.
    """
    if not settings.tiktok_style_vision_enabled or not explicit:
        return "not_available"

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents.style_observation import (  # noqa: PLC0415
        StyleObservationAgent,
        StyleObservationInput,
    )
    from app.pipeline.agents.gemini_analyzer import gemini_upload_and_wait  # noqa: PLC0415
    from app.services.tiktok_profile import fetch_profile_enriched  # noqa: PLC0415

    persona_uuid = uuid.UUID(str(persona_id))
    with sync_session() as session:
        persona_row = session.get(Persona, persona_uuid)
        if persona_row is None:
            return "persona_not_found"
        if style_observations_are_fresh(persona_row.tiktok_profile, now=datetime.now(UTC)):
            log.info("style_vision.fresh_cache_hit", persona_id=persona_id)
            return None
        creator_id = str(persona_row.user_id)

    clean = handle
    profile = fetch_profile_enriched(clean)
    if profile is None:
        log.info("style_vision.no_profile", persona_id=persona_id, handle=clean)
        return "profile_unavailable"

    videos = profile.get("videos") or []
    downloadable = _representative_videos(videos)
    if not downloadable:
        log.info(
            "style_vision.no_downloadable_videos",
            persona_id=persona_id,
            handle=clean,
            total=len(videos),
        )
        return "no_public_videos"

    log.info(
        "style_vision.start",
        persona_id=persona_id,
        handle=clean,
        n_videos=len(downloadable),
    )

    agent = StyleObservationAgent(default_client())
    run_context = RunContext(
        creator_id=creator_id,
        request_id=f"style-vision:{persona_id}:{self.request.id}",
        usage_purpose=usage_purpose,
        test_run_id=test_run_id,
        estimated_max_cost_usd=estimated_max_cost_usd,
        reservation_approved=reservation_approved,
        release_canary_id=release_canary_id,
    )
    observations: list[dict] = []
    per_video: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        # Phase 1: parallel downloads bounded by _CONCURRENT_DOWNLOADS.
        # Submit each video alongside its metadata so we can correlate results.
        with concurrent.futures.ThreadPoolExecutor(max_workers=_CONCURRENT_DOWNLOADS) as pool:
            future_to_meta = {
                pool.submit(_download_video, v["webpage_url"], tmpdir): v for v in downloadable
            }
            paired: list[tuple[dict, str | None]] = []
            for fut, video_meta in future_to_meta.items():
                paired.append((video_meta, fut.result()))

        # Phase 2: Gemini uploads + vision inference (sequential).
        for video_meta, local_path in paired:
            if local_path is None:
                continue
            video_id = video_meta.get("video_id") or ""
            try:
                file_ref = gemini_upload_and_wait(local_path, timeout=120)
                file_uri = file_ref.uri
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "style_vision.upload_failed",
                    video_id=video_id,
                    error=str(exc)[:200],
                )
                continue

            try:
                obs = agent.run(
                    StyleObservationInput(
                        file_uri=file_uri,
                        file_mime="video/mp4",
                        caption=video_meta.get("caption", ""),
                        view_index=video_meta.get("view_index"),
                    ),
                    ctx=run_context,
                )
                obs_dict = obs.model_dump()
                observations.append(obs_dict)
                per_video.append(
                    {
                        "video_id": video_id,
                        "webpage_url": video_meta.get("webpage_url", ""),
                        "view_index": video_meta.get("view_index"),
                        "observation": obs_dict,
                    }
                )
                log.info(
                    "style_vision.video_done",
                    video_id=video_id,
                    has_text=obs.has_on_screen_text,
                    font_feel=obs.font_feel,
                    confidence=obs.confidence,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "style_vision.inference_failed",
                    video_id=video_id,
                    error=str(exc)[:200],
                )

    if not observations:
        log.info("style_vision.no_observations", persona_id=persona_id, handle=clean)
        return "analysis_unavailable"

    aggregate = _aggregate_observations(observations)

    style_observations = {
        "videos_total": len(downloadable),
        "videos_seen": len(observations),
        "aggregate": aggregate,
        "per_video": per_video,
        "observed_at": datetime.now(UTC).isoformat(),
    }

    with sync_session() as session:
        row = session.get(Persona, persona_uuid)
        if row is None:
            return
        blob = dict(row.tiktok_profile or {})
        blob["style_observations"] = style_observations
        blob.pop("style_analysis_failure", None)
        row.tiktok_profile = blob
        session.commit()

    log.info(
        "style_vision.done",
        persona_id=persona_id,
        handle=clean,
        n_seen=len(observations),
        n_total=len(downloadable),
        font_feel=aggregate.get("font_feel"),
        has_text=aggregate.get("has_on_screen_text"),
    )

    # Chain derive_user_style when the render flag is on and the user hasn't
    # hand-edited their style (the "user's say wins" invariant).
    if settings.user_style_enabled:
        with sync_session() as session:
            row = session.get(Persona, uuid.UUID(str(persona_id)))
            if row and row.style and row.style.get("status") != "edited":
                from app.tasks.style_build import derive_user_style  # noqa: PLC0415

                derive_user_style.delay(str(persona_id))
    return None


@celery_app.task(
    name="app.tasks.style_vision_build.analyze_tiktok_style",
    bind=True,
    max_retries=0,
    soft_time_limit=1740,
    time_limit=1800,
)
def analyze_tiktok_style(
    self,
    persona_id: str,
    handle: str,
    *,
    claim_id: str | None = None,
    explicit: bool = False,
    usage_purpose: str | None = None,
    test_run_id: str | None = None,
    estimated_max_cost_usd: float | None = None,
    reservation_approved: bool = False,
    release_canary_id: str | None = None,
) -> None:  # noqa: ANN001
    """Run one explicitly claimed analysis and always release its DB claim."""

    if not settings.tiktok_style_vision_enabled or not explicit or not claim_id:
        return
    try:
        persona_uuid = uuid.UUID(str(persona_id))
        claim_uuid = uuid.UUID(str(claim_id))
    except ValueError:
        return
    now = datetime.now(UTC)
    with sync_session() as session:
        row = session.scalar(select(Persona).where(Persona.id == persona_uuid).with_for_update())
        if (
            row is None
            or row.tiktok_style_analysis_claim_id != claim_uuid
            or row.tiktok_style_analysis_claim_expires_at is None
            or row.tiktok_style_analysis_claim_expires_at <= now
        ):
            return
        # Renew the route claim under the same row lock before any download or
        # provider interaction. A retrying route therefore observes an active
        # worker claim and cannot fan out a second eight-video analysis.
        row.tiktok_style_analysis_claim_expires_at = now + TIKTOK_STYLE_ANALYSIS_CLAIM_LEASE
        session.commit()
    failure_reason: str | None = None
    try:
        failure_reason = _run_tiktok_style_analysis(
            self,
            persona_id,
            handle,
            explicit=explicit,
            usage_purpose=usage_purpose,
            test_run_id=test_run_id,
            estimated_max_cost_usd=estimated_max_cost_usd,
            reservation_approved=reservation_approved,
            release_canary_id=release_canary_id,
        )
    except Exception as exc:  # noqa: BLE001
        failure_reason = "analysis_failed"
        log.exception(
            "style_vision.failed",
            persona_id=persona_id,
            error=str(exc)[:200],
        )
    finally:
        with sync_session() as session:
            row = session.get(Persona, persona_uuid)
            if row is not None and row.tiktok_style_analysis_claim_id == claim_uuid:
                if isinstance(failure_reason, str) and failure_reason:
                    profile = dict(row.tiktok_profile or {})
                    profile["style_analysis_failure"] = {
                        "reason": failure_reason,
                        "failed_at": datetime.now(UTC).isoformat(),
                    }
                    row.tiktok_profile = profile
                row.tiktok_style_analysis_claim_id = None
                row.tiktok_style_analysis_claim_expires_at = None
                session.commit()
