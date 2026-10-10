"""Live plan & review wire contract v2 (KRI-439). Pure models and constants: no I/O, no imports from
app.* (so the planner, routes, tasks and tests can all import it without cycles).
Doc: docs/pipelines/live-plan-blocks.md. Swift mirror: Kria/Core/PlanReviewModels.swift."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = (
    2  # advertised as `live_plan_review_version` on GET /creation-threads/capabilities
)

SectionId = Literal[
    "title", "clips", "captions", "music", "sfx", "overlays", "look", "post_caption"
]
SECTION_ORDER: tuple[str, ...] = (
    "title",
    "clips",
    "captions",
    "music",
    "sfx",
    "overlays",
    "look",
    "post_caption",
)
SCOPABLE_SECTIONS: tuple[str, ...] = SECTION_ORDER[:-1]  # post_caption is display-only in v2
BlockState = Literal["waiting", "deciding", "decided"]
TransitionKind = Literal["cut", "dissolve", "whip", "fade"]

# Editor / recipe wire value -> display vocabulary. Anything not listed maps to None (never guess).
TRANSITION_DISPLAY: dict[str, str] = {
    "cut": "cut",
    "none": "cut",
    "hard-cut": "cut",
    "crossfade": "dissolve",
    "dissolve": "dissolve",
    "dip_to_black": "fade",
    "fade": "fade",
    "curtain-close": "fade",
    "flash": "whip",
    "whip-pan": "whip",
    "whip_pan": "whip",
    "whip": "whip",
}

SUMMARY_MAX, DETAIL_MAX, PAYLOAD_MAX_BYTES = 120, 400, 16 * 1024
MAX_CLIPS = MAX_CAPTION_LINES = 40
MAX_SFX = MAX_OVERLAYS = 30


class _Wire(BaseModel):
    # Readers ignore unknown fields (forward compat); writers dump with exclude_none=True.
    model_config = ConfigDict(extra="ignore")


class TitlePayload(_Wire):
    text: Annotated[str, Field(max_length=300)]
    highlight_word: str | None = None
    bar_id: str | None = None  # manual-edit target (ManualEdit.target_id)


class ClipItem(_Wire):
    index: int = Field(ge=0)  # 0-based position in the OUTPUT order
    media_id: str | None = None  # project media id; iOS resolves its local thumbnail from it
    kind: Literal["video", "image"] = "video"
    role: Annotated[str | None, Field(max_length=40)] = None  # short label, e.g. "Hook"
    label: Annotated[str | None, Field(max_length=60)] = None  # on-screen per-clip label, if any
    start_s: float = Field(ge=0)  # OUTPUT timeline
    end_s: float = Field(ge=0)
    source_start_s: float | None = Field(default=None, ge=0)
    source_end_s: float | None = Field(default=None, ge=0)
    # Transition LEAVING this clip into the next one. None for the last clip or an unmappable value.
    transition: TransitionKind | None = None
    transition_duration_s: float | None = Field(default=None, ge=0, le=1)
    thumbnail_url: str | None = None  # GET /plan ONLY (fresh signed URL); always null inside events


class ClipsPayload(_Wire):
    total_duration_s: float = Field(ge=0)
    clips: list[ClipItem] = Field(max_length=MAX_CLIPS)


class CaptionLine(_Wire):
    id: str  # caption cue id (kind="cue") or text-bar id (kind="bar"); the manual-edit target
    kind: Literal["cue", "bar"] = "cue"
    text: Annotated[str, Field(max_length=200)]
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)


class CaptionsPayload(_Wire):
    count: int = Field(ge=0)  # real total, may exceed len(lines)
    lines: list[CaptionLine] = Field(max_length=MAX_CAPTION_LINES)
    truncated: bool = False


class MixLevels(_Wire):
    music_level: float | None = Field(default=None, ge=0, le=1)  # editor `mix.music_level`
    original_level: float | None = Field(default=None, ge=0, le=1)  # the footage's own sound
    music_gain_db: float | None = Field(default=None, ge=-40, le=0)  # smart background bed


class MusicPayload(_Wire):
    source: Literal["catalog", "user_song", "voiceover"]
    mode: Literal["background", "lipsync"] | None = None  # user_song only
    track_id: str | None = None
    title: Annotated[str | None, Field(max_length=120)] = None
    artist: Annotated[str | None, Field(max_length=120)] = None
    bpm: float | None = Field(default=None, gt=0, le=300)  # round(60 / median beat gap), else null
    start_s: float | None = Field(default=None, ge=0)  # offset into the track where it starts
    art_url: str | None = None  # GET /plan ONLY; null inside events
    mix: MixLevels | None = None  # the sound-mix sliders; manual edit = ManualEdit(set_mix)


class SfxItem(_Wire):
    id: str
    label: Annotated[str | None, Field(max_length=60)] = None
    at_s: float = Field(ge=0)
    gain: float | None = Field(default=None, ge=0, le=2)


class SfxPayload(_Wire):
    count: int = Field(ge=0)
    items: list[SfxItem] = Field(max_length=MAX_SFX)


class OverlayItem(_Wire):
    id: str
    kind: Literal["image", "video", "text_card", "motion", "visual"] = "image"
    label: Annotated[str | None, Field(max_length=60)] = None
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    display_mode: Literal["pip", "fullscreen"] | None = None


class OverlaysPayload(_Wire):
    count: int = Field(ge=0)
    items: list[OverlayItem] = Field(max_length=MAX_OVERLAYS)


class LookPayload(_Wire):
    chips: list[Annotated[str, Field(max_length=24)]] = Field(
        max_length=6
    )  # "Warm film", "Serif titles"
    style_id: str | None = None
    look_preset: str | None = None


class PostCaptionPayload(_Wire):
    text: Annotated[str, Field(max_length=2200)]
    hashtags: list[Annotated[str, Field(max_length=60)]] = Field(max_length=15)  # no leading "#"
    platform: Literal["tiktok"] = "tiktok"


PAYLOAD_MODELS: dict[str, type[_Wire]] = {
    "title": TitlePayload,
    "clips": ClipsPayload,
    "captions": CaptionsPayload,
    "music": MusicPayload,
    "sfx": SfxPayload,
    "overlays": OverlaysPayload,
    "look": LookPayload,
    "post_caption": PostCaptionPayload,
}


class PreviousValue(_Wire):
    revision: int = Field(ge=0)
    job_id: str  # the job whose value Undo restores
    summary: str | None = None
    payload: dict[str, Any] | None = None
    skipped: bool = False


class PlanBlockOut(_Wire):
    """One section. Identical shape inside `plan_block` events and GET /plan."""

    section_id: SectionId
    state: BlockState
    summary: str | None = None
    detail: str | None = None
    intent: bool = False
    skipped: bool = False
    decided_at: str | None = None
    # --- v2, all optional for old readers ---
    revision: int = Field(default=0, ge=0)
    changed: bool = False  # decided value differs from the previous job's (revision bumped)
    payload: dict[str, Any] | None = (
        None  # shape = PAYLOAD_MODELS[section_id]; null if waiting/deciding/skipped
    )
    previous: PreviousValue | None = None  # only when changed=True
    editable: bool = False  # GET /plan ONLY: section can be flagged ("Change") for this thread


class DraftHead(_Wire):
    draft_id: str
    draft_revision: int
    etag: str
    can_undo: bool


class UpdateSummary(_Wire):
    turn_id: str
    job_id: str
    text: Annotated[str, Field(max_length=400)]
    changed_sections: list[SectionId]


class PlanSnapshotOut(_Wire):
    thread_id: str
    thread_revision: int
    job_id: str | None = None
    turn_id: str | None = None
    previous_job_id: str | None = None
    status: Literal["empty", "planning", "ready", "updating", "cancelled"]
    scope: list[SectionId] | None = None  # sections being re-rendered right now (status="updating")
    decided_count: int
    total_count: int
    blocks: list[PlanBlockOut]  # always every section in SECTION_ORDER once any event exists
    draft: DraftHead | None = None  # drives "Undo all" (POST /draft/undo)
    update_summary: UpdateSummary | None = None
    next_after_sequence: int  # resume /delta from here


class ManualEdit(BaseModel):
    """A deterministic edit: no model call. v1 = title text, caption line text, sound mix."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["rewrite_text", "set_mix"]
    target_id: Annotated[str | None, Field(max_length=100)] = (
        None  # TitlePayload.bar_id / CaptionLine.id
    )
    text: Annotated[str | None, Field(max_length=300)] = None
    music_level: float | None = Field(default=None, ge=0, le=1)
    original_level: float | None = Field(default=None, ge=0, le=1)
    music_gain_db: float | None = Field(default=None, ge=-40, le=0)


class PlanSectionUndoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_thread_revision: int = Field(ge=0)
    expected_block_revision: int = Field(ge=1)
    expected_draft_revision: int = Field(ge=0)


class PlanSectionUndoOut(BaseModel):
    section_id: SectionId
    thread_revision: int
    draft_revision: int
    turn_id: str | None = None  # the successor render turn (watch it through /delta)
