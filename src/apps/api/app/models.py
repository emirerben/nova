"""SQLAlchemy ORM models matching the plan's data model exactly."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym
from sqlalchemy.sql import func

TIMESTAMPTZ = TIMESTAMP(timezone=True)

CREATOR_AGENT_ACTIVE_STATUSES = (
    "briefing",
    "planning",
    "awaiting_confirmation",
    "executing",
    "rendering",
    "reviewing",
    "awaiting_feedback",
    "revising",
)
CREATOR_AGENT_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
CREATOR_AGENT_STATUSES = CREATOR_AGENT_ACTIVE_STATUSES + CREATOR_AGENT_TERMINAL_STATUSES


class Base(DeclarativeBase):
    pass


class WaitlistSignup(Base):
    __tablename__ = "waitlist_signups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    invited_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # UTM attribution — nullable, NULL when absent from signup URL
    utm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    auth_provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="google")
    # pending | persona_ready | plan_ready | complete
    onboarding_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    jobs: Mapped[list["Job"]] = relationship(back_populates="user")
    oauth_tokens: Mapped[list["OAuthToken"]] = relationship(back_populates="user")
    tiktok_publications: Mapped[list["TikTokPublication"]] = relationship(back_populates="user")
    # 1:1 — the user's onboarding persona (NULL until onboarding starts).
    persona: Mapped["Persona | None"] = relationship(back_populates="user", uselist=False)
    # Explicit operator-owned eligibility markers for the feedback learning loop.
    internal_account_grants: Mapped[list["InternalAccountGrant"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )
    training_consent_events: Mapped[list["TrainingConsentEvent"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )
    edit_artifacts: Mapped[list["EditArtifact"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )
    edit_interaction_receipts: Mapped[list["EditInteractionReceipt"]] = relationship(
        back_populates="creator",
        cascade="all, delete-orphan",
        foreign_keys="EditInteractionReceipt.creator_id",
    )
    edit_feedback_annotations: Mapped[list["EditFeedbackAnnotation"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )
    training_artifact_retention_events: Mapped[list["TrainingArtifactRetentionEvent"]] = (
        relationship(back_populates="creator", cascade="all, delete-orphan")
    )
    training_dataset_exports: Mapped[list["TrainingDatasetExport"]] = relationship(
        back_populates="requested_by_user", foreign_keys="TrainingDatasetExport.requested_by"
    )
    creator_agent_sessions: Mapped[list["CreatorAgentSession"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)  # instagram|youtube|tiktok
    access_token: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)  # Fernet
    refresh_token: Mapped[bytes | None] = mapped_column(BYTEA)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    platform_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    account_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    sync_lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="oauth_tokens")

    __table_args__ = (
        UniqueConstraint("user_id", "platform"),
        Index("idx_oauth_tokens_user_platform", "user_id", "platform"),
        Index(
            "idx_oauth_tokens_expires_at",
            "expires_at",
            postgresql_where="status = 'active'",
        ),
        Index(
            "uq_oauth_tokens_platform_account",
            "platform",
            "platform_account_id",
            unique=True,
            postgresql_where="platform_account_id IS NOT NULL AND status = 'active'",
        ),
    )


class VideoTemplate(Base):
    """Admin-registered curated TikTok templates used for template-mode jobs."""

    __tablename__ = "video_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipe_cached: Mapped[dict | None] = mapped_column(JSONB)
    recipe_cached_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    # {agent_name: prompt_version} captured when recipe_cached was written.
    # The admin staleness check compares this against live AgentSpec.prompt_version
    # values. NULL = unknown (pre-migration row) → treated as stale so existing
    # templates surface for reanalysis on first deploy.
    recipe_cached_versions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # "analyzing" → Gemini analysis in progress; "ready" → recipe_cached populated
    analysis_status: Mapped[str] = mapped_column(Text, nullable=False, default="analyzing")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    voiceover_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_clips_min: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    required_clips_max: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # User inputs the upload UI collects per-template (e.g. location).
    # Shape: list[{key, label, placeholder, max_length, required}].
    required_inputs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Admin lifecycle columns (nullable for backward compat)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Music variant columns
    template_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="standard")
    parent_template_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("video_templates.id"), nullable=True
    )
    music_track_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("music_tracks.id"), nullable=True
    )
    # True = recipe is generated end-to-end by agents (no manual editor edits).
    # False = manually built/edited template (the historical path).
    # Immutable after row creation; the two paths read/write recipe_cached
    # differently and flipping mid-life would orphan a hand-tuned recipe.
    is_agentic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Per-template Layer-2 text-overlay default. Resolution priority when
    # reanalyze-agentic fires:
    #   1. ?use_layer2 query param (present → wins absolutely, true OR false)
    #   2. this column, if not NULL → wins
    #   3. settings.text_overlay_v2_enabled (global flag) → fallback
    # NULL = fall through to the global flag (default for all existing rows).
    use_layer2_default: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Per-template gate for the single-pass encode rollout. Default false
    # means every existing row stays on the multi-pass path until a
    # parity + benchmark run promotes it. Combined with the env-level
    # ``settings.single_pass_encode_enabled`` via AND — flipping either
    # alone has zero render impact (see _run_template_job's effective
    # render-path resolution).
    single_pass_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Per-template lyrics override. NULL = dynamically inherit from the linked
    # MusicTrack.track_config.lyrics_config; non-NULL (including the empty
    # dict) = this template's own setting wins. Resolution happens in
    # template_orchestrate via `is not None` (NOT `or`), so `{}` is a valid
    # "lyrics explicitly off" state. See tests/tasks/test_template_orchestrate
    # for the full fallback matrix.
    lyrics_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    recipe_versions: Mapped[list["TemplateRecipeVersion"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )
    children: Mapped[list["VideoTemplate"]] = relationship(
        back_populates="parent",
        foreign_keys="VideoTemplate.parent_template_id",
    )
    parent: Mapped["VideoTemplate | None"] = relationship(
        back_populates="children",
        remote_side="VideoTemplate.id",
        foreign_keys="VideoTemplate.parent_template_id",
    )
    music_track: Mapped["MusicTrack | None"] = relationship(
        foreign_keys="VideoTemplate.music_track_id",
    )

    __table_args__ = (
        Index("idx_templates_created_at", "created_at"),
        Index("idx_templates_type_created", "template_type", "created_at"),
    )


class TemplateRecipeVersion(Base):
    """Tracks recipe versions across analyze/reanalyze cycles for comparison."""

    __tablename__ = "template_recipe_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[str] = mapped_column(
        Text, ForeignKey("video_templates.id", ondelete="CASCADE"), nullable=False
    )
    recipe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # initial_analysis | reanalysis | manual_edit | remerge | admin_font_override
    # Constrained by ck_recipe_version_trigger — keep in sync with migrations
    # 0010 (added remerge) and 0025 (added admin_font_override).
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    # Build wall-clock start, captured at WORKER pickup (not at button-click
    # time — Celery queue-wait is excluded). Paired with `created_at` (end),
    # gives per-run compute latency without relying on Langfuse trace
    # aggregation. NULL for rows written before migration 0023 (or by an
    # orchestrator that crashed before setting it).
    build_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    template: Mapped["VideoTemplate"] = relationship(back_populates="recipe_versions")

    __table_args__ = (
        CheckConstraint(
            "trigger IN ('initial_analysis', 'reanalysis', 'manual_edit', 'remerge')",
            name="ck_recipe_version_trigger",
        ),
        Index("idx_recipe_versions_template_created", "template_id", "created_at"),
    )


class MusicTrack(Base):
    """Admin-registered music tracks used for beat-sync jobs."""

    __tablename__ = "music_tracks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    artist: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    audio_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    beat_timestamps_s: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # "queued" | "analyzing" | "ready" | "failed"
    analysis_status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # per-song admin fine-tuning: best_start_s, best_end_s, slot_every_n_beats,
    # required_clips_min, required_clips_max
    track_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Gemini audio analysis → cached recipe for audio-only template creation
    recipe_cached: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recipe_cached_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Lyrics extraction (LRCLIB canonical text + Whisper word timings, aligned).
    # See app.agents.lyrics for the producer and app.pipeline.lyric_injector for
    # how this gets baked into music-job text overlays.
    #
    # State machine:
    #   "pending"             — not yet attempted
    #   "extracting"          — Celery task running
    #   "ready"               — publishable; lyrics_source MUST be in
    #                           app.agents.lyrics.PUBLISHABLE_LYRICS_SOURCES
    #   "needs_manual_lyrics" — LRCLIB lookup failed (or matched a wrong
    #                           recording at low confidence). Whisper draft
    #                           stored on `lyrics_whisper_draft` for admin
    #                           reference. Admin must paste a LRCLIB ID/URL
    #                           via the force-id endpoint to recover.
    #   "unavailable"         — LRCLIB confirms instrumental (no lyrics exist)
    #   "failed"              — Whisper crashed or pipeline error
    lyrics_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # Publishable extraction blob (LyricsOutput shape). Production consumers
    # only ever read this — non-publishable Whisper-only transcriptions live
    # on `lyrics_whisper_draft` instead.
    lyrics_cached: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    lyrics_error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    lyrics_extracted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # "lrclib_synced+whisper" | "lrclib_plain+whisper" | "whisper_only"
    # (legacy: "genius+whisper" | "manual"). Only the lrclib_* sources are
    # production-publishable; see app.agents.lyrics.PUBLISHABLE_LYRICS_SOURCES.
    lyrics_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured trace of the latest LRCLIB lookup. Surfaced in admin UI.
    # Shape: {"query": {...}, "get_status": "404"|"hit"|"error", "search_status":
    # "no_strong_match"|"hit"|"skipped", "search_top_score": float?,
    # "lrclib_id_matched": int?, "fallback_path": str, "duration_delta_s":
    # float?, "attempted_at": iso8601, "attempt_count": int}. Null until the
    # agent's new flow lands.
    lyrics_diagnostic: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Whisper-only draft kept for admin reference when production extraction
    # fails (lyrics_status='needs_manual_lyrics'). Same LyricsOutput shape as
    # lyrics_cached. Never read by production consumers.
    lyrics_whisper_draft: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Monotonic counter bumped on every re-extract / force-id action. The
    # extraction task takes an expected_version param and updates conditionally
    # on it — older tasks completing after newer ones get their mutation
    # discarded. Prevents stale-task races when an admin rapidly re-pastes IDs.
    lyrics_extraction_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # song_classifier creative labels (vibe, genre, mood, copy_tone, ...).
    # See app/agents/_schemas/music_labels.py — MusicLabels Pydantic shape.
    # Nullable until backfill runs; the matcher filters out NULL-labeled tracks.
    ai_labels: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Mirrors MusicLabels.label_version so the matcher can refuse stale rows
    # without parsing the JSONB.
    label_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # song_sections agent output: ordered list of 1-3 ranked SongSection blobs
    # (rank 1 = best). See app/agents/_schemas/song_sections.py for the
    # Pydantic shape. NULL until the song_sections agent succeeds; the matcher
    # filters NULL-sectioned tracks out of auto-mode.
    best_sections: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Mirrors CURRENT_SECTION_VERSION so the matcher can refuse stale rows
    # without parsing the JSONB.
    section_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Last reason _run_song_sections returned None for this track (silent
    # fail-open branch). NULL means "no failure since the last successful
    # analyze." Populated truncated to MAX_ERROR_DETAIL_LEN; cleared at the
    # start of every analyze_music_track_task run so a successful re-analyze
    # cannot leave stale text on the row.
    section_error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        Index("idx_music_tracks_status", "analysis_status"),
        Index("idx_music_tracks_published", "published_at"),
        Index("idx_music_tracks_lyrics_status", "lyrics_status"),
        Index("idx_music_tracks_created_at", "created_at"),
    )


class SoundEffect(Base):
    """Admin-curated sound effects for the glossary (click sounds, meme stings, etc.)."""

    __tablename__ = "sound_effects"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    audio_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "pending" | "ready" | "failed" — no analysis stage (simpler than MusicTrack)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Original filename for admin display (set at upload time).
    source_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Smart sound-design metadata. Presets resolve closed role tags; filenames
    # are only a compatibility bridge for pre-metadata library rows.
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    integrated_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_peak_dbtp: Mapped[float | None] = mapped_column(Float, nullable=True)
    attack_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    decay_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy: Mapped[float | None] = mapped_column(Float, nullable=True)
    brightness: Mapped[float | None] = mapped_column(Float, nullable=True)
    contains_voice: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vocal_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    license: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_audit_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        Index("idx_sound_effects_status", "status"),
        Index("idx_sound_effects_published", "published_at"),
        Index("idx_sound_effects_created_at", "created_at"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # importing|queued|processing|clips_ready|clips_ready_partial|
    # posting|posting_partial|done|posting_failed|processing_failed|
    # cancelled (admin cancel via /admin/jobs/{id}/cancel)
    # drive import: importing → queued → processing → ...
    # template jobs: queued → processing → template_ready | processing_failed
    # music jobs:   queued → processing → music_ready   | processing_failed
    # auto-music: queued → processing → matching → rendering →
    #             variants_ready | variants_ready_partial |
    #             matching_failed | no_labeled_tracks | variants_failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    # "default" | "template" | "music" | "auto_music"
    job_type: Mapped[str] = mapped_column(Text, nullable=False, default="default")
    # Phase 3 (auto-music): the orchestrator-level mode discriminator.
    # Currently only set to "auto_music" by orchestrate_auto_music_job.
    # NULL for every pre-Phase-3 row — routing still uses job_type. Kept
    # nullable so the column stays one-way / rollback safe.
    mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("video_templates.id"), nullable=True
    )
    music_track_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("music_tracks.id"), nullable=True
    )
    assembly_plan: Mapped[dict | None] = mapped_column(JSONB)  # populated for template jobs
    raw_storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    selected_platforms: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    probe_metadata: Mapped[dict | None] = mapped_column(JSONB)
    transcript: Mapped[dict | None] = mapped_column(JSONB)
    scene_cuts: Mapped[dict | None] = mapped_column(JSONB)
    all_candidates: Mapped[dict | None] = mapped_column(JSONB)  # all 9 for re-roll
    error_detail: Mapped[str | None] = mapped_column(Text)
    # Structured failure taxonomy for processing_failed jobs. Lets the frontend
    # render specific copy ("music asset missing", "video too short") instead
    # of a generic "Something went wrong". See FAILURE_REASON in
    # tasks/template_orchestrate.py for the canonical set.
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Live pipeline phase name (e.g. "download_clips", "analyze_clips",
    # "assemble", "upload"). Cleared on success/failure terminal state but
    # phase_log retains history. Drives the live progress UI on /template-jobs/[id].
    current_phase: Mapped[str | None] = mapped_column(Text)
    # Append-only history of completed phases:
    # [{name, elapsed_ms, t_offset_ms, ts}, ...]. Written by services/job_phases.
    phase_log: Mapped[list | None] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Append-only log of non-LLM pipeline decisions written by services/pipeline_trace.
    # Each entry: {ts, stage, event, data}. Drives the admin job-debug view's
    # pipeline-trace tab. NULL on legacy/pre-feature jobs — the UI handles that.
    pipeline_trace: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Celery task_id of the orchestrator task dispatched for this job. By
    # convention str(job.id) — set by app.services.job_dispatch.enqueue_orchestrator
    # on every orchestrator dispatch site. NULL on legacy rows (pre-0027)
    # and on rows whose orchestrator was never dispatched. Used by the
    # admin debug UI to call celery_app.control.{inspect,revoke}.
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Reverse link to the content-plan item that minted this job (mode="content_plan").
    # Nullable: every non-plan job leaves it NULL. Used by the admin job-debug view
    # for reverse lookup. The forward link lives on PlanItem.current_job_id; these two
    # FKs are the circular pair resolved across migrations 0038/0039.
    content_plan_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_items.id"), nullable=True
    )
    # Durable owner-generation captured when a content-plan Job is minted.
    # NULL is legacy epoch 0. Public/non-plan Jobs always leave this NULL.
    content_plan_ownership_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # True pipeline-wall-time anchors. Distinct from created_at (queue insert)
    # and updated_at (any column write).
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Liveness beacon ticked by the orchestrator's heartbeat thread every
    # ~30s while a render runs (services/job_phases.job_heartbeat). The status
    # route reports `retrying: true` when a non-terminal job's beacon goes
    # stale — a silently OOM-killed worker otherwise looks identical to
    # healthy progress for the full acks_late redelivery window (30+ min,
    # 2026-07-21 incident, job e8173a25). Read-free UPDATEs by design: the
    # heartbeat must never read-modify-write assembly_plan. (Each beat also
    # refreshes updated_at via this model's onupdate — deliberate; see
    # beat_heartbeat's docstring.)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="jobs")
    tiktok_publications: Mapped[list["TikTokPublication"]] = relationship(back_populates="job")
    clips: Mapped[list["JobClip"]] = relationship(back_populates="job")
    # The plan item this job was minted for (NULL for non-plan jobs). One-directional;
    # PlanItem.current_job is the matching forward link (not a back_populates inverse —
    # the two FKs are distinct columns, see PlanItem.current_job_id).
    content_plan_item: Mapped["PlanItem | None"] = relationship(foreign_keys=[content_plan_item_id])
    creator_agent_sessions: Mapped[list["CreatorAgentSession"]] = relationship(
        back_populates="target_job", foreign_keys="CreatorAgentSession.target_job_id"
    )

    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_template_id", "template_id"),
        Index("idx_jobs_music_track_id", "music_track_id"),
        Index("idx_jobs_failure_reason", "failure_reason"),
        Index("idx_jobs_created_at", "created_at"),
        Index("idx_jobs_content_plan_item_id", "content_plan_item_id"),
    )


class TikTokPublication(Base):
    """One user-consented TikTok delivery attempt.

    ``delivery_mode`` separates a Direct Post from an upload-to-drafts handoff.
    Processing completion and public visibility remain deliberately separate:
    TikTok can finish ingest while moderation is pending, and visibility may
    later be revoked. Draft uploads complete at ``visibility_status='draft'``
    because the creator must finish editing and posting inside TikTok.
    """

    __tablename__ = "tiktok_publications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False
    )
    variant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="direct_post")

    source_object_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_generation: Mapped[str] = mapped_column(Text, nullable=False)
    source_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_object_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_signature: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    edit_signature_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1")

    title: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_level: Mapped[str] = mapped_column(Text, nullable=False)
    allow_comment: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    allow_duet: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    allow_stitch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    brand_content_toggle: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    brand_organic_toggle: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    is_aigc: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    music_usage_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    consent_version: Mapped[str] = mapped_column(Text, nullable=False)
    consented_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    creator_info_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    processing_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    visibility_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    tiktok_publish_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tiktok_post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    public_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    next_poll_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metrics_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    evaluation_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evaluation_captured_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="tiktok_publications")
    job: Mapped["Job"] = relationship(back_populates="tiktok_publications")

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_tiktok_pub_user_idempotency"),
        UniqueConstraint("tiktok_publish_id", name="uq_tiktok_pub_publish_id"),
        Index("idx_tiktok_pub_user_created", "user_id", "created_at"),
        Index("idx_tiktok_pub_user_job", "user_id", "job_id"),
        Index("idx_tiktok_pub_due_poll", "processing_status", "next_poll_at"),
        Index("idx_tiktok_pub_post_id", "tiktok_post_id"),
        CheckConstraint(
            "processing_status IN ('queued','snapshotting','submitting','processing',"
            "'complete','submission_unknown','failed')",
            name="ck_tiktok_pub_processing_status",
        ),
        CheckConstraint(
            "visibility_status IN ('unknown','draft','private','public','removed')",
            name="ck_tiktok_pub_visibility_status",
        ),
        CheckConstraint(
            "delivery_mode IN ('direct_post','draft_upload')",
            name="ck_tiktok_pub_delivery_mode",
        ),
    )


class CreatorStyleAssignment(Base):
    """Server-owned assignment of a reviewed creator-style preset.

    The browser never selects ``preset_id`` directly.  Smart Captions resolves
    this row from the authenticated user and pins the version into each plan.
    """

    __tablename__ = "creator_style_assignments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    preset_id: Mapped[str] = mapped_column(Text, nullable=False)
    preset_version: Mapped[str] = mapped_column(Text, nullable=False)
    shadow_preset_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    shadow_preset_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    assigned_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="system")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(shadow_preset_id IS NULL) = (shadow_preset_version IS NULL)",
            name="ck_creator_style_shadow_pair",
        ),
    )


class JobClip(Base):
    __tablename__ = "job_clips"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-3 rendered; 4-9 re-roll held
    hook_score: Mapped[float] = mapped_column(Float, nullable=False)
    engagement_score: Mapped[float] = mapped_column(Float, nullable=False)
    combined_score: Mapped[float] = mapped_column(Float, nullable=False)
    start_s: Mapped[float] = mapped_column(Float, nullable=False)
    end_s: Mapped[float] = mapped_column(Float, nullable=False)
    hook_text: Mapped[str | None] = mapped_column(Text)
    platform_copy: Mapped[dict | None] = mapped_column(JSONB)
    copy_status: Mapped[str] = mapped_column(Text, nullable=False, default="generated")
    # generated | generated_fallback | edited
    video_path: Mapped[str | None] = mapped_column(Text)  # GCS path
    thumbnail_path: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    render_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # pending | rendering | ready | failed
    post_status: Mapped[dict | None] = mapped_column(JSONB)
    # { instagram: 'posted'|'failed'|'pending', youtube: ..., tiktok: ... }
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    error_detail: Mapped[str | None] = mapped_column(Text)
    # Phase 3 (auto-music): set on rows produced by orchestrate_auto_music_job.
    # NULL for template-mode + manual music-mode rows. The FK lets us answer
    # "which jobs used this track" for the admin music page.
    music_track_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("music_tracks.id"), nullable=True
    )
    # Matcher's 0-10 score for this track on this clip-set. Surfaced on the
    # variant tile so the user knows how confident the pick was.
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Matcher's editor's-voice rationale (1-2 sentences). Rendered as
    # "we picked X because..." copy on the variant tile.
    match_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    job: Mapped["Job"] = relationship(back_populates="clips")

    __table_args__ = (
        Index("idx_job_clips_job_id", "job_id"),
        Index("idx_job_clips_rank", "job_id", "rank"),
        Index("idx_job_clips_music_track_id", "music_track_id"),
    )


class CreatorAgentSession(Base):
    """Durable state for one Main Creator Agent edit session.

    A session is scoped to one creator and plan item.  ``phase`` is the
    controller's source of truth; terminal phases release the partial unique
    index so a later session can be started for the same item.  Events and
    executions are deliberately separate append-only/auditable records so
    retries cannot be mistaken for state transitions.
    """

    __tablename__ = "creator_agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_items.id", ondelete="CASCADE"), nullable=False
    )
    # briefing → planning → awaiting_confirmation → executing → rendering →
    # reviewing → awaiting_feedback → revising → completed|failed|cancelled
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="briefing")
    # ``phase`` is kept as an ORM alias for controller code that uses the
    # state-machine terminology from the original design.
    phase: Mapped[str] = synonym("status")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ownership_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    active_plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    target_variant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_generation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_plan: Mapped[dict | None] = synonym("active_plan")
    current_job_id: Mapped[uuid.UUID | None] = synonym("target_job_id")
    max_render_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    render_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    iteration_budget: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    question_budget: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    agent_call_budget: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    agent_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_review: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_good: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    creator: Mapped["User"] = relationship(back_populates="creator_agent_sessions")
    plan_item: Mapped["PlanItem"] = relationship(back_populates="creator_agent_sessions")
    target_job: Mapped["Job | None"] = relationship(
        back_populates="creator_agent_sessions", foreign_keys=[target_job_id]
    )
    events: Mapped[list["CreatorAgentEvent"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="CreatorAgentEvent.sequence",
    )
    executions: Mapped[list["CreatorAgentExecution"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list["AgentRun"]] = relationship(back_populates="creator_agent_session")

    __table_args__ = (
        CheckConstraint(
            "status IN ('briefing','planning','awaiting_confirmation','executing','rendering',"
            "'reviewing','awaiting_feedback','revising','completed','failed','cancelled')",
            name="ck_creator_agent_sessions_status",
        ),
        CheckConstraint(
            "revision >= 0 AND ownership_epoch >= 0 AND iteration_budget >= 0 "
            "AND question_budget >= 0 AND agent_call_budget >= 0 "
            "AND iteration_count >= 0 AND question_count >= 0 AND agent_call_count >= 0 "
            "AND max_render_attempts >= 0 AND render_attempts >= 0",
            name="ck_creator_agent_sessions_counters_nonnegative",
        ),
        Index(
            "uq_creator_agent_sessions_active_item",
            "creator_id",
            "plan_item_id",
            unique=True,
            postgresql_where=text(
                "status IN ('briefing','planning','awaiting_confirmation','executing',"
                "'rendering','reviewing','awaiting_feedback','revising')"
            ),
        ),
        Index("idx_creator_agent_sessions_item_updated", "plan_item_id", "updated_at"),
        Index("idx_creator_agent_sessions_creator_id", "creator_id"),
        Index("idx_creator_agent_sessions_target_job", "target_job_id"),
    )


class CreatorAgentEvent(Base):
    """Append-only controller event; retries are idempotent by client ID."""

    __tablename__ = "creator_agent_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    client_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="system")
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    session: Mapped["CreatorAgentSession"] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint("sequence >= 0 AND revision >= 0", name="ck_creator_agent_events_counters"),
        CheckConstraint(
            "role IN ('user','assistant','system')", name="ck_creator_agent_events_role"
        ),
        UniqueConstraint("session_id", "sequence", name="uq_creator_agent_events_sequence"),
        UniqueConstraint("session_id", "client_event_id", name="uq_creator_agent_events_client_id"),
        Index("idx_creator_agent_events_session_created", "session_id", "created_at"),
    )


class CreatorAgentExecution(Base):
    """Idempotent execution receipt for a controller action."""

    __tablename__ = "creator_agent_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_manifest_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    session: Mapped["CreatorAgentSession"] = relationship(back_populates="executions")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','stale','duplicate')",
            name="ck_creator_agent_executions_status",
        ),
        CheckConstraint("expected_revision >= 0", name="ck_creator_agent_executions_revision"),
        UniqueConstraint(
            "session_id", "idempotency_key", name="uq_creator_agent_executions_idempotency"
        ),
        Index("idx_creator_agent_executions_session_created", "session_id", "created_at"),
    )


class AgentRun(Base):
    """One row per agent invocation. Captures full input + raw LLM response +
    parsed output so the admin job-debug view can show exactly what each
    agent saw and produced for a given job. job_id is nullable so off-job
    calls (track-level analysis, eval harness, or a pre-render creator-agent
    session) can also be persisted without inventing a fake job UUID.
    """

    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=True,
    )
    # video_templates.id and music_tracks.id are Text (not UUID) so the FK
    # columns must also be Text. ondelete=CASCADE mirrors job_id and avoids
    # a check-constraint violation on parent-delete (see migration 0024).
    template_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("video_templates.id", ondelete="CASCADE"),
        nullable=True,
    )
    music_track_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("music_tracks.id", ondelete="CASCADE"),
        nullable=True,
    )
    creator_agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
        nullable=True,
    )
    segment_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    creator_agent_session: Mapped["CreatorAgentSession | None"] = relationship(
        back_populates="agent_runs", foreign_keys=[creator_agent_session_id]
    )

    __table_args__ = (
        CheckConstraint(
            "(job_id IS NOT NULL) OR (template_id IS NOT NULL) "
            "OR (music_track_id IS NOT NULL) OR (creator_agent_session_id IS NOT NULL)",
            name="ck_agent_run_has_owner",
        ),
        Index("idx_agent_run_job_id_created", "job_id", "created_at"),
        Index("idx_agent_run_agent_name", "agent_name"),
        Index("idx_agent_run_template_id_created", "template_id", "created_at"),
        Index("idx_agent_run_music_track_id_created", "music_track_id", "created_at"),
        Index(
            "idx_agent_run_creator_agent_session_created",
            "creator_agent_session_id",
            "created_at",
        ),
        Index(
            "idx_agent_run_template_id_created_desc",
            "template_id",
            text("created_at DESC"),
            postgresql_where=text("template_id IS NOT NULL"),
        ),
        Index(
            "idx_agent_run_music_track_id_created_desc",
            "music_track_id",
            text("created_at DESC"),
            postgresql_where=text("music_track_id IS NOT NULL"),
        ),
    )


class Persona(Base):
    """1:1 with a user. The onboarding questionnaire plus the editable
    AI-generated persona that threads into content-plan generation and
    intro_writer. See the content-plan plan, Data model section."""

    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # enforces 1:1 with users
    )
    # Raw onboarding answers (work/school/social/location/hobbies/travels/passions,
    # optional tiktok_handle). UNTRUSTED free text — sanitized before any agent call.
    questionnaire: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Editable AI output: {summary, content_pillars[], tone, audience,
    # posting_cadence, posts_per_week, sample_topics[], rationale, goal,
    # content_mode, current_situation}.
    persona: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Scraped public TikTok profile from the onboarding pre-screen.
    # {handle, follower_count, video_count, top_captions[], top_hashtags[], analyzed_at}
    # NULL when user skipped the TikTok step or scrape failed.
    tiktok_profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Per-user derived text style (Creator Agent M1). Pins a curated style_set_id
    # + parity-safe knob overrides applied to every generative render. NULL = no
    # style derived yet → byte-identical render behavior. status="edited" means the
    # user hand-edited; derivation never auto-overwrites it without explicit /rederive.
    style: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Bring-Your-Own-Ideas (M1): user-owned intent seeds that persist across plans.
    # Each seed: {id: str, text: str, pillar: str|null, status: "pending"|"in_plan"}.
    # The id is server-stamped (uuid4 hex) so PlanItem.source_idea_seed_id can
    # reference it without a second migration (T5 populates that link). Empty [] =
    # no seeds yet → byte-identical plan generation (no prompt block injected).
    idea_seeds: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # generating | ready | failed | edited
    persona_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="generating")
    generation_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="persona")
    # The tenant compound FK includes ContentPlan.user_id, which is also written
    # by ContentPlan.user.  Keep this traversal read-only so assigning a Persona
    # can never silently rewrite a plan's tenant key.
    content_plans: Mapped[list["ContentPlan"]] = relationship(
        back_populates="persona", viewonly=True
    )

    __table_args__ = (UniqueConstraint("id", "user_id", name="uq_personas_id_user_id"),)


class ContentPlan(Base):
    """A parent entity owning N PlanItems. NOT a column on Job — each generated
    video stays one Job, and a PlanItem carries current_job_id."""

    __tablename__ = "content_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Optional user-supplied events that bias generation (trips, launches, exams).
    events: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # generating | ready | failed | edited
    plan_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="generating")
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    # Day N maps to start_date + (N-1) days; first week = days 1-7. NULL until scheduled.
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Activation seed (T8): the one batch of recent clips the user uploads after the
    # plan is ready, stored under users/{user_id}/plan/{plan_id}/seed/. clip_plan_matcher
    # assigns these to plan items; a matched item references the seed path directly
    # (no GCS copy — see activate_content_plan).
    seed_clip_paths: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # none | seeding | activating | activated | activated_empty | failed.
    # Plan-level poll scalar — per-item render state stays derived from Job.status (T2).
    activation_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    # Feedback loop (Phase 2): a bounded, deterministic rollup of the user's
    # video_feedback (signal counts + recent notes) — see services/feedback_summary.
    # Additive AI CONTEXT, never a mutation of the plan: it threads into
    # content_plan_generator (on user-triggered regenerate) and intro_writer (future
    # videos), but explicit user edits (PlanItem.user_edited) always win over it.
    # NULL until the user leaves feedback + regenerates; the generator treats NULL
    # as "(none)".
    preference_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Monotonic ownership fence. Long-running plan tasks snapshot this value and
    # must observe the same value before committing any plan-derived result.
    ownership_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # An operator-set containment fence for ownership-integrity incidents.
    # While populated, all user and worker paths fail closed for this plan.
    ownership_quarantined_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Footage pool (plan dogfood feedback #4): the post-activation "dump the
    # whole trip" batch. Shape: {"status": "matching"|"matched"|"matched_empty"|
    # "match_failed", "clips": [{"gcs_path": str, "matched_item_id": str|null}],
    # "updated_at": iso}. Clips live under users/{uid}/plan-pool/{plan_id}/
    # (persistent prefix). match_pool_clips assigns them across PENDING items as
    # machine_matched provisional assignments — never auto-renders. NULL = no
    # pool uploaded yet.
    pool: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    generation_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    activation_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    activation_phase: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship()
    # Read-only for the same reason as Persona.content_plans: the compound
    # relationship shares user_id with the independently writable user
    # relationship.  Callers create plans with explicit user_id + persona_id.
    persona: Mapped["Persona"] = relationship(back_populates="content_plans", viewonly=True)
    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="content_plan", order_by="PlanItem.position"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["persona_id", "user_id"],
            ["personas.id", "personas.user_id"],
            name="fk_content_plans_persona_owner",
            ondelete="CASCADE",
            match="FULL",
        ),
        Index("idx_content_plans_user_id", "user_id"),
    )


class PlanItem(Base):
    """One day's content idea inside a ContentPlan. Live generating/ready/failed
    state is derived from current_job.status at read time — item_status only
    distinguishes idea vs awaiting_clips (no duplicate state machine, see plan T2)."""

    __tablename__ = "plan_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_plans.id", ondelete="CASCADE"), nullable=False
    )
    # Calendar slot (1..horizon_days). Nullable in the idea-centric model — bare ideas
    # have no calendar position until explicitly scheduled.
    day_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # AI-generated theme. Nullable — a bare user idea has no theme until AI fills it.
    theme: Mapped[str | None] = mapped_column(Text, nullable=True)
    # User-controlled ordering position. Backfilled from day_index for existing rows.
    # Must be set explicitly when creating new items — no server_default (see migration 0055).
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    idea: Mapped[str] = mapped_column(Text, nullable=False)
    filming_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The AI's short "why this video works", shown read-only in the dashboard.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The edit shape this day is meant to become (montage|talking_head|day_vlog|
    # single_hero). Plain Text + server_default like item_status — validated in
    # the schema layer (app.agents._schemas.edit_format), not a DB CHECK, so the
    # vocabulary can grow without a migration. Legacy rows read 'montage'.
    edit_format: Mapped[str] = mapped_column(Text, nullable=False, server_default="montage")
    # Per-item montage visual preset. "classic" = current sequential montage;
    # "masonry" = collage-wall visual assembly. Render path validates/coerces
    # defensively; no DB CHECK so the preset vocabulary can grow without a migration.
    montage_preset: Mapped[str] = mapped_column(Text, nullable=False, server_default="classic")
    # Per-item preference for landscape source clips: "fit" (letterbox — full-width,
    # black bars top & bottom, never enlarged — the default) | "fill" (center-crop to
    # fill the 9:16 frame). Portrait and square clips are always cropped regardless.
    # Plain Text + server_default so legacy rows immediately letterbox landscape clips
    # without a backfill (same pattern as edit_format). Validated in the route layer;
    # no DB CHECK so the vocabulary can grow without a migration.
    landscape_fit: Mapped[str] = mapped_column(Text, nullable=False, server_default="fit")
    # Per-item override of the persona-level content_mode (montage plan-vs-have toggle,
    # 0058+). NULL = inherit the persona's content_mode (the default for every legacy row
    # and every item the user never toggled). When set, stores one of:
    #   "create_new"        → "Planning to film" — show shot-plan / ShotSlotUploader flow
    #   "existing_footage"  → "I already have footage" — skip plan, go straight to pool upload
    #   "mixed"             → combination of the two
    # Plain Text, nullable, no server_default (NULL means inherit). No DB CHECK —
    # validated in the route layer (same pattern as edit_format). Only affects the
    # upload UI; the render archetype (montage/narrated/…) is driven solely by
    # edit_format + voiceover_gcs_path + filming_guide.
    content_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-video Smart Captions choice.  Availability remains server-computed
    # from feature flags + creator assignment + edit format; a stored True does
    # not bypass a later kill switch.
    smart_captions_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # False preserves Smart captions/visuals/transitions but emits no auto SFX.
    smart_sound_design_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Themed uploads land here (users/{user_id}/plan/{plan_item_id}/...).
    clip_gcs_paths: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Structured shot list generated at plan time: 2–4 shots, each {what, how, duration_s}.
    # Stored as raw JSONB (no separate table) and returned read-only by the API.
    # Legacy rows receive [] via server_default; no backfill needed.
    filming_guide: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Per-shot clip assignments: [{"gcs_path": str, "shot_id": str | null}].
    # shot_id=null means extra-footage pool; shot_id=str links to a filming_guide entry.
    # clip_gcs_paths is ALWAYS derived from this list (shots-first, pool after) via
    # set_item_clips in app/services/plan_clips.py — the single writer.
    clip_assignments: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Reviewable guided-edit plan. One versioned envelope keeps the live draft
    # and last approval together so media changes can mark it stale without
    # erasing the creator's prior decision. NULL until Plan edit is used.
    edit_proposal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # ConformanceFeedbackAgent result at clip-attach time (best-effort, display-only).
    # {verdict, confidence, summary, mismatches[], suggestions[]}. NULL until
    # CONFORMANCE_FEEDBACK_ENABLED=True and the agent runs; never blocks Generate.
    conformance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # idea | awaiting_clips ONLY. Render state is derived from current_job.status.
    item_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="idea")
    # Forward link to the job currently rendering this item (the circular pair's
    # other half is Job.content_plan_item_id; resolved across migrations 0038/0039).
    current_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True
    )
    # Bring-Your-Own-Ideas provenance link (M1 T5 populates this). References the
    # id field of the Persona.idea_seeds entry that seeded this item. NULL means
    # the item was generated from the market idea-bank (no user seed) OR T5 hasn't
    # run yet. Stored as TEXT (the uuid4 hex from the seed's id field) rather than
    # a FK so it survives seed deletion without a cascade constraint.
    source_idea_seed_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # GCS key of a user-recorded or -uploaded voiceover for narrated-walkthrough items.
    # Set via PATCH /plan-items/{id}/voiceover; threaded to build_generative_job at
    # generate time so the narrated archetype can do force-alignment + per-step trimming.
    voiceover_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Server-authoritative soundtrack policy selected during item setup.
    # "kria" lets the primary-variant policy choose, "original" forces the
    # no-track original-audio variant, and "voiceover" activates the separately
    # stored voiceover_gcs_path. The recording remains resumable when inactive.
    audio_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="kria")
    # Original-audio bed level for the narrated archetype (0.0 = voice only,
    # 1.0 = loudest). NULL → Kria's default level. Set via
    # PATCH /plan-items/{id}/voiceover-bed-level; threaded to build_generative_job
    # so the footage audio plays, side-chain ducked, under the narration.
    voiceover_bed_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Caption style for the narrated archetype: "sentence" (default, sentence-block
    # captions) or "word" (one big word at a time, the "qbuilder" look). NULL →
    # "sentence". Set via PATCH /plan-items/{id}/voiceover-caption-style; threaded to
    # build_generative_job so the narrated render burns the chosen caption style.
    voiceover_caption_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional AI-authored voiceover *script* the creator reads aloud while recording
    # (the "Get a transcript" helper — TRANSCRIPT_HELPER_ENABLED). Raw JSONB (no side
    # table), validated by app.schemas.voiceover_script.VoiceoverScript on read/write:
    #   {version, text, read_time_s, brief, footage_summary?, interview_turns, lines[], source}.
    # NULL until the creator generates a transcript. `version` bumps on every Rewrite.
    voiceover_script: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The `voiceover_script.version` the currently-attached voiceover take was recorded
    # against. Lets the Script step warn "your take was for the previous script" when a
    # Rewrite bumps the version after a take was captured (soft, never blocks). NULL until
    # a take is recorded through the transcript flow.
    voiceover_script_recorded_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional date the user wants to post this idea (distinct from plan-level start_date).
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Freeform notes the user adds to flesh out the idea.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Planned scenes: [{id: str, text: str, transition_after?: str}]. Always reassign
    # (never mutate in-place) so SQLAlchemy detects the change.
    scenes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    content_plan: Mapped["ContentPlan"] = relationship(back_populates="items")
    # One-directional (not the inverse of Job.content_plan_item — distinct FK column).
    current_job: Mapped["Job | None"] = relationship(foreign_keys=[current_job_id])
    creator_agent_sessions: Mapped[list["CreatorAgentSession"]] = relationship(
        back_populates="plan_item", cascade="all, delete-orphan"
    )
    edit_artifacts: Mapped[list["EditArtifact"]] = relationship(
        back_populates="plan_item", cascade="all, delete-orphan"
    )
    edit_interaction_receipts: Mapped[list["EditInteractionReceipt"]] = relationship(
        back_populates="plan_item", cascade="all, delete-orphan"
    )
    edit_feedback_annotations: Mapped[list["EditFeedbackAnnotation"]] = relationship(
        back_populates="plan_item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "NOT smart_captions_enabled OR COALESCE(edit_format, '') = 'subtitled'",
            name="ck_plan_items_smart_captions_format",
        ),
        Index("idx_plan_items_content_plan_id_day", "content_plan_id", "day_index"),
        Index("idx_plan_items_content_plan_id_position", "content_plan_id", "position"),
    )


class PlanItemAsset(Base):
    """One visual asset in a plan item's pool (auto-placement PR0, plan 005).

    The pool feeds the overlay auto-placement matcher: creators drop screenshots /
    screen recordings here; each row carries the upload location plus (later, PR1a)
    the persisted analysis output. Preparing rows stage under lifecycle-covered
    `dev-user/`; registration promotes one verified generation into the persistent
    `users/{user_id}/plan/{plan_item_id}/pool/` prefix before analysis can claim it.

    status lifecycle: preparing → promoting → queued → analyzing → ready | failed. The
    maintenance-only `cleanup_pending` claim fences expired reservation deletion.
    (`uploaded` remains a legacy/reconciliation state). `content_fingerprint`
    powers new upload dedupe from immutable storage metadata; `content_hash`
    remains only for legacy provider compatibility.
    """

    __tablename__ = "plan_item_assets"
    __table_args__ = (
        UniqueConstraint(
            "plan_item_id",
            "client_upload_id",
            name="uq_plan_item_assets_item_client_upload",
        ),
        # List query: WHERE plan_item_id ORDER BY created_at.
        Index("idx_plan_item_assets_item_created", "plan_item_id", "created_at"),
        Index(
            "idx_plan_item_assets_heif_unreadable_recovery",
            "id",
            postgresql_where=text(
                "status = 'failed' AND error_code = 'analysis_unreadable' "
                "AND upload_content_type IN ('image/heic', 'image/heif') "
                "AND analysis_attempt_count < 2"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_items.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    gcs_path: Mapped[str] = mapped_column(Text, nullable=False)
    # "image" | "video" — derived from the upload content type in the route layer.
    # Plain Text (no DB CHECK) so the vocabulary can grow without a migration.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Legacy SHA-256 hex supplied by older clients. New registrations use the
    # server-authoritative `content_fingerprint` below and do not hash the File
    # in the browser.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Versioned fingerprint derived from immutable object metadata (currently
    # GCS MD5 + size). Client-provided hashes are never used when this exists.
    content_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stable browser identity for one selected file. New clients reuse it when
    # refreshing a presign, making reservations idempotent across HTTP retries.
    # NULL keeps every pre-0074 row compatible with the unique constraint.
    client_upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upload_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Immutable GCS generation verified during registration. Workers must fetch
    # this generation, never whichever bytes happen to occupy the path later.
    gcs_generation: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Server-side ffprobe results (PR1a wires probing for video assets). The matcher
    # never trusts client-probed values.
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # width / height of the source — drives aspect-aware slot resolution (plan 005,
    # outside-voice finding 4). NULL until analysis runs.
    aspect: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Persisted analysis output (image_metadata / clip_metadata agents, PR1a).
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Creator-authored context about what this visual represents. Kept separate
    # from Nova's generated analysis so matching can prefer user intent without
    # rewriting AI metadata.
    user_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # preparing | promoting | cleanup_pending | uploaded (legacy) | queued |
    # analyzing | ready | failed
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="uploaded")
    # Media readiness is deliberately independent from AI analysis status:
    # manual overlays only need verified decode/probe (and a required preview),
    # while suggestions still require status="ready" and an analysis payload.
    # pending | ready | unreadable | failed
    media_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # Stable, user-safe failure information. Raw provider exceptions stay in
    # structured logs and are never serialized to creators.
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Analysis dispatch/claim fence. A stale worker may finish after a retry;
    # the token prevents that old attempt from overwriting the newer result.
    analysis_attempt_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    analysis_last_dispatched_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    analysis_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Browser-safe JPEG preview object key (pool preview pipeline). NULL = never
    # attempted; "" = attempted, none produced (failure sentinel — do not retry
    # from the fast paths, only the bounded maintenance backfill); non-empty =
    # object key of a JPEG sibling under the same persistent pool prefix.
    preview_gcs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_gcs_generation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A retained, hidden receipt for a deduplication race/lost response. It is
    # excluded from capacity/list queries and points to the canonical asset.
    deduplicated_to_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_item_assets.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


# Allowed signals — kept in lockstep with the CHECK constraint in migration 0043
# and the Literal on the POST /me/feedback body. 'note' carries free text; the
# three thumb-class signals (up/down/more_like_this) are mutually exclusive per
# video (enforced in the write endpoint, not the DB, so a note can coexist).
VIDEO_FEEDBACK_SIGNALS = ("up", "down", "more_like_this", "note")
VIDEO_FEEDBACK_THUMB_SIGNALS = ("up", "down", "more_like_this")


class VideoFeedback(Base):
    """One feedback signal a user left on their own video or content plan (Phase 2).

    The raw signal store behind the feedback loop. Rows are user-scoped writes;
    a deterministic rollup (services/feedback_summary) compresses them into the
    bounded ContentPlan.preference_summary that re-tunes generation. `job_id` is
    set for per-video feedback (👍/👎/more-like-this/note on a library tile);
    `content_plan_id` is set for the plan-level "Tell the AI" steer note. Exactly
    one of the two is set per row (enforced in the write endpoint)."""

    __tablename__ = "video_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Per-video feedback target (NULL for plan-level steer notes).
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True
    )
    # Plan-level steer target (NULL for per-video feedback).
    content_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_plans.id", ondelete="CASCADE"), nullable=True
    )
    # up | down | more_like_this | note (CHECK-constrained, see migration 0043).
    signal: Mapped[str] = mapped_column(Text, nullable=False)
    # Free text for `signal == 'note'`; UNTRUSTED — sanitized before it enters any
    # agent prompt (services/feedback_summary).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "signal IN ('up', 'down', 'more_like_this', 'note')",
            name="ck_video_feedback_signal",
        ),
        # Bounded most-recent-N rollup query: WHERE user_id ORDER BY created_at DESC.
        Index("idx_video_feedback_user_created", "user_id", "created_at"),
        # Batched feedback_signal lookup for GET /me/jobs (job_id = ANY(:ids)).
        Index("idx_video_feedback_job", "job_id"),
        Index("idx_video_feedback_content_plan", "content_plan_id"),
    )


INTERNAL_ACCOUNT_GRANT_STATUSES = ("active", "revoked")
TRAINING_CONSENT_ACTIONS = ("grant", "revoke")
TRAINING_CONSENT_PURPOSES = ("edit_feedback_training",)
EDIT_ARTIFACT_KINDS = ("final_render", "poster", "contact_sheet")
EDIT_ARTIFACT_CAPTURE_ORIGINS = ("creator", "internal", "admin", "system")
EDIT_ARTIFACT_ELIGIBILITY_BASES = ("internal_grant", "training_consent")
EDIT_INTERACTION_EVENT_KINDS = ("proposal", "execution", "save_link")
EDIT_INTERACTION_PROPOSAL_OUTCOMES = (
    "applied",
    "clarification",
    "no_effect",
    "unsupported",
    "stale",
    "failed",
)
EDIT_INTERACTION_EXECUTION_OUTCOMES = ("applied", "no_effect", "rejected", "stale", "failed")
EDIT_FEEDBACK_RATINGS = ("good", "bad", "mixed", "not_applicable")
EDIT_FEEDBACK_DIMENSIONS = (
    "overall_quality",
    "ai_guidance_and_response",
    "instruction_fit",
    "hook",
    "pacing",
    "cuts",
    "clip_selection",
    "clip_ordering",
    "captions",
    "text",
    "transitions",
    "music",
    "audio",
    "effects",
    "overlays",
)
RETENTION_EVENT_TYPES = ("copy", "purge", "build", "ready", "failed")
RETENTION_EVENT_STATUSES = ("pending", "started", "succeeded", "failed")
TRAINING_DATASET_EXPORT_STATUSES = (
    "pending",
    "building",
    "ready",
    "failed",
    "revoked",
)


class InternalAccountGrant(Base):
    """Explicit operator-owned eligibility marker for internal creators.

    Internal status is deliberately not inferred from an email domain,
    authentication provider, or the synthetic user.  Grant rows are retained
    as an audit trail; revocation is a status transition performed by the
    operator-owned service, not a deletion of the grant record.
    """

    __tablename__ = "internal_account_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    granted_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    creator: Mapped["User"] = relationship(back_populates="internal_account_grants")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_internal_account_grants_status",
        ),
        UniqueConstraint(
            "creator_id",
            "idempotency_key",
            name="uq_internal_account_grants_idempotency",
        ),
        Index(
            "uq_internal_account_grants_active_creator",
            "creator_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class TrainingConsentEvent(Base):
    """Append-only grant/revoke ledger for customer training consent."""

    __tablename__ = "training_consent_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="creator")
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    revokes_consent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_consent_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    creator: Mapped["User"] = relationship(back_populates="training_consent_events")
    revokes_consent: Mapped["TrainingConsentEvent | None"] = relationship(
        remote_side="TrainingConsentEvent.id", foreign_keys=[revokes_consent_id]
    )

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('edit_feedback_training')",
            name="ck_training_consent_events_purpose",
        ),
        CheckConstraint(
            "action IN ('grant', 'revoke')",
            name="ck_training_consent_events_action",
        ),
        UniqueConstraint(
            "creator_id",
            "idempotency_key",
            name="uq_training_consent_events_idempotency",
        ),
        Index(
            "idx_training_consent_events_creator_purpose_effective",
            "creator_id",
            "purpose",
            "effective_at",
        ),
    )


class EditArtifact(Base):
    """Append-only identity record for one retained final render or derivative."""

    __tablename__ = "edit_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_items.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    parent_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("edit_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    render_generation_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_schema_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1")
    proposal_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Allowlisted immutable direction/rationale snapshot. Never read the
    # mutable PlanItem.edit_proposal when constructing training records.
    direction_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    render_hash: Mapped[str] = mapped_column(Text, nullable=False)
    render_receipt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    render_receipt_schema_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The verified receipt is retained for exact identity/audit. Exporters must
    # project an allowlisted subset rather than serializing this JSONB wholesale.
    render_receipt: Mapped[dict] = mapped_column(JSONB, nullable=False)
    prompt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_manifest: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Exact product-render source identity. This is not the retained training
    # copy; TrainingArtifactRetentionEvent owns that separate path/generation.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_generation: Mapped[str] = mapped_column(Text, nullable=False)
    storage_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    storage_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capture_origin: Mapped[str] = mapped_column(Text, nullable=False)
    eligibility_basis: Mapped[str] = mapped_column(Text, nullable=False)
    consent_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_consent_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    internal_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("internal_account_grants.id", ondelete="SET NULL"),
        nullable=True,
    )
    creator_split: Mapped[str] = mapped_column(Text, nullable=False)
    plan_item_split: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    creator: Mapped["User"] = relationship(back_populates="edit_artifacts")
    plan_item: Mapped["PlanItem"] = relationship(back_populates="edit_artifacts")
    parent_artifact: Mapped["EditArtifact | None"] = relationship(
        remote_side="EditArtifact.id", foreign_keys=[parent_artifact_id]
    )
    consent_event: Mapped["TrainingConsentEvent | None"] = relationship(
        foreign_keys=[consent_event_id]
    )
    internal_grant: Mapped["InternalAccountGrant | None"] = relationship(
        foreign_keys=[internal_grant_id]
    )
    feedback_annotations: Mapped[list["EditFeedbackAnnotation"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )
    retention_events: Mapped[list["TrainingArtifactRetentionEvent"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('final_render', 'poster', 'contact_sheet')",
            name="ck_edit_artifacts_kind",
        ),
        CheckConstraint(
            "capture_origin IN ('creator', 'internal', 'admin', 'system')",
            name="ck_edit_artifacts_capture_origin",
        ),
        CheckConstraint(
            "eligibility_basis IN ('internal_grant', 'training_consent')",
            name="ck_edit_artifacts_eligibility_basis",
        ),
        CheckConstraint(
            "creator_split IN ('train', 'validation', 'test', 'holdout')",
            name="ck_edit_artifacts_creator_split",
        ),
        CheckConstraint(
            "plan_item_split IN ('train', 'validation', 'test', 'holdout')",
            name="ck_edit_artifacts_plan_item_split",
        ),
        Index("idx_edit_artifacts_creator_created", "creator_id", "created_at"),
        Index("idx_edit_artifacts_plan_item_created", "plan_item_id", "created_at"),
        Index("idx_edit_artifacts_kind_created", "artifact_kind", "created_at"),
        Index("idx_edit_artifacts_split", "creator_split", "plan_item_split"),
        # Distinct edit versions may intentionally reuse the same immutable
        # rendered object. Identity belongs to the exact job/variant/render,
        # not globally to the storage object that backs it.
        UniqueConstraint(
            "job_id",
            "variant_id",
            "render_generation_id",
            "artifact_kind",
            name="uq_edit_artifacts_render_identity",
        ),
    )


class EditInteractionReceipt(Base):
    """Append-only Copilot proposal/execution evidence.

    A proposal row records exactly what the server returned. The browser then
    appends one execution row after its local validator/applier runs. Execution
    retries reuse ``client_event_id``; the creator-scoped unique constraint
    prevents a retry against a different proposal from duplicating evidence.
    """

    __tablename__ = "edit_interaction_receipts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_kind: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("edit_interaction_receipts.id", ondelete="CASCADE"),
        nullable=True,
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_items.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    utterance: Mapped[str] = mapped_column(Text, nullable=False)
    inferred_intent: Mapped[str] = mapped_column(Text, nullable=False)
    model_reply: Mapped[str] = mapped_column(Text, nullable=False)
    eligibility_basis: Mapped[str] = mapped_column(Text, nullable=False)
    consent_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("training_consent_events.id", ondelete="CASCADE"),
        nullable=True,
    )
    internal_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("internal_account_grants.id", ondelete="CASCADE"),
        nullable=True,
    )
    proposed_operations: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    proposed_operations_digest: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    execution_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    before_revision_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_revision_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    creator: Mapped["User"] = relationship(
        back_populates="edit_interaction_receipts", foreign_keys=[creator_id]
    )
    plan_item: Mapped["PlanItem"] = relationship(back_populates="edit_interaction_receipts")
    proposal_receipt: Mapped["EditInteractionReceipt | None"] = relationship(
        remote_side="EditInteractionReceipt.id", foreign_keys=[proposal_receipt_id]
    )

    __table_args__ = (
        CheckConstraint(
            "event_kind IN ('proposal', 'execution', 'save_link')",
            name="ck_edit_interaction_receipts_event_kind",
        ),
        CheckConstraint(
            "proposal_outcome IN ('applied', 'clarification', 'no_effect', "
            "'unsupported', 'stale', 'failed')",
            name="ck_edit_interaction_receipts_proposal_outcome",
        ),
        CheckConstraint(
            "execution_outcome IS NULL OR execution_outcome IN "
            "('applied', 'no_effect', 'rejected', 'stale', 'failed')",
            name="ck_edit_interaction_receipts_execution_outcome",
        ),
        CheckConstraint(
            "(eligibility_basis = 'training_consent' AND consent_event_id IS NOT NULL "
            "AND internal_grant_id IS NULL) OR "
            "(eligibility_basis = 'internal_grant' AND internal_grant_id IS NOT NULL "
            "AND consent_event_id IS NULL)",
            name="ck_edit_interaction_receipts_eligibility",
        ),
        CheckConstraint(
            "(event_kind = 'proposal' AND proposal_receipt_id IS NULL "
            "AND client_event_id IS NULL AND execution_outcome IS NULL) OR "
            "(event_kind IN ('execution', 'save_link') AND proposal_receipt_id IS NOT NULL "
            "AND client_event_id IS NOT NULL AND execution_outcome IS NOT NULL)",
            name="ck_edit_interaction_receipts_event_shape",
        ),
        UniqueConstraint(
            "creator_id", "client_event_id", name="uq_edit_interaction_receipts_creator_event"
        ),
        Index(
            "idx_edit_interaction_receipts_proposal_created",
            "proposal_receipt_id",
            "created_at",
        ),
        Index("idx_edit_interaction_receipts_plan_item_created", "plan_item_id", "created_at"),
    )


class EditFeedbackAnnotation(Base):
    """Append-only reviewer annotation; corrections supersede prior rows."""

    __tablename__ = "edit_feedback_annotations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_items.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("edit_artifacts.id", ondelete="CASCADE"), nullable=False
    )
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    frame_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewer_identity: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("edit_feedback_annotations.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    creator: Mapped["User"] = relationship(back_populates="edit_feedback_annotations")
    plan_item: Mapped["PlanItem"] = relationship(back_populates="edit_feedback_annotations")
    artifact: Mapped["EditArtifact"] = relationship(back_populates="feedback_annotations")
    supersedes_annotation: Mapped["EditFeedbackAnnotation | None"] = relationship(
        remote_side="EditFeedbackAnnotation.id", foreign_keys=[supersedes_annotation_id]
    )

    __table_args__ = (
        CheckConstraint(
            "dimension IN ('overall_quality', 'ai_guidance_and_response', "
            "'instruction_fit', 'hook', 'pacing', 'cuts', 'clip_selection', "
            "'clip_ordering', 'captions', 'text', 'transitions', 'music', "
            "'audio', 'effects', 'overlays')",
            name="ck_edit_feedback_annotations_dimension",
        ),
        CheckConstraint(
            "rating IN ('good', 'bad', 'mixed', 'not_applicable')",
            name="ck_edit_feedback_annotations_rating",
        ),
        CheckConstraint(
            "frame_start_ms IS NULL OR frame_start_ms >= 0",
            name="ck_edit_feedback_annotations_frame_start",
        ),
        CheckConstraint(
            "frame_end_ms IS NULL OR frame_end_ms >= 0",
            name="ck_edit_feedback_annotations_frame_end",
        ),
        CheckConstraint(
            "frame_start_ms IS NULL OR frame_end_ms IS NULL OR frame_end_ms >= frame_start_ms",
            name="ck_edit_feedback_annotations_frame_order",
        ),
        CheckConstraint(
            "rating = 'not_applicable' OR (rationale IS NOT NULL AND length(trim(rationale)) > 0)",
            name="ck_edit_feedback_annotations_rationale",
        ),
        Index("idx_edit_feedback_annotations_creator_created", "creator_id", "created_at"),
        Index("idx_edit_feedback_annotations_plan_item_created", "plan_item_id", "created_at"),
        Index("idx_edit_feedback_annotations_artifact_created", "artifact_id", "created_at"),
    )


class TrainingArtifactRetentionEvent(Base):
    """Immutable audit event for generation-pinned training-copy retention."""

    __tablename__ = "training_artifact_retention_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("edit_artifacts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # Dedicated generation-pinned training copy under the creator edit-feedback
    # prefix. Never substitute the product-render source path from EditArtifact.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_generation: Mapped[str] = mapped_column(Text, nullable=False)
    # The retained copy does not have a stable hash until the copy succeeds.
    # Pending/started/failed events therefore keep this null; the table check
    # below requires it for succeeded events.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    creator: Mapped["User"] = relationship(back_populates="training_artifact_retention_events")
    artifact: Mapped["EditArtifact"] = relationship(back_populates="retention_events")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('copy', 'purge', 'build', 'ready', 'failed')",
            name="ck_training_retention_event_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'started', 'succeeded', 'failed')",
            name="ck_training_retention_event_status",
        ),
        CheckConstraint(
            "status != 'succeeded' OR content_hash IS NOT NULL",
            name="ck_training_retention_succeeded_hash",
        ),
        UniqueConstraint(
            "artifact_id", "idempotency_key", name="uq_training_retention_event_idempotency"
        ),
        Index("idx_training_retention_events_artifact_created", "artifact_id", "created_at"),
        Index("idx_training_retention_events_creator_created", "creator_id", "created_at"),
    )


class TrainingDatasetExport(Base):
    """Immutable manifest for an allowlisted, consent-safe dataset export."""

    __tablename__ = "training_dataset_exports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_schema_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1")
    export_format: Mapped[str] = mapped_column(Text, nullable=False, server_default="jsonl")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    creator_split_version: Mapped[str] = mapped_column(Text, nullable=False)
    plan_item_split_version: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_generation: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    requested_by_user: Mapped["User | None"] = relationship(
        back_populates="training_dataset_exports", foreign_keys=[requested_by]
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'building', 'ready', 'failed', 'revoked')",
            name="ck_training_dataset_exports_status",
        ),
        CheckConstraint(
            "export_format IN ('jsonl', 'parquet')",
            name="ck_training_dataset_exports_format",
        ),
        UniqueConstraint(
            "requested_by",
            "idempotency_key",
            name="uq_training_dataset_exports_idempotency",
        ),
        Index("idx_training_dataset_exports_status_created", "status", "created_at"),
        Index("idx_training_dataset_exports_requested_by_created", "requested_by", "created_at"),
    )


class BuildTask(Base):
    """A unit of autonomous-dev-loop builder work (M4 — the builder cron's
    task queue). The GitHub Actions builder claims the oldest incomplete row
    with `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`, does a bounded chunk in a
    worktree, WIP-commits to `branch`, writes a `progress_note` checkpoint, and
    releases the row. The schedule (every ~30-60 min) is the auto-resume
    mechanism: a soft-exit on a Claude usage-limit leaves the row resumable so
    the next tick continues from the checkpoint — there is no waiting logic.

    All status transitions go through `app.services.build_task_repo` (the
    builder, reaper, and heartbeat import it — no scattered SQL). See the
    Session-Resilience plan section.

    Security invariant (CEO D3): `provenance` records whether the signal that
    minted this task is `trusted` (rubric-gap finder, failing evals, founder
    notes) or `untrusted` (VideoFeedback notes, future Reddit/TikTok comments).
    In v1 an `untrusted` signal must NEVER auto-mint a build_task — only trusted
    signals mint. Enforced in `build_task_repo.create_build_task` + tested.
    """

    __tablename__ = "build_task"

    # Status lifecycle:
    #   queued           → not yet claimed; reaper never touches it.
    #   in_progress      → claimed by a builder run (claimed_at / claimed_by set);
    #                      the reaper resets a stale one back to `queued`.
    #   gating           → built; a gate tick claimed it to run the hard gates +
    #                      rebase onto origin/main (Phase 2). The reaper sweeps a
    #                      stale `gating` row (claimed OR unclaimed) back to queued.
    #   awaiting_approval→ gates green + PR opened; rests here until a human merges
    #                      (Phase 3's phone surface reads these). Idle, NOT claimed
    #                      — the reaper leaves it alone; the digest surfaces it.
    #   blocked          → attempt_count tripped the cap; needs a human (no
    #                      infinite retry loop). Terminal until a human re-queues.
    #   done             → completed; idempotent skip on any future claim.
    STATUSES = (
        "queued",
        "in_progress",
        "gating",
        "awaiting_approval",
        "blocked",
        "done",
    )
    PROVENANCES = ("trusted", "untrusted")
    # Only trusted provenance may mint a build_task in v1 (security invariant).
    MINTABLE_PROVENANCES = ("trusted",)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # queued | in_progress | blocked | done (CHECK-constrained; see migration 0045).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    # Free-text checkpoint label the builder writes each run ("Stage E: aligning
    # overlays"); how a fresh session re-orients without a resumable Claude
    # session. NULL until the first checkpoint.
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The WIP git branch the builder commits to; `git log -1` + `git diff` on it
    # is the resume anchor. NULL until the builder creates the branch.
    branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Incremented every time a run fails (non-zero hard exit, NOT a soft-exit on
    # a usage limit). The reaper trips this over ATTEMPT_CAP → status `blocked`.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # trusted | untrusted (CHECK-constrained). Security boundary — see docstring.
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default="trusted")
    # Lower number = higher priority (claimed first). Ties broken by created_at.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    # Set when a builder run claims the row (in_progress); the reaper compares
    # claimed_at against a generous threshold to detect a runner that died.
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Opaque run identity (e.g. the GH Actions run id) — observability only.
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    # Human-readable task spec (TODOS.md house format: title / what / why / how).
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Ship-gate (Phase 2) ──────────────────────────────────────────────────
    # The exact commit the builder pushed; the gate tick asserts
    # origin/<branch> == head_sha before running so it never gates a branch the
    # builder never finished pushing. NULL until the first push.
    head_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The PR opened once gates pass (open_pr). NULL until then.
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-gate pass/fail + advisory /qa + codex results; rendered into the PR
    # body and the daily digest. NULL until the gate tick writes it.
    gate_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # Keep in lockstep with STATUSES above + migration 0046's _STATUS_NEW.
        # create_all() (used by tests) reads THIS constraint, not the migration.
        CheckConstraint(
            "status IN ('queued', 'in_progress', 'gating', 'awaiting_approval', 'blocked', 'done')",
            name="ck_build_task_status",
        ),
        CheckConstraint(
            "provenance IN ('trusted', 'untrusted')",
            name="ck_build_task_provenance",
        ),
        # Claim path: WHERE status='queued' ORDER BY priority, created_at LIMIT 1
        # FOR UPDATE SKIP LOCKED. This index serves the ORDER BY directly.
        Index("idx_build_task_status_priority_created", "status", "priority", "created_at"),
        # Reaper path: WHERE status='in_progress' AND claimed_at < cutoff.
        Index("idx_build_task_status_claimed", "status", "claimed_at"),
    )
