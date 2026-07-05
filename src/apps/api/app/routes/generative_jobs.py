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

A generative job needs no pre-selected song or template — the orchestrator auto-matches
a track, writes its own intro text, and renders three variants. Per-variant state lives
in `Job.assembly_plan["variants"]`, which the status endpoint surfaces directly.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.agents._schemas.text_element import (
    append_ai_text_tombstones,
    merge_projected_text_elements_for_variant,
)
from app.auth import CurrentUserOrSynthetic, ensure_job_owner
from app.database import get_db
from app.models import Job, MusicTrack, User
from app.routes.admin_music import _validate_clip_path_prefixes, _validate_voiceover_path
from app.services.media_overlay_preview import (
    convert_heif_overlay_preview,
    is_heif_overlay,
    nonblank_str,
)
from app.storage import signed_get_url

log = structlog.get_logger()
router = APIRouter()

_MAX_CLIPS = 20

# TextElement feature flag (kill switch).  Apply:
#   fly secrets set TEXT_ELEMENTS_ENABLED=false --app nova-video + worker restart.
_TEXT_ELEMENTS_ENABLED = os.getenv("TEXT_ELEMENTS_ENABLED", "true").lower() != "false"

# Maximum number of TextElement entries accepted per PUT (A—).
_TEXT_ELEMENTS_MAX = 50

# Variant blobs live under `generative-jobs/` which is NOT in the GCS delete rule
# (infra/gcs-lifecycle.json) — the bytes persist indefinitely. But `output_url` is
# persisted at render time as a 1-day-TTL signed URL (storage.upload_public_read),
# so after ~24h the stored URL is an expired signature pointing at live bytes: the
# item still reads "ready" but `<video>` gets a 400 ExpiredToken. Re-sign on every
# read from the persisted relative key (`video_path`) so playback URLs are always
# fresh. 6h comfortably covers a viewing session; the page re-polls to refresh.
PLAYBACK_URL_TTL_MIN = 360
_HEIF_PREVIEW_BACKFILL_ATTEMPTED: set[str] = set()


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
        # Reject arbitrary bucket keys — only upload-endpoint prefixes are allowed.
        return _validate_clip_path_prefixes(v)

    @field_validator("voiceover_gcs_path")
    @classmethod
    def validate_voiceover(cls, v: str | None) -> str | None:
        return _validate_voiceover_path(v) if v else v


class GenerativeJobResponse(BaseModel):
    job_id: str
    status: str


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
    music_track_id: str | None = None
    track_title: str | None = None
    text_mode: str | None = None
    style_set_id: str | None = None
    rank: int | None = None
    intro_text_size_px: int | None = None
    intro_size_source: str | None = None
    resolved_archetype: str | None = None
    mix: float | None = None
    # Background-sound (voice/bed) level for narrated variants — None means Nova's
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
    # Narrated on-video caption editor: editable cues [{text, start_s, end_s}]
    # (assembled-time). Present only on narrated variants with an editable base.
    caption_cues: list[dict] | None = None
    # Subtitles on/off, independent of caption_cues count — off always yields the
    # caption-free burn even when cues are stored, so toggling back on needs no
    # re-transcription. None on legacy variants predating this field; the editor
    # treats missing as enabled (matches the render-time default of True).
    captions_enabled: bool | None = None
    # User-pinned independent overrides (decoupled from style_set_id).
    # null when not pinned; the renderer uses the style-set value.
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

    model_config = {"extra": "allow"}


class GenerativeJobStatusResponse(BaseModel):
    job_id: str
    status: str
    variants: list[dict]
    error_detail: str | None
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


# ── Timeline editor schemas ────────────────────────────────────────────────────

_TIMELINE_MAX_SLOTS = 50
# Server-side guardrails on a user-edited timeline. The floor keeps a slot long
# enough to register as a cut (and clear of xfade window collapse); it applies
# only to slots whose window the user CHANGED — the worker itself produces
# sub-floor slots (1 beat at fast BPM, footage trims) that must round-trip.
# The ceiling matches the product's sub-60s short-form contract.
TIMELINE_MIN_SLOT_S = 0.6
TIMELINE_MAX_TOTAL_S = 60.0
# Only the montage text variants carry a user-editable slot timeline. Lyrics are
# beat/line synced (re-cutting breaks sync), voiceover variants are fit to the
# voice bed, talking_head has no slot layout at all.
_TIMELINE_EDITABLE_VARIANTS = ("song_text", "original_text")


class TimelineSlotEdit(BaseModel):
    """One slot as posted by the timeline editor.

    `slot_id=None` marks a NEW slot (the server assigns a uuid4). `clip_index`
    indexes into `job.all_candidates["clip_paths"]` — clients never send paths.
    Beat slots size in `duration_beats` (walked against the real grid); slots
    with `duration_beats=None` (no-grid variants, or footage-trimmed slots on
    grid variants) send their exact window in `duration_s`.
    """

    slot_id: str | None = None
    clip_index: int
    in_s: float
    duration_beats: int | None = None
    duration_s: float | None = None
    removed: bool = False


