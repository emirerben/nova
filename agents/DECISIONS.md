# Nova — Technical Decisions

> Key decisions logged here until ARCHITECTURE.md is written. Format: date, decision, why, revisit trigger.

---

## [2026-03-22] Monorepo over separate repos

**Decision:** Single repo `emirerben/nova` with `apps/web` + `apps/api`
**Why:** Frontend and backend API contract will change constantly early on. Monorepo eliminates cross-repo PRs for contract changes. Two-person team doesn't need the isolation overhead.
**Revisit if:** API needs to be licensed/distributed separately, or team grows >5 engineers.

---

## [2026-03-22] FastAPI over Flask

**Decision:** FastAPI for the Python backend
**Why:** Async-native (important for job status streaming), auto-generates OpenAPI docs (aids frontend/agent integration), faster than Flask for our use cases.
**Revisit if:** team has strong Flask expertise or a library we need is Flask-only.

---

## [2026-03-22] FFmpeg subprocess over MoviePy

**Decision:** FFmpeg via `subprocess.run()` directly, not MoviePy
**Why:** MoviePy's `VideoFileClip` buffers the entire video into RAM. A 2GB source video = OOM crash. FFmpeg streams. Existing `~/src/vid-to-audio/` project is the cautionary example.
**Revisit if:** never. This is a permanent constraint.

---

## [2026-03-22] GitHub under emirerben (personal)

**Decision:** Repos live at `github.com/emirerben/nova` and `github.com/emirerben/nova-workspace`
**Why:** Fastest setup, no new org to create. ybyesilyurt is collaborator.
**Revisit if:** Nova incorporates, or we add a third engineer.

---

## [2026-03-27] Interstitials as separate clips, not xfade parameters

**Decision:** Render interstitials (curtain-close, black hold, white flash) as standalone video clips inserted between template slots, rather than encoding them as xfade transition parameters.
**Why:** xfade can only blend two adjacent clips. Curtain-close is a three-phase effect (bars closing, hold, next clip) that needs its own timeline segment. Separate clips also make beat-snap accounting explicit (cumulative_s tracks total duration).
**Revisit if:** FFmpeg adds native curtain-close xfade type, or performance requires fewer concat segments.

---

## [2026-03-27] Playfair Display over Montserrat for editorial overlays

**Decision:** Bundle Playfair Display (Bold + Regular) as the primary editorial font. Montserrat retained for font-cycle contrast.
**Why:** Playfair's serif forms are more readable at mobile text sizes and signal editorial quality. Sans/serif contrast during font-cycle adds visual variety. ASS subtitle filter uses `fontsdir` to discover bundled .ttf files.
**Revisit if:** user testing shows readability issues on specific devices, or font-cycle contrast feels jarring.

---

## [2026-03-27] geq pixel expression over drawbox for curtain-close animation

**Decision:** Use FFmpeg `geq` filter to animate curtain-close bars, not `drawbox`.
**Why:** drawbox's `h`, `w`, `x`, `y` parameters are static expressions that do NOT have access to the `t` (timestamp) variable. Only drawbox's `enable` expression can reference `t`, but that only toggles visibility, it cannot animate bar height over time. `geq` evaluates per-pixel per-frame with full access to `T` (timestamp), `X`, `Y`, `H`, `W`. Performance cost is mitigated by splitting the clip: stream-copy the prefix, geq-process only the short tail segment.
**Revisit if:** FFmpeg adds timestamp support to drawbox geometry expressions, or a lighter filter gains per-frame expression evaluation.

---

## [2026-03-27] Font-cycle timing: separate multi-PNG timestamps from overlay timestamps

**Decision:** Font-cycle overlays generate their own `start_s`/`end_s` timestamps per PNG frame. `_burn_text_overlays()` must not overwrite these with the parent overlay's single timestamp.
**Why:** A font-cycle overlay produces N PNGs with precise per-frame timing (e.g., 0.15s intervals). The previous code had a 1:1 timing reassignment loop that overwrote all font-cycle timestamps with the single overlay's `start_s`/`end_s`, collapsing every frame to the same time window. This caused all font-cycle PNGs to display simultaneously instead of sequentially.
**Revisit if:** overlay rendering is refactored to a single-pass approach (e.g., ASS-only rendering for font-cycle).

---

## [2026-03-28] Fly.io over Cloud Run / Railway for API + Worker deploy

**Decision:** Deploy API and Celery worker as separate Fly.io process groups in a single app, with Alembic migrations as a release_command.
**Why:** Fly.io natively supports multi-process apps (api + worker) with per-group VM sizing. Worker needs 2048MB for FFmpeg; API only needs 512MB. `release_command` runs migrations before any new code starts serving traffic. Cloud Run would require two separate services with separate deploy pipelines. Railway lacks per-process memory controls.
**Revisit if:** worker FFmpeg memory usage exceeds 2GB (bump vm sizing), or we need GPU transcoding (move worker to a GPU-capable platform).

---

## [2026-04-11] overlay-constants.ts: pure logic extracted from editor components

**Decision:** All canvas math, position maps, font maps, and helper functions (`getEffectiveTiming`, `isOverlayVisible`, `snapToNearestZone`, `computeBarPosition`) live in `overlay-constants.ts`, separate from React components.
**Why:** Makes the mapping layer independently testable and keeps components free of coordinate arithmetic. Constants must stay in sync with `app/pipeline/text_overlay.py` (same pixel values: CANVAS_W=1080, POSITION_Y_MAP, FONT_SIZE_MAP).
**Revisit if:** backend constants change — frontend map must be updated to match.

---

## [2026-03-27] Gemini vocabulary translation layer

**Decision:** Map Gemini's human-friendly transition names (whip-pan, zoom-in, dissolve) to internal FFmpeg xfade types via `translate_transition()`, rather than constraining Gemini's output vocabulary.
**Why:** Gemini produces better creative direction when using natural film terminology. The translation layer is 10 lines and easy to extend. Unknown types default to "none" (hard-cut) for safety.
**Revisit if:** the vocabulary mapping grows beyond 20 entries, or Gemini starts generating types that don't map cleanly.

---

## [2026-04-17] yt-dlp subprocess over yt-dlp Python API for audio download

**Decision:** Invoke yt-dlp as a subprocess (`subprocess.run(['yt-dlp', ...])`) rather than importing the yt-dlp Python API directly.
**Why:** Consistent with the FFmpeg subprocess pattern — keeps RAM usage flat regardless of source file size. The Python API has different release cadence from the CLI binary, which can cause breakage when YouTube/SoundCloud change their protocols. Subprocess always uses the installed binary, which is simpler to update.
**Revisit if:** yt-dlp Python API stabilizes and we need programmatic progress reporting.

---

## [2026-04-17] FFmpeg energy-peak beat detection over librosa

**Decision:** Use FFmpeg `silencedetect`/`astats` for beat detection rather than librosa.
**Why:** No additional dependency (~50MB numpy/scipy stack). FFmpeg is already required; adding a pure-subprocess beat detector keeps the Docker image lean. Quality is sufficient for cut-on-beat videos (energy transients = drum hits, bass drops). librosa is logged in TODOS.md as a P3 upgrade path if this proves insufficient.
**Revisit if:** users report poor beat alignment on melodic-only tracks (no percussion). Librosa onset detection handles these cases better.

---

## Pipeline incident archaeology (extracted from CLAUDE.md for size)

These are the "why" behind invariants stated tersely in CLAUDE.md's pipeline sections. CLAUDE.md keeps the rule + guard test; the narrative lives here.

### [2026-05-18] Single-pass CFR-before-xfade invariant
**Incident:** prod job `856daa32-…` on the BAD BUNNY music template aborted with `current rate of 1/0 is invalid`. The per-clip chain head `framerate=fps=N` interpolates against source PTS and silently fails on inputs reporting `avg_frame_rate=1/0` ("unknown rate" — some phone HEVC, HEIF-derived video, screen recordings); xfade then rejects the stream.
**Fix:** every per-clip chain in `single_pass.py` (`_per_clip_filter_chain`) must end with `fps={output_fps}, setpts=PTS-STARTPTS, settb=AVTB` — `fps=` drops/duplicates frames independent of PTS coherence, so it works where `framerate=` can't. Locked by `test_per_clip_chain_forces_cfr_before_xfade`.

### [2026-05] Renderer-parity invariant (#296/#297 class — "looks right locally, clips in prod")
**Incident:** the admin overlay-preview + classic templates render via Pillow, while agentic + music jobs render via Skia. #296 carried `text_anchor` through the burn dict and fixed Pillow + preview, but Skia's `_draw_centered_text`/`_draw_karaoke_line` kept centering every line on `position_x_frac`; prod job `ff0d2e1c` still rendered `text_anchor="left"` overlays clipped ("It's not just luck" → "s not just luck"). #297 fixed `_draw_centered_text`/`_draw_pop_in_with_suffix`; the karaoke path was fixed later.
**Rule:** any overlay field plumbed through the burn dict MUST be honored by BOTH renderers. Guard: `test_both_renderers_honor_text_anchor_left`. Agentic/music overlay changes are NOT verified by the admin preview — verify the burned Skia video (`make verify-overlays`).

### [2026-05-13] Gemini metadata never becomes on-screen overlay text
**Incident:** job `a1091488` (Rule of Thirds) rendered "pilot in cockpit" — Gemini's `detected_subject` from a cockpit clip — in place of "The"/"Thirds".
**Fix:** removed the `_consensus_subject(clip_metas)` fallback and the empty-hook `clip_meta.hook_text` fallback. Overlay substitution input is now exclusively user-provided (`inputs.location`). Sentinel: `TestNoGeminiTextLeaks`. Does NOT cover `copy_writer` (captions are a separate trust surface).

### [2026-05] Dockerfile / .dockerignore coupling (PR #118/#119)
**Incident:** PR #118 (`f156c19`, v0.4.7.0) added `COPY src/apps/api/tests/evals/rubrics ./tests/evals/rubrics` for the Loop B online judge. Local pytest + the `test-api` CI job both passed (they run against the source tree, not the image). The deploy then failed on Fly with "not found in build context" because `.dockerignore` excluded the source path — the two files don't track each other and nothing local flags the mismatch. PR #119 (`121a6e9`) hotfixed `.dockerignore` with negation patterns to let the rubrics through.
**Rule:** any new `COPY <src> ...` in the prod `Dockerfile` must have its source path verified against `.dockerignore`. `.github/workflows/docker-build.yml` now runs `docker build` against the prod Dockerfile on every PR touching `Dockerfile`, `.dockerignore`, or `src/apps/api/**`, so this class of bug fails on the PR, not on merge-to-main.

### Encoder-policy preset history
- PR #102: curtain-close → `medium`.
- PR #105: curtain-close → `fast` + `--concurrency=1` + PNG-overlay.
- Brazil pixelation fix: propagated `fast` to the three `template_orchestrate.py` final-output sites the prior PRs missed (`ultrafast` disables `mb-tree`/`psy-rd`/B-frames/trellis → visible 16×16 macroblocking on smooth gradients; CRF does not compensate). Locked by `tests/test_encoder_policy.py`.

---

## Storage retention incidents (extracted from CLAUDE.md for size)

### Generative re-signing invariant (added 2026-06)
**Rule:** `generative-jobs/*` blobs persist forever (NOT in the 24h delete rule) but
`upload_public_read` still signs `output_url` for only 1 day. A content-plan /
generative item viewed >24h after render reads "ready" but its stored signature is
expired → GCS 400 `ExpiredToken` → empty `<video>`.

**Fix:** read-time re-signing, NOT a TTL bump (a longer global TTL would make
`dev-user/`/`music-jobs/` URLs outlive their 1-day-deleted blobs):
`GET /generative-jobs/{id}/status` re-signs each ready variant's `output_url` from
the persisted `video_path` key via `_variants_for_response` (`routes/generative_jobs.py`,
`PLAYBACK_URL_TTL_MIN`). The raw `_variants_of` stays unsigned for the mutate paths so
a short-lived URL never lands back in the DB.

Pinned by `test_variants_for_response_resigns_ready_variant` in
`tests/routes/test_generative_jobs.py`. Admin debug/list views still show stored (stale)
URLs — follow-up.

### [2026-08-31] Posters moved off the video prefix — a thumbnail must outlive its source (v0.59.1.0)

**Incident:** the mobile Library showed older videos stuck behind a permanent
spinner. Two independent causes, both storage-shaped.

1. Posters were written as a sibling of the video (`<source>.poster.jpg`), so they
   inherited the source's lifecycle rule. A music-mode poster died at 24h with
   `music-jobs/*`, a template-mode poster at 30d with `jobs/*` — days after the
   video was made, the tile went blank with no way back.
2. Videos rendered before the poster feature existed never had a poster at all,
   and nothing in the product could mint one after the fact. The batch **Video
   Poster Backfill** workflow was the only actuator, and it had never been
   dispatched (zero runs as of 2026-08-30).

**Decision:** posters live on their own job-scoped, lifecycle-exempt prefix,
`job-posters/{job_id}/{sha1(source)}.poster.jpg`, named once in
`services/job_storage_paths.JOB_POSTER_PATH_PREFIX` so cleanup paths can bound a
delete to it without importing the ffmpeg-heavy poster module. The prefix matches
no rule in `infra/gcs-lifecycle.json`, so the thumbnail can no longer be deleted
by the retention rule of the video it was extracted from. Legacy sibling keys stay
readable; the key stored on the Job row is authoritative. Writers:
`services/template_poster.py` plus every render path that mints a poster —
`tasks/generative_build.py` (the main Library path), the template, music, and
auto-music orchestrators, `scripts/backfill_video_posters.py`, and
`tasks/poster_repair.py`.

**Actuator:** `app/tasks/poster_repair.py` (`tasks.repair_job_poster`), enqueued
by `POST /me/jobs/posters/refresh` when an owned ready job's preview has no
poster. The endpoint stops being a pure re-signer only under
`POSTER_ONDEMAND_REPAIR_ENABLED` (default false) — already-deployed frontends
poll it in bursts, so a default-true ship would have been a zero-canary
activation. Off is byte-identical to the pre-feature endpoint: no marker writes,
no dispatch, and an already-queued task re-checks the flag and no-ops.

**Queue ordering is the operational trap.** The task downloads the full source MP4
into the RAM-backed `/tmp` and runs ffmpeg — the exact workload that OOM'd the 1GB
`light`/Beat machine on 2026-08-02. So `repair_job_poster` is deliberately NOT in
`MAINTENANCE_TASK_NAMES`, and `POSTER_REPAIR_QUEUE` must be set to
`autoplace-jobs` (2GB) **before** the flag is enabled. The route is pinned in
`worker.py`'s `task_routes` rather than passed at each dispatch, so a future bare
`repair_job_poster.delay(...)` cannot silently land on the default `celery` queue
and head-of-line-block the concurrency=1 render worker. The code default stays
`celery` because `dev-auto.sh` consumes no autoplace queue.

**Known consequence (tracked in TODOS.md):** a poster now outlives its source for
music/template modes, so a tile older than its source's retention window shows a
real thumbnail for an MP4 that GCS already deleted. Playback fails cleanly; the
retention question ("what does *your videos* promise?") is a product decision, not
a local fix.

Guards: `tests/services/test_job_storage_paths.py`,
`tests/tasks/test_poster_repair.py`, `tests/routes/test_me_jobs.py`,
`tests/test_template_poster.py`, `tests/tasks/test_task_time_limits.py`.
Runbook: `docs/runbooks/video-poster-backfill.md`.

---

## Celery time-limit invariant (extracted from CLAUDE.md for size)

### [2026-06-01] Duplicate-worker space exhaustion (prod job 08532ba3)
**Incident:** generative voiceover job `08532ba3` at `time_limit=2000` vs broker
`visibility_timeout=1900s`. With `task_acks_late=True`, a task still in-flight past
visibility_timeout is redelivered to a SECOND worker while the first runs — duplicate
concurrent execution. Two workers both writing to the RAM-backed `/tmp` (tmpfs) →
`No space left on device` mid HDR pre-tonemap.

**Rule:** every long-running task's `time_limit` MUST stay strictly under the worker's
broker `visibility_timeout` (`app/worker.py`, currently 1900s). Render orchestrators
use `soft_time_limit=1740, time_limit=1800`. `batch_import_from_drive` (2400) is the
deliberate exception (download-bound, separate handling).

Locked by `tests/tasks/test_task_time_limits.py`.

---

## Stuck-variant reaper: a jsonpath that never parsed (v0.69.1.0, 2026-09-07)

`reconcile_stuck_variants` builds a Postgres jsonpath to find variants frozen
mid-render. One of its two paths was:

    $.variants[*] ? (@.render_status == "rendering" && @.editor_render_attempt)

In jsonpath, operands of `&&`/`||` must be PREDICATES, not accessors. A bare
`@.editor_render_attempt` makes the parser fail at the `)`:

    syntax error at or near ")" of jsonpath input

Postgres parses jsonpath at EXECUTION time, so this was invisible to import,
to ruff, and to every test — `tests/tasks/test_reaper.py` mocks `sync_session`
wholesale. `app/worker.py` catches the `worker_ready` reconcile as non-fatal,
so every worker logged the ProgrammingError and moved on. Both jsonpaths are
OR'd into one discovery query, so the malformed branch took the valid one down
with it: the entire sweep was a silent no-op from #962 until v0.69.1.0.
Confirmed on the autoplace machine and both speech_analysis machines 2026-09-06.

Fix: `exists (@.editor_render_attempt)`. Both paths are now bound as SQL
parameters cast to `jsonpath` rather than spliced through `literal_column` —
splicing forced the quoting and the `::jsonpath` cast to be hand-managed inside
a Python string literal, which is how it shipped unnoticed.

**The reactivation was the bigger risk.** Turning the sweep back on exposed
correctness bugs in a write path that had been dead for months:

1. `_finalize_stuck_variant` flipped a stuck variant with a `video_path` to
   `ready` unless it carried an editor lease. Only editor Saves take a lease
   (`_stamp_editor_render_attempt`); swap-song, retext, caption reburn and
   style change use `stamp_variant_attempt`, which bumps `render_started_at`
   and LEAVES the previous `video_path`. So a dead re-render was reported as a
   finished edit, serving the PRE-EDIT video. Now keyed on
   `_replacement_render_in_flight` (`render_started_at` vs `render_finished_at`),
   which needs no schema change. A variant that never recorded a start is
   genuine legacy status drift and still flips to `ready`.
2. Status promotion to `variants_ready_partial` fired only for the editor-lease
   case, so a generic-stale variant went `failed` under a `variants_ready`
   parent — and `_prepare_partial_variant_retry` 409s on exactly that, while the
   UI still offers Retry. Now fires for ANY failed variant.
3. That write was unguarded on current status and could move a job OUT of
   `variants_failed` into the ready bucket. Now guarded on
   `{done, variants_ready}`, matching the sibling writer in `generative_build`.

Also: Postgres lax mode auto-wraps a non-array, so `$.variants[*]` matches a
JSON-OBJECT `variants`. The reconcile loop would then iterate the dict's KEYS
and persist `variants: ["render_status", ...]`, destroying the plan. Guarded
with `isinstance(variants, list)` in both reaper loops.

Migration 0099 adds a sparse `(updated_at, id)` index with the sweep's jsonpath
as its predicate; without it the reactivated query seq-scans `jobs` on every
`worker_ready`. Verified with EXPLAIN: the planner absorbs the jsonpath into the
index predicate and the ORDER BY short-circuits the LIMIT.

**Lesson:** a caught-and-logged exception on a background sweep is indistinguishable
from "nothing to do". Deferred follow-ups (broker inspect inside the row lock,
LIMIT-50 head starvation) are in TODOS.md.

## Kill-switch incidents (extracted from CLAUDE.md for size)

### LYRIC_DYNAMIC_CROSSFADE_ENABLED — WARNING on disabling
**Background:** Defaults to `true`. Set to `false` to fall back to legacy
`_inject_line` scheduler behavior byte-identically.

**WARNING:** disabling this flag re-introduces the stacked-text bug observed in prod
jobs `5a71226e` and `e72d52e9` (Mirea track) — the legacy timing math is precisely
what produced the bug. Use ONLY for emergency rollback (e.g., the new path itself ships
a regression), and re-enable as soon as the regression is patched. Do NOT leave the
kill switch off as a long-term mode.

Kill-switch byte-identical test:
`tests/pipeline/test_lyric_injector_no_stacking.py::test_kill_switch_disabled_reproduces_pre_fix_output`

Apply: `fly secrets set LYRIC_DYNAMIC_CROSSFADE_ENABLED=false --app nova-video` then
`fly machine restart <id>` on the worker process group.

### FULLSCREEN_CUTAWAYS_ENABLED — the version-skew trap (review C8, plans/009)

**Background:** `FULLSCREEN_CUTAWAYS_ENABLED` (Fly, default false) gates the AI
fullscreen branch. Its Vercel twin `NEXT_PUBLIC_FULLSCREEN_CUTAWAYS_ENABLED`
gates something different: the MANUAL popover promote affordances.

**Why the order matters:** a new web build talking to an OLD api bakes a manual
fullscreen request as a pip — the api's `extra="ignore"` silently strips
`display_mode` off the payload, so the user asks for fullscreen and gets a pip
card with no error. Keep the Vercel twin FALSE until the Fly deploy is live:
**Fly first, then Vercel.**

Rule (g) fails closed without ANALYSIS_VERSION-3 pixel dims; the backfill
re-analyzes on first match. Render-side rollback is `MEDIA_OVERLAYS_ENABLED`.
Guards: `tests/test_overlay_fullscreen_rules.py` (kill-switch byte-identical
off) and the dual preset pins in `tests/test_media_overlay_command.py`
(fullscreen pass = `preset=fast` + shared 1500s budget + one-shot veryfast
retry).

### POSTER_ONDEMAND_REPAIR_ENABLED / POSTER_REPAIR_QUEUE — set the queue first

Full narrative in "Storage retention incidents" above. The one-line rule: the
repair task downloads a full MP4 and runs ffmpeg, so `POSTER_REPAIR_QUEUE` must
be `autoplace-jobs` (2GB) **before** `POSTER_ONDEMAND_REPAIR_ENABLED` flips —
enabling the flag while the queue still points at `celery` puts full-MP4
downloads on the concurrency=1 render worker.

---

## [2026-06-07] Style-chat 500 + false "Done": validator divergence incident

**Symptom (prod):** user typed "I like using small texts…" → 500 "Something went wrong".
Retry succeeded with "Done" but stored nothing size-related.

**Root cause 1 — 500:** Two Pydantic validators for `text_size_px` disagreed.
`StyleKnobs._clamp_px` silently clamps any value to `[40, 80]` (passes), but
`StyleKnobsEdit(ge=40)` was constructed from the **raw** agent value (e.g. 14) and
raised `ValidationError` outside any `try/except` → unhandled HTTP 500.
The model emits CSS-scale px (12/14) because the prompt never stated the legal range
or that these are 1080×1920 video-overlay px (not CSS px).

**Root cause 2 — false "Done":** Retry returned `style_edit` with only `free_text` (no
structured knob). Route called `_apply_style_edit` anyway, wrote only `status="edited"`,
set `applied=True` → "Done — your next render will use this style." with nothing stored.

**Root cause 3 — no read-back:** `_style_snapshot` exposed 3 of 10 knobs; the intent
taxonomy had no `describe` intent; the prompt steered "what is it set to?" to `unknown`.

**Fixes (PR #484):**
- Route: build `StyleKnobsEdit` from the clamped `StyleKnobs` output, never from raw
  agent dict. Detect clamping → append honest note to reply.
- Route: materiality check before `_apply_style_edit` — free_text-only returns
  `applied=False` without writing.
- Prompt: document px range 40–80, semantic size map ("small"→40–48), and the rule
  that `style_edit` MUST carry a concrete field.
- Agent + route: add `describe` intent; expose all 10 knobs in `_style_snapshot`.
- Pinned by `test_agent_turn_style_edit_small_px_clamped_to_40`,
  `test_agent_turn_style_edit_free_text_only_no_write`,
  `test_agent_turn_describe_intent_no_write`,
  `test_validator_parity_text_size_px_boundary_values`.

**Design rule going forward:** whenever two Pydantic validators cover the same field with
different strictness (one clamps, one raises), always use the lenient validator's OUTPUT
to construct the strict model — never the raw input.

## Editorial sequence: never-overlap + composite stream (2026-06-13, PR pending)

The editorial "sequence" feature (transcript-synced + rhythm-mode kinetic typography) carries
three invariants worth knowing before touching `phrase_sequence.py` / `text_overlay_skia.py`:

- **Scenes never overlap.** The original design crossfaded scenes (0.25s overlap); frame-by-frame
  analysis of the user's reference video proved phrases must exit cleanly before the next enters
  (verified empty frames between phrases). `split_phrases` ends every scene 0.1s before the next
  (`SCENE_CLEAR_GAP_S`); a 7-case property test pins no-overlap. Do not reintroduce crossfade.
- **Demo==production golden.** `test_golden_demo_quote_reproduces_approved_scene_windows` pins the
  user-approved render's exact 9 scene windows (3-decimal equality) through
  `synthesize_phrase_timings`. If rhythm pacing math changes, that approval is void — re-render and
  re-approve before merging.
- **Sequence overlays burn as ONE composite PNG stream** (`_render_sequence_composite`): FFmpeg burn
  cost scales with INPUT COUNT (~6.5s/input on a 60s canvas), not frames or bytes — 80 per-block
  inputs took 525s and flirted with the 600s subprocess timeout; the composite is 11.3s. Unique
  frames render only at pops/fade ramps; holds are hard-linked. Never emit sequence blocks as
  separate FFmpeg inputs.

## [2026-07-11] Celery task failures are ACKED — no redelivery self-heal (plan 010, PR #627)

**Fact:** a Celery task that RAISES is acked even with `task_acks_late=True` —
`task_acks_on_failure_or_timeout` defaults True. Late-ack redelivery only covers worker
DEATH (`task_reject_on_worker_lost=True` in `app/worker.py`), not task failures.

**Where it bit (accepted, R3-B / OV-9 in plans/010-subtitled-sfx-overlay-lanes.md):**
during PR #627's rolling deploy, a caption save from an upgraded API can land on an old
worker that TypeErrors on the new task kwarg. The failed message is acked — no
redelivery. The variant sits "rendering" until the 60-min reaper (`app/tasks/reaper.py`)
converts it to a failed badge; the user recovers by re-tapping Apply. Judged a
one-deploy, minutes-wide window; a two-phase kwarg rollout was rejected as
over-engineering.

**Reusable rule:** adding a required kwarg to an existing Celery task always opens this
window. Either accept it consciously (document it + confirm the reaper backstop covers
the queue) or ship the kwarg with a server-side default the old worker tolerates.

## [2026-07-20] Smart Captions v2 review decisions (v0.11.0.0)

Internals: `docs/pipelines/smart-captions.md`. Four calls from the pre-merge review
worth keeping:

**Hook-caption suppression is deferred, not implemented.** The compiler sets
`hook_caption_suppression_eligible` when the preset's `hook_accumulation` layout says
`suppress_if_resolved` and enough hook visuals resolved, but `hook_caption_suppressed`
always stays false. Compile-time asset resolution is planning evidence only —
downloads, normalization, collision arbitration, and FFmpeg can all still fail later,
so suppressing speech captions there could ship a hook with neither captions nor
visuals. Suppression waits until both lanes share a transactional compositor that reads
the applied-media manifest; the persisted eligibility receipts show how often the
deferral actually matters. Follow-up tracked as TODOS.md T-SMART-COMP-1.

**Survivors vs manifest (review D4).** Persisted `media_overlays` after a Smart apply
pass = survivors with arbitration-resolved geometry PLUS download-failed cards with
their ORIGINAL payload (the next reburn retries them — a transient storage failure must
never permanently delete a creator's card). Arbitration-OMITTED cards are dropped: the
reburn path has no arbitration, so persisting them would resurrect the exact occlusion
the omission prevented. `media_overlays_applied_ids` is the separate record of what
actually reached the burned video. Guard:
`tests/smart_edit/test_v2_render_contract.py::test_media_overlay_persistence_keeps_failed_cards_drops_omitted`.

**Music-bed eligibility is a closed allowlist, not a filter.** A track reaches the v2
music bed only when an admin explicitly set `track_config.smart_captions_licensed =
true` (music Config tab toggle). Default is ineligible — licensing exposure from an
uncleared track landing in a creator's export outweighs bed coverage. Guard:
`test_music_eligibility_is_closed_and_requires_explicit_license`.

**`SMART_MUSIC_BED_ENABLED` is deliberately independent of `SOUND_EFFECTS_ENABLED`.**
The SFX lane is user-authored; the bed is agent-selected — an incident in one must be
killable without silencing the other. Off: new renders resolve no treatment and reburns
skip re-mixing the bed, but persisted `smart_music_treatment` state is never deleted,
so re-enabling restores creators' saved mixes (same preserve-on-rollback rule the SFX
lane follows).

## [2026-07-21] Worker OOM mid-reframe + 30-min silent redelivery gap (prod job e8173a25)

**Incident:** a 170MB / 134s high-bitrate clip OOM-killed worker 6e826515c714e8 during
`reframe_and_export` (last log `reframe_filter_chain` 18:26:17Z, silent death, fresh
Celery boot 18:31:41Z). Compounding: 7 `analyze_pool_asset` tasks had just run on the
same worker, leaving CLIP/torch/Whisper residency in the single long-lived prefork
child (concurrency=1) for the ffmpeg peak to stack on. acks_late +
visibility_timeout=1900s redelivered at 18:56:57Z and attempt 2 finished cleanly — so
recovery worked, but the user stared at healthy-looking "rendering" for 30+ minutes.

**Three-part fix (this entry is the narrative; invariants live in the guard tests):**

1. **Heavy-source downscale guard** — `app/pipeline/source_guard.py`, wired into
   `_ingest_clips` (generative_build.py). SDR sources with short edge > 1920px are
   re-encoded ONCE at ingest (2-thread decode cap, h264 crf16/fast, cover-scale of
   1080x1920, never upscaled, audio stream-copied, original deleted from tmpfs), so
   every downstream per-slot reframe decodes a bounded intermediate instead of native
   4K HEVC — and Gemini uploads shrink too. HDR is excluded: `_pretonemap_hdr_clips`
   already downscales HDR inside its zscale chain, and an 8-bit re-encode here would
   destroy its input. Still images excluded (image_clip owns those). Kill switch:
   `SOURCE_DOWNSCALE_GUARD_ENABLED=false` + worker restart (byte-identical off).
   Guards: `tests/pipeline/test_source_guard.py`. Template/music ingest paths NOT
   wired yet — follow-up if the class recurs there.

2. **Prefork child recycling** — `worker_max_memory_per_child` in `app/worker.py`
   (`WORKER_MAX_MEMORY_PER_CHILD_KB`, default 3GB, 0 disables). Recycles the child
   BETWEEN tasks once RSS exceeds the threshold; the replacement forks from the parent
   and keeps the prewarmed CLIP singleton via copy-on-write. A dedicated queue/machine
   for analysis tasks was rejected: concurrency=1 already serializes execution — the
   problem was residual memory, not co-execution. Guards:
   `tests/tasks/test_worker_memory_recycle.py` (conf carries the value; threshold
   stays under the fly.toml worker VM size).

3. **User-visible retrying state** — `jobs.worker_heartbeat_at` (migration 0068) is
   ticked ~30s by a daemon thread (`job_phases.job_heartbeat`, column-only UPDATE by
   design — it must never read-modify-write assembly_plan) wrapped around
   `orchestrate_generative_job`. The status route computes `retrying: true` at READ
   time when a processing/rendering job's beacon is older than
   `RENDER_HEARTBEAT_STALE_AFTER_S` (150s); the redelivered attempt's synchronous
   entry beat clears it immediately. ProgressTheater swaps the leave-note for honest
   recovery copy. NULL beacon never flags (legacy rows / non-heartbeating
   orchestrators). Guards: `tests/routes/test_generative_retrying.py`,
   `src/apps/web/src/__tests__/progress/retrying.test.tsx`.

**Env vars (not in CLAUDE.md — its 38k budget was full at the time):**
`SOURCE_DOWNSCALE_GUARD_ENABLED` / `SOURCE_DOWNSCALE_SHORT_EDGE_MAX` /
`SOURCE_DOWNSCALE_FFMPEG_THREADS`, `WORKER_MAX_MEMORY_PER_CHILD_KB`,
`RENDER_HEARTBEAT_INTERVAL_S` / `RENDER_HEARTBEAT_STALE_AFTER_S`. Apply on Fly:
`fly secrets set <VAR>=<val> --app nova-video` + `fly machine restart <id>` (worker).

### [2026-07-22] Pre-merge review hardening of the OOM fixes (same branch)

/review (7 specialists + red team + adversarial) confirmed the design and
forced these changes before merge — all shipped in the same PR:

- **Guard aggregate budget:** per-clip timeouts alone let 20 heavy clips ×
  serial re-encodes eat the orchestrator's soft_time_limit (the d30c61fe
  serial-preprocessing class). `_GUARD_TOTAL_BUDGET_S=900` now bounds the
  whole pass; overflow clips keep originals + trace event. Serial stays
  deliberate — parallel conversions would double the peak memory the guard
  exists to bound.
- **Guard coverage widened:** `_prepare_timeline_assembly` (timeline re-render
  decodes durable ORIGINALS — would have reproduced the incident verbatim) and
  the bed-level reburn now run the guard too. AAC-transcode retry when `-c:a
  copy` can't mux into .mp4 (PCM/.mov, Opus) — the silent-skip class. Failure
  branch now emits a pipeline-trace event + deletes the partial tmpfs file.
