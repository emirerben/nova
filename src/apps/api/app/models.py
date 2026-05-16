"""SQLAlchemy ORM models matching the plan's data model exactly."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    jobs: Mapped[list["Job"]] = relationship(back_populates="user")
    oauth_tokens: Mapped[list["OAuthToken"]] = relationship(back_populates="user")


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
    # Per-template gate for the single-pass encode rollout. Default false
    # means every existing row stays on the multi-pass path until a
    # parity + benchmark run promotes it. Combined with the env-level
    # ``settings.single_pass_encode_enabled`` via AND — flipping either
    # alone has zero render impact (see _run_template_job's effective
    # render-path resolution).
    single_pass_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

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


class TemplateRecipeVersion(Base):
    """Tracks recipe versions across analyze/reanalyze cycles for comparison."""

    __tablename__ = "template_recipe_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[str] = mapped_column(
        Text, ForeignKey("video_templates.id", ondelete="CASCADE"), nullable=False
    )
    recipe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # initial_analysis | reanalysis | manual_edit | remerge
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        Index("idx_music_tracks_status", "analysis_status"),
        Index("idx_music_tracks_published", "published_at"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # importing|queued|processing|clips_ready|clips_ready_partial|
    # posting|posting_partial|done|posting_failed|processing_failed
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

    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_template_id", "template_id"),
        Index("idx_jobs_music_track_id", "music_track_id"),
        Index("idx_jobs_failure_reason", "failure_reason"),
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
    )