class TimelineEditRequest(BaseModel):
    slots: list[TimelineSlotEdit]

    @field_validator("slots")
    @classmethod
    def validate_slot_count(cls, v: list[TimelineSlotEdit]) -> list[TimelineSlotEdit]:
        # Payload cap: a 9:16 sub-60s edit never needs more than 50 cuts.
        if len(v) > _TIMELINE_MAX_SLOTS:
            raise ValueError(f"Maximum {_TIMELINE_MAX_SLOTS} timeline slots allowed")
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

    model_config = {"extra": "allow"}


class TimelineClipOut(BaseModel):
    """One entry of the job's full clip pool (including clips not currently used)."""

    clip_index: int
    signed_url: str | None = None
    duration_s: float | None = None
    used: bool = False


class TimelineResponse(BaseModel):
    editable: bool
    reason: str | None = None
    beat_grid: list[float]
    total_duration_s: float
    has_user_edits: bool
    slots: list[TimelineSlotOut]
    clips: list[TimelineClipOut]


# ── Transactional editor commit (E2) ──────────────────────────────────────────


class EditorCommitMix(BaseModel):
    """Editor mix section. `music_level` maps onto the existing per-variant `mix`
    semantics (voice/bed balance — voiceover variants only). `original_level` is
    persisted for round-tripping but not yet honored by the render pipeline."""

    music_level: float | None = Field(None, ge=0.0, le=1.0)
    original_level: float | None = Field(None, ge=0.0, le=1.0)


class EditorCommitRequest(BaseModel):
    """One atomic editor Save: every provided section validates first; nothing
    persists unless ALL sections are valid. `base_generation` is the baseline the
    client loaded (the variant's `render_generation_id`, falling back to
    `render_finished_at` for variants never edited through the editor) — a moved
    baseline means another tab/render won and the commit 409s (baseline_conflict).
    """

    text_elements: list[dict] | None = None
    timeline_slots: list[TimelineSlotEdit] | None = None
    mix: EditorCommitMix | None = None
    sound_effects: list[dict] | None = None
    media_overlays: list[dict] | None = None
    title: str | None = Field(None, max_length=300)
    base_generation: str = ""

    @field_validator("timeline_slots")
    @classmethod
    def validate_commit_slot_count(
        cls, v: list[TimelineSlotEdit] | None
    ) -> list[TimelineSlotEdit] | None:
        if v is not None and len(v) > _TIMELINE_MAX_SLOTS:
            raise ValueError(f"Maximum {_TIMELINE_MAX_SLOTS} timeline slots allowed")
        return v


class EditorCommitSections(BaseModel):
    text_elements: bool
    timeline: bool
    mix: bool
    sound_effects: bool
    media_overlays: bool
    title: bool


class EditorCommitResponse(BaseModel):
    ok: bool
    generation: str
    sections: EditorCommitSections


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
_READABLE_MODES = ("generative", "content_plan")


async def _load_generative_job(
    job_id: str,
    db: AsyncSession,
    current_user: User,
    *,
    allowed_modes: tuple[str, ...] = ("generative",),
) -> Job:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.mode not in allowed_modes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    ensure_job_owner(job.user_id, current_user)
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
    changed = _lazy_backfill_media_overlay_previews(job)
    if changed:
        setattr(job, "_media_overlay_preview_backfilled", True)

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
        # Intro mode (D19): expose the authoritative mode plus the FE-convenience
        # `sequence_synced` boolean. Legacy variants (pre-intro_mode) fall back to
        # the persisted intro_layout — they can never be "sequence".
        intro_mode = v.get("intro_mode") or v.get("intro_layout") or None
        v = {**v, "intro_mode": intro_mode, "sequence_synced": intro_mode == "sequence"}
        # Drop server-only sequence internals from the polled payload: the full
        # per-word `transcript` and parallel `scenes` are read by the reburn path
        # from the persisted Job row, never by the FE. Returning them on every
        # status poll is wasted bandwidth and needless exposure of the footage
        # transcript to the client. (`v` is already a fresh copy here.)
        v.pop("transcript", None)
        raw_scenes = v.pop("scenes", None) or []
        v["scene_timings"] = [
            {"text": s.get("text", ""), "start_s": s.get("start_s"), "end_s": s.get("end_s")}
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
                "text_elements": merge_projected_text_elements_for_variant(v),
                "text_elements_user_edited": v.get("text_elements_user_edited", False),
                "geometry_materialized_at_version": v.get("geometry_materialized_at_version"),
                "text_elements_materialized_from": v.get("text_elements_materialized_from"),
            }
        # E4: per-variant editor capabilities — one server-side truth source for
        # which editor surfaces the FE may enable (no endpoint probing).
        v = {**v, "editor_capabilities": _editor_capabilities(job, v)}
        out.append(v)
    return out


def _find_variant(job: Job, variant_id: str) -> dict | None:
    return next((v for v in _variants_of(job) if v.get("variant_id") == variant_id), None)


# ── Shared variant-edit validation + dispatch ───────────────────────────────────
# These are public (no leading underscore) so the content-plan routes
# (`routes/plan_items.py`) can reuse them verbatim across modules — content_plan
# jobs share the generative per-variant assembly_plan shape, so the validation
# rules and the `regenerate_generative_variant` dispatch are identical. The only
# difference between the two surfaces is how the Job is loaded (public job-id vs
# ownership-checked plan item), so that stays in each route; everything below the
# loaded Job is single-sourced here.