- **tmpfs orphan sweep:** a SIGKILL'd child's TemporaryDirectory survives on
  RAM-backed /tmp into the redelivered attempt (invisible to
  worker_max_memory_per_child — not process RSS). `task_prerun` →
  `app/pipeline/tmp_sweep.py` sweeps nova* dirs older than 1850s; the cutoff
  invariant (> every render time_limit 1800, ≤ visibility_timeout 1900) is
  pinned by `test_tmp_sweep_cutoff_stays_inside_redelivery_window`.
- **Heartbeat honesty:** beacon written with `func.now()` (DB clock — worker/API
  VM skew shifted the 150s window); `retrying` now has an UPPER bound
  (visibility_timeout + stale + 300s slack) because a hard-time_limit SIGKILL
  ACKS the message and no redelivery ever comes — past the window the reaper
  owns the row. Threshold floors at 2× beat interval (misconfig guard).
  Beats also refresh `updated_at` via the model's onupdate — documented as
  deliberate, not a leak.
- **worker_max_memory_per_child semantics:** billiard compares lifetime PEAK
  RSS (ru_maxrss), not current residency — one >3GB spike recycles the child
  even if freed. Deliberate, but validate in prod via billiard's "child
  process exceeding memory limit" log line; every-task recycling ⇒ raise it.
- **Frontend:** EditPayoff (onboarding) was a missed ProgressTheater call site
  — a dead first-render attempt showed "About 90 seconds" indefinitely; ETA
  label suppressed while retrying; recovery note is an aria-live status
  region; contradictory static reassurance lines gated off while retrying.

**Known accepted gaps (documented, not fixed):** variant re-render/reburn tasks
(`regenerate_generative_variant`, caption/bed reburns) do NOT heartbeat and run
while job.status is terminal, so a dead re-render attempt still shows
render_status="rendering" until the boot-time variant reconciler — a
variant-level beacon is future work. Template/music ingest paths still lack the
downscale guard (separate task chip). Guard conversions re-run on every
swap-song/retext regen (clip_metas re-analysis already does; cacheable later).

---

## [2026-07-26] Vendor the face-detection Haar cascade instead of trusting cv2's wheel data dir

**Decision:** Ship `haarcascade_frontalface_default.xml` in the repo/image at `src/apps/api/assets/cv/` and resolve it via `resolve_face_cascade_path()` (`app/pipeline/face_sampler_worker.py`), preferring the bundled asset over `cv2.data.haarcascades`.
**Why:** Prod job `1e768d5b-3c82-499e-9063-c25449562844` showed `worker_error: rc_1 ... Can't open file '/usr/local/lib/python3.11/site-packages/cv2/data/haarc...'` — face-aware caption/thumbnail placement silently fell back to preset placement (`reason=sampler_error`) on every prod job. Root cause: `pyproject.toml`'s documented collision (mediapipe pulls `opencv-contrib-python` alongside `opencv-python-headless`) means whichever `cv2` package wins dependency resolution in the built image, its `cv2.data.haarcascades` directory does not reliably ship the cascade XML — a wheel-packaging detail outside our control that only reproduces in the built image, not local dev. `_load_cascade()` also now checks `cascade.empty()` and raises `FaceCascadeLoadError` naming the resolved path instead of silently constructing an always-empty classifier (the class of failure that produced the opaque `rc_1` in the first place).
**Revisit if:** the opencv/mediapipe dependency collision gets a real fix upstream (e.g. mediapipe drops its own opencv pin) — at that point the vendored asset becomes a belt-and-braces fallback rather than the primary path, but keeping it costs ~1MB and removes a wheel-packaging dependency either way.

## [2026-07-27] Behind-subject glitch: RVM backbone + boundary resets + oscillation gate (prod job add80a9c)

Plan item 9f51eee2 (4-clip landscape beach montage, one behind_subject
element spanning all clips) rendered with visibly strobing occlusion. The
matte showed the selfie segmenter's confidence oscillating en masse every
~5–9 frames (mask area 7%↔63% — sand/rock read like skin), which the 3-frame
median can't suppress and BOTH sanity gates missed: presence never flipped
(mask never vanished; 4 flips @ 0.365/s under the AND-gate) and the median
IoU stayed 0.927 (~15 jump pairs hidden among 308 stable ones). Two more
mechanisms compounded it: the temporal median carried ~2 frames of the
previous clip's silhouette across every slot cut, and the 0.25s window pad
(7.5 frames) put mask_at on a half-frame offset where banker's rounding
repeats/skips mask indices every ~3 frames.

Decisions:
- **Swap the segmentation backbone to RobustVideoMatting** (onnxruntime CPU,
  recurrent) — measured on the exact failing footage: median adjacent-frame
  IoU 0.980 vs the flapping selfie-segmenter mask, 30fps on M-series CPU at
  downsample 0.25. Mediapipe stays as the automatic fallback +
  `MATTE_RVM_ENABLED=false` kill switch. RVM weights are **GPL-3.0**: used
  server-side only, never distributed to users, and our inference code is
  written fresh against onnxruntime — acceptable; revisit if we ever ship
  the model client-side.
- **Reset temporal state at known cuts** (`cut_boundaries_s` threaded from
  variant timelines / assembly plans into `compute_subject_matte`) instead of
  detecting scene cuts in the matte engine — the orchestrator already knows
  the exact slot joins.
- **Add the large-jump oscillation gate** (adjacent-pair IoU < 0.5 count +
  rate AND-gate, cut pairs excluded) so any backbone that oscillates falls
  back to text-in-front rather than shipping a strobing occlusion.
- **Version the matte cache key** (`.matte.v2.mp4`): old cached mattes may be
  glitching mattes the old gate accepted; suffix mismatch = cache miss +
  recompute + best-effort v1 delete. Chosen over sidecar re-validation (old
  sidecars lack the new stats — defaults would always pass).

Trade-off accepted: RVM mattes people, not arbitrary objects — the "behind
the rock" look on clip 1 of the beach montage was the broken model
hallucinating and is gone. Behind-arbitrary-objects would need a
salient-object/video-segmentation model (follow-up if ever wanted).

Ship-review addenda (same day): pre-downscale frames to 0.25 natural aspect +
`downsample_ratio=1.0` (kills the discarded full-res guided-filter/fgr work —
review measured the full-res path infeasible for the 90s budget on Fly
vCPUs); ORT threads pinned (2 intra-op, spinning off) for the shared-vCPU
worker; windows totalling >1800 ticks fall back to mediapipe up front; a
definitive sanity-gate rejection persists a `.matte.v2.unstable` sentinel so
reburns of the same base stop re-paying the recompute (transient failures
still retry); matte-migration deletes are prefix-guarded to
`generative-jobs/*.matte.*`.

## [2026-07-30] Render-attempt clock: anchor at Save, not at worker pickup (v0.18.1.1)

A re-render of a 5-minute edit came back reading "40m 32s". Every clock in the
progress band was pinned to the FIRST render, because a re-render reuses the
SAME `Job` row and nothing moved the anchors:

- `job.started_at` — written only by `job_phases.mark_started`, which refuses to
  move it once set (correct for its own job: it models worker pickup of ONE
  orchestrator run, and a Celery redelivery must not restart a clock mid-render).
- `variants[i]["render_started_at"]` — written in exactly ONE place repo-wide,
  the initial render loop in `tasks/generative_build.py`.

Both are read as live wall clocks: the elapsed counter, the ETA, the "taking
longer than usual" stall copy, and the per-variant tile all count from them. So
after the first render finished, every subsequent Save produced a counter
counting from the original render, an ETA floored at "less than a minute", the
stall hint firing instantly, and a receipt reading "Ready in -36:-12" (because
`finished_at` was still ahead of `started_at`).

Decisions:

- **Anchor at DISPATCH, not at worker pickup.** The user's mental model is the
  Save press, and queue wait is time they are genuinely waiting. Two helpers in
  `app/services/job_phases.py`: `mark_reattempt(job)` (moves `started_at` to
  now, returns whether it moved) and `stamp_variant_attempt(variant)` (sets
  `render_status="rendering"` + a fresh `render_started_at`). Wired into all
  **16** re-render dispatch paths in `routes/generative_jobs.py` +
  `tasks/autoplace.py`.
- **`mark_reattempt` takes the ORM object, `stamp_variant_attempt` takes the
  variant dict** — deliberately unlike the `job_id`-keyed helpers around them.
  Route dispatchers already hold a loaded (often row-locked) `Job` and commit
  themselves; and several dispatchers build `updated = dict(v)` then assign
  `variants[i] = updated`, so a helper that re-walked the job's variant list
  would mutate the original and have its write silently overwritten by that
  assignment. Passing the dict the caller actually persists makes that class of
  bug unrepresentable.
- **Skip the reset while an orchestrator run is in flight** (`current_phase` is
  non-None until `mark_finished` clears it). Editing an already-ready variant
  while its siblings are still on their FIRST render would otherwise move the
  anchor out from under that run: every later `record_phase` would compute
  `t_offset_ms` against the new origin (non-monotonic `phase_log`) and the
  whole-job clock the user is watching would visibly jump back to zero.
- **Never touch `finished_at`.** `plan_items.py` exports it as the plan item's
  ready date and no re-render task calls `mark_finished`, so nulling it would
  erase that date permanently. Readers guard on `started_at > finished_at`
  instead — `deriveReceiptText` (`components/progress/logic.ts`) returns the
  generic "Your edits are ready" whenever the pair can't yield an honest
  duration. Rejected: nulling `finished_at` at dispatch (data loss), and adding
  a parallel `attempt_started_at` column (a migration plus a second anchor every
  reader would have to learn, for a value `started_at` already means).
- **Gen-id minting stays in `_mark_variant_rendering`** and was NOT folded into
  the stamp helper: `_update_variant_entry` discards any worker write whose
  token differs from the stored one, so minting a token on a dispatch path whose
  task doesn't carry it would strand the variant in "rendering" forever.
- **Structural (AST) guard, not a grep.** The bug existed because 14 hand-rolled
  `render_status = "rendering"` blocks had no choke point, and NONE of them wrote
  the timestamp — at the base commit `render_started_at` appears in
  `routes/generative_jobs.py` only as a Pydantic field default, and not at all in
  `tasks/autoplace.py`. A source-grep guard matches one exact spelling of one
  line, so the same write in ANOTHER module slips through — which is exactly how
  `tasks/autoplace.py` diverged. `tests/routes/test_render_attempt_clock.py`
  walks the AST of both dispatch modules instead. Known residual: the raw-write
  guard matches `variant["render_status"] = "rendering"` assignments, so a
  `.update({...})` call or a whole-dict literal would still evade it; the pairing
  guard (stamp ⇒ reset) catches those in any function that also stamps.

Frontend half of the same bug: a re-render does NOT move `job.status` off
`variants_ready`, so a "terminal status wins" poll predicate stopped polling the
instant the user pressed Save (new video only appeared after a manual reload),
and `ProgressTheater`'s collapsed band never reopened on the same mount.
`isGenerativeJobSettled` (`lib/generative-api.ts`) is now the single predicate
for all three surfaces (item page, public `/generative`, onboarding EditPayoff):
non-terminal ⇒ not settled; a FAILED terminal ⇒ settled regardless of variant
state (a variant frozen in "rendering" after a failed job is a backend
data-integrity gap and must never block the UI); a SUCCESS terminal ⇒ not
settled while a variant is genuinely rendering, where "genuinely" means inside
the 30-minute `STUCK_RENDER_CEILING_MS`. The ceiling exists because
`reconcile_stuck_variants` only heals a stranded variant after ~60 min — without
it the UI would poll a dead render forever with a live-ticking timer.

Accepted costs (all tracked in TODOS.md, none silently degraded):

- **Admin render-timing breakdown is last-attempt only.** `admin_jobs.py`
  computes `queue_wait_ms = created_at → started_at` and
  `processing_ms = started_at → finished_at`. Once `started_at` moves at every
  dispatch, the first measures "job creation → last Save" and the second covers
  only the last attempt. `started_at` is the user-facing wall clock first and an
  admin metric second; per-attempt truth lives on the variants.
- **A dead re-render now climbs confidently.** `_compute_retrying` gates on
  `job.status in {processing, rendering}` plus an orchestrator-ticked heartbeat,
  and a re-render leaves `job.status` terminal. Before this change the lie was
  at least static (frozen counter, stopped poller); now it updates smoothly
  until the 30-minute ceiling. The fix makes a known gap more visible rather
  than creating one.
- **Bar and ETA still read the first render's phases.** No re-render task calls
  `record_phase`, so a null `currentPhase` pins the bar under 5% and
  `get_baselines("generative")` advertises the full-pipeline estimate for a
  20-second text reburn.
- **Client-vs-API clock skew got sharper, not worse.** The anchor used to be
  minutes in the past (skew invisible); it is now "a moment ago", so a browser
  clock trailing the API clock parks the counter at "0s" for the skew window.

**Revisit if:** the re-render tasks start driving `record_phase` /
`mark_finished` and heartbeating. At that point `job.status` and the phase log
cover re-renders the way they cover first renders, the 30-minute frontend
ceiling becomes a backstop rather than the only bound, and `phase_log` needs an
origin-aware reader (it will carry entries written against two `started_at`
origins).

## [2026-07-30] Intro placement: persist what the render resolved; readers mirror the renderer (v0.18.1.3)

Two bugs with one shape — something downstream of the burn re-derived a value
the render had already resolved, and guessed wrong. Mechanics live in
`docs/pipelines/generative.md` ("Intro placement snapshot"); the reusable rules
are here.

**1. The resolved placement was never written down.**
`_resolve_intro_overlay_params` folds knobs > curated set > agent advisory into
one placement, but nothing persisted the result. The editor's read adapter
(`_base_text_elements_for_variant`) had to reconstruct it and always landed on
`generative_overlays._DEFAULT_POSITION` — so a curated set or an
`overlay_format_matcher` run that chose `bottom` burned at the bottom while the
editor drew the hook mid-frame, and saving baked the wrong spot in.
`_intro_placement_from_params` now snapshots position / fracs / max_width /
anchor / rotation onto `variants[i]["intro_placement"]`.

- **`None` for the plain centered placement.** Most variants are centered;
  persisting a dict for all of them would change the stored shape of every
  variant to buy nothing, since the adapter's legacy path is already right
  there. The one exception is a variant carrying `text_placement_candidates` —
  the legacy fallback reads those candidate fracs, so even a centered
  resolution has something to disagree with and must be recorded.
- **Fold the persisted position back at the ADVISORY tier, and only when it is
  not "center".** A no-LLM re-render rebuilds `agent_form` without the agent's
  original advisory, so without the fold the first text edit silently
  re-centered a `bottom` intro. Folding it as an advisory (not an override)
  keeps knobs and curated sets winning exactly as they did on the first render.
  Folding "center" is deliberately suppressed rather than treated as a harmless
  no-op: it would resolve `style["position"] == "center"`, flip
  `has_explicit_position` True, and make the resolver skip the
  placement-candidate branch — silently dropping a masonry intro's
  whitespace-pocket fracs on re-render.

**2. `_finalize_job`'s allowlist is a trap worth stating once.** Finalization
rebuilds each variant from an explicit key list, so a new per-variant key is
DELETED on the first render unless it is added there. This is the third time
the repo has paid for rediscovering it (plans/007 autoplace, plans/010 silence
cut, now `intro_placement`) and it fails in the worst way: the feature works
end-to-end in the render, then the value vanishes at the finish line. Any PR
adding a variant key adds it to the allowlist AND pins it with a
`test_finalize_job_preserves_*` guard — here,
`test_finalize_job_preserves_intro_placement`.

