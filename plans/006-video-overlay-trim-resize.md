# Plan 006 — Video-overlay smart trim + resize-on-approve (delta on plan 005)

**Status:** IMPLEMENTED 2026-07-02 (T1-T4 shipped on claude/inspiring-leakey-d96083; live-verified on localhost)
**Base:** plans/005-auto-media-overlay-sfx.md (PR0 + core shipped on this branch)

## Problem

Video assets already upload, analyze, match, and render (plan 005 shipped the
plumbing). Two gaps make video-into-video feel dumb:

1. **A suggested video always plays from 0:00.** The matcher emits no
   `clip_trim_*`, and `_analyze_video` discards Gemini's `best_moments`. A 30s
   screen recording matched to a 4s spoken window shows its first 4 seconds —
   not the segment that actually demonstrates the thing being said.
2. **Card size is take-it-or-leave-it at approval.** The rail preview renders
   the aspect-aware slot size (5A); the creator can't tighten or enlarge a card
   before Apply, even though the schema clamps and the Apply path already
   accept an edited envelope.

## What already exists (reuse, don't rebuild)

- Pool video upload E2E: AssetPool accepts mp4/quicktime → proxy upload →
  `probe_video` (server-side duration/aspect) → `ClipMetadataAgent` analysis.
- Render: `media_overlay.py` honors `clip_trim_start_s/end_s` (trim filter) and
  `tpad=stop_mode=clone` (freeze-last-frame when the clip is shorter than the
  window). Nothing to change in FFmpeg land.
- Envelope (005/5A): `OverlaySuggestion.overlay` embeds `MediaOverlay` verbatim —
  trim + scale fields flow through Apply with existing validation.

## Backend delta

### 1. Persist the video content map (`_analyze_video`)
- Keep `best_moments` (list of `{start_s, end_s, energy, description}`) and
  `duration_s` in `Asset.analysis` instead of dropping them.
- Stamp `analysis_version: 2` in the payload.
- No schema change: `analysis` is already JSONB.

### 1b. Self-healing backfill (eng decisions 1-A + C, 2026-07-02)
- When the matcher encounters a READY video asset whose analysis is
  **`analysis_version < 2` AND `source != "stub"`** (pre-006 REAL analyses), it:
  suggests WITHOUT trim this run, emits
  `record_pipeline_event("autoplace_stale_analysis", asset_id=…)`, and
  re-enqueues `analyze_pool_asset(asset_id, refresh=True)` in the background.
  The next Re-match sees full data. Nobody waits; data heals itself.
- **Stub analyses NEVER trigger backfill** (finding 2: keyless machines would
  loop forever — every re-analysis yields another stub). If a Gemini key appears
  later, a separate one-shot retry can be triggered by re-uploading or a future
  admin action — out of scope here.
- **`refresh=True` keeps `status="ready"` throughout** (finding 3): the asset
  never leaves the matcher pool and the rail button never flickers off; only the
  analysis payload is swapped on completion.
- Heuristic matcher (finding 8): `end = start + min(4.0, pacing_cap)` — the
  keyless path obeys 2-A instead of a hardcoded 4s; dev fixture set gains a v2
  video asset WITH best_moments so trim is developable/testable keyless.

### 1c. Trim runtime guards (eng decision B, 2026-07-02 — outside-voice findings 1/7/11)
- `MediaOverlay` gains cross-field validators: `clip_trim_start_s < clip_trim_end_s`
  and `clip_trim_end_s ≤ clip_duration_s` (when both set) — every write path
  (agent, manual, edited envelope) is guarded, not just evals.
- `pick_trim_window` server clamps: `start ∈ [0, max(0, duration − window)]`;
  `asset.duration_s` None ⇒ NO trim emitted (never TypeError).
- **Ordering pinned:** trim is computed AFTER the window's final clamps
  (word-snap, min-extension, pacing cap, duration clamp) — trim_dur always equals
  the final window or less, so tpad padding math stays correct.
- Apply route re-validates edited envelopes: per-card overlap check against
  existing cards + each other, trim fields re-clamped — 005-6A's "drop the bad
  item, keep the set" extends to edited cards.

### 2. Deterministic content-aware trim (`build_suggestions`)
- New rule-based step for `kind == "video"` suggestions — NO new LLM decision
  [Layer 1], so the matcher prompt and `prompt_version` stay untouched:
  - window = the placement's `end_s − start_s` (anchored to spoken words).
  - Pick the best_moment with the highest energy whose duration ≥ window;
    else the one with the largest overlap potential; else start at 0.
  - `clip_trim_start_s = moment.start_s`,
    `clip_trim_end_s = min(moment.start_s + window, asset.duration_s)`.
  - **Freeze cap (eng decision D, 2026-07-02):** if the asset is SHORTER than
    the window, the window shrinks to `max(asset.duration_s + 1.0s freeze
    allowance, _MIN_ON_SCREEN_S)` — a ≤1s tpad freeze is acceptable; a 6s frozen
    frame (finding 10) is impossible. Speech anchoring keeps `start_s`; only the
    tail tightens.
  - **Main-duration provenance (eng decision D):** pacing input = persisted
    variant duration keys → `words[-1].end_s + 1.0` WITH a trace event — the
    silent `60.0` default is DELETED; tests assert the source, not just the clamp.