def require_editable_variant(job: Job, variant_id: str) -> dict:
    """Return the variant; 404 if unknown, 409 if it's already re-rendering."""
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    if variant.get("render_status") == "rendering":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Variant is already re-rendering."
        )
    return variant


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

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, new_track_id=new_track_id)


# Mode-neutral: a sequence variant is either transcript-synced (voiceover) OR
# rhythm-mode (an authored quote over music, no voiceover) — the copy must not
# claim a voiceover that rhythm variants don't have.
_SEQUENCE_TEXT_LOCKED_DETAIL = (
    "Text is synced for this Editorial variant — switch layout to Classic to edit text."
)


def dispatch_retext(job: Job, variant_id: str, *, text: str | None, remove: bool) -> None:
    """Validate + enqueue an intro-text edit/removal for one variant.

    T8: the sequence lock that used to 422 here is removed.  PUT /text-elements
    handles multi-block editorial layout edits; dispatch_retext now proceeds for
    all variant types including sequence (intro_text override on re-render).
    """
    # Guard: raises 404/409 when variant is unknown or already rendering.
    require_editable_variant(job, variant_id)
    if not remove and not (text and text.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide `text` to update, or set `remove=true` to clear the overlay.",
        )

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(
        str(job.id),
        variant_id,
        override_text=(text.strip() if (text and not remove) else None),
        remove_text=bool(remove),
    )


# A caption edit may never bloat the JSONB / slow the libass burn unbounded; the
# sibling timeline editor caps at 50 slots, narration rarely exceeds a few dozen lines.
_MAX_CAPTION_CUES = 300


class CaptionWord(BaseModel):
    # Same inf/nan rejection as CaptionCue — a poisoned word time crashes the reburn.
    model_config = {"allow_inf_nan": False}

    text: str
    start_s: float
    end_s: float


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

    @field_validator("words")
    @classmethod
    def _cap_words(cls, v: list[CaptionWord] | None) -> list[CaptionWord] | None:
        if v is not None and len(v) > 100:
            raise ValueError("Too many words on one caption line (max 100).")
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
_CAPTION_EDIT_ARCHETYPES = frozenset({"narrated", "subtitled"})


def _is_editable_caption_variant(variant: dict) -> bool:
    """True iff this variant is an editable caption variant.

    Gates the caption endpoints. `base_video_path` alone is NOT sufficient — the
    agent_text montage fast-reburn base sets it too; burning captions over a
    montage's text-free base would destroy that variant. Only caption-capable
    archetypes (narrated voiceover, subtitled single-clip) ship `caption_cues`, so
    require one of those.
    """
    return variant.get("resolved_archetype") in _CAPTION_EDIT_ARCHETYPES and bool(
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
    await _patch_narrated_variant(job_id, variant_id, {"voiceover_caption_font": caption_font}, db)


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


def _mark_variant_rendering(job: Job, variant_id: str) -> None:
    """Persist render_status="rendering" synchronously at dispatch (the swap-song
    pattern) so the 409 gate closes IMMEDIATELY — without it, two dispatches in the
    enqueue→dequeue window both pass the gate and race to a last-writer-wins state
    (e.g. a reburn of old cues landing after a re-transcribe)."""
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}


def dispatch_apply_captions(job: Job, variant_id: str) -> None:
    """Reburn the variant's (persisted, hand-edited) caption cues onto its
    caption-free base — the Apply step of the on-video caption editor."""
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if not _is_editable_caption_variant(variant):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Captions can only be applied on captioned variants.",
        )
    _mark_variant_rendering(job, variant_id)
    from app.tasks.generative_build import reburn_narrated_captions  # noqa: PLC0415

    reburn_narrated_captions.delay(str(job.id), variant_id)


def dispatch_retranscribe_captions(job: Job, variant_id: str, *, language: str) -> None:
    """Re-transcribe a subtitled variant's own audio in a new language and reburn (D5
    override). Subtitled-only — narrated captions come from a separate voiceover. This
    REPLACES the current cues + any hand-edits; the frontend confirms first."""
    lang = (language or "").strip().lower()
    if lang not in _SUBTITLED_CAPTION_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported caption language.",
        )
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 if rendering
    if variant.get("resolved_archetype") != "subtitled":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Changing the caption language is only available on subtitled videos.",
        )
    if not variant.get("base_video_path"):
        # A no-speech subtitled variant has no caption-free base — the worker would
        # no-op. Surface it at the route like the sibling caption endpoints do.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This video has no captions to re-transcribe.",
        )
    _mark_variant_rendering(job, variant_id)
    from app.tasks.generative_build import retranscribe_subtitled_captions  # noqa: PLC0415

    retranscribe_subtitled_captions.delay(str(job.id), variant_id, lang)


def dispatch_change_style(job: Job, variant_id: str, *, style_set_id: str) -> None:
    """Validate + enqueue a text-style-set change for one variant."""
    from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

    require_editable_variant(job, variant_id)
    if style_set_id not in set(style_set_ids(applies_to="generative")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown or non-generative style set.",
        )

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, style_set_id=style_set_id)


def dispatch_set_intro_size(job: Job, variant_id: str, *, text_size_px: int) -> None:
    """Validate + enqueue a user intro font-size override for one variant."""
    from app.pipeline.overlay_sizing import clamp_intro_px  # noqa: PLC0415

    variant = require_editable_variant(job, variant_id)
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
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, size_override_px=px)