**3. A burn-dict reader must mirror the renderer's fallback table, not invent
0.5.** The renderer-parity invariant (2026-05 #296/#297) says every burn-dict
field must be honored by both renderers. This is its mirror image: anything
that reads a burn dict back — the editor adapters, not just the renderers — has
to fall back the way `_resolve_anchor` does. `_burn_dict_position` hardcoded
`y = 0.5` for a HALF-pinned overlay (x set, y null), but `_POSITION_Y["center"]`
is 0.45 and `_POSITION_Y["bottom"]` is 0.85. `_burn_dict_position` is shared, so
the silent second victim was lyric seeds: `word_reveal`, `typewriter`,
`ai_answer`, and `lyric_word_pop_punchy` all pin x with a null y, and
`GET .../lyric-seeds` is a PERSIST path, so the invented y got saved. Guard:
`test_seed_elements_half_pinned_style_set_uses_the_renderers_y`.

**4. Do not truthiness-coerce a placement frac.** `0.0` is meaningful and
reachable — `position_x_frac` is `ge=0.0` on the knob route and
`_rotation_for_empty_pocket` returns `rotation_deg=0.0` for every non-portrait
masonry pocket. Coercing a falsy frac to a default re-centers the block. The
`value or fallback` pattern in the adapter applies to the two STRING keys
(`position`, `text_anchor`) only.

## [2026-08-02] Worker VM reverted to shared-cpu-4x — the performance upgrade paid for an unused flag

The 2026-06-13 bump of the `worker` process from `shared-cpu-4x/6144MB` to
`performance-4x/8192MB` (~$33/mo → ~$155/mo) was justified by two things: faster
single renders, and making `GENERATIVE_PARALLEL_VARIANTS_ENABLED` safe (concurrent
FFmpeg encodes need real cores, not oversubscribed shared vCPUs — see the
`d018d1c3` incident referenced in `fly.toml`). Seven weeks later, that flag was
still `default=False` in `config.py` and absent from `fly secrets list` — it was
never turned on. With `--concurrency=1` hard-pinned regardless, 3 of the 4
dedicated vCPUs were paid-for idle capacity the entire time.

A Fly-cost audit (Aug 2026) found the bill was ~$172/mo with this one machine at
90% of it, running at a 0.63% duty cycle (4.58 render-hours billed as 730 in July).
Reverted `fly.toml` to the pre-6/13 sizing — zero behavior change, since the
capability the bigger VM existed for was never in use — recovering ~$122/mo (81%
of a ~$150/mo total cost-cut plan) with a one-line change and no new failure modes.

**Sequencing note, not a reversal of the original upgrade's logic:** the plan to
scale the worker to zero (stop it when idle, start it on demand) is a separate,
larger follow-up. Once that ships, the worker's *active* hours drop to roughly the
same ~5/month regardless of VM class, so the cost difference between
shared-cpu-4x and performance-4x collapses to well under $1/mo — at that point
performance-4x should come back, and `GENERATIVE_PARALLEL_VARIANTS_ENABLED` should
actually be flipped on, since there's no longer a real cost tradeoff against doing
so. This entry documents the interim state: performance capability off, flag off,
cost down. Revisit trigger: when the scale-to-zero follow-up lands.

## [2026-08-02] Render-worker autostop — app-controlled Fly Machines start/stop

Follow-up to the VM revert above. Same cost audit, same plan doc
(`~/.claude/plans/this-month-our-fly-io-twinkling-peach.md`), the harder ~$28/mo
of the ~$150/mo total: the `worker` machine now stops itself when idle and
starts back up on demand, instead of running 24/7 regardless of the ~0.63%
duty cycle the audit measured.

**Fly has no built-in for this — verified, not assumed.** The obvious
built-in, `fly-autoscaler`, was evaluated first and rejected: its own sample
config has no scale-down key, and a Fly staff reply on the community forum to
this exact question ("how do I auto stop/start a Celery worker") states
plainly that fly.toml has no config for it. The stop half has to be
hand-rolled; there is no shortcut.

**Design, in the order the pieces depend on each other:**

1. **Split the lanes first (Phase 1).** A `light` process (fly.toml,
   shared-cpu-1x/512MB) now runs everything Celery Beat schedules —
   `task_routes` in `worker.py` (`MAINTENANCE_TASK_NAMES`) sends every
   Beat-triggered task there instead of the render worker's queues. Before
   this, the 5-min `sweep_stale_jobs` entry alone guaranteed the worker was
   never idle for more than a few minutes — autostop is structurally
   impossible without this split. Deliberately narrow: analysis tasks that
   download media or load torch (`match_pool_clips`, `agentic_template_build_task`,
   etc.) stay on the render worker; they'd OOM a 512MB box.

2. **One idle-check, shared by two different questions.** `render_worker_idle()`
   (`queue_state.py`) answers "is there ANY render-queue work anywhere" —
   an aggregate check. The existing reaper's `_live_job_ids` answers "is
   THIS specific job_id live" — a per-job check. These do NOT share a
   function despite both meaning "check if Celery is doing something" —
   forcing the reaper to call an aggregate idle-check would answer the
   wrong question for its purpose. What they DO share: `RENDER_WORKER_QUEUES`,
   one constant, so "what counts as a render-worker queue" can't drift
   between the two call sites (plus a third: the wake-hook's routing_key
   filter, below).

3. **The wake hook filters by queue, not task name — on purpose.** The
   obvious design (hook the wake call into `job_dispatch.enqueue_orchestrator`,
   the existing first-render dispatch chokepoint) was checked against the
   actual call sites and rejected: 17 of ~24 render-task dispatches
   (`regenerate_generative_variant`, `reburn_narrated_captions`, etc. — swap-song,
   retext, bed-level, caption-language actions in `generative_jobs.py`) call
   `.delay()`/`.apply_async()` directly and bypass that helper by design
   (they act on an *existing* job, not a first render). A hand-curated
   task-name list for the wake hook kept missing tasks too — even the
   *analysis* tasks that run on this same worker (clip/track analysis)
   aren't render tasks by name but still need the wake. The fix: one
   `before_task_publish` signal in `worker.py`, filtered on `routing_key`
   (empirically verified to equal the target queue name in this app's
   config — no custom exchange topology declared). This mirrors Celery's
   actual dispatch mechanism exactly, closing the whole bypass class
   structurally rather than patching each site.

4. **The wake hook must never block the request path.** Most dispatch sites
   run inside FastAPI async handlers on the public `api` process. The
   Fly API call fires from a background thread with a tight timeout,
   debounced via a short-TTL (60s) Redis key — deliberately much shorter
   than the 10-min idle grace period, so a "we already woke it" skip can
   never coincide with a legitimate stop-then-restart within its own
   window. This is why no live Fly-state check is needed on the hot path:
   the debounce alone is correct, and the periodic lifecycle backstop
   (below) is what actually guarantees a missed/failed wake gets recovered.
   Fails OPEN (calls Fly anyway) on any Redis error — an extra harmless API
   call beats a silently-skipped wake.

5. **The stop/backstop lifecycle task (`tasks.manage_render_worker_lifecycle`,
   every 2 min on `light`) is the actual correctness guarantee**, not the
   wake hook. It stops the machine after `RENDER_IDLE_GRACE_MIN` (10 min)
   of continuous idleness, and — this is the part that bounds every other
   failure mode in this design — starts it back up if there's real work
   and it isn't already running, regardless of whether the wake hook fired,
   fired but failed, or was never reached at all. Idle-duration is tracked
   across ticks via a Redis timestamp (`_decide_lifecycle_action`, a pure
   function with no I/O, tested exhaustively) since Beat itself carries no
   state between firings.

6. **A new failure mode this design creates: Beat's death now blocks all
   renders.** Before autostop, if Beat died, the render worker kept running
   and kept consuming jobs — Beat's death only affected maintenance/cleanup.
   After autostop, the worker spends most of its life stopped, and the
   *only* thing that restarts it on a missed wake is Beat's own lifecycle
   task. If Beat dies while the worker happens to be stopped, there is no
   path back — every new render sits `queued` forever, silently (the API
   still returns 200 on job creation). The first fix tried — fold a
   heartbeat into the existing daily-digest task — doesn't actually work:
   that task is *itself* Beat-scheduled, so it shares Beat's exact blind
   spot and can't detect Beat's own total absence. Confirmed no
   external-to-Beat monitoring exists anywhere in this codebase (no
   scheduled GitHub Actions workflow; the existing "dead-man's-switch" in
   `send_daily_digest.py` has the identical blind spot). The actual fix:
   `GET /health/beat` (`main.py`) reads a Redis timestamp any Beat-scheduled
   task writes on success via a `task_success` signal
   (`BEAT_SCHEDULED_TASK_NAMES`, derived from `beat_schedule` itself so a
   future entry is covered automatically) — meant to be pinged by a service
   OUTSIDE this app entirely (UptimeRobot, cron-job.org, a scheduled GH
   Actions workflow), since that's the only kind of check immune to Beat's
   own death. [2026-08-04: that external pinger now exists —
   `.github/workflows/beat-health.yml`, every 15 min; see the 2026-08-04
   entry below for the incident its two-day absence allowed.]

7. **The Fly API token now lives on the public `api` process, not just
   `worker`/`beat`** — the wake hook fires from FastAPI request handlers.
   Scope `FLY_API_TOKEN` narrowly (app-scoped to nova-video, never a full
   personal/org token): if the internet-facing process is ever compromised
   through some unrelated vulnerability, the blast radius should be
   "control of this one app's machines," not "full Fly org access."

**Deliberately deferred, not forgotten:** generative clip-ingest cache reuse
and baking CLIP weights into the image (both TODOS.md, 2026-08-02 entry) —
real, but neither required for the cost win, both needing their own pass.
Merging `beat` into `light` to save an additional ~$2/mo was considered and
explicitly rejected — it would concentrate the exact Beat-liveness SPOF from
point 6 onto a single machine for a saving too small to justify it.

Kill switch: `RENDER_AUTOSTOP_ENABLED`, default `false` — byte-identical to
pre-autostop behavior when off. Restoring `performance-4x` and flipping
`GENERATIVE_PARALLEL_VARIANTS_ENABLED` (the VM-revert entry's Phase 3) is a
deliberate later step, gated on this autostop design being verified in prod
first — not bundled into this same change.

## [2026-08-02] Worker VM re-reverted to performance-4x — the downsize missed the other justification, and prod proved it within an hour

The shared-cpu-4x downsize above was live for less than an hour before it
caused a real render failure: job `6bdb58dc-5933-4e82-b55c-ba2de00da19c` (the
first render dispatched after the downsize deployed) failed when its Skia
PNG-sequence text-overlay burn (`text_overlay_skia.py:2025`,
`subprocess.run(..., timeout=600)`) exceeded its 600s ceiling and was killed.
Two of its stages timed out this way (916s and 616s elapsed before failing).

**The downsize's reasoning was incomplete, not wrong about the flag.** The
2026-06-13 performance-4x upgrade had two stated justifications:
`GENERATIVE_PARALLEL_VARIANTS_ENABLED` safety (genuinely unused, correctly
identified) and single-render speed — *"shared cores are
oversubscribed/throttled, so every FFmpeg encode + the Skia per-frame PNG
render runs slower than the vCPU count suggests. Dedicated cores make each
render materially faster."* The cost-cut plan treated "the flag was never
used" as license to downsize, without separately verifying the second
justification didn't matter on its own. It did — this incident is a direct,
fast confirmation of the original upgrade's own documented rationale, on
exactly the workload type (Skia PNG-sequence overlay burns) it named.

**Confidence level, stated plainly:** high but not proven by a controlled
comparison. The mechanism (oversubscribed shared vCPUs run CPU-bound Skia/
ffmpeg work slower), the timing (first render after the downgrade), and the
pre-existing first-party claim about this exact workload all point the same
direction. What's missing: a byte-identical rerun of the same input on both
VM classes — Fly's shared-vCPU throttling happens at the hypervisor level
and isn't visible from inside the guest (`/sys/fs/cgroup/cpu.stat` and
similar aren't present in a Fly Firecracker guest), so this couldn't be
confirmed directly from the machine itself.

**Why revert immediately instead of gathering more evidence first:** a
failed render is a real user-visible cost (a lost video, and the current
recovery path requires the user to notice and retry). Continuing to run
degraded while collecting more failures to be sure would trade a small,
already-large-enough amount of evidence for actual harm. Reverting costs
$122/mo for a matter of days, until `RENDER_AUTOSTOP_ENABLED` is verified —
at which point the worker's active hours drop enough that this VM-class
choice barely matters on cost either way (see the scale-to-zero entry
above), so this reversal has no long-run cost, only a short deliberate delay
on banking part of the savings.

**Lesson for future cost-cut work on this codebase:** when a resource
upgrade has multiple stated justifications, downsizing requires checking
that NONE of them still apply — not just the one that's easiest to prove
unused. "Unused capability" and "unused VM class" are different claims;
this incident is what happens when the first gets treated as proof of the
second.

## [2026-08-04] Frozen "Starting…" on Generate — mint the Job in-request, don't shorten the queue (plans/014, v0.23.2.0)

A mobile user tapped Generate on a plan item and sat on a disabled
"Starting…" button for ~8 minutes (plan item `f2b9201d`, job `4fb8fa0f`).
Root cause, prod-verified from the admin API: `POST /plan-items/{id}/generate`
didn't create the Job row — it only enqueued the `generate_plan_item_videos`
Celery task on the default `celery` queue, and the Job (whose existence is
what flips `derive_item_status()` to `"generating"`) was minted by that task.
The single `worker` machine (`--concurrency=1`, draining `celery` +
`plan-jobs` + `overlay-jobs`) had an in-flight render (`61c4d859`) holding
the slot, so the tiny Job-minting task waited behind it head-of-line. FIFO
drain signature: `61c4d859` finished at 11:25:35.99Z and the user's Job was
created 244 ms later. Corroboration: content_plan jobs routinely showed
multi-minute `created→started` gaps, while public generative jobs'
`created_at` is the click time — because `POST /generative-jobs` builds the
Job synchronously in the route. Ruled out: render-worker autostop (not
enabled in Fly secrets), mobile-specific frontend paths, poll caching.

**Decision — make the wait visible, not shorter.** The fix ports the public
generative-jobs pattern: Generate mints the Job in-request
(`dispatch_item_render_for` in `content_plan_build.py`, run via
`anyio.to_thread.run_sync`), so the page's immediate post-tap refetch flips
to the progress view and shows an honest "queued" phase. The single-slot head-of-line
blocking itself is deliberately NOT fixed here — worker topology is a cost
decision (second machine / analysis lane split) deferred to TODOS.md
("Render-worker queue latency" section). Kill switch:
`PLAN_SYNC_DISPATCH_ENABLED=false` restores the legacy `.delay()`+409
contract byte-identically.

**Sub-decisions worth keeping:**

- **One dispatch entry point, route AND task.** `dispatch_item_render_for`
  (SELECT … FOR UPDATE on the item + active-render re-check) is shared by
  the route thread and the Celery task body, so the two paths can't drift.
  The activation loop applies the same lock+re-check inline — every minting
  path locks, which is the duplicate-mint guard since
  `jobs.content_plan_item_id` deliberately has no uniqueness constraint
  (retries mint new rows).
- **Duplicate Generate ⇒ idempotent 200, not 409.** A lost-response mobile
  retry self-heals into the render that's already running instead of
  stranding the user on an error banner.
- **Publish-failure containment is CONDITIONAL on `status == "queued"`.**
  `apply_async` raising does not prove Redis rejected the message; if the
  worker already flipped the job to `processing`, an unconditional
  `processing_failed` write would mark a RUNNING render failed and invite a
  duplicate. rowcount 0 ⇒ the worker owns it ⇒ report dispatched. A genuine
  publish failure flips the Job terminal (`dispatch_publish_failed`) because
  the reaper deliberately never reaps `queued` — an uncontained failure
  would strand a forever-"generating" ghost.
- **Status buckets single-sourced** in `app/services/job_status.py`:
  `plan_items.py`'s hand-copied bucket had drifted from `me.py`'s (missing
  `template_ready`/`music_ready`), so a template/music job pinned to a plan
  day read as "generating" forever AND blocked new generates. The dispatch
  re-check and the read path must share one terminal set or a failed item
  refuses to re-generate / a live render double-dispatches.

Full plan + prod evidence table + review deltas: `plans/014-sync-plan-generate-registration.md`.

## [2026-08-04] Light machine OOM'd into a permanent `stopped` — gate worker_ready hooks by consumed queue

Two days after the autostop lane split (#764), the `light` machine
(`48e2547a50d138`) was found `stopped` with the state surviving every deploy —
meaning the 5-min stale-job sweeper, the daily digest, and all TikTok
periodics had silently been dead since Aug 2 ~12:33Z. `/health/beat` was
returning 503 with age ≈ 55h; nothing alerted.

**Root cause (Fly Prometheus, org `emir-erben`):** `worker_ready` signal
handlers in `app/worker.py` fire in EVERY celery worker process, so
`_prewarm_clip_font_matcher` loaded torch + open-clip ViT-B/32 (~450-600MB
peak per `clip_font_matcher.py`'s own docstring) on the 512MB light VM. The
machine ran with ~7MB available from its first minute, thrashed through ~13
OOM kill/reload cycles between 11:49Z and 12:33Z, exhausted flyd's
`on-failure, max_retries: 10` restart budget, and parked `stopped`. Two
compounding Fly behaviors made that permanent and invisible: `fly deploy`
updates a stopped machine but leaves it stopped (observed directly in the
machine's event log: `launch` → `stopped` on every subsequent deploy), and a
machine's event history is truncated on update, so the OOM evidence was gone
from `fly machine status` — the memory timeline had to come from the
Prometheus API.

**Decisions:**

- **Gate the prewarm by consumed queues, fail-closed** —
  `_worker_consumes_render_queues` (worker.py) checks
  `sender.app.amqp.queues.consume_from` against `RENDER_WORKER_QUEUES` and
  skips the prewarm for maintenance-only workers OR when introspection
  fails. The asymmetry that justifies fail-closed: a wrongly-skipped prewarm
  costs one ~3-5s lazy load on the render worker; a wrongly-run prewarm
  kills the light machine. Pinned by `tests/test_worker_prewarm_gate.py`.
  The fly.toml "light never loads torch" rule only ever covered task
  routing; boot-time signal hooks were the uncovered class, and any future
  `worker_ready` hook that loads a model must use the same gate.
- **light 512 → 1024MB** (fly.toml) — even mid-thrash the baseline (celery
  parent + 2 prefork children of the full app import) left single-digit MB
  available; 512 was sized against the tasks, not the worker shape.
- **Wire the external dead-man's switch that was designed but never
  deployed** — `.github/workflows/beat-health.yml` pings `/health/beat`
  every 15 min. `beat_heartbeat.py` predicted this exact blind spot ("a
  check that is itself Beat-scheduled shares Beat's exact blind spot") and
  the endpoint existed; the outside-the-app pinger was the missing half, and
  its absence is why the outage ran 2+ days undetected.
- **Recovery is NOT just `fly machine start`** — the stopped machine still
  runs the old image; starting it without this fix re-enters the same OOM
  loop (it survived ~66 min on Aug 2). Deploying this change recreates/
  updates the machine config; if it stays `stopped` after the deploy, one
  `fly machine start 48e2547a50d138 -a nova-video` brings the lane back on
  the fixed image (the ID is valid as of 2026-08-04 — a recreating deploy
  mints a new one, so resolve the current `light` machine via
  `fly machine list -a nova-video` first). Expect a brief drain burst: ~2 days of queued
  maintenance messages (~5-6k, mostly no-op polls) are sitting in Redis.

## [2026-08-05] Intro-hook transformation slop — ban the pattern class, lint the exemplars, scan don't warn (plans/015)

A montage plan-item render burned "the monkey changed my whole marketing
perspective" as its opening hook: intro_writer glued unrelated footage (a
monkey) to a persona pillar (marketing) with a fabricated transformation
claim. Root cause was three-way: the prompt's DON'T list banned slop as
LITERAL strings ("changed everything") that paraphrase trivially; the pillar
instruction outweighed the existing drop-the-theme escape clause (a subjective
"changed my perspective" isn't an invented fact/place/event, so the clause
never fired); and the exemplar library itself shipped the banned frame —
`transformation-before-after-karaoke-01` read "this is what changed
everything" (added PR #338, phrase banned later by PR #507; prompt/exemplar
drift with no guard).

**Decisions:**

- **Ban the pattern CLASS in the prompt, not more strings** — retrospective
  transformation / lesson-learned framing is named as a class with examples,
  plus a translate-don't-echo rule for plan ideas that arrive already
  slop-framed ("how X changed my life"). The existing pillar escape clause at
  write_intro_text.txt was deliberately left untouched: rewriting it was
  wording churn carrying over-correction risk (model drops persona when
  aligned); escalate only if live shadow A/B says the ban alone is
  insufficient.
- **One deterministic pattern source, three free consumers** —
  `slop_structural_failures()` in `app/agents/intro_writer.py` (same shape as
  sequence_quote's `quote_structural_failures`) feeds the eval structural
  floor, `tests/agents/test_overlay_examples_slop_guard.py` (every exemplar
  AND every recorded fixture output must pass — kills the #338/#507 drift
  class), and `scripts/dev/scan_intro_slop.py`. Turkish trap worth knowing:
  `str.casefold()` maps İ to `i` + U+0307 combining dot BETWEEN letters, so
  naive lowercase regexes silently miss uppercase Turkish — the normalizer
  strips U+0307 and a test pins it.
- **No runtime enforcement, no runtime warn** — a parse()-level rejection
  would downgrade good hooks to the generic fallback on any false positive
  (rejected at eng review), and a runtime warn was cut too: persisted
  `intro_text` is REUSED on re-render with no LLM call, so a runtime hook
  structurally cannot see legacy slop, ships with the fix (no baseline), and
  conflates deliberate false positives into a blind rate. The offline scanner
  (read-only) gives the pre-deploy baseline, the legacy remediation list, and
  the post-deploy delta instead.
- **Rubric had the incentive backwards** — persona_coherence scored 1 for
  "ignores the persona/theme", punishing the correct drop-the-theme behavior
  on conflict footage. Now: dropping an unhonorable theme scores 5 (conflict
  rule); lesson-glue lines score an automatic 1 on persona_coherence AND
  voice_match, with the monkey line as the calibrated example. Four
  adversarial fixtures (marketing/monkey, fitness/cooking, TR, slop-framed
  idea) pin the desired outputs.
- **Merge gate:** live judge shadow A/B on a keyed machine (repo
  prompt-change rule) — replay CI guards artifacts, not model behavior.

---

## [2026-08-12] TikTok "drafts" copy named a destination that doesn't exist

A user's Upload API delivery succeeded (prod logs: `inbox/video/init/` 200 OK,
TikTok pulled the media, `status/fetch` reached a terminal state, the poll
sweep went quiet) but the receipt said **"Ready in TikTok drafts."** The user
checked tiktok.com on desktop and could not find the video, and reasonably
reported it as missing/broken.

**Root cause:** TikTok's Upload API does not write to the Drafts tab at all —
it pushes a notification into the creator's TikTok **Inbox**, visible only in
the mobile app. TikTok's own docs require apps to instruct users to "click on
inbox notifications to continue the editing flow." Kria's dominant copy
(`TikTokReleaseRail.tsx`, `TikTokPublishDialog.tsx`, `LibraryTile.tsx`,
`TikTokProductWorkspace.tsx`) said "TikTok drafts" everywhere prominent, and
never said "phone app only" — contradicting the handful of places that
correctly said "inbox notification."

**Decision:** Renamed every "TikTok drafts" destination string to name the
real place (TikTok app inbox), added explicit find-it steps (open the app →
Inbox → tap the notification; will not appear on tiktok.com or under Profile →
Drafts) to the receipt and the pre-send confirm step, wired a download
fallback into the receipt state (previously receipts had no download
affordance at all), and exposed `tiktok_publish_id` on `GET
/tiktok/publications/{id}` for support correlation without reading Fly logs.
Internal identifiers (`delivery_mode="draft_upload"`,
`privacy_level="TIKTOK_DRAFT"`, `visibility_status="draft"`) were deliberately
left unchanged — only presentation strings changed, no migration.
**Revisit if:** TikTok ever ships an API that writes directly to Drafts, or if
`draft_upload` being the auto-selected default whenever `video.publish` isn't
granted (`TikTokPublishDialog.tsx`) keeps causing this complaint — that's a
scope grant-recovery problem, not a copy problem.

## [2026-08-14] Optional lyrics must not erase the content-plan intro

A Corfu content-plan item rendered a technically valid 3.2-second sailboat
video with no visible text. The intro writer had produced “pov: sailboat
mornings in korfu,” and the selected song had usable lyrics, but two individually
tested decisions interacted badly: content-plan primary montages chose the
first renderable music variant (`song_lyrics`), while optional-lyrics mode made
that variant start with lyrics off. Because a lyrics-mode variant does not burn
the agent intro, both possible text layers disappeared. The renderer correctly
produced an MP4; the product contract was wrong.

**Decisions:**

- **A generated intro is the default visible text.** With
  `LYRICS_OPTIONAL_ENABLED=true`, a track-backed content-plan montage selects
  `song_text` / `agent_text`, so the authored intro is persisted and burned.
- **Lyrics are a capability, not a competing variant.** A track-backed
  `song_text` variant with renderable lyrics is marked `lyrics_baked=false`; the existing
  lyric-seed endpoint and text-element editor can add lyrics later without
  replacing the intro. Its API capability follows `LYRICS_OPTIONAL_ENABLED`,
  not the separate legacy baked-lyrics editor flag.
- **The kill switch preserves the old path.** With optional lyrics disabled,
  the selector still takes the first renderable variant, so a lyric-capable
  track produces legacy `song_lyrics` with baked lyrics and the persisted dict
  shape remains unchanged.
- **Test interactions, not only branches.** The critical guard renders a
  lyric-capable `song_text` under optional-lyrics mode and requires both the
  Corfu-style intro persistence and the lyric-seed capability. Separate tests
  continue to pin the legacy flag-off behavior.

## [2026-08-14] Guided edits require an approved, exact-media proposal

The Corfu incident was not only missing text. Initial generation treated one attached sailboat
clip as the story while twelve separately stored travel assets remained advisory overlay material.
Trying to make overlay placement more aggressive would preserve the wrong abstraction: food,
architecture, streets, and beaches are primary story moments, not decorations over a one-clip
montage.

**Decisions:**

- **Approve the story before rendering.** Plan items get one versioned `edit_proposal` envelope
  with a live draft and retained last approval. Direction, goal, pace, duration, title, media,
  layouts, ordered beats, and thoughts become reviewable product data rather than implicit LLM
  decisions inside the renderer.
- **Unify references, not storage.** Attached clips and `PlanItemAsset` rows stay in their existing
  lanes. A proposal-level `MediaRef` union gives both stable IDs and immutable storage generations.
  Legacy clip assignments receive an ID only when Plan edit first processes them.
- **Object identity is part of approval.** The canonical media digest includes lane, stable ID,
  path, generation, kind, and content hash. Generate re-reads storage generations and snapshots the
  approval under the Plan → Persona → PlanItem → Job lock order. A changed generation is stale even
  when its filename is unchanged.
- **AI thoughts remain visibly provisional.** The planner may describe visible qualities and infer
  tone, but it may not claim where the creator went, what food tasted like, who was present, or how
  the creator felt without creator-written context. AI thoughts retain `thought_source=ai_draft`
  until the whole proposal is approved; direct user edits switch that thought to `user`.
- **Proposal beats are chapters, not a file list.** Live evaluation showed the first planner could
  produce one generic beat per upload and use experiential wording without creator context. The
  planner now groups at least three distinct topics into no more than five chapters. Its parser
  strips safe-to-remove sensory modifiers and rejects unsupported personal actions or remaining
  sensory claims before a draft reaches the creator.
- **No legacy bypass after enforcement.** Route and task-side dispatch both return
  `proposal_required`, `proposal_draft`, `proposal_stale`, or `proposal_analyzing`. Media mutations
  retain the last approval for comparison and mark the current plan stale. CAS versions prevent a
  second tab from overwriting a newer edit.
- **Planning and rendering ship dark and separately.** PR 2 can persist and snapshot proposals but
  does not claim to render them. Capability, frontend, and enforcement switches all default off;
  enforcement waits for PR 3's strict story assembler and the exact Corfu preview.

## [2026-08-14] Approved guided stories render as their own strict timeline

The proposal contract is now executable. An approved plan no longer becomes hints for the legacy
montage matcher: it compiles once into a task-owned execution plan and renders directly from every
selected photo/video beat.

**Decisions:**

- **Approval is the render program.** The compiler keeps the proposal version, media digest, ordered
  beat/media IDs, exact source and music storage generations, source windows, layouts, wording,
  duration, typography, transition policy, and whole-story music match. Redelivery reuses the saved
  plan rather than making fresh creative decisions.
- **Duration and transitions are reconciled before FFmpeg.** Beat durations are weights under the
  approved top-level target. Crossfade inputs are extended by the overlap they consume, so the final
  duration remains the approved duration; impossible short-source combinations fail instead of
  dropping media.
- **Photos and supporting cards are story footage.** Photos receive subtle motion, videos use the
  existing crop/trim pipeline, and a requested supporting card renders as an inset over a blurred
  background. Neither lane is treated as an optional post-render overlay.
- **Publication requires measured evidence.** The receipt records exact downloaded generations and
  hashes, every rendered moment, actual per-element alpha bounds, media/topic IDs, duration, canvas,
  codecs, final hash, and exact published base/output object generations. Missing text, media, cards,
  or format invariants keep the Job failed; there is no simple-montage fallback.
- **Worker duplication is fenced, not assumed away.** A row-locked, heartbeating attempt lease admits
  one renderer even when Celery delivers the same task twice. Losing deliveries cannot stamp the live
  job finished; stale leases can be reclaimed after a worker disappears.
- **Text stays editable without reopening creative decisions.** The renderer preserves a clean story
  base. TextElement-only edits reburn that base and refresh alpha evidence. Legacy song/timeline/layout
  operations fail closed for guided stories until they have proposal-aware implementations.
- **Rollout remains dark by default.** Strict-renderer readiness now permits enforcement, but capability,
  frontend, and enforcement flags stay false until a production-parity Corfu preview passes.

## [2026-08-15] Guided-story photos use decoded render copies

The first production retry of the approved Corfu story passed duration compilation but stopped on its
first HEIC photo. Pillow had already verified the uploaded file; FFmpeg then selected its HEIF demuxer,
which rejects the image2-only `-loop` input option used for photo motion.

**Decisions:**

- **Source evidence and render inputs are separate.** Exact-generation downloads remain untouched for
  byte count, SHA-256, and publication receipts. The worker creates a separate EXIF-corrected JPEG, or
  PNG when alpha is present, for FFmpeg assembly.
- **Normalize every selected photo.** The renderer no longer relies on filename-specific FFmpeg demuxer
  behavior. JPEG, PNG, WebP, HEIC, and HEIF all cross the same verified decode boundary before motion.
- **Bound memory instead of parallelizing decode.** Downloads and video probes remain bounded-parallel,
  while full-resolution phone photos decode serially so several large images cannot multiply peak
  worker memory.
- **Reject rather than fall back.** A photo that cannot fully decode keeps the stable
  `guided_story_media_replaced` failure. The renderer never drops it or substitutes a simple montage.

## [2026-08-16] Guided edits accept natural-language direction before and after planning

The first guided-edit release exposed direction, pace, length, and goal as a form. It was safe, but
it asked creators to translate creative intent into product controls before Kria helped.

**Decisions:**

- **Conversation produces the same typed contract.** Creators can speak in ordinary terms and the
  edit guide persists a direction, goal, pace, and duration in the versioned proposal envelope.
  `brief_ready` is advisory; creators may start planning at any time.
- **The conversation continues during review.** Requests such as “put food first” or “use less text”
  can revise a draft. Any revision returns an approved proposal to `draft`, so new wording and order
  cannot render without approval.
- **Media identity stays server-owned.** Model-authored revisions preserve every beat ID exactly once.
  Media reassignment was added later through bounded short aliases; the route always rejoins real
  identities and retains creator-written thoughts verbatim.
- **Slow model calls do not hold item locks.** The route first persists a short-lived, token-fenced
  single-flight reservation, closes the transaction, calls the model, then reloads under lock and
  requires the same token and proposal version. A reload sees only safe thinking/retry state, never
  the token, and generic planning stays blocked until the reply lands or the creator retries an
  expired attempt. Concurrent changes cannot overwrite the older reply or duplicate model spend.
- **Conversation has its own staged writer gate.** `briefing` is a new persisted state that older
  readers reject. `GUIDED_EDIT_CONVERSATION_ENABLED` therefore stays off until every API and worker
  runs the compatible reader; the API advertises the switch and the web keeps the existing brief
  form until it is safe to write conversational state.

## [2026-08-17] Guided-story results remain authoritative when reopened

Production dogfood rendered the requested five-video travel cut, but reopening it exposed two legacy
projections: the text reader collapsed the approved title and five thoughts into one intro, and the
timeline reader marked every source unused because it only understood `ai_timeline`.

**Decisions:**

- **Persisted guided-story text wins on reads.** The renderer's verified `text_elements` document is
  authoritative even before a creator manually edits it. Legacy intro projection must not replace it.
- **The verified story cut is visible but read-only.** The editor projects `story_timeline` into its
  existing clip-lane response so creators can inspect order, trims, and used media without making the
  legacy timeline mutation APIs responsible for guided stories.
- **Different text regions may share time.** A top-positioned title and bottom-positioned first thought
  can both start at zero. Serializing them made the first thought only one frame long in short edits.

## [2026-08-17] Fly release_command startup kill: retry once, don't touch REMAP_SIGTERM

`Fly Deploy` intermittently went red with exit 143 (SIGTERM) on the release_command machine
(`python -m alembic upgrade head`) — the machine reported `has state: destroyed` roughly 3 seconds
after starting, well before alembic could plausibly have failed, and the very next deploy of the
identical code succeeded. Rate: 2 in ~12 releases (v891, v896). The concerning part wasn't the
occasional red run — it was that a deploy pipeline crying wolf 1-in-6 can't also be trusted as the
signal for a real freeze; this exact noise class is how the 24-hour deploy freeze (#812) hid earlier.

Issue #834 ranked three hypotheses, top of which was `REMAP_SIGTERM = "SIGQUIT"` in fly.toml `[env]`
leaking onto the release machine. That was refuted during investigation: `REMAP_SIGTERM` is a Celery
5.5 feature read by the Celery worker process itself (to reinterpret SIGTERM as cold shutdown, opening
the soft-shutdown window used by `app/worker.py`'s late-ack design) — Fly's init never reads it, and
it's absent from Fly's own configuration docs. The release machine does inherit the `[env]` block (Fly
docs: the temporary release machine "has full access to the network, environment variables and
secrets"), but `python -m alembic` never reads that variable, so it is inert there. The exit code is
the tell: 143 = 128+15 = a plain, unremapped SIGTERM; a SIGQUIT death (what an actual remap would
cause) would be 131. The observed 143 proves the process died from platform-side SIGTERM, not a
Celery-side remap that doesn't even apply to this process. Conclusion: this is a Fly platform-side
machine lifecycle race (flyctl and the machine disagreeing about state, per the accompanying "timeout
waiting for release command logs" log line), not something fixable from our config.

**Decisions:**

- **Retry, don't reconfigure.** Nothing in fly.toml changed in value — `REMAP_SIGTERM` stays
  `SIGQUIT` at the top-level `[env]` block, which `tests/test_deploy_shutdown_policy.py` continues to
  pin, because the worker's late-ack soft-shutdown design is load-bearing on it. A comment was added
  directly above the line recording the refutation, so the next person chasing a #834-shaped report
  doesn't re-open the same dead end.
- **Signature-gated, not blanket.** `scripts/fly-deploy-with-retry.sh` retries exactly once, and only
  when the deploy log shows BOTH `has state: destroyed` and `release_command failed running on
  machine ... with exit code 143` (word-bounded regex, ANSI-stripped first). Any other failure — most
  importantly a genuine migration `raise`, which exits 1 — fails immediately on the first attempt.
  Acceptance criterion from #834: a startup-killed release command no longer fails the deploy outright,
  AND a real migration failure still fails on attempt one, visibly. A blanket retry-on-any-failure was
  explicitly rejected — it would recreate the exact noise class that hid #812.
- **The gate is log-based, NOT flyctl-exit-code-based — deliberately.** flyctl itself exits 1 on a
  release_command failure (verified in run 31972609339: log says `exit code 143`, step says "Process
  completed with exit code 1"); the 143 only ever appears in the LOG. Cross-model review suggested
  "hardening" the gate with `$exit_code -eq 143` — that would silently disable the retry forever.
  Pinned by `test_signature_then_success_retries_once_and_warns` (exit 1 + signature ⇒ retries).
- **Known accepted over-match:** a migration that runs long enough to be SIGTERM'd by Fly's 5-minute
  release timeout produces the same signature and gets the one loud retry. Accepted because alembic
  migrations are transactional, every deploy attempt re-runs the release command anyway, and a second
  timeout still fails the deploy. The retry annotations deliberately hedge the root-cause claim
  ("usually the #834 race") rather than asserting it — the signature proves SIGTERM + destroyed, not
  which SIGTERM.
- **Loud, not silently green.** A retry emits a `::warning title=Fly deploy retried::` GitHub Actions
  annotation (unconditional) plus a `$GITHUB_STEP_SUMMARY` block with a 40-line log tail, whenever that
  var is set (CI only — local runs no-op instead of crashing). A green deploy after a retry should
  still be visibly flagged as "retried," not indistinguishable from a clean first-attempt pass.

**Revisit if:** the retried-signature rate rises materially above the observed ~2-in-12 baseline (would
suggest the platform race is worsening, not just noise), or Fly ships a fix for release-machine
lifecycle reporting — at that point the retry wrapper becomes a belt-and-braces fallback rather than a
load-bearing part of the pipeline, and `timeout-minutes` can likely drop back toward 25.
## [2026-08-17] Conversational revisions use short review references

Production dogfood asked Kria to swap two named travel videos and shorten five thoughts. The model's
reply described the correct change, but both schema attempts failed because one long generated beat ID
was lost while reproducing the full revision.

**Decisions:**

- **Models edit aliases, servers retain identities.** Review prompts expose `beat_1`, `beat_2`, and
  similar short references. Parsed revisions must preserve every alias exactly once, after which the
  server maps them back to the original beat IDs.
- **Visible-content references have an explicit join.** Media already supplied to the edit guide gets
  a short `media_1`-style reference, and every review beat lists its associated media references. This
  lets “the bridge video,” “the Istanbul clip,” and uploaded filenames resolve to the intended beat.
- **Safety remains fail-closed.** Unknown, missing, or duplicated aliases are rejected. The model still
  cannot add media, replace object identity, remove beats, or overwrite creator-authored thoughts.

## [2026-08-17] Conversational revisions may reassign existing media safely

The first filename-reference repair let the model understand which upload a creator meant, but the
revision output could only reorder beat IDs. In production dogfood the model changed “Cityscape” to
“Lisbon” while leaving the Istanbul source underneath it, then claimed the request was complete.

**Decisions:**

- **Every revised beat returns media aliases explicitly.** A request such as “moment 1 uses the bridge
  video” can move `media_1` to that beat instead of merely changing its text.
- **The server validates the complete assignment.** The multiset of returned aliases must exactly match
  the currently assigned aliases. Unknown, missing, or duplicated media fails closed before persistence.
- **Aliases never become authority.** Only the server maps validated aliases back to stable media IDs;
  the model still cannot introduce another user's object, replace storage identity, or drop a source.

## [2026-08-17] Approved guided media satisfies the Generate footage gate

Production dogfood approved a five-source guided story built entirely from the visuals pool, then the
page disabled Generate with “Add clips to generate.” The strict renderer already consumes both media
lanes from the approved snapshot; only the legacy frontend gate still counted attached clips.

**Decision:** A current approved proposal with at least one selected story-beat media ID satisfies both
the frontend and API footage gates. Unguided items and empty/malformed approvals still require an
attached clip, and the lock-owning dispatcher continues to revalidate the approved snapshot before
dispatch. The initial frontend-only repair exposed the API twin in production; both layers now share
the same contract.

## [2026-08-17] Stale lock reads are gated by shape, not by convention

PR #813 fixed a bug where `SELECT ... FOR UPDATE` serialized correctly but the Python object behind
it did not refresh: SQLAlchemy only writes a freshly-locked row onto an instance it is loading for the
first time in that session, so a prior unlocked read of the same PK makes the locked call return the
cached object with pre-lock values. Two concurrent Generate posts each read `current_job_id` as `None`
and each minted a Job. The same staleness let the ownership fence read a stale `ownership_epoch`.

**Decisions:**

- **The gate matches the bug shape, not the convention.** `tests/test_row_lock_policy.py` flags an
  unlocked read of a PK followed by a locked re-read of the same PK in the same function that does not
  pass `populate_existing=True`. Requiring the pairing at all ~78 `with_for_update` sites was rejected:
  a bare lock in a fresh session with nothing cached is perfectly safe, and 60-odd allow-list entries
  written to satisfy a linter is noise nobody reads. One guard people trust beats a broad one they mute.
- **Matching is session-scoped, not function-scoped.** The identity map lives on the session, so two
  reads only interact when they share one. A function that opens a session, closes it, does slow work,
  then opens a SECOND session to take the lock is the safest possible shape. A function-scoped first
  draft flagged four such cases — including `autoplace.generate_pool_asset_preview`, merged the same
  day — so 4 of its 6 findings were noise. Scoping to the enclosing `with ... as <session>:` block
  removed all four and left 2 verified instances. A gate that cries wolf is a gate people mute, which
  would have defeated the whole point.
- **The allow-list is a bug backlog and is asserted in both directions.** The 2 verified instances are
  quarantined so the gate can protect new code immediately, not because they are believed correct
  (issue #845). A second test fails if a listed entry stops matching, so a fixed or renamed entry
  cannot rot the list into fiction — an allow-list nobody trusts is an allow-list nobody reads.
- **The count is a floor.** The detector keys on the source text of the identifier expression, so the
  same row reached two ways (`db.get(PlanItem, content_plan_item_id)` then
  `db.get(PlanItem, item_ref.id, with_for_update=True)`) is the same bug and is not flagged. Stated in
  the module docstring so the number is never mistaken for a total.

Verified by reintroducing #813 (removing `populate_existing` from `dispatch_item_render_for`): the gate
names the exact function and row, and goes green again when restored.

## [2026-08-17] Deploy health is measured as drift, and staged migrations are enforced

The 2026-08-12 freeze was two failures wearing one costume. #804 shipped migrations `0072` and `0073`
together; `0073` refuses to apply while a mismatched content plan exists, so `alembic upgrade head`
threw and rolled the chain back, and release 1 could never reach production. Its own runbook said "do
not combine these into one Fly release" — an instruction with no mechanism behind it. Separately,
nobody noticed for ~24 hours: Fly aborts a release *before* replacing machines, so the previous image
kept serving, `/health` stayed 200, and four consecutive red `Fly Deploy` runs read as normal.

**Decisions:**

- **Deploy health is measured as drift, never as failure.** `deploy-drift.yml` compares main's HEAD
  against the last successful `Fly Deploy` and fails once the oldest undeployed commit exceeds
  `DRIFT_HOURS`. Alerting on red deploys was rejected deliberately: per #834 the release command is
  killed during startup roughly 2 runs in 12 and self-heals on the next push, so a redness alarm would
  fire twice a fortnight and be learned-past — which is exactly how the real freeze hid. A self-healing
  flake leaves zero drift; only genuinely stuck code accumulates it. Drift is also cause-agnostic: a
  migration refusal, a revoked token, a Fly outage and a persistent exit-143 all present identically.
- **A PR adding 2+ migrations where any can refuse must declare its release order.**
  `migration-staging-guard.yml` fails it unless the body carries `[staged-migration-ok]`. Verified
  against the real incident: #804 added exactly `0072` and `0073`, both contain `raise`, so the guard
  would have blocked it. A lone refusing migration is the safe shape and is not flagged.
- **CI runs on pushes to main, not only on pull requests.** A PR is tested as a merge with main, so
  main can be broken while every open PR is green. On 2026-08-13 a stale-read bug on main made a
  required check fail deterministically and it surfaced on a frontend-only PR that touched zero Python.
  Push-triggered CI attributes a breakage to the commit that caused it. The cost is roughly double CI
  minutes, accepted consciously.

The narrative lives here rather than in CLAUDE.md because that file is at 37,914 of its 38,000-char
budget; the guards announce themselves through their own CI failure messages.

## [2026-08-17] Guided stories inherit selected-media orientation

Production dogfood proved that all five approved travel sources were 16:9 while the strict story
renderer still hardcoded a 9:16 canvas. The output was technically valid but needlessly cropped, and
the editor then disabled the control that could have corrected it.

**Decisions:**

- **Approved screen time chooses the default canvas.** Only media referenced by story beats votes;
  each source is weighted by its approved exposure. Near-square and unknown aspects are neutral,
  ties follow the first selected non-square source, and no usable metadata falls back to portrait.
- **Orientation is part of the strict program.** Compiler v3 pins the chosen canvas and explanation;
  moment rendering, text burn, receipt dimensions, variant projection, and the editor all read that
  same value. Persisted v1/v2 plans remain portrait and replay without reinterpretation.
- **Editor changes remain story-native.** A 9:16/16:9 change re-renders the pinned media, timing,
  music, and validated text through the strict story renderer. Token-gated publication and the full
  receipt apply again; a generic montage is never an orientation fallback.
- **Editorial defaults do not outline text.** New guided titles use Fraunces and thoughts use DM Sans
  with warm-white fill, a lime accent, and soft shadow. Both persist `stroke_width=0`; old plans and
  user edits remain unchanged.

## [2026-08-18] Guided-edit wedge: no duration floor, AI designs by default

A real user's plan item (6.77s clip, 0 pool assets) was permanently stuck: `status="failed"`,
`failure.code="proposal_generation_failed"`, zero `agent_run` rows in the failure window, Generate
409ing forever with no retry path. Two root causes, one product gap:

- **The crash.** `_analyze_clip_assignment`'s cache-miss path calls `analyze_pool_video` — a raw
  Gemini call outside the `Agent` framework, so it never produces an `agent_run` row on failure
  (matches the "zero rows" evidence exactly; a `SchemaError` from the real `EditProposalAgent` would
  have logged one). A transient `AnalysisTemporarilyUnavailableError` propagated uncaught through
  `draft_edit_proposal`'s outer blanket `except Exception`, permanently marking the proposal failed
  with `retryable=True` that never actually retried anything. Fixed: mirror
  `autoplace.analyze_pool_asset`'s existing `AssetUnreadableError`/`AnalysisTemporarilyUnavailableError`
  split — transient errors now trigger a real Celery retry; unreadable media fails fast, non-retryable.
  The outer blanket handler (and every `_fail` call) now also persists an admin/debug-only
  `ProposalFailure.detail` (exception type + reason), stripped before the public response.
- **The artificial floor.** Even with the crash fixed, `EditProposalSnapshot.duration_s`/agent
  input+output all required `ge=10` — a 6.77s clip can never reach 10s without inventing footage.
  `draft_edit_proposal` now computes a feasible-duration estimate from analyzed media
  (`feasible_guided_duration_s`: real video durations summed, images credited a fixed floor) and
  clamps the brief's target down to it (`adapt_target_duration_s`) before calling the agent — no
  slow-mo/stretch. Floors dropped to `ge=3` end to end; under 3s is genuinely infeasible and fails
  fast (`guided_edit_infeasible`) without ever calling the agent.
- **The product gap.** Even correctly failing fast, the creator was still stuck behind a review step
  they never asked for. `GUIDED_AUTO_DESIGN_ENABLED` (default true) makes Generate reserve, draft, and
  auto-approve (`approval_mode="auto"`) instead of 409ing whenever media exists — one-click Generate
  always works. A drafting failure with zero registered pool assets falls back to the legacy montage
  render directly (`bypass_guided_edit_gate` on `_dispatch_item_render`, used only by this caller);
  pool assets present never silently fall back (2026-08-15 pool-media-drop invariant) — the failure
  just stays retryable. Flipping the flag off is not byte-identical rollback: proposals an earlier
  auto-design attempt already approved stay approved (see
  `docs/runbooks/conversational-edit-rollback.md`).

**Adversarial review same day** found the first cut unconditionally clobbered any non-in-flight
proposal (including one a human was reviewing or had already approved), serialized an
already-`rollback()`-expired ORM row on the idempotent path (`MissingGreenlet` in prod under a real
`AsyncSession`), let auto-design's clip-only montage fallback bypass the narrated-voiceover and
photo-collage business rules, fed the agent a feasible-duration estimate that credited too-short
videos as if they were full images, and passed `auto_finalize` as a Celery task kwarg (a rolling-
deploy hazard). All fixed same day: `_maybe_auto_design_generate` now branches on the FOR-UPDATE-
locked proposal status (never clobbers `approved`/`draft`/a live `conversation_attempt`; auto-
finalizes an existing draft instead of redrafting it); the idempotent path always reloads after
rollback before serializing; the two hard business-rule checks run before auto-design can intercept;
`feasible_guided_duration_s` credits a video only when its own duration clears the renderer's
per-moment minimum; the auto-finalize intent is read off the persisted `approval_mode` field instead
of a task kwarg. See `docs/pipelines/guided-edit.md` for the full mechanism.

## [2026-08-19] Empty best_moments is an answer, not a failure — keep the Gemini analysis, synthesize the windows (v0.40.0.1)

**Incident:** prod jobs `82fb4c57`/`f95b43b8` — 6 of 7 clips across the pair (a coffee-ad shoot)
were calm single-shot footage (talking-head take, product b-roll). Gemini analyzed each successfully
but returned `best_moments: []`, and `_analyze_one` (`app/tasks/template_orchestrate.py`) treated
that as a hard failure (`GeminiAnalysisError`), discarding the whole ClipMeta for the bare Whisper
fallback. Each discarded field poisoned a different downstream consumer: hero selection lost
`hook_score` (coin-flip tie at the default), the hook was written about the wrong clip (off-topic
"subscribe" hook on a coffee ad), `detected_subject` vanished from matching, and the music matcher
was left with the fallback's flat `energy: 5.0` — the field's neutral midpoint, carrying no real
signal. Note the fix does NOT change energy: synthetic windows still carry the flat 5.0 (only the
subject/description improved); content-aware energy stays open in TODOS.md.

**Fix:** empty `best_moments` from a successful analysis keeps the real meta and synthesizes only
the moment windows: `_fallback_moments(clip_dur, description=...)` duration-bucketed windows,
labeled via `_synthetic_moment_description` with the clip's real `detected_subject`/`hook_text`
instead of the literal `"fallback"` (agentic_matcher builds router candidates from
`moment.description` before `meta.hook_text`). New `ClipMeta.moments_synthetic=True` marks them.
When Gemini also omitted the transcript, a best-effort Whisper pass restores the old discard path's
transcript guarantee.

**Cache contract (the subtle half):**
- Synthetic moments are job-local heuristics — `set_cached_meta` refuses `moments_synthetic` metas
  (same rule as degraded/failed) so a later run retries the real Gemini path.
- Prefetch (`services/clip_prefetch.py`) DOES cache legitimately-empty `best_moments`, and cache
  hits skip `_analyze_one` entirely — so `_backfill_cached_empty_moments` sweeps cache-hit metas
  on BOTH the full-hit and partial-hit returns of `_analyze_clips_with_cache` (idempotent for
  freshly analyzed metas; skips image metas via the runtime-only `is_image` attr — not a ClipMeta
  field, so it never survives a cache round-trip; unprobed clips default to 30.0s via
  `_clip_duration`). Without the sweep, a warm cache renders worse than a cold run of the same
  clip.
- Adding the field bumped `CACHE_SCHEMA_VERSION` `s1`→`s2` in `pipeline/clip_cache.py`: any new
  ClipMeta field MUST bump it, or pre-field cache entries deserialize with silently-defaulted
  values. `scripts/export_clip_metadata_fixtures.py` now parses the key's schema segment
  generically instead of hardcoding `s1`.
- The generative path threads the field through `_clip_meta_to_cache`/`_clip_meta_from_cache` in
  `tasks/generative_build.py` — same class of trap as the clip_metadata parse()-threading bug
  (#398): a field left out of these mirrors silently defaults on round-trip.

**Guards:** `test_empty_best_moments_backfills_moments_keeps_analysis`,
`test_empty_moments_and_empty_transcript_gets_whisper_backstop`,
`test_backfill_cached_empty_moments_sweep`, `test_moments_backfilled_records_phase_log_event`
(tests/tasks/test_template_orchestrate.py); `test_set_skips_synthetic_moments_metas`
(tests/pipeline/test_clip_cache.py).

**Revisit if:** synthetic windows show up in a rendered-quality regression (the windows are still
duration buckets, not content-aware — see the "Content-aware `_fallback_moments()`" TODO), or a
new consumer starts trusting `best_moments` energy/timing without checking `moments_synthetic`.

## [2026-08-19] Depth-occluder backbone closes the "behind-arbitrary-objects" gap (prod job 30b717b9)

Closes the trade-off explicitly deferred in the 2026-07-27 entry above ("RVM
mattes people, not arbitrary objects ... Behind-arbitrary-objects would need
a salient-object/video-segmentation model"). Prod job `30b717b9-90ab-4d99-
a311-ea1103a5e80e` (plan item `85d1de16-ba11-4533-9290-927a45819cd3`,
"ACROPOLIS"/"ATHENS" `behind_subject: true` text on a person-less landscape
shot) rendered fully in front of the landmark: the person-only matte engine
found nobody, `matte_is_sane` rejected on `max_coverage < 0.01`, and the
resolver silently stripped the effect.

Decisions:
- **New depth backbone, selected only when the person pass finds nothing.**
  `compute_subject_matte` runs RVM/MediaPipe first as always; a depth pass is
  attempted only when `stats.max_coverage < _PERSON_MAX_COVERAGE_FLOOR`
  (0.01, factored out of the existing gate literal) AND
  `MATTE_DEPTH_OCCLUDER_ENABLED` is true. `MatteStats` gains
  `backbone: str = "rvm"` (`"rvm" | "mediapipe" | "depth"`), threaded into
  the matte sidecar JSON and the `subject_matte_resolved` pipeline-trace
  event, so the admin job-debug view shows which segmenter actually produced
  a given occlusion.
- **Model: Depth Anything V2 Small, fp16 ONNX, ~47.3MB.** License verified
  Apache-2.0 for both the small checkpoint and the specific onnx-community
  export committed — same verify-before-commit bar the 2026-07-27 RVM
  GPL-3.0 note set, but this model is permissively licensed outright (no
  server-side-only carve-out needed). Provenance: HF
  `onnx-community/depth-anything-v2-small` `onnx/model_fp16.onnx` at commit
  `4472b7362082ad9968fee890ca0f1e5aca36b93d`, SHA256
  `2df6223f206b5164e21f664ace61dabeb9bb6a49b8b5a3e00510b4807d0f5b04`;
  license evidence: DepthAnything/Depth-Anything-V2 README ("Depth-Anything-
  V2-Small model is under the Apache-2.0 license"; Base/Large/Giant are
  CC-BY-NC-4.0 and must never be substituted in) plus the export repo's
  `license: apache-2.0` metadata inheriting from the small checkpoint.
  Benchmarked (local M-series; Fly shared vCPUs assumed ~half): ~194ms/frame
  CPU at the SHIPPED native 518×518 input (`_DEPTH_INPUT_SIZE`); a reduced
  patch-multiple 266×476 measured ~87ms/frame and remains a future perf
  lever, not adopted because E2E occlusion verification ran at 518×518.
  Sampled at `_DEPTH_INFER_FPS = 10` (disparity held between samples) to
  stay inside the existing 90s per-burn wall-clock budget even when
  person-less footage pays for both the person pass and the depth pass.
- **Threshold: sky-epsilon on robustly-normalized disparity, not Otsu.** An
  Otsu (bimodal-histogram) threshold was tried first and rejected —
  validated empirically on the real Acropolis footage, raw disparity is
  skewed rather than bimodal (sky/far-background clusters near ~0,
  near-foreground dominates the rest of the histogram), so Otsu's split
  misclassified the landmark itself as background. Shipped rule instead:
  per sampled frame, clip disparity to its own [p1, p99] range and rescale
  to `[0, 1]`; occluder = `normalized_disparity > _DEPTH_SKY_EPS` (0.05) —
  anything measurably nearer than the far-background mode occludes. A scene
  with no identifiable far layer over-occludes and is caught by the
  existing `mean_coverage <= 0.85` sanity-gate ceiling rather than shipping
  a bad mask. `matte_is_sane` additionally floors the depth branch at
  `mean_coverage >= 0.02` (`_DEPTH_MIN_MEAN_COVERAGE`) — depth has no
  per-pixel subject-confidence prior the way person segmentation does, so a
  near-zero split is more likely threshold noise than a real occluder.
  Shape-stability and oscillation/large-jump gates are unchanged and apply
  identically to both backbones.
- **Cache key bumped `.matte.v2.*` → `.matte.v3.*` for NEW writes; healthy
  v2 BLOBS stay accepted** (`_MATTE_CACHE_SUFFIX`,
  `_MATTE_ACCEPTED_CACHE_SUFFIXES`, `_MATTE_UNSTABLE_SUFFIX` in
  `generative_build.py`). A `.matte.v2.unstable` sentinel (a definitive
  person-only rejection) predates the depth backbone, so it must not be
  trusted as "no usable occluder" — its suffix no longer matches, making it
  a cache miss with one recompute now eligible for depth. But v2 BLOBS only
  exist where the person pass found a real subject, and the person write
  contract is unchanged in v3 — invalidating them (the ship-review red
  team's finding) would have recomputed identical mattes fleet-wide for
  pure churn AND funneled working v2 blobs through a delete hazard. No data
  migration needed; the fix reaches existing person-REJECTED renders on
  their next burn.
- **Dual-sentinel retryability** (ship-review finding train: Codex
  dark-ship poisoning → Claude adversarial transient-conclusive confusion →
  performance per-reburn tax). A gate rejection where the person pass found
  nobody is only CONCLUSIVE if the depth pass ran to completion and was
  itself gate-rejected (`MatteStats.depth_rejected`,
  `matte_rejection_is_retryable`); only conclusive rejections mint the
  permanent `.matte.v3.unstable`. Inconclusive ones (flag off, model
  missing, budget, crash, NaN output) mint `.matte.v3.nodepth.unstable`:
  short-circuits while the depth flag is off, reads as stale (one
  depth-eligible recompute) once it flips on. A real previous-version BLOB
  survives any inconclusive rejection instead of minting either sentinel.
- **Dark by default.** `MATTE_DEPTH_OCCLUDER_ENABLED` (`app/config.py`
  `matte_depth_occluder_enabled`), default `False`, fails closed on
  config-import failure. Off: `compute_subject_matte` behaves exactly as it
  did before this feature (person pass only). Rollback: `fly secrets set
  MATTE_DEPTH_OCCLUDER_ENABLED=false --app nova-video` + worker restart —
  and cached depth mattes are EVICTED on their next burn (sidecar
  `backbone: "depth"` + flag off → recompute → retryable sentinel), so
  rollback stops depth occlusion already in the cache too. No
  `NEXT_PUBLIC_` twin — this is a render-time backbone choice, not a
  user-facing toggle; `TEXT_BEHIND_SUBJECT_ENABLED` remains the single kill
  switch for the feature as a whole.

See `docs/pipelines/text-behind-subject.md` ("Non-person occlusion: depth
backbone") for the full mechanics. Deferred: surfacing `fallback_stripped`
to users, relaxing `match_overlay_format.txt` for landmark shots, ROI
refinement for depth (same deferral list as 2026-07-27, now scoped down by
one item).

## [2026-08-21] Audio-led render intent takes precedence over dormant guided proposals

The production artifact `variant_1_guided_story_8fca41651a0877e9.mp4` contained a generic visual
description burned into the picture and a music bed instead of the selected voiceover. Media
forensics proved there was no intelligible speech or subtitle track; the deployed guided renderer
had stripped source audio, matched music, and rendered proposal title/thought text before the native
voiceover resolver could run.

**Decisions:**

- One shared pure render-intent policy is authoritative. An explicit uploaded voiceover, or the
  `narrated*`, `subtitled`, or `talking_head` formats, is native/audio-led. Only `montage`,
  `day_vlog`, and `single_hero` without voiceover are guided-compatible; unknown non-empty formats
  fail closed to native.
- The route, response serializer, lock-owning dispatcher, and worker recompute the same policy.
  Audio-led items hide guided capability flags, skip guided enforcement/auto-design, require real
  clip inputs, and never stamp `assembly_plan.guided_edit`.
- Approved guided proposals remain dormant and unchanged while native intent is selected. They are
  not marked stale because the media digest intentionally represents media identity, not intent.
- A mixed-version worker ignores an incompatible guided snapshot when genuine clips exist. An
  asset-only Job whose only clip is the synthetic guided seed fails closed with a stable reason and
  asks the creator to Generate again; it must not partially render and drop approved pool media.
- Content-plan items carrying uploaded voiceover use the existing narrated assembler, including
  transcription and burned captions, even if a stale format token says `montage`. Generic legacy
  voiceover formats preserve audio but are not promised transcript captions; narrated format is the
  product contract when captions are required.

## [2026-09-07] One canonical row-lock order; the routes had drifted into two

Production returned 500s on `POST /creation-threads/{id}/media` (nova-video api
machine e78413def29018, 2026-09-07T18:16:14Z):

```
asyncpg.exceptions.DeadlockDetectedError: deadlock detected
DETAIL:  Process 18773 waits for ShareLock on transaction 44786; blocked by process 18871.
Process 18871 waits for ShareLock on transaction 44785; blocked by process 18773.
```

The media-attach guard `_reject_input_mutation_while_rendering` locked
`PlanItem` then `Job`; the retry-partial path `_prepare_partial_variant_retry`
locked `Job` then `PlanItem`. Concurrent requests each held one row and waited
for the other. `git blame` puts both sites in `1f102edd` (v0.59.4.0, #956) —
pre-existing, and not from the speech-cleanup preflight work that was in flight.

The interesting part is not the swap. It is *why* the swap survived review: the
codebase carried two contradictory lock orders, each documented as authoritative
in its own file. `app/tasks/kria_runtime.py` said "Global mutation order: Plan ->
PlanItem -> Job -> Session -> ..."; `app/routes/creator_agent.py` said "Match the
route-wide lock order: CreatorAgentSession -> Job", complete with a warning that
reversing it would deadlock. Both comments were written in good faith. Neither
was checkable, so each file's convention was locally consistent and globally
wrong. A convention that lives in a comment is not a convention.

Resolution: `app/db_locks.CANONICAL_LOCK_ORDER` is now the single source of
truth (`User → Persona → ContentPlan → PlanItem → PlanItemAsset → Job →
CreatorAgentSession → CreatorAgentTurn → CreatorEditDraft → CreatorAgentApproval
→ CreatorAgentExecution → CreationThread`), the Kria order extended at both
ends. `acquire_locked_rows` sorts the requested rows internally so a call site
cannot be typed wrong, and `tests/routes/test_lock_order.py` reconstructs every
function's lock sequence from the AST — inlining module-local helpers, because
this inversion spanned two functions, and splitting at commit/rollback, because
locks released between transactions cannot deadlock.

Two findings worth keeping:

- **`Job` before `CreatorAgentSession` won on evidence, not taste.** The
  Session-first convention had 4 sites; Job-first had the Kria runtime plus 4
  creation-thread repair helpers. Flipping `creator_agent._rollback_craft_commit`
  without also flipping `tasks/creator_quality_review.py` would have created a
  *new* deadlock pair — the fix has to move the whole equivalence class at once.
- **Teaching the guard about the helper found a bug the audit missed.** Once
  `acquire_locked_rows` calls were modelled as locks, the test flagged
  `claim_exact_review`, which locked `CreatorAgentSession` FOR UPDATE and read
  `Job` *unlocked* — then its callers locked `Job` moments later in the same
  transaction. A per-statement audit sees no inversion there; only reconstructing
  the transaction's full lock history does.

`PlanItemAsset`'s rank relative to `Job` is genuinely undetermined: measuring
both placements across `app/` gave 18 violating windows either way. It sits
before `Job` to match the ownership hierarchy.

Seven pre-existing inversions remain, listed with rationale in
`KNOWN_INVERSIONS` and in `docs/runbooks/row-lock-order.md`. The largest is
`delete_thread`, which locks the `CreationThread` root first because the whole
delete cascade is discovered through that row; fixing it means restructuring a
destructive path and was deliberately not bundled into a deadlock hotfix.

Deadlocks are also no longer 500s: `main.transient_db_conflict_handler` maps
SQLSTATE `40P01`/`40001` to a 409 with `Retry-After: 1`, logging
`transient_db_conflict` at warning level. A sustained rate of that log line means
an inversion slipped past the static guard.

## [2026-09-08] ASR token timestamps are not speech truth — mixed-gap-v2 (jobs a75981f8 / d0e284cb)

Two `required_v1` renders that had the mixed-gap-v1 candidate APPLIED still carried
an audible hesitation: the Turkish clip from the original plans/021 incident
(`a75981f8`, "the ıı between the 3rd and 4th second") and an English clip
(`d0e284cb`, a 1.4 s hole at 3–4 s). `build_cut_plan_comparison` over the persisted
`timed_words` reproduced both persisted plans exactly, so the detector was not flaky —
its inputs were wrong in a way it structurally could not see.

Every V2 rule windows off whisper-1 token boundaries, and the validator forbids
cutting into a token. That is sound only if token timestamps are speech truth. Around
disfluencies they are not, deterministically (3/3 fresh runs per clip):

- TR: the real `ııı` is voiced 7.406–7.978 s; whisper stamped `ııı,` at 8.04–8.26 s,
  entirely inside FFmpeg silence 7.978–8.293 s. The lexical cut removed silence. The
  voiced island WAS found by rule 2 and rejected `right_silence_too_short`, because its
  right flank was measured on the silence CLIPPED at the mis-stamped token start —
  62 ms of a 315 ms span. In August whisper emitted no token there (V1 missed it); now
  it emits one 0.6 s late (V2 missed it). Same audio, different failure, same result.
- EN: `a...` spans 5.48–7.20 s and contains a 1.4 s silence. Rules 2/3 only look
  BETWEEN tokens; a pause inside a token is invisible, and a fresh run merely moves the
  pause into `English` — still inside a token.

**Decision:** treat FFmpeg silencedetect as the authority on where a token is NOT. V2
gets a rule 0 (`_reconcile_words_with_silence`, `silence_cut.py`): a silence span of
at least 0.6 s (`MAX_PAUSE_S`) that overlaps a token by at least 0.3 s is carved out
of the token (head/tail trim or split) before rules 1–3 run; remnants under 50 ms are
absorbed; a token with no remnant is kept untouched. Rule 4 (retake word indices)
keeps the original list. Everything below is V2-only except the shared
`_normalize_silences` snap; V1 and the persisted `timed_words` are byte-identical.
Guard:
`tests/pipeline/test_silence_cut_asr_timestamp_golden.py`, built from the exact
persisted words of both rows. Geometry and thresholds: plans/021, addendum 2026-09-08.

Five smaller mechanisms ship with it, each fixing a way the newly exposed dead air
leaked back into the output:

- **`_interior_soundful_islands` measures a flank on the full silencedetect span only
  when that side's boundary token is a removable filler** (`filler_lo_boundary` /
  `filler_hi_boundary`). NOT unconditional — the conditional IS the safety property.
  Next to a real word the flank stays window-clipped, because whisper stamps real words
  late too, and the clipped flank is what stops such a word's own vocalization from
  being scored as a silence-bounded island and cut as an acoustic filler. Next to a
  filler there is nothing left to protect: rule 1 is removing that token anyway.
- **`_pause_removals(edges_skip_fillers=True)`** measures `LEAD_KEEP_S`/`TRAIL_KEEP_S`
  from the first/last token rule 1 will KEEP, and lets an interior pause window meet a
  filler neighbour at that neighbour's own `PAD_S` instead of `KEPT_GAP_S/2`. A clip
  opening on a filler otherwise stranded a sliver of silence between the lead trim and
  the filler's padded cut — too short to hear, too short to earn its own cut, and
  expensive: at the budget ceiling the micro-gap pass paid for it by evicting the whole
  lead trim.
- **`_retreat_from_micro_gap`** pulls one flexible silence carrier back by the missing
  fraction of a second so a sub-`MIN_KEEP_SEGMENT_S` flash becomes a real keep segment,
  instead of evicting a cut. Eviction returns the carrier's ENTIRE span to the edit;
  at the consent budget's ceiling every fitted carrier ends up flush against its
  neighbour, so eviction there silently undid most of the cleanup the creator asked
  for. Atomic filler/retake groups and protected spans never retreat, and eviction is
  still the fallback.
- **The clamp budget reserves room for protected spans.** Forced/manual spans are exempt
  from the budget but still shorten the output, so `duration − MIN_OUTPUT_S` now has
  their merged length subtracted before the clamp. Without it, a fully allocated plan
  plus a large forced span tripped `MIN_OUTPUT_S` at the very end and collapsed the
  whole candidate to a no-op.
- **`_normalize_silences` snaps a span within one 24 fps frame of 0 or `duration_s` to
  that boundary.** silencedetect measures decoded audio; the container is up to a frame
  longer (the EN clip's trailing silence ended 20 µs early), and that sliver kept the
  trailing cut from reaching the end of the clip — enough, once micro-gap hygiene got
  involved, to strand a quarter second of dead air at the end of the edit. This one is
  shared input normalization, NOT V2-gated: it leaves both incident clips' V1 baselines
  where production had them, but a V1 plan whose trailing silence stopped just short of
  the container can now reach the end.

And one selection change outside the detector: `analyze_speech_cleanup` now prefers the
V1 baseline when the V2 candidate BAILED OUT and the baseline did not and has cuts.
Rule 0 exposes more dead air, so more plans hit the bailout policy's rails; without this
a source whose V1 plan removed a second of silence would have shipped zero cleanup.

Three things worth keeping:

- **Tokens are never dropped, even when wholly inside silence.** The prototype did
  drop them and it was tempting — it is the cleanest fix for the TR clip. But a token
  with no voiced remnant is also exactly what a very quiet clip produces for EVERY
  word, and dropping those turns "cleanup did nothing" into "cleanup deleted the
  transcript". The filler-gated full-span flank covers the TR case without that
  failure mode.
- **Only silence is ever cut through a carve.** Rule 3 intersects with silencedetect,
  so the worst a bad carve can do is trim audio FFmpeg already called −30 dB silence,
  leaving `KEPT_GAP_S/2` on each side. The residual risk (review finding) is a soft
  onset under −30 dB for 0.3 s or more next to a long silence: it gets `trim_head` and
  loses its quietest fraction. `token_adjustments` in the receipt exists so shadow
  windows can count those before the canary widens.
- **A golden built from fabricated words would have passed.** The #960 golden
  modelled the tokenless gap it was written against; it could not model a token that
  whisper places on silence, because nobody had seen one yet. The new golden uses the
  persisted `timed_words` of both rows verbatim, and `scripts/speech_cleanup_preview.py`
  renders a clip through the real engine so the next detector change can be listened
  to, not just diffed.

**`DETECTOR_VERSION` → `mixed-gap-v2` — what the bump does, and deliberately does not:**

- `current_detector_policy()` (`services/plan_item_media.py`) folds `DETECTOR_VERSION`
  into every `source_policy_fingerprint`, and `_source_snapshot_matches` compares it
  directly. An existing `ready`/`no_findings` `SpeechCleanupAnalysis` row analysed
  under v1 is therefore never reused: the next preflight for that source supersedes it
  (`ensure_current_analysis_*`), queues a fresh v2 analysis on the `speech-analysis`
  worker, and `_clear_item_cleanup_choice` resets `speech_cleanup_enabled`. The creator
  sees the `SpeechCleanupDecisionCard` again on regenerate and re-consents against the
  new findings — a consent given to a v1 cut list must not silently authorise a v2 one.
  Expect one extra analysis per active item the first time each is touched after the
  deploy, and budget the `speech-analysis` queue accordingly.
- Already-stamped Jobs are untouched. The Job's private `preflight_snapshot` carries
  its own `detector_version`, `hydrate_speech_cleanup_snapshot` checks only
  snapshot-internal consistency, and the render replays the persisted `cut_plan`.
  In-flight `required_v1` jobs finish under the v1 plan they were consented to — the
  runbook's immutable-contract promise (`docs/runbooks/chat-speech-cleanup-rollout.md`).
- A required-speech render that crashed mid-stage resumes iff re-running it would
  reproduce the same label. `_reserve_required_speech_pending` no longer hands
  `classify_required_speech_resume` the deployed constant unconditionally: for a
  preflight Job it passes the accepted snapshot's OWN `detector_version`
  (`_expected_resume_detector_version`), because that Job applies the snapshot's
  `cut_plan` and a re-render would stamp the identical label — demanding the deployed
  constant there would rotate every staged-but-unfinalized generation on the bump and
  throw away finished, still-correct output. Only a legacy render-time-detector Job (no
  usable snapshot) is held to the deployed label and rotates with
  `terminal_context_mismatch`; a malformed snapshot falls back to the same strict
  behaviour, which is safe because such a render fails closed anyway.
- A `SpeechCleanupAnalysis` row QUEUED before the deploy is restamped at claim time
  (`claim_analysis` sets `row.detector_version = DETECTOR_VERSION`): the analysis is
  performed by the code running now, so persisting its plan under the previous
  version's name would make every downstream reuse/snapshot check treat a v2 plan as v1
  evidence. Claim only accepts queued/expired work, so no result is being relabelled.
  `_validate_work` then fails closed on any surviving mismatch — a row that reaches the
  engine still carrying another detector's label was not claimed by this code, and a
  plan must never be published under a name it did not earn.
- The render-side cut-cache key includes `detector=`, so sibling variants never reuse a
  v1 plan within one job.
- **Preflight cohort membership DOES reshuffle** — the earlier draft of this entry said
  it did not, and that was wrong. Two different buckets are involved and only one of
  them is salt-based. `select_mixed_gap_mode` buckets on
  `sha256("mixed-gap-v1:" + rollout_fingerprint)`, and `rollout_fingerprint` is
  `sha256("speech-cleanup-source-v1:{job_id}:{source_instance_id}")` — no detector
  version anywhere, salt deliberately left alone, so a source that reaches preflight
  keeps its shadow/apply assignment. But whether a source reaches preflight at all is
  decided upstream by `preflight_enabled_for_source` →
  `stable_preflight_cohort(source_policy_fingerprint)`, and that fingerprint hashes
  `detector_policy`, which contains `DETECTOR_VERSION`. Bumping it moves essentially
  every source to a new 0..99 cohort (≈99% of a 2000-source simulation), so at any
  `SPEECH_CLEANUP_PREFLIGHT_ROLLOUT_PERCENT` under 100 a different population is
  enrolled after the deploy. Operationally: a canary comparison must not be carried
  across the bump, the same percentage is not the same creators, and rollout evidence
  windows must be fresh — the runbook never reuses a window or receipt.
- `scripts/audit_speech_cleanup_shadow.py` had to learn the new version too
  (`_SUPPORTED_DETECTOR_VERSIONS = {"mixed-gap-v1", DETECTOR_VERSION}`): it hard-matched
  the string `mixed-gap-v1`, so the first audit after the bump would have matched zero
  receipts and refused exactly the deploy it exists to verify. Any future bump must keep
  the earlier versions in that set for as long as their receipts are still in the trace.

**Known limit these two clips demonstrated — since removed:** better detection did not
finish the job. Both clips are mostly dead air, and the then-current
`MAX_REMOVAL_FRAC_REQUIRED = 0.55` capped a `required_v1` clamp. The EN clip is 71%
silence by silencedetect; the v2 candidate PROPOSED 6.001 s (60% of the runtime) against
a `clamp_budget_s` of 5.507 s and shipped 5.391 s — the refused 0.61 s was not spread
thin, the entire interior pause `4.325–4.775` was dropped, leaving roughly 0.8 s of
continuous dead air audible. TR proposed 7.461 s (75%) and shipped 5.414 s. The entry
below ("The 55% explicit-consent removal cap is gone") is the product decision that removed that
ceiling, deliberately as its own change rather than riding along with the detector.
The triage advice survives the removal, with a different meaning: the next "cleanup left
a pause" report is still read from the receipt first — `clamped=true` with
`proposed_removed_s` above `clamp_budget_s`, and the span present in
`proposed_removals` but missing from `removed`, means the detector found it and the
budget declined it, and nothing in `silence_cut.py` can fix that. What `clamped=true`
now reports is the `MIN_OUTPUT_S` 3.0 s floor, not a fraction of the runtime.

**Lesson:** an upstream model's timestamps are a hypothesis, not ground truth; every
consumer that windows off them needs an independent signal to reconcile against. Any
future detector change — thresholds included — must bump `DETECTOR_VERSION`, or the
fingerprint/cache/snapshot machinery keeps serving pre-change plans indefinitely — and
the bump is never free: audit the everything-keyed-on-the-fingerprint list above,
because one of those keys decides who is in the rollout at all.

## [2026-09-08] The 55% explicit-consent removal cap is gone — MIN_OUTPUT_S is the only rail (jobs a75981f8 / d0e284cb)

`MAX_REMOVAL_FRAC_REQUIRED` in `app/pipeline/silence_cut.py` goes `0.55 → 1.0`. The
`over_budget_policy="clamp"` budget is still
`min(frac × duration, duration − MIN_OUTPUT_S − protected) − CLAMP_BUDGET_SLACK_S`, but
with `frac = 1.0` the fraction leg can never bind: on a 10 s clip the budget moves from
5.499 s to 6.999 s, and `MIN_OUTPUT_S` (3.0 s) is the only thing left holding a
`required_v1` plan back.

**Read this with the entry below it.** Removing the cap is only safe because the two
speech-safety guards in "Two guards so rule 0 cannot cut real speech" replaced the
protection this cap was incidentally providing. Do not port the constant change into
another branch without them.

**Why.** The cap was inherited caution on a path the creator has already consented to.
`required_v1` only exists because the creator turned Speech cleanup ON for this item;
the clips that reach it are the filler-heavy, pause-heavy talking-to-camera clips the
feature was built for, and those are precisely the clips a fraction-of-runtime ceiling
declines. The mixed-gap-v2 detector (entry above) found the dead air correctly and then
watched the budget refuse it: the TR clip needed ~65% removal and shipped with a second
of dead air still in it. A rail that declines the feature's own best case is not a
safety rail, it is a bug with a constant's name. Yasin's call: take it out.

**Measured on the two incident clips** (`tests/pipeline/test_silence_cut_asr_timestamp_golden.py`,
fixtures are the exact persisted `timed_words` of both rows):

| Clip | removed before → after | output before → after | `clamped` |
|---|---|---|---|
| TR (job a75981f8) | 5.414 s → 6.855 s | 4.586 s → 3.145 s | true → true (now the floor) |
| EN (job d0e284cb) | 5.391 s → 5.900 s | 4.623 s → 4.114 s | true → false |

The TR clip's last second of dead air — the residual the previous entry's "Known limit"
paragraph described as unrecoverable by any detector work — is gone. The EN candidate
proposed 6.001 s all along; the budget went 5.507 s → 7.013 s, so nothing is clamped out
of it any more, and it ships 5.900 s: the missing 0.102 s is `MIN_KEEP_SPEECH_SEGMENT_S`
buying a 0.498 s speech fragment up to the shot floor (next entry). TR proposes 7.461 s against a 6.999 s budget
and still comes back `clamped=true`, which is the reading change that matters
operationally: **`clamped=true` now means the `MIN_OUTPUT_S`
floor bound the plan, not a fraction of the runtime.** A receipt showing
`proposed_removed_s > clamp_budget_s` today is a clip whose removals would have left
under 3 s of video.

**What still bounds a plan.**
- `MIN_OUTPUT_S = 3.0` — the hard floor; an edit can never collapse below a usable
  length, and protected (forced/manual) spans are reserved out of the budget before the
  clamp so they cannot push it through the floor.
- `MIN_CLIP_S = 5.0` — clips shorter than this are never cut at all.
- `MAX_REMOVAL_FRAC = 0.4` — **a different rail, untouched.** It is the
  `over_budget_policy="bailout"` rail on the auto/legacy path, where nobody consented to
  anything; do not conflate the two constants because their names rhyme. Nothing in this
  change touches `legacy_auto` or `off_v1`, which stay byte-identical.

**The operator switch.** `settings.speech_cleanup_max_removal_frac_required`
(`SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED`, default `1.0`) threads through
`build_cut_plan` / `build_cut_plan_comparison` as the optional
`max_removal_frac_required` argument, resolved per call by `_resolve_removal_frac` so
the module constant stays monkeypatchable. It is passed by BOTH callers that build a
plan — `tasks/speech_cleanup_analysis.py` (analysis, also exposed on
`SpeechCleanupAnalysisInput`) and `tasks/generative_build.py` (render) — so an analysis
and the render that replays it can never disagree about the budget. Rollback:

```bash
fly secrets set SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED=0.55 --app nova-video
# + restart api and worker
```

That restores the previous behaviour exactly: the golden asserts a 0.55 run reproduces
the plan production actually shipped (`TR_V1_BASELINE_CAPPED_055`).

**The setting is part of the policy fingerprint.** `current_detector_policy()`
(`services/plan_item_media.py`) now includes `max-removal-frac=<value>`, which is hashed
into every `source_policy_fingerprint`. Flipping the switch therefore behaves like a
`DETECTOR_VERSION` bump: existing `ready`/`no_findings` analyses stop matching and are
re-analysed on the `speech-analysis` queue, consent is re-collected (the creator sees
`SpeechCleanupDecisionCard` again — a consent given to a 55% cut list must not silently
authorise a 100% one), and preflight cohort membership reshuffles, so evidence windows
must not be carried across the flip. That is the point: a budget change IS a plan
change, and a cached plan built under the other budget is not evidence for this one.
Stamped Jobs are untouched and finish under the `cut_plan` they were consented to.

**Pins:** `TestOverBudgetClamp` plus `test_operator_switch_restores_a_fraction_cap` and
`test_min_output_is_the_only_remaining_rail` in `tests/pipeline/test_silence_cut.py`;
the 0.55-rollback assertion in `tests/pipeline/test_silence_cut_asr_timestamp_golden.py`;
budget arithmetic in `tests/tasks/test_generative_build_silence_cut.py`.
Plan context: plans/019 (the clamp itself) and plans/021 (the detector that made the
ceiling visible).

## [2026-09-08] Two guards so rule 0 cannot cut real speech — the cap was the accidental backstop

Removing the 55% cap (entry above) was safe ONLY because of the two guards in this
entry. They were added in response to an adversarial review of that removal, and they
ship with it. Read the two entries as one change: the cap had been silently providing a
speech-safety property nobody had assigned to it, and taking it out without replacing
that property would have shipped a detector that cuts words.

**The blocker.** `silencedetect` thresholds on an ABSOLUTE −30 dBFS floor — the pipeline
already says so itself (`generative_build`: "silencedetect undercounts quiet/lapel
speech"). On a soft-spoken or lapel-mic take it reports quiet SPEECH as silence. Rule 0
(`_reconcile_words_with_silence`) exists to carve FFmpeg silence out of a mis-stamped
ASR token, and it cannot tell that kind of span from a real pause: it carved the quiet
speech, the carve made it cuttable, and **the validator structurally could not object.**
`word_intrusion` is checked against the RECONCILED words, so by the time the validator
runs, the span it should be defending is no longer inside a word. A cut into the
ORIGINAL token is invisible to every downstream check. The 0.55 cap was what had been
holding this back, by accident: over-budget eviction dropped rule-0 carves before they
reached the output.

**Measured on a synthetic soft-spoken fixture** (52.5 s, 10 real 2.2 s words each with a
1.0 s under-read middle — the shape a lapel mic produces):

| | real-word audio removed | token carves |
|---|---|---|
| before the guards, `frac = 0.55` | 1.124 s | — |
| before the guards, `frac = 1.0` | **7.5 s** | — |
| after the guards, either frac | **0.000 s** | 0 |

The real pauses between those words are still cut (>20 s saved). The middle row is what
the cap removal would have shipped.

**Guard 1 — rule 0 carves only a dominated INTERIOR split
(`TOKEN_SPLIT_VOICE_RATIO = 0.5`, `TOKEN_SPLIT_PIECE_MAX_S = 0.35`,
`app/pipeline/silence_cut.py`).** A first attempt gated only the split branch on
the voice ratio; an adversarial re-verification then moved the same under-read
span from a word's MIDDLE to its ONSET and measured 8.575 s of real-word audio
destroyed on the guard's own fixture — worse than the 7.5 s it was written to
prevent — because `trim_head`/`trim_tail` were unchecked, and a seeded sweep
attributed ~94% of all real-speech loss to those edge trims. So the rule was
narrowed to the single shape that cannot plausibly be a word: rule 0 now carves ONE interior span per token and only when both remnants are slivers (`TOKEN_SPLIT_PIECE_MAX_S` 0.35 s) totalling at most `TOKEN_SPLIT_VOICE_RATIO` 0.5 of the carve; edge trims are refused outright because a carve at a token boundary is indistinguishable from a quiet onset, and edge trims were measured as ~94% of the real speech an earlier, looser guard still destroyed. A token
with several quiet patches, a substantial remnant, or a carve at either
boundary is returned untouched. Measured after: 0.000 s of real-word audio cut
on every attack geometry (quiet middle, quiet onset, quiet tail, multi-carve
escape, the 0.05 s sliver discontinuity), and the reviewer's own 2600-layout
realistic sweep goes from 1126 lossy plans to 0.

Cost, stated plainly: the 41 s reference clip (`clip3`) gains nothing any more —
its three extra dead-air removals all came from edge trims, so it renders
exactly as `main` does. Both REPORTED bugs are still fixed: the Turkish
hesitation (which the filler-gated flank change fixes, not rule 0) and the
English clip's 1.4 s hole (one legitimate interior split). Cutting a pause is
worth less than not cutting a word.

**Guard 2 — `MIN_KEEP_SPEECH_SEGMENT_S = 0.6`.** A surviving keep segment that contains
words and is shorter than this reads as a stutter between two jump cuts, not a shot.
Micro-gap hygiene widens it by retreating a neighbouring flexible silence carrier
(`_retreat_from_micro_gap`, `target_gap_s=MIN_KEEP_SPEECH_SEGMENT_S`); it is **never
absorbed**, because the fragment is audio and may be speech. Protected spans never
retreat. This fixed a 0.498 s isolated fragment between two jump cuts on the EN clip;
it costs 0.102 s of removal to buy the fragment up to the floor.

**Shipped numbers, guards in, cap out** — TR is unchanged by the guards; EN gives back
the 0.102 s guard 2 spends on the fragment:

| Clip | removed | output | shortest keep | `clamped` |
|---|---|---|---|---|
| TR (job a75981f8) | 6.855 s | 3.145 s | 0.760 s | true (the `MIN_OUTPUT_S` floor) |
| EN (job d0e284cb) | 5.900 s | 4.114 s | 0.600 s | false |

**Guard tests:** `TestRuleZeroCannotCutRealSpeech` in
`tests/pipeline/test_silence_cut_asr_timestamp_golden.py` —
`test_mumbled_words_are_never_split` (0.000 s of word audio removed, 0 token
adjustments, real pauses still cut), `test_the_guard_does_not_depend_on_the_removed_fraction_cap`
(the plan is byte-identical at `frac = 1.0` and `frac = 0.55`, which is the assertion
that the guard, not the budget, is doing the protecting),
`test_a_token_stretched_over_a_pause_is_still_split` (the EN `5.48` token must still
split — the guard must not disarm rule 0), and
`test_surviving_speech_fragments_clear_the_shot_floor` (every word-bearing keep segment
clears 0.6 s).

**Three smaller fixes from the same review ship with these.**

- `speech_cleanup_max_removal_frac_required` is `gt=0.0`, not `ge=0.0`. Settings accepted
  `0`, the engine input rejected it, so `SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED=0`
  booted clean and then failed every analysis at runtime. A config value that validates
  must be a value the thing it configures accepts.
- The rollback note says restart **api AND worker**. The policy fingerprint is computed
  API-side (`current_detector_policy`), so a worker-only restart leaves the API handing
  out the old fingerprint.
- The render path (`tasks/generative_build.py`) gained the same "never prefer a bailed-out
  candidate over a working baseline" guard the preflight engine already had, and
  `build_cut_plan`'s early-return path no longer drops `max_removal_frac_required`.

**Reverted, deliberately:** a V1 clamp change that reserved protected spans out of the
budget. V1 compares `total_removed` INCLUDING protected spans, so reserving them
double-counts. The reservation stays V2-only.

**Lesson:** when you remove a limit, first find out what it was actually doing. The 55%
cap was documented as a caution rail on removal aggressiveness; what it was ALSO doing
was evicting every rule-0 carve that a −30 dBFS floor had mislabelled, which is the only
reason nobody had seen the detector cut a word. Ask of any constant you are deleting:
which failures does it currently absorb, and who catches them afterwards? And a
validator that checks a derived value is not a check on the input it was derived from —
`word_intrusion` over reconciled words can never see a cut into an original token.

## [2026-09-08] The motion-preview "flake" reproduced perfectly — an absolute ms budget measures the runner, not the code

`motion-preview-performance.test.ts` failed on main (run 34191496370) and on
PR #985 (run 34252147237), a diff that touches no motion, CanvasKit or preview
code. Both reported the same thing:

```
Expected: <= 1
Received:    2
```

It was filed as a flake, and the file already carried the flake fix — a
`jest.retryTimes(2)` added in v0.55.0.2 (#939) whose comment reasoned that "a
real regression is deterministic and fails every attempt". The CI log shows why
that never worked: `RETRY 1` returned 2, `RETRY 2` returned 2, the final attempt
returned 2. Three independent attempts, identical result. The premise was
inverted — the failure *was* deterministic, and retrying a deterministic failure
just pays for it three times inside a 28-minute job.

What the test actually measured: 24 real CanvasKit draws, asserting at most one
over 50ms. Draw cost turns out to be a fixed function of the animation frame,
not noise — profiling the same 24 frames four times in a row reproduces the same
shape every pass (frames 105-115 the most expensive at ~5.4ms, frames 5-25 at
~4.2ms, the rest 0.2-1.9ms). That whole profile scales with machine speed. On an
idle M-series Mac the peak is ~5.4ms; under emulated CPU contention the same
draws take 47-59ms, and the count over 50ms walks 0 → 1 → 3 → 7 as load rises.
The 50ms line runs straight through the middle of a dense cluster of inherent
costs, so the assertion resolved to "is this runner slightly faster or slightly
slower than X" — a hardware speed test wearing a performance-budget costume.

Resolution: measure relative to a calibration workload built from raw CanvasKit
primitives (no motion-runtime code), drawn on the same machine in the same
process, and assert on the ratio. Machine speed divides out; a slowdown in our
drawing code does not. Two refinements came from measurement rather than
intuition, and one plausible idea died there:

- Interleaving calibration with draws, to make both see identical conditions,
  made the ratio *worse* (0.400-0.633 vs 0.423-0.517 phased). Rejected.
- Taking the block with the smallest ratio is biased low — it selects the block
  whose denominator was most inflated, reading 0.31-0.41 under load against an
  idle 0.51, which would mask regressions. Minimising draw cost and calibration
  cost *independently* across 3 blocks removes the bias: interference can only
  make a sample slower, so each minimum is the least-polluted estimate.

The result is load-invariant. Clean runs measure 0.510-0.518 idle, 0.422-0.589
at 3x contention, 0.405-0.513 at 10x; doubling the draw cost measures 0.866-1.149
across all three. The 0.8 ceiling sits in that gap, so the test still fails on
roughly a 1.55x slowdown — verified by injecting a 2x regression into
`drawCreatorBlockV2` and confirming it fails at idle and under both load levels,
while 12 consecutive clean runs across the same conditions all pass.

The general lesson: an absolute wall-clock threshold on a shared runner is a
measurement of the runner. Retrying does not fix it, because the thing being
measured is not transient. Normalise against something measured on the same
machine at the same moment, and pick the threshold from the observed
distribution instead of from a round number.

---

## [2026-09-10] Degrade a drifted creation-thread projection instead of 404ing it (KRI-26)

**Decision:** `GET /creation-threads/{id}` used to withhold the *entire* thread — title,
revision, full chat transcript — the moment any single render-graph edge
(`PlanItem`/`CreatorAgentSession`/`Job` ownership) failed its coherence check, via a bare
`HTTPException(404, "Creation thread not found")`. The web client mapped any 404 on an
explicit thread id to a terminal "Project unavailable — may have been deleted" screen with
no retry. `_load_authorized_projection_rows` now takes a `degrade` flag: read paths (the
GET response, the render-projection sync, and the inert "is it ready?" status-only message
branch) drop only the incoherent tier to `None` and report it via a new `integrity` field,
while write paths (`record_editor_events`, `action_thread`'s confirmation flow) stay
fail-closed. `_load` itself now raises a typed `RuntimeFailure` (`thread_not_found` /
`thread_deleted` — the latter only when an owner-scoped `CreationThreadDeletion` tombstone
exists — / `thread_id_invalid`) instead of an untyped 404, and the client's new
`creationLoadFailureCopy` taxonomy reads that code (plus the proxy's own `code`/`retryable`)
to show a retryable state for a network/service failure and reserve the terminal screen for
a confirmed deletion. Gated by `CREATION_THREAD_DEGRADED_PROJECTION_ENABLED` (default `true`
— off reproduces the pre-fix behavior byte-identically). A read-only
`GET /admin/creation-threads/{id}/integrity` reuses the exact same predicate so the operator
view can never say "coherent" for an edge the API still hides.

**Why:** the fail-closed 404 was designed to stop a stale/cross-linked projection from
re-signing another user's render URL — a real security property, but it was implemented by
discarding the *entire* response instead of only the tainted edge, and conflated "this
specific ownership edge doesn't check out" with "this thread doesn't exist." Two review
passes (the design review and an adversarial follow-up) surfaced three ways the naive
"just don't raise" version would have made things worse instead of better: (1)
`_render_projection`'s job-branch fell back from `session=None` to the raw
`thread.active_creator_agent_session_id` — re-adopting into a *committed, client-visible*
projection the exact id the fence had just rejected; (2) `_sync_agent` copied a
`CreatorAgentSession`'s transcript into the thread's own state/events with **no** ownership
check at all — previously masked because the 404 upstream of it always discarded the
response; degrading `_response` would have made that leak visible; (3) the client infers
"still rendering" from `active_job_id` being set with no matching `job` projection, so
leaving a detached `active_job_id` on the wire would have turned a stuck project into an
infinite poll loop. All three are fixed alongside the degrade change, not left for later.

**Revisit if:** a future edge is added to the render graph and its own coherence check needs
the same drop-not-discard treatment — extend `_load_authorized_projection_rows`'s pattern,
never bolt on a parallel ad hoc check. If `_sync_agent` or `_render_projection` grow a new
caller, re-verify neither the ownership fence nor the never-re-adopt-a-rejected-id rule can
be bypassed from that call site.

## [2026-09-16] Creator manifest membership must not depend on a Visual's analysis state (prod thread 168b17ec)

`resolve_item_creator_context` used to include pool assets (Visuals) only once
`PlanItemAsset.status == "ready"`, which the `autoplace` worker sets after Gemini image
analysis. Nothing on the chat planning path waits for that. A user who added six photos and
sent the prompt 105 s later got a direction whose `manifest_hash` covered only the five video
clips (the stored strategy's `selected_media_ids` had no `asset-…` entry); by the time they
tapped "Create this video" the photos were ready, the live manifest had eleven media, and
`confirm_creator_plan_controller` answered 409 "Footage or capabilities changed; review the
plan again" on every attempt. iOS renders that as "Connection interrupted / This project
changed. Review the latest options and try again." and re-shows the same proposal; the web
shows a generic "I couldn't start that render". Neither client can re-plan from that state,
so the proposal was permanently unconfirmable. Confirmed by recomputing the manifest hash in
the prod API container (stored `8fb408e4…` vs live `f6431961…`, plan format montage on both
sides).

Fix: the manifest now admits every registered state (`CREATOR_VISIBLE_ASSET_STATES` =
uploaded/queued/analyzing/ready) and keeps reservations, cleanup claims and failed rows out.
Analysis evidence is still handed to the planner only for ready rows; pending rows carry
`"analysis_status": "pending"` in `media_context` so the model knows the file exists. This
follows the rule already encoded in `canonical_manifest_hash` ("hash confirmation identity
without asynchronous analysis observations"): readiness is such an observation. The
proposal build already tolerates queued/analyzing assets (`edit_proposal_build` retries every
15 s), so dispatching a plan that references a not-yet-analyzed Visual is safe.

**Why:** the alternative fixes were worse. Blocking the chat turn until analysis finishes
turns a normal upload race into a user-visible 409 that neither client explains; auto
re-planning inside the confirm route when only newly-ready assets differ adds a second
planning state machine to the most fence-heavy route in the API. Changing what the hash
covers is one predicate and one test.

**Revisit if:** a Visual can legitimately change identity after registration (kind is fixed
at registration from content type today; `deduplicated_to_asset_id` is decided at
registration too). If either becomes post-registration, the same drift returns through a
different field. Also revisit the client dead-end: iOS and web should surface the 409 detail
and offer "refresh the direction" instead of re-showing a proposal the server will never
accept (tracked as a follow-up, not part of this change).

## [2026-09-23] Narrated phone render: spoken lines are not labels; label lanes draw like cloud; cleanup receipts only for cleanup failures (prod job 76db6913, PR #1185)

An iPhone narrated story (`guided_voiceover_v1`, cleaned voiceover) failed with
`phone_plan_unsupported` / "sequence effect needs composite-stream parity". The Main Creator had
read the creator's shot/spoken-line script ("show each shot while I say its line: <shot> ·
<line>") as transcript topic-label requests; the annotator grounded the first 13-word sentence,
and `narration_labels._append_element` emitted it as a `generative_sequence` pop-in over the
title, duplicating the voiceover caption. Decisions:

- Topic labels must be label-shaped (the visual label limit, or a proper name up to 5 words /
  32 chars). A grounded spoken sentence is rejected with a receipt reason. Captions already carry
  every spoken word; a sentence-length "label" is duplication, never a label.
- The phone compiler shares the cloud's label-lane role assignment (`_assign_label_lane_roles`),
  so context/narration labels are independent overlays on both renderers and a pop-in label can
  no longer fail a device render. Text-lane sequence effects still fail closed. (PR #1183 fixed
  the crash by checking the compiled layer's fade kind instead; it was closed in favour of
  #1185, which also carries its iOS `verify.sh` simulator boot-race fix.)
- A speech-cleanup receipt says `failed` only for a cleanup-attributed failure (the
  `or "internal_error"` fallback from #968 mislabelled every failed required_v1 Job, cloud
  included); a read-side fence hides already-mislabelled receipts.
- Consent follows the card, not the attempt: generate/retry with no explicit choice re-send the
  analysis row's recorded `clean`/`keep_original`; `create_without_cleanup` on a finished check
  is refused before an attempt is reserved; a dispatch refused after a successful draft fails
  the attempt instead of leaving the session `executing` ("preparing your footage" forever).

**Revisit if:** topic labels need multi-word non-name phrases (widen the shape rule with a
creator opt-in), or the phone gets a sequence compositor.

---

## [2026-09-23] Pool-asset analysis retries transient Gemini failures instead of going terminal

On 2026-09-23 15:27-15:29 UTC, 16 of 17 Gemini `generateContent` calls on the autoplace machine
returned 503. `analyze_pool_asset` was single-attempt: `_analyze_image`/`_analyze_video` wrapped
the 503 as `AnalysisTemporarilyUnavailableError`, the task persisted a terminal `failed` row with
`analysis_attempt_count=1` and returned success to Celery. The reaper never re-dispatches
`failed`, so every Visual in the iOS Add-media sheet stayed "Failed" until a manual Retry tap.
Decisions:

- Same pattern as `draft_edit_proposal`: `bind=True` and `self.retry(countdown=15,
  max_retries=4)`, but the retry fires only when the failure's underlying cause is classified
  transient, whichever wrapper it arrives in. `_is_transient_analysis_failure` walks the explicit
  `__cause__` chain (never implicit `__context__`, so a bug raised while a 503 is being handled,
  such as a failing `release_paid_call`, is not laundered into a retry) and reuses the existing
  classifiers: `_is_genai_transient` (5xx / 429), the agent
  runtime's `TransientError`, and transport errors (dropped connection, read timeout). A
  quota-shaped 429 anywhere in the chain is never transient. `_analyze_image`/`_analyze_video`
  wrap every provider-region failure as `AnalysisTemporarilyUnavailableError`, including
  deterministic bugs (post-processing `AttributeError`, malformed model JSON); those stay
  terminal on the first invocation instead of burning 4 more Gemini calls.
- Retries stop at whichever comes first: `max_retries`, or a 6-minute wall-clock budget from the
  attempt's first invocation (`_POOL_ANALYSIS_RETRY_BUDGET_S`, carried in a Celery header like
  the attempt token). A video invocation already retries internally and can near the 240s soft
  limit, so the count alone could keep a Visual `analyzing` for ~20 minutes, past the reaper's
  10-minute `analyzing` window. Either stop falls through to the unchanged terminal persist, so
  the exhausted contract (`analysis_temporarily_unavailable`, `error_retryable=True`, same
  `error_detail`) is byte-identical.
- The row stays `analyzing` across retries. A retry re-enters its own attempt under the same
  attempt token, re-arms `analysis_started_at` once the retried run actually starts (the
  countdown and queue wait still count against the previous run), and exits without work if
  the row is no longer `analyzing` (e.g. the reaper terminalized it).
- Each retry uses a distinct paid-call ledger key (`request_id` suffix `:retryN`). Reusing the
  first invocation's key is refused once that call is `settled` or `unknown`, which would turn a
  retry into a non-retryable `provider_outcome_unknown`. The first invocation keeps its old key,
  so broker redelivery stays idempotent.
- No client-wide `HttpRetryOptions` on the shared `genai.Client`: it would stack with the
  existing manual upload/poll retry loops and the agent runtime's own attempts.
- `_AUTOPLACE_TASK_LIMITS` (240/300) is unchanged. Each retry is a fresh invocation, so the
  time-limit < `visibility_timeout` invariant holds per attempt.

Guards in `tests/tasks/test_autoplace_tasks.py`:
`test_analyze_pool_asset_transient_gemini_error_retries_instead_of_terminal`,
`test_analyze_pool_asset_deterministic_failure_is_not_retried`,
`test_analyze_pool_asset_retry_budget_exhaustion_persists_terminal_failure`,
`test_video_analysis_failure_is_classified_by_its_cause_chain`,
`test_analyze_pool_asset_bug_with_implicit_503_context_is_not_retried`, and
`test_analyze_pool_asset_persists_safe_retryable_failure[transient_retries_exhausted]`.

**Revisit if:** Gemini outages routinely outlast ~1 minute (lengthen the countdown or add
backoff), or GCS download errors start stranding uploads (google-cloud-storage errors are not
classified transient, so a failed download is still terminal on the first failure).

---

## [2026-09-24] Browser motion benchmark: interleaved samples and a ceiling recalibrated from CI (0.8 → 0.7)

`e2e/motion-preview.spec.ts` › "the calibrated budget catches a doubled draw workload"
failed 4/4 attempts on PR #1197, a diff that never touches the page. It was not new. In
the 87 Mobile E2E runs before it, the 2x ratio read ≤ 0.8 on **33 first attempts
(38%)**; the Playwright retry hid most of them, and 7 of 33 retries failed too. 1x read
0.43-0.55. 2x read 0.64-1.14 (median 0.85, sd 0.098).

**Root cause: the 0.8 ceiling was calibrated on arm64 and judged on x86.** The 2026-09-08
envelope (clean 0.41-0.59, doubled 0.87-1.15) was measured on an M-series Mac. The
Mobile E2E runners are 4-vCPU AMD EPYC (9V74 / 7763; `nproc`=4, 2 cores × SMT). There a
clean run reads ~0.47 and a doubled workload ~0.87. So on CI the "doubled" check sat only
~0.07 above the ceiling, and the 2x test's noise was enough to cross it. The 2x check was
reporting something true: on CI hardware, 0.8 only caught a ~1.7x slowdown, not the
~1.55x it was designed for.

**Contributing: phased sampling widened the spread.** Playwright's 2 workers run the 1x
and 2x pages at the same moment. Each page timed 24 calibrations, then 24 draws, per
block, so a busy stretch could land on one side of the ratio only. A same-VM A/B on CI
(4 runs, 1x+2x concurrent, `--repeat-each`) gave these results. Phased: 1x 0.40-0.62,
2x 0.68-1.12 (sd 0.093), 2x ≤ 0.8 on 6/42. Interleaved, one sample per frame with the
order alternating: 1x 0.43-0.53, 2x 0.78-1.01 (sd 0.046), 2x ≤ 0.8 on 3/47. Interleaving
halves the spread but does not move the centre (per-VM 2x medians 0.84-0.95 phased,
0.85-0.89 interleaved). My first
hypothesis, that phasing *biased* 2x low, was wrong. It only widened the tails.

**Not the flush.** `?benchmark=2x` repeats `drawMotionFrame` but flushes once. The CI A/B
also ran a flush-per-repeat variant: 2x medians 0.85-0.88, no different. The flush stays
once per sample, which is also what a real 2x regression in `drawMotionFrame` would look
like.

**Fix:** interleave the browser samples, and set the browser ceiling from the CI envelope.
Interleaved CI samples: clean 0.393-0.584 (n=77, counting the flush variant, which runs
identical code at 1x), doubled 0.777-1.006 (n=47). 0.7 sits in
that gap: 1.20x above the worst clean sample and 1.11x below the lowest doubled one. The
doubled side gets more room because its sd is larger. That catches a ~1.5x slowdown on CI.
The worst clean sample came from a block where the runner went briefly quiet:
calibration sped up 30% but the draw only 12%. On x86 an idle runner pushes the ratio up
and heavy contention pushes it down, so the ceiling has to clear both tails. Locally
(arm64) the browser reads 1x 0.49-0.55 and 2x 0.96-1.00. The spec now logs per-block
draw/calibration costs, so the next excursion can be diagnosed from the CI log alone.

**Why the Jest twin keeps phased sampling and 0.8.** The 2026-09-08 entry rejected
interleaving on measured evidence, and it reproduces on an M-series Mac. With 36 busy
loops (3x load), back-to-back interleaved samples in Node read 0.16-1.14 (median 0.86), so
the 1x test would fail; phased read 0.30-0.73. The cause is the hybrid CPU. Confined to
efficiency cores only (`taskpolicy -c background`, 2x load), interleaved reads 0.46-0.52
and phased 0.42-0.50. Under load the thread migrates between P and E cores, which differ
2-3x in speed. Back-to-back alternation splits each side across both, and median
(calibration) against trimmed mean (draw) reads that split unevenly. The browser page waits
a frame before every sample, and its renderer outranks background load. Both variants read
0.49-0.51 / 0.96-0.98 there under the same 3x load. The Node check reads 0.46-0.54 on CI and
up to 0.73 on a loaded Mac, and it has no doubled-workload check, so 0.8 stays.

**Lesson:** a ceiling tuned where the code is developed is not the ceiling where it is
judged. The 2026-09-08 comment said so ("the envelope above is arm64, CI is x86_64 … so the
ceiling can be tightened from real CI samples"), and nobody closed that loop until a
canary started failing. Also, check a noise hypothesis against a same-machine A/B before
believing it: the local contention studies here were dominated by hybrid-core scheduling
and by QoS, neither of which exists on the runner.

## [2026-09-24] Rolling deploys took both api machines down at once (KRI-193, found via KRI-180)

`fly.toml`'s `[[services]]` block for `api` set `min_machines_running = 1`, with a comment
explaining that the floor made autostop a no-op for "current single-replica prod" and would
need raising "if we ever add a second api machine." Prod WAS scaled to a second machine
(`fly scale count api=2`, confirmed live via `fly scale show`) at some point outside of a
commit — `git log --all --grep` on scale/api turns up nothing — and the floor was never
bumped to match. `auto_stop_machines = "stop"` was then free to idle the second machine down
to 0 during low traffic, same as it always had for the single-replica case the comment
described.

That silently reintroduced the exact race the floor was written to prevent, one layer up:
`gh run view 35983295511` shows both api machines (`e78413def29018`, `843277b2423298`)
logging "Updating machine config" within the same second at 09:48:22 UTC (release v1267,
repeated ~10 min later at v1268). Going into that deploy only one of the two was actually
running — the other had already autostopped — so replacing the sole running machine left
zero healthy `api` machines for ~18s (`instance refused connection`, `machine was recently
stopped`). Any upload reserve/attach landing in that window failed; KRI-180 was filed at
09:58 chasing exactly that. KRI-180's "couldn't be processed" symptom is a separate
client-side check and isn't explained by this — the outage window only explains the reserve/
attach failures.

**Fix:** `min_machines_running` raised from 1 to 2 to match the actual `fly scale count
api=2`. This makes autostop a no-op for the current fleet size again (mirrors the original
single-replica reasoning, just at the current count), so both api machines stay running
between deploys and a rolling update of one always has a healthy peer. The comment now says
to track the actual scale count rather than assume a specific replica count.

**Lesson:** an invariant written as "assume N replicas, revisit if we ever add more" needs
the "if" to actually get caught — `fly scale count` is an out-of-band operational change
that doesn't touch git, so nothing forced fly.toml to be revisited when prod's replica count
changed. There's no automated check tying `min_machines_running` to the live scale count;
that drift is now findable only by reading this entry or noticing another `[[services]]`
comment says "single-replica" while `fly scale show` says otherwise.

## [2026-09-24] CLAUDE.md flag detail moved out to stay under the 38k budget (KRI-185)

CLAUDE.md hit 38,000/38,000 chars while KRI-185 lanes each needed a flag line. The eight longest env-var lines were shortened to invariants + guard names; their full original text is preserved verbatim below.

- `EDITORIAL_SEQUENCE_ENABLED` — defaults to `true`. Editorial (cluster) variants with audible, coherent original speech render the transcript-synced typographic sequence (`phrase_sequence.py` + `SequenceEmphasisAgent` + `EDITORIAL_STYLE`); otherwise RHYTHM MODE paces an agent-authored quote (`SequenceQuoteWriterAgent`; agent failure ⇒ static cluster, never heuristic). `false` ⇒ legacy single static cluster, byte-identical. Guards: `tests/tasks/test_generative_build_sequence.py`. Apply: `fly secrets set EDITORIAL_SEQUENCE_ENABLED=false --app nova-video` + `fly machine restart <id>`.

- `SOUND_EFFECTS_ENABLED` / `MEDIA_OVERLAYS_ENABLED` — both default **`false`**. Gate the SFX-lane and overlay-lane write/render routes in `routes/plan_items.py` (404 when off); public `GET /sound-effects` stays ungated (picker loads). Caption archetypes (subtitled/narrated) carry both lanes; every caption reburn re-applies persisted lanes (`docs/pipelines/generative.md`). **Dual-flag trap:** frontend lanes use the `NEXT_PUBLIC_` twins (Vercel); frontend on + backend off ⇒ saves 404. Keep Fly + Vercel in sync. Apply: `fly secrets set SOUND_EFFECTS_ENABLED=true --app nova-video` + `fly machine restart <id>` (api + worker).

- `FULLSCREEN_CUTAWAYS_ENABLED` — defaults **`false`**. Gates the AI fullscreen branch (`build_suggestions` slot `"full"` → `display_mode="fullscreen"` cover-crop takeover). Dual-flag with `NEXT_PUBLIC_FULLSCREEN_CUTAWAYS_ENABLED` (Vercel; gates the MANUAL promote affordances): **Fly first, then Vercel** — new web + OLD api silently bakes manual fullscreen as pip. Render rollback = `MEDIA_OVERLAYS_ENABLED`. Guards: `tests/test_overlay_fullscreen_rules.py`, dual preset pins in `tests/test_media_overlay_command.py`. Narrative: agents/DECISIONS.md "Kill-switch incidents"; plans/009.

- `SUBTITLED_TEXT_LANE_ENABLED` — defaults **`false`**. Styled-text lane on subtitled variants: text burns (Skia) onto the caption-free base FIRST, captions LAST via `_compose_subtitled_final`; every fast-reburn mints a NEW GCS key + deletes the old (CDN staleness). Dual-flag with `NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED` (Vercel). Fly first, then Vercel. Apply: `fly secrets set SUBTITLED_TEXT_LANE_ENABLED=true --app nova-video` + `fly machine restart <id>` (api + worker). Internals: `docs/pipelines/generative.md`.

- `SUBTITLED_ARCHETYPE_ENABLED` — **ON in prod** (code default `false`). Gates the subtitled single-clip edit style (talk-to-camera clip → auto-language captions, editable, sentence-per-cue; TR/EN). Off ⇒ montage. Dual-flag with `NEXT_PUBLIC_SUBTITLED_ENABLED` (Vercel, also ON) — build-time inlined, so a change needs a `vercel --prod` rebuild. Companions: `SUBTITLED_CAPTION_CORRECTION_ENABLED` (default `true`) + `CAPTION_CORRECTION_MODEL` (default `gpt-4o`). Rollback: `fly secrets set SUBTITLED_ARCHETYPE_ENABLED=false` (api + worker) + `vercel env rm NEXT_PUBLIC_SUBTITLED_ENABLED production` + `vercel --prod`.

- `NARRATED_SELF_NARRATION_ENABLED` — defaults **`false`**. Narrated items generate WITHOUT a recorded voiceover when the footage's own audio carries the voice: 1 clip → `subtitled` (captions), 2+ → `talking_head` (speech spine); no speech → montage + reason persisted on `assembly_plan["archetype_fallback"]` (item-page banner). SOLE gate — deliberately bypasses the two archetype flags above. Dual-flag `NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED` (Vercel); flip Fly first. Voiceover, when recorded, still wins (narrated archetype unchanged). Guards: flag-off pins in `tests/tasks/test_generative_dispatch.py`.

- `POSTER_ONDEMAND_REPAIR_ENABLED` / `POSTER_REPAIR_QUEUE` — default **`false`** / `celery`. `POST /me/jobs/posters/refresh` stops being a pure re-signer and enqueues `tasks.repair_job_poster` (`app/tasks/poster_repair.py`) to mint a missing library poster; off is byte-identical. **Set the queue FIRST** — the task downloads a full MP4 + runs ffmpeg, so prod needs `autoplace-jobs` (2GB), never the 1GB `light`/Beat machine. Guards: `tests/tasks/test_poster_repair.py`, `tests/routes/test_me_jobs.py`. Narrative + apply order: agents/DECISIONS.md "Storage retention incidents".

- `SILENCE_CUT_ENABLED` / `RETAKE_CUT_ENABLED` — default `false`; speech paths only, fail-open. Removal-cap lever `SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED` (default `1.0`) + `DETECTOR_VERSION` are in the policy fingerprint — a flip retires analyses, re-consents, reshuffles cohorts. **"cleanup cut a word" is a guard bug, not this lever** — use the kill switch. Full narrative + triage: `docs/runbooks/chat-speech-cleanup-rollout.md`. Pins `test_silence_cut*.py` (`TestRuleZeroCannotCutRealSpeech`); plans/010/019/021.

Moved out of CLAUDE.md 2026-10-06 (KRI-470 PR-A, to make room for `KRIA_PLAN_AUTHORITY_ENABLED`); the full text of these two env-var lines, preserved verbatim:

- `RECONCILE_STUCK_VARIANTS_ENABLED` — default `true`. Kill switch for the stuck-variant watchdog (`reconcile_stuck_variants`), which WRITES user-visible state. **Invariant:** every reaper jsonpath is param-bound and run against real Postgres by `tests/tasks/test_reaper_jsonpath.py`; migration 0099's index predicate must match `_STUCK_VARIANT_JSONPATH`. Apply: fly secret `…=false` + worker restart.

- `NARRATED_SELF_NARRATION_ENABLED` — default **`false`**; narrated items without a voiceover use the footage's own audio (1 clip → `subtitled`, 2+ → `talking_head`, no speech → montage + `assembly_plan["archetype_fallback"]`). SOLE gate, bypasses the archetype flags. Twin `NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED`; Fly first. Guards: `tests/tasks/test_generative_dispatch.py`.

Also moved out of CLAUDE.md 2026-10-06 (KRI-470 PR-A), verbatim:

- `IOS_DEVICE_ONLY_MODE` / `KRIA_MINIMUM_CLIENT_PROTOCOL` / `CLOUD_RENDER_EXECUTION_ENABLED` — defaults `false` / `2` / `true`. The staged cutover retires web/cloud creation for protocol-2 native clients, then disables cloud execution only after drain verification; API and workers must share the flags. Runbook: `docs/runbooks/ios-device-only-runtime.md`.

- `SUBTITLED_ARCHETYPE_ENABLED` — **ON in prod** (code default `false`); subtitled single-clip style, off ⇒ montage. Dual-flag `NEXT_PUBLIC_SUBTITLED_ENABLED` (Vercel, ON, build-time inlined ⇒ needs `vercel --prod`). Companions: `SUBTITLED_CAPTION_CORRECTION_ENABLED`, `CAPTION_CORRECTION_MODEL`. Rollback + detail: DECISIONS.md "CLAUDE.md flag detail".

- `SILENCE_CUT_ENABLED` / `RETAKE_CUT_ENABLED` — default `false`; speech paths only, fail-open. `SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED` + `DETECTOR_VERSION` are in the policy fingerprint. **"cleanup cut a word" is a guard bug, not this lever.** Runbook: `docs/runbooks/chat-speech-cleanup-rollout.md`; pins `test_silence_cut*.py`; plans/010/019/021.

- `NARRATIVE_CLIP_ORDER_ENABLED` — defaults to `true`. Plan-item edits follow the filming guide's shot order (narrative mode in `template_matcher.match`; dispatch contract in `content_plan_build._narrative_clip_order`). Read at render time (affects queued jobs). `false` ⇒ pure greedy matching. Apply: fly secret + restart.

- `TIKTOK_DEEP_ANALYSIS_ENABLED` — defaults to `true`. When false, `scrape_tiktok_profile` skips chaining `analyze_tiktok_profile` and persona/plan/hook prompts receive no TikTok analysis block (byte-identical to pre-feature). Apply: fly secret + restart.

## [2026-09-24] One montage plan: phone montage compiles through the guided fast-montage format (KRI-190)

Context. A v2 phone approval reaches the worker with no `guided_edit`, so the plain
phone-montage lane ran. It rejected every portrait-canvas job at the item's default
`landscape_fit="fit"` (job 94c4c865, `phone_plan_unsupported`), ignored the brief, and
could not take a chat text edit.

Decision. Behind `MONTAGE_UNIFIED_PLAN_ENABLED`, build the guided plan in the worker
deterministically (`app/pipeline/unified_montage.py`) and reuse `_run_phone_guided_job`,
instead of running the item-locked `draft_edit_proposal` Celery flow (LLM planner,
async approval, second Job mint) inside the render worker. The planner reuses
`FastMontageCut`, `EditProposalSnapshot`, the strict compiler and the P3 capture
ordering; the new logic is only order/label/reading-time/title. Per-clip text is a new
snapshot field (`clip_labels`) rather than `montage_text_bindings` because bindings cap
at 12 sources, are suppressed by an opening title, and `shot_labels` are ignored for
fast montage.

Consequences. Readable text beats a requested length; an ungrounded label is dropped
and the receipt says partial. A montage with nothing to title with is titled with the
`Montage` (never a model-authored hook, never unrequested place text). The unified
`guided_edit` carries no minted `generation_attempt_id`: a v2 session that dispatched
without a proposal has none, and the render projection drops a job whose attempt id
differs. Deliberate deviation from the wave-2 amendment: the planner is a new
deterministic one rather than `draft_edit_proposal` (item-locked, async Celery flow).
Behaviour change vs the plain lane: no matched music bed, beat-snap or hero intro; the
render keeps source audio only. Fraunces lacks
"→", so the planner selects a font that covers every string. Old lane deletion is a
follow-up after a device visual comparison.

## [2026-09-25] Draft-time receipts defer to the unified montage planner; a country alone is not a place (KRI-190)

Context. The first simulator and phone runs of the unified montage both rendered well, but the
chat showed "Not everything you asked for made it in: Couldn't: the name of the place as text
(None of the 14 clips got its own text in this draft)" before the render, and it stayed on
screen after the video had 14 labels. The strategy draft is checked by
`plan_facts_from_strategy`, which has no per-clip text, order or timing, because the unified
planner writes those at render time. The render's own `assistant_review` already carries the
true receipts and the "I guessed these, tell me if any is wrong" list. Separately, two clips were
captioned "Türkiye": a geocode that found nothing finer than the country became a place fact.

Decision. (1) `requirements_to_check_at_draft` leaves `text`, `order` and `timing` out of the
draft-time check when the render will go through the unified planner, so the draft reply is the
plain summary. The test (`defers_to_unified_montage`) mirrors the worker's fork, not the item
row: some clip is a phone analysis proxy, the account is enrolled, `montage_unified_plan_for`,
a montage-family format, and `audio_strategy` is `original_audio` or `licensed_music`
(approval sends anything else down the voiceover lane, which builds no render receipts, so it
must still be judged at draft time; an absent strategy also is). `audio`/`style`/`select` are
still judged at draft time and every flag-off path is unchanged. (2) `capture_facts` no longer
emits a place fact for a country alone (no `sub_locality` and no `locality`). It is decided
there, where the parts are still separate, so a city-state whose locality shares its country's
name ("Singapore") keeps its label; the planner's `_place_label` is unchanged.

Consequences. The only receipts the creator sees for a unified montage are the render's. A clip
whose only place was a country now goes through the existing unlabelled-clip handling (dropped,
receipt says partial). Facts already stored on earlier threads still hold the country-only place
until those clips are re-attached. If the render's brief version changes after planning, its
receipts are discarded (`_unified_montage_review`), which now leaves nothing at all for the
deferred kinds; that needs a brief update between plan and review and is rare.

## [2026-09-25] Speech cleanup's phone gate narrows to apply-only (KRI-205)

Context. Talking-to-camera (`subtitled`/`talking_head`) never showed the speech cleanup
question or check on iPhone. `preflight_enabled_for_source` (KRI-118 L1 item 1) refused to
even schedule a `SpeechCleanupAnalysis` whenever the resolved narration source's storage path
was a phone `analysis-proxy-*` object, on the premise that "speech cleanup needs the real
audio, and a phone project never uploads it." That premise only ever held for *applying* a
cut: the detector (`analyze_speech_cleanup`) reads nothing but a downmixed 16kHz mono WAV, and
an iOS analysis-proxy's audio track is already a faithful, full-duration copy of the original
(`MediaSourceContract.analysisProxy` rejects a duration/audio-presence mismatch before upload;
`_run_phone_subtitled_job` already trusts this exact file for word-level Whisper caption
transcription in production). So the one iOS journey whose narration source is always an
embedded, phone-proxied clip — Talking-to-camera — could never enter the cohort, while a
Narrated item with a fully-uploaded recorded voiceover could.

Decision. `preflight_enabled_for_source` no longer takes a `storage_path`/no longer branches on
source type at all — a phone source is scheduled, projected as `applicable`, and enforced on
generate exactly like a cloud one. The other half of KRI-118 is untouched and now carries the
whole invariant on its own: `content_plan_build._speech_cleanup_dispatch_snapshot` still refuses
`choice == "clean"` outright whenever the active source is an analysis proxy, because applying a
cut on an embedded-spine source means re-encoding real video frames
(`generative_build.py`'s `reframe_and_export` cut path), and a phone-rendered project never
uploads the full-resolution video — only its audio, via the proxy. That refusal is no longer a
defense against a stale/forged request; it is the first-class, expected outcome the very first
time a phone Talking-to-camera creator taps "Clean up speech and create". Both clients already
had a graceful path for it before this shipped: iOS's `nonRetryablePhoneGateErrorCodes` already
listed `speech_cleanup_unavailable_on_phone` and renders the server's own sentence instead of a
generic error, and web's generic mutation-failure copy ("I couldn't start that video. Your
project and speech choice are safe—try again.") is safe, non-crashy, and leaves the decision
card's "Keep original speech" option live to retry with. Neither needed a code change.

Consequences. A phone Talking-to-camera creator now sees real findings and can pick "Keep
original speech" to proceed, or "Clean up speech" to learn (via the existing message) that it
can't run on iPhone yet and fall back to the same card. No cut is ever actually applied for a
phone-sourced item — that remains a real, separate gap: applying one would need either a new
on-device frame-accurate trim in `KriaMediaEngine` (there is none today) or uploading the
full-resolution video, which would break the "phone renders stay on device" rule this project
already treats as non-negotiable. That's tracked as a distinct follow-up, not attempted here.
Self-narration phone journeys (narrated format, no separate voiceover, multi-clip embedded
audio) go through the identical `embedded_spine` path and get the same benefit/limit, though
KRI-205 was reported specifically for Talking-to-camera.

## [2026-09-28] Runtime-v2 montage with Visuals: the unified phone montage places them instead of refusing (KRI-217)

Context. Every iOS montage that included a Visuals photo answered "I couldn't start the
render. Your draft is still saved, so you can retry..." (thread 6BF1213E: 6 phone videos,
2 ready photos, 1 abandoned upload reservation). `execute_kria_approval` dispatches every
v2 approval with `bypass_guided_edit_gate=True`, and the bypass re-count from P2-4
(2026-08-18) refuses any item with a pool row, because its original caller
(draft_edit_proposal's fallback) renders a clip-only montage. The KRI-190 unified plan was
clip-only too, so the refusal was correct about the renderer, but the planner had already
promised the photos and the reply offered a retry that could never pass. iOS picks v2
whenever offered and the server offers it to every account. No v2 test had Visuals, and
`tests/_prod_profile.py` predated the v2 and unified-plan flags.

Decision. Extend the unified planner rather than add a guided-proposal step to v2: ready
Visuals become `lane="asset"` fast cuts spread between the clips (the montage opens on a
clip), which `_run_phone_guided_job` already binds and draws (KRI-121). The dispatcher
counts only the creator's Visuals for a v2 approval (manifest-visible states; a
reservation or a failed photo never reached a plan), lets the unified lane through,
refuses while one is still uploaded/queued/analyzing (`visuals_processing`), and keeps
refusing on clip-only lanes. Both refusals get their own copy and `recovery: ask_user`.
The non-v2 bypass callers stay byte-identical (any pool row refuses).

Replaying the thread's real media through the planner then found a second, older bug
on the same path: whole frames stop a fraction of a frame short of a clip under 0.4s
(the thread's 0.298s IMG_4327), and the strict snapshot only accepts such a clip shown
whole, so the unified plan raised. A clip under `MIN_VIDEO_CUT_S` is now shown whole.

Consequences. Photos keep upload order and hold 1.2s (3s cap); they carry no capture time,
so a "filmed order" brief lists them as fallback. Positional `shot_labels` follow
on-screen order, photos included. A cloud-rendered v2 montage still cannot place Visuals;
that needs v2 guided execution in the cloud worker (follow-up on KRI-217). The unified
lane still renders source audio only (KRI-190), so a plan that promises "an upbeat track"
does not get one on the phone. Still open on the unified planner: per-cut rounding can
leave a montage that lands exactly on the 3s floor at 2.999s, which the snapshot refuses.

## [2026-09-29] Two montage writers by design: voiceover montages -> voiceover montage writer; everything else -> unified (KRI-220)

Context. KRI-190 put every non-voiceover phone montage on the unified planner behind
`MONTAGE_UNIFIED_PLAN_ENABLED` (ON in prod) and left the plain lane in place, with a
follow-up to delete `compile_phone_montage_plan`. Inspection showed it was not dead code:
it is the only phone writer for a montage with a recorded voiceover.

Decision (founder, 2026-09-29). Keep it, rename and narrow it. `compile_phone_voiceover_montage_plan`
(`app/pipeline/phone_voiceover_montage_plan.py`) and `_run_phone_voiceover_montage_job` handle ONLY the
montage-family `voiceover` archetype (`voiceover_only` / `voiceover_music`) and fail closed
(`narrationAudio`) without a narration bed. The worker forks on `voiceover_gcs_path`; every
other montage goes to the unified planner with no flag. `MONTAGE_UNIFIED_PLAN_ENABLED`, the user
allowlist and `montage_unified_plan_for` are removed (prod value confirmed `true` via
`GET /admin/kria/phone-render-config` before removal, so there is no behaviour change in prod).

Why two writers. The unified planner cannot yet mix a recorded voice with a low music bed,
trim to a long voice, or carry the intro hook; the voiceover writer does all three.

Reversal condition. Delete the voiceover writer once the unified planner gains voice + music
mix and long-voice trimming and a device comparison shows parity.


## [2026-10-01] Rule 0 may carve the stretched tail of a sentence-final token — the one edge trim (KRI-236, job 62716037)

Context. After KRI-234 (`mixed-gap-v3`) the night-rain TR Talking take still kept ~1.0 s
of dead air after "alakalı.". whisper stamped the token 5.84 → 6.86 s; the 50 ms RMS
envelope puts the voice end at ~6.15–6.20 s, and the ambient span is 6.23 → 7.12 s with
"Abi" at 7.26 s. The stamped word gap (0.40 s) is under `MAX_PAUSE_S`, so rule 3 never
looked, and rule 0 refused the carve because it sits on the token's edge. Edge trims are
refused on purpose: they were ~94% of the real speech an earlier, looser guard destroyed
(2026-09-08 entries above).

Decision (2026-10-01, proposed in KRI-236). Allow exactly one edge-trim shape, V2 only
(`_sentence_final_tail_carve` in `app/pipeline/silence_cut.py`, diagnostic kind
`trim_sentence_tail`). All of these must hold:
the token ends in `. ? !` and not in an ellipsis (`a...` is a trailing-off hesitation);
it overlaps exactly ONE long (>= `TOKEN_SILENCE_MIN_S`) silence span, by at least
`TOKEN_SILENCE_MIN_OVERLAP_S`; that span starts >= `SENTENCE_TAIL_MIN_VOICED_S` (0.3 s)
after the token's start (start times are reliable, D16); and it covers the whole stamped
tail and ends within `SENTENCE_TAIL_REACH_S` (PAD_S + the 80 ms ambient edge guard) of the
next word's start, without overlapping it. The token is shortened to the silence start, and
rule 3 then tightens the pause like any other, keeping `KEPT_GAP_S/2` on each side.
`DETECTOR_VERSION` → `mixed-gap-v4`.

Why it is safe enough. A quiet trailing syllable that the floor still hears leaves sound
between the silence and the next word, so the reach check refuses it. Several quiet
patches mean a mumbled word, which is refused. Starting the silence close to the token's
start means the word has no real voiced length, which is also refused. The residual risk
is a final syllable that is quieter than the floor all the way to the next word. That
syllable sits inside a span of at least 0.6 s and is followed by a sentence boundary, and
the kept `KEPT_GAP_S/2` pre-roll still covers its first 125 ms.

Evidence. On the job 62716037 source the V2 plan now removes 6.355 → 7.12 s. The kept
"alakalı." → "Abi" pause is 0.35 s from the voice end, and no audio before 6.355 s is cut.
Every RMS frame inside the cut is at the −40 to −43 dB ambient floor. Guards:
`TestSentenceFinalTailTrim` + the punctuated case in `TestRuleZeroCannotCutRealSpeech`
(`tests/pipeline/test_silence_cut_asr_timestamp_golden.py`), plus the seeded rule-0
property test, which now has to produce this kind.

Reversal condition. If a "cleanup clipped the end of my sentence" report traces to a
`trim_sentence_tail` adjustment, return `None` from `_sentence_final_tail_carve` and bump
`DETECTOR_VERSION`. For an immediate stop, the kill switch is `SILENCE_CUT_ENABLED=false`.

## [2026-10-02] A montage with no grounded title has no opening title (KRI-255)

Context. The unified phone-montage planner used `Montage` both as the required internal
`EditProposalSnapshot.title` and as `opening_title`. The guided compiler correctly turns every
non-empty `opening_title` into a visible `guided-title`, so a generic implementation label was
burned into a production video even though the creator had not asked for a title.

Decision. Keep `Montage` only as the snapshot schema's internal fallback. When neither exact
creator text nor eligible brief facts produce a title, persist `title: null`,
`title_source: "none"`, omit `opening_title`, and compile no `guided-title`. Do not substitute
the conversation/project name, unrequested clip-location facts, or a model-authored hook.
Explicit creator titles and deterministic titles grounded in brief facts are unchanged.

Consequences. Internal proposal consumers still receive the non-empty snapshot title their
schema requires, while render receipts and pixels agree that no visible opening title was
selected. The execution-plan validator permits an empty approved text lane only for
`fast_montage`; guided stories and explainers retain their text requirement. A creator
requirement asking for a title that could not be grounded is now reported as partial instead
of being cosmetically satisfied by a generic label.

## [2026-10-03] Celery soft shutdown ends when in-flight work does, not after a fixed 240 s (found during KRI-294)

Context. PR #808 (v0.25.11.0) remapped Fly's SIGTERM to Celery's SIGQUIT soft shutdown
(`worker_soft_shutdown_timeout=240`, `kill_timeout=300`) and turned on
`worker_enable_soft_shutdown_on_idle` "so reserved work is restored". In Celery 5.5/5.6
`WorkController.wait_for_soft_shutdown` treats that flag as "always wait" and then runs one
unconditional `sleep(240)`. So every worker group paused for the full window on every
deploy, even when idle. Fly Deploy run 37105374364 (v1390): "Updating" at 07:21:52.69Z, then
"reached started" for light +251.7 s, speech_analysis +253.7 s, autoplace +254.8 s and
worker +257.1 s. A user's five Visuals photos sat "Queued" with no consumer for that time. With
~20 deploys a day this happens many times daily.

Two Celery internals shape the fix. Both were checked against Celery 5.6.3 / kombu 5.6.2 /
billiard 4.3.0, which prod installed in its last dependency-layer build (run 36064301375):

- The wait runs inside the SIGQUIT handler on the MainProcess thread, and that thread also
  drives the event loop. The loop is frozen while it waits, so `celery.worker.state` never
  changes: a task that finishes in a prefork child still looks active until the wait ends.
  Polling `active_requests` would therefore never end the wait early.
- ETA/countdown messages whose time has not come are not in `reserved_requests`. kombu holds
  them unacked and `Channel.close()` restores them during the cold shutdown that follows the
  wait. That includes reading an outstanding BRPOP first. The restore does not depend on the
  wait. The Celery docs warn that ETA re-queueing can fail on the idle path, but this did not
  reproduce on these versions (drill below).

Decision. `app/services/celery_soft_shutdown.py` replaces the worker instance's
`wait_for_soft_shutdown` at `celeryd_init`. Each prefork child writes the id of every task it
finishes (`task_postrun`) into a shared anonymous mmap. There is one slot per billiard
process index. The mmap is created in the MainProcess before the pool forks. The new wait
works like this:

- If nothing is reserved or active at the signal, there is no wait.
- Otherwise it waits until every request that was reserved at the signal appears as
  finished, plus a 1 s settle so the child's result reaches the pool pipe. The late ack is
  sent from that pipe during cold shutdown. The wait still ends at 240 s at the latest.

Every unexpected state falls back to the stock full wait, never a shorter one. That covers a
request that never reached a child, a child killed before `task_postrun`, an index beyond 32
and a missing mmap. Whatever is unfinished at the deadline is cancelled and restored exactly
as before. `worker_enable_soft_shutdown_on_idle` is now `not CELERY_SOFT_SHUTDOWN_EARLY_EXIT_ENABLED`,
so the kill switch restores the pre-change behaviour (on_idle on, stock sleep). The flag is read
on the old machine at stop time, so a flip takes effect from the deploy after it.

Drill. A real prefork worker ran against local Redis on DB 15, with
`REMAP_SIGTERM=SIGQUIT`, acks_late, prefetch 1, `polling_interval` 10 and the window scaled to
20 s. Each run sent SIGTERM to the MainProcess, then a replacement worker drained the queue
and the task executions were counted:

| scenario | today (on_idle) | early exit |
|---|---|---|
| idle | exit 21.6 s | exit 9.0 s (2/2) |
| idle, holding a 1 h countdown task | 21.6 s, ETA restored | 10.1 s, ETA restored (2/2); stock without on_idle also restored it 4/4 |
| 4 s task in flight | 22.1 s, ran once, acked | wait ended at 4.6 s, exit 10.0 s, ran once, acked (2/2) |
| task finishing 0.1 s after the signal | — | exit 10.6 s, ran once, acked (5/5) |
| 4 s task plus a held ETA | — | 10.1 s, ran once, ETA restored (2/2) |
| 35 s task (outlasts window) | 21.1 s, restored, re-run once | 21.2 s, restored, re-run once |
| 4 s and 35 s tasks concurrently | — | 22.1 s: short ran once; long restored and re-run |

In the early-exit runs, the remaining ~9–10 s before exit is kombu's `Channel.close()` reading
the outstanding BRPOP. Its timeout equals `polling_interval` (10 s).

Alternatives rejected.

- **Shorter timeout for short-task groups (autoplace, light, speech_analysis).** Once the wait
  ends when work finishes, a shorter cap only matters for a task still running at the cap. For
  that task it trades waiting for a cancel-and-restart from scratch. light also runs
  `run_kria_turn` on agent-control. Revisit only if a group's tasks routinely outlast the window.
- **Fly bluegreen.** The strategy is app-wide. It would run two `light` machines and so two
  embedded Beat schedulers (Beat does not de-duplicate). It would also give
  `fly_machines._resolve_worker_machine` two `worker` machines, and it then deliberately acts on
  neither. Bluegreen also expects health checks, which the worker groups have no HTTP service for.

Residual, measured but not changed. With `polling_interval` 10, a freshly started worker picked
up its first message 9.0 s after "ready" (0.04 s at `polling_interval` 1). Steady-state pickup
stays instant (0.01–0.05 s). So each deploy still carries up to ~10 s at stop (BRPOP drain) and
up to ~10 s after start, which `reached started` does not show. That is the cost of the
Upstash command-count trade in `worker.py`. Revisit it there, not here.

Guards. `tests/test_deploy_shutdown_policy.py` pins:

- the 240/300 budget;
- `on_idle` off while the early exit is on;
- the instance binding via `celeryd_init`;
- the kill-switch fallback, in a subprocess.

`tests/services/test_celery_soft_shutdown.py` covers:

- the exit rules, with a fake clock;
- a real billiard child writing to parent-visible memory;
- a Celery-upgrade tripwire. If `on_cold_shutdown` stops calling
  `worker.wait_for_soft_shutdown()`, or `celeryd_init` stops passing the worker instance, the
  override would be silently ignored.

Revisit if a deploy shows a task executed twice around a worker stop (check for "Soft shutdown:"
log lines before restoring), if Celery's soft-shutdown wait stops blocking the event loop (then
`active_requests` polling is enough), or if prefork concurrency exceeds 32.

## [2026-10-06] A format's limits are receipts, not a reason to refuse the first draft (thread 17f666cb)

A phone Talking chat ("Cut out the long pauses, the part where I say 'let me start that one
again' ... Karaoke captions ... hook title ... Keep it under 45 seconds") produced no draft at
all: the brief filed the cut sentence as `timing` with `duration_s: 45`, `_check_timing` judged
it "Partly: a Talking edit keeps your whole take" (KRI-142), and the KRI-459 binding gate rolled
the draft back with "Your current draft is unchanged. Should I try a different approach, or
make this simpler version?" on a thread that had no draft. Every Talking or voiceover chat
that names a length dead-ended the same way, and "simpler version" could never satisfy it.

Decision: a checked receipt whose reason only describes what the chosen format does
(`_FORMAT_LIMIT_REASON_PREFIXES`: the take sets a Talking edit's length, the voiceover sets a
voiceover edit's, speech cleanup is chosen at approval and cuts pauses, never a named line) is
reported as an honest "Partly" and the draft is made; the question is reserved for a real
simplification Kria made instead of the ask (`needs_creator_choice`). When the gate does fire
on a thread with no draft, it says so. New checkers: `_check_speech_cleanup` (a cleanup ask in
any of timing/audio/style/select; met once `speech_cleanup_enabled`, else "choose Clean up
speech when you approve"; a quoted line or retake is editor work; a length in the same
sentence follows the cut take) and `_check_captions` (karaoke/kinetic are word-by-word; on/off
against `caption_style`). Karaoke captions were always honored (`karaoke` -> word captions,
spoken word lit); they only lacked a checker. Retakes are still not cut by the preflight
cleanup on any path (the retake detector is wired only into the legacy cloud `legacy_auto`
analysis behind `RETAKE_CUT_ENABLED`) -- a follow-up, not a receipt bug.

Guards: `tests/kria/test_brief_talking_receipts.py` (the exact sourdough requirement, the
helper, both checkers), `tests/kria/test_runtime_talking_first_draft.py` (first draft made;
a dropped karaoke ask still asks, with "I haven't started a draft yet").

## [2026-10-07] A take-length format never pins a requested length in the render contract (job e1c5f89e)

The first retry of the sourdough Talking chat (thread 6bc94e7b) made its draft, the creator
approved it, and the render failed at the pin step: `creator_render_contract.verify_phone_recipe`
raised "This edit couldn't keep the confirmed length" because the approved strategy carried
`target_duration_s: 40, target_duration_requested: true` ("Keep it under 45 seconds") and the
single-clip Talking recipe is the 68 s take. KRI-470 (PR #1439, the day before) pins a requested
length as a hard requirement for every format, but no compiler trims a Talking or voiceover edit
to a length: `subtitled`/`talking_head` keep the take (minus speech-cleanup pauses) and the
narrated family runs as long as the voiceover (KRI-142's `_TALKING_KEEPS_WHOLE_TAKE` /
`_VOICEOVER_SETS_LENGTH` receipts already say so). Every such edit that named a length failed
with "This edit uses something your iPhone can't render yet."

Decision: `build_render_contract` pins a duration (from the strategy or a brief timing fact)
only when the edit format is not in `TAKE_LENGTH_EDIT_FORMATS`; montage-family and speech
montage keep KRI-470's rule unchanged. The creator still sees the honest "Partly: ... the length
follows what's left of your take" receipt at draft time, so the approved summary and the contract
agree. Guards: `tests/services/test_creator_render_contract.py::test_a_take_length_format_never_pins_a_requested_length`,
`::test_the_sourdough_talking_recipe_passes_its_pinned_contract`, `::test_a_montage_still_pins_its_requested_length`.

## [2026-10-07] Speech-cleanup preflight pads audio that ends before the picture (items 50004c29, cc5f9715)

Both sourdough Talking chats scheduled a preflight analysis on `media_added`; both failed
`snapshot_mismatch` 1.5 s later and, since that code is non-retryable, were never retried. The
iPhone's approval card then showed "The speech check couldn't finish" with "Create without
cleanup" as the primary button, so the creator's "cut out the long pauses" rendered uncut
(`speech_cleanup_contract: off_v1`, `bypassed_unchecked`). Measured on the speech machine: the
68.0 s proxy's AAC track runs 66.13 s; every other recent phone proxy was within 25 ms of its
picture. `_read_pcm_duration` compared the decoded audio (66.13 s) with the picture window
(68.0 s) under a 0.25 s tolerance and called the clip a changed source.

Decision: audio that ends before the picture is the clip, not a changed source. The window
stays the picture's (the render-time `require_source` fence keeps comparing it to the clip
duration); ffmpeg anchors the track to the window's first instant (`aresample=async=1:first_pts=0`,
so a track that STARTS late gets leading silence and words stay in picture time) and a tail
shortfall is padded with silence after decoding, which the engine may cut like any other pause.
The shortfall is capped (the larger of 3 s and 10% of the window); beyond it, and for audio
LONGER than the window, it is still `snapshot_mismatch`. The failure
log now carries the private detail code (`detail=source_duration`), which was neither logged
nor persisted before. Guards: `tests/tasks/test_speech_cleanup_analysis.py::test_audio_that_ends_before_the_picture_is_padded_to_the_window`
and siblings. Not changed: the iOS card still offers a dead "Retry speech check" for a
non-retryable failure and makes "Create without cleanup" primary while the brief asks for
cleanup -- an iOS follow-up.

## [2026-10-11] Incident fixes carry a shared acceptance gate, not a private test (KRI-577)

Between 2026-10-04 and 10-11 Yasin's agents opened 55 PRs; a session audit
(`docs/reviews/kri-577-session-audit.md`) found that most follow-ups came from four
habits, not from wrong code: fixing one symptom's call site while its siblings
(other archetypes, the empty-selection path, the receipt) stayed open; verifying with
tests that faked an internal boundary (KRI-454's resolver `run()`, KRI-515's harness
pinning the failing case as correct); running the suite against a missing `postgres`
role and reading "92 pre-existing failures" as green; and fanning out parallel PRs on
one hot file (`brief_checks.py`, `MAIN_CREATOR_PROMPT_VERSION`). Fixes verified by unit
tests alone failed on the phone within minutes of "live" three times in one chain
(#1433 -> #1443 -> #1445).

Decision: every incident gets a row in `tests/gates/reliability-gates.json` naming the
boundary, the fix, the regression tests, how real the harness is, the evidence level
reached and the neighbours still open. `scripts/reliability-gates.py` selects the
gates for a diff, runs them (environment skips reported apart from failures) and runs
the red control (the fix's parent must fail the gate). The CI journey
`kri-577-reliability-gates` runs the whole union whenever one of those paths changes;
`preship-check.sh` step 6 runs the mapped subset before a PR; the PR template asks for
the five acceptance lines. `scripts/dev/test-db.sh` gives each worktree a migrated
scratch database so the first test run is not thrown away. Rejected: a per-incident
CI step (the kri-548 pattern) for each of 25 incidents -- the manifest union is one
step and one source of truth; and blocking merges on the PR-body lines -- Emir's
review reads them, CI runs the tests.


---

## [2026-10-13] Slide stickers: the image stays in the pool but is never a slide; old builds get a 426 (KRI-647 / KRI-460)

**Decision:** A pasted slide sticker (`SlideEdits.stickers`) references an ordinary image `PlanItemAsset`, so upload, dedupe, HEIC handling,
quota and ownership checks are reused and no migration is needed (the stickers live inside the `slide_post` JSON). Because every route that
derives slides from the pool (`compose`/`propose` with no ids, the app's "continue with N visuals") would otherwise promote that image to a
slide, the sticker assets of a draft are subtracted in each of them, and the PUT refuses a slide that is also a sticker. Unlike drawings (an
old build silently drops the unknown key and the "missing keeps stored" merge protects it), a save that CARRIES stickers from a build below
protocol 8 is refused with `426 client_update_required`, because such a save can only be forged or replayed.
**Why:** A render-time trap, not a UI one: a sticker image that became a slide would silently change the exported post. Rendering is best
effort for a stale sticker asset (dropped, never fatal) so deleting a pool image cannot brick an export.
**Revisit if:** a pool-wide "role" column on `PlanItemAsset` (sticker vs slide) is ever added; the subtraction can then become one query filter.

## [2026-10-15] Vercel env-var history moved out of CLAUDE.md (KRI-680 budget)

`NEXT_PUBLIC_GOOGLE_CLIENT_ID` + `NEXT_PUBLIC_GOOGLE_PICKER_API_KEY` were removed in
v0.7.8.2 with the dead `/template/[id]` route (Drive picker gone; NextAuth uses the
server-side `GOOGLE_CLIENT_ID`). Moved here to make room for the
`CLIP_EXPOSURE_FACT_ENABLED` line.

## [2026-10-18] Consented speech cleanup never claims a retake cut; the reply states what it cut and left (KRI-551)

**Decision:** Phone Voiceover/Talking cleanup (`required_v1`) cuts long pauses and filler sounds only. The preflight analysis is built
without `retake_spans`, so a restarted sentence ("At kilometer 30... sorry." in stress kit N3, job 8a0ced82) is never cut, whatever
`RETAKE_CUT_ENABLED` says. We chose honesty over a half-tested feature: the iOS cleanup card says "pauses and filler sounds" and that a
restarted sentence stays in; the Talking whole-take receipt no longer says "retakes may be cut"; and the finished-render reply appends
`speech_cleanup_review_note(variant)`, read from the variant's own `silence_cut.removed` rows (14 silences + 1 filler on N3), so it holds
even when the render's brief binding was lost (KRI-552) and no requirement receipt could report the limit.
**Why:** The only retake detector is a paid Gemini agent (`retake_detector`); precision matters because a retake cut removes real words
under consent, and validating that needs paid live runs. A deterministic text matcher would be a new heuristic reading creator words.
**Real cut (not done, needs a paid precision run):** inject `retake_detect_fn` into `analyze_speech_cleanup` after `_timed_words`, call
`run_retake_detector` from `speech_cleanup_analysis._run_engine_once` behind `RETAKE_CUT_ENABLED` (fail open to zero spans), add a retakes
token to `current_detector_policy` / bump `DETECTOR_VERSION` (retires `ready` analyses, re-collects consent, reshuffles cohorts), expose
`retakes` in `public_projection` + the iOS stats, then drop the limit sentence and let `_check_rendered_cleanup` read a cut retake as met.
**Guards:** `tests/tasks/test_kri551_cleanup_review_note.py` (real N3 words + cut record, English and Turkish).

---

## [2026-10-27] The cleanup review counts only cleanup's own cuts and says what it heard (KRI-978)

**Decision:** `speech_cleanup_review_note` (`_cleanup_counts` in `app/kria/brief_checks.py`) sums its seconds over the cleanup rows of `silence_cut.removed` only (`silence`, `filler_*`, `retake`), the same rows it counts, instead of reading `time_saved_s`. It now says "Speech cleanup cut what it heard as …" and "It looks only for pauses and filler sounds, so a sentence you restarted stays in." (TR: "duyduğu şu sesleri kesti" / "arar"). This amends the KRI-551 entry above.
**Why:** Don Julio lunch T3 (job 0d8d644f) said "1 filler sound (41.4 s)" for a 0.32 s filler. `time_saved_s` has also counted the quoted-line trim (`opening_cut` / `end_cut`) since those cuts were folded into the same record. T1 (job 81ce940b, no trim) said 0.8 s and was right, which is why it went unseen. Counting and timing from the same rows means the two cannot disagree; `speech_cleanup_outcome.removed_ms` would also give 0.32 s, but it is a second source. The old wording promised what was cut, and on T1 and T3 cleanup had just cut part of "provolone" (KRI-970): its filler cuts trust the transcript's timing. A record with only trims already has `silence_cut_outcome = "no_change"` on the phone Talking path, so it never claims a cut.
**Not done:** reporting the trim separately ("Kept 3.4–10.5 s of the take as you asked"), which the ticket listed as optional.
**Guards:** `tests/tasks/test_kri978_cleanup_review_seconds.py` (real T3/T1 `silence_cut` records, English and Turkish).

---

## [2026-10-20] Editor Save: one clip-window rule, readable refusals, and a code catalog (KRI-770)

**Decision:** Every Save path compares a clip window against the minimum with `below_editor_minimum` / `snap_to_editor_minimum` (`app/schemas/editor_window.py`, 0.1 s with a 1e-6 tolerance), never a bare `< 0.1`. The phone's edge-trim drag goes through `KriaSourceWindowGeometry` like the Range sheet. Every non-2xx editor-commit logs (`editor_commit_422` / `editor_commit_refused`). The editor Save code catalog (`ios Tests/Fixtures/editor_save_codes.json`) lists each machine code the Save can raise as `mapped` (specific app copy) or `generic`; `tests/routes/test_editor_save_code_catalog.py` (AST scan) and `EditorSaveCodeCatalogTests` keep server and app in step. The app shows a readable server sentence instead of "This save was rejected".
**Why:** Troya Road Trip could not be saved after a clip trim. The trailing-edge drag to the 0.1 s minimum encoded `(in_s + 0.1) - in_s` = `0.09999999999999998`; the resolvers refused it with `TIMELINE_INVALID_WINDOW` / `TIMELINE_INVALID_DURATION`, which the app words only as the generic line. Nothing recorded the refusal, so the cause had to be re-derived from code. The same shape (phone allows, server refuses, creator sees nothing specific) had already cost five PRs in KRI-736.
**Also closed in the same change:** guided-story Saves now fit a submitted text lane to the trimmed total (the authored path already did); the capability map no longer advertises `clips.playback_rate` on a device-rendered guided story (the phone compiler refuses any rate but 1); three non-HTTP failures in the Save prepare path now answer 409/422 instead of 500; a talking-source clip has no trim handle.
**Revisit if:** the phone compiler learns story-footage retime (reopen `clips.playback_rate` on device), or a Save refusal needs structured per-clip detail in the app.

---

## [2026-10-21] Language policy: models understand, code stays language-neutral (KRI-773, KRI-786)

**Decision:** Kria treats Turkish and English as equals by keeping language out of rule-based code:
1. **Models own understanding.** The model that reads the message records the meaning in a structured field (brief `facts`, clip-intent ops, copilot `claimed_ops`). Server code checks that field. No new regex or `word in text.casefold()` test that guesses intent from words.
2. **One fold.** `app/text_fold.py` `fold_text` (İ/I/ı → i, casefold) and `loose_text` (plus diacritics stripped), plus Turkish-aware `lower`/`upper`/`capitalize` for shown text (`app/kria/brief_route.py` re-exports the fold). No private fold tables, `.replace("ı", ...)` chains, or NFKD + ascii-ignore copies; the remaining copies are a shrink-only baseline (KRI-801).
3. **Server copy goes through `say(en=, tr=)`** (`app/kria/reply_language.py`).
4. **Prompts get the language.** Every agent that writes text a creator reads takes `prompt_language_line(...)` or a `$language` placeholder (`load_prompt` is `string.Template`, so a `{language}` brace is never filled), and every caller passes the creator's language rather than leaving an `"en"` default. Agents that write no creator prose are listed as language-neutral with a reason.
5. **Parity suite + guards.** `tests/parity/test_decision_parity.py` runs every rule-based decision on English and Turkish phrasings (gaps are strict xfails). `tests/evals/parity/` checks model-layer pairs from recorded cassettes. Guards, all ratchets that fail only when a count goes up: `tests/kria/test_kri667_no_new_intent_regexes.py` (regexes + word-membership tests on folded text), `tests/kria/test_single_text_fold.py`, `tests/kria/test_prompt_language_input.py`, `tests/parity/test_map_coverage.py` (every language-sensitive row in `docs/reviews/turkish-parity-map-2026-10.md` has a table row or a reason). All run in `make verify-kria`.

**Why:** KRI-520 added ~170 Turkish regex twins in a week. They drifted (the legacy fold broke English "TRY AGAIN"), missed paraphrases, and older layers kept overriding the model with English-only rules. A Turkish creator got the model's guess; an English creator got a deterministic fix.
**Revisit if:** a third creator language arrives (the same rules apply; the fold and `say()` grow a language, not a twin per module).

---

## [2026-10-25] Idea forms: drop mislabelled ideas, keep single-form sets

**Decision:** `content_plan.form` is validated server-side in `CameraRollStoriesAgent.parse`. An idea whose form lacks its evidence (guide < 3 distinct sources, factual_story without a selected fact) is dropped alone. Three surviving ideas with one form are kept and logged (`camera_roll_stories_single_form_set`); variety is a prompt rule only. `StoryContentPlan.version` stays 1; legacy = `form` absent.
**Why:** A mislabelled idea is independently wrong and the others stay valid, so a subset is safe (the service already drops invalid ideas the same way). A first draft failed the whole response when three ideas shared a form, but the server cannot tell a lazy model from a creator who asked for "just the performance clips" or owns one clip (`atmosphere`/`review`/`performance` have no evidence need, so "footage supports several forms" is always true); that raised a 503 for obeying the request, and with `max_attempts=1` every repeat paid and failed again. Bumping `version` would make older iOS builds, which echo the plan back on wording edits, send a v2 plan without a form.
**Revisit if:** the logged single-form rate is high, then prefer one bounded re-ask when the query is empty.

**Rollback:** `STORY_IDEA_FORMS_ENABLED` (default `true`). `false` (Fly secret, api + worker, restart) = form neither required, validated nor stored on new ideas, and wording edits drop a stored form. An older API image cannot read stored `content_plan.form` (`extra="forbid"` => 500 on the saved-stories list), so before rolling the image back: flip the flag off, then strip stored forms with `UPDATE media_collection_stories SET story_json = story_json #- '{content_plan,form}'`. During a rolling deploy an old machine can 500 on a new row for minutes; ship the API before any iOS build that echoes `form`.

---

## [2026-10-28] Camera-roll ideas are written for a stranger, not the creator's diary (KRI-1004)

**Decision:** The `camera_roll_stories` prompt (`2026-10-28.2`) is audience first. `audience` names who stops scrolling and `why_it_works` says what they get in the first two seconds. `opening_text` is a hook: a concrete promise, observation or fact from the footage, the creator's words or research, never a recap of the creator's day or a mood label. `atmosphere` comes last (a mood request, or an idea no other form fits); guide, review, factual_story and performance come first when the footage supports them. Captions talk to the viewer: no first-person recap and no invented feelings, and a first-person opinion only in a review whose opinion the creator supplied. `creator_provided_event_context` (KRI-1003) counts as creator direction: a named place goes into each idea, and a stated intent such as "show why people should visit" makes why-visit and guide ideas welcome, built on listed features the footage shows. A self-check (diary, brochure, viewer payoff, grounding) runs before returning. The prompt now starts with the `grounded-hook` runtime skill. `social-caption` is not loaded: it tells the writer to sound like "the creator sharing this particular moment with someone they know", which is the diary register this change removes.
**Why:** The voice eval baseline (`src/apps/api/tests/evals/baselines/camera_roll_voice/baseline-2026-10-28.json`, 3 runs a variant) scored the no-place request 2.5 composite: diary_voice 4, viewer_value 2, specificity 2, 7 of 9 ideas `atmosphere`, openings like "Wandering through quiet stone streets", captions like "Loved seeing ...". With the place typed into the query it reached 3.5 but kept brochure lines ("Discover Cunda Island's quiet charm"). The cause was the prompt: it said "Do not force ... a dramatic hook", called the mood/day-in-the-life form equally valid, banned "tourism invitations" and "a generic advertisement for visiting" even when the creator wanted a why-visit post, and let place names come only from the query or research. Grounding rules (no invented facts, prices, hours or history; candidate ids only; proposed-copy limits) are unchanged. The slogan ban stays as the brochure test: a concrete reason beats a slogan.
**Coupling:** the idea cache is keyed on `prompt_version`, not on the skill text. An edit to `skills/grounded-hook/SKILL.md` changes this prompt, so bump this agent's `prompt_version` with it. The skill file is in the `kri-729-camera-roll-recommendations` CI journey, so such an edit runs the camera-roll tests.
**Revisit if:** the after-run on the same fixture does not lower diary_voice and raise viewer_value and specificity with grounding no worse, or creators report invented claims in why-visit ideas.

**Tried and removed (round 2, prompt `2026-10-28.3`): a model copy-edit pass.** A second Flash call after the ideas parse quoted stock words ("Experience ...", "full of charm", "perfect for a peaceful getaway") and rewrote each line. It did remove the words, but the paid after-run (`a2-copy-edit-2026-10-28.json` against `a2-social-voice-2026-10-28.json`) did not move the judge: composite bare +0.17, event +0.08, query -0.08, and viewer_value, specificity and grounding were flat. It added a model call, latency and cost to every idea request with no kill switch, so it was removed and the prompt is `2026-10-28.4` (round-1 content, bumped so cached ideas from `.3` do not linger). The structural `filler` check keeps the wider stock vocabulary in `tests/evals/runners/camera_roll_voice.py`; `filler_rate` fell because the words were gone, not because the ideas got more useful.
**Likely real limiter:** grounded copy can only be as specific as the footage descriptions and the creator's own event notes. When a place or clip has nothing concrete behind it, the model fills the gap with brochure wording whatever the prompt says. That is why the event title and about fields matter more than prompt wording.
**Open (round 3):** concrete facts have to come from the creator (event notes, answers to `open_questions`) or from research, not from rewriting the model's own draft.

---

## [2026-10-25] Open questions: server-owned text, answer through the wording edit

**Decision:** Unknowns live in `content_plan.open_questions`, not in `why_it_works` prose. Answer/skip state is written through the existing `correct_story` wording edit (a story revision), and `select_story` refuses with 409 `story_open_questions` until none is pending, but only for builds declaring `X-Kria-Client-Protocol` >= `OPEN_QUESTIONS_MIN_CLIENT_PROTOCOL` (22, the first iOS build with the question card). Older builds build the idea with its neutral copy. The client may change only `answer`/`dismissed` of known ids; an omitted list keeps the stored one. The prepared story (`PreparedCollectionStory.content_plan`, its digest, turn plan and planning diagnostics) carries no `open_questions`: answers are private creator text that nothing downstream reads.
**Why:** Prose cannot gate anything. A separate answers store would need its own revisioning and approval binding, while a wording edit already invalidates stale approvals. Letting a missing list overwrite the stored one would let a build that predates the field silently clear the gate. Gating a build that cannot answer is a dead end, so the gate follows the protocol header like every other native-only step. A malformed or surplus question is trimmed by the agent instead of failing the response, since the idea itself is still valid.
**Client gate:** questions are emitted, and the 409 enforced, only for builds sending `X-Kria-Client-Protocol` >= `OPEN_QUESTIONS_MIN_CLIENT_PROTOCOL` (22, the first build that renders them). Older builds get ideas without `open_questions` and select without the gate, so they never hit a 409 they cannot explain. Kill switch: `…=999` (api) + restart.
**Rollout and rollback:** `form` and `open_questions` are new keys inside stored `story_json.content_plan`, which the previous image rejects (`extra="forbid"`). Deploy api and worker together (Fly Deploy). To roll back, set `CAMERA_ROLL_STORY_EXTRAS_ENABLED=false` first (api + worker, restart; new ideas are stored in the legacy shape and `select` stops gating), then strip the keys from existing rows (SQL in `docs/pipelines/media-library.md`), then redeploy the old image.
**Revisit if:** answers should regenerate the proposed copy (today the creator edits the wording; the answer stays on the story row).

---

## [2026-10-25] Research topic suggestion is a regex prefill, never a search

**Decision:** The server suggests a research topic with a no-model heuristic over the creator's query and clip descriptions and returns it as `suggested_research_topic`. The creator must confirm it into the existing topic field; only that field is sent to public search.
**Why:** Searching on a guess from private media would break the invariant that public search sees only a topic the creator confirmed, and a model call per request would add cost to every idea request. A query-named place or landmark is trusted; a bare name in clips needs two clips (one clip is too often a person or brand). Lowercase places and names under four characters are missed on purpose: a miss only means the creator types the topic, a false positive would put a stranger's name in a search box.
**Revisit if:** creators ignore the chip because it misses their places; then consider a gazetteer, not a model.

---

## [2026-10-27] Speech cleanup keeps a filler stamped over a squeezed word (KRI-970)

**Decision:** Rule 1 no longer cuts a lexicon filler when the word next to it (within 0.15 s, 2+ vowel groups, not another filler) is stamped faster than 12 syllables/s. That token is then treated as a word by the acoustic and pause rules too. `DETECTOR_VERSION` → `mixed-gap-v6-squeeze`. The finished-render note's "It only cuts pauses and filler sounds" is fixed separately with its seconds (KRI-978, entry after KRI-551).
**Why:** Don Julio T1 and T3 (jobs 81ce940b, 0d8d644f): whisper-1 put an "uh," that nobody said over "the provo-" and gave "provolone" 0.18 s / 0.28 s. The cut was right against the transcript and wrong against the audio: whisper large-v3-turbo hears "Eastern cheese" / "It's prolon cheese" on the old cuts and "It's provolone cheese" on the new ones. Segment logprob signals cannot see this, and whisper-1 has no per-word confidence. The vowel-group count is deliberately language-blind: it over-counts English silent "e", which can only keep a filler. An English silent-e rule would under-count "provolone" and let the T3 cut through. Replayed on the 46 distinct transcripts in 321 Oct 2026 prod jobs plus the test kits, the guard refuses 7 of 73 filler cuts: both provolone cases, three more that look the same (a 0.04 s "once" after a 0.82 s "uh"; a 0.10 s "ready" after a 2.6 s "um"; "zami" 0.14 s after "eh"), and two unclear ones ("people" / "morning," at 0.14 s). Keeping a filler costs a beat; cutting a word mangles what the creator said under a caption that still shows it.
**Bump effects:** as with earlier bumps, `ready` analyses keyed on the old version are retired, consent is re-collected and preflight cohorts reshuffle. Stamped jobs finish under their own persisted plan.
**Revisit if:** creators report fillers left in next to long words, or a per-word aligner (forced alignment / word confidence) becomes available. Then judge the token on the audio, not its neighbour's stamp.

## [2026-10-27] Speech cleanup places filler cuts on the sound and re-listens before cutting (KRI-970 follow-up)

**Decision:** With `SPEECH_CLEANUP_FILLER_CHECK_ENABLED` (default on), the preflight analysis re-times English takes that have lexicon fillers with a forced aligner (`facebook/wav2vec2-base-960h`, pinned revision, baked into the image at `/opt/models`; `app/pipeline/speech_cleanup_alignment.py`) and gives each filler the gap between its aligned neighbours. It skips the re-timing when any filler fails the KRI-970 squeeze guard on whisper's own stamps, has no room, or the alignment is weak. Then it re-listens around every filler cut (two short whisper-1 calls per cut, at most 8 cuts) and drops a cut when a real word within 0.5 s of it disappears. `DETECTOR_VERSION` → `mixed-gap-v7-filler-check`; the switch is part of the source policy fingerprint.
**Why:** Merope café (KRI-790 brief transcript): whisper-1 stamped every word from 3.8 s to 12 s 0.3-0.5 s late, so the red "Uh" and "um" cuts removed "we are with" and "tell" and left the fillers in. Only 2 of 6 filler cuts in that take hit a filler. The squeeze guard cannot see a shifted stretch. Offline on the real audio: searching the sound near the stamp picked words; re-timing alone cut "the" before "provolone" (the aligner placed the fake "uh," on it); re-listening alone removed nothing; the two together removed all four written fillers with no word lost, kept provolone, and repaired a deliberately mis-timed sourdough transcript. CTC scoring also shows "It's the provolone" fits the T1/T3 audio better than "It's uh provolone", confirming that the KRI-970 "uh," was never said.
**Limits:** one real take with live fillers (2 of 14 own finished videos with a stored transcript contain any filler, both the fake provolone "uh"). The re-listen judgement is noisy in the safe direction: with free stand-in listeners it sometimes kept a filler (never lost a word). Fillers whisper never writes down (Caro's "eh") are untouched. The aligner is English-only.
**Revisit if:** creators report fillers left in after cleanup (look at `diagnostics.filler_check.relisten.dropped`), the re-listen cost matters, or whisper-1 gains word confidence.

## [2026-10-28] Speech cleanup never cuts a span that holds a voice, whatever the transcript says (KRI-1017)

**Decision:** After the plan is chosen and before the re-listen, every silence, acoustic-filler and lexical-filler cut is checked against the audio itself (`app/pipeline/speech_voice_guard.py`). A sustained voiced run is periodicity of at least 0.7 at a 60-400 Hz pitch lag, in the 80-1000 Hz band, for 50 ms or more, at least 3 dB above the take's 5th-percentile frame level. A pause cut holding one keeps only its longer voice-free edge, `PAD_S` clear of the voice, if that edge is still a `MIN_CUT_S` cut, and is dropped otherwise; the stretch between two voiced runs is never kept as a cut (soft speech is detected in bursts). A filler cut holding one is dropped whole. Voicing inside a transcribed filler token (± `PAD_S`), a consented retake or a creator-chosen span is not judged: those have their own checks. Undecodable audio drops every judged cut. The same guard runs on the in-render cleanup path. Findings and the receipt count only the cuts that survive (this also fixes re-listen drops, which left their filler findings in the count). `DETECTOR_VERSION` → `mixed-gap-v8-voice-guard`.
**Why:** Plaza de Mayo "maximo 2" (job e46baf42): 13 cuts, 8.8 s, in a 48 s two-person take with no real silence. Plain silencedetect found no silence at all. Every span came from the KRI-234 ambient fallback (floor = 10th percentile −30.5 dB, threshold −24.5 dB). The guide, farther from the mic at −25 to −28 dB, sat under it. The mixed-language transcript had no tokens for him or for the Turkish stretches, so pause cuts landed on his words. The soundful bits between two fake silences became tokenless "acoustic fillers", and merged clusters took the label "silence" and escaped the filler re-listen. Offline replay of the real planner on the kit file and the job's recorded words reproduces all 13 cuts. Spectrograms and an independent faster-whisper pass put a voice in 6 of them: the guide's soft "Maximo", the first "1810", "Yes", a held "the", "whole", and the tail of "Aires". The other 7, plus a filler cut the live re-listen had dropped, are noise-only pauses. The guard gives back all 7 voiced cuts (the 6 live ones plus 43.23-43.73 s: 5 dropped, 2 trimmed to their noise-only edge) and keeps the 8 pauses, so 4.6 s of real pause is still cut and no checked voice stretch is touched (`tests/pipeline/test_kri1017_maximo2_replay.py` with `KRI1017_SOURCE`). Loudness alone cannot separate them: the guide's far-mic voice is only 2-4 dB above the plaza noise, and the noise bed itself swings from −24 to −35 dB. Periodicity does: traffic and wind are aperiodic.
**Not this lever:** `SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED`, `SILENCE_CUT_ENABLED` and the KRI-234 ambient spans stay as they are. The ambient spans are still right on rain, wind and traffic takes; the guard is what stops them landing on a quiet voice.
**Limits:** measured offline on 5 other takes (café with prod words, Don Julio lunch T1/T3, night boat, Plaza T1) plus the whole 68 s café take: no real pause and no transcribed filler was lost, and the cuts it gave back clipped a word's first or last 0.1 s. It does not catch a real word under a filler token whisper stamped over it (late "Uh" over "we are with", the KRI-970 class): the exemption covers that token, so the aligner re-time and re-listen still own it. Weak or short words in loud noise can stay under the bar: a cut placed on purpose over the middle of each word was caught 33 of 41 times, and most misses were short words in the restaurant take (some were timing artefacts). Periodic background keeps pauses in: engine hum 3 dB over the floor reads as voice (no cut is planned on those takes today, as no silence is found), and so do low restaurant tones. Tokenless real "eee"/"uh" are voiced, so the guard now keeps them. A weakly periodic word tail can still slip (the faint "Yeah" end at 2.75 s; the live re-listen caught it).
**Revisit if:** creators report pauses left in on noisy takes (look at `diagnostics.voice_guard.dropped`), or a language-independent voice-activity model becomes cheap enough to run in the preflight worker.

## [2026-10-31] Route disclosure: the approval card says what the render route does not read (prompt fidelity)

**Decision:** `app/kria/route_disclosure.py` is the one registry of what the phone unified montage route does not consume, and the draft turn (`tasks/kria_runtime.py`, strategy drafts under approval readiness on the unified route) now says it BEFORE approval, never blocking. (a) Strategy fields the route ignores (`UNREAD_STRATEGY_FIELDS`: intro_hook, story_structure, pacing, direction, caption_style, optional_treatments, archetype, hero_media_id, overlay_display, licensed_sfx) get one plain "I can't use X on this video yet" line each, only when set and not equal to the route's own behaviour. A route unit that starts reading a field deletes its entry and the line disappears. (b) An ask nothing reads (today: animated text outside a typed title animation) becomes a typed `not_possible` receipt whose reason is a route decline (`is_route_decline_reason`): a format limit, so it never asks "simpler version?", and readiness gives it the new approvable disposition `declined`. (c) On the unified montage route only (the same `defers_to_unified_montage` predicate the declines use), `assess_readiness` no longer holds a draft for `evidence_missing` on a brief_checks-owned ask (it becomes `unverified`, same as a vague ask: that route's render judges it). Every other route (voiceover, talking, slides, lipsync, cloud) keeps the hold. Always still held: order asks, a quoted text (`literal`; the worker refuses a render whose confirmed text is missing from the plan), a length ask with a duration target (missing length evidence is never a pass), research asks, keep-text-off marks, prepared stories, editor drafts and policy problems. (d) The length on the card is `planned_length_s(media)` when the creator stated none (Assos: 20 s shown, 18.005 s rendered). (e) When the planner chose the `text_explainer` direction, the render's text step may search the web after approval: one line says so unless the sourced preview already did. Limit: the real trigger is the text composer's own `research_request` at render, which no draft-time signal predicts, so a non-explainer direction that still researches gets no line; Assos check 7 passes because that plan's direction is `text_explainer`, not because research is always disclosed. Typed records ride on the `draft_applied` event as `not_applied` (`code`: `not_applied_on_route` | `research_after_approval`).

**Why:** Assos (thread 48af6a52): "With animations, explain why people should visit Assos" drew static text, dropped the planner's hook and story order, researched the web after approval and showed 20 s for an 18 s video, while the card said "Balanced pacing" and "Have a look at these in the video". The more precisely an ask was recognised the harder it blocked, and a vague one passed in silence.

**Kill switch:** `KRIA_ROUTE_DISCLOSURE_ENABLED=false` (fly secret) + restart api and worker: no lines, no declines, no length rewrite, and `evidence_missing` holds the draft again, byte-identical to before. Default on.

**Premises (each tested):** the unified route reads a title animation only from a typed id on a title-scope ask or `strategy.title_animation` (`unified_montage.brief_view`); it ignores `target_duration_s` unless the creator stated a length; `planned_length_s` is the length the recipe renders; a checker's own verdict is never replaced by a decline. Pins: Assos scorecard checks 1, 3, 4, 5, 7 and its `hold:` controls (a quoted text, an order ask and a length ask without length evidence stay held; a vague ask stays approvable) in `scripts/kria/score_assos.py`; check 10 stays report-only and also lists registry drift.

**KRI-667:** no intent regex was added (baseline unchanged). `asks_text_animation` reads the typed `facts.animation` only (an edge id, or `"any"` for a generic "animated text" ask, which the animation unit's `main_creator` prompt v106 records; this unit's interim `facts.animated` was dropped at integration so the model has one way to say it). An ask the model records without the fact is not disclosed (silent gap, the same as before this unit). Whether the live model writes it is graded by a live extractor run, not by the offline scorecard.

**Limits:** disclosure runs only on the readiness-bound unified route; the legacy non-readiness path and other routes are unchanged. The "Balanced pacing" change chip still prints beside its "not applied" line until the pace unit consumes `pacing`. `unsupported`/`needs_choice` (a checker's real answer) still hold a draft. The research line is a conservative "may": the text composer decides at render whether to search.

**Revisit if:** a route unit consumes any listed field (remove its entry), or creators find the heads-up noisy on drafts they did not ask about.

## [2026-10-31] Prompt fidelity, errors unit: a provider failure is not an unreadable request (Assos)

**Kill switch:** `KRIA_ERROR_HONESTY_ENABLED` (default `true`, `app/config.py`). `false` + restart api and worker restores all three behaviours below byte for byte.
**Decision:**
- **Honest reply.** A follow-up whose brief extractor failed for a provider/transport reason (`planner._is_provider_fault`: the wrapped error is a `TerminalError` that is not a `TerminalSchemaError`) now ends in reason `request_extraction_provider_failed`: "I couldn't prepare those edits this time. ... Try again, or tell me the most important change first." (the `editor_planning_failed` wording). A real misread (`TerminalSchemaError`, `BriefUpdateBatchError`) keeps "I couldn't reliably read every requested change" and the "which clip?" question. The new reason is in `request_following_cases.failed_pre_job_turns`. Before giving up, `_call_brief_extractor` retries ONCE when the failure is an exhausted-transient one (plain `TerminalError` caused by `TransientError`) and at least 20 s of turn budget remains, after a 1 s pause, with a distinct `request_id` (`<thread>:extractor-retry`) so the paid-call reservation key does not collide with the failed run. Never retried: schema, refusal, truncation, spend cap, quota, provider-outcome-unknown.
- **Error class in sensitive logs.** `_safe_error` for a `sensitive_io` agent with no `error_class` tag now returns `sensitive_agent_error:<class chain>` (for example `transient_error:server_error`): snake-cased exception CLASS names of the error and up to two `__cause__` links, never `str(exc)`. `terminal_refusal` and `terminal_unknown` log through `_safe_error` too (non-sensitive agents unchanged). Tests that pinned the bare string now pin the class form.
- **"Animate the title" chip.** `kria_editor_ops_text.title_bar_ids(snapshot)` is the one definition of "the title" (it is `classify`, which the `group: "title"` selector already uses). The suggester offers the chip only for a bar in that set (`suggestion_chips._editable_title_bar`). Matrix rows: `title_resolution_cases()` in `tests/evals/suggestion_chips_harness.py` plus a G1 dry-run check that the editor resolves the bar the chip points at.
**Why the chip is hidden, not the editor widened:** in Assos the only text is researched caption bars, the first at 2.27 s. The editor rightly calls nothing there a title (`classify` needs an intro bar within the first 0.5 s). Letting `group: "title"` fall back to "the first bar" would make "remove the title" or "make the title bigger" hit a body fact on every video without a real title, and that is destructive in a way a hidden chip is not. The hook chip (`_hook_present`) keeps the looser `_title_bar` on purpose: it asks "is there opening text", a different question. Nothing is refused: the creator can still type "animate the text".
**Failure modes listed before coding:** (1) a schema misread classed as a provider fault; (2) the retry reusing the failed run's cost-reservation key; (3) a retry past the turn deadline or on a spend-cap/outcome-unknown error; (4) the class tag leaking user text (class names only, `[a-z_:]`, 80 chars); (5) `title_bar_ids` and the suggester drifting again (G1 dry run, boundary rows at 0.4 s, 0.51 s, no `start_s`, `kria-` ids, removed bars, a late title behind a body caption).
**Revisit if:** the extractor's transient failures stay frequent after the retry (look at `kria_extractor_retry` logs against `request_extraction_provider_failed` coverage), or creators ask for "animate the title" on videos with no opening title (then add a chip that says what it will animate).

## [2026-10-31] Prompt fidelity: honesty (creator-selected stamp, look chip font, export text note)

Three small statements the product made that were not true. None blocks, retries or refuses anything.

**(a) "The creator selected this output format." only for a real pick.** The iOS app sends the server's preselected shape back on approve when the creator never touches the picker (same echo KRI-1027 fixed for fit/crop), so a shape in the body is not proof of a pick. `render_shape.stamp_origin` now writes `origin` ("creator" / "default") inside the stored `creator_render_shape`. `ApprovalDecisionBody.render_shape_origin` (`explicit_pick` / `preselected`, optional) is the client's own word; without it the server infers: a value that differs from the offered default must be a pick, an equal one is read as the default. The legacy `creator_agent` confirm route infers the same way. `render_shape.orientation_reason` picks the plan wording; `plan_unified_montage` and `plan_lipsync_montage` take it as `output_orientation_reason` (None keeps the old wording). A shape with no stamp (jobs approved before this shipped, direct callers) reads as a default. The prepared-execution `shape` decision is `model_default`, not `creator_answer`, for a default. Orientation behaviour is unchanged. The schema validator at `edit_proposal.py` (any orientation with no reason gets "creator selected") stays as the last resort: every planner path now passes a reason.
**Kill switch:** `RENDER_SHAPE_ORIGIN_HONEST_ENABLED=false` (fly secret on api + worker, restart). Off writes no `origin` key and every reader says "creator selected" again (byte-identical).
**iOS follow-up (not done here):** send `render_shape_origin: "explicit_pick"` only when `VideoShapePicker` was touched and `"preselected"` when echoing `offer.defaultChoice` (`ChatWorkspaceView.swift` ~2937-2993). Until then the server inference is all there is: a deliberate pick equal to the default reads as the default.

**(b) The look chip names the font that was burned.** The live "look" block is emitted from the pinned plan before the phone typography agent picks the face, so it named the plan's default ("Alte haas grotesk titles") while Montserrat was burned. While the phone text policy is active the early block now carries no font chip (`_early_look_font`), and after the device request is pinned `_emit_burned_look_block` re-emits the look block with the most used face in the final text rows. The pinned plan is not touched (its text lane is re-validated against the approval). `score_assos.py` check 8 now reads the last look block on the live feed (it rebuilds the early one with the worker's own helper because the replay stubs `_guided_execution_plan`).
**Kill switch:** `LOOK_CHIP_BURNED_FONT_ENABLED=false` (api + worker, restart) restores the plan-font chip.

**(c) A rejected export text check is told, not hidden.** When the phone export check fails a text layer that should be visible (`copy_words_missing` etc.) the video still ships (advisory, unchanged). `device_render._export_text_notes` now stores one creator-language line on the variant (`export_check_notes`, cleared on the next export) and `creation_threads` appends it to `render_notes`, which the app shows with the finished video. It says "couldn't confirm", never "is wrong" (the check reads pixels). No retry, no repair, no new gate.
**Kill switch:** `EXPORT_TEXT_NOTE_ENABLED=false` (api, restart).

## [2026-10-31] Prompt fidelity, pace unit: the planner's pacing sets how long clips are held (Assos check 2)

Problem: `unified_montage.py` and `lipsync_montage.py` hard-coded `direction="fast_montage"` and `pace="fast"`, and nothing read `strategy.pacing`, so "relaxed" and "fast" cut identically (Assos: 16 clips x 1.1253 s for all three paces) while the approval card printed the planner's pacing.

What changed (kill switch `PACE_DRIVES_CUTS_ENABLED`, default `true`; `false` + restart api+worker = byte-identical to before, covered by `tests/services/test_pace_hold_limits.py`):
- ONE rule, `schemas/edit_proposal.pace_hold_bounds` / `pace_selection_limits` (next to `mixed_media_hold_bounds`): fast 0.8/1.2/1.5 s, balanced 1.2/1.8/3.0 s, relaxed 1.4/2.5/6.0 s (min/preferred/max). Relaxed and balanced raise the shortest hold; fast and balanced cap the longest, and only when nobody owns the total.
- `services/length_planning.paced_selection` applies it; approval-time `build_length_plan` (generic window branch) and the render (`unified_montage` no-approved-plan selector, default cut length, growth ceiling) both call it, so a pre-approval plan and the worker's plan cannot drift. It is used only when the selector reports no conflict and the video stays >= 3 s; otherwise the plain plan stands. Photos keep their own hold rules; moment/quote/single-hero branches ignore pace.
- Precedence: a creator-stated length (`creator_target_s`) beats pace and pace never drops a clip. A song section that nobody stated (catalog `selected_music_section`, reference-only on the phone) is extended by the least a relaxed pace needs (clips x floor: Assos 18.0 s -> 22.4 s), recorded as `length_plan["pace"]` (`natural_duration_s` keeps the section's 18.0). The floor is deliberately modest: extending to the preferred hold would turn an 18 s section into 40 s. Creator-uploaded songs (`user_song`) keep their own cap.
- Provenance: `CreativeStrategy.pacing_requested` (server-owned, mirrors `target_duration_requested`) is set where the creator's words name a pace (`routes/creator_agent.py`, EN + TR cues). `services/length_policy.effective_pace`: "relaxed"/"fast" always count (the creator's or the planner's decision reaches the cut); a plain "balanced" is silence (today's lengths) unless `pacing_requested`.
- Snapshot echo: `pace` = the strategy's pacing (fallback "fast"); `direction` = the strategy's when this route can build it (`length_policy.ROUTE_BUILDABLE_DIRECTIONS`, today only `fast_montage`), else `fast_montage` with `UnifiedMontagePlan.requested_direction` / `unified_montage.requested_direction` in the receipt for the disclosure unit. Pace has no render effect on a fast_montage snapshot by itself (policy transition is already "none"); its effect is the hold lengths above.
- Not done on purpose: crossfades / `FastMontageCut.transition`, text_explainer / guided_story rendering. A text_explainer snapshot with `fast_cuts` is refused by the schema (`EditProposalSnapshot`, fast_cuts only for fast_montage) and ~10 `guided_story` gates, so the Assos "direction follows the strategy" half of check 2 stays open until the unit that builds those directions extends `ROUTE_BUILDABLE_DIRECTIONS`.
- Fix pass: `paced_selection` itself now requires `MIN_PROPOSAL_DURATION_S + 0.1 s` (`PACE_MIN_HEADROOM_S`) on a free total, so approval and render share the headroom (a free 2-clip fast pace totals exactly 3.0 s, 2.967 s after whole-frame rounding; the plain plan stands and carries no `pace` record). A creator-stated total is not affected. `pacing_requested` is now erased/re-stamped wherever `target_duration_requested` is: `generative_jobs` factory, `kria/drafts` (only the head draft's own stamp survives), `_model_strategy_hygiene`, and the Main Creator copy (the route re-stamps it from the creator's words).
- Intended behaviour change, not an accident: a planner-chosen "fast" (no creator ask) caps a free-total montage's holds at 1.5 s (was 3.0 s). The planner's pacing is its own decision and reaches the cut; `PACE_DRIVES_CUTS_ENABLED=false` restores the old lengths. How often the planner picks "fast" for ordinary edits is not measured; check prod `strategy.pacing` before widening.

## [2026-10-31] Prompt fidelity (animation): "with animations" reaches the text, or the card says it cannot

**Decision:** A creator's animation ask is one structured fact, `text` requirement `facts.animation`: a registry edge id when the creator named one, `"any"` when they did not (main creator prompt v106, brief extractor v45; the extractor shares the planner's brief section). One reader, `app/kria/animation_ask.py`, serves the three consumers. (1) `unified_montage.brief_view` reads the ask at ANY text scope (a named edge for "all the text" also becomes the title's animation, so the title keeps its own held look, and a global `position` is the corner the route's labels sit in). (2) `compose_creation_text` ends with a check, like the hierarchy facts but never raising: every live text row the ask names must play an entrance. A missing one is filled by `TextCompositionProgram.text_look`, applied inside `replay_text_composition` after the composed ops, only to rows that play none (never a row the composer or creator animated, a pinned corner text, or the caption lane). Researched explanations are added with the entrance on the `add_text` op itself (`run_researched_revision(look=...)`). (3) `brief_checks` judges it: a whole-video ask is judged on the finished text lane (`check_text_look`, unnamed = any entrance), and before approval the draft states what the render will do as an `unchecked` receipt. The creator's named id wins; an unnamed ask gets a plain `fade`, never a designed edge.
**Phone support:** the form is decided at compose time from what the phone verified and stored in the program (replay never reads settings): `animation_phases` when `authoredText` is verified, the legacy `effect` (`fade-in`, ...) when only `animatedText` is, nothing otherwise. A closed gate emits nothing (a recipe carrying phases without `authoredText` is refused at validation) and the card says "animated text isn't available on this phone build yet".
**Never blocks:** every added receipt is `unchecked`, so readiness stays `unverified` (approvable); `_no_pre_render_checker` treats a whole-video animation ask as render-judged. The verification pass swallows its own errors (`creation_text_animation_skipped`) and only logs a miss (`creation_text_animation_unmet`).
**Structured only (KRI-667):** nothing reads the creator's words, so a brief recorded without the fact (an older thread, or a model that omitted it) is not an animation ask. The prompt is the only reader of the words. The Assos fixture's r1 `facts.animation = "any"` is therefore a re-recording under v106, labelled in the fixture's `source.reconstructed`; the offline replay cannot show that the model emits it (the structured round trip and the downstream chain are pinned; the model leg is the prompt's job).
**Kill switch:** `TEXT_ANIMATION_ASK_ENABLED` (default `true`). Off: the facts are read exactly as before (title scope only), no look is applied or verified, the research ops are unchanged, receipts as before. Verified: with it off the Assos replay reproduces prod's baseline rows (`ASSOS_EXPECT_BASELINE=1`). Apply: `fly secrets set TEXT_ANIMATION_ASK_ENABLED=false` (api + worker) + restart. The prompt change itself is not switchable; with the switch off `animation: "any"` is an ignored fact.
**Not done:** a designed edge the phone cannot play falls back to the plain fade in the `effect` form and the check then reports the mismatch honestly; per-row timing (text over the opening) and a global `position` for researched/composed text are unchanged; cloud (non-phone) renders ignore the ask as before.

## [2026-10-31] Prompt fidelity, integration: how the five units fit together

Merged in order: disclosure, errors, honesty, pace, animation. Kill switches stay independent (`KRIA_ROUTE_DISCLOSURE_ENABLED`, `KRIA_ERROR_HONESTY_ENABLED`, `RENDER_SHAPE_ORIGIN_HONEST_ENABLED` / `LOOK_CHIP_BURNED_FONT_ENABLED` / `EXPORT_TEXT_NOTE_ENABLED`, `PACE_DRIVES_CUTS_ENABLED`, `TEXT_ANIMATION_ASK_ENABLED`); each OFF restores its own behaviour.
**Overlaps resolved (the merge, not a unit):**
- **One animation fact.** Disclosure and animation both bumped `main_creator` to v106 and both taught the model a fact for "animated text" with no style (`animated: true` vs `animation: "any"`). Kept `animation: "any"` only (animation's `animation_ask` reader); the `animated` sticky fact, its prompt sentence and the scorecard's code-side fixture reconstruction are gone. The fixture file carries `facts.animation = "any"`. Prompt hash pins are the animation unit's.
- **Disclosure no longer declines an animation the text pass plays.** `route_disclosure.ask_declines` skips a `text` ask when `TEXT_ANIMATION_ASK_ENABLED` is on and the phone verified a text-entrance form (`text_entrance_form()`); the animation unit's own `unchecked` receipt ("the text will animate in...") then stands. With the gate closed or the switch off, the "I can't animate on-screen text" decline stays, so the creator is never promised motion nobody renders.
- **Disclosure no longer says pacing is not applied when pace applies it.** `_pacing` returns no line when `length_policy.effective_pace(strategy)` is set (`relaxed`, `fast`, or a creator-requested `balanced`). A silent default `balanced` is still disclosed.
**Scorecard check 2 stays red on purpose:** it demands the snapshot `direction` equal the strategy's (`text_explainer`), but the unified route can only build `fast_montage`; the pace unit keeps the true value and records `requested_direction`, and disclosure tells the creator. Pace (cut lengths and snapshot pace) is satisfied; only the direction half is not, and faking it would be a lie about what renders. Revisit if a route unit builds a `text_explainer` cut.

## [2026-10-31] Song rule: a Kria creator job never gets an auto-matched catalog song (integration finding)

**Problem (read-only check at integration):** the owner's rule is that only a creator-uploaded song ever plays. Three render-time callers of `generative_build._match_best_track` ranked the whole analyzed catalog for jobs that are Kria creator jobs: (1) `_run_phone_voiceover_montage_job` (a phone voiceover montage got `voiceover_music` with a catalog bed whenever `musicBed` was device-verified; that gate is device capability, not creator consent); (2) the cloud orchestrator's `fut_match` for a cloud Job minted by the creator flow; (3) `_guided_execution_plan` (pins the matched track into the guided plan, used by the phone guided path and the cloud guided render). Scorecard check 11 stayed green because it drives the unified montage writer, which never matches.
**Kill switch:** `CREATOR_JOBS_NEVER_AUTO_MATCH_CATALOG_SONG_ENABLED` (default `true`, `app/config.py`). `false` + restart api and worker is byte-identical to before. Apply: `fly secrets set` on api and worker.
**Rule:** `app/services/creator_song_rule.py`. The phone voiceover writer never calls the matcher. The cloud and guided paths skip it when the job's assembly plan carries a creator stamp (`creator_generation_id`, `creator_plan_authority_version`, `native_device_only`, `_phone_sources_v1`); a web generative job has none, so its song variants are unchanged. A job that matches nothing renders without a catalog song (cloud: the song variants skip and `original_text` renders, as with an empty library). Nothing raises, refuses or blocks approval.
**Failure modes listed before coding:** (1) one of the three call sites missed; (2) a web generative job caught by too wide a creator marker; (3) kill switch off not byte-identical; (4) a creator-uploaded song (`user_song`) or pinned narration affected (they never reach the matcher, unchanged); (5) a track the creator already approved (frozen licensed music, `status == "selected"`) is not an auto-match.
**Guards:** `test_phone_voiceover_job_never_plays_a_catalog_song`, `test_guided_plan_never_pins_a_catalog_song_for_a_creator_job` (creator job, web job, switch off). The cloud orchestrator call site has no direct test (the orchestrator is too heavy to drive here); it is the same predicate as the guided one.
**Not done, needs the owner:** (a) `kria/planner.py` runs `_match_best_track` at plan time and `_approved_licensed_music_track` can freeze that catalog song into an approved plan (`licensed_music.status == "selected"`), which still plays. If "uploaded only" is strict, that product path needs its own decision; (b) `IOS_DEVICE_ONLY_MODE` / `IOS_NATIVE_DEVICE_ONLY_ENABLED` would keep creator jobs off cloud entirely (deploy setting); (c) scorecard check 11 should be extended to drive the voiceover writer.


## [2026-10-31] Prompt fidelity, text-explainer unit: text_explainer and guided_story are built as stories on the phone montage route (Assos check 2 direction, check 4)

**Decision:** the planner's `direction` reaches the phone montage. Before, `plan_unified_montage` always built hard-cut `fast_montage` (`fast_cuts`), whatever `strategy.direction` said. Now `text_explainer` and `guided_story` are built as story beats on the same route, not routed to the specialist/`draft_edit_proposal` path (owner decision). `app/pipeline/story_direction.py` turns the fast plan the route already made into a story: the same clips in the same order with the same source windows (so every creator ask the route reads is already in them, no selected clip is dropped and a stated length is not moved), shared out into beats of at most four clips. `story_structure` gives the chapters: each outline line is a beat's topic and its placeholder caption (enough beats for four clips each and one per line, the outline spread over them in order). The text step is the unchanged `compose_creation_text` path (research, composer, the animation look): its captions replace the placeholders; placeholders it leaves are dropped once it wrote any text (`_drop_unused_story_seeds`), and stay when it wrote none. The guided compiler then holds every moment at least the direction's own minimum (`guided_story._DIRECTION_POLICY`: text_explainer 1.8 s, guided_story 1.4 s) with crossfades and fade-in text.

**Length (card == recipe):** the minimum hold rides the pace rule. `length_policy.story_hold_floor_s` (relaxed pace raises it to the relaxed hold, 2.5 s) feeds `paced_selection(story_floor_s=...)`, used by approval-time `build_length_plan` (via `planner._enrich_best_parts_snapshot`, only where `story_direction.story_blocker` finds nothing) and by the render's no-approved-plan selector. With no creator length, a catalog song section nobody stated (reference-only on the phone) is extended by the least the holds need, recorded as `length_plan["story"]` (Assos: 18.0 s section, 16 clips, 28.44 s planned = card = recipe). A creator-stated length is never moved: if the holds do not fit it the plain windows stand and the render cuts a fast montage.

**Never fails the creator (all of it is a fallback, nothing raises):** `build_story_plan` returns a reason instead of a plan, and the route keeps the fast montage with `UnifiedMontagePlan.direction_fallback`, when (a) an ask only the fast-cut layout can express is live: per-clip text or shot labels, a freeze or speed change, a hidden part, held photos or mixed-media/cadence timing, repeated footage, curved text, named moments, spoken lines kept whole, a creator song (`per_clip_text`, `freeze_speed`, `hidden_parts`, `photo_timing`, `repeated_footage`, `curved_text`, `named_moments`, `spoken_windows`, `creator_song`); (b) the holds do not fit the length in force (`holds_do_not_fit`: the strict compile is dry-run); (c) the phone is not verified for `crossfade` (`crossfade_unavailable`); (d) the plan has no outline (`no_story_lines`) or does not compile; (e) after planning, the text step cannot place the story's text or the phone dry-run (`validate_proposal_phone_compiles`) rejects it: `generative_build._composed_plan` composes the fast plan instead (`story_text_failed` / `phone_rejected`). A provider timeout/quota/budget error is not the story's doing and is raised as before.

**Disclosure:** a text_explainer cut as a fast montage says so, before approval where the draft knows it (`route_disclosure._story_verdict`, the same pure rules over the live asks and the pinned length plan: "I'll cut this as a fast montage instead of a text explainer: ...") and with the finished video otherwise (`unified_montage.repair_notes`). A built story drops the false "direction / pacing is not applied" lines; the `story_structure` line becomes "sets the chapters (which clips are grouped together), but it won't reorder your clips". `guided_story` is the planner's default shape, not something a creator asks for, so its fallbacks are recorded (`direction_fallback`) but not said. **Check 4 is satisfied by that disclosure, not by reordering**: the outline groups the clips into chapters (and seeds each beat's topic; the text step's own captions replace the placeholders), not a clip order and not the on-screen words.

**Kill switch:** `STORY_DIRECTIONS_ON_MONTAGE_ENABLED` (default `true`, `app/config.py`). `false` + restart api and worker is byte-identical to before (`tests/pipeline/test_story_direction.py::test_the_kill_switch_gives_back_the_plan_the_route_made_before`). Apply: `fly secrets set STORY_DIRECTIONS_ON_MONTAGE_ENABLED=false --app nova-video` (api + worker) + restart.

**Premises (each tested):** the guided compiler places a moment on the first best moment with room (`guided_story._source_window`), so the plan's own window is put first among the clip's `best_moments`; the strict compile is the same dry run `compose_creation_text` and the replay already make; a story plan's placeholder rows are `guided-thought-story-beat-N` and nothing else uses that prefix; a removal addresses a bar by its place in the lane the program started from (removed bars keep their place), so cleanup indexes the base lane.
**Failure modes listed before coding, pinned in `tests/pipeline/test_story_direction.py`:** the build fails or blocks a render; a selected clip is dropped; a stated length moves; an ask only the fast layout carries is lost; card and render disagree (length, layout); the text step fails on the beat layout and fails the render; the kill switch is not byte-identical; a creator song or the lip-sync route picks up a story; a named entrance is overridden by the direction's default fade; placeholder prose is left on screen beside real captions; a phone without crossfade is asked for one.
**Touched beyond the route (small, each switch-guarded):** `researched_text` treats a story placeholder as replaceable (a research window may land partly over it, and it is not an "opening" to preserve, so the first beat can be captioned); `_plays_entrance` does not count the direction's default fade-in on a placeholder as a decided entrance (a creator-named edge still lands); the guided compiler's non-fast title plays a typed `title_animation` (`animation_phases`) like the fast labels branch does.

**Harness changes (scorecard, not product):** the shared `score_29_ekim.run_post_approval` takes `extra_features` and the Assos replay verifies `crossfade` (real prod does: `tests/_prod_profile.PROD_VERIFIED_FEATURES`; the shared list names only what 29-Ekim needed). The recorded caption-binding answer names research windows by id, which only mean something against the timeline they were written for; `score_assos._Sequence(rebind=True)` keeps each caption's words, evidence and picture quote and re-seats it on the first free window of the plan that shows that picture and fits (identity on the plan it was recorded against). `story_direction.py` joins the consumer scan's route files. The xfail marker on check 2 is removed.

**Not done / limits:** (1) `story_structure` does not reorder clips; a deterministic lexical clip-to-line match was judged too noisy to ship (the planner's `editorial_plan`, when present, already orders clips by beat), so reversing the outline changes chapter text, not order. (2) `guided_story` shares the switch and the machinery but has no live-prod measurement: a plain montage whose planner direction is `guided_story` and whose text step writes nothing now shows the outline lines as chapter captions; if that reads wrong in prod, narrow `story_direction` to `text_explainer`. (3) The plan's source window per clip reaches the story as the first best moment, widened by 0.4 s for the crossfade overlap (a moment renders its overlap longer than it is held) and pulled earlier where the clip's tail is short; on Assos 15 of 16 moments start where the plan's window starts and one starts 0.36 s earlier. (4) A window that uses a clip whole cannot also pay the crossfade overlap, so such a plan falls back to the fast montage (`holds_do_not_fit`); analysed clips always leave slack. (5) The offline replay proves the structure, not that the live model's captions fit the beat windows; a live model that picks a window the placement rules refuse falls back to the fast montage (`story_text_failed`), at the price of a second text pass. (6) The longer holds are chosen from the strategy and the live asks (`story_floor_for`), before the plan-level blockers (`_plan_blocker`: spoken windows, closing speech, named moments, creator still holds, asset holds) are known. A text_explainer that then falls back to the fast montage for one of those reasons keeps the windows it was planned with, so its cuts are the story's holds (1.8 s, relaxed 2.5 s), longer than a plain fast montage's, while the finished video says it was cut as a fast montage. This is deliberate: the approved windows are the card's length, and re-cutting without the floor would break card == recipe (Assos check 5) and move a length the creator already approved. Follow-up if it reads wrong in prod: move the plan-level blockers that are knowable from the strategy into `story_blocker` so the card never promises the longer holds.

## [2026-10-31] CLAUDE.md lines condensed for the SPEAKER_BACKGROUND_ENABLED flag line (KRI-1117)

CLAUDE.md was 37,989/38,000 chars. Two lines were shortened; their previous text is kept verbatim here.

- `SOUND_EFFECTS_ENABLED` / `MEDIA_OVERLAYS_ENABLED` — default **`false`**; gate the SFX/overlay lane routes in `routes/plan_items.py` (404 off). **Dual-flag trap:** `NEXT_PUBLIC_` twins on Vercel; frontend on + backend off ⇒ saves 404. Fly first, then Vercel; restart api + worker. Detail: `docs/pipelines/generative.md`, DECISIONS.md "CLAUDE.md flag detail".

- **Pre-PR text-overlay verify (`make verify-overlays`):** before any PR touching the Skia renderer, overlay layout, or the burn dict. Renders the REAL Skia path in the prod image, asserts overlays un-clipped, writes `.overlay-verify/{montage.png,report.json}`. Library: `app/pipeline/overlay_verify.py`; CLI: `app.cli.verify_overlays`; guard: `tests/pipeline/test_overlay_verify.py`. **Agent pre-PR gate:** run it, read both files, fix any FAIL, then `/ship`.

## [2026-11-01] Prompt fidelity wave 2 (KRI-1097 follow-ups)

**Decision:** what a render route does with a plan is data, one module per route under `app/kria/route_capabilities/` (`unified_montage`, `voiceover_montage`, `talking_head`, `narrated`, `lipsync_song`, `speech_excerpt`, `slide_post`). Each exports `FIELD_HANDLING` (strategy field -> `Applied` | `RecordedOnly` | `NotApplied(builder)`), `ASK_DECLINES` (typed brief asks it cannot do), `READERS` (its code, for the consumer gate) and optionally `NOTICES` / `story_verdict`. `resolve_route_id` picks the route for a draft from the dispatch's own predicates (`brief_checks.defers_*`, `render_route.resolve_route` over the facts the contract will carry). `route_disclosure` reads the table of the resolved route, so a lip-sync or spoken-excerpt draft no longer gets the unified montage's lines and voiceover / Talking / Narrated / slide drafts get their own. The unified table moved without changing its output (Assos report identical). Seeds are today's honest handling: every audited silent drop is `NotApplied`; a lane that starts honoring a field flips it to `Applied`, and the gate refuses an `Applied` claim no reader in `READERS` backs.

**Card lines for every account:** route lines and route declines no longer need `approval_readiness_for`; `KRIA_ROUTE_DISCLOSURE_ENABLED` stays the kill switch. A creator song blocks story directions on the card as it does at render (`creator_song`), and the song's lyric captions count as the caption style being applied (`song_captions`).

**Feature gaps disclosed, not built (unified montage):** a typed captions-on ask (no speech captions on that route outside a song's lyrics), a title-scope edge or middle position (only corners reach a title, through the phone text style pass), and a crossfade boundary in `mixed_media_timing` (that profile always cuts hard). "No text" and "bigger text" have no typed brief fact, so nothing can decline them without reading wording (KRI-667); they wait for the brief writer to record one.

**caption_style "none":** disclosed on Talking and Narrated, not rendered off. The creation path has no captions-off input (the item stores "none" as NULL, which the worker reads as sentence captions; the only off switch is the editor's `caption_meta.enabled`), and `strategy_policy` already repairs "none" to "auto" with a notice for those formats; the registry line is the backstop when a "none" reaches the card.

**Kill switches (all default true; false + restart api and worker = byte-identical to before that change):** `ROUTE_REGISTRY_ENABLED` (this foundation: off = unified montage only, readiness-bound), and one per follow-up lane, reserved now so lanes never edit `config.py` together: `VOICEOVER_ROUTE_FOLLOWS_PROMPT_ENABLED`, `TALKING_HEAD_ROUTE_FOLLOWS_PROMPT_ENABLED`, `NARRATED_ROUTE_FOLLOWS_PROMPT_ENABLED`, `LIPSYNC_ROUTE_FOLLOWS_PROMPT_ENABLED`, `SPEECH_EXCERPT_ROUTE_FOLLOWS_PROMPT_ENABLED`, `SLIDE_ROUTE_FOLLOWS_PROMPT_ENABLED`, `PLANNER_BOUNDARY_HONEST_ENABLED`, `COPILOT_DIRECTION_FOLLOWS_REQUEST_ENABLED`, `CREATOR_JOBS_NO_CATALOG_SONG_FREEZE_ENABLED`.

**Verification:** `scripts/kria/score_route.py` grades any route fixture (`make kria-replay FIXTURE=<name>` falls back to it): route resolved, each planner-chosen field honored (render evidence only from `scripts/kria/route_replays/<route>.py`) or disclosed, each ask met or declined, the registry gate, and negative controls with the INVALID rule. On code without the registry it grades what that code did, so the drops show red there.

**Limits:** `voice_behind_footage` has no table (its drafts get no lines, never the unified ones). The unified table still lists `licensed_sfx` as unread although KRI-608 says the unified writer materializes a licensed effect; left byte-identical for a lane to verify.

**Hardening after the foundation review:** the grader no longer trusts text. A field counts as disclosed only through its own registry line (`Disclosure` code `not_applied_on_route`, subject = the field) that the creator reads, or a route decline the grader maps to that field (animation -> `title_animation`, captions -> `caption_style`); a title-position decline discloses no field. A lane's render evidence must come from a replay that runs the route's code (traced), returns the recipe, and reports nothing honored with the asks removed or with its lane switch off (else INVALID). An `Applied` claim needs a strategy read in `READERS` or a `via` naming a real `file:symbol` that names the field, and a failing claim is a scored check (`10.claims`). The card never cuts a disclosure: past the 640-character budget the rest are named on one compact line (registry on only). With the registry off, `route_disclosure` reads `route_capabilities/_legacy_unified.py`, a frozen copy of the pre-registry table (guard: `tests/kria/test_route_registry_flag_off.py`, golden from ff5f2adbe). Route declines now reach the card for asks the first draft leaves to the render (`requirements_with_route_declines`), and route resolution mirrors the dispatch for unstamped jobs (`KRIA_PLAN_AUTHORITY_ENABLED=false` ignores `voice_mode`) and for the Talking writer's clip-count gates.

**Second hardening (grade recipe values, not booleans):** two adversaries beat a grader that took a replay module's `honored: True`, so the premise changed. A lane replay module returns render recipe values (`recipe(fx) -> {"title_animation", "pace", "length_s", "caption_mode"}`) and the grader compares each with what the strategy field or typed ask asked, then runs its own controls on the module: the asked value perturbed must move the value, one ask dropped reverts only its key, the lane switch off makes the ask inert, each value must be one the route's `READERS` code returned, and the lane switch may be read only by route code (a probe records the reader). Synthetic constant, ask-keyed, switch-keyed and echo modules must fail every run. A `via` carrier must be a function in the route's own `READERS` or a listed shared carrier whose body reads the field off a strategy (AST); a schema class or another route's file never counts, and two routes may not list the same reader. A route decline speaks for a field only when the declined ask's typed facts and the registry's exact decline text say so. Card lines carry their table entry's typed code (`recorded_only_on_route` for `RecordedOnly`), are graded in English and Turkish, and are pinned by a recorded golden (`tests/fixtures/kria_replay/golden_lines.json`, guard `tests/kria/test_route_lines_golden.py`). Product: outside approval readiness a route decline no longer replaces the planner's summary; its line follows the summary (`reply_from_receipts(declines_after_summary=True)`), so a summary can still name a pace or length the heads-up lines below then say is not applied. The draft route also mirrors the writers' refusals (spoken-excerpt writer off, creator songs off, recorded voice beside kept camera sound).
## [2026-11-01] Research: an unmet part of the ask no longer holds back sourced wording

Prod, "With animations, explain why people should visit Assos, Turkey" (new chat, after the
prompt-fidelity train): the reply was "I can't make this yet: ... Sources could not be
confirmed before approval: animations". The research copy writer listed the creator's
"animations" under `unmet_requests` next to its sourced captions, and
`research_preview.prepare` treated any unmet entry as a failed research result, which the
approval-readiness gate turned into a hold-back. Animation is applied by another step.

- `research_preview.prepare`: when sourced wording exists, unmet entries are shown as
  "Not covered: ..." in the preview note before approval and no longer fail the research.
  With no sourced wording the result still fails with the reason.
- Copy-writer prompt (`nova.research.caption_binding`) v8: animation, look, position,
  timing and music asks are never `unmet_requests`.
- Kill switch: `RESEARCH_UNMET_IS_NOTE_ENABLED` (default true). `false` + restart api and
  worker restores the hold-back.
- Not changed: at render time (no pinned research) an unmet entry still stops the text
  composition (`creation_text_composition`, pinned by
  `test_initial_creation_research_replays_without_searching_again[True]`). Prod renders use
  the pinned preview, so this only bites a render without a preview.

## [2026-11-01] Research agents are told which asks another step applies

Second refusal of the same Assos prompt, after the unmet-list fix (#2012): the claim reviewer
(`nova.research.claim_review`) rejected the sourced captions with "The request explicitly
asked to include animations ... not listed in unmet_requests", so research failed with
"Research needs correction" and the draft was held back. The research writer and reviewer read
the whole creator request, so a style ask in it looked like a research ask nobody covered.

- `ResearchCopyInput.handled_elsewhere` (default empty) carries the live asks another step
  applies: "animation" (any live animation ask) and "text position". Set by
  `research_preview._handled_elsewhere` before approval and by the text composer at render.
  The writer (v9) and the reviewer (v4) prompts say never to list or reject for them.
- No new flag: the field is empty unless an animation/position ask is live, so every other
  request sends the same data as before. Prompt versions bumped; replay evals pass.
- Verified offline with a reviewer that rejects when the data does not name the ask. The real
  Gemini reviewer has not been run against this prompt (paid live runs need an approved ledger).

## [2026-11-01] Research proven + a look judged after render is approvable (Assos, third refusal)

Third refusal of "With animations, explain why people should visit Assos, Turkey" (prod 07:54,
after #2012 and #2013): "I can't make this yet: ... I can't confirm this draft does it yet."
Research had passed. Reproduced locally with the real Main Creator and real Gemini research
(`scripts/kria/live_assos_turn.py`, reports under `.dev/assos-live/`). The Main Creator writes
ONE requirement `r1` (text, global, no literal) whose facts hold both `research` (topic, quote)
and `animation: "any"`. Its receipt is `met` + `unchecked` ("the text will animate in as it
appears; I'll check it in the finished video"), because the research part is proven by the
preview and the animation is judged on the finished video (`brief_checks._research_with_animation`).

Root cause (`approval_readiness._no_pre_render_checker`): any requirement carrying a research
fact returned "a draft check can settle this", so an unchecked receipt became
`evidence_missing`, which holds approval. That rule is right for research that is NOT proven
(no preview, a limit, a failure) and wrong once the sourced wording is on the card: what is left
is the look, which no draft can show. Premise tested, not assumed: the live row was
`evidence_missing/brief_checks` with `prepared_research.status == "ready"` and three texts, and
the same draft with the fix is `unverified`.

- A requirement whose research the preview proved (`_check_research` is `met` + `checked`)
  falls through to the checks of its other facts, so its look becomes `unverified` (approved,
  named after render). Research that is not proven still blocks; the claim reviewer, the copy
  writer and the preview are untouched. Keep-text-off marks still block.
- Same class checked: research with a named animation, a position, a font or a colour on one
  requirement. Animation shapes were held, position/font/colour were already approved because
  `_check_research` answers first (their receipt reads "checked"; that overclaim is not
  changed here).
- The journey file `test_kri881_research_before_approval.py` never turned the readiness gate on
  (code default off, prod on), which is why three research fixes shipped without seeing it. The
  new rows run the gate on, with `r1` recorded from the live run (`assos/live_r1.json`).
- Kill switch: `KRIA_RESEARCHED_LOOK_AFTER_RENDER_ENABLED` (default true). `false` + restart api
  and worker restores the old hold exactly (pinned by
  `test_kill_switch_off_holds_the_researched_animation_ask_as_before`).

## [2026-11-01] Research before approval: the writer retries, truncation is not a source limit

Fully live local runs of the Assos prompt on main (real Main Creator, search, writer, reviewer),
after #2012, #2013 and #2015, still refused or stalled in 3 of 3 runs, each for a different
reason: the copy writer ran out of its 8,192 output tokens (Gemini 3 counts thinking against
it) and the preview filed that as "no usable cited facts from institutional sources" (a
deterministic limit, cached for an hour); the writer's captions overlapped ("Research
explanations overlap") and the first miss ended the turn; a reading-time conflict asked a
question instead of choosing. A live prod thread also ended "I couldn't turn that into a plan"
(Main Creator `TerminalError` after 5 s; cause not recoverable, errors are masked and prod DB
reads were not available).

- Writer (`nova.research.caption_binding` v10): `max_attempts=2` with
  `enable_clarification_retries=True` so the runtime's KRI-542 retry at "low" thinking runs on a
  truncation; `max_output_tokens` 8192 -> 16384.
- `research_preview._schema_failure`: a truncated answer is transient (never a source limit,
  never cached); a malformed answer still is.
- `run_researched_revision`: one correction round. A rejected answer (overlap, timing,
  evidence, the claim reviewer) goes back to the writer once with the reason (`correction`).
  Kill switch `RESEARCH_CORRECTION_ROUND_ENABLED` (default true; false = no correction round).
- Not changed: the reading-time question ("longer edit or fewer parts") and the Main Creator
  planning failure. Both are listed on KRI-1097.
- Verified: 3 of 3 fully live runs on the fixed code ended on the approval card with sourced
  wording (`scripts/kria/live_assos_turn.py --live`); the same code before the change refused or
  stalled 3 of 3. Small samples; the models vary run to run.

## [2026-11-01] Phone text placement never fails a render (KRI-1152)

Job 07efa44c failed `phone_plan_unsupported` ("This text cannot fit clearly in the requested area")
after KRI-1035 (#1927) fixed the same message for long titles. KRI-1035 rested on the premise "only
long titles fail to fit". Refuted: captions, labels, pinned text and lyrics got one attempt at the
saved position (`bottom_left` from the creator's saved style) with faces, the caption lane and the
watermark as hard obstacles, then raised.

- `phone_text_style._place`: every role walks the fit cascade. A saved position is soft and moves to
  the next clear anchor. A position requested this turn (`position_requested`) or authored on the
  row stays fixed. Last resort keeps the text in frame and overlaps the least important obstacle
  (`SACRIFICE_ORDER`: earlier text, caption lane, unlabeled, faces, watermark) and emits a
  `text_fit_relaxed` pipeline event with the row id, role, text, requested position and obstacles.
- `measure_box` only raises its fit declines when given a `floor` (a fit probe). Re-measuring a placed
  row never fails a render.
- Text groups (`text_stack_v1`) retry at their own corner, then each other corner, when the saved
  corner is crowded; the layout that gives way only the least important obstacles is kept, with a
  receipt. A corner requested this turn is never abandoned.
- A pinned or label row whose corner came from its lane (no saved or requested position) keeps that
  corner; it relaxes in place with a receipt rather than jumping to another corner.
- Each placement has its own probe allowance (`_PLACE_PROBE_BUDGET`), so a caption block cannot
  starve the rows styled after it. A spent allowance falls through to best-effort with reason
  `measurement_budget`. The old global "too many complex text layouts" decline is gone.
- Kept: `needs_choice` stays for font coverage and outline-width declines; its chat copy is generic.
  `phone_plan_repair._place_added_contract_text` (confirmed exact text) still declines and is not
  covered here.
- Guard: `tests/pipeline/test_phone_text_fit_never_fails.py` (roles x saved positions x obstacle
  sets, text groups, replay of job 07efa44c conditions). No burn-dict field changed, so the
  renderer-parity guard `test_both_renderers_honor_text_anchor_left` is untouched.

## [2026-11-01] Prompt fidelity wave 2: lipsync

Lane L4 of KRI-1097 wave 2 (creator-song lip-sync montage, `pipeline/lipsync_montage.py`).

**Decision:** the lip-sync titles enter the way the creator asked. `plan_lipsync_montage` takes the
bound brief's live asks (`requirements`, a plan parameter the render-entry pin allows) and stamps
`snapshot.title_animation` (`_title_entrance`): a named animation ask on the title or on all the
text wins (a `style` ask such as "animate the title in" counts: `animation_ask(styles=True)`),
then the plan's own `title_animation`, then an unnamed ask's plain fade. `guided_story` plays it on
the lip-sync opening and closing titles (`_lipsync_title_motion`) in the form the phone verified
(`text_entrance_form` / `entrance_patch`: phases with `authoredText`, the legacy effect with
`animatedText`, static otherwise). The runner passes the asks: `_run_phone_unified_montage_job` ->
`_plan_phone_user_song_montage` -> `_plan_phone_user_song_core` -> the entry. Table:
`title_animation` is `Applied(via=plan_lipsync_montage)`; the animation decline now fires only for
an ask the titles cannot play (per-clip, one clip, "none"), so the card no longer says the title
won't animate when it will.

**Still disclosed, not honored:** a stated length (the song span owns the length), the pace
(`RecordedOnly`: cuts follow the song), the song section (no typed ask exists), a story direction
(`creator_song`) and a caption style with no lyric captions.

**Kill switch:** `LIPSYNC_ROUTE_FOLLOWS_PROMPT_ENABLED` (default true). `false` + restart api and
worker: no entrance is stamped, the projection ignores one already stamped, and every animation
ask is declined again. Proof: the fixture's recipe (snapshot, plan record and compiled text
elements, with and without an opening and closing title) dumped on the foundation and on this lane
with the switch off is byte-identical; with it on the only differences are `title_animation` and
the two title rows' `animation_phases`.

**Limits:** (1) `FIELD_HANDLING` is static, so with the switch off the plan-field line "I can't
animate the title" does not come back (the typed-ask decline does). (2) A plan with no title words
records the entrance and nothing plays it, and the card says nothing; the unified montage has the
same gap. The `lipsync_song` fixture is this case (r3 is honored at `snapshot.title_animation`, the
grader's recipe path). (3) With an entrance stamped and title words present, the creation text pass
runs the composer instead of skipping it (`creation_text_composition`: an entrance match does not
prove segmentation), as on the unified montage. (4) The fixture's `replay_inputs` (song analysis,
take positions) are reconstructed; the fixture builder does not carry them. (5) No native iPhone
render or export was checked.

**Verified:** `make kria-replay FIXTURE=lipsync_song` 15/15 VERIFIED with valid controls (4.title_animation
and 5.r3 turned green and left `lane_targets.json`); the other seven fixtures report what they did on
the foundation. A two-take lip-sync plan with a title, a closing title and a typed "typewriter"
style ask, compiled to the phone recipe with production features: both title layers enter with
typewriter and the dispatch capability gate accepts the recipe; switch off, both are static.
