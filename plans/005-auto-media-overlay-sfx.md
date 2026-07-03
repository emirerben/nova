# Plan 005 — AI auto-placement: transcript-matched media overlays + sound effects

**Status:** DRAFT — under design review
**Reference:** TikTok @qbuilder/7651054516120341791 (54s creator explainer: talking-head A-roll,
PiP screen-recording cards popping in at the exact spoken moment, word captions, SFX on pop-ins).
**Scope decision (D1):** automate matching + placement from the creator's OWN asset pool.
Generated diagram B-roll (assets that don't exist yet) is explicitly a follow-up plan.

## Problem

Nova can already burn media overlays and sound effects onto a variant — but the creator
does 100% of the placement work by hand: pick the asset, drag it to the right second,
size it, pick an SFX, place it. The sample video has ~8 overlay events in 54s; placing
them manually is 10–15 minutes of scrubbing. The hard part of the product is deciding
**which asset appears at which spoken moment** — that's what we automate.

## User journey (creator perspective, mirrors the sample video)

1. **Film + upload.** Creator uploads a talking-head clip to a plan item (existing flow).
   Variant renders with captions (existing).
2. **Add visuals.** Creator drops screenshots / screen recordings / photos into the item's
   asset pool. Each asset is analyzed on upload (what it shows, subject, on-screen text).
3. **Auto-place.** Creator clicks "Place visuals for me". The matcher reads the word-level
   transcript + asset descriptions and drafts overlay placements (asset, start_s/end_s,
   position, scale) and SFX events at each pop-in.
4. **Review.** Draft placements appear as AI-suggested cards in the existing Overlay and
   SFX timeline lanes (CSS preview — nothing is burned yet). Creator accepts all, or
   tweaks/removes individual cards with the drag/trim interactions they already know.
5. **Accept → render.** Accepted placements go through the existing dispatch
   (`dispatch_set_media_overlays` / `dispatch_set_sound_effects`) and burn in one pass.
6. **Download** bakes SFX (existing v0.6.3 behavior).

## Backend

### PR0 (foundation, outside-voice finding 2): the asset pool itself
The pool does NOT exist today (no model, routes, or UI — overlay uploads are per-card
presigned PUTs). Build first; everything else depends on it:
- `Asset` model: `{id, plan_item_id, user_id, gcs_path, kind, content_hash, duration_s,
  aspect, analysis (JSONB), status, created_at}` + Alembic migration (bumps the
  migration head-pin test — known gotcha).
- CRUD/list routes (ownership-scoped); persistent non-swept GCS prefix
  (`users/{uid}/plan-pool/…` family).
- Pool upload UI on the item page with the 2A analyzing/failed states.
- **Cap + dedupe (finding 9):** max 20 assets/item (route + UI enforced, inline reason);
  `content_hash` dedupe skips re-analysis of identical bytes.

### Asset analysis at upload (stills + video, findings 3)
- **Stills** → new `image_metadata` agent: input GCS image, output
  `{subject, description, on_screen_text, kind_hint}` — Gemini Flash, same AgentSpec
  pattern, replay-mode eval fixture in `tests/evals/`.
- **Video (screen recordings — the reference's core asset type)** → existing
  `ClipMetadataAgent` wired onto pool uploads + server-side `ffprobe` for
  `duration_s` and `aspect` (client-side probing is not trusted for matching).
- Analysis runs at upload time (async, non-blocking, traced), persisted on the Asset row.
- **Keyless dev mode (finding 10):** a dev-only fixture flag serves a recorded
  `OverlaySuggestion` set for any variant, so frontend PRs develop + test without
  GEMINI_API_KEY.

### New: `overlay_placement` agent (the matcher)
- **Input:** transcript `Word[]` (from existing `TranscriptAgent` output), asset metadata
  list `{asset_id, kind, description, duration_s}`, variant duration, archetype.
- **Output:** `placements: [{asset_id, start_s, end_s, position, scale, confidence,
  reason, sfx_intent}]` — validated against existing `MediaOverlay` clamps.
- **Model:** Gemini 2.5 Flash, `thinking_budget` set (structured matching, not creative).
- **Placement vocabulary (decision 5A, 2026-07-02; aspect-aware per outside-voice
  finding 4):** the agent picks from named slots only — `top` (y_frac 0.18, base scale
  0.72 — the sample-video PiP card), `center` (y_frac 0.50, base scale 0.80), `full`
  (scale 1.0 takeover, landscape assets only). x always centered. Freeform x/y stays
  human-only (drag). **The server resolves slot × asset aspect → effective scale** so
  the rendered bbox stays inside its zone (portrait assets shrink or auto-promote to
  `center`; `scale` is width-fraction, height follows source aspect — a portrait
  screenshot at raw 0.72 would stand ~88% of canvas height). Eval asserts the RENDERED
  bbox against the keep-out rectangles, not slot-name compliance.
- **Hard keep-out zones (baked into slots, eval-asserted):** caption band (bottom 25%),
  platform UI rail (right 12%), and the speaker's face on talking_head content
  (`text_safe_zone` from clip metadata biases card away).
- **Constraints in prompt:** max 1 concurrent overlay, min on-screen duration,
  snap start to word boundaries, leave the hook window (first 2–3s) clean unless
  confidence is high, and never overlap existing (manual/staged/accepted) placements —
  current placements are passed to the agent as occupied intervals.
- **SFX mapping is rule-based, not LLM:** `sfx_intent ∈ {pop_in, whoosh, click, none}`
  maps deterministically to curated glossary effects. No agent picks raw audio files.
- **SFX coupling (decision 9A, 2026-07-02):** each SFX is a CHILD of its overlay
  placement — accepted/rejected atomically with it. Rail rows show "+ pop sound" with an
  inline × to strip just the audio. No orphaned sounds possible. Standalone sound-only
  suggestions are out of scope (manual SFX lane still exists).
- **Confidence (decision 10A, 2026-07-02):** placements below threshold are dropped
  server-side. Survivors carry a two-tier signal the agent returns (`confident` | `likely`);
  copy templates own the words — confident rows get declarative reasons, mid rows get a
  "Might match — …" prefix. No numeric scores anywhere in the UI.
- **Volume cap (decision 11A, 2026-07-02):** max 1 suggestion per 5s of runtime,
  ceiling 10 — enforced server-side, stated in the prompt, asserted in evals.
  Over budget → keep highest-confidence.

### Data flow

```
 upload asset ──▶ presigned PUT (users/{uid}/…)          [existing]
      │
      ▼
 image_metadata task (default queue, traced) ──▶ asset metadata persisted
      │                                            {subject, description, on_screen_text}
      ▼
 "Place visuals for me" ──▶ POST /plan-items/{id}/suggest-overlays
      │                        └─ transcript_source() helper → (Word[], transcript_hash)
      ▼
 overlay_placement task (default queue, traced)
      │  input:  words + asset metadata + occupied intervals + duration + archetype
      │  output: placements[{asset_id, slot, start_s, end_s, confidence, reason, sfx_intent}]
      │  server: validate asset_id ∈ pool, clamp via MediaOverlay validators,
      │          drop below-threshold, cap 1/5s ≤10, map sfx_intent → glossary (rule-based)
      ▼
 variants[i].suggested_media_overlays / .suggested_sound_effects   (+ transcript_hash)
      │            (row-locked write: db.get(Job, id, with_for_update=True))
      ▼
 rail review (CSS preview only) ──▶ stage (✓ / drag) ──▶ "Apply N to video"
      │                                                     │ (ONE dispatch)
      ▼                                                     ▼
 clear-pending on retext/swap/re-render        existing dispatch_set_media_overlays
 (hash mismatch → §2 zinc notice)              + dispatch_set_sound_effects → burn
```

### Engineering contract (eng review 2026-07-02, decisions 1A–4A)
- **Celery (1A):** `overlay_placement` + `image_metadata` tasks run on the DEFAULT queue
  (light LLM calls — `overlay-jobs` stays reserved for ffmpeg renders), with
  `soft_time_limit=240, time_limit=300` (invariant: < broker `visibility_timeout=1900`,
  `worker.py`), both wrapped in `with pipeline_trace_for(job_id):` (mandatory orchestrator
  contract). Extend `tests/tasks/test_task_time_limits.py` to cover both.
- **Flags (2A):** backend `OVERLAY_AUTOPLACE_ENABLED` (Fly) + frontend
  `NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED` (Vercel) — keep in sync like the sibling
  overlay/SFX flags. Frontend-on + backend-404 surfaces the D10 quiet error tile, never silent.
- **Transcript source (3A, extended per outside-voice finding 1):** one helper returns
  `(Word[], transcript_hash)` with explicit precedence: `variants[i]["transcript"]` →
  word-timed caption cues → **on-demand bounded Whisper** (90s wall-clock, existing
  in-repo path; result persisted to `variants[i]["transcript"]` so it runs once) →
  `None` (button disabled). The persisted sources alone do NOT cover the flagship
  talking-head-with-original-audio case — the Whisper branch is what makes the feature
  work on its own demo. Hash covers word texts + timings + variant duration. Matcher
  AND lifecycle both call this helper — they can never diverge.
- **Staleness is read-time, not hook-time (outside-voice finding 5):** GET rail data
  and Apply both recompute the hash and compare; mismatch ⇒ clear pending + §2 zinc
  notice. The three named hooks (retext/swap/re-render) remain as fast-path UX only —
  correctness never depends on enumerating mutation routes (caption hand-edits,
  scene/intro-timing patches are automatically covered).
- **Fresh-interval validation (finding 8):** per-item overlap validation (6A) runs
  against placements read under the SAME `with_for_update` lock that persists
  suggestions — never against the task-start snapshot.
- **Write discipline (4A):** task-side suggestion persists use
  `db.get(Job, job_id, with_for_update=True)` like every sibling persist; route-side
  stage/clear uses `flag_modified(job, "assembly_plan")` + `await db.commit()`.
  Route tests assert `db.commit.await_count ≥ 1` (silent-rollback trap).

### Persistence + safety
- **Suggestion schema is an ENVELOPE (decision 5A, 2026-07-02):**
  `OverlaySuggestion{id, confidence_tier, reason, transcript_anchor,
  sfx: SoundEffectPlacement | None, overlay: MediaOverlay}` — embeds the existing
  models verbatim. One validator set; accept = unwrap + copy through existing dispatch.
  No parallel field copies (renderer-parity drift class, killed at the type level).
- **Per-item validation (decision 6A, 2026-07-02):** invalid matcher items (unknown
  `asset_id`, overlap, clamp violation, cap overflow) are dropped individually with a
  `record_pipeline_event` trace; the valid remainder persists. All-dropped ⇒ zero-match
  state. Never fail the whole set on one bad item.
- Suggestions persist on the variant as `variants[i]["overlay_suggestions"]`
  (envelope list, separate from applied lists) with `source: "agent"`.
- Accept copies them into the real `media_overlays` / `sound_effects` fields through the
  existing validated dispatch path — the agent never writes directly to render inputs.
- Kill switch: `OVERLAY_AUTOPLACE_ENABLED` (default false), same fly-secrets pattern.
- Respects existing `MEDIA_OVERLAYS_ENABLED` / `SOUND_EFFECTS_ENABLED` dual-flag gates.

## Frontend

### Entry point
- "Place visuals for me" button on the variant editor, following the §12
  Generate-with-AI token pattern (✦ prefix, zinc border, lime hover).
- Disabled until: transcript exists AND ≥1 analyzed asset in the pool.

### Analyzing state
- Pulse tier (§7): lime ping dot + serif line ("Matching your visuals to the script…"),
  no fake progress bar.

### Suggestion review — **approved direction: checklist rail (wireframe variant C, 2026-07-02)**
- Right-hand rail card ("Suggested edit", Fraunces headline "3 visuals, matched to your
  script") listing each suggestion: thumbnail, filename, time range, one-line REASON
  grounded in the transcript ("You say 'it builds a payload' — this diagram shows it"),
  per-row ✓ accept / × reject, footer Accept all + Dismiss (§12 token pattern).
- AI-suggested cards simultaneously render in the existing `OverlayLane` / `SfxLane`
  with provenance styling (dashed lime-600 border + ✦ badge) — visually distinct from
  manual cards; rail row hover highlights the matching lane card (synced selection).
- CSS-preview only until accepted (existing local preview stack) — no render cost to look.
- Per-card accept/remove also available in the existing card popover.

### Reveal interaction (decision 1A, 2026-07-02)
- On suggestions arriving, the preview auto-seeks to `first_suggestion.start_s − 1s` and
  plays through the pop-in — SFX audible (v0.6.6 LiveEditPreview SFX path). Guard: never
  auto-play while the user is actively scrubbing/interacting with the timeline.
- Clicking a rail row seeks the preview to `row.start_s − 1s` and plays through the
  pop-in. The rail is an index into the video; the video is the primary object.
- Wireframes + approved.json: `~/.gstack/projects/emirerben-nova/designs/overlay-autoplace-review-20260702/`.

### Interaction states (decision 2A, 2026-07-02) — what the user SEES

| Surface | Loading | Empty | Error | Success | Partial |
|---|---|---|---|---|---|
| Asset pool tile | §7 shimmer + `motion-safe:animate-ping` lime dot + "Analyzing…" micro-label per uploading/analyzing asset | Serif invitation: "Drop the screenshots you mention in your script" + upload CTA (leads with action, §9) | Dashed zinc-300 tile, "Couldn't read this file" + Remove action; no red | Thumbnail + subject micro-label from analysis | n/a |
| "Place visuals for me" button | Pulse tier (§7): lime ping + serif "Matching your visuals to the script…"; >60s → D19 "Still working…" (stays Pulse, no tier upgrade) | Flag off → button HIDDEN. No transcript or 0 analyzed assets → visible + `opacity-50 cursor-not-allowed` (§12 disabled tokens) + inline reason TEXT below button, never tooltip-only: "Add at least one visual first" / "Waiting for your transcript" | D10: dashed `border-zinc-200` tile on light surface, "Couldn't match your visuals this time." + single Retry action; raw agent errors never shown | Suggestion rail + reveal interaction (1A) | n/a |
| Suggestion rail | n/a (arrives whole) | **Zero match:** §2 zinc notice line + asset wishlist: "Add a screenshot of the settings toggle you mention at 0:32" | n/a (covered by button error) | Rows + Accept-all footer | **Partial:** place what matched; unmatched moments listed as wishlist lines under the rows |

### Accept semantics + burn timing (decision 4A, 2026-07-02)
- Per-row ✓ (and any drag/trim of a suggested card) only **stages** — no render fires.
- One footer CTA, labeled **"Apply N to video"**, dispatches ONCE. **Single render chain
  (decision 10A, 2026-07-02):** Apply persists BOTH lists, then fires only
  `dispatch_set_media_overlays`; the overlay pass's terminal
  `_reapply_persisted_sfx_if_any` bakes the sound in the same re-encode — never two
  sequential renders (T-SFX-2 composition class). SFX-only accepts (no visuals staged)
  use the cheap SFX-only dispatch (`-c:v copy`). Test pins that the terminal hook fires
  with agent-suggested SFX.
- Burn runs in the background: editor + CSS preview stay fully interactive during it
  (existing `render_status` guard prevents conflicting dispatches). Receipt copy at
  dispatch: "Baking your 3 visuals in — the preview above is exactly what renders."
  On completion: §7 D12 receipt line ("✓ Ready in 2:41"), one amber-free lime pulse.
- Download stays instant (burn completes during natural review time).

### User journey storyboard
| Step | User does | User feels | Plan supports it with |
|---|---|---|---|
| 1 | Uploads talking head | routine | existing flow |
| 2 | Drops screenshots in pool | mild effort | per-asset "Analyzing…" shimmer (2A) — visible progress |
| 3 | Clicks "Place visuals for me" | anticipation | Pulse tier serif line; D19 stall copy past 60s |
| 4 | **Watches the reveal** | delight — "it landed on the word" | auto-seek + play-through with SFX (1A) |
| 5 | Reviews rail rows, tweaks | control, trust | transcript-grounded reason lines; row-click seeks preview |
| 6 | Clicks "Apply 3 to video" | commitment | single dispatch; honest receipt copy (4A) |
| 7 | Keeps editing / leaves | patience, not deflation | editor stays interactive; D12 receipt on completion |
| 8 | Downloads | payoff | instant — burn already done |

### Suggestion lifecycle (decision 3A, 2026-07-02)
- The suggestion set persists with a `transcript_hash` (+ variant duration). Any operation
  that changes transcript or duration (retext, swap-song, re-render) **clears PENDING
  suggestions** and shows the §2 zinc notice line: "Your script changed — suggestions
  cleared. Place visuals again?" Accepted/staged placements are the user's work — never touched.
- Re-running the matcher **replaces all pending** suggestions; never duplicates, never
  touches staged/accepted/manual cards. While pending suggestions exist the button
  relabels to "Re-match visuals".
- **Asset deletion (decision 11A, 2026-07-02):** deleting a pool asset also removes
  dependent PENDING suggestion rows + §2 zinc notice ("Removed 1 suggestion that used
  this file"). Apply re-validates as backstop (defense in depth, test pinned).
- **Storage prefix:** pool assets live under a persistent, non-swept prefix (like
  `users/{uid}/plan-pool/…`) — NOT a 24h-lifecycle path; suggestions must never
  reference sweepable objects.

### Failure modes (eng review)

| Codepath | Realistic failure | Test | Handling | User sees |
|---|---|---|---|---|
| image_metadata task | Gemini timeout/refusal | ✓ (failure→failed state) | task marks asset `failed` terminally | "Couldn't read this file" tile |
| overlay_placement task | hard time_limit kill / crash | ✓ | except-handler writes terminal failed state (never stuck "matching") | D10 error tile + Retry |
| Matcher output | hallucinated asset_id / overlap | ✓ (per-item drop, 6A) | drop + trace | remainder shown; wishlist for gaps |
| Suggest persist | concurrent render write | ✓ (row-lock) | `with_for_update` serializes | nothing (correct) |
| Route stage/clear | silent rollback | ✓ (`db.commit` assert) | explicit commit | nothing (correct) |
| Apply | asset deleted post-match | ✓ (backstop) | eager clear on delete (11A) + re-validate | zinc notice at delete time |
| Task redelivery | in-flight > visibility_timeout | ✓ (limits test) | limits ≪ 1900s | nothing |

No critical gaps remain (every failure has test + handling + visible-or-correctly-silent UX).

### Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| PR0: Asset pool (model + migration + routes + upload UI + cap/dedupe) | `app/models.py`, `app/migrations/`, `app/routes/`, `src/apps/web/.../plan/_components/` | — |
| PR1a: agents + prompts + evals (image_metadata, overlay_placement, video wiring) | `app/agents/`, `prompts/`, `tests/evals/` | PR0 (Asset row shape) |
| PR1b: transcript_source helper (+Whisper fallback) + suggestion persistence + read-time staleness + routes | `app/routes/`, `app/tasks/`, `tests/` | PR1a (envelope schema) |
| PR2: rail + reveal + states (desktop, fixture-served dev mode) | `src/apps/web/.../plan/_components/` | PR1a schema (mock-first), converges on PR1b |
| PR3: mobile sheet + a11y + DESIGN.md | `src/apps/web/`, `DESIGN.md` | PR2 |

Lane A: PR0 → PR1a → PR1b (sequential, shared schema). Lane B: PR2 mock-first against the
PR1a envelope schema + fixture dev mode → converges on PR1b merge. PR3 after PR2.
Conflict flags: PR0 and PR2 both touch `plan/_components/` (pool UI vs rail) — coordinate
or sequence; PR2/PR3 same directory — sequential.

### Responsive (decision 7A, 2026-07-02)
- **Desktop (≥md):** three-zone composition per approved wireframe C — phone preview /
  timeline / right rail (~290px).
- **Mobile (375px):** rail becomes a swipeable bottom sheet over the preview. Peek state:
  headline count + "Apply N" CTA. Expanded: rows at ≥44px tap height. Video stays visible
  above the sheet; row tap seeks the preview (1A). Sheet motion uses `t-modal` tokens (§6).
- Timeline lanes keep their existing mobile behavior; provenance styling unchanged.

### Accessibility (decision 8A, 2026-07-02)
- Suggestions arriving: `role="status" aria-live="polite"` — "3 suggestions ready."
- Rail rows: keyboard focusable; Enter/Space = accept, Delete/× = reject;
  `focus-visible` rings per §8; focus mirrors the hover→lane-card highlight sync.
- All targets ≥44px on mobile. `prefers-reduced-motion` disables the auto-play reveal
  (1A becomes seek-without-play) and the accept transition (6A) per D17.

### DESIGN.md updates (same PR — decision 6A, 2026-07-02)
- New provenance token (§2): **dashed lime-600 border + ✦ badge = AI-suggested,
  provisional** — fourth dashed-border meaning, distinct from empty/failed/add-input.
- Accept transition (§6): card border animates dashed→solid, ✦ badge fades out,
  ~250ms `t-modal`-class easing, `motion-safe:` guarded. Accepting = the card becomes yours.

## Non-goals / NOT in scope (considered and deferred)
- **Generating new visual assets (diagram B-roll)** — the sample's animated payload
  sequence; research-grade motion-graphics generation, own plan (D1 scope decision).
- **Standalone sound-only suggestions** — SFX without a visual (9A); manual lane covers it.
- **Auto-placement for template/music jobs** — generative + plan-item variants only.
- **Beat-synced SFX** — music pipeline owns that.
- **Freeform agent coordinates** — slot vocabulary only (5A); freeform stays human-drag.
- **Numeric confidence display** — language carries confidence (10A).
- **Auto-rematch after transcript changes** — explicit re-match keeps user in command (3A).

## What already exists (reuse, don't reinvent)
- `MediaOverlay` / `SoundEffectPlacement` schemas + FFmpeg apply passes + dispatch routes.
- `OverlayLane` / `SfxLane` / `TextLane` drag-trim UI with undo/redo (v0.6.x).
- `TranscriptAgent` word timestamps; `phrase_sequence.py` grouping; LiveEditPreview
  with SFX playback (v0.6.6).
- `ClipMetadataAgent` (`best_moments`, `text_safe_zone`, `visual_density`); footage pool.
- Design idioms: §12 Expand-with-AI proposal card, ✦ Generate-with-AI button tokens,
  §7 Pulse/Theater tiers, §2 notice line, D10 failure tone, SeedProvenanceBadge precedent.
- `AgentSpec` runtime + `tests/evals/` replay harness.

## Approved Mockups

| Screen/Section | Mockup Path | Direction | Notes |
|---|---|---|---|
| Suggestion review (desktop) | ~/.gstack/projects/emirerben-nova/designs/overlay-autoplace-review-20260702/variant-C-final.png | Checklist rail (variant C) | Rail = index, video = primary object (1A); "Apply N to video" single CTA (4A); "+ pop sound ×" child rows (9A); hedged copy tier (10A); dashed-lime provenance (6A) |
| Alternates considered | …/variant-A.png (banner-led), …/variant-B.png (quiet lane-led) | rejected 2026-07-02 | kept for reference |

## Tests (eng review 2026-07-02: decisions 7A/8A/9A — full coverage)

**CRITICAL regression (iron rule, blocks ship):** the lifecycle clear hooks modify
existing retext / swap-song / re-render routes → regression test: each of those
operations with NO suggestion set present behaves **byte-identically** to today.

Backend (pytest):
- `transcript_source()` helper: variant-transcript branch, caption-cue fallback branch,
  `None` branch, hash covers words+timings+duration (4 tests).
- Placement agent evals (replay, CI): slot vocabulary only, keep-out zones, volume cap
  1/5s ceiling 10, no overlap incl. occupied intervals, confidence tiers present, hook
  window clean, per-item drop (unknown asset_id / overlap / clamp / cap → item dropped,
  remainder kept, trace emitted; all-dropped ⇒ empty set).
- `image_metadata` eval fixture (5 screenshot types) + analysis-failure ⇒ asset
  `failed` state test.
- Suggest/persist task: row-locked write (`with_for_update`), hash stored, re-run
  replaces pending only (staged/accepted/manual untouched).
- Lifecycle clears: retext clears pending; swap-song clears pending; re-render clears
  pending; accepted+staged survive every clear.
- Stage/Apply routes: single dispatch on Apply, existing validation path,
  `db.commit.await_count ≥ 1` asserted (silent-rollback trap), flag off ⇒ 404.
- `sfx_intent → glossary` mapping: unit test per intent (deterministic).
- `tests/tasks/test_task_time_limits.py` extended to both new tasks.

Frontend (Jest):
- **Stage-fires-no-network contract:** per-row ✓ and drag/trim dispatch NO API call;
  only "Apply N to video" does — pinned by test, not convention.
- Reveal: auto-seek+play on arrival, scrub guard blocks auto-play, row-click seek.
- Re-match replaces pending rows (no duplicates); hedged copy tier rendering.
- Error tile on backend-404 with frontend flag on (dual-flag surface, never silent).
- `useSfxPreview` hook tests (bundled T-SFX-1, decision 8A): audio positioned at
  `video.currentTime − at_s`, gain clamped 0–2, play/pause/seek lockstep, future-start
  timeout scheduled + cleared on pause/ended/unmount. Closes the standing TODO.
- Mobile sheet peek/expand + a11y (aria-live announce, keyboard ✓/×, focus-sync).

Evals / prompt rule (decision 9A + precision gate per outside-voice tension 4):
- Both new prompts get `prompt_version` in their AgentSpec from day one.
- Replay evals in CI. **Live eval run required before merge** (~$2-5, needs
  GEMINI_API_KEY machine — flag pending-in-CI when developing on keyless machines).
- Matcher fixtures: 3 transcripts × asset pools (perfect / partial / zero match).
- **Precision gate (B+):** a golden set built from REAL creator footage; suggestion
  precision ≥80% (human-rated "correct moment + correct asset") required before
  `OVERLAY_AUTOPLACE_ENABLED` flips in prod. Manual lanes
  (`MEDIA_OVERLAYS_ENABLED`/`SOUND_EFFECTS_ENABLED`) flip on alongside — they are the
  fallback UX. LLM self-reported confidence is treated as uncalibrated: the tier only
  drives copy hedging (10A), never the gate.

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~3d / CC: ~2h)** — backend — `overlay_placement` agent: slot vocabulary,
  keep-out zones, occupied intervals, volume cap, confidence tiers, sfx_intent
  - Surfaced by: Pass 4 issue 5 + outside-voice findings 3/10/14
  - Files: `src/apps/api/app/agents/overlay_placement.py`, `prompts/`, `tests/evals/`
  - Verify: `pytest tests/evals/ -v` structural assertions
- [ ] **T2 (P1, human: ~1d / CC: ~45min)** — backend — `image_metadata` agent + upload-time analysis
  - Surfaced by: plan core (asset intelligence for stills)
  - Files: `src/apps/api/app/agents/image_metadata.py`, asset pool routes
  - Verify: eval fixture + route test
- [ ] **T3 (P1, human: ~2d / CC: ~1h)** — backend — suggestion persistence + `transcript_hash`
  lifecycle (clear pending on retext/swap/re-render; re-run replaces pending)
  - Surfaced by: Pass 2 issue 3 (decision 3A) + outside-voice findings 4/7
  - Files: `src/apps/api/app/routes/plan_items.py`, `generative_jobs.py`, `tasks/generative_build.py`
  - Verify: route tests incl. `db.commit()` await assertions
- [ ] **T4 (P1, human: ~3d / CC: ~2h)** — frontend — SuggestionRail (desktop) + provenance styling
  + stage/Apply-once semantics + "+ pop sound ×" child rows + hedged copy tiers
  - Surfaced by: approved wireframe C + decisions 4A/9A/10A
  - Files: `src/apps/web/src/app/plan/_components/` (new SuggestionRail.tsx, OverlayLane, SfxLane, PlanVariantEditor)
  - Verify: jest + `npx tsc --noEmit`
- [ ] **T5 (P1, human: ~1d / CC: ~30min)** — frontend — reveal interaction: auto-seek+play on
  arrival, row-click seek, scrub guard
  - Surfaced by: Pass 1 issue 1 (decision 1A)
  - Files: `LiveEditPreview.tsx`, SuggestionRail
  - Verify: jest interaction tests
- [ ] **T6 (P2, human: ~1d / CC: ~30min)** — frontend — full state coverage: per-asset analyzing
  shimmer, D10 error tile + retry, gated states with inline reasons, D19 stall copy
  - Surfaced by: Pass 2 issue 2 (decision 2A) + outside-voice findings 5/11/12
  - Files: asset pool components, SuggestionRail
  - Verify: jest state tests
- [ ] **T7 (P2, human: ~2d / CC: ~1h)** — frontend — mobile bottom sheet (peek/expanded, 44px rows,
  t-modal motion)
  - Surfaced by: Pass 6 issue 7 (decision 7A)
  - Files: SuggestionRail (sheet variant), globals.css
  - Verify: jest + 375px manual pass
- [ ] **T8 (P2, human: ~1d / CC: ~20min)** — frontend — a11y contract: aria-live announce, keyboard
  ✓/×, focus-sync, reduced-motion guards
  - Surfaced by: Pass 6 issue 8 (decision 8A) + outside-voice finding 15
  - Files: SuggestionRail, LiveEditPreview
  - Verify: jest + keyboard manual pass
- [ ] **T9 (P2, human: ~1h / CC: ~5min)** — docs — DESIGN.md: dashed-lime provenance token (§2) +
  accept transition motion (§6)
  - Surfaced by: Pass 5 issue 6 (decision 6A) + outside-voice finding 13
  - Files: `DESIGN.md`
  - Verify: same-PR diff review
- [ ] **T10 (P2, human: ~1d / CC: ~30min)** — backend — SFX child coupling: atomic accept/reject,
  rule-based `sfx_intent` → glossary mapping, strip-audio endpoint behavior
  - Surfaced by: Pass 7 issue 9 (decision 9A) + outside-voice finding 9
  - Files: suggestion schema, `plan_items.py`
  - Verify: route tests

_No new tasks from Pass 3 (journey fixes are copy inside T4/T6)._

Eng review additions (2026-07-02):

- [x] **T0 (P1, human: ~3d / CC: ~2h)** — backend+frontend — Asset pool foundation: model,
  migration (+head-pin bump), CRUD routes, persistent prefix, upload UI, cap 20 + content-hash dedupe
  — **DONE 2026-07-02** (PR0 implemented on claude/inspiring-leakey-d96083: `PlanItemAsset` +
  migration 0063, 4 flag-gated routes, `AssetPool.tsx` + plan-api client, 22 pytest + 8 jest tests)
  - Surfaced by: outside-voice finding 2 (pool doesn't exist) + finding 9
  - Files: `app/models.py`, `app/migrations/`, `app/routes/plan_items.py`, `plan/_components/`
  - Verify: migration test + route tests + pool UI jest
- [ ] **T11 (P1, human: ~1d / CC: ~45min)** — backend — `transcript_source()` helper with
  Whisper fallback branch (bounded, persist-once) + 5 unit tests + read-time staleness check
  - Surfaced by: eng issues 3/tension 1/tension 3
  - Files: `app/services/`, `app/routes/plan_items.py`
  - Verify: pytest helper suite
- [ ] **T12 (P1, human: ~1d / CC: ~30min)** — backend — CRITICAL regression suite: retext /
  swap-song / re-render with no suggestions ⇒ byte-identical; Celery contract
  (default queue, limits 240/300, `pipeline_trace_for`) + time-limits test extension
  - Surfaced by: Test review iron rule + eng issue 1
  - Files: `app/tasks/`, `tests/tasks/test_task_time_limits.py`
  - Verify: pytest tests/
- [ ] **T13 (P1, human: ~half-day / CC: ~20min)** — backend — single-chain Apply:
  persist both lists, one overlay dispatch, `_reapply_persisted_sfx_if_any` pinned by test;
  SFX-only accepts use the cheap dispatch
  - Surfaced by: eng issue 10 (performance)
  - Files: `app/routes/plan_items.py`, `app/tasks/generative_build.py`
  - Verify: route + task tests
- [ ] **T14 (P1, human: ~1d / CC: ~30min)** — backend — aspect-aware slot resolution
  (server computes effective scale; portrait shrink/promote) + rendered-bbox eval assertions;
  video-asset analysis wiring (ClipMetadataAgent + ffprobe on pool uploads)
  - Surfaced by: outside-voice findings 3/4
  - Files: `app/agents/overlay_placement.py`, `app/pipeline/`, `tests/evals/`
  - Verify: eval geometric assertions
- [ ] **T15 (P2, human: ~1d / CC: ~45min)** — evals — golden set from real creator footage +
  ≥80% precision gate before prod flag flip; useSfxPreview hook tests (bundled T-SFX-1);
  fixture-served keyless dev mode
  - Surfaced by: tension 4 (B+), test issue 8A, finding 10
  - Files: `tests/evals/`, `src/apps/web/src/__tests__/plan/`, dev fixture route
  - Verify: eval run + jest

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Codex CLI not installed; outside voices ran single-model (Claude subagents: design 15 findings, eng 10 findings — all resolved) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAN (PLAN) | 21 issues, 0 critical gaps — all folded (decisions 1A–11A eng + 10 outside-voice resolutions) |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAN (FULL) | score: 2/10 → 9/10, 12 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** outside voice (fresh-context Claude subagent) contradicted 4 locked decisions; all 4 tensions resolved by user: Whisper transcript fallback accepted, read-time staleness accepted, aspect-aware slots accepted, strategy proceeds with a ≥80% precision gate + manual lanes enabled alongside.
- **VERDICT:** DESIGN + ENG CLEARED — ready to implement (PR0 → PR1a → PR1b → PR2 → PR3).

NO UNRESOLVED DECISIONS