def dispatch_set_intro_timing(job: Job, variant_id: str, *, start_s: float, end_s: float) -> None:
    """Validate + enqueue a user intro-timing override for one variant."""
    variant = require_editable_variant(job, variant_id)
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
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            v["intro_start_s"] = start_s
            v["intro_end_s"] = end_s
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(
        str(job.id),
        variant_id,
        intro_start_s_override=start_s,
        intro_end_s_override=end_s,
    )


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
    variant_context: dict | None = None,
) -> list[dict]:
    """Validate a full media-overlay replacement list for one user's namespace."""
    from app.agents._schemas.media_overlay import (  # noqa: PLC0415
        coerce_media_overlays,
        validate_overlay_gcs_path,
    )

    _user_prefix = f"users/{user_id}/"
    validated: list[dict] = []
    if overlays_raw:
        cards = coerce_media_overlays(overlays_raw) or []
        for card in cards:
            try:
                validate_overlay_gcs_path(card.src_gcs_path)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid overlay asset path: {exc}",
                ) from exc
            if not card.src_gcs_path.startswith(_user_prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Overlay asset path must be under '{_user_prefix}': {card.src_gcs_path!r}"
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
                if not card.preview_gcs_path.startswith(_user_prefix):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Overlay preview path must be under '{_user_prefix}': "
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


def validate_sound_effects_for_user(*, sfx_raw: list[dict], user_id: str) -> list[dict]:
    """Validate a full sound-effect placement replacement list for one user."""
    from app.agents._schemas.sound_effect import (  # noqa: PLC0415
        coerce_sound_effects,
        validate_sfx_gcs_path,
    )

    _user_prefix = f"users/{user_id}/"
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
            if is_user_path and not placement.src_gcs_path.startswith(_user_prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"SFX asset path must be under '{_user_prefix}': {placement.src_gcs_path!r}"
                    ),
                )
            validated.append(placement.model_dump())
    return validated


def dispatch_set_media_overlays(
    job: Job,
    variant_id: str,
    *,
    overlays_raw: list[dict],
    user_id: str,
) -> None:
    """Validate + enqueue a media-overlay card apply-pass for one variant.

    Full-replace semantics: the caller sends the entire new card list.
    An empty list clears all cards (restores the clean variant from
    pre_media_overlay_video_path if available).

    Persists render_status="rendering" on the variant BEFORE enqueuing so the
    frontend immediately reflects the in-progress state — same pattern as
    dispatch_edit_timeline (persist first, enqueue second).
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

    # Persist render_status="rendering" first (row-locked by the DB session the
    # route holds), then enqueue — prevents a race where the worker reads "ready"
    # and an immediate second PUT sees "ready" and double-enqueues.
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    # NOTE: .delay() sends to Redis immediately (synchronously). The DB commit
    # in the caller's route (`await db.commit()`) happens after this function
    # returns. The window where the task sees an uncommitted row is milliseconds —
    # the same accepted race as other dispatch_* functions in this module.
    # (Celery's ALWAYS_EAGER test mode is the exception — tasks run inline.)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    # Route overlay-only tasks to the dedicated overlay queue so they land on
    # the --pool=solo worker (overlay-jobs) rather than the prefork worker.
    # On macOS the CLIP model causes SIGSEGV in forked prefork children; the
    # solo worker avoids the fork entirely. Prod: fly.toml worker listens on
    # celery,plan-jobs,overlay-jobs so no extra process needed.
    regenerate_generative_variant.apply_async(
        args=[str(job.id), variant_id],
        kwargs={"media_overlays_override": validated},
        queue="overlay-jobs",
    )


def dispatch_set_sound_effects(
    job: Job,
    variant_id: str,
    *,
    sfx_raw: list[dict],
    user_id: str,
    db_for_glossary,  # AsyncSession for resolving sound_effect_id references
) -> None:
    """Validate + enqueue a sound-effects apply-pass for one variant.

    Full-replace semantics: the caller sends the entire new placement list.
    An empty list clears all effects (restores the clean variant from
    pre_sfx_video_path if available).

    Persists render_status="rendering" on the variant BEFORE enqueuing.
    Routes to the overlay-jobs queue (same as media overlays — solo worker,
    no CLIP model fork hazard).
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.sound_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sound effects are not available.",
        )

    require_editable_variant(job, variant_id)

    validated = validate_sound_effects_for_user(sfx_raw=sfx_raw, user_id=user_id)

    # Persist render_status="rendering" first (same pattern as dispatch_set_media_overlays).
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.apply_async(
        args=[str(job.id), variant_id],
        kwargs={"sfx_override": validated},
        queue="overlay-jobs",
    )


