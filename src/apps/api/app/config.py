from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    storage_bucket: str
    storage_provider: str = "gcs"
    gcloud_project: str = ""
    google_application_credentials: str = ""
    google_service_account_json: str = ""

    # Redis / Celery
    redis_url: str = "redis://localhost:6379"

    # Database
    database_url: str

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, v: str) -> str:
        """Fly Postgres (and Heroku) provide postgres:// URLs, but
        SQLAlchemy 1.4+ only recognises the postgresql:// scheme."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        return v

    @property
    def asyncpg_database_url(self) -> str:
        """Return a postgresql+asyncpg:// URL suitable for asyncpg.

        asyncpg does not understand libpq-specific query params like
        ``sslmode``.  We translate ``sslmode`` to asyncpg's ``ssl``
        param and swap the scheme to ``postgresql+asyncpg://``.
        """
        url = self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        # Translate libpq sslmode → asyncpg ssl
        sslmode_vals = qs.pop("sslmode", None)
        if sslmode_vals and "ssl" not in qs:
            qs["ssl"] = sslmode_vals  # e.g. ["disable"]
        cleaned_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=cleaned_query))

    # OpenAI (Whisper fallback)
    openai_api_key: str = ""

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"  # "gemini-2.5-flash" | "gemini-2.5-pro"

    # Admin
    admin_api_key: str = ""

    # yt-dlp cookies for admin URL imports. Use YTDLP_COOKIES_B64 in hosted
    # environments (secret-safe, decoded into a short-lived 0600 temp file) or
    # YTDLP_COOKIES_PATH for local development / mounted secret files.
    ytdlp_cookies_b64: str = ""
    ytdlp_cookies_path: str = ""

    # Transcription backend
    transcriber_backend: str = "gemini"  # "gemini" | "whisper"

    # Template
    default_template_id: str = ""

    # Whisper
    whisper_backend: str = "openai-api"  # "openai-api" | "local"
    whisper_model: str = "base.en"

    # Hard ceiling on lyric extraction wall time (LRCLIB search + Whisper
    # transcription + alignment). Beat analysis caps at 300s; the lyric task
    # runs after it so we keep a tight budget. LRCLIB itself needs no API
    # key — see app/services/lrclib_client.py.
    lyrics_extraction_timeout_s: float = 90.0

    # CORS — JSON array in env: ALLOWED_ORIGINS='["https://nova.io","http://localhost:3000"]'
    allowed_origins: list[str] = ["http://localhost:3000"]

    # Waitlist admin
    waitlist_admin_secret: str = "changeme"

    # Resend (transactional email) — leave empty to skip confirmation emails
    resend_api_key: str = ""

    # Eval harness — gates per-slot GCS upload for visual comparison
    eval_harness_enabled: bool = False

    # Single-pass FFmpeg encode — collapses stages 1-6 (reframe, pre-burn,
    # curtain, join, text overlay) into one filter_complex invocation. Default
    # OFF; flip after empirical SSIM/VMAF parity is proven on REF jobs. Can
    # also be forced per-job via the `force_single_pass` kwarg on
    # orchestrate_template_job.
    single_pass_encode_enabled: bool = False

    # Layer-2 text-overlay extraction pipeline. When False, the existing
    # single-call `nova.compose.template_text` Gemini agent runs unchanged.
    # When True, the OCR + grouping + transcript-alignment pipeline replaces
    # the agent's body (same input/output schema, different implementation).
    # Default OFF until PR 2 lands the pipeline behind the flag and PR 3
    # validates output quality on staging templates. See
    # plans/template-text-overlay-layer-2-architecture.md.
    text_overlay_v2_enabled: bool = False

    # Auto-music mode (Phase 3 of the auto-music feature). When False, the
    # `orchestrate_auto_music_job` Celery task and the (yet-to-land Phase 4)
    # POST /auto-music-jobs route are unreachable from user input — the
    # orchestrator early-exits with `processing_failed` if it's somehow
    # invoked. Existing template-mode + manual music-mode flows are
    # unaffected either way. Flip to True when the music library has
    # enough labeled tracks for the matcher to do real work (~15+).
    enable_auto_music_mode: bool = False

    # Skia text renderer for agentic templates + music lyrics. When True
    # (default), agentic-template and music-job text overlays render via
    # `app.pipeline.text_overlay_skia` instead of Pillow + libass. Classic
    # non-music templates are unaffected either way. Flip to False on Fly
    # (`fly secrets set TEXT_RENDERER_SKIA_ENABLED=false --app nova-video`
    # then restart workers) to revert agentic + music jobs to the Pillow +
    # libass path instantly. The kill switch is read per render call —
    # no in-flight job is mid-rendered with a switched-on flag.
    text_renderer_skia_enabled: bool = True

    # Dynamic crossfade scheduling for the line-style lyric overlay path
    # (`app.pipeline.lyric_injector._inject_line`). When True (default), the
    # scheduler matches per-pair crossfade durations, anchors actual emitted
    # overlap to the matched window, and tags the outgoing overlay with
    # `fade_out_curve="sqrt"` so both renderers use mirror-symmetric fade
    # curves during inter-line crossfades — α_L_N + α_L_N+1 = 1 at every t,
    # so no two consecutive lyric overlays render readably simultaneously.
    # When False, the scheduler reproduces pre-fix behavior byte-identically:
    # legacy `dynamic_max_overlap = min(max_overlap_s, fade_in_s + fade_out_s)`
    # cap, no `fade_out_curve` key, no dynamic duration matching. Flip to
    # False on Fly (`fly secrets set LYRIC_DYNAMIC_CROSSFADE_ENABLED=false
    # --app nova-video` then restart workers) to roll back music + agentic
    # lyric scheduling instantly. The kill switch is read inside
    # `inject_lyric_overlays`, so every job picks up the current value at
    # scheduling time.
    lyric_dynamic_crossfade_enabled: bool = True

    # Universal text-overlay constraint pass. When True (default), every
    # overlay collected by `_collect_absolute_overlays` (agentic templates +
    # music lyrics + generative edits — NOT classic templates, which never
    # carry the canonical overlay dict through this path) is run through
    # `app.pipeline.overlay_constraints.apply_overlay_constraints`: text is
    # shrunk/wrapped to fit a max line count and repositioned into the 9:16
    # safe zone. The pass only rewrites `text_size_px` / `position_*_frac`
    # (fields both renderers already honor), so it is renderer-parity-safe and
    # can only improve overflowing text. Flip to False to disable the guarantee
    # (e.g. to isolate a layout regression) without redeploying.
    style_constraints_enabled: bool = True

    # agent_run retention (days). Rows with job_id IS NOT NULL and
    # created_at older than this are deleted by the daily
    # `tasks.cleanup_agent_runs` Beat task. Template- and track-scoped rows
    # (job_id IS NULL) are kept indefinitely — they back the per-template
    # debug view and aren't growth-bound to job volume. Default 30d keeps
    # the job-debug "what did each agent see?" window meaningful while
    # bounding table size. Set higher temporarily during incident triage.
    agent_run_retention_days: int = 30

    # Security
    token_encryption_key: str = ""

    # OAuth
    instagram_client_id: str = ""
    instagram_client_secret: str = ""
    instagram_redirect_uri: str = "http://localhost:8000/auth/instagram/callback"

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = "http://localhost:8000/auth/youtube/callback"

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://localhost:8000/auth/tiktok/callback"

    # Scoring weights (named constants — change here only)
    hook_weight: float = 0.65
    engagement_weight: float = 0.35

    # Pipeline limits
    max_upload_bytes: int = 4 * 1024 * 1024 * 1024  # 4GB
    max_duration_s: float = 1800.0  # 30 min
    max_concurrent_jobs_per_user: int = 5
    max_jobs_per_hour_per_user: int = 20

    # Output spec
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int = 30
    # Capped-CRF ceiling, NOT an ABR target — see reframe._encoding_args. CRF 18
    # is the quality driver; this bounds peak bitrate (bufsize = 2× this). Raised
    # 4M→8M after the 4M ceiling macroblocked the dark sky on a 10-bit HLG iPhone
    # sunset (lisbon1.MOV, 2026-05-25). 8M cleared the blocking with no visible
    # gain from 12M at phone-delivery scale; +~70% file size on gradient-heavy clips.
    output_video_bitrate: str = "8M"
    output_audio_bitrate: str = "192k"
    output_min_duration_s: float = 45.0
    output_max_duration_s: float = 59.0
    output_target_lufs: float = -14.0


settings = Settings()
