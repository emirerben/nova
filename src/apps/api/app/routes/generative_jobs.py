"""Generative-edit job endpoints.

POST /generative-jobs                                  — create a generative-mode job
GET  /generative-jobs/style-sets                       — curated text style sets (gen-eligible)
GET  /generative-jobs/{id}/status                      — poll status + variants
POST /generative-jobs/{id}/variants/{vid}/swap-song    — async re-slot against a new song
POST /generative-jobs/{id}/variants/{vid}/retext       — async re-render with new/removed text
POST /generative-jobs/{id}/variants/{vid}/change-style — async re-render with a new style set
POST /generative-jobs/{id}/variants/{vid}/edit         — combined text+style+size in ONE render
GET  /generative-jobs/{id}/variants/{vid}/timeline     — effective clip timeline + clip pool
POST /generative-jobs/{id}/variants/{vid}/timeline     — persist user timeline + re-render
DELETE /generative-jobs/{id}/variants/{vid}/timeline   — reset to the AI timeline + re-render
GET  /generative-jobs/{id}/variants/{vid}/lyric-seeds  — instant-materialize lyric TextElements
                                                          (LYRICS_OPTIONAL_ENABLED editor toggle)

A generative job needs no pre-selected song or template — the orchestrator auto-matches
a track, writes its own intro text, and renders three variants. Per-variant state lives
in `Job.assembly_plan["variants"]`, which the status endpoint surfaces directly.
"""

from __future__ import annotations

import asyncio
import copy
import math
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.agents._schemas.text_element import (
    append_ai_text_tombstones,
    merge_projected_text_elements_for_variant,
)
from app.agents._schemas.visual_block import VisualBlock
from app.auth import CurrentUserOrSynthetic, ensure_job_owner
from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import AgentRun, ContentPlan, Job, MusicTrack, PlanItem, User
from app.pipeline.look_presets import (
    EDIT_WIDE_LOOK_PRESETS,
    LOOK_PRESETS,
    LookAdjustments,
    LookPreset,
    normalize_look_adjustments,
    normalize_look_preset,
)
from app.routes.admin_music import _validate_clip_path_prefixes, _validate_voiceover_path
from app.routes.music_jobs import classify_slot_kind
from app.routes.waitlist import get_real_ip
from app.schemas.guided_edit_revision import (
    guided_editor_revision_from_approval,
    guided_editor_state_hash,
    normalize_guided_editor_revision,
)
from app.schemas.montage_preset import MASONRY_MONTAGE_PRESET, is_collage_montage_preset
from app.services.content_plan_persona import (
    PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
    PlanPersonaOwnershipError,
    load_owned_plan_persona,
)
from app.services.editor_limits import EDITOR_MAX_TIMELINE_SLOTS
from app.services.generative_upload_paths import (
    DIRECT_CLIP_PREFIX,
    DIRECT_VOICEOVER_PREFIX,
    direct_clip_owner,
    direct_clip_path,
    direct_voiceover_path,
)
from app.services.job_phases import mark_reattempt, stamp_variant_attempt
from app.services.media_overlay_preview import (
    convert_heif_overlay_preview,
    is_heif_overlay,
    nonblank_str,
)
from app.services.nova_steps import NovaStep, project_nova_steps
from app.smart_edit.schemas import SemanticRole
from app.storage import signed_get_url

log = structlog.get_logger()
router = APIRouter()

_MAX_CLIPS = 20
_DIRECT_UPLOAD_MAX_BYTES = 200 * 1024 * 1024
_DIRECT_UPLOAD_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_IMAGE_CLIP_EXTENSIONS = frozenset({".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"})

# TextElement feature flag (kill switch).  Apply:
#   fly secrets set TEXT_ELEMENTS_ENABLED=false --app nova-video + worker restart.
_TEXT_ELEMENTS_ENABLED = os.getenv("TEXT_ELEMENTS_ENABLED", "true").lower() != "false"
_LYRICS_EDITOR_ENABLED = os.getenv("LYRICS_EDITOR_ENABLED", "false").lower() == "true"
_LANDSCAPE_OUTPUT_ENABLED = os.getenv("LANDSCAPE_OUTPUT_ENABLED", "false").lower() == "true"

# Maximum number of TextElement entries accepted per PUT (A—).
_TEXT_ELEMENTS_MAX = 50
_LYRIC_LINE_OVERRIDES_MAX = 100
_LYRIC_LINE_KEY_RE = re.compile(r"^L\d+$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_LYRIC_OVERRIDE_STYLE_KEYS = frozenset({"color", "highlight_color", "font_family", "size_px"})
_LYRIC_OVERRIDE_KEYS = frozenset({"text", "style", "orig_text", "orig_start_s"})
# Lyrics-as-optional-elements: float-rounding slack when comparing a resubmitted
# `role=lyric_line` element's start_s/end_s against its own previously-persisted
# value (see the `lyrics_baked=False` branch of `validate_text_elements_payload`).
_LYRIC_ELEMENT_TIMING_TOLERANCE_S = 0.02
_LYRIC_TIMING_KEYS = frozenset(
    {"start_s", "end_s", "time_s", "duration_s", "line_start_s", "line_end_s"}
)
_UNSET = object()

_TEXT_ELEMENT_LOG_SAFE_FIELDS = frozenset(
    {
        "alignment",
        "effect",
        "end_s",
        "fade_out_ms",
        "font_family",
        "highlight_color",
        "letter_spacing",
        "line_spacing",
        "max_width_frac",
        "shadow_enabled",
        "shadow_style",
        "position",
        "reveal_s",
        "role",
        "size_class",
        "size_px",
        "start_s",
        "stroke_width",
        "text_case",
        "x_frac",
        "y_frac",
        "z",
    }
)

# Variant blobs live under `generative-jobs/` which is NOT in the GCS delete rule
# (infra/gcs-lifecycle.json) — the bytes persist indefinitely. But `output_url` is
# persisted at render time as a 1-day-TTL signed URL (storage.upload_public_read),
# so after ~24h the stored URL is an expired signature pointing at live bytes: the
# item still reads "ready" but `<video>` gets a 400 ExpiredToken. Re-sign on every
# read from the persisted relative key (`video_path`) so playback URLs are always
# fresh. 6h comfortably covers a viewing session; the page re-polls to refresh.
PLAYBACK_URL_TTL_MIN = 360
_HEIF_PREVIEW_BACKFILL_ATTEMPTED: set[str] = set()


def _text_element_shape_for_log(raw: object) -> dict:
    """Redact user text while keeping enum/numeric shape useful in prod logs."""
    if not isinstance(raw, dict):
        return {"type": type(raw).__name__}
    return {
        "id": raw.get("id"),
        "keys": sorted(str(k) for k in raw),
        "safe_fields": {k: raw.get(k) for k in sorted(_TEXT_ELEMENT_LOG_SAFE_FIELDS) if k in raw},
        "word_timings_len": (
            len(raw.get("word_timings")) if isinstance(raw.get("word_timings"), list) else None
        ),
        "source_params_keys": (
            sorted(str(k) for k in raw.get("source_params"))
            if isinstance(raw.get("source_params"), dict)
            else None
        ),
    }


# ── Schemas ────────────────────────────────────────────────────────────────────


class CreateGenerativeJobRequest(BaseModel):
    # No `target_duration_s`: output length is DERIVED, never user-set. The edit
    # is sized to the uploaded footage (and the matched song's beat structure) so
    # it can never be longer than the content the user provided. A stale frontend
    # that still posts `target_duration_s` is harmless — Pydantic drops the extra
    # field (default `extra="ignore"`).
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]
    # Closed allowlist: adding a new language requires (a) TR-style prompt branches
    # in intro_writer + overlay_format_matcher, (b) a render-side glyph-presence
    # assertion for any new diacritic ranges. Pydantic rejects unknowns at the edge.
    language: Literal["en", "tr"] = "en"
    # Optional declared edit format. The web UI does NOT send it (format selection is
    # a content-plan affordance + Lane E) — public jobs default to montage. Accepted
    # here so local-render / API clients can exercise the talking_head archetype;
    # `coerce_edit_format` normalizes it and the EDIT_FORMAT_TALKING_HEAD_ENABLED flag
    # still gates whether it actually routes. A bad token harmlessly coerces to montage.
    edit_format: str | None = None
    # Optional user-supplied voiceover (audio-only). When present the job renders
    # voiceover variants (voice over a footage montage) instead of song/original.
    # Validated against its OWN prefix so it can't be smuggled in as a footage clip.
    voiceover_gcs_path: str | None = None
    # Onboarding-supplied context: what the footage is about (topic) and what the
    # creator wants viewers to feel or do (intent). Passed through to build_generative_job
    # as item_theme / item_idea so intro_writer produces a coherent hook even without
    # a full persona. Old clients posting without these fields get None — no 422.
    topic: str | None = None
    intent: str | None = None

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clips(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 clip is required")
        if len(v) > _MAX_CLIPS:
            raise ValueError(f"Maximum {_MAX_CLIPS} clips allowed")
        # The current direct-upload path is lifecycle-managed under dev-user/.
        # Older clients still produce music-uploads/ and slot-uploads/ paths, so
        # keep those through the shared validator during the rollout window.
        legacy_paths = [path for path in v if not path.startswith(DIRECT_CLIP_PREFIX)]
        if legacy_paths:
            _validate_clip_path_prefixes(legacy_paths)
        for path in v:
            if path.startswith(DIRECT_CLIP_PREFIX) and direct_clip_owner(path) is None:
                raise ValueError("Invalid direct-upload clip path")
        return v

    @field_validator("voiceover_gcs_path")
    @classmethod
    def validate_voiceover(cls, v: str | None) -> str | None:
        return _validate_voiceover_path(v) if v else v


class GenerativeJobResponse(BaseModel):
    job_id: str
    status: str


class GenerativeUploadUrlRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    file_size_bytes: int = Field(gt=0, le=_DIRECT_UPLOAD_MAX_BYTES)


class GenerativeUploadUrlResponse(BaseModel):
    upload_url: str
    gcs_path: str
    kind: Literal["video", "image", "audio"]
    content_type: str
    upload_headers: dict[str, str]


async def validate_direct_uploads(
    req: CreateGenerativeJobRequest,
    current_user: User,
) -> None:
    """Verify ownership and real GCS metadata for browser-direct uploads.

    The signing request's byte count is only an early UX guard. The object in GCS
    is authoritative, so this check runs immediately before a render job is
    queued. Legacy clip paths remain compatible only for the synthetic public
    user. Authenticated callers use owned clip paths; legacy voiceovers have a
    temporary, metadata-validated compatibility window behind the strict flag.
    """
    from app.auth import SYNTHETIC_USER_ID  # noqa: PLC0415

    user_id = str(current_user.id)
    direct: list[tuple[str, Literal["clip", "voiceover"]]] = []
    expected_clip_prefix = f"{DIRECT_CLIP_PREFIX}{user_id}/generative/"
    expected_persistent_prefix = f"users/{user_id}/"
    is_synthetic = current_user.id == SYNTHETIC_USER_ID
    for path in req.clip_gcs_paths:
        if path.startswith(DIRECT_CLIP_PREFIX):
            if not path.startswith(expected_clip_prefix):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Upload owner mismatch"
                )
            direct.append((path, "clip"))
            continue
        if is_synthetic:
            direct.append((path, "clip"))
            continue
        if not path.startswith(expected_persistent_prefix):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Upload owner mismatch"
            )
        direct.append((path, "clip"))

    voiceover_path = req.voiceover_gcs_path
    if voiceover_path and voiceover_path.startswith(DIRECT_VOICEOVER_PREFIX):
        expected_voice_prefix = f"{DIRECT_VOICEOVER_PREFIX}{user_id}/"
        if not voiceover_path.startswith(expected_voice_prefix):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Upload owner mismatch"
            )
        direct.append((voiceover_path, "voiceover"))
    elif (
        voiceover_path and not is_synthetic and settings.generative_direct_voiceover_strict_enabled
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload owner mismatch")
    elif voiceover_path:
        # Narrow API-first compatibility window: the request schema has already
        # restricted this to voiceover-uploads/* and validate_one still verifies
        # the stored object is real audio. Direct paths never enter this branch,
        # so another user's direct namespace remains denied even while flag-off.
        direct.append((voiceover_path, "voiceover"))

    async def validate_one(path: str, role: Literal["clip", "voiceover"]) -> int:
        try:
            metadata = await run_in_threadpool(storage.object_metadata, path)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded file is missing — upload it again",
            ) from exc
        except Exception as exc:  # noqa: BLE001 — storage outage is retryable
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Upload verification unavailable — try again",
            ) from exc

        if metadata.size <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded file is empty",
            )
        if metadata.size > _DIRECT_UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File too large. Maximum 200 MB.",
            )
        kind = classify_slot_kind(Path(path).name, metadata.content_type)
        if role == "voiceover" and kind != "audio":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Voiceover upload must be audio",
            )
        if role == "clip" and kind not in {"video", "image"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Clip upload must be video or image",
            )
        return metadata.size

    sizes = await asyncio.gather(*(validate_one(path, role) for path, role in direct))
    if sum(sizes) > _DIRECT_UPLOAD_MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploads are too large together. Maximum 1 GB combined per project.",
        )


class UnplacedShot(BaseModel):
    """An assigned shot clip that could not be placed in this variant.

    shot_index is the 1-based ordinal of the clip in narrative_order (the only
    shot pointer recoverable at render time — shot_id is stripped before the job).
    reason is one of:
      "unusable_footage"  — clip absent from clip_metas (analysis failed / missing)
      "song_too_short"    — analyzed but unplaceable because the song window had
                            fewer beats than assigned shots even at n=1
    """

    clip_id: str
    gcs_path: str | None = None
    shot_index: int
    reason: Literal["unusable_footage", "song_too_short"]


class MusicWindowCapabilityOut(BaseModel):
    editable: bool
    preserve_available: bool
    video_duration_s: float
    track_duration_s: float
    recommended_start_s: float
    beat_timestamps_s: list[float]
    reason: (
        Literal[
            "track_unavailable",
            "video_duration_unknown",
            "track_duration_unknown",
            "song_shorter_than_video",
            "timing_metadata_unavailable",
        ]
        | None
    ) = None
    preserve_reason: Literal["linear_timeline_unavailable"] | None = None


class EditorCapabilitiesOut(BaseModel):
    """Typed additions plus forward-compatible existing editor capability fields."""

    music_window: MusicWindowCapabilityOut | None = None

    model_config = {"extra": "allow"}


class BackgroundMusicOut(BaseModel):
    track_id: str
    title: str
    artist: str | None = None
    preview_url: str
    src_gcs_path: str
    start_s: float
    end_s: float
    duration_s: float
    track_duration_s: float
    gain_db: float
    muted: bool
    enabled: bool = True


class GenerativeVariant(BaseModel):
    """Per-variant state as surfaced on the status response.

    All fields are optional so the model is forward-compatible: older jobs (rendered
    before PR2 instrumentation) may lack timestamps and error_class.
    """

    variant_id: str
    render_status: str | None = None
    ok: bool | None = None
    output_url: str | None = None
    video_path: str | None = None
    poster_path: str | None = None
    poster_url: str | None = None
    music_track_id: str | None = None
    track_title: str | None = None
    # Fresh-signed preview URL (+ best-section offset) for the variant's matched
    # track, minted on every status read. The editor's virtual preview cannot
    # rely on the public /music-tracks gallery for this: the generative matcher
    # deliberately considers unpublished tracks, which the gallery filters out.
    # Owner-gated response, so only tracks the user's own variants reference
    # are signed — the unpublished library is not enumerable through this.
    music_preview_url: str | None = None
    music_preview_start_s: float | None = None
    background_music: BackgroundMusicOut | None = None
    editor_capabilities: EditorCapabilitiesOut | None = None
    speech_cut_candidates: list[dict] | None = None
    speech_cut_revision: str | None = None
    speech_cut_in_flight: dict | None = None
    speech_cut_last_receipt: dict | None = None
    speech_cut_last_error: dict | None = None
    silence_cut: dict | None = None
    silence_cut_outcome: str | None = None
    speech_cleanup_failure_reason: str | None = None
    text_mode: str | None = None
    style_set_id: str | None = None
    rank: int | None = None
    intro_text_size_px: int | None = None
    intro_size_source: str | None = None
    resolved_archetype: str | None = None
    mix: float | None = None
    # Background-sound (voice/bed) level for narrated variants — None means Kria's
    # render-time default. Editable post-gen via the BackgroundSoundControl reburn
    # (NOT `mix`, which is scoped to voiceover_only/voiceover_music variants).
    voiceover_bed_level: float | None = None
    # Per-variant render timing (D6 tile clock — instrumented by PR2).
    render_started_at: str | None = None
    render_finished_at: str | None = None
    # Machine-readable error class for the frontend copy taxonomy (PR2).
    # The raw `error` field stays as-is (admin-only debug detail).
    error_class: str | None = None
    # Persisted AI-intro text (agent_text variants) — the instant-edit overlay seed.
    intro_text: str | None = None
    intro_highlight_word: str | None = None
    # Effective intro layout: "linear" (default) or "cluster" (editorial word-
    # cluster). The instant text editor MUST NOT local-preview cluster intros —
    # its TS layout mirror only models the linear single-block layout; cluster
    # edits go through the server reburn path instead.
    intro_layout: str | None = None
    # Authoritative intro mode (D19): "sequence" (transcript-synced editorial
    # typography) | "cluster" | "linear". `sequence_synced` is the FE-convenience
    # boolean (intro_mode == "sequence") — synced variants disable intro-text /
    # highlight edits (the words come from the voiceover) but keep the size nudge.
    intro_mode: str | None = None
    sequence_synced: bool | None = None
    # Fast-reburn base: the text-free, audio-mixed video behind agent_text variants.
    # `base_video_path` is the persisted GCS key; `base_video_url` is a fresh-signed
    # playback URL minted on every status read (mirrors output_url re-signing) so
    # the browser can play the base under a client-side text overlay (instant edit).
    base_video_path: str | None = None
    base_video_url: str | None = None
    base_poster_path: str | None = None
    base_poster_url: str | None = None
    pre_overlay_poster_path: str | None = None
    pre_overlay_poster_url: str | None = None
    # Strict guided-story approval and publication evidence.
    story_timeline: list[dict] | None = None
    proposal_version: int | None = None
    media_digest: str | None = None
    render_receipt: dict | None = None
    duration_s: float | None = None
    # Narrated on-video caption editor: editable cues [{text, start_s, end_s}]
    # (assembled-time). Present only on narrated variants with an editable base.
    caption_cues: list[dict] | None = None
    # Subtitles on/off, independent of caption_cues count — off always yields the
    # caption-free burn even when cues are stored, so toggling back on needs no
    # re-transcription. None on legacy variants predating this field; the editor
    # treats missing as enabled (matches the render-time default of True).
    captions_enabled: bool | None = None
    # Independent caption position override (decoupled from style_set_id); null
    # means the renderer uses the style-set value. Written EITHER by a creator
    # position edit (which also sets caption_position_user_edited) OR by the
    # render worker's face-aware placement, which mirrors its chosen y here
    # WITHOUT the user-edited flag — so a non-null value does not imply the
    # creator pinned it. Only caption_position_user_edited means "user pinned".
    caption_margin_v: int | None = None
    caption_size_px: int | None = None
    caption_text_color: str | None = None
    caption_highlight_color: str | None = None
    caption_stroke_width: int | None = None
    caption_shadow_enabled: bool | None = None
    intro_font_family: str | None = None
    intro_effect: str | None = None
    intro_text_color: str | None = None
    intro_cluster_hero_font: str | None = None
    intro_cluster_body_font: str | None = None
    intro_cluster_accent_font: str | None = None
    intro_cluster_hero_size_px: int | None = None
    intro_cluster_body_size_px: int | None = None
    intro_cluster_accent_size_px: int | None = None
    # Assigned shot clips that couldn't be placed in this variant. Absent (None)
    # on pool-only jobs, legacy renders, and variants where all shots landed.
    # Present only when ≥1 assigned clip was left unplaced after match().
    unplaced_shots: list[UnplacedShot] | None = None
    # Spoken-word timing map for the editor + copilot: {"source", "words":
    # [{"w","s","e"}], "pauses": [{"s","e","after"}]}, assembled-timeline
    # seconds (see services/speech_map.py). Absent when the variant has no
    # persisted word-level speech source.
    speech_map: dict | None = None
    # Advisory SFX placements proposed by sfx_autoplace (dark-flagged);
    # stale-filtered against the current transcript hash on every read.
    pending_sfx_suggestions: list[dict] | None = None

    model_config = {"extra": "allow"}


class ArchetypeFallbackOut(BaseModel):
    """Typed shape for assembly_plan["archetype_fallback"] on the status response.

    Field-level allowlist: whatever else a hand-edited JSONB row carries under that
    key, only these two documented fields reach the public payload (Pydantic strips
    unknown keys). Mirrored by `ArchetypeFallback` in the web app's
    plan-generate-gate.ts — keep in sync.
    """

    declared: str | None = None
    reason: str | None = None


class GenerativeJobStatusResponse(BaseModel):
    job_id: str
    status: str
    # Keep dict pass-through for backwards-compatible internal callers.
    variants: list[dict]
    error_detail: str | None
    # Stable machine-readable failure taxonomy persisted on the Job. Public
    # owners need this to distinguish strict-story verification failures from
    # a generic render error without exposing internal exception details.
    failure_reason: str | None = None
    speech_cleanup_failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    # The plan-declared edit format (montage default). Per-variant `resolved_archetype`
    # (what actually rendered, after footage resolution + fallback) lives on each
    # variant dict. Carried for verification + Lane E UI; the current UI ignores it.
    edit_format: str | None = None
    # Phase tracking (D2/D6 — instrumented by PR2).
    # content_plan-mode jobs run through orchestrate_generative_job and carry full phase fields;
    # null only for pre-0015 legacy rows or deploy-skew window.
    current_phase: str | None = None
    phase_log: list[dict] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expected_phase_durations: dict[str, int] | None = None
    # Style-downgrade explanation persisted by the orchestrator when the declared
    # edit_format fell back to montage (e.g. narrated self-narration found no speech).
    # Null when the declared format rendered. Drives the item-page banner so a style
    # swap is never silent. `reason` values are an INTERNAL enum (no_speech,
    # spine_extraction_failed, flag_disabled, ...) — clients map the known ones to
    # specific copy and show a generic downgrade banner for anything else.
    archetype_fallback: ArchetypeFallbackOut | None = None
    # True while a non-terminal job's worker heartbeat has gone stale — the
    # render attempt died silently (OOM/SIGKILL) and Celery's acks_late
    # redelivery hasn't resumed it yet (2026-07-21 incident, job e8173a25).
    # Computed at READ time from jobs.worker_heartbeat_at, never persisted;
    # flips back to false the moment the redelivered attempt starts beating.
    retrying: bool = False
    # Owner-safe Nova activity feed (app/services/nova_steps.py), projected
    # from pipeline_trace + phase_log + AgentRun at READ time -- never
    # persisted. None (not []) when `nova_steps_feed_enabled` is off, so the
    # response stays byte-identical to pre-PR1 output while the flag is off.
    steps: list[NovaStep] | None = None


class SwapSongRequest(BaseModel):
    new_track_id: str


class RetextRequest(BaseModel):
    # text=None + remove=True removes the overlay; text set replaces it.
    text: str | None = None
    remove: bool = False


class ChangeStyleRequest(BaseModel):
    style_set_id: str


class SetIntroSizeRequest(BaseModel):
    # Absolute font size in px for the AI intro overlay; clamped to the intro
    # envelope server-side. The frontend ±stepper sends current_px ± step.
    text_size_px: int = Field(..., gt=0)


class SetIntroTimingRequest(BaseModel):
    # User-authored intro overlay timing in assembled-video seconds.
    # Both bounds are required; the renderer clamps them to the video duration.
    start_s: float = Field(0.0, ge=0.0)
    end_s: float = Field(..., gt=0.0)


class SceneTimingPatch(BaseModel):
    # Index into the variant's current scene_timings array (0-based).
    scene_index: int = Field(..., ge=0)
    start_s: float = Field(..., ge=0.0)
    end_s: float = Field(..., gt=0.0)


class PatchSceneTimingRequest(BaseModel):
    overrides: list[SceneTimingPatch]


class SetMixRequest(BaseModel):
    # Voice-prominence for a voiceover variant: 1.0 = bed fully ducked (voice only),
    # 0.0 = bed at full. The frontend slider sends the absolute value.
    mix: float = Field(..., ge=0.0, le=1.0)


class CarouselMomentEditRequest(BaseModel):
    """Partial carousel-moment config for the carousel editor panel.

    All fields optional — an omitted field keeps the variant's persisted
    value; the top-level `EditVariantRequest.carousel_moment` itself is what
    carries the absent/null/dict tri-state (see `dispatch_edit_variant`):
    omitting `carousel_moment` entirely leaves the moment unchanged, an
    explicit top-level `null` removes it, and a `CarouselMomentEditRequest`
    body here partial-merges over whatever's persisted (present keys win,
    absent keys keep). Enum/range validation happens in
    `dispatch_edit_variant`, matching every other loosely-typed field on
    `EditVariantRequest` (font_family, effect, text_color, ...).
    """

    position: str | None = None  # "intro" | "middle" | "outro"
    mode: str | None = None  # "focus" | "rolling" | "stills"
    effect: str | None = None  # "scale_sweep" | "cover_flow" | "cards_stack" | "flipbook"
    # Index into the variant's ordered carousel clips; only meaningful for
    # mode="focus". Explicit `null` clears a previously-pinned focus card.
    focus_clip_index: int | None = None
    duration_s: float | None = None  # clamped to [2.0, 15.0] in dispatch, not rejected
    transition: str | None = None  # "crossfade" | "none"
    sequence: list[dict[str, Any]] | None = None
    move_duration_s: float | None = None
    zoom_duration_s: float | None = None
    transition_in: str | None = None
    transition_in_duration_s: float | None = None
    transition_out: str | None = None
    transition_out_duration_s: float | None = None
    timing_model: str | None = None

    model_config = {"extra": "forbid", "allow_inf_nan": False}


class EditVariantRequest(BaseModel):
    """Combined text/style/size edit — the instant-edit "Done" commit.

    The browser previews edits locally (base video + DOM overlay) and commits the
    whole editing session as ONE request → ONE `regenerate_generative_variant` run,
    instead of the legacy one-render-per-field endpoints. At least one field must
    be set; `text` and `remove_text` are mutually exclusive.
    """

    text: str | None = None
    remove_text: bool = False
    style_set_id: str | None = None
    text_size_px: int | None = Field(None, gt=0)
    # Intro layout pick: "linear" (one centered block) or "cluster" (editorial
    # word-cluster). User-facing style option after render — applies via the
    # fast-reburn path. Cluster requires a 3-6 word hook (validated below).
    intro_layout: str | None = None
    # Independent style overrides — decouple font / animation / color from style_set_id.
    # Each overrides only its aspect; the style set continues to own the rest
    # (position, anchor, stroke, highlight color). Validated in dispatch_edit_variant.
    font_family: str | None = None
    effect: str | None = None
    text_color: str | None = None
    # Editorial cluster per-role font overrides.
    cluster_hero_font: str | None = None
    cluster_body_font: str | None = None
    cluster_accent_font: str | None = None
    # Editorial cluster per-role size overrides (absolute px, clamped server-side).
    cluster_hero_size_px: int | None = Field(None, gt=0)
    cluster_body_size_px: int | None = Field(None, gt=0)
    cluster_accent_size_px: int | None = Field(None, gt=0)
    # Occlude the AI-intro overlay behind the moving subject (text-behind-subject
    # feature). Tri-state: None = keep current. Gated server-side by
    # settings.text_behind_subject_enabled; a stale client sending this while the
    # flag is off is coerced to None (not rejected) in dispatch_edit_variant.
    text_behind_subject: bool | None = None
    # Carousel-moment edit (Blossom carousel). Tri-state at THIS level:
    # absent (field not in the request body) = leave unchanged; explicit
    # `null` = remove the moment; an object = partial edit. `edit_variant`
    # reads `model_fields_set` to tell absent from explicit-null (both
    # otherwise deserialize to Python `None` on this field).
    carousel_moment: CarouselMomentEditRequest | None = None


class TextElementsRequest(BaseModel):
    """Full-replace body for PUT /text-elements.

    `elements` is the entire new TextElement list (raw dicts — validation is
    performed inside `dispatch_set_text_elements` via `coerce_text_elements`).
    Invalid entries are dropped silently; if all entries are invalid the list
    is stored empty (clears the overlay).

    `render=True` (default): persist + enqueue the fast-reburn.  The variant
    flips to render_status="rendering".
    `render=False`: persist only — no Celery task is dispatched.  Useful for
    a "save draft" step before an explicit Apply.
    """

    elements: list[dict] = Field(default_factory=list)
    render: bool = True


class LyricsSectionRequest(BaseModel):
    enabled: bool | None = None
    line_overrides: dict | None = None


class LyricSeedsResponse(BaseModel):
    """GET .../lyric-seeds (lyrics-as-optional-elements instant materialize).

    `elements` are TextElement-shaped dicts (`role="lyric_line"`) the FE can
    merge straight into its local editor state when the Lyrics toggle flips
    on — nothing is persisted until the user Saves (the normal text-elements
    write path, `dispatch_set_text_elements` / editor-commit)."""

    elements: list[dict]


# ── Timeline editor schemas ────────────────────────────────────────────────────

_TIMELINE_MAX_SLOTS = EDITOR_MAX_TIMELINE_SLOTS
# Server-side guardrails on a user-edited timeline. Positive durations are
# required below; beat timelines retain a natural one-beat minimum and no-grid
# timelines retain half-second snapping. The ceiling matches the product's
# sub-60s short-form contract.
TIMELINE_MAX_TOTAL_S = 60.0
# Only the montage text variants carry a user-editable slot timeline. Lyrics are
# beat/line synced (re-cutting breaks sync), voiceover variants are fit to the
# voice bed, talking_head has no slot layout at all.
_TIMELINE_EDITABLE_VARIANTS = ("song_text", "original_text")


def visual_block_variant_duration(variant: dict) -> float:
    """Resolve output duration for visual-block bounds, including old variants."""
    explicit = float(variant.get("duration_s") or variant.get("output_duration_s") or 0.0)
    if explicit > 0:
        return explicit
    timeline = variant.get("user_timeline") or variant.get("ai_timeline") or {}
    slots = [slot for slot in timeline.get("slots") or [] if not slot.get("removed")]
    slot_total = _active_timeline_duration_s(slots)
    if slot_total > 0:
        return slot_total
    timed_rows = (
        list(variant.get("caption_cues") or [])
        + list(variant.get("narrated_timings") or [])
        + list(variant.get("text_elements") or [])
    )
    return max((float(row.get("end_s") or 0.0) for row in timed_rows), default=0.0)


class TimelineSlotEdit(BaseModel):
    """One slot as posted by the timeline editor.

    `slot_id=None` marks a NEW slot (the server assigns a uuid4). `clip_index`
    indexes into `job.all_candidates["clip_paths"]` — clients never send paths.
    Beat slots size in `duration_beats` (walked against the real grid); slots
    with `duration_beats=None` (no-grid variants, footage-trimmed slots, or the
    exact terminal tail after a grid's final usable beat) send their exact
    window in `duration_s`.
    """

    slot_id: str | None = None
    parent_segment_id: str | None = None
    clip_index: int
    in_s: float
    duration_beats: int | None = None
    duration_s: float | None = None
    removed: bool = False
    transition_after: Literal["cut", "crossfade", "dip_to_black", "flash"] = "cut"
    transition_duration_s: float | None = Field(default=None, ge=0.0, le=1.0)
    # None means an older client omitted the field; resolution preserves the
    # current slot value in that case. Explicit "none" remains the clear action.
    look_preset: LookPreset | None = None
    # Omission preserves legacy state. Explicit null clears controls; for a
    # customizable preset the renderer then resolves its authored defaults.
    look_adjustments: LookAdjustments | None = None

    @field_validator("look_preset", mode="before")
    @classmethod
    def reject_null_look_preset(cls, value: object) -> object:
        if value is None:
            raise ValueError("look_preset cannot be null")
        return value

    @model_validator(mode="after")
    def normalize_cut_transition_duration(self) -> TimelineSlotEdit:
        """Accept the guided revision's canonical zero-duration hard cut.

        Timeline drafts use ``0`` for cuts while the legacy commit payload used
        ``None``. Normalize both representations here so a valid guided draft
        cannot fail Save merely because it crossed that adapter boundary.
        """
        if self.transition_after == "cut":
            self.transition_duration_s = None
        elif self.transition_duration_s is not None and self.transition_duration_s < 0.1:
            raise ValueError("animated transition duration must be at least 0.1 seconds")
        return self


class TimelineEditRequest(BaseModel):
    slots: list[TimelineSlotEdit] = Field(default_factory=list)
    # Guided-story editor v2 CAS. Legacy timeline clients omit this field.
    revision_number: int | None = Field(default=None, ge=1)
    base_generation: str | None = None
    # Full story-native revision payload used by Save/Apply. Timeline-only
    # clients can continue sending `slots`; the server projects those into the
    # revision without touching the immutable approval.
    guided_revision: dict[str, Any] | None = None

    @field_validator("slots")
    @classmethod
    def validate_slot_count(cls, v: list[TimelineSlotEdit]) -> list[TimelineSlotEdit]:
        # Removed rows are wire tombstones, not active output slots. Keep the
        # active capacity aligned with the browser/compiler while still
        # bounding total request size against pathological tombstone payloads.
        if sum(not slot.removed for slot in v) > _TIMELINE_MAX_SLOTS:
            raise ValueError(f"Maximum {_TIMELINE_MAX_SLOTS} active timeline slots allowed")
        if len(v) > _TIMELINE_MAX_SLOTS * 2:
            raise ValueError(f"Maximum {_TIMELINE_MAX_SLOTS * 2} timeline rows allowed")
        return v


class TimelineSlotOut(BaseModel):
    """One effective-timeline slot on the GET response (user slot if edited,
    else AI slot). All fields optional + extra-allowed so a worker-side schema
    addition never 500s the read path."""

    slot_id: str | None = None
    clip_index: int | None = None
    source_gcs_path: str | None = None
    source_duration_s: float | None = None
    in_s: float | None = None
    duration_s: float | None = None
    duration_beats: int | None = None
    order: int | None = None
    moment_energy: float | None = None
    moment_description: str | None = None
    removed: bool = False
    look_preset: LookPreset = "none"
    look_adjustments: LookAdjustments | None = None

    model_config = {"extra": "allow"}


class TimelineClipOut(BaseModel):
    """One entry of the job's full clip pool (including clips not currently used)."""

    clip_index: int
    signed_url: str | None = None
    duration_s: float | None = None
    used: bool = False
    media_id: str | None = None
    generation: str | None = None
    kind: Literal["image", "video"] | None = None


class TimelineResponse(BaseModel):
    editable: bool
    reason: str | None = None
    beat_grid: list[float]
    total_duration_s: float
    has_user_edits: bool
    slots: list[TimelineSlotOut]
    clips: list[TimelineClipOut]
    edit_wide_look_presets: list[LookPreset] = Field(default_factory=list)
    look_presets: list[LookPreset] = Field(default_factory=list)
    revision_number: int | None = None
    revision_hash: str | None = None
    base_generation: str | None = None
    source_pool: list[dict[str, Any]] = Field(default_factory=list)
    tombstones: list[dict[str, Any]] = Field(default_factory=list)


# ── Transactional editor commit (E2) ──────────────────────────────────────────


class EditorCommitMix(BaseModel):
    """Editor mix section. `music_level` maps onto the existing per-variant `mix`
    semantics (voice/bed balance — voiceover variants only). `original_level` is
    persisted for round-tripping but not yet honored by the render pipeline."""

    music_level: float | None = Field(None, ge=0.0, le=1.0)
    original_level: float | None = Field(None, ge=0.0, le=1.0)


class EditorCommitMusicWindow(BaseModel):
    start_s: float = Field(ge=0.0)
    alignment: Literal["preserve_cuts", "resync_beats"]

    @field_validator("start_s")
    @classmethod
    def validate_finite_start(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Song start must be finite")
        return value


class EditorCommitBackgroundMusic(BaseModel):
    """Full replacement background bed for editor commits.

    ``track_id=None`` or ``enabled=false`` clears the bed. When a track is set,
    the persisted renderer contract remains ``smart_music_treatment``.
    """

    track_id: str | None = None
    enabled: bool = True
    start_s: float | None = Field(None, ge=0.0)
    end_s: float | None = Field(None, ge=0.0)
    gain_db: float | None = Field(None, ge=-40.0, le=0.0)
    muted: bool = False

    @field_validator("start_s", "end_s")
    @classmethod
    def validate_finite_time(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Music timing must be finite")
        return value


class EditorCommitCaptionMeta(BaseModel):
    enabled: bool | None = None
    style: Literal["sentence", "word"] | None = None
    font: str | None = None
    font_set: bool = False
    y_frac: float | None = Field(None, ge=0.30, le=0.90)
    size_px: int | None = Field(None, ge=36, le=160)
    color: str | None = None
    highlight_color: str | None = None
    stroke_width: int | None = Field(None, ge=0, le=12)
    shadow_enabled: bool | None = None

    @field_validator("color", "highlight_color")
    @classmethod
    def validate_caption_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if not _HEX_COLOR_RE.match(clean):
            raise ValueError("Caption colors must be #RRGGBB hex colors.")
        return clean.upper()


class EditorCommitRequest(BaseModel):
    """One atomic editor Save: every provided section validates first; nothing
    persists unless ALL sections are valid. `base_generation` is the baseline the
    client loaded (the variant's `render_generation_id`, falling back to
    `render_finished_at` for variants never edited through the editor) — a moved
    baseline means another tab/render won and the commit 409s (baseline_conflict).
    """

    text_elements: list[dict] | None = None
    caption_cues: list[dict] | None = None
    caption_meta: EditorCommitCaptionMeta | None = None
    timeline_slots: list[TimelineSlotEdit] | None = None
    mix: EditorCommitMix | None = None
    music_track_id: str | None = None
    # Explicit music removal. Deliberately NOT an overloaded nullable
    # music_track_id: `music_track_id=None` is indistinguishable from "omitted"
    # in JSON/Pydantic, so removal gets its own flag. Mutually exclusive with
    # music_track_id / music_window (422).
    remove_music: bool = False
    music_window: EditorCommitMusicWindow | None = None
    background_music: EditorCommitBackgroundMusic | None = None
    lyrics: LyricsSectionRequest | None = None
    orientation: str | None = None
    sound_effects: list[dict] | None = None
    media_overlays: list[dict] | None = None
    visual_blocks: list[VisualBlock] | None = None
    motion_scenes: list[dict] | None = None
    motion_runtime_hash: str | None = None
    camera_effects: list[dict] | None = None
    # Carousel-moment edit (Blossom carousel), staged as a batched-Save section —
    # reuses `CarouselMomentEditRequest` verbatim (do not fork). Tri-state at
    # THIS level, mirroring `EditVariantRequest.carousel_moment` /
    # `dispatch_edit_variant`: absent (not in `model_fields_set`) = leave
    # unchanged; explicit top-level `null` = remove the moment; an object =
    # partial edit, merged over whatever's persisted by
    # `_merge_carousel_moment_override`. Read via `model_fields_set` in
    # `prepare_editor_commit`, same pattern as `edit_variant`'s route handler.
    carousel_moment: CarouselMomentEditRequest | None = None
    title: str | None = Field(None, max_length=300)
    base_generation: str = ""
    # AI-suggestion resolution metadata, NOT a section: envelope ids from
    # variants[i]["overlay_suggestions"] the user ✓-accepted in the editor.
    # Their cards arrive inside `media_overlays`/`sound_effects`; the commit
    # drops the envelopes atomically with that write. Explicit ids (not
    # overlay.id inference) so a replayed/double Save is a no-op and validators
    # can never confuse a user upload with an accepted suggestion.
    accepted_suggestion_ids: list[str] | None = None
    # Audit-only identities for the latest untouched Copilot turn. The Save
    # route links them to the canonical server revision in the same commit.
    copilot_receipt_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    # Story-native v2 revision. When present this is the complete normalized
    # guided editor state; legacy sections remain mutually exclusive.
    guided_revision: dict[str, Any] | None = None
    guided_revision_number: int | None = Field(default=None, ge=1)
    retry_guided_revision: bool = False

    @field_validator("timeline_slots")
    @classmethod
    def validate_commit_slot_count(
        cls, v: list[TimelineSlotEdit] | None
    ) -> list[TimelineSlotEdit] | None:
        if v is not None and sum(not slot.removed for slot in v) > _TIMELINE_MAX_SLOTS:
            raise ValueError(f"Maximum {_TIMELINE_MAX_SLOTS} active timeline slots allowed")
        if v is not None and len(v) > _TIMELINE_MAX_SLOTS * 2:
            raise ValueError(f"Maximum {_TIMELINE_MAX_SLOTS * 2} timeline rows allowed")
        return v


class EditorCommitSections(BaseModel):
    text_elements: bool
    caption_cues: bool
    caption_meta: bool
    timeline: bool
    mix: bool
    music: bool
    background_music: bool = False
    lyrics: bool
    orientation: bool = False
    sound_effects: bool
    media_overlays: bool
    visual_blocks: bool
    motion_scenes: bool = False
    camera_effects: bool = False
    carousel_moment: bool = False
    title: bool


class EditorCommitResponse(BaseModel):
    ok: bool
    generation: str
    sections: EditorCommitSections
    revision_number: int | None = None
    revision_hash: str | None = None
    expected_duration_s: float | None = Field(default=None, ge=0)


def _is_generated_effect_source(value: object) -> bool:
    source = str(value or "").strip()
    return source in {"smart_captions", "overlay_suggestion", "edit_ai"}


def _removed_overlay_effect_groups(
    existing: list[dict] | None,
    replacement: list[dict] | None,
    transferred_media_owners: list[dict] | None = None,
) -> set[str]:
    """Groups whose generated owner card was explicitly removed.

    Legacy cards have no group and are deliberately ignored: timing proximity
    is not proof of ownership and could delete an unrelated manual effect.
    """

    kept_ids = {
        str(item.get("id"))
        for item in replacement or []
        if isinstance(item, dict) and item.get("id")
    }
    transferred_owners = {
        (str(item.get("id")), str(item.get("effect_group_id")))
        for item in transferred_media_owners or []
        if isinstance(item, dict)
        and item.get("kind") == "media"
        and item.get("id")
        and item.get("effect_group_id")
        and _is_generated_effect_source(item.get("source"))
    }
    return {
        str(item.get("effect_group_id"))
        for item in existing or []
        if isinstance(item, dict)
        and item.get("id")
        and str(item.get("id")) not in kept_ids
        and item.get("effect_group_id")
        and (str(item.get("id")), str(item.get("effect_group_id"))) not in transferred_owners
        and _is_generated_effect_source(item.get("source"))
    }


def _without_generated_effect_groups(
    effects: list[dict] | None,
    removed_group_ids: set[str],
) -> list[dict]:
    return [
        effect
        for effect in effects or []
        if not (
            isinstance(effect, dict)
            and str(effect.get("effect_group_id") or "") in removed_group_ids
            and _is_generated_effect_source(effect.get("source"))
        )
    ]


def cascade_removed_overlay_effect_groups(
    variant: dict,
    replacement_overlays: list[dict],
    *,
    sound_effects: list[dict] | None = None,
    camera_effects: list[dict] | None = None,
    transferred_media_owners: list[dict] | None = None,
) -> tuple[list[dict] | None, list[dict] | None]:
    """Return explicit sibling-lane replacements for removed generated cards.

    ``None`` means the lane was neither supplied by the caller nor changed by
    the cascade. This lets commit/render routing avoid materializing empty
    lanes or forcing a full render when no grouped camera effect existed.
    """

    removed_group_ids = _removed_overlay_effect_groups(
        list(variant.get("media_overlays") or []),
        replacement_overlays,
        transferred_media_owners,
    )
    if not removed_group_ids:
        return sound_effects, camera_effects

    sfx_source = (
        sound_effects if sound_effects is not None else list(variant.get("sound_effects") or [])
    )
    camera_source = (
        camera_effects if camera_effects is not None else list(variant.get("camera_effects") or [])
    )
    filtered_sfx = _without_generated_effect_groups(sfx_source, removed_group_ids)
    filtered_camera = _without_generated_effect_groups(camera_source, removed_group_ids)
    cascaded_sfx = (
        filtered_sfx if sound_effects is not None or len(filtered_sfx) != len(sfx_source) else None
    )
    cascaded_camera = (
        filtered_camera
        if camera_effects is not None or len(filtered_camera) != len(camera_source)
        else None
    )
    return cascaded_sfx, cascaded_camera


class OrientationRequest(BaseModel):
    orientation: str
    revision_number: int | None = Field(default=None, ge=1)
    base_generation: str | None = None


class StyleSetIntroPreview(BaseModel):
    """Display-only `intro`-role styling, consumed by the instant-edit client
    preview (DOM overlay on the base video). Projection-only — never reaches the
    renderer burn dict (see style_sets.style_set_intro_preview)."""

    font_family: str | None = None
    css_family: str | None = None
    font_file: str | None = None
    font_weight: int | None = None
    text_color: str | None = None
    highlight_color: str | None = None
    effect: str | None = None
    position: str | None = None
    position_x_frac: float | None = None
    position_y_frac: float | None = None
    text_anchor: str | None = None
    stroke_width: int | None = None
    text_size_px: int | None = None


class StyleSetSummary(BaseModel):
    id: str
    label: str
    tags: list[str]
    # Display-only typography of the set's representative (hook) role so the picker
    # can render a real-font preview chip BEFORE a re-render. Never reaches the
    # renderer burn dict (see style_sets.style_set_preview — #296 parity invariant).
    font_family: str | None = None
    css_family: str | None = None
    font_file: str | None = None
    font_weight: int | None = None
    text_color: str | None = None
    highlight_color: str | None = None
    effect: str | None = None
    # Full intro-role look for the instant-edit client preview.
    intro: StyleSetIntroPreview | None = None


class StyleSetListResponse(BaseModel):
    style_sets: list[StyleSetSummary]


# ── Helpers ────────────────────────────────────────────────────────────────────


# content_plan jobs reuse the generative render + per-variant assembly_plan shape,
# so they are READ-able via the status endpoint (the plan item page polls it). The
# mutate endpoints (swap-song / retext / change-style) stay generative-only — those
# are generative-UX affordances that don't apply to a plan item.
_READABLE_MODES = ("generative", "content_plan", "manual_draft")
_PLAN_OWNED_READ_MODES = frozenset({"content_plan", "manual_draft"})


async def _load_generative_job(
    job_id: str,
    db: AsyncSession,
    current_user: User,
    *,
    allowed_modes: tuple[str, ...] = ("generative",),
    allow_cancelled: bool = False,
    with_for_update: bool = True,
) -> Job:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    # Resolve the mode without a lock first. Content-plan Jobs must then enter
    # the canonical Plan -> Persona -> PlanItem -> Job lock order; locking Job
    # here first would deadlock with quarantine/remediation.
    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.mode not in allowed_modes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    ensure_job_owner(job.user_id, current_user)

    if job.mode in _PLAN_OWNED_READ_MODES:
        item_id = getattr(job, "content_plan_item_id", None)
        item_ref = await db.get(PlanItem, item_id) if item_id is not None else None
        if item_ref is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )
        plan = await db.get(
            ContentPlan,
            item_ref.content_plan_id,
            **({"populate_existing": True, "with_for_update": True} if with_for_update else {}),
        )
        if plan is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )
        try:
            await load_owned_plan_persona(db, plan, for_update=with_for_update)
        except PlanPersonaOwnershipError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            ) from exc
        if with_for_update:
            item = await db.get(
                PlanItem,
                item_ref.id,
                populate_existing=True,
                with_for_update=True,
            )
            locked_result = await db.execute(
                select(Job)
                .where(Job.id == job_uuid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked_job = locked_result.scalar_one_or_none()
        else:
            item = item_ref
            locked_job = job
        if (
            item is None
            or locked_job is None
            or item.content_plan_id != plan.id
            or item.current_job_id != locked_job.id
            or locked_job.content_plan_item_id != item.id
            or locked_job.user_id != plan.user_id
            or locked_job.mode not in _PLAN_OWNED_READ_MODES
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )
        job = locked_job
    elif with_for_update:
        locked_result = await db.execute(
            select(Job)
            .where(Job.id == job_uuid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = locked_result.scalar_one_or_none()
        if job is None or job.mode not in allowed_modes:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        ensure_job_owner(job.user_id, current_user)

    if job.status == "cancelled" and not allow_cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be edited.",
        )
    return job


def _variants_of(job: Job) -> list[dict]:
    return ((job.assembly_plan or {}).get("variants")) or []


def _lazy_backfill_media_overlay_previews(job: Job) -> bool:
    """Generate missing JPEG previews for legacy HEIC overlay cards.

    Upload-confirm handles new HEIC/HEIF cards, but rows created before that
    feature can have only the original HEIC source. Chromium cannot preview that
    source, so the status read does one best-effort conversion and stamps the
    preview path back onto the variant JSON. Failed conversions are guarded per
    process and per overlay source path to avoid repeating slow reads on every
    poll.
    """
    variants = _variants_of(job)
    if not variants:
        return False

    changed = False
    next_variants: list[dict] = []
    for v in variants:
        if not isinstance(v, dict):
            next_variants.append(v)
            continue
        raw_overlays = v.get("media_overlays")
        if not raw_overlays:
            next_variants.append(v)
            continue

        next_overlays: list[object] = []
        variant_changed = False
        for card in raw_overlays:
            if not isinstance(card, dict):
                next_overlays.append(card)
                continue

            src_gcs_path = nonblank_str(card.get("src_gcs_path"))
            preview_gcs_path = nonblank_str(card.get("preview_gcs_path"))
            if (
                src_gcs_path
                and not preview_gcs_path
                and is_heif_overlay(src_gcs_path)
                and src_gcs_path not in _HEIF_PREVIEW_BACKFILL_ATTEMPTED
            ):
                _HEIF_PREVIEW_BACKFILL_ATTEMPTED.add(src_gcs_path)
                preview_gcs_path, preview_url = convert_heif_overlay_preview(src_gcs_path)
                preview_gcs_path = nonblank_str(preview_gcs_path)
                preview_url = nonblank_str(preview_url)
                if preview_gcs_path:
                    card = {**card, "preview_gcs_path": preview_gcs_path}
                    variant_changed = True
                    log.info(
                        "overlay_heif_preview_lazy_backfilled",
                        job_id=str(job.id),
                        variant_id=v.get("variant_id"),
                        src_gcs_path=src_gcs_path,
                        preview_gcs_path=preview_gcs_path,
                    )
                else:
                    log.error(
                        "overlay_heif_preview_lazy_backfill_failed",
                        job_id=str(job.id),
                        variant_id=v.get("variant_id"),
                        src_gcs_path=src_gcs_path,
                    )
            elif "preview_gcs_path" in card and card.get("preview_gcs_path") != preview_gcs_path:
                card = {**card, "preview_gcs_path": preview_gcs_path}
                variant_changed = True

            next_overlays.append(card)

        if variant_changed:
            v = {**v, "media_overlays": next_overlays}
            changed = True
        next_variants.append(v)

    if changed:
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": next_variants}
    return changed


def _collect_media_overlay_preview_stamps(job: Job) -> dict[str, str]:
    """Map `src_gcs_path -> preview_gcs_path` for cards the lazy backfill just stamped.

    Read straight off the in-memory (post-backfill) job so the stamps can be
    re-applied onto a freshly row-locked row — see
    `_persist_media_overlay_preview_backfill`.
    """
    stamps: dict[str, str] = {}
    for v in _variants_of(job):
        if not isinstance(v, dict):
            continue
        for card in v.get("media_overlays") or []:
            if not isinstance(card, dict):
                continue
            src = nonblank_str(card.get("src_gcs_path"))
            preview = nonblank_str(card.get("preview_gcs_path"))
            if src and preview:
                stamps[src] = preview
    return stamps


async def _persist_media_overlay_preview_backfill(
    db: AsyncSession, job_id: uuid.UUID, preview_by_src: dict[str, str]
) -> None:
    """Persist lazily-backfilled HEIC overlay preview paths under a row lock.

    The status read computes these previews from an UNLOCKED snapshot; committing
    that snapshot's whole `assembly_plan` back would clobber any concurrent worker
    write (a completing render / finalize / SFX pass) that landed in between — the
    lost-update the worker-side `with_for_update` locks now prevent. So we re-fetch
    FOR UPDATE and merge ONLY the preview stamps onto the fresh row (mirrors
    `persist_user_timeline` / the worker's `_update_variant_entry`). The caller must
    have discarded the unlocked snapshot's pending mutation first (db.rollback()).
    """
    if not preview_by_src:
        return
    job = await db.get(Job, job_id, with_for_update=True)
    if job is None or getattr(job, "status", None) == "cancelled":
        return
    variants = list((job.assembly_plan or {}).get("variants") or [])
    changed = False
    next_variants: list[dict] = []
    for v in variants:
        if not isinstance(v, dict) or not v.get("media_overlays"):
            next_variants.append(v)
            continue
        next_cards: list[object] = []
        variant_changed = False
        for card in v.get("media_overlays") or []:
            if isinstance(card, dict):
                src = nonblank_str(card.get("src_gcs_path"))
                if src and not nonblank_str(card.get("preview_gcs_path")) and src in preview_by_src:
                    card = {**card, "preview_gcs_path": preview_by_src[src]}
                    variant_changed = True
            next_cards.append(card)
        if variant_changed:
            v = {**v, "media_overlays": next_cards}
            changed = True
        next_variants.append(v)
    if changed:
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": next_variants}
        await db.commit()


def _variants_for_response(job: Job) -> list[dict]:
    """Variants with `output_url` (and `base_video_url`) re-signed fresh on read.

    The stored `output_url` is a 1-day-TTL signature minted at render time, but the
    blob persists forever (see PLAYBACK_URL_TTL_MIN). Return shallow copies with a
    freshly-signed URL derived from the persisted `video_path` key so playback never
    serves an expired signature. Must NOT mutate the raw variant dicts — the mutate
    endpoints read those via `_variants_of` and we never want a re-signed URL written
    back to the DB. Failed/unrendered variants (no `video_path`) keep their value.

    `base_video_path` (the text-free fast-reburn base) gets the same treatment into
    `base_video_url` — regardless of `render_status`, because the instant editor keeps
    playing the base while a committed re-render is in flight. A signing failure just
    omits the key (the editor degrades to the legacy controls).
    """
    if getattr(job, "status", None) == "cancelled":
        return []

    changed = _lazy_backfill_media_overlay_previews(job)
    if changed:
        setattr(job, "_media_overlay_preview_backfilled", True)

    from app.agents._schemas.sound_effect import normalize_generated_sound_effects  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415
    from app.services.speech_map import build_speech_map  # noqa: PLC0415
    from app.services.transcript_source import (  # noqa: PLC0415
        compute_transcript_hash,
        speech_words_for_variant,
        variant_duration_s,
    )

    out: list[dict] = []
    for v in _variants_of(job):
        video_path = v.get("video_path")
        # Re-sign whenever a rendered video exists — NOT only when "ready". A variant
        # whose re-render FAILED keeps its last good `video_path`, and that video must
        # stay playable past the 24h signature expiry. Only an in-flight re-render
        # ("rendering") keeps the stored URL untouched: the player holds the base/last
        # frame until the poll flips the status.
        if video_path and v.get("render_status") != "rendering":
            try:
                v = {**v, "output_url": signed_get_url(video_path, PLAYBACK_URL_TTL_MIN)}
            except Exception:  # noqa: BLE001 — one bad sign must not 500 the poll
                log.warning(
                    "variant_resign_failed",
                    job_id=str(job.id),
                    variant_id=v.get("variant_id"),
                    video_path=video_path,
                    exc_info=True,
                )
                # fall through with the stored (possibly stale) output_url
            try:
                variant_id = str(v.get("variant_id") or "video")
                v = {
                    **v,
                    "download_url": storage.signed_download_url(
                        video_path,
                        f"kria-{variant_id}.mp4",
                        expiration_minutes=PLAYBACK_URL_TTL_MIN,
                    ),
                }
            except Exception:  # noqa: BLE001 — playback remains usable without attachment URL
                log.warning(
                    "variant_download_sign_failed",
                    job_id=str(job.id),
                    variant_id=v.get("variant_id"),
                    video_path=video_path,
                    exc_info=True,
                )
        poster_path = v.get("poster_path")
        if poster_path:
            try:
                v = {**v, "poster_url": signed_get_url(poster_path, PLAYBACK_URL_TTL_MIN)}
            except Exception:  # noqa: BLE001 — poster is optional
                log.warning(
                    "variant_poster_resign_failed",
                    job_id=str(job.id),
                    variant_id=v.get("variant_id"),
                    poster_path=poster_path,
                    exc_info=True,
                )
        base_video_path = v.get("base_video_path")
        if base_video_path:
            try:
                v = {**v, "base_video_url": signed_get_url(base_video_path, PLAYBACK_URL_TTL_MIN)}
            except Exception:  # noqa: BLE001 — one bad sign must not 500 the poll
                log.warning(
                    "variant_base_resign_failed",
                    job_id=str(job.id),
                    variant_id=v.get("variant_id"),
                    base_video_path=base_video_path,
                    exc_info=True,
                )
                # no base_video_url key → the instant editor simply stays hidden
            base_poster_path = v.get("base_poster_path")
            if base_poster_path:
                try:
                    v = {
                        **v,
                        "base_poster_url": signed_get_url(base_poster_path, PLAYBACK_URL_TTL_MIN),
                    }
                except Exception:  # noqa: BLE001 — poster is optional
                    log.warning(
                        "variant_base_poster_resign_failed",
                        job_id=str(job.id),
                        variant_id=v.get("variant_id"),
                        base_poster_path=base_poster_path,
                        exc_info=True,
                    )
        source_audio_options = v.get("source_audio_options")
        if isinstance(source_audio_options, list):
            signed_audio_options = []
            for option in source_audio_options:
                if not isinstance(option, dict):
                    continue
                audio_path = option.get("audio_path")
                if not audio_path:
                    signed_audio_options.append(option)
                    continue
                try:
                    signed_audio_options.append(
                        {
                            **option,
                            "audio_url": signed_get_url(audio_path, PLAYBACK_URL_TTL_MIN),
                        }
                    )
                except Exception:  # noqa: BLE001 — alternate audio is optional
                    log.warning(
                        "variant_source_audio_resign_failed",
                        job_id=str(job.id),
                        variant_id=v.get("variant_id"),
                        audio_path=audio_path,
                        exc_info=True,
                    )
                    signed_audio_options.append(option)
            v = {**v, "source_audio_options": signed_audio_options}
        # Overlay-clean base (plan 008 live edit): the un-carded video captured
        # before the first overlay burn. When present, the hero can play THIS
        # and render every card as a live CSS layer — timeline edits reflect
        # instantly and the burn waits for Download. Same graceful-skip contract.
        pre_overlay_path = v.get("pre_media_overlay_video_path")
        if pre_overlay_path:
            try:
                v = {
                    **v,
                    "pre_overlay_video_url": signed_get_url(pre_overlay_path, PLAYBACK_URL_TTL_MIN),
                }
            except Exception:  # noqa: BLE001 — one bad sign must not 500 the poll
                log.warning(
                    "variant_pre_overlay_resign_failed",
                    job_id=str(job.id),
                    variant_id=v.get("variant_id"),
                    pre_overlay_path=pre_overlay_path,
                    exc_info=True,
                )
                # no pre_overlay_video_url → live-edit mode stays off (baked playback)
        pre_overlay_poster_path = v.get("pre_overlay_poster_path")
        if pre_overlay_poster_path:
            try:
                v = {
                    **v,
                    "pre_overlay_poster_url": signed_get_url(
                        pre_overlay_poster_path, PLAYBACK_URL_TTL_MIN
                    ),
                }
            except Exception:  # noqa: BLE001 — poster is optional
                log.warning(
                    "variant_pre_overlay_poster_resign_failed",
                    job_id=str(job.id),
                    variant_id=v.get("variant_id"),
                    pre_overlay_poster_path=pre_overlay_poster_path,
                    exc_info=True,
                )
        # Media-overlay cards: sign each card's src_gcs_path into a preview_url so
        # the browser can show existing applied cards as a live CSS overlay without
        # re-uploading them. Signing failure skips the key on that card (graceful).
        raw_overlays = v.get("media_overlays")
        if raw_overlays:
            signed_overlays = []
            for card in raw_overlays:
                if not isinstance(card, dict):
                    signed_overlays.append(card)
                    continue
                src = nonblank_str(card.get("preview_gcs_path")) or nonblank_str(
                    card.get("src_gcs_path")
                )
                if src:
                    try:
                        signed_overlays.append(
                            {**card, "preview_url": signed_get_url(src, PLAYBACK_URL_TTL_MIN)}
                        )
                    except Exception:  # noqa: BLE001
                        signed_overlays.append(card)
                else:
                    signed_overlays.append(card)
            v = {**v, "media_overlays": signed_overlays}
        raw_visual_blocks = v.get("visual_blocks")
        if raw_visual_blocks:
            signed_visual_previews: dict[str, str] = {}
            for block in raw_visual_blocks:
                if not isinstance(block, dict) or block.get("kind") != "media":
                    continue
                block_id = nonblank_str(block.get("id"))
                src = nonblank_str(block.get("src_gcs_path"))
                preview = nonblank_str(block.get("preview_gcs_path"))
                # Authoritative pool/legacy previews are sibling JPEGs derived
                # from the owned source key. The shape check also protects any
                # pre-release rows created before commit-time canonicalization.
                trusted_preview = (
                    preview
                    if src
                    and preview
                    and preview.startswith(f"{src}.preview")
                    and preview.endswith(".jpg")
                    else None
                )
                src = trusted_preview or src
                if block_id and src:
                    try:
                        signed_visual_previews[block_id] = signed_get_url(src, PLAYBACK_URL_TTL_MIN)
                    except Exception:  # noqa: BLE001 — one bad sign must not 500 the poll
                        log.warning(
                            "variant_visual_block_preview_resign_failed",
                            job_id=str(job.id),
                            variant_id=v.get("variant_id"),
                            visual_block_id=block_id,
                            exc_info=True,
                        )
            if signed_visual_previews:
                v = {**v, "visual_block_preview_urls": signed_visual_previews}
        raw_sound_effects = v.get("sound_effects")
        if raw_sound_effects:
            v = {
                **v,
                "sound_effects": normalize_generated_sound_effects(raw_sound_effects),
            }
        # Intro mode (D19): expose the authoritative mode plus the FE-convenience
        # `sequence_synced` boolean. Legacy variants (pre-intro_mode) fall back to
        # the persisted intro_layout — they can never be "sequence".
        intro_mode = v.get("intro_mode") or v.get("intro_layout") or None
        v = {**v, "intro_mode": intro_mode, "sequence_synced": intro_mode == "sequence"}
        # Speech map (word/pause timing for the editor + copilot): derived from
        # the un-stripped variant BEFORE the transcript pop below. Only for
        # variants with a rendered video that isn't mid-re-render — the words
        # describe the persisted render's timeline. Pure arithmetic (≤150 words),
        # safe on every poll. None → key absent → the copilot honestly reports
        # no speech data for this variant.
        speech_map = None
        speech_src = None
        # getattr: real Job rows always carry the column; test doubles may not.
        if (
            getattr(job, "content_plan_item_id", None) is not None  # plan-item editor only
            and v.get("video_path")
            and v.get("render_status") != "rendering"
        ):
            speech_src = speech_words_for_variant(v)
            if speech_src is not None:
                speech_map = build_speech_map(speech_src[0], variant_duration_s(v), speech_src[1])
        if speech_map is not None:
            v = {**v, "speech_map": speech_map}
        # Pending SFX suggestions (sfx_autoplace, dark-flagged): advisory-only,
        # stale-filtered — a suggestion minted against different words/duration
        # (clip re-cuts, re-transcription) is dropped, not served.
        raw_sfx_suggestions = v.get("pending_sfx_suggestions")
        if raw_sfx_suggestions and not settings.sfx_autoplace_enabled:
            # Kill switch stops exposure too, not just dispatch.
            v = {**v, "pending_sfx_suggestions": None}
        elif raw_sfx_suggestions:
            if speech_map is not None and speech_src is not None:
                current_hash = compute_transcript_hash(speech_src[0], variant_duration_s(v))
                fresh_suggestions = [
                    s
                    for s in raw_sfx_suggestions
                    if isinstance(s, dict) and s.get("transcript_hash") == current_hash
                ]
                v = {**v, "pending_sfx_suggestions": fresh_suggestions}
            else:
                # Freshness unverifiable right now (mid-re-render / source
                # momentarily absent): serve null, NOT [] — the FE must be able
                # to tell "verified, none fresh" from "unknown, hold state".
                v = {**v, "pending_sfx_suggestions": None}
        # Drop server-only sequence internals from the polled payload: the full
        # per-word `transcript` and parallel `scenes` are read by the reburn path
        # from the persisted Job row, never by the FE. Returning them on every
        # status poll is wasted bandwidth and needless exposure of the footage
        # transcript to the client. (`v` is already a fresh copy here.)
        v.pop("transcript", None)
        raw_scenes = v.pop("scenes", None) or []
        v["scene_timings"] = [
            {
                "text": s.get("text")
                or " ".join(str(word) for word in (s.get("words") or []) if word)
                or "",
                "start_s": s.get("start_s"),
                "end_s": s.get("end_s"),
            }
            for s in raw_scenes
            if s.get("start_s") is not None and s.get("end_s") is not None
        ]
        v = {**v, "render_generation_id": v.get("render_generation_id")}
        # TextElement overlay (plan-item-timeline feature).  Surfaced when the
        # kill switch is on so the FE can populate its timeline editor from the
        # persisted state (both the AI-snapshot and user-authored lists).
        if _TEXT_ELEMENTS_ENABLED:
            v = {
                **v,
                "text_elements": merge_projected_text_elements_for_variant(
                    v, include_lyric_projection=_LYRICS_EDITOR_ENABLED
                ),
                "text_elements_user_edited": v.get("text_elements_user_edited", False),
                "geometry_materialized_at_version": v.get("geometry_materialized_at_version"),
                "text_elements_materialized_from": v.get("text_elements_materialized_from"),
            }
        if _LYRICS_EDITOR_ENABLED:
            v = {**v, "lyrics_enabled": _variant_lyrics_enabled(v)}
        v = {**v, "orientation": _variant_orientation(v)}
        # E4: per-variant editor capabilities — one server-side truth source for
        # which editor surfaces the FE may enable (no endpoint probing).
        from app.pipeline.speech_cut_state import (  # noqa: PLC0415
            cut_revision,
            public_candidates,
        )

        v = {
            **v,
            "speech_cut_candidates": public_candidates(v),
            "speech_cut_revision": cut_revision(v),
            "editor_capabilities": _editor_capabilities(job, v),
        }
        out.append(v)
    return out


def _find_variant(job: Job, variant_id: str) -> dict | None:
    return next((v for v in _variants_of(job) if v.get("variant_id") == variant_id), None)


@dataclass(frozen=True, slots=True)
class PendingVariantPublish:
    """A committed render attempt whose broker publication is still pending.

    Route handlers commit the row-locked mutation before touching Celery so eager
    workers cannot deadlock on the same Job.  The receipt keeps enough exact state
    to roll back only that newly-minted attempt if publication fails.  It remains
    callable for the content-plan dispatchers that still use the legacy immediate
    publication path.
    """

    callback: Callable[[], None]
    job_id: uuid.UUID
    variant_id: str
    render_generation_id: str
    previous_variant: dict[str, Any]
    rollback_fields: frozenset[str]
    previous_started_at: datetime | None
    attempted_started_at: datetime | None

    def __call__(self) -> None:
        self.callback()


def _pending_variant_publish(
    job: Job,
    variant_id: str,
    *,
    callback: Callable[[], None],
    render_generation_id: str,
    previous_variant: dict[str, Any],
    previous_started_at: datetime | None,
) -> PendingVariantPublish:
    current = _find_variant(job, variant_id) or {}
    changed = frozenset(
        key
        for key in set(previous_variant) | set(current)
        if previous_variant.get(key, _UNSET) != current.get(key, _UNSET)
    )
    return PendingVariantPublish(
        callback=callback,
        job_id=job.id,
        variant_id=variant_id,
        render_generation_id=render_generation_id,
        previous_variant=copy.deepcopy(previous_variant),
        rollback_fields=changed,
        previous_started_at=previous_started_at,
        attempted_started_at=getattr(job, "started_at", None),
    )


async def _publish_committed_variant_render(
    receipt: PendingVariantPublish, db: AsyncSession
) -> None:
    """Publish after commit, restoring only this attempt on broker failure.

    A fresh ``FOR UPDATE`` read linearizes recovery with cancellation and newer
    edits.  A cancelled tombstone or a superseding generation is never mutated.
    Unrelated fields written after the dispatch commit are retained because only
    fields changed by this exact attempt are restored.
    """

    try:
        receipt()
        return
    except Exception as publish_exc:
        restored = False
        try:
            stmt = (
                select(Job)
                .where(Job.id == receipt.job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            result = await db.execute(stmt)
            locked_job = result.scalar_one_or_none()
            if locked_job is not None and locked_job.status != "cancelled":
                variants = list((locked_job.assembly_plan or {}).get("variants") or [])
                for index, current in enumerate(variants):
                    if current.get("variant_id") != receipt.variant_id:
                        continue
                    if current.get("render_generation_id") != receipt.render_generation_id:
                        break
                    restored_variant = copy.deepcopy(current)
                    for field in receipt.rollback_fields:
                        if field in receipt.previous_variant:
                            restored_variant[field] = copy.deepcopy(receipt.previous_variant[field])
                        else:
                            restored_variant.pop(field, None)
                    variants[index] = restored_variant
                    locked_job.assembly_plan = {
                        **(locked_job.assembly_plan or {}),
                        "variants": variants,
                    }
                    # Do not rewind a newer sibling attempt's clock.
                    if locked_job.started_at == receipt.attempted_started_at:
                        locked_job.started_at = receipt.previous_started_at
                    await db.commit()
                    restored = True
                    break
            if not restored:
                await db.rollback()
        except Exception as recovery_exc:
            await db.rollback()
            log.error(
                "variant_render_publish_recovery_failed",
                job_id=str(receipt.job_id),
                variant_id=receipt.variant_id,
                render_generation_id=receipt.render_generation_id,
                publish_error=str(publish_exc),
                recovery_error=str(recovery_exc),
            )
        log.error(
            "variant_render_publish_failed",
            job_id=str(receipt.job_id),
            variant_id=receipt.variant_id,
            render_generation_id=receipt.render_generation_id,
            restored=restored,
            error=str(publish_exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The render queue is temporarily unavailable. Please try again.",
        ) from publish_exc


# ── Shared variant-edit validation + dispatch ───────────────────────────────────
# These are public (no leading underscore) so the content-plan routes
# (`routes/plan_items.py`) can reuse them verbatim across modules — content_plan
# jobs share the generative per-variant assembly_plan shape, so the validation
# rules and the `regenerate_generative_variant` dispatch are identical. The only
# difference between the two surfaces is how the Job is loaded (public job-id vs
# ownership-checked plan item), so that stays in each route; everything below the
# loaded Job is single-sourced here.


_GUIDED_STORY_EDIT_ERROR = {
    "code": "guided_story_edit_unsupported",
    "message": "Change this in Plan edit, approve the updated story, then generate again.",
}
_GUIDED_STORY_TEXT_REQUIRED_ERROR = {
    "code": "guided_story_text_required",
    "message": "Keep the approved title and thought moments; edit their wording instead.",
}


def require_editable_variant(job: Job, variant_id: str, *, allow_guided_text: bool = False) -> dict:
    """Return the variant; 404 if unknown, 409 if it's already re-rendering."""
    if getattr(job, "status", None) == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be edited.",
        )
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    if variant.get("render_status") == "rendering" or variant.get("speech_cut_in_flight"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Variant is already re-rendering."
        )
    if variant.get("resolved_archetype") == "guided_story" and not allow_guided_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_GUIDED_STORY_EDIT_ERROR,
        )
    return variant


def _require_guided_story_text_ids(variant: dict, elements: list[dict]) -> None:
    if variant.get("resolved_archetype") != "guided_story":
        return
    receipt = variant.get("render_receipt") or {}
    required = list(receipt.get("approved_text_ids") or receipt.get("expected_text_ids") or [])
    try:
        from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

        parsed = [TextElement.model_validate(element) for element in elements]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_GUIDED_STORY_TEXT_REQUIRED_ERROR,
        ) from exc
    submitted = [element.id for element in parsed if element.text.strip()]
    if not required or any(submitted.count(required_id) != 1 for required_id in required):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_GUIDED_STORY_TEXT_REQUIRED_ERROR,
        )


async def dispatch_swap_song(
    job: Job, variant_id: str, *, new_track_id: str, db: AsyncSession
) -> None:
    """Validate + enqueue a song swap for one variant (async re-slot)."""
    variant = require_editable_variant(job, variant_id)
    # Swapping a song only makes sense on a song variant. The original-audio variant
    # has no track; converting it to a song variant would silently change its identity.
    if variant.get("variant_id") == "original_text" or variant.get("music_track_id") is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This is the original-audio edit — it has no song to swap.",
        )
    # The new track must exist and be ready (published not required — swap is a
    # deliberate user pick from the gallery, mirroring admin test-job semantics).
    track = (
        await db.execute(select(MusicTrack).where(MusicTrack.id == new_track_id))
    ).scalar_one_or_none()
    if track is None or track.analysis_status != "ready" or not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Requested song is not available for rendering.",
        )
    selecting_existing_unpublished = str(track.id) == str(variant.get("music_track_id") or "")
    if not selecting_existing_unpublished and (
        track.published_at is None or track.archived_at is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "music_track_unavailable"},
        )

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            v["music_track_id"] = new_track_id
            v["base_video_stale"] = True
            v["music_start_s"] = round(
                _recommended_music_start(track, visual_block_variant_duration(variant)), 3
            )
            v.pop("music_window_video_duration_s", None)
            v["lyric_line_overrides"] = None
            v["lyric_overlay_snapshot"] = None
            v["text_elements"] = [
                element
                for element in (v.get("text_elements") or [])
                if not isinstance(element, dict) or element.get("role") != "lyric_line"
            ]
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)
    # Make the generation visible before enqueue. A slower legacy swap can
    # then be rejected if a newer atomic editor commit supersedes it.
    await db.commit()

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(
        str(job.id),
        variant_id,
        new_track_id=new_track_id,
        render_gen_id=render_gen_id,
    )


# Mode-neutral: a sequence variant is either transcript-synced (voiceover) OR
# rhythm-mode (an authored quote over music, no voiceover) — the copy must not
# claim a voiceover that rhythm variants don't have.
_SEQUENCE_TEXT_LOCKED_DETAIL = (
    "Text is synced for this Editorial variant — switch layout to Classic to edit text."
)


def dispatch_retext(
    job: Job,
    variant_id: str,
    *,
    text: str | None,
    remove: bool,
    publish: bool = True,
) -> PendingVariantPublish:
    """Validate + enqueue an intro-text edit/removal for one variant.

    T8: the sequence lock that used to 422 here is removed.  PUT /text-elements
    handles multi-block editorial layout edits; dispatch_retext now proceeds for
    all variant types including sequence (intro_text override on re-render).
    """
    # Guard: raises 404/409 when variant is unknown or already rendering.
    variant = require_editable_variant(job, variant_id)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)
    if not remove and not (text and text.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide `text` to update, or set `remove=true` to clear the overlay.",
        )

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    def _publish() -> None:
        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            override_text=(text.strip() if (text and not remove) else None),
            remove_text=bool(remove),
            render_gen_id=render_gen_id,
        )

    receipt = _pending_variant_publish(
        job,
        variant_id,
        callback=_publish,
        render_generation_id=render_gen_id,
        previous_variant=previous_variant,
        previous_started_at=previous_started_at,
    )
    if publish:
        receipt()
    return receipt


# A caption edit may never bloat the JSONB / slow the libass burn unbounded.
# The sibling timeline editor has its own shared slot cap; narration rarely
# exceeds a few dozen lines.
_MAX_CAPTION_CUES = 300


class CaptionWord(BaseModel):
    # Same inf/nan rejection as CaptionCue — a poisoned word time crashes the reburn.
    model_config = {"allow_inf_nan": False}

    text: str
    start_s: float
    end_s: float
    # Whisper-alignment confidence stamped by the Smart chunker (mirrors
    # smart_edit.schemas.SmartWord.timing_quality — keep the literals in sync).
    # Round-tripped provenance only; absent on plain narrated/subtitled cues.
    timing_quality: Literal["aligned", "segment_estimate", "unsafe"] | None = None


# Mirrors app/smart_edit/schemas._WORD_ID_RE ("w000001") — keep in sync.
_SMART_WORD_ID_RE = re.compile(r"^w\d{6}$")


class CaptionCue(BaseModel):
    # Reject NaN/±Infinity at the edge — format_ass_time(inf) crashes the reburn
    # worker and leaves the cue poisoned (every Apply then fails).
    model_config = {"allow_inf_nan": False}

    # Length-capped: the word-pop burn emits one Dialogue event PER TOKEN, each
    # carrying the full line (O(tokens²) chars per cue) — an unbounded text field
    # would let one captions PATCH build a multi-GB ASS on the worker. Cues are
    # ≤ ~14 words by construction, so 600 chars is generous.
    text: str = Field(max_length=600)
    start_s: float
    end_s: float
    # Optional per-word timings for the word-by-word subtitled style. Carried so a
    # reburn re-pops the SAME words at their real (audio-locked) times; when the user
    # edits a cue its stored words no longer spell the text and the burn re-synthesizes
    # them (E3). None for sentence-style captions. Bounded: cues are ≤ ~14 words by
    # construction (build_plain_cues), so a generous cap keeps the debounced PATCH from
    # becoming an unbounded JSONB write surface.
    words: list[CaptionWord] | None = None
    # Server-authored by default, but CLIENT-EDITABLE (plan Workstream 4b): the
    # editor's per-cue "Emphasize" toggle sets/clears this alongside
    # smart_emphasis below, through this SAME PATCH — no dedicated endpoint.
    # The Literal closes the value set (persist_variant_captions rejects
    # anything outside it with a 422); an unchanged cue round-trips whatever
    # the server originally authored.
    smart_style: Literal["hook", "context", "list_item", "example", "payoff", "cta"] | None = None
    # Smart Captions v2 provenance. The captions PATCH replaces the ENTIRE cue
    # list, so anything not whitelisted here is stripped from ALL cues on the
    # first text edit. Read-only from the editor's perspective — the chunker
    # assigns it and it is never client-set (distinct from smart_style above,
    # which the Emphasize toggle DOES set). Not read at render time
    # (smart_style is the ASS styling carrier) but preserved for the planner
    # lineage plan 011 extends. Uses the smart_edit SemanticRole vocabulary —
    # distinct from smart_style's ("context_shift" vs "context").
    # The derived smart_render_* caches are deliberately NOT round-tripped:
    # generate_ass_from_cues recomputes them from text + the pinned policy at
    # every burn, so client-sent values would be dead weight.
    smart_role: SemanticRole | None = None
    smart_word_ids: list[str] | None = None
    # Plan 011/012 provenance the whitelist above was written to be extended with:
    # smart_emphasis marks a cue that was isolated as a named-entity moment (drives
    # the standalone min-hold at plan time) — CLIENT-EDITABLE alongside
    # smart_style above (plan Workstream 4b's Emphasize toggle sets/clears both
    # together). smart_keep_together holds the line-layout adjacency pairs the
    # reburn honors and stays server-only/round-tripped untouched.
    # Both must survive a caption text edit or the emphasis/layout look is silently
    # lost on the FIRST edit (same trap #699 closed for smart_role/smart_word_ids).
    smart_emphasis: bool | None = None
    smart_keep_together: list[list[int]] | None = None
    # Lane PR-A — per-cue style overrides ("This caption" section of the editor,
    # distinct from the variant-level "All captions" globals below on the
    # variant response: voiceover_caption_font/caption_size_px/caption_text_color/
    # etc.). None/absent on every field ⇒ the cue inherits the variant defaults,
    # unchanged from pre-feature behavior — `generate_ass_from_cues` treats an
    # all-None cue as byte-identical to today (no override tags emitted).
    # font_family reuses the SAME registry-key contract + validation as the
    # variant-level `voiceover_caption_font` (see `is_valid_caption_font` /
    # `resolve_caption_font` in narrated_assembler.py) — not the Skia
    # TextElement font allowlist, which is a different renderer.
    font_family: str | None = None
    text_color: str | None = None
    size_px: int | None = Field(None, ge=36, le=160)

    @field_validator("font_family")
    @classmethod
    def _validate_cue_font_family(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from app.pipeline.narrated_assembler import is_valid_caption_font  # noqa: PLC0415

        if not is_valid_caption_font(v):
            raise ValueError("Unknown caption font.")
        return v

    @field_validator("text_color")
    @classmethod
    def _validate_cue_text_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        clean = v.strip()
        if not _HEX_COLOR_RE.match(clean):
            raise ValueError("Caption text_color must be a #RRGGBB hex color.")
        return clean.upper()

    @field_validator("words")
    @classmethod
    def _cap_words(cls, v: list[CaptionWord] | None) -> list[CaptionWord] | None:
        if v is not None and len(v) > 100:
            raise ValueError("Too many words on one caption line (max 100).")
        return v

    @field_validator("smart_word_ids")
    @classmethod
    def _cap_smart_word_ids(cls, v: list[str] | None) -> list[str] | None:
        # User input on the PATCH edge — same cap as `words` (one id per word)
        # and the closed w000001 format, so a forged PATCH can't stuff the JSONB.
        if v is None:
            return v
        if len(v) > 100:
            raise ValueError("Too many word ids on one caption line (max 100).")
        if any(not _SMART_WORD_ID_RE.fullmatch(str(word_id)) for word_id in v):
            raise ValueError("Invalid smart caption word id.")
        return v

    @field_validator("smart_keep_together")
    @classmethod
    def _validate_keep_together(cls, v: list[list[int]] | None) -> list[list[int]] | None:
        # Each pair is [start, end] cue-relative word offsets (0-based, start <= end).
        # Bounded like `words` so a forged PATCH can't stuff arbitrary JSONB, and
        # malformed pairs are rejected rather than silently poisoning the reburn.
        if v is None:
            return v
        if len(v) > 100:
            raise ValueError("Too many keep-together pairs on one caption line (max 100).")
        for pair in v:
            if len(pair) != 2 or not all(isinstance(n, int) for n in pair):
                raise ValueError("Each keep-together entry must be a [start, end] pair.")
            start, end = pair
            if not (0 <= start <= end < 100):
                raise ValueError("Keep-together offsets out of range.")
        return v


class CaptionsRequest(BaseModel):
    """Edited narrated caption cues (assembled-time), the on-video editor's payload."""

    cues: list[CaptionCue]

    @field_validator("cues")
    @classmethod
    def _cap_cues(cls, v: list[CaptionCue]) -> list[CaptionCue]:
        if len(v) > _MAX_CAPTION_CUES:
            raise ValueError(f"Too many caption lines (max {_MAX_CAPTION_CUES}).")
        return v


class CaptionFontRequest(BaseModel):
    """Caption font choice for a narrated variant. ``None`` resets to the default."""

    caption_font: str | None = None


class CustomEffectRequest(BaseModel):
    """Raw EffectSpec dict for `apply_custom_effect` (PR6, effect-language
    train). Never trusted at this shape — `dispatch_apply_custom_effect`
    below runs it through `validate_effect_spec` before anything happens,
    and the execution task validates it again independently at render time.
    """

    effect: dict[str, Any]


class CaptionPositionRequest(BaseModel):
    """Caption vertical position as a normalized y coordinate from the top."""

    y_frac: float = Field(ge=0.30, le=0.90)

    @property
    def caption_margin_v(self) -> int:
        from app.pipeline.captions import y_frac_to_margin_v  # noqa: PLC0415

        return y_frac_to_margin_v(self.y_frac)


class CaptionStyleRequest(BaseModel):
    """Sentence/word caption style for a caption variant."""

    caption_style: Literal["sentence", "word"]


class CaptionsEnabledRequest(BaseModel):
    """Subtitles on/off toggle for a caption variant, independent of cue count."""

    enabled: bool


class BedLevelRequest(BaseModel):
    """Background-sound (voice/bed) level for a narrated variant (0 = voice only,
    1 = loudest original audio)."""

    bed_level: float = Field(ge=0.0, le=1.0)


# Languages the subtitled caption override accepts. Lockstep with the worker's
# `_SUBTITLED_CAPTION_LANGUAGES`.
_SUBTITLED_CAPTION_LANGUAGES = frozenset({"en", "tr"})


class CaptionLanguageRequest(BaseModel):
    """New caption language for a subtitled variant (D5 override). Triggers a
    re-transcription in that language, REPLACING the current cues + any edits."""

    language: Literal["en", "tr"]


# Archetypes whose caption cues are editable + reburnable. Keep in LOCKSTEP with the
# worker's `_CAPTION_REBURN_ARCHETYPES` (generative_build) — the route gate and the
# reburn guard must accept exactly the same archetypes or an edit 200s here then 500s
# in the worker.
CAPTION_EDIT_ARCHETYPES = frozenset({"narrated", "subtitled"})
# Compat alias — import the public name; kept so pre-rename importers don't break.
_CAPTION_EDIT_ARCHETYPES = CAPTION_EDIT_ARCHETYPES

# Single copy for every caption-archetype capability reason / 422 detail.
# EditorShell string-compares this exact copy (CAPTIONS_TAB_REASON in
# editor-capabilities.ts) for disabled-state copy — byte-stable
# contract; never reword without updating the frontend constant in lockstep.
CAPTION_TAB_COPY = "Captions can be selected and edited in this editor"


def _text_elements_allowed(variant: dict) -> bool:
    """Text/Styles elements are editable on this variant (caption-archetype rule).

    Single source for BOTH `_editor_capabilities`' text_elements derivation and
    `prepare_editor_commit`'s 422 guard (OV-1): subtitled captions own the
    on-video text; narrated keeps text_elements (its captions ride a separate
    voiceover lane). PR #625's styled-text lane re-opens subtitled text behind
    SUBTITLED_TEXT_LANE_ENABLED — folding the flag HERE keeps the capability map
    and the commit 422 guard in lockstep. Lyrics/flag gating lives in
    `validate_text_elements_payload`.
    """
    if variant.get("resolved_archetype") != "subtitled":
        return True
    from app.config import settings  # noqa: PLC0415

    return settings.subtitled_text_lane_enabled


def _variant_lyrics_enabled(variant: dict) -> bool:
    persisted = variant.get("lyrics_enabled")
    return persisted if isinstance(persisted, bool) else variant.get("text_mode") == "lyrics"


def _variant_lyrics_capable(variant: dict) -> bool:
    return (
        variant.get("text_mode") == "lyrics"
        or variant.get("lyrics_available") is True
        or bool(variant.get("lyric_overlay_snapshot"))
    )


def _lyrics_capabilities(variant: dict) -> dict:
    from app.config import settings  # noqa: PLC0415

    enabled = _variant_lyrics_enabled(variant)
    elements_model = variant.get("lyrics_baked") is False
    optional_song_text = elements_model and variant.get("variant_id") == "song_text"
    editor_enabled = bool(
        _LYRICS_EDITOR_ENABLED or (settings.lyrics_optional_enabled and optional_song_text)
    )
    if not editor_enabled:
        reason = "disabled"
    elif not variant.get("music_track_id"):
        reason = "no_track"
    elif variant.get("lyrics_available") is not True:
        reason = "no_renderable_lyrics"
    else:
        reason = None
    return {
        "editable": bool(editor_enabled and enabled and reason is None),
        "enabled": enabled,
        "can_toggle_on": bool(
            editor_enabled
            and (variant.get("text_mode") == "lyrics" or variant.get("lyrics_available") is True)
            and reason is None
        ),
        "reason": reason,
        # "elements" = lyrics-as-optional-elements (LYRICS_OPTIONAL_ENABLED render;
        # lines are ordinary editable role=lyric_line TextElements, materialized
        # via GET .../lyric-seeds). "baked" = every other variant (legacy renders,
        # and any render made while the flag was off) — lyrics are permanently
        # burned into pixels and the lyric_line projection stays read-only.
        "lyrics_model": "elements" if elements_model else "baked",
    }


def _is_editable_caption_variant(variant: dict) -> bool:
    """True iff this variant is an editable caption variant.

    Gates the caption endpoints. `base_video_path` alone is NOT sufficient — the
    agent_text montage fast-reburn base sets it too; burning captions over a
    montage's text-free base would destroy that variant. Only caption-capable
    archetypes (narrated voiceover, subtitled single-clip) ship `caption_cues`, so
    require one of those.
    """
    return variant.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES and bool(
        variant.get("base_video_path")
    )


async def _patch_narrated_variant(
    job_id: uuid.UUID, variant_id: str, mutation: dict, db: AsyncSession
) -> None:
    """Row-locked read-modify-write of one narrated variant's `assembly_plan` entry.

    The single lock + guard ladder (404 no-render / 404 unknown-variant / 422
    not-narrated / 409 rendering) shared by every narrated-variant editor PATCH, so
    they can't drift on which states they accept. ``mutation`` is shallow-merged onto
    the target variant. No re-render — Apply reburns later. Matches the worker's
    `_update_variant_entry` locking so a concurrent reburn can't clobber the edit.
    """
    result = await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    if getattr(job, "status", None) == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be edited.",
        )
    plan = dict(job.assembly_plan or {})
    variants = list(plan.get("variants") or [])
    target = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    if not _is_editable_caption_variant(target):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Captions can only be edited on captioned variants.",
        )
    if target.get("render_status") == "rendering":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Captions are being applied — try again once the render finishes.",
        )
    for i, v in enumerate(variants):
        if v.get("variant_id") == variant_id:
            variants[i] = {**v, **mutation}
            break
    plan["variants"] = variants
    job.assembly_plan = plan
    await db.commit()


async def persist_variant_captions(
    job_id: uuid.UUID, variant_id: str, cues: list[CaptionCue], db: AsyncSession
) -> None:
    """Persist hand-edited caption cues on a caption variant. No re-render — the edit
    is instant (the player overlays the cues); Apply reburns them later.

    ``exclude_none`` drops the optional ``words`` when absent so sentence/narrated cues
    stay byte-identical; word-by-word cues keep their per-word timings for the reburn.
    """
    await _patch_narrated_variant(
        job_id,
        variant_id,
        {"caption_cues": [c.model_dump(exclude_none=True) for c in cues]},
        db,
    )


async def persist_variant_caption_font(
    job_id: uuid.UUID, variant_id: str, caption_font: str | None, db: AsyncSession
) -> None:
    """Persist the chosen caption font on a narrated variant. No re-render — the
    on-video editor previews it locally; Apply reburns in the chosen font.

    Validates the font against the registry (only known, non-deprecated fonts; or
    None to reset to the default) so unknown input can never reach the ASS Fontname.
    """
    from app.pipeline.narrated_assembler import is_valid_caption_font  # noqa: PLC0415

    if not is_valid_caption_font(caption_font):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown caption font.",
        )
    await _patch_narrated_variant(
        job_id,
        variant_id,
        {
            "voiceover_caption_font": caption_font,
            "caption_font_user_edited": True,
        },
        db,
    )


async def dispatch_set_caption_position(
    job_id: uuid.UUID, variant_id: str, *, y_frac: float, db: AsyncSession
) -> int:
    """Persist caption position and enqueue the caption reburn (one locked write).

    Same discipline as `dispatch_apply_captions` (plan 010 R1-1): row-locked
    fetch, margin merge + gen mint in ONE JSONB rewrite, COMMIT BEFORE the
    enqueue — the reburn's start write is token-checked, so an enqueue that
    outruns the commit would strand the variant in "rendering" forever.
    """
    req = CaptionPositionRequest(y_frac=y_frac)
    result = await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if not _is_editable_caption_variant(variant):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Captions can only be edited on captioned variants.",
        )
    plan = dict(job.assembly_plan or {})
    variants = list(plan.get("variants") or [])
    for i, v in enumerate(variants):
        if v.get("variant_id") == variant_id:
            variants[i] = {
                **v,
                "caption_margin_v": req.caption_margin_v,
                "caption_position_user_edited": True,
            }
            break
    plan["variants"] = variants
    job.assembly_plan = plan
    render_gen_id = _mark_variant_rendering(job, variant_id)
    await db.commit()
    from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

    # Caption tasks inline-run the overlay/SFX reapply passes, which the solo
    # overlay-jobs worker exists to serialize (macOS prefork CLIP fork hazard).
    reburn_narrated_captions.apply_async(
        args=[str(job_id), variant_id],
        kwargs={"render_gen_id": render_gen_id},
        queue="overlay-jobs",
    )
    return req.caption_margin_v


_CAPTION_STYLES = frozenset({"sentence", "word"})


async def persist_variant_caption_style(
    job_id: uuid.UUID, variant_id: str, caption_style: str, db: AsyncSession
) -> None:
    """Persist sentence/word caption style on a caption variant. No re-render — the
    editor previews the choice; Apply reburns in the chosen style."""
    if caption_style not in _CAPTION_STYLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown caption style.",
        )
    await _patch_narrated_variant(
        job_id, variant_id, {"voiceover_caption_style": caption_style}, db
    )


async def persist_variant_captions_enabled(
    job_id: uuid.UUID, variant_id: str, enabled: bool, db: AsyncSession
) -> None:
    """Persist the subtitles on/off toggle, independent of stored cue count.

    Never destroys `caption_cues` — off always yields the caption-free burn on
    Apply regardless of cue count; on reburns the ORIGINAL cues with no
    re-transcription. See `_burn_persisted_captions_onto_base`'s gate.
    """
    await _patch_narrated_variant(job_id, variant_id, {"captions_enabled": bool(enabled)}, db)


def _mark_variant_rendering(job: Job, variant_id: str) -> str:
    """Persist render_status="rendering" synchronously at dispatch (the swap-song
    pattern) so the 409 gate closes IMMEDIATELY — without it, two dispatches in the
    enqueue→dequeue window both pass the gate and race to a last-writer-wins state
    (e.g. a reburn of old cues landing after a re-transcribe).

    Also mints and stamps a fresh `render_generation_id` (2A/OV-3, plan 010) so
    every caption re-render joins the editor-commit supersession model — pass the
    returned token to the task as `render_gen_id` so a superseded run discards
    its terminal write (and its old-blob deletes, OV-4).

    Gen-id minting stays HERE and is never folded into `stamp_variant_attempt`:
    `_update_variant_entry` discards any worker write whose expected token differs
    from the stored one, so minting a token on a dispatch path whose task does not
    carry it would strand the variant in "rendering" forever.
    """
    render_gen_id = uuid.uuid4().hex
    render_enqueued_at = datetime.utcnow().isoformat() + "Z"
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            v["render_enqueued_at"] = render_enqueued_at
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)
    return render_gen_id


async def dispatch_apply_captions(job_id: uuid.UUID, variant_id: str, *, db: AsyncSession) -> None:
    """Reburn the variant's (persisted, hand-edited) caption cues onto its
    caption-free base — the Apply step of the on-video caption editor.

    Row-locked re-fetch + COMMIT BEFORE the enqueue (mirrors
    `dispatch_set_narrated_bed_level`): the reburn's start write is token-checked
    against the just-minted `render_generation_id`, so a worker that dequeues
    before the commit would read the OLD gen, discard its start write, and
    strand the variant in "rendering" forever.
    """
    result = await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if not _is_editable_caption_variant(variant):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Captions can only be applied on captioned variants.",
        )
    render_gen_id = _mark_variant_rendering(job, variant_id)
    await db.commit()
    from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

    # Caption tasks inline-run the overlay/SFX reapply passes, which the solo
    # overlay-jobs worker exists to serialize (macOS prefork CLIP fork hazard).
    reburn_narrated_captions.apply_async(
        args=[str(job_id), variant_id],
        kwargs={"render_gen_id": render_gen_id},
        queue="overlay-jobs",
    )


async def dispatch_retranscribe_captions(
    job_id: uuid.UUID, variant_id: str, *, language: str, db: AsyncSession
) -> None:
    """Re-transcribe a subtitled variant's own audio in a new language and reburn (D5
    override). Subtitled-only — narrated captions come from a separate voiceover. This
    REPLACES the current cues + any hand-edits; the frontend confirms first.

    Row-locked re-fetch + COMMIT BEFORE the enqueue — same rationale as
    `dispatch_apply_captions` (the task's start write is token-checked).
    """
    lang = (language or "").strip().lower()
    if lang not in _SUBTITLED_CAPTION_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported caption language.",
        )
    result = await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if variant.get("resolved_archetype") != "subtitled":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Changing the caption language is only available on subtitled videos.",
        )
    if variant.get("smart_captions_applied"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Smart Caption language changes require generating a new video.",
        )
    if not variant.get("base_video_path"):
        # A no-speech subtitled variant has no caption-free base — the worker would
        # no-op. Surface it at the route like the sibling caption endpoints do.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This video has no captions to re-transcribe.",
        )
    render_gen_id = _mark_variant_rendering(job, variant_id)
    await db.commit()
    from app.tasks.generative_build import retranscribe_subtitled_captions  # noqa: PLC0415

    # Caption tasks inline-run the overlay/SFX reapply passes — overlay-jobs queue.
    retranscribe_subtitled_captions.apply_async(
        args=[str(job_id), variant_id, lang],
        kwargs={"render_gen_id": render_gen_id},
        queue="overlay-jobs",
    )


async def dispatch_apply_custom_effect(
    job_id: uuid.UUID, variant_id: str, *, effect_raw: dict, db: AsyncSession
) -> None:
    """Validate + enqueue Nova's sandboxed effect-language burn for one variant
    (PR6, effect-language train).

    Route-level flag gate (`settings.custom_effects_enabled`, 404 when off)
    happens in the caller, matching the SFX-lane pattern — this function is
    the shared dispatcher any caller (copilot-driven or, later, a direct panel
    control) can reuse once the gate has already passed.

    Validates the spec HERE, before the row-locked fetch, so an invalid spec
    422s without ever touching `render_status` — chat-authored specs are
    already validated once at parse time in `edit_copilot.py`, but the PATCH
    body reaching this route is untrusted regardless of origin.
    `apply_custom_effect_render` validates AGAIN, independently, at execution
    time (never trusts a stored/dispatched value either) — two checks, one
    boundary each.

    Row-locked re-fetch + COMMIT BEFORE the enqueue — same discipline as
    `dispatch_retranscribe_captions` (the task's start write is token-checked
    against the just-minted render_generation_id, so a worker that dequeues
    before the commit would strand the variant in "rendering" forever).
    """
    from app.pipeline.custom_effects import (
        EFFECT_COST_CEILING as _EFFECT_COST_CEILING,  # noqa: PLC0415
    )
    from app.pipeline.custom_effects import (  # noqa: PLC0415
        EffectValidationError,
        estimate_cost,
        validate_effect_spec,
    )

    try:
        spec = validate_effect_spec(effect_raw)
    except EffectValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_effect_spec", "reason": exc.reason, "detail": exc.detail},
        ) from exc

    result = await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if not variant.get("base_video_path") and not variant.get("video_path"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This video has no rendered source to apply an effect to.",
        )
    duration_hint = float(variant.get("duration_s") or 0.0)
    if estimate_cost(spec, duration_s=duration_hint) > _EFFECT_COST_CEILING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This effect is too complex for the current clip length — shorten the window "
            "or use fewer filters.",
        )

    render_gen_id = _mark_variant_rendering(job, variant_id)
    await db.commit()
    from app.tasks.custom_effects_render import apply_custom_effect_render  # noqa: PLC0415

    apply_custom_effect_render.apply_async(
        args=[str(job_id), variant_id, spec.model_dump(mode="json")],
        kwargs={"render_gen_id": render_gen_id},
        queue="overlay-jobs",
    )


def _speech_timing_rerender_thunk(job_id: str, operation_id: str) -> Callable[[], None]:
    def _enqueue() -> None:
        from app.tasks.generative_build import rerender_speech_timing  # noqa: PLC0415

        rerender_speech_timing.apply_async(args=[job_id, operation_id], queue="plan-jobs")

    return _enqueue


def _legacy_silence_flag_for_operation(plan: dict, desired: bool) -> bool:
    """Keep the mutable silence flag only for pre-contract legacy jobs."""

    if plan.get("speech_cleanup_contract") in {"required_v1", "off_v1"}:
        return bool(plan.get("silence_cut_disabled"))
    return bool(desired)


def dispatch_apply_speech_cut_candidate(
    job: Job,
    variant_id: str,
    *,
    candidate_id: str,
    expected_revision: str,
) -> tuple[dict, Callable[[], None]]:
    """Accept one persisted source-timeline cut and force a complete rebuild."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.pipeline.speech_cut_state import accept_candidate  # noqa: PLC0415

    variant = require_editable_variant(job, variant_id)
    if not settings.retake_cut_enabled:
        raise _timeline_error(404, "automatic_cut_disabled")
    if variant.get("resolved_archetype") not in {"subtitled", "talking_head"}:
        raise _timeline_error(422, "automatic_cut_unavailable")
    try:
        updated, request = accept_candidate(
            variant,
            candidate_id_value=candidate_id,
            expected_revision=expected_revision,
        )
    except ValueError as exc:
        raise _timeline_error(409, str(exc)) from exc
    except LookupError as exc:
        raise _timeline_error(404, str(exc)) from exc

    render_gen_id = uuid.uuid4().hex
    operation_id = uuid.uuid4().hex
    request["operation_id"] = operation_id
    updated.update(
        {
            "ok": False,
            "render_status": "rendering",
            "render_generation_id": render_gen_id,
            "speech_cut_last_error": None,
        }
    )
    variants = [
        updated if v.get("variant_id") == variant_id else v
        for v in (job.assembly_plan or {}).get("variants") or []
    ]
    control = {
        "variant_id": variant_id,
        "forced_removals": updated["speech_cut_in_flight"]["desired_forced_removals"],
        "desired_disabled": False,
        "prior_disabled": (job.assembly_plan or {}).get("silence_cut_disabled") is True,
        "operation": request,
        "operation_id": operation_id,
        "finalizer_claim": None,
        "revision": request["revision"],
        "in_flight": updated["speech_cut_in_flight"],
        "execution_contract": "reviewed_cut_v1",
    }
    plan = job.assembly_plan or {}
    job.assembly_plan = {
        **plan,
        "silence_cut_disabled": _legacy_silence_flag_for_operation(plan, False),
        "speech_cut_control": control,
        "speech_cut_previous_variant": variant,
        "speech_cut_previous_variants": list((job.assembly_plan or {}).get("variants") or []),
        "speech_cut_last_error": None,
        "variants": variants,
    }
    job.status = "processing"
    flag_modified(job, "assembly_plan")
    mark_reattempt(job)
    return request, _speech_timing_rerender_thunk(str(job.id), operation_id)


def dispatch_restore_original_timing(
    job: Job,
    variant_id: str,
    *,
    expected_revision: str,
) -> tuple[dict, Callable[[], None]]:
    """Disable all speech cuts and rebuild from the durable original source."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.pipeline.speech_cut_state import restore_original_timing  # noqa: PLC0415

    variant = require_editable_variant(job, variant_id)
    if variant.get("resolved_archetype") not in {"subtitled", "talking_head"}:
        raise _timeline_error(422, "automatic_cut_unavailable")
    try:
        updated, request = restore_original_timing(variant, expected_revision=expected_revision)
    except ValueError as exc:
        raise _timeline_error(409, str(exc)) from exc
    operation_id = uuid.uuid4().hex
    request["operation_id"] = operation_id
    updated.update(
        {
            "ok": False,
            "render_status": "rendering",
            "render_generation_id": uuid.uuid4().hex,
            "speech_cut_last_error": None,
        }
    )
    variants = [
        updated if v.get("variant_id") == variant_id else v
        for v in (job.assembly_plan or {}).get("variants") or []
    ]
    plan = job.assembly_plan or {}
    job.assembly_plan = {
        **plan,
        "silence_cut_disabled": _legacy_silence_flag_for_operation(plan, True),
        "speech_cut_control": {
            "variant_id": variant_id,
            "forced_removals": [],
            "desired_disabled": True,
            "prior_disabled": (job.assembly_plan or {}).get("silence_cut_disabled") is True,
            "operation": request,
            "operation_id": operation_id,
            "finalizer_claim": None,
            "revision": request["revision"],
            "in_flight": updated["speech_cut_in_flight"],
            "execution_contract": "restore_original_v1",
        },
        "speech_cut_previous_variant": variant,
        "speech_cut_previous_variants": list((job.assembly_plan or {}).get("variants") or []),
        "speech_cut_last_error": None,
        "variants": variants,
    }
    job.status = "processing"
    flag_modified(job, "assembly_plan")
    mark_reattempt(job)
    return request, _speech_timing_rerender_thunk(str(job.id), operation_id)


def rollback_speech_cut_dispatch(
    job: Job, error: str, *, expected_operation_id: str | None = None
) -> None:
    """Restore the exact last-good state when queue publication fails."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    plan = job.assembly_plan or {}
    prior = plan.get("speech_cut_previous_variant")
    control = plan.get("speech_cut_control") or {}
    if expected_operation_id and control.get("operation_id") != expected_operation_id:
        return
    if not isinstance(prior, dict):
        return
    previous_variants = plan.get("speech_cut_previous_variants")
    variants = (
        list(previous_variants)
        if isinstance(previous_variants, list)
        else [
            prior if v.get("variant_id") == control.get("variant_id") else v
            for v in plan.get("variants") or []
        ]
    )
    job.assembly_plan = {
        **plan,
        "silence_cut_disabled": bool(control.get("prior_disabled")),
        "speech_cut_control": None,
        "speech_cut_previous_variant": None,
        "speech_cut_previous_variants": None,
        "speech_cut_last_error": str(error)[:300],
        "variants": variants,
    }
    job.status = "variants_ready"
    flag_modified(job, "assembly_plan")


def dispatch_change_style(
    job: Job, variant_id: str, *, style_set_id: str, publish: bool = True
) -> PendingVariantPublish:
    """Validate + enqueue a text-style-set change for one variant."""
    from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

    variant = require_editable_variant(job, variant_id)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)
    if style_set_id not in set(style_set_ids(applies_to="generative")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown or non-generative style set.",
        )

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    def _publish() -> None:
        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            style_set_id=style_set_id,
            render_gen_id=render_gen_id,
        )

    receipt = _pending_variant_publish(
        job,
        variant_id,
        callback=_publish,
        render_generation_id=render_gen_id,
        previous_variant=previous_variant,
        previous_started_at=previous_started_at,
    )
    if publish:
        receipt()
    return receipt


def dispatch_set_intro_size(
    job: Job, variant_id: str, *, text_size_px: int, publish: bool = True
) -> PendingVariantPublish:
    """Validate + enqueue a user intro font-size override for one variant."""
    from app.pipeline.overlay_sizing import clamp_intro_px  # noqa: PLC0415

    variant = require_editable_variant(job, variant_id)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)
    # Only the AI-intro text variants carry a resizable hero overlay. The lyrics
    # variant's typography is governed by its style set and a text-removed variant
    # has no overlay, so resizing either is a no-op — reject rather than spin up a
    # render that changes nothing.
    if variant.get("text_mode") != "agent_text":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This edit has no resizable intro text.",
        )
    px = clamp_intro_px(text_size_px)

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    def _publish() -> None:
        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            size_override_px=px,
            render_gen_id=render_gen_id,
        )

    receipt = _pending_variant_publish(
        job,
        variant_id,
        callback=_publish,
        render_generation_id=render_gen_id,
        previous_variant=previous_variant,
        previous_started_at=previous_started_at,
    )
    if publish:
        receipt()
    return receipt


def dispatch_set_intro_timing(
    job: Job,
    variant_id: str,
    *,
    start_s: float,
    end_s: float,
    publish: bool = True,
) -> PendingVariantPublish:
    """Validate + enqueue a user intro-timing override for one variant."""
    variant = require_editable_variant(job, variant_id)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)
    if variant.get("text_mode") != "agent_text":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This variant has no intro overlay to retime.",
        )
    if end_s <= start_s:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_s must be greater than start_s.",
        )
    variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            v["intro_start_s"] = start_s
            v["intro_end_s"] = end_s
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    def _publish() -> None:
        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            intro_start_s_override=start_s,
            intro_end_s_override=end_s,
            render_gen_id=render_gen_id,
        )

    receipt = _pending_variant_publish(
        job,
        variant_id,
        callback=_publish,
        render_generation_id=render_gen_id,
        previous_variant=previous_variant,
        previous_started_at=previous_started_at,
    )
    if publish:
        receipt()
    return receipt


def dispatch_patch_scene_timing(job: Job, variant_id: str, *, overrides: list[dict]) -> None:
    """Store user-edited scene timing overrides; no re-render (apply-on-request)."""
    variant = require_editable_variant(job, variant_id)
    if variant.get("intro_mode") != "sequence":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Scene timing is only editable on sequence variants.",
        )
    # Persist overrides onto the variant dict; render path applies them.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["scene_timing_overrides"] = [
                o if isinstance(o, dict) else o.model_dump() for o in overrides
            ]
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    # NOTE: no render enqueue here — overrides are applied at next reburn.


def validate_media_overlays_for_user(
    *,
    overlays_raw: list[dict],
    user_id: str,
    plan_item_id: str | None = None,
    variant_context: dict | None = None,
) -> list[dict]:
    """Validate a full media-overlay replacement list for one user's namespace.

    ``plan_item_id`` narrows user-uploaded assets to the exact item being
    edited.  Callers that operate outside the item editor retain the broader
    user namespace contract by omitting it.
    """
    from app.agents._schemas.media_overlay import (  # noqa: PLC0415
        coerce_media_overlays,
        validate_overlay_gcs_path,
    )

    _user_prefix = f"users/{user_id}/"
    _asset_prefix = (
        f"{_user_prefix}plan/{plan_item_id}/" if plan_item_id is not None else _user_prefix
    )
    validated: list[dict] = []
    if overlays_raw:
        # Fail loudly on schema-invalid cards (prod 2026-07-12): coerce is
        # deliberately lenient for agent/render paths, but a user-facing
        # full-replace endpoint must never 200 while silently discarding cards
        # it was sent — a payload of all-invalid cards used to persist [] and
        # wipe the user's overlays.
        dropped_indices: list[int] = []
        cards = coerce_media_overlays(overlays_raw, dropped_indices=dropped_indices) or []
        if dropped_indices:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{len(dropped_indices)} of {len(overlays_raw)} overlay card(s) "
                    f"failed validation (indices: {dropped_indices}). "
                    "No changes were saved."
                ),
            )
        for card in cards:
            try:
                validate_overlay_gcs_path(card.src_gcs_path)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid overlay asset path: {exc}",
                ) from exc
            if not card.src_gcs_path.startswith(_asset_prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Overlay asset path must be under '{_asset_prefix}': {card.src_gcs_path!r}"
                    ),
                )
            if card.preview_gcs_path:
                try:
                    validate_overlay_gcs_path(card.preview_gcs_path)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid overlay preview path: {exc}",
                    ) from exc
                if not card.preview_gcs_path.startswith(_asset_prefix):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Overlay preview path must be under '{_asset_prefix}': "
                            f"{card.preview_gcs_path!r}"
                        ),
                    )
            if card.end_s <= card.start_s:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Card {card.id}: end_s must be greater than start_s.",
                )
            validated.append(card.model_dump())
        if variant_context is not None:
            # Plan 009 E4+E9: fullscreen contract. Shared by render:true,
            # render:false autosave, AI apply, and editor-commit Save paths.
            from app.services.overlay_apply import (  # noqa: PLC0415
                validate_fullscreen_constraints,
            )

            try:
                validate_fullscreen_constraints(cards, variant_context)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
    return validated


def validate_sound_effects_for_user(
    *, sfx_raw: list[dict], user_id: str, plan_item_id: str | None = None
) -> list[dict]:
    """Validate a full sound-effect placement replacement list for one user.

    With ``plan_item_id``, user-uploaded effects are narrowed to that item's
    asset prefix. Curated ``sound-effects/`` catalog paths remain valid.
    """
    from app.agents._schemas.sound_effect import (  # noqa: PLC0415
        coerce_sound_effects,
        normalize_generated_sound_effects,
        validate_sfx_gcs_path,
    )

    _user_prefix = f"users/{user_id}/"
    _asset_prefix = (
        f"{_user_prefix}plan/{plan_item_id}/" if plan_item_id is not None else _user_prefix
    )
    validated: list[dict] = []
    if sfx_raw:
        placements = coerce_sound_effects(sfx_raw) or []
        for placement in placements:
            try:
                validate_sfx_gcs_path(placement.src_gcs_path)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid SFX asset path: {exc}",
                ) from exc
            is_user_path = placement.src_gcs_path.startswith("users/")
            if is_user_path and not placement.src_gcs_path.startswith(_asset_prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"SFX asset path must be under '{_asset_prefix}': "
                        f"{placement.src_gcs_path!r}"
                    ),
                )
            validated.append(placement.model_dump())
    return normalize_generated_sound_effects(validated)


def _caption_reburn_enqueue_thunk(
    job_id: str, variant_id: str, render_gen_id: str
) -> Callable[[], None]:
    """Deferred enqueue for the lane dispatchers' caption branch (R1-1).

    The reburn's start write is token-checked against `render_gen_id`, so the
    caller MUST commit the gate/gen/lane write before invoking this — otherwise
    a fast worker reads the pre-commit generation, discards its start write,
    and strands the variant in "rendering" behind the 409 gate.
    """

    def _enqueue() -> None:
        from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

        # Caption tasks inline-run the overlay/SFX reapply passes — the solo
        # overlay-jobs worker exists to serialize those (macOS prefork CLIP fork
        # hazard), so every caption-task enqueue rides that queue.
        reburn_narrated_captions.apply_async(
            args=[job_id, variant_id],
            kwargs={"render_gen_id": render_gen_id},
            queue="overlay-jobs",
        )

    return _enqueue


def _caption_camera_rerender_enqueue_thunk(
    job_id: str, variant_id: str, render_gen_id: str
) -> Callable[[], None]:
    """Deferred caption-base rebuild after a grouped camera-effect removal."""

    def _enqueue() -> None:
        from app.tasks.generative_build import rerender_caption_camera_effects  # noqa: PLC0415

        rerender_caption_camera_effects.apply_async(
            args=[job_id, variant_id],
            kwargs={"render_gen_id": render_gen_id},
            queue="overlay-jobs",
        )

    return _enqueue


_OVERLAY_CAMERA_REBUILD_PENDING = "overlay_camera_rebuild_pending"


def dispatch_set_media_overlays(
    job: Job,
    variant_id: str,
    *,
    overlays_raw: list[dict],
    user_id: str,
) -> Callable[[], None] | None:
    """Validate + enqueue a media-overlay card apply-pass for one variant.

    Full-replace semantics: the caller sends the entire new card list.
    An empty list clears all cards (restores the clean variant from
    pre_media_overlay_video_path if available).

    Persists render_status="rendering" on the variant BEFORE enqueuing so the
    frontend immediately reflects the in-progress state — same pattern as
    dispatch_edit_timeline (persist first, enqueue second).

    Returns None when the montage fast-pass can enqueue inline. Caption reburns
    and generated-effect cascades return a deferred-enqueue thunk INSTEAD of
    enqueuing: those workers read state persisted alongside the newly minted
    `render_generation_id`, so async routes MUST `await db.commit()` before
    invoking the thunk. Otherwise a fast worker could rebake a deleted sibling
    or discard its token-checked start write against the old generation.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.media_overlays_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media overlays are not available.",
        )

    variant = require_editable_variant(job, variant_id)
    validated = validate_media_overlays_for_user(
        overlays_raw=overlays_raw,
        user_id=user_id,
        variant_context=variant,
    )
    cascaded_sfx, cascaded_camera_effects = cascade_removed_overlay_effect_groups(
        variant,
        validated,
    )
    camera_rebuild_required = bool(
        cascaded_camera_effects is not None or variant.get(_OVERLAY_CAMERA_REBUILD_PENDING)
    )

    # Persist render_status="rendering" first (row-locked by the DB session the
    # route holds), then enqueue — prevents a race where the worker reads "ready"
    # and an immediate second PUT sees "ready" and double-enqueues.
    # render_generation_id joins the same write so every overlay writer (manual
    # item-page apply, rail apply, zero-click auto-apply — all funnel through
    # here) participates in the editor-commit supersession model: an editor Save
    # whose base_generation predates this apply now 409s instead of silently
    # replacing the cards, and a superseded apply-render discards its terminal
    # write (same pattern as dispatch_set_text_elements).
    render_gen_id = uuid.uuid4().hex

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    # R2 (plan 010 review): on caption archetypes a lane save must render through
    # the caption reburn + reapply chain — the fast pass composites onto the
    # CURRENT video, so an in-flight caption reburn superseded by this save would
    # silently lose the caption edit. Persist the validated lane in the SAME
    # write as the gate/gen (the reburn renders from persisted state, not
    # overrides); legacy variants without a base keep the fast pass.
    caption_reburn_route = variant.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES and bool(
        variant.get("base_video_path")
    )

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            if caption_reburn_route:
                v["media_overlays"] = validated or None
            if cascaded_sfx is not None:
                v["sound_effects"] = cascaded_sfx or None
            if cascaded_camera_effects is not None:
                v["camera_effects"] = cascaded_camera_effects or None
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    mark_reattempt(job)
    # NOTE (montage branch only): the enqueue sends to Redis immediately
    # (synchronously). The DB commit in the caller's route (`await db.commit()`)
    # happens after this function returns. The window where the task sees an
    # uncommitted row is milliseconds — the same accepted race as other
    # dispatch_* functions in this module. (Celery's ALWAYS_EAGER test mode is
    # the exception — tasks run inline.) The caption branch is NOT allowed that
    # race: the reburn's start write is token-gated, so it returns a thunk the
    # caller invokes after its commit (R1-1).

    if caption_reburn_route and camera_rebuild_required:
        return _caption_camera_rerender_enqueue_thunk(str(job.id), variant_id, render_gen_id)
    if caption_reburn_route:
        return _caption_reburn_enqueue_thunk(str(job.id), variant_id, render_gen_id)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    # Route overlay-only tasks to the dedicated overlay queue so they land on
    # the --pool=solo worker (overlay-jobs) rather than the prefork worker.
    # On macOS the CLIP model causes SIGSEGV in forked prefork children; the
    # solo worker avoids the fork entirely. Prod: fly.toml worker listens on
    # celery,plan-jobs,overlay-jobs so no extra process needed.
    regen_kwargs: dict = {
        "media_overlays_override": validated,
        "render_gen_id": render_gen_id,
    }
    if camera_rebuild_required:
        # Camera effects are base-affecting. Persist the overlay replacement too
        # and rebuild from authoritative variant state instead of taking the
        # lightweight outer-overlay pass.
        for v in variants:
            if v.get("variant_id") == variant_id:
                v["media_overlays"] = validated or None
                break
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
        regen_kwargs = {"render_gen_id": render_gen_id, "force_full_render": True}

    def _enqueue() -> None:
        regenerate_generative_variant.apply_async(
            args=[str(job.id), variant_id],
            kwargs=regen_kwargs,
            queue="overlay-jobs",
        )

    if cascaded_sfx is not None or camera_rebuild_required:
        # The worker reads cascaded siblings from persisted state. Defer until
        # the route commits so an eager/fast worker cannot rebake deleted effects.
        return _enqueue
    _enqueue()
    return None


def dispatch_set_sound_effects(
    job: Job,
    variant_id: str,
    *,
    sfx_raw: list[dict],
    user_id: str,
    db_for_glossary,  # AsyncSession for resolving sound_effect_id references
) -> Callable[[], None] | None:
    """Validate + enqueue a sound-effects apply-pass for one variant.

    Full-replace semantics: the caller sends the entire new placement list.
    An empty list clears all effects (restores the clean variant from
    pre_sfx_video_path if available).

    Persists render_status="rendering" on the variant BEFORE enqueuing.
    Routes to the overlay-jobs queue (same as media overlays — solo worker,
    no CLIP model fork hazard).

    Return contract mirrors dispatch_set_media_overlays: None on the montage
    fast-pass branch (enqueued inline); on the caption-reburn branch a
    deferred-enqueue thunk the caller MUST invoke only after `await
    db.commit()` (R1-1 — the reburn's start write is token-checked).
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.sound_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sound effects are not available.",
        )

    variant = require_editable_variant(job, variant_id)

    validated = validate_sound_effects_for_user(sfx_raw=sfx_raw, user_id=user_id)

    # Persist render_status="rendering" first (same pattern as dispatch_set_media_overlays).
    # render_generation_id joins the same write — see dispatch_set_media_overlays.
    render_gen_id = uuid.uuid4().hex

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    # R2 (plan 010 review): caption archetypes render lane saves through the
    # caption reburn + reapply chain — see dispatch_set_media_overlays.
    caption_reburn_route = variant.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES and bool(
        variant.get("base_video_path")
    )

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            stamp_variant_attempt(v)
            v["render_generation_id"] = render_gen_id
            if caption_reburn_route:
                v["sound_effects"] = validated or None
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    mark_reattempt(job)

    if caption_reburn_route:
        # R1-1: deferred enqueue — the caller commits the gate write first.
        return _caption_reburn_enqueue_thunk(str(job.id), variant_id, render_gen_id)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.apply_async(
        args=[str(job.id), variant_id],
        kwargs={"sfx_override": validated, "render_gen_id": render_gen_id},
        queue="overlay-jobs",
    )
    return None


def validate_text_elements_payload(
    variant: dict,
    elements: list[dict],
    *,
    require_base: bool,
    strict_drop: bool = False,
    append_projection_tombstones: bool = True,
) -> tuple[list[dict], bool]:
    """Shared text-element SECTION validation (PUT /text-elements + editor-commit E2).

    Raises (no writes):
      - Feature flag disabled → 404
      - text_mode='lyrics' → 422 (A16; lyric lines are beat-synced)
      - len(elements) > _TEXT_ELEMENTS_MAX → 422 (A—)
      - `require_base` + base_video_path is None → 422 (no cached base yet)
      - end_s <= start_s on any coerced element → 422

    Returns `(validated_element_dicts, materialized_from_sequence)` — the flag is
    True when an empty payload on a first-edit sequence variant was seeded from
    the live scenes (T8 materialization), so the caller records the metadata.
    Invalid entries are dropped silently by `coerce_text_elements` by default
    (legacy PUT behavior). editor-commit passes `strict_drop=True`, turning any
    dropped entry into a 422 so Save never loses user-authored text silently.
    """
    if not _TEXT_ELEMENTS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Text element editing is not available.",
        )

    # A16: lyrics variant is beat-synced; ordinary user-authored text is only
    # reopened by the lyrics editor flag. The lyric_line projections themselves
    # remain timing-locked and are edited through the lyrics section.
    if variant.get("text_mode") == "lyrics" and not _LYRICS_EDITOR_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Text elements cannot be edited on a lyrics variant.",
        )
    if variant.get("lyrics_baked") is not False:
        # Legacy/baked variant: lyric_line is only ever a read-only projection
        # (see text_element.py's text_elements_for_variant) — a submission
        # containing one means a stale client re-sent a projected bar. Reject
        # outright, same as before this feature existed.
        if any(isinstance(raw, dict) and raw.get("role") == "lyric_line" for raw in elements):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "lyric_timing_locked", "message": "Lyric timing is locked."},
            )
    else:
        # Lyrics-as-optional-elements: role=lyric_line elements are ordinary
        # editable elements on a lyrics_baked=False variant — text/style may
        # change freely, but timing must not drift once anchored. GET
        # .../lyric-seeds is the sole source of a lyric line's initial timing,
        # so the FIRST save for a given lyric-line id accepts whatever
        # start_s/end_s the client sent as the new ground truth (it can only
        # have come from our own seeds endpoint — a bogus first-time value
        # merely mistimes the user's own edit, not a security concern). Every
        # SUBSEQUENT save is compared against the variant's own
        # previously-persisted value for that id and rejected on drift beyond
        # float-rounding slack. This is the simpler of the two options the
        # spec offered (recompute-and-compare vs. refuse deltas) — it needs no
        # DB/track lookup inside this synchronous validator, unlike
        # recomputing the live seed schedule would.
        existing_lyric_by_id = {
            e.get("id"): e
            for e in (variant.get("text_elements") or [])
            if isinstance(e, dict) and e.get("role") == "lyric_line"
        }
        for raw in elements:
            if not isinstance(raw, dict) or raw.get("role") != "lyric_line":
                continue
            prior = existing_lyric_by_id.get(raw.get("id"))
            if prior is None:
                continue
            try:
                prior_start = float(prior.get("start_s"))
                prior_end = float(prior.get("end_s"))
                new_start = float(raw.get("start_s"))
                new_end = float(raw.get("end_s"))
            except (TypeError, ValueError):
                continue
            if (
                abs(new_start - prior_start) > _LYRIC_ELEMENT_TIMING_TOLERANCE_S
                or abs(new_end - prior_end) > _LYRIC_ELEMENT_TIMING_TOLERANCE_S
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "lyric_timing_locked", "message": "Lyric timing is locked."},
                )

    if variant.get("resolved_archetype") == "subtitled":
        from app.config import settings as _settings  # noqa: PLC0415

        if not _settings.subtitled_text_lane_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Text element editing is not available for subtitled variants.",
            )
        if not variant.get("base_video_path"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "No cached base video for subtitled text reburn; regenerate the variant first."
                ),
            )

    # A—: payload size cap (50 elements comfortably covers the longest short-form edit)
    if len(elements) > _TEXT_ELEMENTS_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many text elements (max {_TEXT_ELEMENTS_MAX}).",
        )

    # fast-reburn requires a pre-built text-free base; older/lyrics variants lack it.
    if require_base and not variant.get("base_video_path"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No cached base video for fast-reburn — regenerate the variant first.",
        )

    # T8 — Sequence materialization: on the first text-element write for a sequence
    # variant, seed elements from the live scenes when the user sent an empty list.
    # This gives them the current editorial sequence as their starting point.
    _is_first_sequence_edit = (
        not variant.get("text_elements_user_edited") and variant.get("intro_mode") == "sequence"
    )
    if _is_first_sequence_edit and not elements:
        from app.agents._schemas.text_element import (  # noqa: PLC0415
            text_elements_for_variant,
        )

        snapshot = text_elements_for_variant(variant)
        if snapshot:
            elements = [e.model_dump() for e in snapshot]

    # Validate + coerce elements; drop invalid entries silently (A—).
    from app.agents._schemas.text_element import (  # noqa: PLC0415
        TextElement,
        coerce_text_elements,
    )

    validated: list[dict] = []
    if elements:
        coerced = coerce_text_elements(elements)
        if strict_drop and len(coerced or []) != len(elements):
            for idx, raw in enumerate(elements):
                elem_label = f"#{idx}"
                if isinstance(raw, dict):
                    elem_label = str(raw.get("id") or elem_label)
                    try:
                        TextElement.model_validate(raw)
                    except Exception as exc:  # noqa: BLE001
                        errors = getattr(exc, "errors", lambda: [])()
                        first = errors[0] if errors else {}
                        loc = first.get("loc") or ("element",)
                        field = ".".join(str(part) for part in loc)
                        value = raw.get(field) if "." not in field else None
                        msg = first.get("msg") or str(exc)
                        if not variant.get("text_elements_user_edited"):
                            log.warning(
                                "projected_text_element_strict_rejected",
                                variant_id=variant.get("variant_id"),
                                element_index=idx,
                                field=field,
                                message=msg,
                                shape=_text_element_shape_for_log(raw),
                            )
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=(
                                f"Text element {elem_label}: field {field} has invalid value "
                                f"{value!r}: {msg}"
                            ),
                        ) from exc
                else:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Text element {elem_label}: expected an object, "
                            f"got {type(raw).__name__}."
                        ),
                    )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="One or more text elements are invalid and were not saved.",
            )
        if coerced:
            # Additional cross-field check: end_s must be > start_s.
            for elem in coerced:
                if (elem.end_s or 0.0) <= (elem.start_s or 0.0):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Element {elem.id}: end_s must be greater than start_s.",
                    )
            from app.pipeline.text_motion_v2 import text_motion_complexity_error  # noqa: PLC0415

            complexity_error = (
                text_motion_complexity_error(list(coerced))
                if settings.text_motion_v2_enabled
                else None
            )
            if complexity_error is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=complexity_error,
                )
            validated = [e.model_dump() for e in coerced]
            if append_projection_tombstones:
                validated = append_ai_text_tombstones(variant, validated)
    elif variant.get("text_elements_user_edited"):
        # Explicit empty list = delete all generated AI text. Persist tombstones
        # so the read adapter does not resurrect projected bars on reload.
        validated = append_ai_text_tombstones(variant, []) if append_projection_tombstones else []
    return validated, _is_first_sequence_edit


def dispatch_set_text_elements(
    job: Job,
    variant_id: str,
    *,
    elements: list[dict],
    render: bool = True,
    publish: bool = True,
) -> PendingVariantPublish | None:
    """Validate + persist TextElements on a variant; optionally enqueue fast-reburn.

    Full-replace semantics: `elements` becomes the authoritative element list for
    this variant.  An empty list clears all text overlays.

    Guards (all raise HTTPException before any write):
      - Feature flag disabled → 404
      - Unknown / rendering variant → 404 / 409 (via require_editable_variant)
      - Section rules → 404/422 (via validate_text_elements_payload)

    On write (all before enqueue):
      - Stores validated elements as text_elements on the variant dict
      - Sets text_elements_user_edited=True
      - Writes render_generation_id (A20) for stale-write detection
      - Sets render_status='rendering' when render=True
      - Replaces job.assembly_plan (SQLAlchemy change tracking via flag_modified)
    """
    if not _TEXT_ELEMENTS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Text element editing is not available.",
        )

    variant = require_editable_variant(job, variant_id, allow_guided_text=True)
    _require_guided_story_text_ids(variant, elements)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)

    validated, _is_first_sequence_edit = validate_text_elements_payload(
        variant,
        elements,
        require_base=render,
        strict_drop=variant.get("resolved_archetype") == "guided_story",
    )
    _require_guided_story_text_ids(variant, validated)

    # Write render_generation_id before any DB mutation so the stale check in the
    # worker can compare against the value that was current when we enqueued.
    render_gen_id = uuid.uuid4().hex

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["text_elements"] = validated
            v["text_elements_user_edited"] = True
            v["render_generation_id"] = render_gen_id
            # T8: record sequence materialization metadata on first sequence edit.
            if _is_first_sequence_edit:
                v["geometry_materialized_at_version"] = "1"
                v["text_elements_materialized_from"] = "sequence"
            if render:
                stamp_variant_attempt(v)
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")

    if render:
        # Only a rendering commit is a new attempt — a persist-only save
        # (render=False) must leave the clock alone.
        mark_reattempt(job)

        from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

        def _publish() -> None:
            # Route to the overlay-jobs queue (solo worker — avoids macOS
            # prefork CLIP fork crashes).
            regenerate_generative_variant.apply_async(
                args=[str(job.id), variant_id],
                kwargs={"render_gen_id": render_gen_id},
                queue="overlay-jobs",
            )

        receipt = _pending_variant_publish(
            job,
            variant_id,
            callback=_publish,
            render_generation_id=render_gen_id,
            previous_variant=previous_variant,
            previous_started_at=previous_started_at,
        )
        if publish:
            receipt()
        return receipt
    return None


def _lyrics_error(status_code: int, code: str, message: str | None = None) -> HTTPException:
    detail = {"code": code}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status_code, detail=detail)


def _validate_lyric_override_style(style: object, *, line_key: str) -> dict:
    if not isinstance(style, dict):
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_line_override",
            f"{line_key}.style must be an object.",
        )
    unknown = set(style) - _LYRIC_OVERRIDE_STYLE_KEYS
    if unknown:
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_line_override",
            f"{line_key}.style has unsupported keys: {sorted(unknown)}.",
        )
    from app.agents._schemas.text_element import _ALLOWED_FONTS  # noqa: PLC0415

    out: dict = {}
    for key, value in style.items():
        if key in {"color", "highlight_color"}:
            if not isinstance(value, str) or not _HEX_COLOR_RE.match(value.strip()):
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    f"{line_key}.style.{key} must be a #RRGGBB hex color.",
                )
            out[key] = value.strip()
        elif key == "font_family":
            if not isinstance(value, str) or value.strip() not in _ALLOWED_FONTS:
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    f"{line_key}.style.font_family is not an allowed font.",
                )
            out[key] = value.strip()
        elif key == "size_px":
            if isinstance(value, bool):
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    f"{line_key}.style.size_px must be an integer.",
                )
            try:
                size_px = int(value)
            except (TypeError, ValueError) as exc:
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    f"{line_key}.style.size_px must be an integer.",
                ) from exc
            if size_px < 8 or size_px > 300:
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    f"{line_key}.style.size_px must be between 8 and 300.",
                )
            out[key] = size_px
    return out


def validate_lyrics_section(
    variant: dict, payload: dict, *, music_track: MusicTrack | None
) -> dict:
    if not _LYRICS_EDITOR_ENABLED:
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "lyrics_editor_disabled",
            "Lyrics editing is not available.",
        )
    if not isinstance(payload, dict):
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_lyrics_section",
            "Lyrics section must be an object.",
        )
    if "enabled" not in payload and "line_overrides" not in payload:
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_lyrics_section",
            "Provide enabled or line_overrides.",
        )
    enabled_raw = payload.get("enabled")
    enabled = None
    if "enabled" in payload:
        if not isinstance(enabled_raw, bool):
            raise _lyrics_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_lyrics_section",
                "enabled must be a boolean.",
            )
        enabled = enabled_raw
        if enabled:
            if not variant.get("music_track_id"):
                raise _lyrics_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_track")
            from app.pipeline.lyric_support import lyrics_variant_renderable  # noqa: PLC0415

            if music_track is None or not lyrics_variant_renderable(music_track.lyrics_cached):
                raise _lyrics_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_renderable_lyrics")

    line_overrides = None
    if "line_overrides" in payload:
        raw_overrides = payload.get("line_overrides")
        if raw_overrides is None:
            line_overrides = None
        else:
            if not isinstance(raw_overrides, dict):
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    "line_overrides must be an object or null.",
                )
            if len(raw_overrides) > _LYRIC_LINE_OVERRIDES_MAX:
                raise _lyrics_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_line_override",
                    f"Too many lyric line overrides (max {_LYRIC_LINE_OVERRIDES_MAX}).",
                )
            line_overrides = {}
            for line_key, raw_override in raw_overrides.items():
                line_key = str(line_key)
                if not _LYRIC_LINE_KEY_RE.match(line_key):
                    raise _lyrics_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "invalid_line_override",
                        f"Invalid lyric line key {line_key!r}.",
                    )
                if not isinstance(raw_override, dict):
                    raise _lyrics_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "invalid_line_override",
                        f"{line_key} override must be an object.",
                    )
                unknown = set(raw_override) - _LYRIC_OVERRIDE_KEYS
                timing_unknown = unknown & _LYRIC_TIMING_KEYS
                if unknown:
                    suffix = " Timing fields are locked." if timing_unknown else ""
                    raise _lyrics_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "invalid_line_override",
                        f"{line_key} has unsupported keys: {sorted(unknown)}.{suffix}",
                    )
                if not isinstance(raw_override.get("orig_text"), str):
                    raise _lyrics_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "invalid_line_override",
                        f"{line_key}.orig_text is required.",
                    )
                orig_start_s = raw_override.get("orig_start_s")
                if isinstance(orig_start_s, bool):
                    raise _lyrics_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "invalid_line_override",
                        f"{line_key}.orig_start_s is required.",
                    )
                try:
                    orig_start = float(orig_start_s)
                except (TypeError, ValueError) as exc:
                    raise _lyrics_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "invalid_line_override",
                        f"{line_key}.orig_start_s is required.",
                    ) from exc
                clean_override = {
                    "orig_text": raw_override["orig_text"],
                    "orig_start_s": orig_start,
                }
                if "text" in raw_override:
                    text = str(raw_override.get("text") or "").strip()
                    if not text or len(text) > 200:
                        raise _lyrics_error(
                            status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "invalid_line_override",
                            f"{line_key}.text must be 1-200 characters.",
                        )
                    clean_override["text"] = text
                if "style" in raw_override:
                    clean_override["style"] = _validate_lyric_override_style(
                        raw_override.get("style"), line_key=line_key
                    )
                line_overrides[line_key] = clean_override
    return {"enabled": enabled, "line_overrides": line_overrides}


def _orientation_error(status_code: int, code: str, message: str | None = None) -> HTTPException:
    detail: dict[str, str] = {"code": code}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status_code, detail=detail)


def _variant_orientation(variant: dict) -> str:
    return variant.get("orientation") or "portrait"


def _orientation_unsupported_reason(variant: dict) -> str | None:
    archetype = variant.get("resolved_archetype")
    if archetype in {"subtitled", "narrated", "talking_head"}:
        return "orientation_unsupported"
    if is_collage_montage_preset(
        variant.get("montage_preset_rendered") or variant.get("montage_preset")
    ):
        return "orientation_unsupported"
    # Visual blocks (#660) render portrait-sized boards; compositing them onto a
    # landscape canvas is unsupported until the block renderer is canvas-aware.
    if variant.get("visual_blocks"):
        return "orientation_unsupported"
    return None


def validate_orientation_section(variant: dict, orientation: object) -> str:
    if orientation not in {"portrait", "landscape"}:
        raise _orientation_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_orientation",
            "`orientation` must be 'portrait' or 'landscape'.",
        )
    if orientation == "landscape":
        reason = _orientation_unsupported_reason(variant)
        if reason is not None:
            raise _orientation_error(status.HTTP_422_UNPROCESSABLE_ENTITY, reason)
    return str(orientation)


async def dispatch_set_orientation(
    db: AsyncSession,
    job: Job,
    variant_id: str,
    *,
    orientation: str,
    revision_number: int | None = None,
    base_generation: str | None = None,
) -> None:
    if not _LANDSCAPE_OUTPUT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Landscape output is not available.",
        )

    result = await db.execute(select(Job).where(Job.id == job.id).with_for_update())
    locked_job = result.scalar_one_or_none()
    if locked_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    candidate = _find_variant(locked_job, variant_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    is_guided_v2 = candidate.get("resolved_archetype") == "guided_story" and getattr(
        settings, "guided_story_editor_v2_enabled", False
    )
    variant = require_editable_variant(
        locked_job,
        variant_id,
        allow_guided_text=is_guided_v2,
    )
    validated = validate_orientation_section(variant, orientation)

    if is_guided_v2:
        current_revision = _guided_v2_revision(locked_job, variant)
        if current_revision is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="guided_story_revision_unavailable",
            )
        if revision_number is None or base_generation is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GUIDED_REVISION_TOKEN_REQUIRED",
            )
        if revision_number != int(current_revision["revision_number"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GUIDED_REVISION_STALE",
            )
        if base_generation != variant_render_baseline(variant):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="baseline_conflict")
        await _require_current_guided_story_sources(db, locked_job)

    render_gen_id = uuid.uuid4().hex
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((locked_job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            if is_guided_v2:
                current_revision = _guided_v2_revision(locked_job, v)
                assert current_revision is not None
                current_revision["revision_number"] = int(current_revision["revision_number"]) + 1
                current_revision["orientation"] = validated
                current_revision["base_generation"] = variant_render_baseline(v)
                current_revision = normalize_guided_editor_revision(current_revision)
                v["guided_edit_revision"] = current_revision
            v["orientation"] = validated
            v["render_generation_id"] = render_gen_id
            stamp_variant_attempt(v)
            v["base_video_stale"] = True
            break
    locked_job.assembly_plan = {**(locked_job.assembly_plan or {}), "variants": variants}
    flag_modified(locked_job, "assembly_plan")
    mark_reattempt(locked_job)
    await db.commit()

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    kwargs = {"render_gen_id": render_gen_id, "force_full_render": True}
    if is_guided_v2:
        kwargs["guided_revision"] = next(
            v.get("guided_edit_revision") for v in variants if v.get("variant_id") == variant_id
        )
    else:
        kwargs["orientation_override"] = validated
    regenerate_generative_variant.apply_async(args=[str(locked_job.id), variant_id], kwargs=kwargs)


async def dispatch_set_lyrics(
    db: AsyncSession,
    job: Job,
    variant_id: str,
    *,
    enabled: object = _UNSET,
    line_overrides: object = _UNSET,
) -> None:
    if not _LYRICS_EDITOR_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lyrics editing is not available.",
        )

    result = await db.execute(select(Job).where(Job.id == job.id).with_for_update())
    locked_job = result.scalar_one_or_none()
    if locked_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    variant = require_editable_variant(locked_job, variant_id)
    track = None
    if variant.get("music_track_id"):
        track = await db.get(MusicTrack, variant.get("music_track_id"))
    payload: dict = {}
    if enabled is not _UNSET:
        payload["enabled"] = enabled
    if line_overrides is not _UNSET:
        payload["line_overrides"] = line_overrides
    validated = validate_lyrics_section(
        variant,
        payload,
        music_track=track,
    )

    render_gen_id = uuid.uuid4().hex
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((locked_job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            if enabled is not _UNSET:
                v["lyrics_enabled"] = bool(validated["enabled"])
            if line_overrides is not _UNSET:
                v["lyric_line_overrides"] = validated["line_overrides"]
            v["render_generation_id"] = render_gen_id
            stamp_variant_attempt(v)
            v["base_video_stale"] = True
            break
    locked_job.assembly_plan = {**(locked_job.assembly_plan or {}), "variants": variants}
    flag_modified(locked_job, "assembly_plan")
    mark_reattempt(locked_job)
    await db.commit()

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    # force_full_render: lyric-state changes carry no override kwargs, so without
    # it the regen can pick the fast-reburn path and skip lyric re-injection
    # entirely (2026-07-18 E2E bug — overrides silently dropped).
    regenerate_generative_variant.apply_async(
        args=[str(locked_job.id), variant_id],
        kwargs={"render_gen_id": render_gen_id, "force_full_render": True},
    )


def dispatch_edit_variant(
    job: Job,
    variant_id: str,
    *,
    text: str | None,
    remove_text: bool,
    style_set_id: str | None,
    text_size_px: int | None,
    intro_layout: str | None = None,
    font_family: str | None = None,
    effect: str | None = None,
    text_color: str | None = None,
    cluster_hero_font: str | None = None,
    cluster_body_font: str | None = None,
    cluster_accent_font: str | None = None,
    cluster_hero_size_px: int | None = None,
    cluster_body_size_px: int | None = None,
    cluster_accent_size_px: int | None = None,
    text_behind_subject: bool | None = None,
    carousel_moment: object = _UNSET,
    publish: bool = True,
) -> PendingVariantPublish:
    """Validate + enqueue a combined text/style/size/layout edit as ONE re-render.

    The instant editor batches an entire editing session into a single commit, so
    the user pays for one render instead of one per field. Reuses the same
    validation rules as the per-field dispatchers; `regenerate_generative_variant`
    already accepts all overrides together.

    `carousel_moment` (Blossom carousel) is tri-state, using the module's
    `_UNSET` sentinel: `_UNSET` (the default) = no carousel edit requested,
    the persisted moment (if any) is left alone; `None` = explicit removal;
    a `dict` (field-validated below) = partial edit, merged over whatever's
    persisted by `_merge_carousel_moment_override` on the worker side.
    """
    variant = require_editable_variant(job, variant_id)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)

    # Server-side flag gate (text-behind-subject): a stale client with the FE
    # flag cached on can still submit this field after a rollback — coerce to
    # None rather than 4xx, matching the fail-open safety rule this repo uses
    # for kill-switched edit fields.
    if text_behind_subject is not None:
        from app.config import settings as _tbs_settings  # noqa: PLC0415

        if not _tbs_settings.text_behind_subject_enabled:
            log.info(
                "text_behind_subject_edit_flag_disabled",
                job_id=str(job.id),
                variant_id=variant_id,
            )
            text_behind_subject = None

    # A15: once the user has edited via the timeline TextElement editor, the
    # instant-edit surface (which only understands the single-block linear intro)
    # must not clobber their work.  Redirect to PUT /text-elements instead.
    if variant.get("text_elements_user_edited") and _TEXT_ELEMENTS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Text has been edited via the timeline editor. "
                "Use PUT /text-elements to update this variant."
            ),
        )

    if (
        text is None
        and not remove_text
        and style_set_id is None
        and text_size_px is None
        and intro_layout is None
        and font_family is None
        and effect is None
        and text_color is None
        and cluster_hero_font is None
        and cluster_body_font is None
        and cluster_accent_font is None
        and cluster_hero_size_px is None
        and cluster_body_size_px is None
        and cluster_accent_size_px is None
        and text_behind_subject is None
        and carousel_moment is _UNSET
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one edit field.",
        )
    if text is not None and remove_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`text` and `remove_text` are mutually exclusive.",
        )
    # Sequence-synced variants (D19): intro-text/highlight edits are locked (the
    # words come from the voiceover transcript). Size nudge, style set, and
    # layout picks (the opt-out path) remain editable.
    if variant.get("intro_mode") == "sequence" and (text is not None or remove_text):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_SEQUENCE_TEXT_LOCKED_DETAIL,
        )
    # Behind-subject occlusion is not supported on sequence-role overlays in v1
    # (the renderer already strips it defensively — see
    # _strip_behind_subject_for_sequence_role in text_overlay_skia.py — but reject
    # here so the client gets an actionable 422 instead of a silent no-op).
    if variant.get("intro_mode") == "sequence" and text_behind_subject is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Behind-subject text isn't supported on synced (sequence) variants yet.",
        )
    if text is not None and not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide `text` to update, or set `remove_text=true` to clear the overlay.",
        )
    if style_set_id is not None:
        from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

        if style_set_id not in set(style_set_ids(applies_to="generative")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Unknown or non-generative style set.",
            )

    size_override_px: int | None = None
    if text_size_px is not None:
        # Same guard as dispatch_set_intro_size, relaxed for the add-text case: a
        # `none`-mode variant gains a resizable overlay when this edit supplies
        # text. Lyrics variants never have a resizable intro (their typography is
        # set-driven) — reject even with text, or the size silently drops.
        text_mode = variant.get("text_mode")
        size_ok = text_mode == "agent_text" or (text_mode == "none" and text is not None)
        if not size_ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This edit has no resizable intro text.",
            )
        from app.pipeline.overlay_sizing import clamp_intro_px  # noqa: PLC0415

        size_override_px = clamp_intro_px(text_size_px)

    if intro_layout is not None:
        if intro_layout not in ("linear", "cluster"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`intro_layout` must be 'linear' or 'cluster'.",
            )
        # A layout applies to the AI-intro overlay only — same eligibility rule
        # as size: agent_text, or a none-mode variant gaining text in this edit.
        text_mode = variant.get("text_mode")
        layout_ok = text_mode == "agent_text" or (text_mode == "none" and text is not None)
        if not layout_ok or remove_text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This edit has no intro text to lay out.",
            )
        if intro_layout == "cluster":
            # Sequence-capable variants bypass the hook word-count gate: a synced
            # variant (or one with a persisted transcript) renders the editorial
            # treatment from the SPOKEN words, not intro_text, so its hook length
            # is irrelevant. An explicit layout pick on a synced variant opts it
            # OUT of the sequence (the worker renders the static cluster from the
            # persisted intro_text and clears the transcript) — from then on the
            # variant is a plain cluster variant and this gate applies again.
            sequence_capable = variant.get("intro_mode") == "sequence" or bool(
                variant.get("transcript")
            )
            if not sequence_capable:
                from app.pipeline.intro_cluster import MAX_WORDS, MIN_WORDS  # noqa: PLC0415

                # Validate against the text that will actually render: the override
                # if supplied, else the persisted intro. The layout engine enforces
                # the same bound at render time (falling back to linear) — rejecting
                # here turns a silent fallback into actionable feedback.
                effective_text = (text or variant.get("intro_text") or "").strip()
                n_words = len(effective_text.split())
                if not (MIN_WORDS <= n_words <= MAX_WORDS):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"The editorial layout needs a {MIN_WORDS}-{MAX_WORDS} word hook "
                            f"(this text has {n_words}). Shorten the text first."
                        ),
                    )

    if effect is not None:
        from app.pipeline.style_sets import _INTRO_ANIMATION_EFFECTS  # noqa: PLC0415

        if effect not in _INTRO_ANIMATION_EFFECTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown animation effect: '{effect}'. "
                f"Allowed: {sorted(_INTRO_ANIMATION_EFFECTS)}",
            )

    if font_family is not None:
        from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

        if font_family not in _FONT_REGISTRY.get("fonts", {}):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown font '{font_family}'.",
            )

    for _cf_label, _cf_value in (
        ("cluster_hero_font", cluster_hero_font),
        ("cluster_body_font", cluster_body_font),
        ("cluster_accent_font", cluster_accent_font),
    ):
        if _cf_value is not None:
            from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

            if _cf_value not in _FONT_REGISTRY.get("fonts", {}):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown font '{_cf_value}' for {_cf_label}.",
                )

    from app.pipeline.overlay_sizing import clamp_intro_px as _clamp  # noqa: PLC0415

    cluster_hero_size_px = (
        _clamp(cluster_hero_size_px) if cluster_hero_size_px is not None else None
    )
    cluster_body_size_px = (
        _clamp(cluster_body_size_px) if cluster_body_size_px is not None else None
    )
    cluster_accent_size_px = (
        _clamp(cluster_accent_size_px) if cluster_accent_size_px is not None else None
    )

    if text_color is not None:
        import re  # noqa: PLC0415

        if not re.match(r"^#[0-9A-Fa-f]{6}$", text_color):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`text_color` must be a hex color (#RRGGBB).",
            )

    # Carousel-moment edit (Blossom carousel). `carousel_moment_override` is
    # what actually reaches `regenerate_generative_variant.delay(...)` below —
    # `_UNSET` (untouched by this block) means "no carousel edit requested",
    # translated to the Celery-safe `CAROUSEL_MOMENT_UNSET` sentinel at the
    # enqueue call since the route-local `_UNSET` object can't survive task
    # serialization. Field validation itself is shared with the editor-commit
    # staging path — see `_validate_carousel_moment_patch`.
    carousel_moment_override: object = _UNSET
    if carousel_moment is not _UNSET:
        if carousel_moment is None:
            # Explicit removal is always allowed — no eligibility check. A
            # moment can need clearing even after a flag flip or an
            # archetype change that would otherwise reject an ADD.
            carousel_moment_override = None
        else:
            carousel_moment_override = _validate_carousel_moment_patch(carousel_moment, variant)

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    _variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for _v in _variants:
        if _v.get("variant_id") == variant_id:
            stamp_variant_attempt(_v)
            _v["render_generation_id"] = render_gen_id
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": _variants}
    mark_reattempt(job)

    from app.tasks.generative_build import (  # noqa: PLC0415
        CAROUSEL_MOMENT_UNSET,
        regenerate_generative_variant,
    )

    def _publish() -> None:
        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            override_text=(text.strip() if text and not remove_text else None),
            remove_text=bool(remove_text),
            style_set_id=style_set_id,
            size_override_px=size_override_px,
            layout_override=intro_layout,
            font_family_override=font_family,
            effect_override=effect,
            text_color_override=text_color,
            cluster_hero_font_override=cluster_hero_font,
            cluster_body_font_override=cluster_body_font,
            cluster_accent_font_override=cluster_accent_font,
            cluster_hero_size_px_override=cluster_hero_size_px,
            cluster_body_size_px_override=cluster_body_size_px,
            cluster_accent_size_px_override=cluster_accent_size_px,
            text_behind_subject=text_behind_subject,
            render_gen_id=render_gen_id,
            carousel_moment_override=(
                CAROUSEL_MOMENT_UNSET
                if carousel_moment_override is _UNSET
                else carousel_moment_override
            ),
        )

    receipt = _pending_variant_publish(
        job,
        variant_id,
        callback=_publish,
        render_generation_id=render_gen_id,
        previous_variant=previous_variant,
        previous_started_at=previous_started_at,
    )
    if publish:
        receipt()
    return receipt


def dispatch_set_mix(
    job: Job, variant_id: str, *, mix: float, publish: bool = True
) -> PendingVariantPublish:
    """Validate + enqueue a voice/bed mix change for one voiceover variant."""
    variant = require_editable_variant(job, variant_id)
    previous_variant = copy.deepcopy(variant)
    previous_started_at = getattr(job, "started_at", None)
    # Only voiceover variants carry a voice bed to rebalance. A song/original/lyrics
    # variant has no `mix`, so adjusting it is a no-op — reject rather than spin up a
    # render that changes nothing. (Voiceover variants persist a non-None `mix`.)
    if variant.get("mix") is None and not variant_id.startswith("voiceover"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This edit has no voiceover to mix.",
        )

    # A mix change is a Save like any other, so it restarts the attempt clock and
    # closes the 409 re-entrancy gate. This dispatcher used to write no
    # render_status at all, which left BOTH clocks reading the first render's
    # timestamps and let two concurrent mix edits race.
    _variants = list((job.assembly_plan or {}).get("variants") or [])
    render_gen_id = uuid.uuid4().hex
    for _v in _variants:
        if _v.get("variant_id") == variant_id:
            stamp_variant_attempt(_v)
            _v["render_generation_id"] = render_gen_id
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": _variants}
    mark_reattempt(job)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    def _publish() -> None:
        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            mix_override=float(mix),
            render_gen_id=render_gen_id,
        )

    receipt = _pending_variant_publish(
        job,
        variant_id,
        callback=_publish,
        render_generation_id=render_gen_id,
        previous_variant=previous_variant,
        previous_started_at=previous_started_at,
    )
    if publish:
        receipt()
    return receipt


async def dispatch_set_narrated_bed_level(
    job_id: uuid.UUID, variant_id: str, *, bed_level: float, db: AsyncSession
) -> None:
    """Validate + enqueue a background-sound (voice/bed) change for a NARRATED variant.

    NOT `dispatch_set_mix` — that dispatches the generic regenerate path, which is
    scoped to `voiceover_only`/`voiceover_music` variants and explicitly rejects
    narrated/subtitled as no-ops. Narrated has no `mix` field at all (it hard-codes
    `mix: 1.0` and uses `voiceover_bed_level` instead) and subtitled has no bed
    concept whatsoever (its own clip audio is the only track) — so this is a
    dedicated dispatch onto the dedicated `reburn_narrated_bed_level` task.

    Row-locked (mirrors `_patch_narrated_variant`), NOT the unlocked
    `_mark_variant_rendering` + bare-commit pattern the sibling `dispatch_*`
    functions use (swap-song, retext, apply-captions, set-mix). Those all mutate
    an ALREADY-loaded, unlocked `job` snapshot and blind-overwrite the whole
    `assembly_plan` column on commit — safe enough when each variant only has one
    plausible concurrent writer, but the Background Sound slider (auto-commits on
    a debounce) sits in the same editor panel as the Captions on/off toggle
    (locked via `_patch_narrated_variant`), and a real drag-while-toggling race
    would silently revert whichever committed first while still marking the
    variant "rendering". Locking here closes that specific window; the
    inconsistency across the OTHER dispatch_* functions is a pre-existing,
    broader pattern this fix does not attempt to unify (see TODOS.md).
    """
    result = await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if variant.get("resolved_archetype") != "narrated":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Background sound can only be adjusted on narrated videos.",
        )
    render_gen_id = _mark_variant_rendering(job, variant_id)
    await db.commit()
    from app.tasks.generative_build import reburn_narrated_bed_level  # noqa: PLC0415

    # Caption tasks inline-run the overlay/SFX reapply passes, which the solo
    # overlay-jobs worker exists to serialize (macOS prefork CLIP fork hazard).
    reburn_narrated_bed_level.apply_async(
        args=[str(job_id), variant_id, float(bed_level)],
        kwargs={"render_gen_id": render_gen_id},
        queue="overlay-jobs",
    )


# ── Timeline editor: eligibility + GET/POST/DELETE dispatch ─────────────────────
# Single-sourced here so plan_items.py wraps the same logic verbatim (mirrors
# dispatch_retext & friends). The worker (lane 2) writes `ai_timeline` on each
# variant at render time and accepts `timeline_override` on regenerate.


def _durable_sources_prefix(job: Job) -> str:
    """Worker-copied per-job sources persist here (NOT in the 24h GCS delete rule).

    Anything else on a slot's `source_gcs_path` is a legacy/pre-feature job whose
    raw uploads may already be swept — treated as expired for editing purposes.
    """
    assembly_plan = getattr(job, "assembly_plan", None)
    all_candidates = getattr(job, "all_candidates", None)
    has_manual_source_provenance = (
        getattr(job, "mode", None) == "manual_draft"
        or (isinstance(assembly_plan, dict) and assembly_plan.get("manual_draft") is True)
        or (isinstance(all_candidates, dict) and all_candidates.get("manual_draft") is True)
    )
    if has_manual_source_provenance and getattr(job, "content_plan_item_id", None) is not None:
        # Manual drafts attach directly to the persistent per-item prefix. They
        # have not gone through the generative orchestrator's source-copy pass
        # yet, but their uploads are still durable and ownership-scoped.
        return f"users/{job.user_id}/plan/{job.content_plan_item_id}/"
    return f"generative-jobs/{job.id}/sources/"


def _timeline_parts(variant: dict) -> tuple[list[dict], list[dict], list[float]]:
    """(ai_slots, user_slots, beat_grid) with null-safe defaults."""
    ai = variant.get("ai_timeline") or {}
    ai_slots = [s for s in (ai.get("slots") or []) if isinstance(s, dict)]
    user = variant.get("user_timeline") or {}
    user_slots = [s for s in (user.get("slots") or []) if isinstance(s, dict)]
    beat_grid = [float(b) for b in (user.get("beat_grid") or ai.get("beat_grid") or [])]
    return ai_slots, user_slots, beat_grid


def _is_synthetic_carousel_slot(slot: dict) -> bool:
    """True for a carousel-moment segment's synthetic timeline slot — never a
    real uploaded clip.

    `_insert_carousel_moment_step` (generative_build.py) registers the
    rendered carousel segment under `clip_id_to_gcs[synthetic_id] =
    moment_path`, where `moment_path` is a LOCAL filesystem path
    (`render_carousel_moment` never uploads it — see its docstring). Every
    real clip's `source_gcs_path` is a bucket-relative GCS object key
    ("generative-jobs/...", "music-uploads/...", ...) which never starts with
    "/"; a synthetic slot's does. A slot with no `source_gcs_path` at all
    (e.g. a minimal test fixture, or a not-yet-probed timeline entry) is NOT
    treated as synthetic — only an unambiguous absolute local path is, so
    this never over-filters real data.
    """
    path = slot.get("source_gcs_path")
    return isinstance(path, str) and path.startswith("/")


def _variant_carousel_clip_indices(variant: dict) -> list[int]:
    """Distinct active source identities in render-card order (max five)."""
    ai_slots, user_slots, _beat_grid = _timeline_parts(variant)
    effective = user_slots if user_slots else ai_slots
    ordered: list[int] = []
    for slot in effective:
        index = slot.get("clip_index")
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and not _is_synthetic_carousel_slot(slot)
            and index not in ordered
        ):
            ordered.append(index)
        if len(ordered) == 5:
            break
    return ordered


def _variant_clip_count(variant: dict) -> int:
    """Best-effort count of distinct source clips a variant's montage uses.

    Read from the persisted timeline (the user-edited cut if present, else
    the AI cut) — the same ground truth `_timeline_parts` already exposes to
    the timeline editor. Used by the carousel-editor dispatch validation
    (`focus_clip_index` bounds) and `_editor_capabilities`'s `carousel` flag
    (>= 2 clips). Returns 0 (unknown) for a variant with no persisted
    timeline yet — e.g. not rendered, or a masonry/visual-blocks preset,
    which don't populate `ai_timeline` (see the
    `GENERATIVE_TIMELINE_EDITOR_ENABLED` guard in `_render_generative_variant`)
    — callers treat 0 as "can't derive a clip count", never as "zero clips".

    A carousel-moment segment's synthetic slot (see
    `_is_synthetic_carousel_slot`) is excluded — belt-and-braces alongside
    `_build_ai_timeline`'s write-side fix, which stops NEW timelines from
    ever persisting one. Without this, a carousel'd variant's clip count
    creeps up by one on every render (4 real clips -> 5 counted), corrupting
    `focus_clip_index` bounds validation on the next edit.
    """
    return len(_variant_carousel_clip_indices(variant))


_MUSIC_WINDOW_VARIANTS = frozenset({"song_text", "song_lyrics"})
_MUSIC_WINDOW_EPSILON_S = 0.02


def _track_duration(track: MusicTrack | None) -> float:
    try:
        value = float(getattr(track, "duration_s", 0.0) or 0.0) if track is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _track_beats(track: MusicTrack | None) -> list[float]:
    beats: list[float] = []
    for raw in (getattr(track, "beat_timestamps_s", None) or []) if track is not None else []:
        try:
            beat = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(beat) and beat >= 0:
            beats.append(beat)
    return sorted(set(beats))


def _recommended_music_start(track: MusicTrack | None, video_duration_s: float) -> float:
    cfg = (getattr(track, "track_config", None) or {}) if track is not None else {}
    try:
        requested = float(cfg.get("best_start_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        requested = 0.0
    track_duration_s = _track_duration(track)
    if track_duration_s <= 0:
        return max(0.0, requested)
    return max(0.0, min(requested, max(0.0, track_duration_s - video_duration_s)))


def _snap_music_start(track: MusicTrack, start_s: float, video_duration_s: float) -> float:
    max_start = max(0.0, _track_duration(track) - video_duration_s)
    requested = float(start_s)
    if requested < 0 or requested > max_start + _MUSIC_WINDOW_EPSILON_S:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "music_window_out_of_range"},
        )
    clamped = max(0.0, min(requested, max_start))
    candidates = [beat for beat in _track_beats(track) if beat <= max_start]
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "timing_metadata_unavailable"},
        )
    return round(min(candidates, key=lambda beat: (abs(beat - clamped), beat)), 3)


def _relative_music_grid(track: MusicTrack, start_s: float, video_duration_s: float) -> list[float]:
    end_s = start_s + video_duration_s
    relative = [0.0]
    relative.extend(
        round(beat - start_s, 3)
        for beat in _track_beats(track)
        if start_s + _MUSIC_WINDOW_EPSILON_S < beat < end_s - _MUSIC_WINDOW_EPSILON_S
    )
    relative.append(round(video_duration_s, 3))
    return sorted(set(relative))


def _valid_background_music_track(track: MusicTrack | None) -> bool:
    return bool(
        track is not None
        and track.analysis_status == "ready"
        and track.published_at is not None
        and track.archived_at is None
        and isinstance(track.audio_gcs_path, str)
        and track.audio_gcs_path.startswith("music/")
        and _track_duration(track) > 0
    )


def _background_music_response(
    variant: dict,
    track: MusicTrack | None,
    preview_url: str | None,
) -> dict | None:
    treatment = variant.get("smart_music_treatment")
    if not isinstance(treatment, dict) or not _valid_background_music_track(track):
        return None
    try:
        start_s = max(0.0, float(treatment.get("section_start_s") or 0.0))
        end_s = max(start_s, float(treatment.get("section_end_s") or 0.0))
        gain_db = min(0.0, max(-40.0, float(treatment.get("gain_db") or -18.0)))
    except (TypeError, ValueError):
        return None
    if end_s <= start_s or not preview_url:
        return None
    return {
        "track_id": str(track.id),
        "title": track.title,
        "artist": track.artist,
        "preview_url": preview_url,
        "src_gcs_path": track.audio_gcs_path,
        "start_s": round(start_s, 3),
        "end_s": round(end_s, 3),
        "duration_s": round(end_s - start_s, 3),
        "track_duration_s": round(_track_duration(track), 3),
        "gain_db": gain_db,
        "muted": gain_db <= -39.9,
        "enabled": True,
    }


def _resolve_background_music_treatment(
    payload: EditorCommitBackgroundMusic,
    *,
    track: MusicTrack | None,
    variant: dict,
) -> dict | None:
    if payload.track_id is None or not payload.enabled:
        return None
    if not _valid_background_music_track(track) or str(track.id) != str(payload.track_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "music_track_unavailable"},
        )
    video_duration_s = visual_block_variant_duration(variant)
    track_duration_s = _track_duration(track)
    current = variant.get("smart_music_treatment") or {}
    try:
        start_s = (
            float(payload.start_s)
            if payload.start_s is not None
            else float(current.get("section_start_s", 0.0) or 0.0)
        )
    except (TypeError, ValueError):
        start_s = 0.0
    default_end = min(track_duration_s, start_s + max(0.01, video_duration_s or 10.0))
    try:
        end_s = (
            float(payload.end_s)
            if payload.end_s is not None
            else float(current.get("section_end_s", default_end) or default_end)
        )
    except (TypeError, ValueError):
        end_s = default_end
    start_s = max(0.0, min(start_s, max(0.0, track_duration_s - 0.01)))
    end_s = max(start_s + 0.01, min(end_s, track_duration_s))
    if end_s <= start_s:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "music_window_out_of_range"},
        )
    gain_db = (
        -40.0
        if payload.muted
        else (
            payload.gain_db
            if payload.gain_db is not None
            else float(current.get("gain_db", -18.0) or -18.0)
        )
    )
    return {
        "track_id": str(track.id),
        "src_gcs_path": str(track.audio_gcs_path),
        "section_start_s": round(start_s, 3),
        "section_end_s": round(end_s, 3),
        "gain_db": min(0.0, max(-40.0, float(gain_db))),
        "speech_duck_db": float(current.get("speech_duck_db", -12.0) or -12.0),
        "final_lufs": float(current.get("final_lufs", -14.0) or -14.0),
    }


def _has_linear_slots(source: list[dict]) -> bool:
    active = [slot for slot in source if not slot.get("removed")]
    if not active:
        return False
    for slot in active:
        try:
            duration_s = float(slot.get("duration_s"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(duration_s) or duration_s <= 0:
            return False
    return True


def _has_linear_timeline(variant: dict) -> bool:
    ai_slots, user_slots, _ = _timeline_parts(variant)
    return _has_linear_slots(user_slots or ai_slots)


def _music_window_capability(
    variant: dict,
    track: MusicTrack | None,
    *,
    guided_revision: dict[str, Any] | None = None,
) -> dict | None:
    """Authoritative editor contract. None means hidden for unsupported variants."""
    guided_revision = guided_revision or (
        variant.get("guided_edit_revision")
        if variant.get("resolved_archetype") == "guided_story"
        else None
    )
    if str(variant.get("variant_id") or "") not in _MUSIC_WINDOW_VARIANTS and not isinstance(
        guided_revision, dict
    ):
        return None
    video_duration_s = (
        max(
            (
                float(segment.get("output_end_s") or 0.0)
                for segment in guided_revision.get("segments") or []
                if isinstance(segment, dict)
            ),
            default=0.0,
        )
        if isinstance(guided_revision, dict)
        else visual_block_variant_duration(variant)
    )
    track_duration_s = _track_duration(track)
    beats = _track_beats(track)
    reason: str | None = None
    if (
        track is None
        or getattr(track, "analysis_status", "ready") != "ready"
        or not getattr(track, "audio_gcs_path", None)
    ):
        reason = "track_unavailable"
    elif video_duration_s <= 0:
        reason = "video_duration_unknown"
    elif track_duration_s <= 0:
        reason = "track_duration_unknown"
    elif track_duration_s + _MUSIC_WINDOW_EPSILON_S < video_duration_s:
        reason = "song_shorter_than_video"
    elif not beats:
        reason = "timing_metadata_unavailable"
    preserve_available = isinstance(guided_revision, dict) or _has_linear_timeline(variant)
    return {
        "editable": reason is None,
        "preserve_available": preserve_available,
        "video_duration_s": round(video_duration_s, 3),
        "track_duration_s": round(track_duration_s, 3),
        "recommended_start_s": round(_recommended_music_start(track, video_duration_s), 3),
        "beat_timestamps_s": beats,
        "reason": reason,
        "preserve_reason": None if preserve_available else "linear_timeline_unavailable",
    }


def _freeze_music_window_timeline(
    variant: dict, source_slots: list[dict] | None = None
) -> list[dict]:
    ai_slots, user_slots, _ = _timeline_parts(variant)
    source = source_slots if source_slots is not None else user_slots or ai_slots
    if not source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "linear_timeline_unavailable"},
        )
    # Legacy variants without a stored linear timeline cannot use Preserve,
    # even if a client attempts to synthesize slots in the same request.
    if not _has_linear_timeline(variant) or not _has_linear_slots(source):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "linear_timeline_unavailable"},
        )
    frozen: list[dict] = []
    for order, slot in enumerate(source):
        copied = dict(slot)
        copied["order"] = order
        copied["duration_s"] = round(float(slot.get("duration_s") or 0.0), 3)
        copied["duration_beats"] = None
        frozen.append(copied)
    if not any(not slot.get("removed") and slot["duration_s"] > 0 for slot in frozen):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "linear_timeline_unavailable"},
        )
    return frozen


def _timeline_ineligibility(job: Job, variant: dict) -> str | None:
    """First matching reason this variant's timeline can't be edited, or None."""
    from app.config import settings  # noqa: PLC0415

    if not settings.GENERATIVE_TIMELINE_EDITOR_ENABLED:
        return "disabled"
    vid = str(variant.get("variant_id") or "")
    if (vid == "song_lyrics" or variant.get("text_mode") == "lyrics") and variant.get(
        "lyrics_baked"
    ) is not False:
        # Legacy (baked) lyric lines are timed to the RECIPE's slot layout, so
        # re-cutting clips breaks sync. A `lyrics_baked=False` variant's lyric
        # lines are ordinary TextElements timed to the continuous song audio —
        # clip re-cuts don't move the song, so they stay in sync and the
        # timeline is editable like any other variant.
        return "lyrics_sync"
    if variant.get("resolved_archetype") == "narrated":
        return "locked_to_voiceover"  # clip timing is driven by the narrated VO bed
    if vid.startswith("voiceover"):
        return "voiceover_bed_fit"  # slots are fit to the voice bed, not user cuts
    if variant.get("resolved_archetype") == "talking_head":
        return "no_slot_timeline"  # talking_head renders have no slot layout
    if is_collage_montage_preset(variant.get("montage_preset_rendered")):
        return "masonry_preset"  # collage tiles do not map to a linear slot timeline
    if vid not in _TIMELINE_EDITABLE_VARIANTS and not (
        vid == "song_lyrics" and variant.get("lyrics_baked") is False
    ):
        # `_TIMELINE_EDITABLE_VARIANTS` excludes song_lyrics for the SAME
        # underlying reason the lyrics_sync check above did — once that no
        # longer applies (lyrics_baked=False), song_lyrics is an ordinary
        # montage timeline like song_text/original_text.
        return "unsupported_variant"
    ai_slots, _, _ = _timeline_parts(variant)
    if not ai_slots:
        return "no_timeline"  # legacy variant rendered before lane-2 instrumentation
    prefix = _durable_sources_prefix(job)
    if any(not str(s.get("source_gcs_path") or "").startswith(prefix) for s in ai_slots):
        # Non-durable sources = legacy job cutting from 24h-swept uploads.
        return "sources_expired"
    return None


def _timeline_error(status_code: int, code: str, **context: object) -> HTTPException:
    detail = {"code": code}
    detail.update({key: value for key, value in context.items() if value is not None})
    return HTTPException(status_code=status_code, detail=detail)


# ── Carousel-moment editor (Blossom carousel) ───────────────────────────────
# Talking_head/narrated/subtitled render through caption/talking-head-specific
# assemblers, not the montage `steps` list `_insert_carousel_moment_step`
# splices into — a carousel moment on those is a no-op at best (see
# `_author_carousel_moments`'s montage-path eligibility rule in
# generative_build.py, which excludes archetype specs for the same reason).
# Voiceover (voiceover_only/voiceover_music) renders through the montage path
# too and IS supported for a manual/editor moment (auto-authoring still
# excludes it — see generative_build.py's `_author_carousel_moments`).
_CAROUSEL_UNSUPPORTED_ARCHETYPES = frozenset({"talking_head", "narrated", "subtitled"})
_CAROUSEL_POSITIONS = frozenset({"intro", "middle", "outro"})
_CAROUSEL_MODES = frozenset({"focus", "rolling", "stills"})
_CAROUSEL_EFFECTS = frozenset({"scale_sweep", "cover_flow", "cards_stack", "flipbook"})
_CAROUSEL_TRANSITIONS = frozenset({"crossfade", "none"})
_CAROUSEL_DURATION_MIN_S = 2.0
_CAROUSEL_DURATION_MAX_S = 15.0
_CAROUSEL_HOLD_RANGE_S = (0.5, 5.0)
_CAROUSEL_MOVE_RANGE_S = (0.2, 4.0)
_CAROUSEL_ZOOM_RANGE_S = (0.2, 2.0)
_CAROUSEL_BOUNDARY_RANGE_S = (0.1, 1.0)


def _manual_carousel_duration_s(moment: dict, active_indices: list[int]) -> float | None:
    sequence = moment.get("sequence")
    if moment.get("timing_model") != "ripple_v1" or not isinstance(sequence, list) or not sequence:
        return None
    previous = active_indices[0] if active_indices else sequence[0].get("clip_index")
    moves = 0
    holds = 0.0
    for item in sequence:
        clip_index = item.get("clip_index")
        if clip_index != previous:
            moves += 1
        previous = clip_index
        holds += float(item.get("hold_s") or 0.0)
    move_s = float(moment.get("move_duration_s") or 0.6)
    zoom_s = 0.0
    if moment.get("mode") != "rolling":
        zoom_s = len(sequence) * float(moment.get("zoom_duration_s") or 0.6) * 2
    return round(holds + moves * move_s + zoom_s, 1)


def _carousel_capability_reason(variant: dict) -> str | None:
    """Single source of truth for whether a variant can carry a carousel-moment
    edit — honest reasons shared by BOTH `dispatch_edit_variant`'s validation
    (the 422 body on an add/edit) and `_editor_capabilities`'s
    `carousel`/`carousel_reason` pair (what the editor UI gates on before the
    user ever opens the panel). `None` means eligible.
    """
    if not settings.carousel_effects_enabled:
        return "Carousel effects are disabled"
    if variant.get("resolved_archetype") in _CAROUSEL_UNSUPPORTED_ARCHETYPES:
        return "Not available for this edit type"
    if _variant_clip_count(variant) < 2:
        return "Needs at least 2 clips"
    return None


def _validate_carousel_moment_patch(raw: dict, variant: dict) -> dict:
    """Validate + field-clean one carousel-moment partial-edit dict.

    Shared by BOTH the instant-edit dispatcher (`dispatch_edit_variant`, one
    render per Done) and the batched editor-commit staging path
    (`prepare_editor_commit`, staged synchronously and rendered on Save) — the
    two callers only differ in when the merged result reaches Celery, not in
    what's a valid edit. Only called for an ADD/EDIT (a non-null `raw`);
    explicit removal (`carousel_moment=null` at the caller's tri-state level)
    skips this entirely — no eligibility check needed to clear a moment.

    Raises `HTTPException` (422) on:
      - the variant being carousel-ineligible right now (`_carousel_capability_reason`)
      - any unknown enum value / out-of-range `focus_clip_index`
      - `duration_s` explicitly set to `null` (vs. omitted, which is fine —
        tri-state at the FIELD level too)

    `duration_s` is clamped (not rejected) to `[_CAROUSEL_DURATION_MIN_S,
    _CAROUSEL_DURATION_MAX_S]`, matching the documented API contract.

    Returns a dict containing only the keys present in `raw` (a true partial —
    absent keys are NOT filled in from `variant`), ready for
    `_merge_carousel_moment_override` to merge over whatever's persisted.
    """
    reason = _carousel_capability_reason(variant)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=reason)
    cleaned_moment: dict[str, Any] = {}
    if "position" in raw:
        position_val = raw["position"]
        if position_val not in _CAROUSEL_POSITIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"`carousel_moment.position` must be one of {sorted(_CAROUSEL_POSITIONS)}."
                ),
            )
        cleaned_moment["position"] = position_val
    if "mode" in raw:
        mode_val = raw["mode"]
        if mode_val not in _CAROUSEL_MODES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"`carousel_moment.mode` must be one of {sorted(_CAROUSEL_MODES)}.",
            )
        cleaned_moment["mode"] = mode_val
    if "effect" in raw:
        carousel_effect_val = raw["effect"]
        if carousel_effect_val not in _CAROUSEL_EFFECTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(f"`carousel_moment.effect` must be one of {sorted(_CAROUSEL_EFFECTS)}."),
            )
        cleaned_moment["effect"] = carousel_effect_val
    if "focus_clip_index" in raw:
        focus_idx = raw["focus_clip_index"]
        if focus_idx is not None:
            if not isinstance(focus_idx, int) or isinstance(focus_idx, bool):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="`carousel_moment.focus_clip_index` must be an integer.",
                )
            available_indices = _variant_carousel_clip_indices(variant)
            if focus_idx not in available_indices:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "`carousel_moment.focus_clip_index` must identify an active Carousel "
                        f"video: {available_indices}."
                    ),
                )
        cleaned_moment["focus_clip_index"] = focus_idx
    if "duration_s" in raw:
        duration_val = raw["duration_s"]
        if duration_val is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`carousel_moment.duration_s` cannot be null.",
            )
        # Clamp, don't reject — matches the API contract ("clamp 2.0..15.0").
        cleaned_moment["duration_s"] = max(
            _CAROUSEL_DURATION_MIN_S, min(_CAROUSEL_DURATION_MAX_S, float(duration_val))
        )
    if "transition" in raw:
        transition_val = raw["transition"]
        if transition_val not in _CAROUSEL_TRANSITIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"`carousel_moment.transition` must be one of {sorted(_CAROUSEL_TRANSITIONS)}."
                ),
            )
        cleaned_moment["transition"] = transition_val
    if "sequence" in raw:
        sequence_val = raw["sequence"]
        if sequence_val is None:
            cleaned_moment["sequence"] = None
        else:
            if not isinstance(sequence_val, list) or not sequence_val:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="`carousel_moment.sequence` must contain at least one video.",
                )
            available_indices = _variant_carousel_clip_indices(variant)
            seen: set[int] = set()
            cleaned_sequence: list[dict[str, float | int]] = []
            for item in sequence_val:
                if not isinstance(item, dict) or set(item) != {"clip_index", "hold_s"}:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            "Each `carousel_moment.sequence` item must contain only "
                            "`clip_index` and `hold_s`."
                        ),
                    )
                clip_index = item["clip_index"]
                if (
                    not isinstance(clip_index, int)
                    or isinstance(clip_index, bool)
                    or clip_index not in available_indices
                ):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            "Sequence clip indices must be active Carousel videos: "
                            f"{available_indices}."
                        ),
                    )
                if clip_index in seen:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="`carousel_moment.sequence` clip indices must be unique.",
                    )
                try:
                    hold_s = float(item["hold_s"])
                except (TypeError, ValueError):
                    hold_s = float("nan")
                if not _CAROUSEL_HOLD_RANGE_S[0] <= hold_s <= _CAROUSEL_HOLD_RANGE_S[1]:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Sequence `hold_s` must be between 0.5 and 5 seconds.",
                    )
                seen.add(clip_index)
                cleaned_sequence.append({"clip_index": clip_index, "hold_s": hold_s})
            cleaned_moment["sequence"] = cleaned_sequence

    for field_name, valid_range in (
        ("move_duration_s", _CAROUSEL_MOVE_RANGE_S),
        ("zoom_duration_s", _CAROUSEL_ZOOM_RANGE_S),
        ("transition_in_duration_s", _CAROUSEL_BOUNDARY_RANGE_S),
        ("transition_out_duration_s", _CAROUSEL_BOUNDARY_RANGE_S),
    ):
        if field_name not in raw:
            continue
        value = raw[field_name]
        try:
            numeric_value = float(value) if value is not None else float("nan")
        except (TypeError, ValueError):
            numeric_value = float("nan")
        if not valid_range[0] <= numeric_value <= valid_range[1]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"`carousel_moment.{field_name}` must be between "
                    f"{valid_range[0]} and {valid_range[1]} seconds."
                ),
            )
        cleaned_moment[field_name] = numeric_value

    for field_name in ("transition_in", "transition_out"):
        if field_name in raw:
            value = raw[field_name]
            if value not in _CAROUSEL_TRANSITIONS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"`carousel_moment.{field_name}` must be one of "
                        f"{sorted(_CAROUSEL_TRANSITIONS)}."
                    ),
                )
            cleaned_moment[field_name] = value
    if "timing_model" in raw:
        if raw["timing_model"] != "ripple_v1":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`carousel_moment.timing_model` must be `ripple_v1`.",
            )
        cleaned_moment["timing_model"] = "ripple_v1"
    prospective = {**(variant.get("carousel_moment") or {}), **cleaned_moment}
    if prospective.get("timing_model") == "ripple_v1":
        natural_duration_s = _manual_carousel_duration_s(
            prospective, _variant_carousel_clip_indices(variant)
        )
        if natural_duration_s is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`ripple_v1` Carousel timing requires a non-empty sequence.",
            )
        if not _CAROUSEL_DURATION_MIN_S <= natural_duration_s <= _CAROUSEL_DURATION_MAX_S:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Manual Carousel phases must total between 2 and 15 seconds; "
                    f"received {natural_duration_s:.1f} seconds."
                ),
            )
        # New timing fields own the complete choreography; never retain an
        # outer duration that would pad or truncate an exact per-video hold.
        cleaned_moment["duration_s"] = natural_duration_s
    return cleaned_moment


def _editor_capabilities(job: Job, variant: dict) -> dict:
    """E4: server-derived editor capability map for one variant (kills FE 404-probing).

    Cheap by design — flag reads, string checks, and the already-persisted
    source-liveness prefix check inside `_timeline_ineligibility`. No GCS calls.
    `reason` carries the timeline-ineligibility code (the same vocabulary the GET
    /timeline endpoint reports) when timeline/split are disabled, else null.
    """
    timeline_reason = _timeline_ineligibility(job, variant)
    timeline_ok = timeline_reason is None
    archetype = variant.get("resolved_archetype")
    # Decoupled from _text_elements_allowed (#625): the styled-text lane can
    # unlock text tools while caption cue edits remain a separate caption_cues
    # section.
    caption_reason = CAPTION_TAB_COPY if archetype == "subtitled" else None
    from app.config import settings  # noqa: PLC0415

    if archetype == "guided_story":
        # A guided story is an approved, immutable editorial plan. Until an
        # operation has a story-native implementation, advertising the legacy
        # montage control is worse than hiding it: the async worker must reject
        # it to avoid dropping approved beats. TextElement reburns are the one
        # supported post-render operation because they reuse the verified clean
        # story base without reconstructing the timeline.
        reason = "guided_story_edit_unsupported"
        text_editable = _TEXT_ELEMENTS_ENABLED and _text_elements_allowed(variant)
        if getattr(settings, "guided_story_editor_v2_enabled", False):
            revision = _guided_v2_revision(job, variant)
            revision_reason = None if revision is not None else "guided_story_revision_unavailable"

            def operation(editable: bool = True, why: str | None = None) -> dict[str, Any]:
                return {
                    "editable": bool(editable and revision is not None),
                    "reason": why or revision_reason,
                }

            clips = {
                name: operation()
                for name in ("add", "remove", "reorder", "split", "trim", "transitions", "looks")
            }
            clips["transitions"] = operation(
                settings.edit_transitions_enabled,
                None if settings.edit_transitions_enabled else "transitions_disabled",
            )
            clips["edit_wide_looks"] = operation(
                settings.edit_wide_looks_enabled,
                None if settings.edit_wide_looks_enabled else "disabled",
            )
            music_operations = {
                "swap": operation(),
                "remove": operation(),
                "level": operation(),
                "window": operation(),
            }
            return {
                "text_elements": text_editable,
                "timeline": bool(revision is not None),
                "timeline_max_slots": _TIMELINE_MAX_SLOTS,
                "split_clips": bool(revision is not None),
                "clips": clips,
                "music_operations": music_operations,
                "automatic_cut": False,
                "automatic_cut_reason": "guided_story_edit_unsupported",
                "mix": False,
                "sfx": bool(settings.sound_effects_enabled),
                "sfx_reason": None if settings.sound_effects_enabled else "sound_effects_disabled",
                "overlays": bool(settings.media_overlays_enabled),
                "overlays_reason": None
                if settings.media_overlays_enabled
                else "media_overlays_disabled",
                "visual_blocks": bool(settings.visual_blocks_enabled),
                "visual_blocks_reason": None
                if settings.visual_blocks_enabled
                else "visual_blocks_disabled",
                "motion_scenes": bool(settings.motion_scenes_enabled),
                "motion_scenes_reason": None
                if settings.motion_scenes_enabled
                else "motion_scenes_disabled",
                "lanes": {
                    "sfx": operation(
                        settings.sound_effects_enabled,
                        None if settings.sound_effects_enabled else "sound_effects_disabled",
                    ),
                    "overlays": operation(
                        settings.media_overlays_enabled,
                        None if settings.media_overlays_enabled else "media_overlays_disabled",
                    ),
                    "visual_blocks": operation(
                        settings.visual_blocks_enabled,
                        None if settings.visual_blocks_enabled else "visual_blocks_disabled",
                    ),
                    "motion_scenes": operation(
                        settings.motion_scenes_enabled,
                        None if settings.motion_scenes_enabled else "motion_scenes_disabled",
                    ),
                    "text": operation(
                        text_editable, None if text_editable else "text_elements_disabled"
                    ),
                    "orientation": operation(
                        _LANDSCAPE_OUTPUT_ENABLED
                        and _orientation_unsupported_reason(variant) is None,
                        (
                            "disabled"
                            if not _LANDSCAPE_OUTPUT_ENABLED
                            else _orientation_unsupported_reason(variant)
                        ),
                    ),
                },
                "motion_runtime_hash": None,
                "evolving_type": False,
                "camera_effects": False,
                "background_music": False,
                "suggestions": False,
                "swap_song": bool(revision is not None),
                "intro_controls": False,
                "reason": revision_reason,
                "orientation": operation(
                    _LANDSCAPE_OUTPUT_ENABLED and _orientation_unsupported_reason(variant) is None,
                    (
                        "disabled"
                        if not _LANDSCAPE_OUTPUT_ENABLED
                        else _orientation_unsupported_reason(variant)
                    ),
                ),
                "text": operation(
                    text_editable, None if text_editable else "text_elements_disabled"
                ),
                "nova": {
                    "trim_clip_start": operation(),
                    "trim_output_start": operation(),
                    "remove_music": operation(),
                },
                "lyrics": {
                    "editable": False,
                    "enabled": False,
                    "can_toggle_on": False,
                    "reason": "disabled",
                    "lyrics_model": "elements",
                },
                "carousel": False,
                "carousel_reason": "guided_story_edit_unsupported",
            }
        return {
            "overlay_upload_mode": (
                "pool" if settings.reliable_overlay_uploads_enabled else "legacy"
            ),
            "text_elements": text_editable,
            "timeline": False,
            "timeline_max_slots": _TIMELINE_MAX_SLOTS,
            "split_clips": False,
            "automatic_cut": False,
            "automatic_cut_reason": reason,
            "mix": False,
            "sfx": False,
            "overlays": False,
            "visual_blocks": False,
            "motion_scenes": False,
            "motion_runtime_hash": None,
            "evolving_type": False,
            "camera_effects": False,
            "background_music": False,
            "suggestions": False,
            "swap_song": False,
            "intro_controls": False,
            "reason": reason,
            "sfx_reason": reason,
            "overlays_reason": reason,
            "visual_blocks_reason": reason,
            "motion_scenes_reason": reason,
            "camera_effects_reason": reason,
            "suggestions_reason": reason,
            "lyrics": {
                "editable": False,
                "enabled": False,
                "can_toggle_on": False,
                "reason": "disabled",
                "lyrics_model": "elements",
            },
            "orientation": {
                "editable": _LANDSCAPE_OUTPUT_ENABLED,
                "value": _variant_orientation(variant),
                "reason": None if _LANDSCAPE_OUTPUT_ENABLED else "disabled",
            },
            "carousel": False,
            "carousel_reason": reason,
        }

    from app.pipeline.motion_scene import (  # noqa: PLC0415
        COMPATIBLE_MOTION_RUNTIME_HASHES,
        LEGACY_MOTION_RUNTIME_HASH,
        MOTION_RUNTIME_HASH,
    )

    # Plan 010: caption archetypes get the manual SFX/overlay lanes — the caption
    # re-render terminals reapply persisted lanes, so effects survive caption edits.
    effects_reason = None
    if not variant.get("video_path") and not variant.get("output_url"):
        effects_reason = "no_video"
    sfx_reason = "sound_effects_disabled" if not settings.sound_effects_enabled else effects_reason
    overlays_reason = (
        "media_overlays_disabled" if not settings.media_overlays_enabled else effects_reason
    )
    if not settings.visual_blocks_enabled:
        visual_blocks_reason = "visual_blocks_disabled"
    elif variant.get("text_mode") == "lyrics":
        visual_blocks_reason = "lyrics_variant"
    elif not variant.get("base_video_path"):
        visual_blocks_reason = "no_clean_base"
    elif visual_block_variant_duration(variant) <= 0:
        visual_blocks_reason = "duration_unknown"
    else:
        visual_blocks_reason = effects_reason
    if not settings.motion_scenes_enabled:
        motion_scenes_reason = "motion_scenes_disabled"
    elif not variant.get("base_video_path"):
        motion_scenes_reason = "motion_clean_base_unavailable"
    elif visual_block_variant_duration(variant) <= 0:
        motion_scenes_reason = "duration_unknown"
    else:
        motion_scenes_reason = effects_reason
    persisted_motion_scenes = variant.get("motion_scenes") or []
    persisted_motion_hash = variant.get("motion_runtime_hash")
    if persisted_motion_scenes:
        legacy_route_only = persisted_motion_hash == LEGACY_MOTION_RUNTIME_HASH and all(
            scene.get("preset_id") == "route_trace" for scene in persisted_motion_scenes
        )
        compatible_hash = persisted_motion_hash in COMPATIBLE_MOTION_RUNTIME_HASHES
        if not compatible_hash and not legacy_route_only:
            motion_scenes_reason = "motion_runtime_mismatch"
    # AI overlay suggestions (plans 005-009): mirrors the suggest-overlays route's
    # eligibility EXCEPT the ready-asset count — that's a DB query and this map is
    # cheap-by-design; the editor's pool strip owns the empty-pool state locally.
    if not settings.overlay_autoplace_enabled:
        suggestions_reason = "autoplace_disabled"
    elif archetype in CAPTION_EDIT_ARCHETYPES:
        # OV-5: manual lanes are open on caption archetypes, but AI suggestions
        # stay off pending a speech-content quality eval. Keep in lockstep with
        # the suggest-overlays route guard (plan_items.py).
        suggestions_reason = "caption_archetype"
    elif variant.get("music_track_id") is not None or variant.get("text_mode") == "lyrics":
        suggestions_reason = "song_or_lyric_variant"
    else:
        suggestions_reason = overlays_reason
    orientation_reason = None
    if not _LANDSCAPE_OUTPUT_ENABLED:
        orientation_reason = "disabled"
    else:
        orientation_reason = _orientation_unsupported_reason(variant)
    background_music_reason = None
    if not settings.smart_music_bed_enabled:
        background_music_reason = "smart_music_bed_disabled"
    elif variant.get("music_track_id") is not None or variant.get("text_mode") == "lyrics":
        background_music_reason = "song_or_lyric_variant"
    else:
        background_music_reason = effects_reason
    carousel_reason = _carousel_capability_reason(variant)
    automatic_cut = (
        settings.retake_cut_enabled
        and archetype in {"subtitled", "talking_head"}
        and bool(variant.get("base_video_path"))
        and variant.get("render_status") != "rendering"
        and not variant.get("speech_cut_in_flight")
        and bool(variant.get("speech_cut_candidates"))
    )
    return {
        "overlay_upload_mode": ("pool" if settings.reliable_overlay_uploads_enabled else "legacy"),
        # Lyrics variants are beat-synced — same rule as dispatch_set_text_elements.
        "text_elements": (
            _TEXT_ELEMENTS_ENABLED
            and (
                variant.get("text_mode") != "lyrics"
                or (_LYRICS_EDITOR_ENABLED and _variant_lyrics_capable(variant))
            )
            and _text_elements_allowed(variant)
        ),
        "timeline": timeline_ok,
        "timeline_max_slots": _TIMELINE_MAX_SLOTS,
        # Splitting a clip is a timeline-override operation — same eligibility.
        "split_clips": timeline_ok,
        "automatic_cut": automatic_cut,
        "automatic_cut_reason": None if automatic_cut else "no_reviewable_speech_timing",
        # Mirrors dispatch_set_mix: only variants carrying a voice bed can rebalance.
        # R1-4: caption archetypes hard-code mix=1.0 at render time but have no
        # montage mix lane — narrated's real knob is the bed-level dispatch on the
        # item page; subtitled has no bed at all. Without this a mix save funnels
        # into the montage regenerate → caption reject → silent no-op "ready".
        "mix": (
            archetype not in CAPTION_EDIT_ARCHETYPES
            and (
                variant.get("mix") is not None
                or str(variant.get("variant_id") or "").startswith("voiceover")
            )
        ),
        "sfx": sfx_reason is None,
        "overlays": overlays_reason is None,
        "visual_blocks": visual_blocks_reason is None,
        "motion_scenes": motion_scenes_reason is None,
        "motion_runtime_hash": MOTION_RUNTIME_HASH if settings.motion_scenes_enabled else None,
        "evolving_type": settings.evolving_type_enabled,
        **(
            {"motion_required_runtime_hash": persisted_motion_hash}
            if persisted_motion_hash is not None
            else {}
        ),
        "camera_effects": (
            effects_reason is None
            and variant.get("resolved_archetype") == "subtitled"
            and bool(variant.get("base_video_path"))
        ),
        "background_music": background_music_reason is None,
        "suggestions": suggestions_reason is None,
        "reason": caption_reason or timeline_reason,
        "sfx_reason": sfx_reason,
        "overlays_reason": overlays_reason,
        "visual_blocks_reason": visual_blocks_reason,
        "motion_scenes_reason": motion_scenes_reason,
        "camera_effects_reason": (
            effects_reason
            if effects_reason is not None
            else (
                None
                if (
                    variant.get("resolved_archetype") == "subtitled"
                    and variant.get("base_video_path")
                )
                else (
                    "unsupported_archetype"
                    if variant.get("resolved_archetype") != "subtitled"
                    else "no_clean_base"
                )
            )
        ),
        "suggestions_reason": suggestions_reason,
        "lyrics": _lyrics_capabilities(variant),
        "orientation": {
            "editable": orientation_reason is None,
            "value": _variant_orientation(variant),
            "reason": orientation_reason,
        },
        "carousel": carousel_reason is None,
        "carousel_reason": carousel_reason,
    }


def speech_cut_director_context(job: Job, variant: dict) -> dict:
    """Authoritative prompt context; never trust the browser for candidate IDs."""
    from app.pipeline.speech_cut_state import cut_revision, public_candidates  # noqa: PLC0415

    capability = _editor_capabilities(job, variant).get("automatic_cut") is True
    return {
        "automatic_cut": capability,
        "speech_cut_revision": cut_revision(variant),
        "speech_cut_candidates": public_candidates(variant) if capability else [],
    }


def dispatch_get_timeline(job: Job, variant_id: str) -> dict:
    """Effective timeline (user_timeline if present, else ai_timeline) + clip pool.

    Read-only and side-effect free; never raises for an ineligible variant — it
    reports `editable=False` + `reason` so the frontend can render the right copy.
    """
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    if variant.get("resolved_archetype") == "guided_story" and (
        getattr(settings, "guided_story_editor_v2_enabled", False)
        or isinstance(variant.get("guided_edit_revision"), dict)
    ):
        return _guided_v2_timeline_projection(job, variant)
    reason = _timeline_ineligibility(job, variant)
    ai_slots, user_slots, beat_grid = _timeline_parts(variant)
    clip_paths = list((job.all_candidates or {}).get("clip_paths") or [])
    projected_story_duration_s: float | None = None
    # Guided stories deliberately have no editable legacy ``ai_timeline``;
    # their immutable, verified cut lives in ``story_timeline`` instead.  Still
    # project that cut into the read-only timeline response so the editor can
    # show which uploaded clips were used and where, rather than labelling the
    # entire source pool "Unused".  A guided story can be assembled entirely
    # from plan-item assets, while ``all_candidates.clip_paths`` contains only
    # the legacy primary-clip lane (or just the first compatibility path).  Its
    # authoritative clip pool must therefore include every path from the
    # verified story in story order; otherwise the editor silently projects
    # only the first beat.  Preserve any legacy pool entries so older jobs keep
    # stable clip indexes and still show their unused sources.
    if not ai_slots and variant.get("resolved_archetype") == "guided_story":
        story_moments = [
            moment for moment in (variant.get("story_timeline") or []) if isinstance(moment, dict)
        ]
        story_paths: list[str] = []
        for moment in story_moments:
            path = nonblank_str(moment.get("gcs_path"))
            if path and path not in story_paths:
                story_paths.append(path)
        clip_paths.extend(path for path in story_paths if path not in clip_paths)
        clip_index_by_path = {path: index for index, path in enumerate(clip_paths)}
        ai_slots = []
        for order, moment in enumerate(story_moments):
            path = moment.get("gcs_path")
            clip_index = clip_index_by_path.get(path)
            if clip_index is None:
                continue
            next_moment = story_moments[order + 1] if order + 1 < len(story_moments) else None
            overlap_s = 0.0
            if next_moment is not None:
                try:
                    overlap_s = max(
                        0.0,
                        float(moment.get("output_end_s"))
                        - float(next_moment.get("output_start_s")),
                    )
                except (TypeError, ValueError):
                    overlap_s = 0.0
            transition_duration_s = round(min(1.0, overlap_s), 3) if overlap_s >= 0.1 else None
            ai_slots.append(
                {
                    "slot_id": moment.get("moment_id") or f"guided-story-{order}",
                    "clip_index": clip_index,
                    "source_gcs_path": path,
                    "source_duration_s": moment.get("source_end_s"),
                    "in_s": moment.get("source_start_s", 0.0),
                    "duration_s": moment.get("duration_s"),
                    "duration_beats": None,
                    "order": order,
                    "moment_description": moment.get("topic"),
                    "removed": False,
                    "transition_after": "crossfade" if transition_duration_s else "cut",
                    "transition_duration_s": transition_duration_s,
                }
            )
        try:
            story_end_s = max(float(moment.get("output_end_s")) for moment in story_moments)
            if story_end_s > 0:
                projected_story_duration_s = story_end_s
        except (TypeError, ValueError):
            projected_story_duration_s = None
    has_user_edits = bool(user_slots)
    effective = user_slots if has_user_edits else ai_slots
    active = [s for s in effective if not s.get("removed")]
    total = _active_timeline_duration_s(active)
    if projected_story_duration_s is not None:
        total = projected_story_duration_s
    used_indices = {s.get("clip_index") for s in active}

    # Source durations are only known where the worker probed them (ai_timeline).
    dur_by_idx: dict[int, float] = {}
    for s in ai_slots:
        idx = s.get("clip_index")
        if idx is not None and s.get("source_duration_s") is not None:
            dur_by_idx.setdefault(idx, float(s["source_duration_s"]))

    clips: list[dict] = []
    for i, path in enumerate(clip_paths):
        try:
            url: str | None = signed_get_url(path, PLAYBACK_URL_TTL_MIN)
        except Exception:  # noqa: BLE001 — one bad sign must not 500 the editor open
            log.warning(
                "timeline_clip_sign_failed", job_id=str(job.id), clip_index=i, exc_info=True
            )
            url = None
        clips.append(
            {
                "clip_index": i,
                "signed_url": url,
                "duration_s": dur_by_idx.get(i),
                "used": i in used_indices,
            }
        )

    return {
        "editable": reason is None,
        "reason": reason,
        "beat_grid": beat_grid,
        "total_duration_s": round(total, 3),
        "has_user_edits": has_user_edits,
        "edit_wide_look_presets": (
            list(EDIT_WIDE_LOOK_PRESETS)
            if reason is None and settings.edit_wide_looks_enabled
            else []
        ),
        "slots": [
            {
                **dict(s),
                "look_preset": (preset := normalize_look_preset(s.get("look_preset"))),
                "look_adjustments": (
                    controls.model_dump()
                    if (
                        controls := normalize_look_adjustments(
                            preset,
                            s.get("look_adjustments"),
                        )
                    )
                    is not None
                    else None
                ),
            }
            for s in effective
        ],
        "clips": clips,
    }


def _lyric_seed_elements_from_snapshot(snapshot: list) -> list[dict]:
    """Build seed elements from an already-anchored `lyric_overlay_snapshot`.

    Preferred over recomputing from the lyrics cache whenever a snapshot
    exists (a prior baked render, or a `lyrics_baked=False` variant that once
    had lyrics injected before the flag flipped) — its `start_s`/`end_s` are
    already absolute video time. Reuses the same burn-dict-shaped → TextElement
    conversion the read-only projection uses (`_element_from_lyric_snapshot`),
    just re-keyed to the `lyr-L<n>` id format this endpoint's contract uses.
    """
    from app.agents._schemas.text_element import _element_from_lyric_snapshot  # noqa: PLC0415

    elements: list[dict] = []
    for entry in snapshot:
        if not isinstance(entry, dict):
            continue
        elem = _element_from_lyric_snapshot(entry)
        if elem is None:
            continue
        line_key = str(entry.get("line_key") or "").strip()
        dumped = elem.model_dump()
        if line_key:
            dumped["id"] = f"lyr-{line_key}"
        elements.append(dumped)
    return elements


async def dispatch_get_lyric_seeds(job: Job, variant_id: str, db: AsyncSession) -> dict:
    """Instant-materialize seed elements (lyrics-as-optional-elements editor
    toggle). Read-only, side-effect free — nothing is persisted here.

    404 when the flag is off (mirrors every other feature-flagged route in
    this file). 422 with a machine-readable `code` when the variant has no
    matched track, or the track has no renderable cached lyrics for the
    variant's section — there is nothing to seed.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.lyrics_optional_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lyrics elements are not available.",
        )
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")

    track_id = variant.get("music_track_id")
    if not track_id:
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no_matched_track",
            "This variant has no matched song to seed lyrics from.",
        )
    track = await db.get(MusicTrack, track_id)
    if track is None:
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no_matched_track",
            "This variant has no matched song to seed lyrics from.",
        )

    snapshot = variant.get("lyric_overlay_snapshot")
    if snapshot:
        elements = _lyric_seed_elements_from_snapshot(snapshot)
    else:
        from app.pipeline.lyric_injector import build_lyric_seed_elements  # noqa: PLC0415
        from app.pipeline.lyric_support import lyrics_variant_renderable  # noqa: PLC0415
        from app.services.lyrics_config_effective import (  # noqa: PLC0415
            effective_lyrics_config,
        )
        from app.services.music_sections import track_config_with_rank_one  # noqa: PLC0415

        if not lyrics_variant_renderable(track.lyrics_cached):
            raise _lyrics_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "no_renderable_lyrics",
                "This track has no renderable cached lyrics.",
            )
        cfg = track_config_with_rank_one(track)
        best_start_s = float(cfg.get("best_start_s", 0.0) or 0.0)
        best_end_s = float(cfg.get("best_end_s", 0.0) or 0.0)
        # Cheap safety clamp against this variant's own known output length —
        # avoids re-probing footage (which `_fit_section_to_footage` at render
        # time needs but a GET route shouldn't pay for) while still keeping
        # seeds from running past a short render. Best-effort only: falls
        # back to the raw rank-1 section window when the duration isn't known
        # yet (e.g. `visual_blocks_enabled` off).
        output_duration_s = visual_block_variant_duration(variant)
        if output_duration_s > 0:
            best_end_s = min(best_end_s, best_start_s + output_duration_s)
        style_set_id = variant.get("style_set_id")
        lyrics_config = (
            {"enabled": True, "style_set_id": style_set_id}
            if style_set_id
            else effective_lyrics_config(track.track_config, {"enabled": True, "style": "karaoke"})
        )
        elements = build_lyric_seed_elements(
            track.lyrics_cached, best_start_s, best_end_s, lyrics_config
        )

    if not elements:
        raise _lyrics_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no_renderable_lyrics",
            "This track has no renderable cached lyrics for this section.",
        )
    return {"elements": elements}


async def persist_user_timeline(
    db: AsyncSession,
    job_id: str,
    variant_id: str,
    slots: list[dict] | None,
    *,
    render_gen_id: str | None = None,
) -> None:
    """Row-locked merge of `user_timeline` into the variant entry; None removes it.

    Re-fetches the Job FOR UPDATE — mirrors the worker's `_update_variant_entry`
    RMW lock (generative_build.py): a concurrent `regenerate_generative_variant`
    completing on a sibling variant must not clobber this write (or vice versa).
    Reassigning a NEW `assembly_plan` dict is what marks the JSONB column dirty
    (same pattern as the worker — no flag_modified needed).
    """
    job = await db.get(Job, uuid.UUID(str(job_id)), with_for_update=True)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    plan = dict(job.assembly_plan or {})
    variants = list(plan.get("variants") or [])
    for i, v in enumerate(variants):
        if v.get("variant_id") == variant_id:
            updated = dict(v)
            if slots is None:
                updated.pop("user_timeline", None)
                duration_slots = (updated.get("ai_timeline") or {}).get("slots") or []
            else:
                updated["user_timeline"] = {"slots": slots}
                duration_slots = slots
            if updated.get("music_track_id"):
                active_duration_s = _active_timeline_duration_s(duration_slots)
                if active_duration_s > 0:
                    # Persist before enqueue so both old and new workers in a
                    # rolling deploy size recipe/audio from the edited cut.
                    updated["music_window_video_duration_s"] = active_duration_s
            if render_gen_id is not None:
                updated["render_generation_id"] = render_gen_id
                # Stamp the COPY, not `v`: `variants[i] = updated` below would
                # overwrite any write made to the original dict.
                stamp_variant_attempt(updated)
                updated["base_video_stale"] = True
                mark_reattempt(job)
            variants[i] = updated
            break
    plan["variants"] = variants
    job.assembly_plan = plan
    await db.commit()


_TIMELINE_REASSEMBLY_SENTINEL = {
    "slot_id": "__nova_timeline_reassembly__",
    "removed": True,
}


def _active_timeline_duration_s(slots: list[dict]) -> float:
    active = [slot for slot in slots if not slot.get("removed")]
    total = sum(float(slot.get("duration_s") or 0.0) for slot in active)
    if settings.edit_transitions_enabled:
        for left, right in zip(active, active[1:]):
            if str(left.get("transition_after") or "cut") == "cut":
                continue
            overlap_s = min(
                0.3,
                float(left.get("transition_duration_s") or 0.3),
                float(left.get("duration_s") or 0.0) * 0.3,
                float(right.get("duration_s") or 0.0) * 0.3,
            )
            if overlap_s >= 0.1:
                total -= overlap_s
    return round(max(0.0, total), 3)


def _guided_v2_revision(job: Job, variant: dict) -> dict[str, Any] | None:
    """Read the active revision or derive a non-persisted initial projection."""

    assembly = job.assembly_plan or {}
    guided_snapshot = assembly.get("guided_edit")
    execution_plan = assembly.get("guided_story_execution_plan")
    if not isinstance(guided_snapshot, dict) or not isinstance(execution_plan, dict):
        return None
    persisted = variant.get("guided_edit_revision")
    if isinstance(persisted, dict):
        try:
            return normalize_guided_editor_revision(
                persisted,
                expected_approval_version=int(guided_snapshot.get("proposal_version") or 0),
                expected_media_digest=str(guided_snapshot.get("media_digest") or ""),
            )
        except ValueError:
            # A corrupt/stale revision must not make the read endpoint 500. The
            # immutable approval remains the safe projection until the client
            # refreshes and saves a new revision.
            log.warning("guided_editor_revision_invalid", job_id=str(job.id), exc_info=True)
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryError,
        validate_guided_snapshot,
    )

    try:
        _version, _digest, snapshot = validate_guided_snapshot(guided_snapshot)
    except GuidedStoryError:
        log.warning("guided_editor_approval_projection_failed", job_id=str(job.id), exc_info=True)
        return None
    try:
        return guided_editor_revision_from_approval(
            proposal_version=int(guided_snapshot.get("proposal_version") or 0),
            media_digest=str(guided_snapshot.get("media_digest") or ""),
            snapshot=snapshot.model_dump(mode="json"),
            execution_plan=execution_plan,
            base_generation=variant_render_baseline(variant),
        )
    except (TypeError, ValueError):
        log.warning("guided_editor_revision_projection_failed", job_id=str(job.id), exc_info=True)
        return None


def _guided_v2_timeline_projection(job: Job, variant: dict) -> dict:
    revision = _guided_v2_revision(job, variant)
    if revision is None:
        return {
            "editable": False,
            "reason": "guided_story_revision_unavailable",
            "beat_grid": [],
            "total_duration_s": 0.0,
            "has_user_edits": False,
            "slots": [],
            "clips": [],
            "edit_wide_look_presets": [],
        }
    sources = list(revision.get("sources") or [])
    source_index = {str(source.get("media_id")): index for index, source in enumerate(sources)}
    used_media = {str(segment.get("media_id")) for segment in revision.get("segments") or []}
    clips: list[dict] = []
    for index, source in enumerate(sources):
        path = str(source.get("gcs_path") or "")
        try:
            url = signed_get_url(path, PLAYBACK_URL_TTL_MIN)
        except Exception:  # noqa: BLE001
            url = None
        clips.append(
            {
                "clip_index": index,
                "signed_url": url,
                "duration_s": source.get("duration_s"),
                "used": str(source.get("media_id")) in used_media,
                "media_id": source.get("media_id"),
                "generation": source.get("generation"),
                "kind": source.get("kind"),
            }
        )
    slots: list[dict] = []
    for order, segment in enumerate(revision.get("segments") or []):
        media_id = str(segment.get("media_id"))
        source = sources[source_index[media_id]] if media_id in source_index else {}
        slots.append(
            {
                "slot_id": segment.get("segment_id"),
                "segment_id": segment.get("segment_id"),
                "parent_segment_id": segment.get("parent_segment_id"),
                "clip_index": source_index.get(media_id),
                "source_gcs_path": source.get("gcs_path"),
                "source_duration_s": source.get("duration_s"),
                "in_s": segment.get("source_start_s"),
                "duration_s": segment.get("duration_s"),
                "duration_beats": None,
                "output_start_s": segment.get("output_start_s"),
                "output_end_s": segment.get("output_end_s"),
                "order": order,
                "removed": False,
                "transition_after": segment.get("transition_after", "cut"),
                "transition_duration_s": segment.get("transition_duration_s"),
                "look_preset": normalize_look_preset(segment.get("look_preset")),
                "look_adjustments": segment.get("look_adjustments"),
            }
        )
    total = max((float(row.get("output_end_s") or 0.0) for row in slots), default=0.0)
    writable = bool(getattr(settings, "guided_story_editor_v2_enabled", False))
    return {
        "editable": writable,
        "reason": None if writable else "disabled",
        "beat_grid": [],
        "total_duration_s": round(total, 3),
        "has_user_edits": bool(variant.get("guided_edit_revision")),
        "slots": slots,
        "clips": clips,
        "edit_wide_look_presets": list(EDIT_WIDE_LOOK_PRESETS)
        if settings.edit_wide_looks_enabled
        else [],
        "look_presets": sorted(LOOK_PRESETS),
        "revision_number": revision.get("revision_number"),
        "revision_hash": revision.get("state_hash") or guided_editor_state_hash(revision),
        "base_generation": variant_render_baseline(variant),
        "source_pool": sources,
        "tombstones": list(revision.get("tombstones") or []),
    }


async def _require_current_guided_story_sources(db: AsyncSession, job: Job) -> None:
    """Fail a story-native write before mutation when approval media drifted."""

    if db is None or job.content_plan_item_id is None:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "guided_story_source_stale")
    item = await db.get(PlanItem, job.content_plan_item_id)
    if item is None or item.user_id != job.user_id:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "guided_story_source_stale")
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryError,
        validate_guided_snapshot,
    )
    from app.routes.plan_items import _proposal_media_is_current  # noqa: PLC0415

    try:
        _version, _digest, snapshot = validate_guided_snapshot(
            (job.assembly_plan or {}).get("guided_edit")
        )
    except GuidedStoryError as exc:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code) from exc
    if not await _proposal_media_is_current(item, snapshot, db, user_id=job.user_id):
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "guided_story_source_stale")


def _timeline_override_for_reassembly(slots: list[dict]) -> list[dict]:
    """Mark an override as a real cut edit without changing the task signature.

    The route persists timeline slots before enqueueing, so a worker comparing
    the override to stored state would otherwise see them as identical and may
    take the preserve-cuts audio-only shortcut. A removed sentinel makes that
    comparison differ while being discarded before assembly. Keeping the signal
    inside the existing payload is safe during rolling deploys: older workers
    also reject the audio-only shortcut and ignore the removed slot.
    """
    return [*(dict(slot) for slot in slots), dict(_TIMELINE_REASSEMBLY_SENTINEL)]


def resolve_timeline_slots_for_edit(
    job: Job, variant: dict, slots: list[TimelineSlotEdit]
) -> list[dict]:
    """Validate a posted slot list against this variant → resolved slot dicts.

    Single-sourced timeline SECTION validation shared by POST /timeline and the
    transactional editor commit (E2): eligibility (422 with the reason code),
    stale slot ids (409 TIMELINE_STALE), beat-grid window math, bounds / floor /
    ceiling checks, and a hard existence check on every durable source. Raises
    HTTPException on any violation; never writes.
    """
    reason = _timeline_ineligibility(job, variant)
    if reason is not None:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, reason)

    ai_slots, user_slots, beat_grid = _timeline_parts(variant)

    # STALE: a posted slot_id the server doesn't know means the client edited
    # against an outdated timeline (e.g. a sibling tab re-rendered) — reject the
    # whole edit rather than guess at intent.
    known_ids = {s.get("slot_id") for s in [*ai_slots, *user_slots] if s.get("slot_id")}
    for e in slots:
        if e.slot_id is not None and e.slot_id not in known_ids:
            raise _timeline_error(status.HTTP_409_CONFLICT, "TIMELINE_STALE")

    clip_paths = list((job.all_candidates or {}).get("clip_paths") or [])
    if not any(not e.removed for e in slots):
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_EMPTY")
    for e in slots:
        if e.clip_index < 0 or e.clip_index >= len(clip_paths):
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_UNKNOWN_CLIP")

    # Per-clip lookups from the AI timeline (durable source path, probed duration,
    # moment metadata). Prior user slots only contribute paths/durations they
    # inherited — ai entries win via setdefault-first ordering.
    src_dur_by_idx: dict[int, float] = {}
    path_by_idx: dict[int, str] = {}
    meta_by_idx: dict[int, dict] = {}
    for is_ai, s in [*((True, s) for s in ai_slots), *((False, s) for s in user_slots)]:
        idx = s.get("clip_index")
        if idx is None:
            continue
        if s.get("source_duration_s") is not None:
            src_dur_by_idx.setdefault(idx, float(s["source_duration_s"]))
        if s.get("source_gcs_path"):
            path_by_idx.setdefault(idx, str(s["source_gcs_path"]))
        if is_ai:
            meta_by_idx.setdefault(idx, s)

    # Baseline windows (current user_timeline if present, else ai_timeline) keyed
    # by slot_id so untouched worker-authored windows remain byte-stable.
    baseline_by_id = {
        s.get("slot_id"): s for s in (user_slots if user_slots else ai_slots) if s.get("slot_id")
    }

    def _window_changed(e: TimelineSlotEdit) -> bool:
        # Compares the POSTED knobs (in_s + duration_beats, or in_s + duration_s
        # for seconds slots) — NOT grid-derived seconds, which legitimately
        # drift from the stored duration by up to the worker's 0.05s beat-span
        # tolerance and would falsely flag untouched slots as edited.
        base = baseline_by_id.get(e.slot_id)
        if base is None:
            return True  # new slot — always a user choice
        if base.get("duration_beats") != e.duration_beats:
            return True
        base_in = base.get("in_s")
        if base_in is None or abs(float(base_in) - e.in_s) > 1e-6:
            return True
        if e.duration_beats is None:
            base_dur = base.get("duration_s")
            if base_dur is None or e.duration_s is None:
                return True
            return abs(float(base_dur) - float(e.duration_s)) > 1e-6
        return False

    resolved: list[dict] = []
    grid_offset = 0  # cumulative beat cursor — grids are NOT uniform
    total = 0.0
    is_song_variant = bool(beat_grid) and bool(variant.get("music_track_id"))
    final_active_order = next(
        (order for order in range(len(slots) - 1, -1, -1) if not slots[order].removed),
        -1,
    )

    def _nearest_beat_count(offset: int, target_s: float | None) -> int:
        max_beats = len(beat_grid) - 1 - offset
        if max_beats < 1:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_BEATS_EXHAUSTED")
        if target_s is None or target_s <= 0:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_INVALID_DURATION")
        return min(
            range(1, max_beats + 1),
            key=lambda beats: abs((beat_grid[offset + beats] - beat_grid[offset]) - target_s),
        )

    def _largest_beat_count_fitting_source(offset: int, max_source_window_s: float) -> int:
        max_beats = len(beat_grid) - 1 - offset
        if max_beats < 1:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_BEATS_EXHAUSTED")
        start = beat_grid[offset]
        for beats in range(max_beats, 0, -1):
            duration = beat_grid[offset + beats] - start
            if duration <= max_source_window_s + 1e-6:
                return beats
        return 0

    def _snap_half_second(duration_s: float | None) -> float:
        if duration_s is None or duration_s <= 0:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_INVALID_DURATION")
        return round(max(0.5, round(float(duration_s) * 2) / 2), 3)

    def _out_of_bounds(
        e: TimelineSlotEdit,
        order: int,
        src_dur: float | None,
        required_duration_s: float | None,
        *,
        reason: str = "source_window_too_short",
        minimum_beat_duration_s: float | None = None,
    ) -> HTTPException:
        available_duration_s = None if src_dur is None else max(0.0, float(src_dur) - float(e.in_s))
        return _timeline_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "TIMELINE_OUT_OF_BOUNDS",
            reason=reason,
            slot_id=e.slot_id,
            slot_order=order,
            clip_index=e.clip_index,
            in_s=round(float(e.in_s), 3),
            source_duration_s=round(float(src_dur), 3) if src_dur is not None else None,
            available_duration_s=(
                round(available_duration_s, 3) if available_duration_s is not None else None
            ),
            required_duration_s=(
                round(float(required_duration_s), 3) if required_duration_s is not None else None
            ),
            minimum_beat_duration_s=(
                round(float(minimum_beat_duration_s), 3)
                if minimum_beat_duration_s is not None
                else None
            ),
        )

    visible_order = 0
    for order, e in enumerate(slots):
        window_changed = _window_changed(e)
        duration_s = e.duration_s
        duration_beats = e.duration_beats
        src_dur = src_dur_by_idx.get(e.clip_index)
        max_source_window_s = None if src_dur is None else max(0.0, src_dur - e.in_s)
        if not e.removed:
            if (
                beat_grid
                and duration_beats is None
                and not window_changed
                and e.duration_s is not None
                and e.duration_s > 0
            ):
                # Some AI song timelines contain footage-trimmed seconds slots
                # inside a beat-gridded edit. An untouched round-trip must keep
                # those exact windows and must not consume beat cursor.
                duration_s = float(e.duration_s)
                duration_beats = None
            elif beat_grid and duration_beats is not None and duration_beats >= 1:
                # Explicit beat slot: walk the REAL grid cumulatively. Slot i's
                # duration is grid[offset+beats] - grid[offset]; the offset then
                # advances, so the same beat count can yield different seconds at
                # different positions (non-uniform grids).
                end = grid_offset + duration_beats
                if end > len(beat_grid) - 1:
                    raise _timeline_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_BEATS_EXHAUSTED"
                    )
                duration_s = beat_grid[end] - beat_grid[grid_offset]
                # Footage-overflow handling is three-way:
                #  - UNTOUCHED slot whose recomputed span differs from its stored
                #    one: an upstream delete/edit shifted this slot's cumulative
                #    grid_offset on the non-uniform grid — reclamp to fit, because
                #    deleting a clip must never fail the save (the worker never
                #    overflows footage; it trims to the real window).
                #  - UNTOUCHED slot whose recomputed span EQUALS its stored one:
                #    a legacy timeline whose saved window already exceeded the
                #    probed source. The worker clamps these at render; an
                #    unrelated save must round-trip them unchanged, not 422.
                #  - USER-CHANGED slot that overflows: fall through to the final
                #    bounds check, which rejects with TIMELINE_OUT_OF_BOUNDS
                #    (readable copy), not a TOO_SHORT reclamp failure.
                overflows_footage = (
                    max_source_window_s is not None and duration_s > max_source_window_s + 1e-6
                )
                # Compare against the SAVED baseline span (the edit payload may
                # post beats without duration_s); tolerance matches the worker's
                # 0.05s beat-span drift allowance in _window_changed's rationale.
                _base_span = (baseline_by_id.get(e.slot_id) or {}).get("duration_s")
                legacy_unshifted = (
                    not window_changed
                    and _base_span is not None
                    and abs(duration_s - float(_base_span)) <= 5e-2
                )
                if overflows_footage and not window_changed and not legacy_unshifted:
                    duration_beats = _largest_beat_count_fitting_source(
                        grid_offset, max_source_window_s
                    )
                    if duration_beats < 1:
                        minimum_beat_duration_s = (
                            beat_grid[grid_offset + 1] - beat_grid[grid_offset]
                        )
                        raise _out_of_bounds(
                            e,
                            visible_order,
                            src_dur,
                            duration_s,
                            minimum_beat_duration_s=minimum_beat_duration_s,
                        )
                    end = grid_offset + duration_beats
                    duration_s = beat_grid[end] - beat_grid[grid_offset]
                grid_offset = end
            elif is_song_variant:
                remaining_beat_span_s = beat_grid[-1] - beat_grid[grid_offset]
                exact_terminal_tail = (
                    order == final_active_order
                    and window_changed
                    and duration_s is not None
                    and duration_s > 0
                    and duration_s > remaining_beat_span_s + 1e-6
                )
                if exact_terminal_tail:
                    # Internal cuts stay on beats, but a song grid can end a few
                    # frames before the source. The final visible endpoint has no
                    # downstream cut to align, so preserve the user's exact tail
                    # instead of silently shortening it to the last natural beat.
                    duration_s = float(duration_s)
                    duration_beats = None
                else:
                    duration_beats = _nearest_beat_count(grid_offset, duration_s)
                    # Beat slot: walk the REAL grid cumulatively. Slot i's duration is
                    # grid[offset+beats] - grid[offset]; the offset then advances, so the
                    # same `duration_beats` can yield different seconds at different
                    # positions (non-uniform grids).
                    end = grid_offset + duration_beats
                    if end > len(beat_grid) - 1:
                        raise _timeline_error(
                            status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_BEATS_EXHAUSTED"
                        )
                    duration_s = beat_grid[end] - beat_grid[grid_offset]
                    # The nearest beat count can round UP past the clip's remaining
                    # footage; reclamp to the largest span that still fits (mirrors
                    # the explicit-beat-slot branch above).
                    if max_source_window_s is not None and duration_s > max_source_window_s + 1e-6:
                        duration_beats = _largest_beat_count_fitting_source(
                            grid_offset, max_source_window_s
                        )
                        if duration_beats < 1:
                            minimum_beat_duration_s = (
                                beat_grid[grid_offset + 1] - beat_grid[grid_offset]
                            )
                            raise _out_of_bounds(
                                e,
                                visible_order,
                                src_dur,
                                duration_s,
                                minimum_beat_duration_s=minimum_beat_duration_s,
                            )
                        end = grid_offset + duration_beats
                        duration_s = beat_grid[end] - beat_grid[grid_offset]
                    grid_offset = end
            elif e.duration_s is not None and e.duration_s > 0:
                # No-music variants snap to 0.5s steps server-side. The editor may
                # send drag-derived floats; persisted/rendered state is the snapped
                # value so the next GET mirrors what will bake. A raw GET -> POST
                # round-trip can carry the AI's original non-stepped timings; keep
                # unchanged slots byte-stable so Save does not dirty a baseline.
                duration_s = (
                    _snap_half_second(e.duration_s) if window_changed else float(e.duration_s)
                )
                duration_beats = None
            else:
                # Neither a usable beat count nor a usable duration.
                raise _timeline_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_INVALID_DURATION"
                )
            total += duration_s
            # Bounds against the probed source duration. New clips the AI never
            # probed have no known duration — skip; the worker's probe will clamp.
            # Older saved timelines can already exceed the source by a few frames;
            # the worker clamps those unchanged windows, so only newly changed
            # source windows should hard-fail here.
            if e.in_s < 0:
                raise _out_of_bounds(
                    e,
                    visible_order,
                    src_dur,
                    duration_s,
                    reason="negative_in_point",
                )
            if window_changed and src_dur is not None and e.in_s + duration_s > src_dur + 1e-6:
                raise _out_of_bounds(e, visible_order, src_dur, duration_s)
            visible_order += 1
        meta = meta_by_idx.get(e.clip_index) or {}
        baseline = baseline_by_id.get(e.slot_id) or {}
        baseline_preset = normalize_look_preset(baseline.get("look_preset"))
        if e.look_preset is None:
            look_preset = normalize_look_preset(baseline.get("look_preset"))
        else:
            look_preset = e.look_preset
        if (
            look_preset in EDIT_WIDE_LOOK_PRESETS[1:]
            and not settings.edit_wide_looks_enabled
            and look_preset != baseline_preset
        ):
            raise _timeline_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "LOOK_PRESET_NOT_AVAILABLE",
                look_preset=look_preset,
            )
        if "look_adjustments" in e.model_fields_set:
            raw_look_adjustments: object = e.look_adjustments
        elif look_preset == baseline_preset:
            raw_look_adjustments = baseline.get("look_adjustments")
        else:
            raw_look_adjustments = None
        look_adjustments_model = normalize_look_adjustments(
            look_preset,
            raw_look_adjustments,
        )
        look_adjustments = (
            look_adjustments_model.model_dump() if look_adjustments_model is not None else None
        )
        resolved.append(
            {
                "slot_id": e.slot_id or str(uuid.uuid4()),
                "clip_index": e.clip_index,
                # Pool-path fallback only for clips with no durable source yet —
                # the worker re-resolves by clip_index either way.
                "source_gcs_path": path_by_idx.get(e.clip_index) or clip_paths[e.clip_index],
                "source_duration_s": src_dur_by_idx.get(e.clip_index),
                "in_s": float(e.in_s),
                "duration_s": round(float(duration_s), 3) if duration_s is not None else None,
                "duration_beats": duration_beats,
                "order": order,
                "moment_energy": meta.get("moment_energy"),
                "moment_description": meta.get("moment_description"),
                "removed": bool(e.removed),
                "look_preset": look_preset,
                "look_adjustments": look_adjustments,
                "transition_after": (
                    e.transition_after if settings.edit_transitions_enabled else "cut"
                ),
                "transition_duration_s": (
                    e.transition_duration_s or 0.3
                    if settings.edit_transitions_enabled and e.transition_after != "cut"
                    else None
                ),
            }
        )
    if total > TIMELINE_MAX_TOTAL_S + 1e-6:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_TOO_LONG")

    # A transition belongs to the boundary after its source slot. Clamp each
    # active boundary against both adjacent resolved clips so preview and the
    # final FFmpeg graph share the same duration contract.
    active_indices = [index for index, slot in enumerate(resolved) if not slot["removed"]]
    for left_index, right_index in zip(active_indices, active_indices[1:]):
        left = resolved[left_index]
        right = resolved[right_index]
        transition = str(left.get("transition_after") or "cut")
        if transition == "cut":
            left["transition_duration_s"] = None
            continue
        max_duration_s = min(
            0.3,
            float(left.get("duration_s") or 0.0) * 0.3,
            float(right.get("duration_s") or 0.0) * 0.3,
        )
        if max_duration_s < 0.1:
            left["transition_after"] = "cut"
            left["transition_duration_s"] = None
            continue
        left["transition_duration_s"] = round(
            min(float(left.get("transition_duration_s") or 0.3), max_duration_s),
            3,
        )

    # Hard existence check on every durable source we're about to cut from — a
    # manually deleted blob must fail HERE, not 12 minutes into a worker render.
    prefix = _durable_sources_prefix(job)
    durable_refs = {
        str(s["source_gcs_path"])
        for s in resolved
        if not s["removed"] and str(s["source_gcs_path"] or "").startswith(prefix)
    }
    for path in sorted(durable_refs):
        if not storage.object_exists(path):
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "sources_expired")

    return resolved


async def dispatch_edit_timeline(
    job: Job, variant_id: str, payload: TimelineEditRequest, *, db: AsyncSession
) -> None:
    """Validate a user timeline, persist it (row-locked), then enqueue the re-render.

    Persist FIRST, enqueue second: a worker that picks the task up instantly must
    always observe the committed `user_timeline` (the override travels with the
    task too, but the persisted copy is what survives retries + the GET merge).
    """
    from app.config import settings  # noqa: PLC0415

    candidate_variant = _find_variant(job, variant_id)
    if candidate_variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    guided_v2 = candidate_variant.get("resolved_archetype") == "guided_story" and getattr(
        settings, "guided_story_editor_v2_enabled", False
    )
    if not guided_v2 and not settings.GENERATIVE_TIMELINE_EDITOR_ENABLED:
        raise _timeline_error(status.HTTP_403_FORBIDDEN, "disabled")
    if guided_v2:
        variant = require_editable_variant(job, variant_id, allow_guided_text=True)
    else:
        variant = require_editable_variant(job, variant_id)
    if variant.get("resolved_archetype") == "guided_story" and getattr(
        settings, "guided_story_editor_v2_enabled", False
    ):
        revision = _guided_v2_revision_for_write(job, variant, payload)
        await _require_current_guided_story_sources(db, job)
        render_gen_id = uuid.uuid4().hex
        plan = dict(job.assembly_plan or {})
        variants = list(plan.get("variants") or [])
        for index, current in enumerate(variants):
            if current.get("variant_id") != variant_id:
                continue
            updated = dict(current)
            updated["guided_edit_revision"] = revision
            updated["render_generation_id"] = render_gen_id
            updated["base_video_stale"] = True
            stamp_variant_attempt(updated)
            variants[index] = updated
            break
        plan["variants"] = variants
        job.assembly_plan = plan
        mark_reattempt(job)
        await db.commit()
        from app.tasks.generative_build import regenerate_generative_variant

        regenerate_generative_variant.delay(
            str(job.id),
            variant_id,
            guided_revision=revision,
            render_gen_id=render_gen_id,
            force_full_render=True,
        )
        return
    # A timeline re-render re-cuts from the shared per-job sources; let any in-flight
    # sibling render finish first so two renders never race the same job row.
    if any(v.get("render_status") == "rendering" for v in _variants_of(job)):
        raise _timeline_error(status.HTTP_409_CONFLICT, "JOB_BUSY")

    resolved = resolve_timeline_slots_for_edit(job, variant, payload.slots)

    render_gen_id = uuid.uuid4().hex
    await persist_user_timeline(
        db,
        str(job.id),
        variant_id,
        resolved,
        render_gen_id=render_gen_id,
    )

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(
        str(job.id),
        variant_id,
        timeline_override=_timeline_override_for_reassembly(resolved),
        render_gen_id=render_gen_id,
    )


def _guided_v2_revision_for_write(
    job: Job, variant: dict, payload: TimelineEditRequest
) -> dict[str, Any]:
    """Validate a complete guided revision or project legacy slots into one."""

    current = _guided_v2_revision(job, variant)
    if current is None:
        raise _timeline_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "guided_story_revision_unavailable"
        )
    if payload.revision_number is None:
        raise _timeline_error(status.HTTP_409_CONFLICT, "GUIDED_REVISION_TOKEN_REQUIRED")
    if payload.revision_number is not None and payload.revision_number != int(
        current["revision_number"]
    ):
        raise _timeline_error(status.HTTP_409_CONFLICT, "GUIDED_REVISION_STALE")
    if payload.guided_revision is not None:
        raise _timeline_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "GUIDED_REVISION_WIRE_UNSUPPORTED"
        )
    if payload.base_generation is None:
        raise _timeline_error(status.HTTP_409_CONFLICT, "GUIDED_REVISION_TOKEN_REQUIRED")
    if payload.base_generation != variant_render_baseline(variant):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="baseline_conflict")

    sources = list(current.get("sources") or [])
    if not payload.slots:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_EMPTY")
    active = [slot for slot in payload.slots if not slot.removed]
    if not active:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_EMPTY")
    if len(active) > _TIMELINE_MAX_SLOTS:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_TOO_LONG")
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    source_by_index = {index: source for index, source in enumerate(sources)}
    slot_durations = [float(slot.duration_s or 0.0) for slot in active]
    effective_transitions: list[tuple[str, float]] = []
    for index, slot in enumerate(active):
        if not settings.edit_transitions_enabled and slot.transition_after != "cut":
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "transitions_disabled")
        transition = slot.transition_after if settings.edit_transitions_enabled else "cut"
        requested = float(slot.transition_duration_s or 0.0) if transition != "cut" else 0.0
        if index == len(active) - 1:
            effective_transitions.append(("cut", 0.0))
            continue
        overlap = min(requested, 0.3, slot_durations[index] * 0.3, slot_durations[index + 1] * 0.3)
        effective_transitions.append(
            (transition, round(overlap, 3)) if overlap >= 0.1 else ("cut", 0.0)
        )
    current_segment_by_id = {
        str(segment.get("segment_id")): segment
        for segment in current.get("segments") or []
        if isinstance(segment, dict) and segment.get("segment_id")
    }
    active_slot_by_id = {str(slot.slot_id): slot for slot in active if slot.slot_id}
    for order, slot in enumerate(active):
        source = source_by_index.get(slot.clip_index)
        if source is None:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_UNKNOWN_CLIP")
        if slot.parent_segment_id:
            if source.get("kind") == "image":
                raise _timeline_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_IMAGE_SPLIT_UNSUPPORTED"
                )
            parent_id = str(slot.parent_segment_id)
            persisted_parent = current_segment_by_id.get(parent_id)
            draft_parent = active_slot_by_id.get(parent_id)
            parent_matches = (
                persisted_parent is not None
                and str(persisted_parent.get("media_id")) == str(source.get("media_id"))
            ) or (
                draft_parent is not None
                and draft_parent is not slot
                and draft_parent.clip_index == slot.clip_index
            )
            if not parent_matches:
                raise _timeline_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_INVALID_PARENT"
                )
        duration = float(slot.duration_s or 0.0)
        if duration < 0.1:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_INVALID_DURATION")
        source_duration = source.get("duration_s")
        if source.get("kind") == "video" and source_duration is not None:
            if slot.in_s < 0 or slot.in_s + duration > float(source_duration) + 0.05:
                raise _timeline_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_SOURCE_BOUNDS"
                )
        if order:
            cursor = max(0.0, cursor - effective_transitions[order - 1][1])
        transition, transition_duration = effective_transitions[order]
        segments.append(
            {
                "segment_id": slot.slot_id or uuid.uuid4().hex,
                "parent_segment_id": slot.parent_segment_id,
                "media_id": source["media_id"],
                "source_start_s": float(slot.in_s),
                "source_end_s": float(slot.in_s) + duration,
                "duration_s": round(duration, 3),
                "transition_after": transition,
                "transition_duration_s": transition_duration,
                "look_preset": slot.look_preset or "none",
                "look_adjustments": slot.look_adjustments.model_dump()
                if slot.look_adjustments
                else None,
                "output_start_s": round(cursor, 3),
            }
        )
        cursor += duration
    if cursor > 60.0 + 1e-6:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_TOO_LONG")
    raw = {
        **current,
        "revision_number": int(current["revision_number"]) + 1,
        "base_generation": variant_render_baseline(variant),
        "segments": segments,
    }
    raw["state_hash"] = ""
    try:
        return normalize_guided_editor_revision(
            raw,
            expected_approval_version=int(current["approval_proposal_version"]),
            expected_media_digest=str(current["approval_media_digest"]),
        )
    except ValueError as exc:
        raise _timeline_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "GUIDED_REVISION_INVALID"
        ) from exc


def _project_guided_revision_lanes(
    raw: dict[str, Any],
    *,
    old_segments: list[dict[str, Any]],
    new_segments: list[dict[str, Any]],
) -> None:
    """Project baseline-clock lane records through one structural edit.

    The browser keeps authored lane times in the baseline coordinate system.
    This mapper resolves each endpoint through media/source time, which makes
    trims, split, repeated sources, deletion, and reorder deterministic. Exact
    overlap boundaries are right-biased by iterating later segments first.
    """

    fps = 30.0

    def window(segment: dict[str, Any]) -> tuple[float, float, float, float]:
        output_start = float(segment.get("output_start_s") or 0.0)
        duration = float(segment.get("duration_s") or 0.0)
        output_end = float(segment.get("output_end_s") or output_start + duration)
        source_start = float(segment.get("source_start_s") or 0.0)
        source_end = float(segment.get("source_end_s") or source_start + duration)
        return output_start, output_end, source_start, source_end

    def old_anchor(time_s: float) -> tuple[dict[str, Any], float] | None:
        for segment in reversed(old_segments):
            output_start, output_end, source_start, source_end = window(segment)
            if output_start <= time_s < output_end:
                source_time = min(source_end, source_start + max(0.0, time_s - output_start))
                return segment, source_time
        if old_segments and abs(time_s - window(old_segments[-1])[1]) <= 1e-6:
            segment = old_segments[-1]
            return segment, window(segment)[3]
        return None

    def project(time_s: float) -> tuple[float, str] | None:
        anchored = old_anchor(time_s)
        if anchored is None:
            return None
        old_segment, source_time = anchored
        old_segment_id = str(old_segment.get("segment_id") or "")
        if old_segment_id:
            candidates = [
                segment
                for segment in new_segments
                if str(segment.get("segment_id") or "") == old_segment_id
                or str(segment.get("parent_segment_id") or "") == old_segment_id
            ]
        else:
            candidates = [
                segment
                for segment in new_segments
                if str(segment.get("media_id")) == str(old_segment.get("media_id"))
            ]
        if not candidates:
            return None
        containing = [
            segment
            for segment in candidates
            if window(segment)[2] - 1e-6 <= source_time <= window(segment)[3] + 1e-6
        ]
        segment = (
            containing[-1]
            if containing
            else min(
                candidates,
                key=lambda candidate: min(
                    abs(source_time - window(candidate)[2]),
                    abs(source_time - window(candidate)[3]),
                ),
            )
        )
        output_start, output_end, source_start, source_end = window(segment)
        clamped_source = min(source_end, max(source_start, source_time))
        projected = min(output_end, max(output_start, output_start + clamped_source - source_start))
        return round(round(projected * fps) / fps, 6), str(segment["segment_id"])

    tombstones = list(raw.get("tombstones") or [])
    lane_time_fields = {
        "text_elements": ("start_s", "end_s"),
        "sound_effects": ("at_s", "end_s"),
        "media_overlays": ("start_s", "end_s"),
        "visual_blocks": ("start_s", "end_s"),
    }
    for lane, (start_field, end_field) in lane_time_fields.items():
        projected_values: list[dict[str, Any]] = []
        for value in raw.get(lane) or []:
            if not isinstance(value, dict) or start_field not in value:
                projected_values.append(value)
                continue
            start = project(float(value[start_field]))
            end = project(float(value.get(end_field, value[start_field])))
            if start is None and end is None:
                tombstones.append(
                    {
                        "lane": lane,
                        "record_id": str(value.get("id") or ""),
                        "segment_id": value.get("segment_id"),
                        "reason": "anchored_interval_removed",
                        "record": value,
                    }
                )
                continue
            start = start or end
            end = end or start
            assert start is not None and end is not None
            updated = dict(value)
            updated[start_field] = start[0]
            updated[end_field] = max(start[0], end[0])
            updated["segment_id"] = start[1]
            projected_values.append(updated)
        raw[lane] = projected_values

    projected_motion: list[dict[str, Any]] = []
    for value in raw.get("motion_scenes") or []:
        if not isinstance(value, dict) or "start_frame" not in value:
            projected_motion.append(value)
            continue
        start = project(float(value["start_frame"]) / fps)
        end = project(float(value.get("end_frame_exclusive", value["start_frame"])) / fps)
        if start is None and end is None:
            tombstones.append(
                {
                    "lane": "motion_scenes",
                    "record_id": str(value.get("id") or ""),
                    "segment_id": value.get("segment_id"),
                    "reason": "anchored_interval_removed",
                    "record": value,
                }
            )
            continue
        start = start or end
        end = end or start
        assert start is not None and end is not None
        updated = dict(value)
        updated["start_frame"] = round(start[0] * fps)
        updated["end_frame_exclusive"] = max(updated["start_frame"] + 1, round(end[0] * fps))
        updated["segment_id"] = start[1]
        projected_motion.append(updated)
    raw["motion_scenes"] = projected_motion
    raw["tombstones"] = tombstones[-200:]


def _guided_v2_revision_from_commit(
    job: Job,
    variant: dict,
    payload: EditorCommitRequest,
    *,
    updated: dict,
    music_track: MusicTrack | None,
    music_track_generation: str | None,
    text_elements: list[dict] | None,
    sound_effects: list[dict] | None,
    media_overlays: list[dict] | None,
    visual_blocks: list[dict] | None,
    motion_scenes: list[dict] | None,
) -> dict[str, Any]:
    """Project the batched editor Save onto the story-native revision.

    The conventional editor request remains the public wire format.  This
    projection is deliberately server-side so lane timing and music stay on
    the output clock when clips are re-cut; the browser never gets to author
    approval identities or revision numbers.
    """
    current = _guided_v2_revision(job, variant)
    if current is None:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "GUIDED_REVISION_INVALID")
    if payload.base_generation != variant_render_baseline(variant):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="baseline_conflict")
    if payload.guided_revision_number is not None and payload.guided_revision_number != int(
        current["revision_number"]
    ):
        raise _timeline_error(status.HTTP_409_CONFLICT, "GUIDED_REVISION_STALE")
    if payload.guided_revision_number is None:
        raise _timeline_error(status.HTTP_409_CONFLICT, "GUIDED_REVISION_TOKEN_REQUIRED")
    raw = dict(payload.guided_revision or current)
    # Source identity is approval-owned. A client may choose among the pool,
    # but cannot replace paths or generations in a revision payload.
    raw["sources"] = list(current.get("sources") or [])
    raw["revision_number"] = int(current["revision_number"]) + 1
    raw["base_generation"] = variant_render_baseline(variant)
    raw["state_hash"] = ""
    # Audio identity is server-owned. Timeline/full-revision payloads may not
    # smuggle an arbitrary GCS object past the conventional music validators.
    raw["audio"] = dict(current.get("audio") or {"mode": "none"})
    if payload.orientation is not None:
        raw["orientation"] = payload.orientation
    if text_elements is not None:
        current_text_ids = {
            str(row.get("id")) for row in current.get("text_elements") or [] if row.get("id")
        }
        approved_text_ids = current_text_ids | {
            str(row.get("record_id"))
            for row in current.get("tombstones") or []
            if row.get("lane") == "text_elements" and row.get("record_id")
        }
        submitted_text_ids = {str(row.get("id")) for row in text_elements if row.get("id")}
        if not current_text_ids.issubset(submitted_text_ids) or not submitted_text_ids.issubset(
            approved_text_ids
        ):
            raise _timeline_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "GUIDED_TEXT_IDENTITY_MISMATCH"
            )
        raw["text_elements"] = text_elements
    if sound_effects is not None:
        raw["sound_effects"] = sound_effects
    if media_overlays is not None:
        raw["media_overlays"] = media_overlays
    if visual_blocks is not None:
        raw["visual_blocks"] = visual_blocks
    if motion_scenes is not None:
        raw["motion_scenes"] = motion_scenes
    active_lane_ids = {
        lane: {
            str(value.get("id"))
            for value in raw.get(lane) or []
            if isinstance(value, dict) and value.get("id")
        }
        for lane in (
            "text_elements",
            "sound_effects",
            "media_overlays",
            "visual_blocks",
            "motion_scenes",
        )
    }
    # Restoring a visible tombstone is an ordinary lane edit. Once its exact
    # record identity is active again, remove the historical deletion marker
    # from the next canonical revision and receipt.
    raw["tombstones"] = [
        value
        for value in raw.get("tombstones") or []
        if not (
            isinstance(value, dict)
            and str(value.get("record_id") or "")
            in active_lane_ids.get(str(value.get("lane") or ""), set())
        )
    ]
    if payload.mix is not None and payload.mix.music_level is not None:
        raw.setdefault("audio", {})["level"] = float(payload.mix.music_level)
    if payload.remove_music:
        raw["audio"] = {
            "mode": "none",
            "removed": True,
            "start_s": 0.0,
            "level": 0.0,
        }
    elif payload.music_track_id is not None:
        if music_track is None or not music_track.audio_gcs_path or not music_track_generation:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "music_track_unavailable")
        raw["audio"] = {
            "mode": "track",
            "removed": False,
            "track_id": str(music_track.id),
            "title": str(music_track.title),
            "audio_gcs_path": str(music_track.audio_gcs_path),
            "generation": music_track_generation,
            "start_s": float(updated.get("music_start_s") or 0.0),
            "level": float((raw.get("audio") or {}).get("level", 1.0)),
        }
    if payload.timeline_slots is not None:
        projected = _guided_v2_revision_for_write(
            job,
            variant,
            TimelineEditRequest(
                slots=payload.timeline_slots,
                revision_number=int(current["revision_number"]),
                base_generation=variant_render_baseline(variant),
            ),
        )
        raw["segments"] = projected["segments"]
        _project_guided_revision_lanes(
            raw,
            old_segments=list(current["segments"]),
            new_segments=list(raw["segments"]),
        )
    if payload.music_window is not None:
        effective_track_id = payload.music_track_id or variant.get("music_track_id")
        if (
            music_track is None
            or str(music_track.id) != str(effective_track_id or "")
            or music_track.analysis_status != "ready"
            or not music_track.audio_gcs_path
        ):
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "music_track_unavailable")
        track_duration_s = _track_duration(music_track)
        story_duration_s = max(
            (float(segment.get("output_end_s") or 0.0) for segment in raw.get("segments") or []),
            default=0.0,
        )
        if track_duration_s <= 0:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "track_duration_unknown")
        if story_duration_s <= 0:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "video_duration_unknown")
        if track_duration_s + _MUSIC_WINDOW_EPSILON_S < story_duration_s:
            raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "song_shorter_than_video")
        clamped_start_s = min(
            float(payload.music_window.start_s),
            max(0.0, track_duration_s - story_duration_s),
        )
        raw.setdefault("audio", {})["start_s"] = clamped_start_s
        raw["audio"]["end_s"] = clamped_start_s + story_duration_s
    try:
        return normalize_guided_editor_revision(
            raw,
            expected_approval_version=int(current["approval_proposal_version"]),
            expected_media_digest=str(current["approval_media_digest"]),
        )
    except ValueError as exc:
        raise _timeline_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "GUIDED_REVISION_INVALID"
        ) from exc


async def dispatch_reset_timeline(job: Job, variant_id: str, *, db: AsyncSession) -> None:
    """Drop the user timeline (row-locked) and re-render from the AI timeline."""
    from app.config import settings  # noqa: PLC0415

    if not settings.GENERATIVE_TIMELINE_EDITOR_ENABLED:
        raise _timeline_error(status.HTTP_403_FORBIDDEN, "disabled")
    variant = require_editable_variant(job, variant_id)
    if any(v.get("render_status") == "rendering" for v in _variants_of(job)):
        raise _timeline_error(status.HTTP_409_CONFLICT, "JOB_BUSY")
    # Same eligibility gate as POST: a reset re-render on a lyrics/voiceover/
    # expired variant would render from a layout the variant doesn't have.
    reason = _timeline_ineligibility(job, variant)
    if reason is not None:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, reason)
    ai_slots, _, _ = _timeline_parts(variant)

    render_gen_id = uuid.uuid4().hex
    await persist_user_timeline(
        db,
        str(job.id),
        variant_id,
        None,
        render_gen_id=render_gen_id,
    )

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    # Pass the AI slots as the override: the regenerate path is identical to an
    # edit, just sourced from the AI's own plan (simplest reset contract).
    regenerate_generative_variant.delay(
        str(job.id),
        variant_id,
        timeline_override=_timeline_override_for_reassembly(ai_slots),
        render_gen_id=render_gen_id,
    )


# ── Transactional editor commit dispatch (E2) ───────────────────────────────────


def variant_render_baseline(variant: dict) -> str:
    """The compare-and-fail baseline a client must echo back on editor-commit.

    `render_generation_id` when the variant has ever been committed through a
    token-stamped edit; else the last `render_finished_at`; else "" (a variant
    that never finished a render has nothing to conflict with).
    """
    return str(variant.get("render_generation_id") or variant.get("render_finished_at") or "")


def require_guided_story_editor_commit(
    job: Job, variant_id: str, payload: EditorCommitRequest
) -> None:
    """Fail unsupported guided sections before route-specific lookups or writes."""

    variant = _find_variant(job, variant_id)
    if not isinstance(variant, dict) or variant.get("resolved_archetype") != "guided_story":
        return
    if getattr(settings, "guided_story_editor_v2_enabled", False):
        # V2 accepts the conventional Save sections and atomically projects
        # them into the revision. Captions/lyrics/speech cuts/intro/carousel
        # remain deliberately outside the story-native contract.
        excluded = (
            payload.caption_cues is not None
            or payload.caption_meta is not None
            or payload.background_music is not None
            or payload.lyrics is not None
            or payload.camera_effects is not None
            or payload.title is not None
            or "carousel_moment" in payload.model_fields_set
        )
        if excluded:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="guided_story_editor_v2_section_unsupported",
            )
        return
    if isinstance(variant.get("guided_edit_revision"), dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="guided_story_editor_v2_disabled",
        )
    guided_text_or_orientation_only = (
        (payload.text_elements is not None or payload.orientation is not None)
        and payload.caption_cues is None
        and payload.caption_meta is None
        and payload.timeline_slots is None
        and payload.mix is None
        and payload.music_track_id is None
        and payload.music_window is None
        and payload.background_music is None
        and payload.lyrics is None
        and payload.sound_effects is None
        and payload.media_overlays is None
        and payload.visual_blocks is None
        and payload.motion_scenes is None
        and payload.camera_effects is None
        and payload.title is None
        and not payload.remove_music
        and "carousel_moment" not in payload.model_fields_set
    )
    if not guided_text_or_orientation_only:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_GUIDED_STORY_EDIT_ERROR,
        )
    if payload.text_elements is not None:
        _require_guided_story_text_ids(variant, payload.text_elements)


def prepare_editor_commit(
    job: Job,
    variant_id: str,
    payload: EditorCommitRequest,
    *,
    user_id: str | None = None,
    music_track: MusicTrack | None = None,
    music_track_generation: str | None = None,
    background_music_track: MusicTrack | None = None,
    plan_item_id: str | None = None,
    visual_assets: dict[str, dict] | None = None,
) -> dict:
    """Validate ALL sections, compare the baseline, then stage ONE atomic write.

    Deliberately does NOT use `require_editable_variant`: saving during an
    in-flight render is the point — the E1 generation guard supersedes the old
    task's terminal write. Raises before ANY mutation:
      - 404 unknown variant
      - 422 no sections provided / any invalid section (single-sourced section
        validators: `validate_text_elements_payload`, `resolve_timeline_slots_for_edit`,
        the dispatch_set_mix voiceover rule)
      - 409 {"detail": "baseline_conflict"} when the variant moved since load

    On success, mutates `job.assembly_plan` IN ONE new-dict replacement — that
    reassignment is what marks the JSONB column dirty (same pattern as
    `persist_user_timeline`); the caller owns the single db.commit. Render-
    affecting sections bump `render_generation_id` and set
    render_status="rendering"; a title-only commit stages nothing here and
    kicks no render.
    """
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    require_guided_story_editor_commit(job, variant_id, payload)

    guided_v2 = variant.get("resolved_archetype") == "guided_story" and getattr(
        settings, "guided_story_editor_v2_enabled", False
    )

    if guided_v2 and payload.retry_guided_revision:
        current = _guided_v2_revision(job, variant)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="guided_story_revision_unavailable",
            )
        if payload.guided_revision_number is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="GUIDED_REVISION_TOKEN_REQUIRED"
            )
        if payload.guided_revision_number != int(current["revision_number"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="GUIDED_REVISION_STALE"
            )
        if payload.base_generation != variant_render_baseline(variant):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="baseline_conflict")
        return {
            "generation": variant_render_baseline(variant),
            "guided_revision": current,
            "guided_revision_render": True,
            "has_render_section": True,
            "timeline_override": None,
            "mix_override": None,
            "sfx_override": None,
            "audio_sfx_override": [],
            "media_overlays_override": None,
            "visual_blocks_override": None,
            "motion_scenes_override": None,
            "camera_effects_override": None,
            "pending_overlay_camera_rebuild": False,
            "orientation_override": None,
            "new_track_id": None,
            "remove_music": False,
            "music_window_alignment": None,
            "caption_cues_override": None,
            "text_requires_full_render": False,
            "resolved_archetype": "guided_story",
            "has_caption_base": False,
            "render_status_at_commit": variant.get("render_status"),
            "revision_number": int(current["revision_number"]),
            "sections": {
                "text_elements": False,
                "caption_cues": False,
                "caption_meta": False,
                "timeline": True,
                "mix": False,
                "music": True,
                "background_music": False,
                "lyrics": False,
                "orientation": False,
                "sound_effects": bool(current.get("sound_effects")),
                "media_overlays": bool(current.get("media_overlays")),
                "visual_blocks": bool(current.get("visual_blocks")),
                "motion_scenes": bool(current.get("motion_scenes")),
                "camera_effects": False,
                "carousel_moment": False,
            },
        }

    if guided_v2 and payload.guided_revision is not None:
        # The public Save contract is the conventional sectioned request. Raw
        # revision JSON would bypass music/source ownership and lane feature
        # gates, so it is intentionally never a writable wire surface.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="GUIDED_REVISION_WIRE_UNSUPPORTED",
        )

    if (
        payload.text_elements is None
        and payload.caption_cues is None
        and payload.caption_meta is None
        and payload.timeline_slots is None
        and payload.mix is None
        and payload.music_track_id is None
        and payload.music_window is None
        and payload.background_music is None
        and payload.lyrics is None
        and payload.orientation is None
        and payload.sound_effects is None
        and payload.media_overlays is None
        and payload.visual_blocks is None
        and payload.motion_scenes is None
        and payload.camera_effects is None
        and payload.guided_revision is None
        and payload.title is None
        and not payload.remove_music
        # Tri-state (see EditorCommitRequest.carousel_moment): only
        # `payload.carousel_moment is None` is ambiguous (absent vs. explicit
        # removal), so gate on `model_fields_set` here like every other read
        # of this field below.
        and "carousel_moment" not in payload.model_fields_set
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one section to commit.",
        )

    # Accepted suggestions may still be legacy overlays or may have been
    # converted to first-class media visual blocks before their initial save.
    if (
        payload.accepted_suggestion_ids
        and payload.media_overlays is None
        and payload.visual_blocks is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "accepted_suggestion_ids requires the media_overlays or visual_blocks section."
            ),
        )
    if (
        payload.music_window is not None
        and payload.timeline_slots is not None
        and payload.music_window.alignment != "preserve_cuts"
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="timeline_slots can only accompany a preserve_cuts music window.",
        )

    # ── Validate every provided section BEFORE any write ──────────────────────
    validated_elements: list[dict] | None = None
    materialized_from_sequence = False
    text_requires_full_render = False
    if payload.text_elements is not None:
        from app.config import settings as _settings_text  # noqa: PLC0415

        # OV-1 (plan 010): the API half of the dual text-elements gate —
        # `_text_elements_allowed` is the same predicate `_editor_capabilities`
        # derives `text_elements` from (lyrics/flag-off are 422/404 in
        # validate_text_elements_payload).
        visual_card_text_only = (
            _settings_text.visual_blocks_enabled
            and payload.visual_blocks is not None
            and all(element.get("visual_block_id") for element in payload.text_elements)
        )
        if not _text_elements_allowed(variant) and not visual_card_text_only:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{CAPTION_TAB_COPY}.",
            )
        # The fast-reburn base is only required when this commit will take the
        # reburn path (no timeline change → no full re-assembly).
        text_requires_full_render = payload.timeline_slots is None and (
            not bool(variant.get("base_video_path"))
            or (_LYRICS_EDITOR_ENABLED and _variant_lyrics_enabled(variant))
        )
        validated_elements, materialized_from_sequence = validate_text_elements_payload(
            variant,
            payload.text_elements,
            require_base=payload.timeline_slots is None and not text_requires_full_render,
            strict_drop=True,
            # Guided v2 owns text identity and deletions in the canonical
            # revision/tombstone document.  The legacy variant projection can
            # synthesize a second intro identity for the same guided title,
            # which makes an unchanged full-lane Save fail the revision's
            # exact-ID guard.
            append_projection_tombstones=not guided_v2,
        )
        if not guided_v2:
            _require_guided_story_text_ids(variant, validated_elements)

    validated_caption_cues: list[dict] | None = None
    if payload.caption_cues is not None:
        if not _is_editable_caption_variant(variant):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{CAPTION_TAB_COPY}.",
            )
        validated_caption_cues = [
            CaptionCue.model_validate(c).model_dump(exclude_none=True) for c in payload.caption_cues
        ]

    caption_meta_patch: dict | None = None
    if payload.caption_meta is not None:
        # Meta toggles (style/font/enabled/position) are accepted for BOTH caption
        # archetypes — the fields and the reburn task are shared.
        if not _is_editable_caption_variant(variant):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{CAPTION_TAB_COPY}.",
            )
        meta = payload.caption_meta
        if meta.font_set:
            from app.pipeline.narrated_assembler import is_valid_caption_font  # noqa: PLC0415

            if not is_valid_caption_font(meta.font):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Unknown caption font.",
                )
        caption_meta_patch = {}
        if meta.enabled is not None:
            caption_meta_patch["captions_enabled"] = bool(meta.enabled)
        if meta.style is not None:
            caption_meta_patch["voiceover_caption_style"] = meta.style
        if meta.font_set:
            caption_meta_patch["voiceover_caption_font"] = meta.font
            # Mirror the Captions-tab dispatch routes: without this flag the
            # smart-caption policy (_effective_smart_caption_policy) ignores the
            # committed font and the edit silently no-ops on Smart Captions.
            caption_meta_patch["caption_font_user_edited"] = True
        if meta.y_frac is not None:
            from app.pipeline.captions import y_frac_to_margin_v  # noqa: PLC0415

            caption_meta_patch["caption_margin_v"] = y_frac_to_margin_v(meta.y_frac)
            caption_meta_patch["caption_position_user_edited"] = True
        if meta.size_px is not None:
            caption_meta_patch["caption_size_px"] = int(meta.size_px)
        if meta.color is not None:
            caption_meta_patch["caption_text_color"] = meta.color
        if meta.highlight_color is not None:
            caption_meta_patch["caption_highlight_color"] = meta.highlight_color
        if meta.stroke_width is not None:
            caption_meta_patch["caption_stroke_width"] = int(meta.stroke_width)
        if meta.shadow_enabled is not None:
            caption_meta_patch["caption_shadow_enabled"] = bool(meta.shadow_enabled)

    resolved_slots: list[dict] | None = None
    if payload.timeline_slots is not None and not guided_v2:
        resolved_slots = resolve_timeline_slots_for_edit(job, variant, payload.timeline_slots)

    if payload.remove_music:
        # Removal is its own section: combining it with a swap or a song-window
        # move in one commit is contradictory — fail loudly, nothing persisted.
        if payload.music_track_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="remove_music cannot be combined with music_track_id.",
            )
        if payload.music_window is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="remove_music cannot be combined with music_window.",
            )
        # Same guard family as the swap branch below: removing music from a
        # variant that has none is a client bug, not a no-op.
        if variant.get("variant_id") == "original_text" or variant.get("music_track_id") is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This edit has no song to remove.",
            )

    if payload.music_track_id is not None:
        if variant.get("variant_id") == "original_text" or variant.get("music_track_id") is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This is the original-audio edit — it has no song to swap.",
            )
        if (
            music_track is None
            or music_track.id != payload.music_track_id
            or music_track.analysis_status != "ready"
            or not music_track.audio_gcs_path
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Requested song is not available for rendering.",
            )
        existing_track_id = str(variant.get("music_track_id") or "")
        selecting_existing_unpublished = str(music_track.id) == existing_track_id
        if not selecting_existing_unpublished and (
            getattr(music_track, "published_at", True) is None
            or getattr(music_track, "archived_at", None) is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "music_track_unavailable"},
            )

    resolved_music_start_s: float | None = None
    music_window_video_duration_s: float | None = None
    music_window_grid: list[float] | None = None
    frozen_music_slots: list[dict] | None = None
    if payload.music_window is not None and not guided_v2:
        if str(variant.get("variant_id") or "") not in _MUSIC_WINDOW_VARIANTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "music_window_unsupported_variant"},
            )
        effective_track_id = payload.music_track_id or variant.get("music_track_id")
        if (
            music_track is None
            or str(music_track.id) != str(effective_track_id or "")
            or music_track.analysis_status != "ready"
            or not music_track.audio_gcs_path
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "music_track_unavailable"},
            )
        existing_track_id = str(variant.get("music_track_id") or "")
        if str(music_track.id) != existing_track_id and (
            music_track.published_at is None or music_track.archived_at is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "music_track_unavailable"},
            )
        video_duration_s = visual_block_variant_duration(variant)
        if payload.music_window.alignment == "preserve_cuts":
            frozen_music_slots = _freeze_music_window_timeline(variant, resolved_slots)
            video_duration_s = round(
                sum(
                    float(slot.get("duration_s") or 0.0)
                    for slot in frozen_music_slots
                    if not slot.get("removed")
                ),
                3,
            )
        track_duration_s = _track_duration(music_track)
        if video_duration_s <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "video_duration_unknown"},
            )
        if track_duration_s <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "track_duration_unknown"},
            )
        if track_duration_s + _MUSIC_WINDOW_EPSILON_S < video_duration_s:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "song_shorter_than_video"},
            )
        resolved_music_start_s = _snap_music_start(
            music_track, payload.music_window.start_s, video_duration_s
        )
        music_window_grid = _relative_music_grid(
            music_track, resolved_music_start_s, video_duration_s
        )
        music_window_video_duration_s = video_duration_s

    background_music_treatment: dict | None = None
    if payload.background_music is not None:
        background_music_treatment = _resolve_background_music_treatment(
            payload.background_music,
            track=background_music_track,
            variant=variant,
        )

    validated_lyrics: dict | None = None
    if payload.lyrics is not None:
        lyric_payload: dict = {}
        if "enabled" in payload.lyrics.model_fields_set:
            lyric_payload["enabled"] = payload.lyrics.enabled
        if "line_overrides" in payload.lyrics.model_fields_set:
            lyric_payload["line_overrides"] = payload.lyrics.line_overrides
        validated_lyrics = validate_lyrics_section(variant, lyric_payload, music_track=music_track)

    validated_orientation: str | None = None
    if payload.orientation is not None:
        if not _LANDSCAPE_OUTPUT_ENABLED:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Landscape output is not available.",
            )
        validated_orientation = validate_orientation_section(variant, payload.orientation)

    mix_override: float | None = None
    if payload.mix is not None:
        # R1-4: caption archetypes persist mix=1.0 but have no montage mix lane —
        # narrated's knob is the item-page bed-level dispatch; subtitled has no
        # bed. Reject loudly instead of funneling into the montage regenerate,
        # whose caption reject would land a silent no-op "ready".
        if variant.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Background sound for this edit is adjusted on the item page.",
            )
        # Same rule as dispatch_set_mix: only voiceover variants carry a voice
        # bed to rebalance.
        if (
            not guided_v2
            and variant.get("mix") is None
            and not str(variant_id).startswith("voiceover")
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This edit has no voiceover to mix.",
            )
        mix_override = payload.mix.music_level

    validated_sfx: list[dict] | None = None
    if payload.sound_effects is not None:
        from app.config import settings as _settings  # noqa: PLC0415

        if not _settings.sound_effects_enabled:
            if not payload.sound_effects:
                # Untouched echo (e.g. undo/redo blanket-dirtied every section
                # regardless of capability) riding a commit where sfx is
                # flag-gated off for this deploy. An empty list carries no
                # information — treat it as not-sent instead of 422ing the
                # WHOLE commit over a section the user never touched.
                log.debug(
                    "editor_commit_ignored_empty_section",
                    section="sound_effects",
                    job_id=str(job.id),
                    variant_id=variant_id,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Sound effects are not available for this editor commit.",
                )
        else:
            if user_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Sound effects require a user-scoped asset namespace.",
                )
            validated_sfx = validate_sound_effects_for_user(
                sfx_raw=payload.sound_effects,
                user_id=user_id,
                plan_item_id=plan_item_id,
            )

    validated_overlays: list[dict] | None = None
    if payload.media_overlays is not None:
        from app.config import settings as _settings  # noqa: PLC0415

        if not _settings.media_overlays_enabled:
            if not payload.media_overlays:
                # See sound_effects above — untouched empty-list echo, not a
                # real request to write this flag-gated-off section.
                log.debug(
                    "editor_commit_ignored_empty_section",
                    section="media_overlays",
                    job_id=str(job.id),
                    variant_id=variant_id,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Media overlays are not available for this editor commit.",
                )
        else:
            if user_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Media overlays require a user-scoped asset namespace.",
                )
            validated_overlays = validate_media_overlays_for_user(
                overlays_raw=payload.media_overlays,
                user_id=user_id,
                plan_item_id=plan_item_id,
                variant_context=variant,
            )

    validated_visual_blocks: list[dict] | None = None
    if payload.visual_blocks is not None:
        from app.agents._schemas.visual_block import (  # noqa: PLC0415
            iter_visual_shots,
            validate_visual_blocks,
        )
        from app.config import settings as _settings_visual  # noqa: PLC0415

        if not _settings_visual.visual_blocks_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if variant.get("text_mode") == "lyrics" or not variant.get("base_video_path"):
            if not payload.visual_blocks:
                # Untouched empty-list echo on a variant that can never accept
                # blocks (lyrics variant, or no clean base yet) — undo/redo
                # (or any stale client) blanket-dirtied this section without
                # the user ever touching it. Skip rather than 422ing the
                # whole commit; a NON-empty list here is a real invalid
                # request and still fails loudly below.
                log.debug(
                    "editor_commit_ignored_empty_section",
                    section="visual_blocks",
                    job_id=str(job.id),
                    variant_id=variant_id,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Visual blocks require a non-lyrics variant with a clean base.",
                )
        else:
            try:
                validated_visual_blocks = validate_visual_blocks(
                    payload.visual_blocks,
                    duration_s=visual_block_variant_duration(variant),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
            assets = visual_assets or {}
            legacy_media = {
                str(card.get("id")): card
                for card in (variant.get("media_overlays") or [])
                if isinstance(card, dict) and card.get("id")
            }
            persisted_media = {
                str(block.get("asset_id")): block
                for block in (variant.get("visual_blocks") or [])
                if isinstance(block, dict)
                and block.get("kind") == "media"
                and block.get("asset_id")
            }
            submitted_media: dict[str, list[dict]] = {}
            for block in validated_visual_blocks:
                if block.get("kind") == "media" and block.get("asset_id"):
                    submitted_media.setdefault(str(block["asset_id"]), []).append(block)
            for shot in iter_visual_shots(validated_visual_blocks):
                asset = assets.get(str(shot.get("asset_id")))
                pool_asset_valid = not (
                    asset is None
                    or asset.get("status") != "ready"
                    or asset.get("gcs_path") != shot.get("src_gcs_path")
                    or asset.get("kind") != shot.get("kind")
                )
                legacy = legacy_media.get(str(shot.get("asset_id")))
                legacy_asset_valid = bool(
                    legacy
                    and legacy.get("src_gcs_path") == shot.get("src_gcs_path")
                    and legacy.get("kind") == shot.get("kind")
                )
                persisted = persisted_media.get(str(shot.get("asset_id")))
                persisted_asset_valid = bool(
                    persisted
                    and persisted.get("src_gcs_path") == shot.get("src_gcs_path")
                    and persisted.get("media_kind") == shot.get("kind")
                )
                if not pool_asset_valid and not legacy_asset_valid and not persisted_asset_valid:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Visual block assets must be ready assets owned by this plan item.",
                    )
                trusted_preview = None
                if pool_asset_valid:
                    trusted_preview = nonblank_str(asset.get("preview_gcs_path"))
                elif legacy_asset_valid:
                    trusted_preview = nonblank_str(legacy.get("preview_gcs_path"))
                elif persisted_asset_valid:
                    trusted_preview = nonblank_str(persisted.get("preview_gcs_path"))
                # Preview identity is server-owned. Replace, rather than merely
                # validate, the client value so an owned source cannot be paired
                # with a guessed private object path.
                for submitted_media_block in submitted_media.get(str(shot.get("asset_id")), []):
                    if trusted_preview:
                        submitted_media_block["preview_gcs_path"] = trusted_preview
                    else:
                        submitted_media_block.pop("preview_gcs_path", None)
            for block in validated_visual_blocks:
                if block.get("kind") != "media" or block.get("media_kind") != "video":
                    continue
                asset = assets.get(str(block.get("asset_id")))
                authoritative_duration = asset.get("duration_s") if asset is not None else None
                if authoritative_duration is None:
                    legacy = legacy_media.get(str(block.get("asset_id")))
                    authoritative_duration = (
                        legacy.get("clip_duration_s") if legacy is not None else None
                    )
                if authoritative_duration is None:
                    persisted = persisted_media.get(str(block.get("asset_id")))
                    authoritative_duration = (
                        persisted.get("source_duration_s") if persisted is not None else None
                    )
                if authoritative_duration is None or float(authoritative_duration) <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            "Video media is still being analyzed. "
                            "Try again when its duration is ready."
                        ),
                    )
                submitted_duration = float(block.get("source_duration_s") or 0)
                if abs(submitted_duration - float(authoritative_duration)) > 0.05:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Video media duration changed. Reload the asset before editing it.",
                    )

    if validated_orientation is not None and validated_visual_blocks is not None:
        prospective_variant = dict(variant)
        prospective_variant["visual_blocks"] = validated_visual_blocks or None
        validated_orientation = validate_orientation_section(
            prospective_variant, validated_orientation
        )

    validated_camera_effects: list[dict] | None = None

    validated_motion_scenes: list[dict] | None = None
    if payload.motion_scenes is not None:
        from app.config import settings as _settings_motion  # noqa: PLC0415
        from app.pipeline.motion_scene import (  # noqa: PLC0415
            COMPATIBLE_MOTION_RUNTIME_HASHES,
            LEGACY_MOTION_RUNTIME_HASH,
            MOTION_FPS,
            MOTION_RUNTIME_HASH,
            validate_motion_instances,
        )

        if not _settings_motion.motion_scenes_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        legacy_route_only = payload.motion_runtime_hash == LEGACY_MOTION_RUNTIME_HASH and all(
            scene.get("preset_id") == "route_trace" for scene in payload.motion_scenes
        )
        compatible_hash = payload.motion_runtime_hash in COMPATIBLE_MOTION_RUNTIME_HASHES
        if not compatible_hash and not legacy_route_only:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "motion_runtime_mismatch"},
            )
        motion_reason = None
        if not variant.get("base_video_path"):
            motion_reason = "motion_clean_base_unavailable"
        duration_s = visual_block_variant_duration(variant)
        if duration_s <= 0:
            motion_reason = motion_reason or "duration_unknown"
        if motion_reason is not None:
            if not payload.motion_scenes:
                log.debug(
                    "editor_commit_ignored_empty_section",
                    section="motion_scenes",
                    job_id=str(job.id),
                    variant_id=variant_id,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": motion_reason},
                )
        else:
            try:
                validated_motion_scenes = validate_motion_instances(
                    payload.motion_scenes,
                    duration_frames=max(1, round(duration_s * MOTION_FPS)),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
            if not _settings_motion.evolving_type_enabled:
                persisted_by_id = {
                    str(scene.get("id")): scene
                    for scene in variant.get("motion_scenes") or []
                    if isinstance(scene, dict) and scene.get("preset_id") == "evolving_type"
                }
                for scene in validated_motion_scenes:
                    if scene.get("preset_id") != "evolving_type":
                        continue
                    persisted = persisted_by_id.get(str(scene.get("id")))
                    if persisted != scene:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "evolving_type_disabled"},
                        )
            assets = visual_assets or {}
            for scene in validated_motion_scenes:
                if scene.get("preset_id") not in {"card_stack", "film_strip"}:
                    continue
                for ref in scene.get("params", {}).get("assets", []):
                    asset = assets.get(str(ref.get("asset_id")))
                    if (
                        asset is None
                        or asset.get("status") != "ready"
                        or asset.get("kind") != "image"
                        or asset.get("gcs_path") != ref.get("gcs_path")
                    ):
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "motion_asset_unavailable"},
                        )

    if payload.camera_effects is not None:
        from app.pipeline.camera_effects import normalize_camera_effects  # noqa: PLC0415

        if variant.get("resolved_archetype") != "subtitled":
            if not payload.camera_effects:
                log.debug(
                    "editor_commit_ignored_empty_section",
                    section="camera_effects",
                    job_id=str(job.id),
                    variant_id=variant_id,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Camera effects are editable on subtitled variants.",
                )
        elif not variant.get("base_video_path"):
            if not payload.camera_effects:
                log.debug(
                    "editor_commit_ignored_empty_section",
                    section="camera_effects",
                    job_id=str(job.id),
                    variant_id=variant_id,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Camera effects require a clean base video.",
                )
        else:
            validated_camera_effects = normalize_camera_effects(
                payload.camera_effects,
                duration_s=visual_block_variant_duration(variant),
            )

    # Older clients may commit only the overlay section. Enforce the bundle
    # invariant server-side so linked generated effects cannot be orphaned.
    # Explicit group IDs are the sole linkage; legacy timing is never guessed.
    if validated_overlays is not None:
        validated_sfx, validated_camera_effects = cascade_removed_overlay_effect_groups(
            variant,
            validated_overlays,
            sound_effects=validated_sfx,
            camera_effects=validated_camera_effects,
            transferred_media_owners=validated_visual_blocks,
        )

    # Carousel-moment edit (Blossom carousel), staged synchronously — unlike
    # every other section here, this one doesn't wait for the worker: the
    # merged config is computed and persisted in THIS request (mirrors
    # `dispatch_edit_variant`'s validation, shared via
    # `_validate_carousel_moment_patch`), and the render simply reads the
    # already-staged `variant["carousel_moment"]` (see `enqueue_editor_commit_render`
    # below, which forces a full render and passes NO carousel override kwarg —
    # the worker's `_merge_carousel_moment_override` sees `CAROUSEL_MOMENT_UNSET`
    # and carries the persisted value forward unchanged).
    #
    # Tri-state, mirroring `edit_variant`'s route handler (~L6356 as of this
    # writing): absent from `model_fields_set` -> leave unchanged; explicit
    # top-level `null` -> remove; an object -> validated partial-merge.
    carousel_moment_touched = "carousel_moment" in payload.model_fields_set
    merged_carousel_moment: dict | None = None
    if carousel_moment_touched:
        from app.tasks.generative_build import (  # noqa: PLC0415
            _merge_carousel_moment_override,
        )

        carousel_override_for_merge: dict | None = (
            None
            if payload.carousel_moment is None
            else _validate_carousel_moment_patch(
                payload.carousel_moment.model_dump(exclude_unset=True), variant
            )
        )
        merged_carousel_moment = _merge_carousel_moment_override(
            variant.get("carousel_moment"), carousel_override_for_merge
        )

    # NOTE: the old motion_portrait_only gate (motion scenes required
    # orientation == "portrait") was removed upstream in #789 — Creator
    # Blocks now render in both portrait and landscape, so no orientation
    # check runs here for motion_scenes.
    pending_overlay_camera_rebuild = bool(
        validated_overlays is not None and variant.get(_OVERLAY_CAMERA_REBUILD_PENDING)
    )
    if pending_overlay_camera_rebuild and validated_camera_effects is None:
        # render=false may already have removed both the overlay and its camera
        # sibling. Preserve the base-affecting signal for this independent Save
        # entrypoint even though there is no longer an overlay to diff against.
        validated_camera_effects = list(variant.get("camera_effects") or [])

    if payload.visual_blocks is not None or payload.text_elements is not None:
        from app.agents._schemas.visual_block import (  # noqa: PLC0415
            validate_visual_block_text_links,
        )

        effective_blocks = (
            validated_visual_blocks
            if validated_visual_blocks is not None
            else list(variant.get("visual_blocks") or [])
        )
        effective_elements = (
            validated_elements
            if validated_elements is not None
            else list(variant.get("text_elements") or [])
        )
        try:
            validate_visual_block_text_links(effective_blocks, effective_elements)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

    # ── Stale-baseline compare-and-fail (multi-tab / superseded-render safety) ─
    if payload.base_generation != variant_render_baseline(variant):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="baseline_conflict")

    # ── Stage the single atomic job-JSON write ─────────────────────────────────
    has_render_section = (
        validated_elements is not None
        or validated_caption_cues is not None
        or caption_meta_patch is not None
        or resolved_slots is not None
        or payload.mix is not None
        or payload.music_track_id is not None
        or payload.remove_music
        or payload.music_window is not None
        or payload.background_music is not None
        or validated_lyrics is not None
        or validated_orientation is not None
        or validated_sfx is not None
        or validated_overlays is not None
        or validated_visual_blocks is not None
        or validated_motion_scenes is not None
        or validated_camera_effects is not None
        or carousel_moment_touched
    )
    new_gen = uuid.uuid4().hex if has_render_section else None
    base_affecting_commit = (
        resolved_slots is not None
        or payload.mix is not None
        or payload.music_track_id is not None
        or payload.remove_music
        or payload.music_window is not None
        or validated_lyrics is not None
        or validated_orientation is not None
        or validated_camera_effects is not None
        or text_requires_full_render
        # A carousel moment splices extra footage into the assembled montage —
        # the cached fast-reburn base (clean, pre-text-burn footage) no longer
        # matches once this lands, same invariant as a timeline/mix/track edit.
        or carousel_moment_touched
    )

    variants = list((job.assembly_plan or {}).get("variants") or [])
    track_changed = payload.music_track_id is not None and payload.music_track_id != variant.get(
        "music_track_id"
    )
    for i, v in enumerate(variants):
        if v.get("variant_id") != variant_id:
            continue
        updated = dict(v)
        if validated_elements is not None:
            updated["text_elements"] = validated_elements
            updated["text_elements_user_edited"] = True
            if materialized_from_sequence:
                updated["geometry_materialized_at_version"] = "1"
                updated["text_elements_materialized_from"] = "sequence"
        if validated_caption_cues is not None:
            updated["caption_cues"] = validated_caption_cues
        if caption_meta_patch is not None:
            updated.update(caption_meta_patch)
        if resolved_slots is not None:
            updated["user_timeline"] = {"slots": resolved_slots}
        if payload.mix is not None:
            if payload.mix.music_level is not None:
                updated["mix"] = float(payload.mix.music_level)
            if payload.mix.original_level is not None:
                # Round-trip persistence only — not yet honored by the renderer.
                updated["original_audio_level"] = float(payload.mix.original_level)
        if payload.music_track_id is not None:
            updated["music_track_id"] = payload.music_track_id
            if track_changed:
                updated["lyric_line_overrides"] = None
                updated["lyric_overlay_snapshot"] = None
                updated["text_elements"] = [
                    element
                    for element in (updated.get("text_elements") or [])
                    if not isinstance(element, dict) or element.get("role") != "lyric_line"
                ]
            if payload.music_window is None and music_track is not None:
                updated["music_start_s"] = round(
                    _recommended_music_start(music_track, visual_block_variant_duration(variant)),
                    3,
                )
                updated.pop("music_window_video_duration_s", None)
        if payload.remove_music:
            # Full music removal: the re-render resolves its track from the
            # persisted music_track_id (None → the existing track-free render
            # path, same as original_text). Clear every song-window field and
            # the track-derived lyric state (same hygiene as a track swap).
            updated["music_track_id"] = None
            updated["music_start_s"] = None
            updated["track_title"] = None
            updated.pop("music_window_video_duration_s", None)
            updated["lyric_line_overrides"] = None
            updated["lyric_overlay_snapshot"] = None
            updated["text_elements"] = [
                element
                for element in (updated.get("text_elements") or [])
                if not isinstance(element, dict) or element.get("role") != "lyric_line"
            ]
        if payload.music_window is not None and resolved_music_start_s is not None:
            updated["music_start_s"] = resolved_music_start_s
            updated["music_window_video_duration_s"] = round(
                music_window_video_duration_s or visual_block_variant_duration(variant), 3
            )
            if payload.music_window.alignment == "preserve_cuts":
                updated["user_timeline"] = {
                    "slots": frozen_music_slots or [],
                    "beat_grid": music_window_grid or [],
                }
            else:
                updated.pop("user_timeline", None)
        if resolved_slots is not None and updated.get("music_track_id"):
            active_duration_s = _active_timeline_duration_s(resolved_slots)
            if active_duration_s > 0:
                # The editor commit persists before enqueue. Keep the duration
                # beside those cuts so an older worker cannot reuse a stale
                # music window during a rolling deploy.
                updated["music_window_video_duration_s"] = active_duration_s
        if payload.background_music is not None:
            updated["smart_music_treatment"] = background_music_treatment
            updated["smart_audio_receipt"] = None
        if validated_lyrics is not None:
            if "enabled" in payload.lyrics.model_fields_set:
                updated["lyrics_enabled"] = bool(validated_lyrics["enabled"])
            if "line_overrides" in payload.lyrics.model_fields_set and not track_changed:
                updated["lyric_line_overrides"] = validated_lyrics["line_overrides"]
        if validated_orientation is not None:
            updated["orientation"] = validated_orientation
        if validated_sfx is not None:
            updated["sound_effects"] = validated_sfx or None
        if validated_overlays is not None:
            updated["media_overlays"] = validated_overlays or None
        if validated_visual_blocks is not None:
            updated["visual_blocks"] = validated_visual_blocks or None
            # Keep the old key reachable until the token-gated worker publishes
            # the replacement. The stale bit prevents reuse; retaining the key
            # lets the winning render free it without a pre-render delete race.
            updated["visual_blocks_cache_stale"] = True
        if validated_motion_scenes is not None:
            updated["motion_scenes"] = validated_motion_scenes or None
            # A legacy route-trace-only client may finish an in-flight save
            # during rollout, but that save migrates desired state forward.
            updated["motion_runtime_hash"] = MOTION_RUNTIME_HASH
            # Desired state is persisted before rendering; the last-good output
            # and applied hash are only replaced by the token-winning worker.
            updated["motion_cache_stale"] = True
        if validated_camera_effects is not None:
            updated["camera_effects"] = validated_camera_effects or None
        if carousel_moment_touched:
            updated["carousel_moment"] = merged_carousel_moment
            # Same idiom as visual_blocks_cache_stale/motion_cache_stale above:
            # the persisted config is the DESIRED state, written synchronously
            # here; the actual rendered splice only lands once the (forced
            # full-render) worker below finishes. Belt-and-braces for any
            # future reader that keys off "is this variant's baked video in
            # sync with its carousel_moment config" — nothing reads this flag
            # today (carousel always forces a full render, so there is no
            # fast-reburn path that could race a stale splice), but every
            # sibling staged section carries the same bit for that reason.
            updated["carousel_moment_cache_stale"] = True
        if payload.accepted_suggestion_ids:
            # Accepted AI suggestions became legacy cards or media visual blocks;
            # drop their envelopes in the SAME atomic write. Unknown ids no-op
            # (replayed Save, already-cleared envelope). An envelope is only
            # cleared when its card actually landed in the committed overlay
            # list — an id whose card is absent (buggy client) keeps its
            # envelope instead of silently losing the suggestion. Mirrors the
            # pending-set bookkeeping in plan_items._clear_suggestions_for_asset.
            accepted = set(payload.accepted_suggestion_ids)
            committed_ids = {o.get("id") for o in (validated_overlays or [])}
            committed_ids.update(
                block.get("id")
                for block in (validated_visual_blocks or [])
                if block.get("kind") == "media"
            )
            pending = list(updated.get("overlay_suggestions") or [])
            kept = [
                s
                for s in pending
                if s.get("id") not in accepted
                or (s.get("overlay") or {}).get("id") not in committed_ids
            ]
            if len(kept) != len(pending):
                updated["overlay_suggestions"] = kept or None
                if not kept and updated.get("overlay_suggest_status") == "ready":
                    updated["overlay_suggest_status"] = "zero"
        guided_revision: dict[str, Any] | None = None
        if guided_v2:
            guided_revision = _guided_v2_revision_from_commit(
                job,
                variant,
                payload,
                updated=updated,
                music_track=music_track,
                music_track_generation=music_track_generation,
                text_elements=validated_elements,
                sound_effects=validated_sfx,
                media_overlays=validated_overlays,
                visual_blocks=validated_visual_blocks,
                motion_scenes=validated_motion_scenes,
            )
            updated["guided_edit_revision"] = guided_revision
        if new_gen is not None:
            updated["render_generation_id"] = new_gen
            # Stamp the COPY, not `v`: `variants[i] = updated` below would
            # overwrite any write made to the original dict. This is the primary
            # Save path for both the item page and the pocket editor.
            stamp_variant_attempt(updated)
            mark_reattempt(job)
        if base_affecting_commit:
            # A newer text-only commit is allowed to supersede this render.
            # Keep the dirty bit sticky until a token-winning full render lands,
            # otherwise that newer task can reburn text onto the last-good but
            # semantically stale footage/audio base.
            updated["base_video_stale"] = True
        variants[i] = updated
        break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    if guided_revision is not None:
        expected_duration_s = max(
            float(
                segment.get("output_end_s")
                or float(segment.get("output_start_s") or 0.0)
                + float(segment.get("duration_s") or 0.0)
            )
            for segment in guided_revision.get("segments") or []
        )
    elif resolved_slots is not None:
        expected_duration_s = _active_timeline_duration_s(resolved_slots)
    else:
        expected_duration_s = float(updated.get("duration_s") or variant.get("duration_s") or 0.0)

    return {
        "generation": new_gen or payload.base_generation,
        "guided_revision": guided_revision if guided_v2 else None,
        "guided_revision_render": bool(guided_v2 and guided_revision),
        "revision_number": (
            int(guided_revision["revision_number"])
            if guided_v2 and guided_revision is not None
            else None
        ),
        "expected_duration_s": round(expected_duration_s, 6),
        "has_render_section": has_render_section,
        "timeline_override": (
            frozen_music_slots
            if payload.music_window is not None
            and payload.music_window.alignment == "preserve_cuts"
            else resolved_slots
        ),
        "mix_override": mix_override,
        "sfx_override": validated_sfx,
        "audio_sfx_override": (
            validated_sfx if validated_sfx is not None else list(variant.get("sound_effects") or [])
        ),
        "media_overlays_override": validated_overlays,
        "visual_blocks_override": validated_visual_blocks,
        "motion_scenes_override": validated_motion_scenes,
        "camera_effects_override": validated_camera_effects,
        "pending_overlay_camera_rebuild": pending_overlay_camera_rebuild,
        "orientation_override": validated_orientation,
        "new_track_id": payload.music_track_id,
        "remove_music": payload.remove_music,
        "music_window_alignment": (
            payload.music_window.alignment if payload.music_window is not None else None
        ),
        "caption_cues_override": validated_caption_cues,
        "text_requires_full_render": text_requires_full_render,
        # R2: lets enqueue_editor_commit_render route caption-archetype lane-only
        # commits through the caption reburn + reapply chain.
        "resolved_archetype": variant.get("resolved_archetype"),
        "has_caption_base": bool(variant.get("base_video_path")),
        "render_status_at_commit": variant.get("render_status"),
        "sections": {
            "text_elements": payload.text_elements is not None,
            "caption_cues": payload.caption_cues is not None,
            "caption_meta": payload.caption_meta is not None,
            "timeline": payload.timeline_slots is not None,
            "mix": payload.mix is not None,
            "music": (
                payload.music_track_id is not None
                or payload.music_window is not None
                or payload.remove_music
            ),
            "background_music": payload.background_music is not None,
            "lyrics": payload.lyrics is not None,
            "orientation": payload.orientation is not None,
            # validated_* (not raw payload presence) so an ignored empty-list
            # echo (see editor_commit_ignored_empty_section above) correctly
            # reports as NOT written — downstream render-lane routing below
            # keys off this dict to decide fast-reburn vs full-render queues.
            "sound_effects": validated_sfx is not None,
            "media_overlays": validated_overlays is not None,
            "visual_blocks": validated_visual_blocks is not None,
            "motion_scenes": validated_motion_scenes is not None,
            "camera_effects": validated_camera_effects is not None,
            "carousel_moment": carousel_moment_touched,
        },
    }


def enqueue_editor_commit_render(
    job_id: str,
    variant_id: str,
    prep: dict,
    *,
    task_id: str | None = None,
) -> None:
    """Kick exactly ONE render for a committed editor Save (call AFTER db.commit).

    Text-only commits ride the overlay-jobs queue (they take the fast-reburn
    path, mirroring PUT /text-elements); anything touching the timeline or mix
    is a full re-assembly and rides the default queue. No-op for title-only
    commits. The task carries the freshly-bumped render_gen_id so E1 can discard
    any older in-flight task's terminal write.
    """
    if not prep["has_render_section"]:
        return
    if prep.get("guided_revision_render"):
        from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

        regenerate_generative_variant.apply_async(
            args=[job_id, variant_id],
            kwargs={
                "guided_revision": prep["guided_revision"],
                "render_gen_id": prep["generation"],
                "force_full_render": True,
            },
            **({"task_id": task_id} if task_id else {}),
        )
        return
    if prep["sections"].get("caption_cues") is True or prep["sections"].get("caption_meta") is True:
        from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

        # 2A: the reburn carries the freshly-bumped token so a superseded run
        # discards its terminal write (and old-blob deletes) like every render.
        # Caption tasks inline-run the overlay/SFX reapply passes, which the solo
        # overlay-jobs worker exists to serialize (macOS prefork CLIP fork hazard).
        reburn_narrated_captions.apply_async(
            args=[job_id, variant_id],
            kwargs={"render_gen_id": prep["generation"]},
            queue="overlay-jobs",
            **({"task_id": task_id} if task_id else {}),
        )
        return
    # R2 (plan 010 review): caption archetypes must reburn when a lane-only Save
    # could race an in-flight caption render, or when the lane changes visuals
    # below captions. Ready/stable outer-lane edits can use the lightweight
    # overlay/SFX passes because they operate on the current finished video and
    # do not need to rebuild captions from the clean base.
    sections = prep["sections"]
    if (
        sections.get("camera_effects") is True
        and prep.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES
        and prep.get("has_caption_base")
    ):
        from app.tasks.generative_build import rerender_caption_camera_effects  # noqa: PLC0415

        rerender_caption_camera_effects.apply_async(
            args=[job_id, variant_id],
            kwargs={"render_gen_id": prep["generation"]},
            queue="overlay-jobs",
            **({"task_id": task_id} if task_id else {}),
        )
        return
    if (
        sections.get("visual_blocks") is True
        and prep.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES
        and prep.get("has_caption_base")
    ):
        from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

        reburn_narrated_captions.apply_async(
            args=[job_id, variant_id],
            kwargs={"render_gen_id": prep["generation"]},
            queue="overlay-jobs",
            **({"task_id": task_id} if task_id else {}),
        )
        return
    lane_only_commit = (
        sections.get("sound_effects") is True
        or sections.get("media_overlays") is True
        or sections.get("visual_blocks") is True
        or sections.get("motion_scenes") is True
        or sections.get("camera_effects") is True
        or sections.get("background_music") is True
    ) and not (
        sections.get("text_elements") is True
        or sections.get("caption_meta") is True
        or sections.get("timeline") is True
        or sections.get("mix") is True
        or sections.get("music") is True
        or sections.get("orientation") is True
    )
    caption_outer_lane_only = (
        lane_only_commit
        and (sections.get("sound_effects") is True or sections.get("media_overlays") is True)
        and sections.get("visual_blocks") is not True
        and sections.get("motion_scenes") is not True
        and sections.get("camera_effects") is not True
        and sections.get("background_music") is not True
    )
    caption_fast_lane_safe = (
        caption_outer_lane_only and prep.get("render_status_at_commit") == "ready"
    )
    if (
        lane_only_commit
        and prep.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES
        and prep.get("has_caption_base")
        and sections.get("background_music") is not True
        and not caption_fast_lane_safe
    ):
        from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

        reburn_narrated_captions.apply_async(
            args=[job_id, variant_id],
            kwargs={"render_gen_id": prep["generation"]},
            queue="overlay-jobs",
            **({"task_id": task_id} if task_id else {}),
        )
        return
    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    kwargs: dict = {"render_gen_id": prep["generation"]}
    if prep["timeline_override"] is not None:
        kwargs["timeline_override"] = (
            _timeline_override_for_reassembly(prep["timeline_override"])
            if prep["sections"].get("timeline") is True
            else prep["timeline_override"]
        )
    if prep["mix_override"] is not None:
        kwargs["mix_override"] = float(prep["mix_override"])
    if prep.get("new_track_id") is not None:
        kwargs["new_track_id"] = prep["new_track_id"]
    if prep.get("orientation_override") is not None:
        kwargs["orientation_override"] = prep["orientation_override"]
    has_text_section = prep["sections"].get("text_elements") is True
    full_render = (
        prep["timeline_override"] is not None
        or prep["mix_override"] is not None
        or prep.get("new_track_id") is not None
        or prep.get("remove_music") is True
        or prep.get("orientation_override") is not None
        or prep.get("text_requires_full_render") is True
        or prep["sections"].get("visual_blocks") is True
        or prep["sections"].get("motion_scenes") is True
        or prep["sections"].get("camera_effects") is True
        or sections.get("lyrics") is True
        or prep.get("music_window_alignment") is not None
        # Carousel always full-renders: `_reburn_text_on_base` (the fast path)
        # only re-burns text onto an already-flattened base, it can't splice a
        # multi-clip carousel segment into it. See generative_build.py's
        # fast-reburn guard for the worker-side belt-and-braces (this route
        # deliberately passes NO carousel_moment_override kwarg — the worker
        # reads the already-staged `variant["carousel_moment"]` instead — so
        # that per-call sentinel check alone can't be relied on here).
        or sections.get("carousel_moment") is True
    )
    if full_render or has_text_section:
        # Text/timeline/mix full re-renders read the just-persisted variant state.
        # SFX are reapplied by the worker's persisted-SFX hook after the new base lands.
        # Lyric-state commits carry no override kwargs, so the regen's own
        # fast-reburn check must be pinned off or lyric re-injection is skipped.
        # remove_music also pins the fast path off: the cached fast-reburn base
        # has the OLD music mixed in — only a full re-assembly (which resolves
        # the just-persisted music_track_id=None into a track-free render) can
        # actually drop the song.
        if (
            sections.get("lyrics") is True
            or prep.get("music_window_alignment") is not None
            or prep.get("text_requires_full_render") is True
            or prep.get("orientation_override") is not None
            or prep.get("remove_music") is True
            or prep.get("pending_overlay_camera_rebuild") is True
            or sections.get("carousel_moment") is True
        ):
            kwargs["force_full_render"] = True
    elif prep["media_overlays_override"] is not None:
        # Overlay pass is outer-video, then the worker's terminal hook reapplies the
        # just-persisted SFX if this same commit also changed sound_effects.
        kwargs["media_overlays_override"] = prep["media_overlays_override"]
    elif prep["sfx_override"] is not None or sections.get("background_music") is True:
        kwargs["sfx_override"] = prep.get("audio_sfx_override") or []
    is_reburn_only = (
        prep["timeline_override"] is None
        and prep["mix_override"] is None
        and prep.get("new_track_id") is None
        and prep.get("remove_music") is not True
        and prep.get("orientation_override") is None
        and prep.get("text_requires_full_render") is not True
        and sections.get("lyrics") is not True
        and prep.get("music_window_alignment") is None
        and sections.get("visual_blocks") is not True
        and sections.get("motion_scenes") is not True
        and sections.get("camera_effects") is not True
        and sections.get("carousel_moment") is not True
    )
    apply_kwargs: dict = {"args": [job_id, variant_id], "kwargs": kwargs}
    if is_reburn_only:
        # Overlay-jobs queue: solo worker — avoids macOS prefork CLIP fork crash.
        apply_kwargs["queue"] = "overlay-jobs"
    if task_id:
        apply_kwargs["task_id"] = task_id
    regenerate_generative_variant.apply_async(**apply_kwargs)


# ── Endpoints ──────────────────────────────────────────────────────────────────


def _creation_montage_preset(clip_paths: list[str]) -> str:
    """Route still photos through the renderer that supports image inputs."""

    if any(
        Path(path.split("?", 1)[0]).suffix.lower() in _IMAGE_CLIP_EXTENSIONS for path in clip_paths
    ):
        return MASONRY_MONTAGE_PRESET
    return "classic"


@router.post(
    "/upload-url",
    response_model=GenerativeUploadUrlResponse,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("25/minute", key_func=get_real_ip)
async def create_generative_upload_url(
    request: Request,
    req: GenerativeUploadUrlRequest,
    current_user: CurrentUserOrSynthetic,
) -> GenerativeUploadUrlResponse:
    """Mint one just-in-time signed PUT URL for a generative clip/voiceover."""
    kind = classify_slot_kind(req.filename, req.content_type)
    content_type = req.content_type.split(";", 1)[0].strip().lower()
    if not content_type:
        content_type = "application/octet-stream"
    ext = Path(req.filename).suffix.lower()
    upload_id = uuid.uuid4().hex
    user_id = str(current_user.id)
    if kind == "audio":
        object_path = direct_voiceover_path(user_id, upload_id, ext or ".webm")
    else:
        default_ext = ".mp4" if kind == "video" else ".jpg"
        object_path = direct_clip_path(user_id, upload_id, ext or default_ext)
    try:
        upload_url = storage.signed_put_url(
            object_path,
            content_type,
            req.file_size_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — signer/storage outage is retryable
        log.error("generative_upload_sign_failed", kind=kind, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload service unavailable — try again",
        ) from exc
    return GenerativeUploadUrlResponse(
        upload_url=upload_url,
        gcs_path=object_path,
        kind=kind,
        content_type=content_type,
        upload_headers={"x-goog-if-generation-match": "0"},
    )


@router.post("", response_model=GenerativeJobResponse, status_code=status.HTTP_201_CREATED)
async def create_generative_job(
    req: CreateGenerativeJobRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Create a generative edit job (auto song + AI text, three variants)."""
    await validate_direct_uploads(req, current_user)
    first_image_path = next(
        (
            path
            for path in req.clip_gcs_paths
            if Path(path.split("?", 1)[0]).suffix.lower() in _IMAGE_CLIP_EXTENSIONS
        ),
        None,
    )
    if req.voiceover_gcs_path and first_image_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{Path(first_image_path).name} is a photo. Final voiceover edits currently "
                "support video footage only. Remove the photo or remove the final voiceover."
            ),
        )
    # Single source of truth for Job shape + clip validation, shared with the
    # content-plan per-item task. Prefixes were already validated by the request
    # schema; build_generative_job re-validates (cheap defense-in-depth).
    from app.agents._schemas.edit_format import DEFAULT_EDIT_FORMAT  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415
    from app.models import Persona as PersonaRow  # noqa: PLC0415
    from app.services.generative_jobs import build_generative_job  # noqa: PLC0415

    # Load the user's style for the render path (Creator Agent M1).
    # Best-effort: a missing persona row → no style → baseline behavior.
    user_style_raw: dict | None = None
    from app.auth import SYNTHETIC_USER_ID  # noqa: PLC0415

    if settings.user_style_enabled and current_user.id != SYNTHETIC_USER_ID:
        try:
            result_p = await db.execute(
                select(PersonaRow).where(PersonaRow.user_id == current_user.id)
            )
            persona_row = result_p.scalar_one_or_none()
            if persona_row is not None and persona_row.style:
                user_style_raw = dict(persona_row.style)
        except Exception:  # noqa: BLE001
            pass  # non-fatal — proceed without style

    job = build_generative_job(
        user_id=current_user.id,
        clip_paths=req.clip_gcs_paths,
        language=req.language,
        selected_platforms=req.selected_platforms,
        edit_format=req.edit_format or DEFAULT_EDIT_FORMAT,
        voiceover_gcs_path=req.voiceover_gcs_path,
        user_style=user_style_raw,
        item_theme=req.topic or "",
        item_idea=req.intent or "",
        montage_preset=_creation_montage_preset(req.clip_gcs_paths),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.services.job_dispatch import enqueue_orchestrator  # noqa: PLC0415
    from app.tasks.generative_build import orchestrate_generative_job  # noqa: PLC0415

    await enqueue_orchestrator(orchestrate_generative_job, job.id, db)

    log.info(
        "generative_job_created",
        job_id=str(job.id),
        clips=len(req.clip_gcs_paths),
        language=req.language,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="queued")


@router.get("/style-sets", response_model=StyleSetListResponse)
async def list_generative_style_sets() -> StyleSetListResponse:
    """The curated text style sets a user/admin can pick from for a generative edit.

    Generative-eligible only (no music-only lyric sets). Mirrors `GET /music-tracks`
    — the gallery the swap-song picker reads. Declared BEFORE `/{job_id}/status` so
    the literal path isn't captured as a job id.
    """
    from app.pipeline.style_sets import (  # noqa: PLC0415
        list_style_sets,
        style_set_intro_preview,
        style_set_preview,
    )

    return StyleSetListResponse(
        style_sets=[
            StyleSetSummary(
                **{**s, **style_set_preview(s["id"])},
                intro=StyleSetIntroPreview(**style_set_intro_preview(s["id"])),
            )
            for s in list_style_sets(applies_to="generative")
        ]
    )


async def _attach_music_previews(variants: list[dict], db: AsyncSession, *, job: Job) -> None:
    """Attach a fresh-signed music preview URL + start offset to each variant.

    Batched lookup by the variants' own music_track_ids, deliberately WITHOUT a
    published_at filter — the matcher considers unpublished tracks (mirrors
    dispatch_swap_song semantics). Best-effort: signing/lookup failures leave
    the fields None, never fail the status read.
    """
    from app.routes.music import _preview_audio_url  # noqa: PLC0415

    track_ids = {v.get("music_track_id") for v in variants if v.get("music_track_id")}
    track_ids.update(
        treatment.get("track_id")
        for v in variants
        if isinstance((treatment := v.get("smart_music_treatment")), dict)
        and treatment.get("track_id")
    )
    if not track_ids:
        return
    try:
        result = await db.execute(select(MusicTrack).where(MusicTrack.id.in_(track_ids)))
        tracks = {str(t.id): t for t in result.scalars().all()}
    except Exception:
        return
    for variant in variants:
        track = tracks.get(variant.get("music_track_id") or "")
        guided_revision = (
            _guided_v2_revision(job, variant)
            if variant.get("resolved_archetype") == "guided_story"
            else None
        )
        capability = _music_window_capability(
            variant,
            track,
            guided_revision=guided_revision,
        )
        if capability is not None:
            editor_capabilities = dict(variant.get("editor_capabilities") or {})
            editor_capabilities["music_window"] = capability
            variant["editor_capabilities"] = editor_capabilities
        if track is not None:
            video_duration_s = visual_block_variant_duration(variant)
            variant["music_preview_url"] = _preview_audio_url(track.audio_gcs_path)
            try:
                guided_audio = (
                    (guided_revision or {}).get("audio") if guided_revision is not None else None
                )
                start_s = float(
                    guided_audio.get("start_s")
                    if isinstance(guided_audio, dict) and guided_audio.get("start_s") is not None
                    else variant.get("music_start_s")
                    if variant.get("music_start_s") is not None
                    else _recommended_music_start(track, video_duration_s)
                )
                variant["music_preview_start_s"] = round(start_s, 2)
            except (TypeError, ValueError):
                variant["music_preview_start_s"] = 0.0
        treatment = variant.get("smart_music_treatment")
        treatment_track = (
            tracks.get(str(treatment.get("track_id") or ""))
            if isinstance(treatment, dict)
            else None
        )
        if treatment_track is not None:
            variant["background_music"] = _background_music_response(
                variant,
                treatment_track,
                _preview_audio_url(treatment_track.audio_gcs_path),
            )
        else:
            variant["background_music"] = None


# Non-terminal statuses an orchestrate_generative_job run passes through while
# its heartbeat thread is expected to be beating ("processing" → "rendering";
# generative_build.py sets no others). Deliberately NOT "queued" (no attempt
# has started — nothing to be stale) and not the terminal set (finished jobs
# stop beating by design).
_HEARTBEAT_LIVE_STATUSES = frozenset({"processing", "rendering"})

# Past this beacon age no acks_late redelivery can still be pending
# (visibility_timeout=1900s in worker.py, plus staleness slack) — a hard
# time_limit SIGKILL ACKS the message (task_acks_on_failure_or_timeout default),
# so "retrying automatically" would be a false promise forever. Beyond the
# window we stop claiming a retry; the stale-job reaper owns the row from there.
_RETRY_WINDOW_SLACK_S = 300


def _compute_retrying(job: Job) -> bool:
    """True when the job's render attempt died silently and awaits redelivery.

    Read-time computation from `jobs.worker_heartbeat_at` (ticked ~30s by the
    orchestrator's daemon thread). NULL beacon → no signal, never stale — this
    keeps legacy rows and non-heartbeating orchestrators at `false` forever.
    """
    from datetime import UTC  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415

    beacon = getattr(job, "worker_heartbeat_at", None)
    if job.status not in _HEARTBEAT_LIVE_STATUSES or beacon is None:
        return False
    age_s = (datetime.now(UTC) - beacon).total_seconds()
    # Misconfiguration guard: if an operator raises the beat interval above the
    # stale threshold, every healthy render would flap `retrying` between
    # beats. Two missed beats is the floor for calling an attempt dead.
    stale_after_s = max(
        settings.render_heartbeat_stale_after_s,
        2 * settings.render_heartbeat_interval_s,
    )
    from app.worker import celery_app  # noqa: PLC0415

    visibility_timeout_s = int(
        (celery_app.conf.broker_transport_options or {}).get("visibility_timeout", 1900)
    )
    retry_window_s = visibility_timeout_s + stale_after_s + _RETRY_WINDOW_SLACK_S
    return stale_after_s < age_s <= retry_window_s


async def _load_agent_runs_for_nova_steps(db: AsyncSession, job_id: uuid.UUID) -> list[AgentRun]:
    """Fetch AgentRun milestones for the Nova steps feed.

    Defers `input_json`/`output_json`/`raw_text`/`error_message` at the query
    level -- on top of `project_nova_steps` never reading them off the ORM
    object -- so the columns are never even fetched for this read path.
    """
    from sqlalchemy.orm import defer  # noqa: PLC0415

    result = await db.execute(
        select(AgentRun)
        .where(AgentRun.job_id == job_id)
        .options(
            defer(AgentRun.input_json),
            defer(AgentRun.output_json),
            defer(AgentRun.raw_text),
            defer(AgentRun.error_message),
        )
        .order_by(AgentRun.created_at)
    )
    return list(result.scalars().all())


@router.get("/{job_id}/status", response_model=GenerativeJobStatusResponse)
async def get_generative_job_status(
    job_id: str,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobStatusResponse:
    """Poll generative job status. `variants` carries the per-variant render state.

    Also serves content_plan jobs (the plan item page polls this for variants).
    """
    from app.services.phase_baselines import get_baselines, scale_render_variants  # noqa: PLC0415

    job = await _load_generative_job(
        job_id,
        db,
        current_user,
        allowed_modes=_READABLE_MODES,
        allow_cancelled=True,
        with_for_update=False,
    )

    # Count pending/rendering variants for baseline scaling.
    variants_list = (job.assembly_plan or {}).get("variants") or []
    pending_count = sum(
        1 for v in variants_list if v.get("render_status") in ("pending", "rendering")
    )
    baselines = get_baselines("generative")
    if baselines and pending_count > 0:
        baselines = scale_render_variants(baselines, pending_count)

    variants = _variants_for_response(job)
    await _attach_music_previews(variants, db, job=job)

    # Null-safe, never-raising read of the style-downgrade stash: a corrupt or
    # non-dict value from a hand-edited row degrades to null rather than a 500.
    _raw_fallback = (job.assembly_plan or {}).get("archetype_fallback")
    archetype_fallback: ArchetypeFallbackOut | None = None
    if isinstance(_raw_fallback, dict):
        _declared = _raw_fallback.get("declared")
        _reason = _raw_fallback.get("reason")
        archetype_fallback = ArchetypeFallbackOut(
            declared=str(_declared) if _declared is not None else None,
            reason=str(_reason) if _reason is not None else None,
        )

    steps: list[NovaStep] | None = None
    if settings.nova_steps_feed_enabled:
        agent_runs = await _load_agent_runs_for_nova_steps(db, job.id)
        steps = project_nova_steps(job, agent_runs)

    response = GenerativeJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        variants=variants,
        error_detail=None if job.status == "cancelled" else job.error_detail,
        failure_reason=(
            None if job.status == "cancelled" else getattr(job, "failure_reason", None)
        ),
        speech_cleanup_failure_reason=(
            None
            if job.status == "cancelled"
            else (job.assembly_plan or {}).get("speech_cleanup_failure_reason")
        ),
        created_at=job.created_at,
        updated_at=job.updated_at,
        edit_format=(job.all_candidates or {}).get("edit_format"),
        current_phase=job.current_phase,
        phase_log=list(job.phase_log or []) if job.phase_log is not None else None,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expected_phase_durations=baselines,
        archetype_fallback=archetype_fallback,
        retrying=_compute_retrying(job),
        steps=steps,
    )
    if getattr(job, "_media_overlay_preview_backfilled", False):
        # `_variants_for_response` mutated an UNLOCKED snapshot of assembly_plan.
        # Committing it as-is would clobber a concurrent worker write (finalize /
        # render / SFX pass) that landed since the unlocked read. Capture the
        # computed stamps, discard the stale snapshot's pending mutation, and
        # re-apply under a row lock.
        stamps = _collect_media_overlay_preview_stamps(job)
        job_pk = job.id
        await db.rollback()
        await _persist_media_overlay_preview_backfill(db, job_pk, stamps)
    return response


@router.post("/{job_id}/variants/{variant_id}/swap-song", response_model=GenerativeJobResponse)
async def swap_song(
    job_id: str,
    variant_id: str,
    req: SwapSongRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant against a different library song (async re-slot)."""
    job = await _load_generative_job(job_id, db, current_user)
    await dispatch_swap_song(job, variant_id, new_track_id=req.new_track_id, db=db)
    log.info(
        "generative_swap_song", job_id=str(job.id), variant_id=variant_id, track_id=req.new_track_id
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/retext", response_model=GenerativeJobResponse)
async def retext(
    job_id: str,
    variant_id: str,
    req: RetextRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with user-supplied intro text, or remove the text."""
    job = await _load_generative_job(job_id, db, current_user)
    pending_publish = dispatch_retext(
        job, variant_id, text=req.text, remove=req.remove, publish=False
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info("generative_retext", job_id=str(job.id), variant_id=variant_id, remove=req.remove)
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.put("/{job_id}/variants/{variant_id}/lyrics", response_model=GenerativeJobResponse)
async def set_variant_lyrics(
    job_id: str,
    variant_id: str,
    req: LyricsSectionRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Toggle lyrics or replace lyric line overrides, then full re-render."""
    job = await _load_generative_job(job_id, db, current_user)
    enabled = req.enabled if "enabled" in req.model_fields_set else _UNSET
    line_overrides = req.line_overrides if "line_overrides" in req.model_fields_set else _UNSET
    await dispatch_set_lyrics(
        db,
        job,
        variant_id,
        enabled=enabled,
        line_overrides=line_overrides,
    )
    log.info(
        "generative_set_lyrics",
        job_id=str(job.id),
        variant_id=variant_id,
        enabled=req.enabled,
        has_line_overrides="line_overrides" in req.model_fields_set,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.put("/{job_id}/variants/{variant_id}/orientation", response_model=GenerativeJobResponse)
async def set_variant_orientation(
    job_id: str,
    variant_id: str,
    req: OrientationRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Set portrait/landscape output for one variant, then full re-render."""
    job = await _load_generative_job(job_id, db, current_user)
    await dispatch_set_orientation(
        db,
        job,
        variant_id,
        orientation=req.orientation,
        revision_number=req.revision_number,
        base_generation=req.base_generation,
    )
    log.info(
        "generative_set_orientation",
        job_id=str(job.id),
        variant_id=variant_id,
        orientation=req.orientation,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/change-style", response_model=GenerativeJobResponse)
async def change_style(
    job_id: str,
    variant_id: str,
    req: ChangeStyleRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with a different curated text style set (async).

    Unlike swap-song this applies to ALL variants — the style set governs the AI
    intro on the text variants and the lyric typography on the lyrics variant.
    """
    job = await _load_generative_job(job_id, db, current_user)
    pending_publish = dispatch_change_style(
        job, variant_id, style_set_id=req.style_set_id, publish=False
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "generative_change_style",
        job_id=str(job.id),
        variant_id=variant_id,
        style_set_id=req.style_set_id,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/intro-size", response_model=GenerativeJobResponse)
async def set_intro_size(
    job_id: str,
    variant_id: str,
    req: SetIntroSizeRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with a user-pinned AI-intro font size (the ±size nudge)."""
    job = await _load_generative_job(job_id, db, current_user)
    pending_publish = dispatch_set_intro_size(
        job, variant_id, text_size_px=req.text_size_px, publish=False
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "generative_set_intro_size",
        job_id=str(job.id),
        variant_id=variant_id,
        px=req.text_size_px,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post(
    "/{job_id}/variants/{variant_id}/caption-position", response_model=GenerativeJobResponse
)
async def set_caption_position(
    job_id: str,
    variant_id: str,
    req: CaptionPositionRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Set caption vertical position and reburn the captioned variant."""
    job = await _load_generative_job(job_id, db, current_user)
    # The dispatcher row-locks, commits the margin + gen mint, then enqueues
    # (R1-1 commit-before-enqueue) — no route-side commit needed.
    margin_v = await dispatch_set_caption_position(job.id, variant_id, y_frac=req.y_frac, db=db)
    log.info(
        "generative_set_caption_position",
        job_id=str(job.id),
        variant_id=variant_id,
        y_frac=req.y_frac,
        margin_v=margin_v,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post(
    "/{job_id}/variants/{variant_id}/set-intro-timing", response_model=GenerativeJobResponse
)
async def set_intro_timing(
    job_id: str,
    variant_id: str,
    req: SetIntroTimingRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with user-pinned intro overlay timing (drag the intro bar)."""
    job = await _load_generative_job(job_id, db, current_user)
    pending_publish = dispatch_set_intro_timing(
        job,
        variant_id,
        start_s=req.start_s,
        end_s=req.end_s,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "generative_set_intro_timing",
        job_id=str(job.id),
        variant_id=variant_id,
        start_s=req.start_s,
        end_s=req.end_s,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.patch("/{job_id}/variants/{variant_id}/scene-timing", response_model=GenerativeJobResponse)
async def patch_scene_timing(
    job_id: str,
    variant_id: str,
    req: PatchSceneTimingRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Persist user-pinned scene timing overrides (applied on next re-render)."""
    job = await _load_generative_job(job_id, db, current_user)
    dispatch_patch_scene_timing(
        job,
        variant_id,
        overrides=[o.model_dump() for o in req.overrides],
    )
    await db.commit()
    log.info(
        "generative_patch_scene_timing",
        job_id=str(job.id),
        variant_id=variant_id,
        override_count=len(req.overrides),
    )
    return GenerativeJobResponse(job_id=str(job.id), status="ready")


@router.post("/{job_id}/variants/{variant_id}/edit", response_model=GenerativeJobResponse)
async def edit_variant(
    job_id: str,
    variant_id: str,
    req: EditVariantRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Apply a whole instant-edit session (text + style + size) in ONE re-render.

    The browser previews these edits at 0 latency (base video + client overlay) and
    commits them here on "Done". Supersedes chaining /retext + /change-style +
    /intro-size, which would enqueue one render each.
    """
    job = await _load_generative_job(job_id, db, current_user)
    # Tri-state (see EditVariantRequest.carousel_moment / dispatch_edit_variant):
    # absent from the request -> _UNSET (leave unchanged); explicit top-level
    # `null` -> None (remove); an object -> only the fields the client
    # actually set (exclude_unset), so an omitted nested field merges over
    # the persisted value instead of being treated as an explicit null.
    if "carousel_moment" not in req.model_fields_set:
        carousel_moment_field: object = _UNSET
    elif req.carousel_moment is None:
        carousel_moment_field = None
    else:
        carousel_moment_field = req.carousel_moment.model_dump(exclude_unset=True)
    pending_publish = dispatch_edit_variant(
        job,
        variant_id,
        text=req.text,
        remove_text=req.remove_text,
        style_set_id=req.style_set_id,
        text_size_px=req.text_size_px,
        intro_layout=req.intro_layout,
        font_family=req.font_family,
        effect=req.effect,
        text_color=req.text_color,
        cluster_hero_font=req.cluster_hero_font,
        cluster_body_font=req.cluster_body_font,
        cluster_accent_font=req.cluster_accent_font,
        cluster_hero_size_px=req.cluster_hero_size_px,
        cluster_body_size_px=req.cluster_body_size_px,
        cluster_accent_size_px=req.cluster_accent_size_px,
        text_behind_subject=req.text_behind_subject,
        carousel_moment=carousel_moment_field,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "generative_edit_variant",
        job_id=str(job.id),
        variant_id=variant_id,
        has_text=req.text is not None,
        remove_text=req.remove_text,
        style_set_id=req.style_set_id,
        text_size_px=req.text_size_px,
        intro_layout=req.intro_layout,
        font_family=req.font_family,
        effect=req.effect,
        text_color=req.text_color,
        cluster_hero_font=req.cluster_hero_font,
        cluster_body_font=req.cluster_body_font,
        cluster_accent_font=req.cluster_accent_font,
        text_behind_subject=req.text_behind_subject,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.get("/{job_id}/variants/{variant_id}/timeline", response_model=TimelineResponse)
async def get_variant_timeline(
    job_id: str,
    variant_id: str,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> TimelineResponse:
    """The variant's effective clip timeline + the job's full clip pool.

    Readable for the same modes as the status endpoint (the plan item page opens
    the same editor). `editable=false` carries a `reason` instead of erroring.
    """
    job = await _load_generative_job(
        job_id,
        db,
        current_user,
        allowed_modes=_READABLE_MODES,
        with_for_update=False,
    )
    return TimelineResponse(**dispatch_get_timeline(job, variant_id))


@router.get("/{job_id}/variants/{variant_id}/lyric-seeds", response_model=LyricSeedsResponse)
async def get_variant_lyric_seeds(
    job_id: str,
    variant_id: str,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> LyricSeedsResponse:
    """Instant-materialize seed elements for the editor's Lyrics toggle.

    Lyrics-as-optional-elements (LYRICS_OPTIONAL_ENABLED): read-only, never
    persists anything. 404 when the flag is off; 422 when the variant has no
    matched track / no renderable cached lyrics.
    """
    job = await _load_generative_job(
        job_id,
        db,
        current_user,
        allowed_modes=_READABLE_MODES,
        with_for_update=False,
    )
    return LyricSeedsResponse(**await dispatch_get_lyric_seeds(job, variant_id, db))


@router.post("/{job_id}/variants/{variant_id}/timeline", response_model=GenerativeJobResponse)
async def edit_variant_timeline(
    job_id: str,
    variant_id: str,
    req: TimelineEditRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Persist a user-edited clip timeline and re-render the variant from it."""
    job = await _load_generative_job(job_id, db, current_user)
    await dispatch_edit_timeline(job, variant_id, req, db=db)
    log.info(
        "generative_edit_timeline",
        job_id=str(job.id),
        variant_id=variant_id,
        slots=len(req.slots),
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.delete("/{job_id}/variants/{variant_id}/timeline", response_model=GenerativeJobResponse)
async def reset_variant_timeline(
    job_id: str,
    variant_id: str,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Discard the user timeline and re-render the variant from the AI timeline."""
    job = await _load_generative_job(job_id, db, current_user)
    await dispatch_reset_timeline(job, variant_id, db=db)
    log.info("generative_reset_timeline", job_id=str(job.id), variant_id=variant_id)
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/mix", response_model=GenerativeJobResponse)
async def set_mix(
    job_id: str,
    variant_id: str,
    req: SetMixRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a voiceover variant at a new voice/bed mix (the mix slider)."""
    job = await _load_generative_job(job_id, db, current_user)
    pending_publish = dispatch_set_mix(job, variant_id, mix=req.mix, publish=False)
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info("generative_set_mix", job_id=str(job.id), variant_id=variant_id, mix=req.mix)
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")