def validate_text_elements_payload(
    variant: dict,
    elements: list[dict],
    *,
    require_base: bool,
    strict_drop: bool = False,
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

    # A16: lyrics variant is beat-synced; re-cutting the text would break sync.
    if variant.get("text_mode") == "lyrics":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Text elements cannot be edited on a lyrics variant.",
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
    from app.agents._schemas.text_element import coerce_text_elements  # noqa: PLC0415

    validated: list[dict] = []
    if elements:
        coerced = coerce_text_elements(elements)
        if strict_drop and len(coerced or []) != len(elements):
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
            validated = [e.model_dump() for e in coerced]
            validated = append_ai_text_tombstones(variant, validated)
        elif variant.get("text_elements_user_edited"):
            # Explicit empty list = delete all generated AI text. Persist tombstones
            # so the read adapter does not resurrect projected bars on reload.
            validated = append_ai_text_tombstones(variant, [])
    return validated, _is_first_sequence_edit


def dispatch_set_text_elements(
    job: Job,
    variant_id: str,
    *,
    elements: list[dict],
    render: bool = True,
) -> None:
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

    variant = require_editable_variant(job, variant_id)

    validated, _is_first_sequence_edit = validate_text_elements_payload(
        variant, elements, require_base=render
    )

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
                v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")

    if render:
        from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

        # Route to the overlay-jobs queue (solo worker — avoids macOS prefork CLIP fork crash).
        regenerate_generative_variant.apply_async(
            args=[str(job.id), variant_id],
            kwargs={"render_gen_id": render_gen_id},
            queue="overlay-jobs",
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
) -> None:
    """Validate + enqueue a combined text/style/size/layout edit as ONE re-render.

    The instant editor batches an entire editing session into a single commit, so
    the user pays for one render instead of one per field. Reuses the same
    validation rules as the per-field dispatchers; `regenerate_generative_variant`
    already accepts all overrides together.
    """
    variant = require_editable_variant(job, variant_id)

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

    # Persist render_status="rendering" before enqueuing — full dict replacement so
    # SQLAlchemy tracks the change without flag_modified.
    _variants = list((job.assembly_plan or {}).get("variants") or [])
    for _v in _variants:
        if _v.get("variant_id") == variant_id:
            _v["render_status"] = "rendering"
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": _variants}

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

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
    )


def dispatch_set_mix(job: Job, variant_id: str, *, mix: float) -> None:
    """Validate + enqueue a voice/bed mix change for one voiceover variant."""
    variant = require_editable_variant(job, variant_id)
    # Only voiceover variants carry a voice bed to rebalance. A song/original/lyrics
    # variant has no `mix`, so adjusting it is a no-op — reject rather than spin up a
    # render that changes nothing. (Voiceover variants persist a non-None `mix`.)
    if variant.get("mix") is None and not variant_id.startswith("voiceover"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This edit has no voiceover to mix.",
        )

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, mix_override=float(mix))


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
    _mark_variant_rendering(job, variant_id)
    await db.commit()
    from app.tasks.generative_build import reburn_narrated_bed_level  # noqa: PLC0415

    reburn_narrated_bed_level.delay(str(job_id), variant_id, float(bed_level))


# ── Timeline editor: eligibility + GET/POST/DELETE dispatch ─────────────────────
# Single-sourced here so plan_items.py wraps the same logic verbatim (mirrors
# dispatch_retext & friends). The worker (lane 2) writes `ai_timeline` on each
# variant at render time and accepts `timeline_override` on regenerate.


def _durable_sources_prefix(job: Job) -> str:
    """Worker-copied per-job sources persist here (NOT in the 24h GCS delete rule).

    Anything else on a slot's `source_gcs_path` is a legacy/pre-feature job whose
    raw uploads may already be swept — treated as expired for editing purposes.
    """
    return f"generative-jobs/{job.id}/sources/"


def _timeline_parts(variant: dict) -> tuple[list[dict], list[dict], list[float]]:
    """(ai_slots, user_slots, beat_grid) with null-safe defaults."""
    ai = variant.get("ai_timeline") or {}
    ai_slots = [s for s in (ai.get("slots") or []) if isinstance(s, dict)]
    user = variant.get("user_timeline") or {}
    user_slots = [s for s in (user.get("slots") or []) if isinstance(s, dict)]
    beat_grid = [float(b) for b in (ai.get("beat_grid") or [])]
    return ai_slots, user_slots, beat_grid


def _timeline_ineligibility(job: Job, variant: dict) -> str | None:
    """First matching reason this variant's timeline can't be edited, or None."""
    from app.config import settings  # noqa: PLC0415

    if not settings.GENERATIVE_TIMELINE_EDITOR_ENABLED:
        return "disabled"
    vid = str(variant.get("variant_id") or "")
    if vid == "song_lyrics" or variant.get("text_mode") == "lyrics":
        return "lyrics_sync"  # lyric lines are beat-synced; re-cutting breaks sync
    if vid.startswith("voiceover"):
        return "voiceover_bed_fit"  # slots are fit to the voice bed, not user cuts
    if variant.get("resolved_archetype") == "talking_head":
        return "no_slot_timeline"  # talking_head renders have no slot layout
    if vid not in _TIMELINE_EDITABLE_VARIANTS:
        return "unsupported_variant"
    ai_slots, _, _ = _timeline_parts(variant)
    if not ai_slots:
        return "no_timeline"  # legacy variant rendered before lane-2 instrumentation
    prefix = _durable_sources_prefix(job)
    if any(not str(s.get("source_gcs_path") or "").startswith(prefix) for s in ai_slots):
        # Non-durable sources = legacy job cutting from 24h-swept uploads.
        return "sources_expired"
    return None