- **Window-length pacing (the "based on the main video's length" rule; eng
  decision 2-A, 2026-07-02):** `_MAX_ON_SCREEN_S` becomes
  `clamp(main_duration * 0.15, 4.0, 10.0)` — a 20s video caps pop-ins at 4s;
  a 60s video allows up to 9s. Applies to ALL suggestion kinds (pacing is a
  rhythm rule; rhythm doesn't care about card type). Deterministic,
  eval-asserted. This INTENTIONALLY changes image-suggestion window caps on
  short videos — the regression promise below is scoped accordingly.

### 3. Envelope passthrough
- `build_suggestions` writes the trim fields into the embedded `MediaOverlay`
  dump. Apply/dispatch untouched (validated passthrough already exists).

## Frontend delta

### 4. Suggested cards edit in OverlayLane (outside-voice tension A, 2026-07-02 —
### implements 005-4A's never-shipped lane rendering; supersedes this review's 3-A)
- **The gap 006 originally patched around is a 005 implementation gap:** wireframe C
  + decision 4A locked "suggested cards render in the lanes with provenance styling;
  drag/trim of a suggested card stages it" — PR2 shipped the rail but skipped lane
  rendering. 006 closes it instead of building a second editor.
- `OverlayLane` renders PENDING suggestion cards (dashed lime-600 border + ✦ badge)
  alongside manual cards; `SfxLane` renders their child SFX diamonds the same way.
- **Size editing = the lane's EXISTING interactions, inherited:** popover scale
  slider + position presets, drag to move, edge-drag window trim, TrimLane clip
  trim. Any edit implicitly STAGES the suggestion (005-4A semantics) and updates
  the staged envelope the rail's Apply already sends — zero new editing surface,
  zero new gesture code, mobile behavior = the lanes' existing behavior.
- The rail stays the review index: reasons, ✓/×, "Apply N to video", Dismiss, row
  click seeks the preview (005-1A). The rail mini-preview becomes read-only; video
  cards in it seek to `clip_trim_start_s` for their poster frame so the creator
  previews the ACTUAL segment.
- **3-A superseded:** no `keep_out` API field — lane edits get manual-card parity
  (no live keep-out warning exists for manual cards today; parity preserved, one
  fewer geometry contract).
- Accept transition on stage: dashed→solid + ✦ fade (005-6A tokens, already specced).

## Tests

- `overlay_autoplace` unit tests: trim picks highest-energy fitting moment;
  overlap fallback; shorter-than-window asset → freeze-cap shrink (decision D);
  pacing clamp at 20s/60s/120s mains; trim fields land in the envelope dump.
- `_analyze_video` test: best_moments + duration persisted into analysis.
- Eval assertion: every video suggestion's `clip_trim_end_s − clip_trim_start_s`
  ≤ window + ε, and trim window ⊆ [0, asset.duration_s].
- Backfill (1-A): stale video asset in match → suggestion emitted WITHOUT trim,
  `autoplace_stale_analysis` trace recorded, `analyze_pool_asset` re-enqueued
  exactly once; `analysis_version: 2` stamped by the new analysis.
- Pacing (2-A): window caps asserted for image AND video suggestions at 20s /
  60s / 120s main durations (clamp 4.0 / 9.0 / 10.0).
- Trim guards (B): schema cross-field validator tests (start≥end 422s,
  end>clip_duration 422s); `pick_trim_window` clamp matrix incl.
  `duration_s=None` → no trim; trim-after-final-window ordering test
  (pacing-capped window ⇒ trim_dur == final window); Apply re-validation drops
  an edited card overlapping an existing one (set survives, 005-6A).
- Backfill (C): stale REAL analysis → trace + one re-enqueue with
  `refresh=True`; stub analysis → NO re-enqueue ever (loop test: two matches,
  zero dispatches); `refresh=True` keeps status "ready" throughout.
- Freeze cap + duration provenance (D): 3s asset in 9s window ⇒ window
  shrinks to ~4s (asset+1s); duration source asserted (variant key → words-end
  fallback WITH trace; no silent 60.0 anywhere).
- Lane rendering (tension A / 005-4A): OverlayLane renders pending suggestions
  with provenance styling; scale-slider edit updates the staged envelope and
  implicitly stages the row; SfxLane renders child diamonds; Apply body carries
  lane-edited scale/trim; rail mini-preview is read-only and seeks video posters
  to `clip_trim_start_s`.
- **Regression (iron rule):** with no videos in the pool, the suggestion set is
  identical EXCEPT window caps (2-A's intentional pacing change); trim fields
  never appear on image cards; manual-card lane behavior byte-identical.

## Failure modes (eng review)

| Codepath | Realistic failure | Test | Handling | User sees |
|---|---|---|---|---|
| pick_trim_window | moments malformed/empty | ✓ | falls to no-trim | card plays from 0:00 (today's behavior) |
| backfill re-enqueue | broker hiccup | — | best-effort try/except (005 register pattern) | nothing; next match retries |
| trim vs asset duration | end_s > duration (stale probe) | ✓ (eval assert) | server clamps end to duration | correct segment |
| resize drag | scale beyond clamps | ✓ | schema clamps at Apply + client clamps live | handle stops at bounds |
| mini-preview seek | signed URL expired mid-session | — | video element error → poster fallback (existing) | static tile |

No critical gaps (every failure has handling + honest-or-silent-correct UX).

## Worktree parallelization

Sequential implementation, no parallelization opportunity — backend trim rule
(PR-A) and frontend resize (PR-B) share the suggestion envelope contract and
`SuggestionRail.tsx` context; two small PRs land back-to-back on this branch.

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [x] **T1 (P1, human: ~1d / CC: ~40min)** — backend — `_analyze_video` persists
  best_moments + duration + `analysis_version:2`; matcher backfill on stale assets (1-A)
  - Files: `app/tasks/autoplace.py`, `tests/`
  - Verify: pytest backfill + persist tests
- [x] **T2 (P1, human: ~1.5d / CC: ~1h)** — backend — `pick_trim_window` pure function
  (clamps + None-guard + after-final-window ordering, decision B) + freeze cap +
  duration provenance (decision D) + pacing clamp all-kinds (2-A) + heuristic cap tie-in (C)
  - Files: `app/services/overlay_autoplace.py`, `app/tasks/autoplace.py`, `tests/`
  - Verify: pytest unit + eval assertions
- [x] **T2b (P1, human: ~half-day / CC: ~20min)** — backend — MediaOverlay cross-field
  validators + Apply-route re-validation of edited envelopes (decision B)
  - Files: `app/agents/_schemas/media_overlay.py`, `app/routes/plan_items.py`, `tests/`
  - Verify: pytest 422 + overlap-drop tests
- [x] **T3 (P1, human: ~2d / CC: ~1.5h)** — frontend — 005-4A lane rendering: pending
  suggestions in OverlayLane/SfxLane with provenance styling; lane edits stage the row +
  update the staged envelope; rail mini-preview read-only + trim-start poster seek
  - Files: `OverlayLane.tsx`, `SfxLane.tsx`, `SuggestionRail.tsx`, item page state wiring
  - Verify: jest suite additions
- [x] **T4 (P2, human: ~2h / CC: ~15min)** — tests — pacing 20s/60s/120s matrix +
  pacing-scoped image regression + backfill loop/status tests + freeze-cap matrix
  - Files: `tests/`, `src/apps/web/src/__tests__/plan/`
  - Verify: full suites green

## NOT in scope
- Trim-range hand-editing in the rail (OverlayLane's TrimLane already does
  this post-apply; duplicate UI would drift).
- Position (x/y) drag in the rail — post-apply OverlayLane owns spatial edits.
- Agent-chosen trim (deterministic rule first; revisit if precision eval says
  the rule mis-picks).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Codex CLI not installed; outside voice single-model (Claude subagent: 11 findings, 4 decision-bearing — all resolved) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAN (PLAN) | 14 issues, 0 critical gaps — decisions 1-A/2-A/3-A(superseded)/A/B/C/D all folded |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | inherits 005's reviewed design (wireframe C + 4A); 006 IMPLEMENTS the locked lane rendering rather than adding new UI vocabulary |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** outside voice (fresh-context Claude subagent) verified the render PTS math, then overturned the draft's core UI approach: the "size not editable" gap is 005-4A's never-shipped lane rendering, and OverlayLane already owns mature scale/trim editing — resolved by implementing 005-4A (rail mini-preview editor dropped, this review's 3-A superseded). Also folded: trim runtime guards (whole-render crash class), version-keyed non-looping backfill (keyless stub loop), freeze cap + honest duration provenance.
- **VERDICT:** ENG CLEARED — ready to implement (T1 → T2/T2b → T3 → T4).

NO UNRESOLVED DECISIONS
