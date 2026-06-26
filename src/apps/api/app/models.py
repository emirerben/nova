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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

TIMESTAMPTZ = TIMESTAMP(timezone=True)


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
    # 1:1 — the user's onboarding persona (NULL until onboarding starts).
    persona: Mapped["Persona | None"] = relationship(back_populates="user", uselist=False)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)  # instagram|youtube|tiktok
    access_token: Mapped[bytes] = mapped_column(BYTEA, nullable=False)  # AES-256 Fernet
    refresh_token: Mapped[bytes | None] = mapped_column(BYTEA)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
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
    # True pipeline-wall-time anchors. Distinct from created_at (queue insert)
    # and updated_at (any column write).
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="jobs")
    clips: Mapped[list["JobClip"]] = relationship(back_populates="job")
    # The plan item this job was minted for (NULL for non-plan jobs). One-directional;
    # PlanItem.current_job is the matching forward link (not a back_populates inverse —
    # the two FKs are distinct columns, see PlanItem.current_job_id).
    content_plan_item: Mapped["PlanItem | None"] = relationship(foreign_keys=[content_plan_item_id])

    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_template_id", "template_id"),
        Index("idx_jobs_music_track_id", "music_track_id"),
        Index("idx_jobs_failure_reason", "failure_reason"),
        Index("idx_jobs_created_at", "created_at"),
        Index("idx_jobs_content_plan_item_id", "content_plan_item_id"),
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


class AgentRun(Base):
    """One row per agent invocation. Captures full input + raw LLM response +
    parsed output so the admin job-debug view can show exactly what each
    agent saw and produced for a given job. job_id is nullable so off-job
    calls (track-level analysis, eval harness) can also be persisted without
    inventing a fake job UUID.
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

    __table_args__ = (
        Index("idx_agent_run_job_id_created", "job_id", "created_at"),
        Index("idx_agent_run_agent_name", "agent_name"),
        Index("idx_agent_run_template_id_created", "template_id", "created_at"),
        Index("idx_agent_run_music_track_id_created", "music_track_id", "created_at"),
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
    # posting_cadence, sample_topics[], signature_quote}.
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
    content_plans: Mapped[list["ContentPlan"]] = relationship(back_populates="persona")


class ContentPlan(Base):
    """A parent entity owning N PlanItems. NOT a column on Job — each generated
    video stays one Job, and a PlanItem carries current_job_id."""

    __tablename__ = "content_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="CASCADE"), nullable=False
    )
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
    persona: Mapped["Persona"] = relationship(back_populates="content_plans")
    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="content_plan", order_by="PlanItem.position"
    )

    __table_args__ = (Index("idx_content_plans_user_id", "user_id"),)


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
    # Per-item preference for landscape source clips: "fit" (letterbox — full-width,
    # black bars top & bottom, never enlarged — the default) | "fill" (center-crop to
    # fill the 9:16 frame). Portrait and square clips are always cropped regardless.
    # Plain Text + server_default so legacy rows immediately letterbox landscape clips
    # without a backfill (same pattern as edit_format). Validated in the route layer;
    # no DB CHECK so the vocabulary can grow without a migration.
    landscape_fit: Mapped[str] = mapped_column(Text, nullable=False, server_default="fit")
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

    __table_args__ = (
        Index("idx_plan_items_content_plan_id_day", "content_plan_id", "day_index"),
        Index("idx_plan_items_content_plan_id_position", "content_plan_id", "position"),
    )


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