def _timeline_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _editor_capabilities(job: Job, variant: dict) -> dict:
    """E4: server-derived editor capability map for one variant (kills FE 404-probing).

    Cheap by design — flag reads, string checks, and the already-persisted
    source-liveness prefix check inside `_timeline_ineligibility`. No GCS calls.
    `reason` carries the timeline-ineligibility code (the same vocabulary the GET
    /timeline endpoint reports) when timeline/split are disabled, else null.
    """
    timeline_reason = _timeline_ineligibility(job, variant)
    timeline_ok = timeline_reason is None
    caption_reason = (
        "captions are edited in the captions tab"
        if variant.get("resolved_archetype") in {"narrated", "subtitled"}
        else None
    )
    from app.config import settings  # noqa: PLC0415

    effects_reason = None
    if variant.get("resolved_archetype") in _CAPTION_EDIT_ARCHETYPES:
        effects_reason = "caption_archetype"
    elif not variant.get("video_path") and not variant.get("output_url"):
        effects_reason = "no_video"
    sfx_reason = (
        "sound_effects_disabled"
        if not settings.sound_effects_enabled
        else effects_reason
    )
    overlays_reason = (
        "media_overlays_disabled"
        if not settings.media_overlays_enabled
        else effects_reason
    )
    return {
        # Lyrics variants are beat-synced — same rule as dispatch_set_text_elements.
        "text_elements": (
            _TEXT_ELEMENTS_ENABLED
            and variant.get("text_mode") != "lyrics"
            and caption_reason is None
        ),
        "timeline": timeline_ok,
        # Splitting a clip is a timeline-override operation — same eligibility.
        "split_clips": timeline_ok,
        # Mirrors dispatch_set_mix: only variants carrying a voice bed can rebalance.
        "mix": (
            variant.get("mix") is not None
            or str(variant.get("variant_id") or "").startswith("voiceover")
        ),
        "sfx": sfx_reason is None,
        "overlays": overlays_reason is None,
        "reason": caption_reason or timeline_reason,
        "sfx_reason": sfx_reason,
        "overlays_reason": overlays_reason,
    }


def dispatch_get_timeline(job: Job, variant_id: str) -> dict:
    """Effective timeline (user_timeline if present, else ai_timeline) + clip pool.

    Read-only and side-effect free; never raises for an ineligible variant — it
    reports `editable=False` + `reason` so the frontend can render the right copy.
    """
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    reason = _timeline_ineligibility(job, variant)
    ai_slots, user_slots, beat_grid = _timeline_parts(variant)
    has_user_edits = bool(user_slots)
    effective = user_slots if has_user_edits else ai_slots
    active = [s for s in effective if not s.get("removed")]
    total = sum(float(s.get("duration_s") or 0.0) for s in active)
    used_indices = {s.get("clip_index") for s in active}

    # Source durations are only known where the worker probed them (ai_timeline).
    dur_by_idx: dict[int, float] = {}
    for s in ai_slots:
        idx = s.get("clip_index")
        if idx is not None and s.get("source_duration_s") is not None:
            dur_by_idx.setdefault(idx, float(s["source_duration_s"]))

    clips: list[dict] = []
    for i, path in enumerate((job.all_candidates or {}).get("clip_paths") or []):
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
        "slots": [dict(s) for s in effective],
        "clips": clips,
    }


async def persist_user_timeline(
    db: AsyncSession, job_id: str, variant_id: str, slots: list[dict] | None
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
            else:
                updated["user_timeline"] = {"slots": slots}
            variants[i] = updated
            break
    plan["variants"] = variants
    job.assembly_plan = plan
    await db.commit()


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
    # by slot_id: the 0.6s floor only applies to slots whose window CHANGED — the
    # worker legitimately produces sub-floor slots (1 beat at fast BPM, footage
    # trims), and an unmodified round-trip must never 422.
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
    for order, e in enumerate(slots):
        duration_s = e.duration_s
        if not e.removed:
            if beat_grid and e.duration_beats is not None and e.duration_beats >= 1:
                # Beat slot: walk the REAL grid cumulatively. Slot i's duration is
                # grid[offset+beats] - grid[offset]; the offset then advances, so the
                # same `duration_beats` can yield different seconds at different
                # positions (non-uniform grids).
                end = grid_offset + e.duration_beats
                if end > len(beat_grid) - 1:
                    raise _timeline_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_BEATS_EXHAUSTED"
                    )
                duration_s = beat_grid[end] - beat_grid[grid_offset]
                grid_offset = end
            elif e.duration_s is not None and e.duration_s > 0:
                # Seconds slot: `duration_s` is the authoritative exact window. This
                # covers no-grid variants AND footage-trimmed slots on grid variants
                # (the worker's `duration_beats: null` slots) — those never walk the
                # grid and don't advance the cursor. No step-multiple requirement:
                # the worker emits round(x, 3) durations and the render is an exact
                # window either way (quantization is a frontend nudge concern).
                duration_s = float(e.duration_s)
            else:
                # Neither a usable beat count nor a usable duration.
                raise _timeline_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_INVALID_DURATION"
                )
            if duration_s < TIMELINE_MIN_SLOT_S - 1e-9 and _window_changed(e):
                raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_TOO_SHORT")
            total += duration_s
            # Bounds against the probed source duration. New clips the AI never
            # probed have no known duration — skip; the worker's probe will clamp.
            src_dur = src_dur_by_idx.get(e.clip_index)
            if e.in_s < 0 or (src_dur is not None and e.in_s + duration_s > src_dur + 1e-6):
                raise _timeline_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_OUT_OF_BOUNDS"
                )
        meta = meta_by_idx.get(e.clip_index) or {}
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
                "duration_beats": e.duration_beats,
                "order": order,
                "moment_energy": meta.get("moment_energy"),
                "moment_description": meta.get("moment_description"),
                "removed": bool(e.removed),
            }
        )
    if total > TIMELINE_MAX_TOTAL_S + 1e-6:
        raise _timeline_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "TIMELINE_TOO_LONG")

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

    if not settings.GENERATIVE_TIMELINE_EDITOR_ENABLED:
        raise _timeline_error(status.HTTP_403_FORBIDDEN, "disabled")
    variant = require_editable_variant(job, variant_id)  # 404 unknown / 409 rendering
    # A timeline re-render re-cuts from the shared per-job sources; let any in-flight
    # sibling render finish first so two renders never race the same job row.
    if any(v.get("render_status") == "rendering" for v in _variants_of(job)):
        raise _timeline_error(status.HTTP_409_CONFLICT, "JOB_BUSY")

    resolved = resolve_timeline_slots_for_edit(job, variant, payload.slots)

    await persist_user_timeline(db, str(job.id), variant_id, resolved)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, timeline_override=resolved)


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

    await persist_user_timeline(db, str(job.id), variant_id, None)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    # Pass the AI slots as the override: the regenerate path is identical to an
    # edit, just sourced from the AI's own plan (simplest reset contract).
    regenerate_generative_variant.delay(
        str(job.id), variant_id, timeline_override=[dict(s) for s in ai_slots]
    )


