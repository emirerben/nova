from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import Field, field_validator
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

    # Internal server-to-server auth (Next.js plan proxy → API).
    # Must match INTERNAL_API_KEY in the Next.js environment.
    internal_api_key: str = ""

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
    # Narrated-voiceover transcription model override (local backend only). The
    # narration becomes burned + editable captions, so accuracy matters more here
    # than for clip analysis — a larger model means fewer words to hand-correct.
    # Empty → fall back to `whisper_model`. Kill switch: set to "base.en" to revert
    # the accuracy bump (smaller/faster, the pre-bump default). Slower + more RAM
    # than base.en; only affects the local backend (openai-api uses whisper-1).
    narrated_whisper_model: str = "small.en"

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

    # Kill switch for the format-aware talking-head archetype (Lane C/D). When
    # False, a job whose plan declares edit_format="talking_head" renders as the
    # default montage — the assembler module exists but the dispatch never routes
    # to it. Lane D (generative_build dispatch) is the consumer; Lane C only
    # defines the flag so the kill switch ships alongside the feature. Mirrors the
    # LYRIC_DYNAMIC_CROSSFADE_ENABLED rollback pattern: flip the Fly secret +
    # restart workers, no redeploy. Default OFF until the assembler is wired +
    # verified end-to-end.
    edit_format_talking_head_enabled: bool = False

    # Kill switch for the narrated walkthrough archetype. When False, a job
    # whose plan declares edit_format="narrated" follows the existing voiceover
    # or montage path. When True, eligible voiceover + filming-guide jobs align
    # each shot's script to the recorded narration and render one cut per step,
    # burn the transcribed narration as synced captions, and reflow short clips
    # to fill their step so the visuals never end before the voice.
    # Flipped on with the narrated captions + no-freeze work (PR1). The flag
    # stays the rollback lever: set False to revert to the voiceover-montage path.
    narrated_archetype_enabled: bool = True

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

    # Linear LRCLIB re-anchor for synced lyrics. When True (default), the
    # alignment layer can fit a small per-time drift curve before falling
    # back to the existing uniform median / single-L0 paths. This catches
    # official-video cuts whose vocals diverge progressively from the album
    # recording indexed by LRCLIB. When False, the linear path is skipped and
    # the old uniform-only behavior is preserved.
    lyric_linear_reanchor_enabled: bool = True

    # Post-snap re-anchor for karaoke + per-word-pop lyric overlays
    # (`app.pipeline.lyric_word_resync`). When True (default), each music-job
    # render rewrites karaoke/popup overlay `start_s`/`end_s` so the per-word
    # highlight (karaoke) and per-stage arrival (pop-in) stay glued to the
    # vocal even after beat-snap shifts the slot's cumulative position. When
    # False, falls back to pre-fix behavior byte-identically: overlays render
    # at their pre-snap slot-relative positions and may drift up to one
    # beat-interval (~250 ms on a 2.4 BPS track) against the audio.
    # Flip to False on Fly
    # (`fly secrets set LYRIC_WORD_RESYNC_ENABLED=false --app nova-video`
    # then restart workers) to roll back instantly if the re-anchor pass
    # itself ships a regression. The flag is read inside
    # `_collect_absolute_overlays` so every job picks up the current value
    # at render-collect time. Line-style overlays NEVER participate in this
    # pass — they don't carry the `section_anchor_s` stamp.
    lyric_word_resync_enabled: bool = True

    # Synced-anchor health check for the lyrics agent
    # (`app.agents.lyrics.LyricsExtractionAgent.compute`). When True (default),
    # if `align_with_line_anchors` returns confidence < 0.20, the agent
    # treats the LRCLIB syncedLyrics as being from a different recording of
    # the same song and falls back to plain_lyrics+whisper (or whisper_only)
    # so Whisper's timestamps drive the line bounds. Pairs with the
    # `duration` query param on `search_lrclib` as a two-layer defense
    # against the "Hawai" version-mismatch bug (synced anchors at 4.59s,
    # actual audio at 22s). Flip to False on Fly
    # (`fly secrets set LYRIC_SYNCED_ANCHOR_FALLBACK_ENABLED=false
    # --app nova-video` then restart workers) for emergency rollback if the
    # threshold turns out to be too aggressive in prod and starts demoting
    # legitimate synced extractions to plain+whisper. Read once per
    # extraction inside `compute()`. Existing cached extractions are not
    # touched by either value.
    lyric_synced_anchor_fallback_enabled: bool = True

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

    GENERATIVE_FAST_REBURN_ENABLED: bool = Field(
        default=True,
        description="Enable fast reburn path for generative variant edits (font/text/size). "
        "Set to false to fall back to full re-render on every edit (slower but safe). "
        "Layer-1 text persistence stays active regardless.",
    )

    # Transcript-synced editorial typographic sequence for generative edits
    # (the "Editorial" layout auto-upgrade, D6/D16). When True (default), an
    # agent_text variant whose intro layout resolves to "cluster" AND whose
    # final audio keeps the montage's original speech audible is transcribed
    # (Whisper, pre-mix `assembled_path` — D11) and rendered as phrase-by-phrase
    # styled scenes (`app/pipeline/phrase_sequence.py` + intro_cluster
    # EDITORIAL_STYLE); ineligible/failed variants fall back to a STYLED static
    # cluster. When False, behavior is byte-identical legacy: no transcription,
    # no styled cluster (compute_cluster_blocks gets style=None), no sequence
    # overlays. Read at render time inside the burn step, so flipping it
    # affects queued jobs and re-renders after a worker restart. Kill switch:
    # `fly secrets set EDITORIAL_SEQUENCE_ENABLED=false --app nova-video`
    # + `fly machine restart <id>` — no deploy needed.
    editorial_sequence_enabled: bool = True

    GENERATIVE_CLUSTER_INTRO_ENABLED: bool = Field(
        default=True,
        description="Allow the editorial word-cluster intro layout for generative edits "
        "(overlay_format_matcher layout='cluster' → app/pipeline/intro_cluster.py). "
        "Set to false to force every intro to the linear layout (kill switch: "
        "`fly secrets set GENERATIVE_CLUSTER_INTRO_ENABLED=false --app nova-video` "
        "+ worker restart). Persisted cluster variants re-render as linear too.",
    )

    NARRATIVE_CLIP_ORDER_ENABLED: bool = Field(
        default=True,
        description="Order plan-item edits by the filming guide's shot sequence "
        "(narrative mode in template_matcher.match). Read at render time, so "
        "flipping it affects queued jobs and re-renders after a worker restart. "
        "Set to false to fall back to pure greedy clip-to-slot matching.",
    )

    GENERATIVE_TIMELINE_EDITOR_ENABLED: bool = Field(
        default=True,
        description="Enable the post-generation clip timeline editor: durable "
        "per-job source copies, ai_timeline persistence on each montage variant, "
        "and the user_timeline override path on re-renders. Read at render time, "
        "so flipping it affects queued jobs and re-renders after a worker "
        "restart. Set to false to fall back to fresh clip matching on every "
        "re-render (timelines neither written nor honored). Kill switch: "
        "`fly secrets set GENERATIVE_TIMELINE_EDITOR_ENABLED=false --app "
        "nova-video` + `fly machine restart <id>` — no deploy needed.",
    )

    GENERATIVE_PARALLEL_VARIANTS_ENABLED: bool = Field(
        default=False,
        description="Render a generative job's variants concurrently (bounded by "
        "GENERATIVE_PARALLEL_VARIANTS_MAX) instead of one-at-a-time. ONLY safe on "
        "a dedicated-CPU worker — on shared CPUs concurrent FFmpeg encodes starve "
        "each other (the concurrency=2 incident, job d018d1c3, ballooned a 14s "
        "encode to >600s). Default off; flip on after the worker is on a "
        "performance VM. Kill switch: "
        "`fly secrets set GENERATIVE_PARALLEL_VARIANTS_ENABLED=false` + restart.",
    )

    GENERATIVE_PARALLEL_VARIANTS_MAX: int = Field(
        default=2,
        description="Max variants rendered concurrently when "
        "GENERATIVE_PARALLEL_VARIANTS_ENABLED is on. Keep ≤ the worker's "
        "dedicated vCPU count so each concurrent FFmpeg encode gets a real core.",
    )

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

    # Deep TikTok profile analysis (enriched fetch + LLM distillation).
    # When true, scrape_tiktok_profile chains analyze_tiktok_profile after the
    # flat fetch, enriching the persona/plan/hooks with the creator's own proven
    # style. Set to false for a quick kill switch (no deploy needed):
    #   fly secrets set TIKTOK_DEEP_ANALYSIS_ENABLED=false --app nova-video
    #   + fly machine restart <id>
    tiktok_deep_analysis_enabled: bool = True

    # Vision-based style ingest (Creator Agent style pipeline). When True,
    # scrape_tiktok_profile chains analyze_tiktok_style which downloads the
    # creator's own videos, runs StyleObservationAgent, and persists the aggregate
    # to persona.tiktok_profile["style_observations"]. Expensive (~30 Flash vision
    # calls + MP4 downloads per user). Ships OFF; flip after cost/latency bake:
    #   fly secrets set TIKTOK_STYLE_VISION_ENABLED=true --app nova-video
    #   + fly machine restart <id>
    # Separate from user_style_enabled (render gate) so ingest and render can be
    # dark-launched independently.
    tiktok_style_vision_enabled: bool = False

    # Per-user style entity (Creator Agent M1). When True, a UserStyle is derived
    # from the persona after generation and applied to every generative render:
    # style-set pin bypasses the per-render AgenticStyleSelectorAgent; knob overrides
    # win over the curated set's values inside _resolve_intro_overlay_params.
    # Ships OFF (False) so zero renders are affected on deploy; enable after live-eval
    # validation of the style_derivation agent:
    #   fly secrets set USER_STYLE_ENABLED=true --app nova-video + restart workers.
    # When False OR when personas.style is NULL: all_candidates carries no
    # "user_style" key → renders are byte-identical to pre-M1 output.
    user_style_enabled: bool = False

    # Conversational style agent (Creator Agent M2). When True, the
    # POST /personas/agent/start and POST /personas/agent/turn routes are live.
    # Ships OFF — enable after live-eval validation of StyleIntentAgent quality:
    #   fly secrets set STYLE_AGENT_ENABLED=true --app nova-video + restart workers.
    # When False: both agent routes return 404 — byte-identical to pre-M2 behavior.
    style_agent_enabled: bool = False

    # Creator Agent M4: ConformanceFeedbackAgent at clip-attach time (best-effort).
    # Ships OFF — fires async after attach_clips, never blocks the 200 response.
    # Enable after live-eval validation of the conformance_feedback agent:
    #   fly secrets set CONFORMANCE_FEEDBACK_ENABLED=true --app nova-video + restart workers.
    # When False: analyze_item_conformance task is a no-op; item.conformance stays NULL.
    conformance_feedback_enabled: bool = False

    # Media-overlay cards (slice 1): timed, positioned image/video "cards"
    # composited on top of a finished plan-item variant via a post-pass ffmpeg
    # encode. Additive + kill-switched; when False all variant bytes are
    # byte-identical to pre-slice-1 output. Enable after browser QA:
    #   fly secrets set MEDIA_OVERLAYS_ENABLED=true --app nova-video + restart workers.
    # When False: upload-urls + set-media-overlays routes return 404; the
    # worker apply-pass branch never fires.
    media_overlays_enabled: bool = False

    # Sound-effects glossary + user placement (PR-1 foundation). Admin-curated
    # SFX + user uploads placed at arbitrary timestamps in a plan-item variant.
    # Kill switch: SOUND_EFFECTS_ENABLED=false → sfx-upload-urls + sound-effects
    # routes return 404; the render apply-pass branch never fires.
    sound_effects_enabled: bool = False

    # Per-item "Ask Nova" advisor (plan dogfood feedback #2): conversational,
    # read-only advice about which clip fits which shot. Additive + auth'd; it
    # never writes state (the re-read offer goes through the clip-note PATCH).
    # Kill switch: PLAN_ITEM_ADVISOR_ENABLED=false → route returns 404.
    plan_item_advisor_enabled: bool = True

    # Idea-centric plan redesign (2026-06-17): PlanItem is the spine; no auto-generate
    # on create; ideas added directly as PlanItems; AI generation is opt-in.
    # Kill switch: IDEA_CENTRIC_PLAN_ENABLED=false → create_plan auto-generates (old behavior).
    idea_centric_plan_enabled: bool = True

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
    # sunset (lisbon1.MOV, 2026-05-25). Raised 8M→16M (2026-05-26) after a dark
    # HLG night clip (job 792f2d52) still pinned to the 8M ceiling and macroblocked:
    # at 16M the same footage uses ~16 Mbps, i.e. CRF 18 genuinely wanted ~2× what
    # 8M allowed. Local A/B (SSIM vs clean master) showed the ceiling bump alone cut
    # dark-region distortion ~31%. CRF still governs, so easy/bright clips stay small.
    output_video_bitrate: str = "16M"
    output_audio_bitrate: str = "192k"
    output_min_duration_s: float = 45.0
    output_max_duration_s: float = 59.0
    output_target_lufs: float = -14.0


settings = Settings()
