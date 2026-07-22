import json
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

    # CORS — comma-separated or JSON array in env:
    # ALLOWED_ORIGINS=https://usekria.com,http://localhost:3000
    # ALLOWED_ORIGINS='["https://usekria.com","http://localhost:3000"]'
    allowed_origins: list[str] = ["http://localhost:3000"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: object) -> object:
        """Support both JSON-array and comma-separated ALLOWED_ORIGINS values."""
        if not isinstance(v, str):
            return v
        raw = v.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return v
        return [part.strip() for part in raw.split(",") if part.strip()]

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

    # Kill switch for the subtitled single-clip talking-head archetype. When False,
    # a job whose plan declares edit_format="subtitled" falls back to montage (so an
    # unimplemented/rolled-back token never renders half a feature). When True, a
    # single talk-to-camera clip keeps its own audio, is transcribed (whisper-1 +
    # language hint, Turkish + English), and renders editable sentence-block captions
    # over a cached caption-free base. Default OFF until the render path + picker card
    # are wired and verified end-to-end; the frontend picker card gates on this too.
    subtitled_archetype_enabled: bool = False

    # Creator-style Smart Captions master gate.  This controls availability only;
    # per-video intent is persisted separately and creator preset assignment is
    # resolved server-side.  Availability also requires the subtitled base
    # renderer gate. Default OFF for controlled creator rollout.
    smart_captions_enabled: bool = False

    # Fleet-wide default Smart Captions preset for users WITHOUT a
    # creator_style_assignments row. Both must be set (e.g. "cigdem" + "v2")
    # or the default is off and rollout stays per-assignment. An existing row
    # always wins — enabled=false stays a per-creator opt-out. Rollback: unset
    # both (fly secrets unset) + machine restart; assigned creators keep v2.
    smart_captions_default_preset_id: str = ""
    smart_captions_default_preset_version: str = ""

    # The word→visual matching brain (nova.compose.scene_matcher): one Gemini
    # call per v2 render pairing pool assets with the exact spoken word and
    # tagging chapters/roles language-agnostically. Fail-open advisory — any
    # failure falls back to the deterministic vocab heuristics. Kill switch:
    # false = pre-agent behavior byte-identically.
    smart_scene_matcher_enabled: bool = True

    # Smarter captions (plans 011 + 012). Four flags: the two plan-011 kill
    # switches (emphasis, layout) default OFF so a merge is a no-op until each is
    # flipped Fly-first; the two plan-012 behavior flags (section heading,
    # transcript cache) default ON — they refine pre-existing behavior and act as
    # rollback levers rather than opt-in gates.
    #
    # EMPHASIS: gates the scene-matcher emphasis prompt block AND consumption of
    # emphasis_spans in the caption chunker — contextual words-per-cue ("number
    # one → Messi" shows Messi alone) plus emphasis-derived keep-together pairs.
    # Off ⇒ the model never sees the emphasis task and cue chunking is
    # byte-identical (the prompt block renders to ""); it is a real rollback, not
    # a consumption veto. Requires smart_scene_matcher_enabled to do anything.
    smart_caption_emphasis_cues_enabled: bool = False
    # LAYOUT: gates the deterministic line-layout additions in measure_caption —
    # the digit+word keep-together adjacency rule and the single-word widow
    # penalty. Fully deterministic (no LLM), so it is verifiable offline and can
    # ship ahead of the emphasis eval train. Off ⇒ line wrapping byte-identical
    # for cues with no persisted emphasis keep-together pairs; a job planned
    # while the emphasis flag was on keeps honoring its persisted pairs on reburn
    # regardless of THIS flag (that is the emphasis dimension, not the layout one).
    smart_caption_layout_balance_enabled: bool = False
    # SECTION HEADING: the "number N" + keyword overlay lane the scene matcher
    # emits for list items (plan 012 P1-3). Default ON (pre-existing behavior);
    # rollback lever if the section overlay crowds the caption band. Off ⇒ the
    # compiler emits no section number/keyword text elements; captions unaffected.
    smart_caption_section_heading_enabled: bool = True
    # TRANSCRIPT CACHE: content-addressed cache of the whisper transcript keyed by
    # clip content hash (plan 012 P1-4). whisper-1 is non-deterministic, so this
    # makes every re-render of the SAME clip reuse the identical word list —
    # killing the "two renders of one clip caption differently" symptom. Fully
    # fail-open (any GCS/hash error falls through to a live transcribe). Default
    # ON; set false to always re-transcribe.
    smart_caption_transcript_cache_enabled: bool = True
    # (Face-aware caption placement — smart_caption_face_placement_enabled — lands
    # with Feature C in its own PR.)

    # Kill switch for authored TextElements on subtitled variants. When False,
    # subtitled remains captions-only and the text-element routes/capabilities
    # reject it. When True, user-authored text is burned onto the caption-free
    # base before captions, so captions stay topmost.
    subtitled_text_lane_enabled: bool = False

    # Subtitled caption correction: after whisper, an LLM fixes each cue's spelling /
    # grammar / case-endings (whisper mishears Turkish morphology) while preserving cue
    # timing. Best-effort — a failure leaves cues untouched. Kill switch: set False to
    # burn the raw whisper transcript. gpt-4o is the default: gpt-4o-mini proved
    # UNRELIABLE on Turkish contextual grammar (missed 'nereye'->'nereyi' 4/4 runs; gpt-4o
    # fixed it 2/2). ~$0.005/render. Set to gpt-4o-mini to trade accuracy for cost.
    subtitled_caption_correction_enabled: bool = True
    caption_correction_model: str = "gpt-4o"

    # Self-narration for narrated walkthroughs: a NARRATED_EDIT_FORMATS item with NO
    # recorded voiceover may still generate when its footage carries the voice — the
    # clip audio IS the narration. Resolution: 1 clip with speech → subtitled (editable
    # captions from its own audio); 2+ clips → talking_head (highest-speech spine +
    # B-roll); no speech anywhere → montage fallback, reason persisted on
    # assembly_plan["archetype_fallback"] and surfaced on the item page. This flag is
    # the SOLE gate for the branch — it deliberately does NOT consult
    # edit_format_talking_head_enabled / subtitled_archetype_enabled (those gate
    # plan-DECLARED formats, not resolution outcomes) so rollback is one switch.
    # INCIDENT NOTE: if either assembler's own kill switch is flipped off because
    # that render path is broken, ALSO flip this flag off — self-narration would
    # otherwise keep routing narrated items into the disabled assembler.
    # Mirror flag NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED gates the frontend
    # Generate button; flip Fly first, then Vercel. Default OFF.
    narrated_self_narration_enabled: bool = False

    silence_cut_enabled: bool = Field(
        default=False,
        description="Automatic silence/filler cutting for speech render paths "
        "(plans/010): subtitled today, talking_head when T6 lands. When true, the "
        "clip's own audio is transcribed verbatim (whisper bias prompt), silences "
        "are detected, and dead air + filler vocalizations are cut inside the "
        "reframe (alternating punch-in jump cuts); captions are built from the "
        "remapped transcript minus filler tokens. Fail-open by design: any stage "
        "failure or safety-rail bailout renders today's uncut video. Per-item "
        "opt-out: POST /admin/jobs/{job_id}/silence-cut-disable (takes effect on "
        "the next FULL re-render only). "
        "Kill switch: `fly secrets set SILENCE_CUT_ENABLED=false --app nova-video` "
        "+ worker restart — byte-identical to pre-feature behavior.",
    )

    retake_cut_enabled: bool = Field(
        default=False,
        description="Retake/restart cutting inside the silence-cut stage "
        "(plans/010): the retake_detector agent flags abandoned takes and their "
        "spans merge into the same CutPlan. Independent of SILENCE_CUT_ENABLED "
        "(silence cutting ships and validates first); only meaningful when that "
        "flag is also on. Detector failure degrades to zero retake cuts "
        "(`retake_detector_failed` event) — never blocks silence/filler cutting. "
        "Kill switch: `fly secrets set RETAKE_CUT_ENABLED=false --app nova-video` "
        "+ worker restart.",
    )

    # "Get a transcript" helper for narrated-walkthrough voiceovers. When False,
    # the transcript routes (POST/GET /plan-items/{id}/transcript/*) return 404 and
    # the frontend entry link is hidden (mirror flag NEXT_PUBLIC_TRANSCRIPT_HELPER_ENABLED
    # in Vercel — keep Fly + Vercel in sync). Default OFF until the flow ships behind
    # the flag. Gates only the optional helper; the record/upload voiceover bar is
    # unaffected. See plans/on-the-narrated-walkthrough-*.md.
    transcript_helper_enabled: bool = False

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

    # Heavy-source downscale guard (2026-07-21 OOM incident, job e8173a25):
    # a 170MB high-bitrate clip OOM-killed the worker mid-reframe. SDR sources
    # whose SHORT edge exceeds `source_downscale_short_edge_max` are re-encoded
    # once at ingest (bounded decoder threads, h264, never upscaled past the
    # 1080x1920 cover scale) so every downstream per-slot reframe decodes a
    # bounded intermediate instead of native 4K HEVC. HDR sources are excluded —
    # the existing `_pretonemap_hdr_clips` pass already downscales those inside
    # its zscale chain. Kill switch: SOURCE_DOWNSCALE_GUARD_ENABLED=false +
    # worker restart → byte-identical to pre-guard behavior.
    source_downscale_guard_enabled: bool = True
    source_downscale_short_edge_max: int = 1920
    # Decoder/encoder thread cap for the guard's own ffmpeg pass. Frame-threaded
    # HEVC decode memory scales with thread count — the whole point of the guard
    # is bounding peak RSS, so its own decode must not spike either.
    source_downscale_ffmpeg_threads: int = 2

    # Celery prefork child recycling (same incident): analyze_pool_asset bursts
    # leave CLIP/torch/Whisper residency in the single long-lived child, which
    # then stacks under the next render's ffmpeg peak. When a child's RSS
    # exceeds this many KB after a task completes, Celery replaces it (fork from
    # the parent keeps the prewarmed CLIP singleton via copy-on-write, so the
    # replacement is cheap). 0 disables. Measured in KB (Celery convention).
    worker_max_memory_per_child_kb: int = 3_145_728  # 3GB

    # Render heartbeat (same incident, user-visible half): the orchestrator
    # ticks jobs.worker_heartbeat_at every `interval` seconds; the status route
    # reports `retrying: true` while a non-terminal job's heartbeat is older
    # than `stale_after` (worker died silently; acks_late redelivery pending).
    # stale_after = 5 missed beats — tolerates one slow/failed DB write without
    # flapping the UI.
    render_heartbeat_interval_s: int = 30
    render_heartbeat_stale_after_s: int = 150

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

    # Text-behind-subject occlusion. When True, generative-edit overlays the
    # OverlayFormatMatcherAgent (or a user-authored TextElement) flags with
    # `behind_subject: True` are burned with a per-frame person-segmentation
    # matte (`app.pipeline.subject_matte`) so a moving subject occludes the
    # text instead of sitting on top of it. Default OFF: no matte is ever
    # computed, no burn dict carries `behind_subject`, and no extra GCS
    # object is written — `_resolve_intro_overlay_params` (generative_build.py)
    # is the single chokepoint that ANDs the resolved decision with this flag,
    # so flipping it off mid-flight degrades every in-flight job to plain text
    # instead of failing it. Flip to False on Fly
    # (`fly secrets set TEXT_BEHIND_SUBJECT_ENABLED=false --app nova-video`
    # then restart workers) to disable instantly.
    text_behind_subject_enabled: bool = False

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

    lyrics_optional_enabled: bool = Field(
        default=False,
        description="Lyrics stop being baked into song_lyrics renders: the variant "
        "renders lyrics-free (clean base, like song_text) and stamps "
        "lyrics_baked=False + lyrics_enabled=False. The editor's Lyrics toggle "
        "(default OFF) then instantly materializes beat-synced lyric lines as "
        "ordinary editable `role=lyric_line` TextElements via GET "
        ".../lyric-seeds; saving burns them through the normal fast text "
        "reburn. Read at render time inside _render_generative_variant, so "
        "flipping it affects queued jobs and re-renders after a worker "
        "restart. Off = byte-identical current (bake-in) behavior; legacy "
        "variants (lyrics_baked absent) always keep baked behavior regardless "
        "of this flag. Kill switch: `fly secrets set "
        "LYRICS_OPTIONAL_ENABLED=false --app nova-video` + "
        "`fly machine restart <id>` — no deploy needed.",
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

    # Chat-based full-editor copilot. When True,
    # POST /plan-items/{item}/variants/{variant}/copilot/turn is live and returns
    # proposed draft edit ops; it never writes Job/PlanItem rows. Frontend twin:
    # NEXT_PUBLIC_EDIT_COPILOT_ENABLED gates the Nova drawer. Default off until
    # localhost QA validates the local-op applier and save parity.
    edit_copilot_enabled: bool = False

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
    # Alpha-preserving image overlays for the media-overlay lane. When False,
    # image cards still flatten through JPEG exactly as before.
    media_overlay_alpha_enabled: bool = False

    # First-class visual replacement blocks (montage + interstitial text cards).
    # Blocks render below authored text/captions and are additive: with the flag
    # off, no existing render path or editor capability changes.
    visual_blocks_enabled: bool = False
    # Separate quality gate for zero-click visual-treatment planning. Manual
    # authoring can launch first while planner evals are still running.
    visual_block_autoplan_enabled: bool = False

    # Sound-effects glossary + user placement (PR-1 foundation). Admin-curated
    # SFX + user uploads placed at arbitrary timestamps in a plan-item variant.
    # Kill switch: SOUND_EFFECTS_ENABLED=false → sfx-upload-urls + sound-effects
    # routes return 404; the render apply-pass branch never fires.
    sound_effects_enabled: bool = False

    # Smart Captions v2 licensed music bed. Deliberately independent of
    # SOUND_EFFECTS_ENABLED (that flag gates the user SFX lane; the music bed
    # is agent-selected). Kill switch: SMART_MUSIC_BED_ENABLED=false → new
    # renders resolve no music treatment and reburns skip re-mixing the bed
    # (persisted treatments are preserved for re-enable, never deleted).
    # Apply: fly secrets set SMART_MUSIC_BED_ENABLED=false --app nova-video
    # + machine restart (worker).
    smart_music_bed_enabled: bool = True

    # Overlay auto-placement (plan 005, PR0+). Gates the plan-item asset-pool
    # routes (upload-urls / register / list / delete) and, in later PRs, the
    # matcher + suggestion routes. Frontend twin: NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED
    # in Vercel — keep in sync (dual-flag trap, see CLAUDE.md).
    # Kill switch: OVERLAY_AUTOPLACE_ENABLED=false → all pool/suggestion routes 404.
    overlay_autoplace_enabled: bool = False

    # Queue for the light autoplace tasks (analysis + matcher — LLM calls, no
    # ffmpeg). Default "celery" (the default queue) in prod; local dev sets a
    # DEDICATED queue (e.g. "autoplace-jobs") because sibling worktree workers
    # share one redis and would grab tasks they don't have registered.
    autoplace_queue: str = "celery"

    # Zero-click auto-apply (plan 007, decision D2-B + G3-A): after a plan-item
    # generate render finalizes, matched visuals are burned in WITHOUT review on
    # speech-bearing variants. Dedicated kill switch — turning THIS off in prod
    # never kills manual suggest (OVERLAY_AUTOPLACE_ENABLED). When
    # MEDIA_OVERLAYS_ENABLED is off, auto-apply degrades to suggest-only + trace.
    overlay_autoapply_enabled: bool = False

    # SFX auto-suggestions (word-level sound design): after a plan-item render
    # finalizes, an agent proposes sound-effect placements anchored to spoken
    # words / pauses / clip moments, persisted as ADVISORY
    # `pending_sfx_suggestions` on the variant (stale-filtered on read; nothing
    # renders until the user or the copilot realizes one as an ordinary
    # placement). Suggest-only by design — there is deliberately no autoapply
    # twin (cut at plan review: SFX are taste-heavy, catalog is thin).
    # Kill switch: SFX_AUTOPLACE_ENABLED=false → chain never dispatches.
    sfx_autoplace_enabled: bool = False

    # Full-screen cutaway takeovers (plan 009). Gates ONLY the AI suggestion
    # branch (build_suggestions slot "full" → display_mode="fullscreen"); the
    # manual popover toggle is ungated, and render rollback is covered by
    # MEDIA_OVERLAYS_ENABLED. Default False per the family rollout pattern
    # (eng review E2): flip on Fly only AFTER the T4/T5 web surfaces are live
    # on Vercel — otherwise auto-applied takeovers preview as small pip tiles
    # (the #296/#297 parity incident class).
    fullscreen_cutaways_enabled: bool = False

    # Per-item "Ask Kria" advisor (plan dogfood feedback #2): conversational,
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