# ── Transactional editor commit dispatch (E2) ───────────────────────────────────


def variant_render_baseline(variant: dict) -> str:
    """The compare-and-fail baseline a client must echo back on editor-commit.

    `render_generation_id` when the variant has ever been committed through a
    token-stamped edit; else the last `render_finished_at`; else "" (a variant
    that never finished a render has nothing to conflict with).
    """
    return str(variant.get("render_generation_id") or variant.get("render_finished_at") or "")


def prepare_editor_commit(
    job: Job,
    variant_id: str,
    payload: EditorCommitRequest,
    *,
    user_id: str | None = None,
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

    if (
        payload.text_elements is None
        and payload.timeline_slots is None
        and payload.mix is None
        and payload.sound_effects is None
        and payload.media_overlays is None
        and payload.title is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one section to commit.",
        )

    # ── Validate every provided section BEFORE any write ──────────────────────
    validated_elements: list[dict] | None = None
    materialized_from_sequence = False
    text_requires_full_render = False
    if payload.text_elements is not None:
        # The fast-reburn base is only required when this commit will take the
        # reburn path (no timeline change → no full re-assembly).
        text_requires_full_render = payload.timeline_slots is None and not bool(
            variant.get("base_video_path")
        )
        validated_elements, materialized_from_sequence = validate_text_elements_payload(
            variant,
            payload.text_elements,
            require_base=payload.timeline_slots is None and not text_requires_full_render,
            strict_drop=True,
        )

    resolved_slots: list[dict] | None = None
    if payload.timeline_slots is not None:
        resolved_slots = resolve_timeline_slots_for_edit(job, variant, payload.timeline_slots)

    mix_override: float | None = None
    if payload.mix is not None:
        # Same rule as dispatch_set_mix: only voiceover variants carry a voice
        # bed to rebalance.
        if variant.get("mix") is None and not str(variant_id).startswith("voiceover"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This edit has no voiceover to mix.",
            )
        mix_override = payload.mix.music_level

    validated_sfx: list[dict] | None = None
    if payload.sound_effects is not None:
        from app.config import settings as _settings  # noqa: PLC0415

        if not _settings.sound_effects_enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Sound effects are not available for this editor commit.",
            )
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Sound effects require a user-scoped asset namespace.",
            )
        validated_sfx = validate_sound_effects_for_user(
            sfx_raw=payload.sound_effects,
            user_id=user_id,
        )

    validated_overlays: list[dict] | None = None
    if payload.media_overlays is not None:
        from app.config import settings as _settings  # noqa: PLC0415

        if not _settings.media_overlays_enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Media overlays are not available for this editor commit.",
            )
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Media overlays require a user-scoped asset namespace.",
            )
        validated_overlays = validate_media_overlays_for_user(
            overlays_raw=payload.media_overlays,
            user_id=user_id,
            variant_context=variant,
        )

    # ── Stale-baseline compare-and-fail (multi-tab / superseded-render safety) ─
    if payload.base_generation != variant_render_baseline(variant):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="baseline_conflict")

    # ── Stage the single atomic job-JSON write ─────────────────────────────────
    has_render_section = (
        validated_elements is not None
        or resolved_slots is not None
        or payload.mix is not None
        or validated_sfx is not None
        or validated_overlays is not None
    )
    new_gen = uuid.uuid4().hex if has_render_section else None

    variants = list((job.assembly_plan or {}).get("variants") or [])
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
        if resolved_slots is not None:
            updated["user_timeline"] = {"slots": resolved_slots}
        if payload.mix is not None:
            if payload.mix.music_level is not None:
                updated["mix"] = float(payload.mix.music_level)
            if payload.mix.original_level is not None:
                # Round-trip persistence only — not yet honored by the renderer.
                updated["original_audio_level"] = float(payload.mix.original_level)
        if validated_sfx is not None:
            updated["sound_effects"] = validated_sfx or None
        if validated_overlays is not None:
            updated["media_overlays"] = validated_overlays or None
        if new_gen is not None:
            updated["render_generation_id"] = new_gen
            updated["render_status"] = "rendering"
        variants[i] = updated
        break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}

    return {
        "generation": new_gen or payload.base_generation,
        "has_render_section": has_render_section,
        "timeline_override": resolved_slots,
        "mix_override": mix_override,
        "sfx_override": validated_sfx,
        "media_overlays_override": validated_overlays,
        "text_requires_full_render": text_requires_full_render,
        "sections": {
            "text_elements": payload.text_elements is not None,
            "timeline": payload.timeline_slots is not None,
            "mix": payload.mix is not None,
            "sound_effects": payload.sound_effects is not None,
            "media_overlays": payload.media_overlays is not None,
        },
    }


def enqueue_editor_commit_render(job_id: str, variant_id: str, prep: dict) -> None:
    """Kick exactly ONE render for a committed editor Save (call AFTER db.commit).

    Text-only commits ride the overlay-jobs queue (they take the fast-reburn
    path, mirroring PUT /text-elements); anything touching the timeline or mix
    is a full re-assembly and rides the default queue. No-op for title-only
    commits. The task carries the freshly-bumped render_gen_id so E1 can discard
    any older in-flight task's terminal write.
    """
    if not prep["has_render_section"]:
        return
    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    kwargs: dict = {"render_gen_id": prep["generation"]}
    if prep["timeline_override"] is not None:
        kwargs["timeline_override"] = prep["timeline_override"]
    if prep["mix_override"] is not None:
        kwargs["mix_override"] = float(prep["mix_override"])
    has_text_section = prep["sections"].get("text_elements") is True
    full_render = (
        prep["timeline_override"] is not None
        or prep["mix_override"] is not None
        or prep.get("text_requires_full_render") is True
    )
    if full_render or has_text_section:
        # Text/timeline/mix full re-renders read the just-persisted variant state.
        # SFX are reapplied by the worker's persisted-SFX hook after the new base lands.
        pass
    elif prep["media_overlays_override"] is not None:
        # Overlay pass is outer-video, then the worker's terminal hook reapplies the
        # just-persisted SFX if this same commit also changed sound_effects.
        kwargs["media_overlays_override"] = prep["media_overlays_override"]
    elif prep["sfx_override"] is not None:
        kwargs["sfx_override"] = prep["sfx_override"]
    is_reburn_only = (
        prep["timeline_override"] is None
        and prep["mix_override"] is None
        and prep.get("text_requires_full_render") is not True
    )
    apply_kwargs: dict = {"args": [job_id, variant_id], "kwargs": kwargs}
    if is_reburn_only:
        # Overlay-jobs queue: solo worker — avoids macOS prefork CLIP fork crash.
        apply_kwargs["queue"] = "overlay-jobs"
    regenerate_generative_variant.apply_async(**apply_kwargs)


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("", response_model=GenerativeJobResponse, status_code=status.HTTP_201_CREATED)
async def create_generative_job(
    req: CreateGenerativeJobRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Create a generative edit job (auto song + AI text, three variants)."""
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

    job = await _load_generative_job(job_id, db, current_user, allowed_modes=_READABLE_MODES)

    # Count pending/rendering variants for baseline scaling.
    variants_list = (job.assembly_plan or {}).get("variants") or []
    pending_count = sum(
        1 for v in variants_list if v.get("render_status") in ("pending", "rendering")
    )
    baselines = get_baselines("generative")
    if baselines and pending_count > 0:
        baselines = scale_render_variants(baselines, pending_count)

    variants = _variants_for_response(job)

    response = GenerativeJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        variants=variants,
        error_detail=job.error_detail,
        created_at=job.created_at,
        updated_at=job.updated_at,
        edit_format=(job.all_candidates or {}).get("edit_format"),
        current_phase=job.current_phase,
        phase_log=list(job.phase_log or []) if job.phase_log is not None else None,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expected_phase_durations=baselines,
    )
    if getattr(job, "_media_overlay_preview_backfilled", False):
        await db.commit()
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
    await db.commit()
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
    dispatch_retext(job, variant_id, text=req.text, remove=req.remove)
    await db.commit()
    log.info("generative_retext", job_id=str(job.id), variant_id=variant_id, remove=req.remove)
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
    dispatch_change_style(job, variant_id, style_set_id=req.style_set_id)
    await db.commit()
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
    dispatch_set_intro_size(job, variant_id, text_size_px=req.text_size_px)
    await db.commit()
    log.info(
        "generative_set_intro_size",
        job_id=str(job.id),
        variant_id=variant_id,
        px=req.text_size_px,
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
    dispatch_set_intro_timing(job, variant_id, start_s=req.start_s, end_s=req.end_s)
    await db.commit()
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
    dispatch_edit_variant(
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
    )
    await db.commit()
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
    job = await _load_generative_job(job_id, db, current_user, allowed_modes=_READABLE_MODES)
    return TimelineResponse(**dispatch_get_timeline(job, variant_id))


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
    dispatch_set_mix(job, variant_id, mix=req.mix)
    log.info("generative_set_mix", job_id=str(job.id), variant_id=variant_id, mix=req.mix)
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")
