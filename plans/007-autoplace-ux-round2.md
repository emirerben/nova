# Plan 007 — Autoplace UX round 2: auto-match on Generate, on-preview editor, timeline hydration (delta on 005/006)

**Status:** IMPLEMENTED 2026-07-02 (Fix 1+2+3 shipped; zero-click chain live-verified end-to-end on localhost)
**Source:** live user testing 2026-07-02 (three findings, screenshots)

## Problems (user-reported, verified)

1. **First render ignores the pool.** Generate renders the variant; visuals sit
   unused until the user manually clicks "Place visuals for me". The pool's
   whole promise is automation — the first thing the user sees should already
   know about their visuals.
2. **Size/position not editable ON THE VIDEO (second time asked).** 006 routed
   editing to the OverlayLane popover slider. The user's actual ask, verbatim:
   "click and arrange visual size and the position on the video screen where
   you approve the placements" — direct manipulation on the hero preview during
   review, not a slider in a lane popover.
3. **Applied overlays/SFX invisible in the timeline after render.** VERIFIED BUG:
   `page.tsx:1490-1502` syncs `overlayCards`/`sfxPlacements` state from the
   variant ONLY on `variant_id` change. After Apply→burn→refetch the variant_id
   is unchanged, so the applied `media_overlays`/`sound_effects` never reach the
   lanes until a full page reload. Screenshot shows empty OVERLAYS/SFX lanes on
   a variant with 2 baked visuals.

## Fix 1 — Full automation on Generate (eng decision D2-B, 2026-07-02)

**Semantics: zero-click.** Generate → render → auto-match → **auto-APPLY** —
the first video the user watches already carries their visuals. This
intentionally converts 005's review gate into optional post-hoc editing FOR THE
GENERATE PATH (user's explicit direction: "use them directly"); the manual
"Place visuals / Re-match" path keeps the review flow unchanged.

- **Hook site (outside-voice CRITICAL-1, tension G1-A):** the dispatch lives
  AT/AFTER `_finalize_job` (never per-variant-ready — finalize's whitelist
  rewrite would strip `overlay_suggest_*`/`media_overlays` written mid-render),
  gated on `content_plan_item_id` (finalize also serves public generative jobs).
  Pinned by a test.
- **Variant scope (tension G2-A):** speech-bearing variants ONLY
  (original_text / talking_head / narrated). Song variants are skipped + traced —
  Whisper on music yields garbage anchors; manual "Place visuals" remains
  available there. Also cuts Whisper cost/latency from ≤3 to ≤1 per generate.
- **Idempotency (CRITICAL-3):** a per-render-generation `autoplace_attempted`
  marker on the variant — the overlay/SFX burn's own "ready" completion, retext
  re-renders, and acks_late re-deliveries NEVER re-fire matching. Test:
  overlay-burn completion does not re-enqueue.
- **Visibility (CRITICAL-2):** the TASK persists
  `overlay_suggest_status="matching"` (today only the route sets it) and the
  page's `isTerminalFn`/polling continues while an auto chain is active — the
  receipt line + D4-A hydration key off real state; the result is never
  invisible.
- Chain: enqueue `match_overlay_suggestions(auto_apply=True)` per eligible
  variant (existing task gains the flag; autoplace queue).
- `auto_apply=True`: after building+persisting suggestions, the task
  **re-reads them under the row lock** (a concurrent dismiss/asset-delete
  between persist and apply must win — the 005-finding-8 class), merges into
  `media_overlays`/`sound_effects`, and fires the single-chain dispatch
  (005-10A path) through the shared helper. Suggestions consumed on apply;
  zero matches ⇒ nothing burns, wishlist waits.
- **Shared-helper contract (G1-A):** `apply_suggestions_to_variant()` extracted
  from the apply route with three hard rules — (a) NEVER commits internally
  (caller owns commit semantics: route `await db.commit()`, task sync commit),
  (b) raises a plain domain error the route maps to HTTPException and the task
  maps to trace+skip, (c) performs the overlap re-validation + merge + dispatch
  + suggestion-clear as one unit. Route/task parity test pins it.
- **Dedicated kill switch (tension G3-A):** `OVERLAY_AUTOAPPLY_ENABLED`
  (default false; local .env true) gates ONLY the zero-click apply — killing
  bad auto-apply in prod never kills manual suggest. Flag matrix:
  `MEDIA_OVERLAYS_ENABLED` off ⇒ auto-apply degrades to suggest-only + trace
  (never a silently-swallowed 404 pile-up); SFX child drop when
  `SOUND_EFFECTS_ENABLED` off is traced.
- **Precision-gate note (finding 10):** 005's ≥80% golden-set threshold was
  rated for review-gated suggestions; auto-apply burns unreviewed — the gate
  rubric must be re-baselined for the auto path before the PROD flag flips
  (rate "would a creator accept this burned-in without review?").
- Reversible by design: `pre_media_overlay_video_path` clean base persists;
  applied cards are visible in the timeline (Fix 3) and editable/removable with
  the mature lane tooling.
- Guards: flag off ⇒ no dispatch (byte-identical); empty pool ⇒ no dispatch;
  best-effort try/except (render success never gated on matching/apply);
  auto-apply respects the render_status guard (409-safe if the user re-rendered
  meanwhile — trace + skip, suggestions stay pending for manual review).
- UX during the post-chain burn: the variant briefly flips to rendering again;
  receipt copy "Adding your visuals in…" (D12 receipt on completion). The 005
  precision gate still governs the prod flag flip.

## Fix 2 — On-preview direct-manipulation editor (review-time)

- New `HeroOverlayEditor` layer over the hero video preview, ACTIVE ONLY while
  pending/staged suggestions exist (review mode):
  - Each kept suggestion card renders at its real position/size over the video
    (same math as the existing CSS overlay stack: center %, width = scale·100%).
  - **Drag body** → patches `x_frac`/`y_frac` (position becomes "custom").
  - **Corner handle (bottom-right, ≥44px)** → patches `scale` around the card
    center, clamped to schema range [0.05, 1.0].
  - Every gesture goes through the EXISTING `onSuggestionEdit(id, patch)` from
    006 — implicitly stages the row, zero new state machinery, zero network
    until Apply (stage-fires-no-network contract holds).
  - Card visual: dashed lime-600 + ✦ while pending; solid when staged (006 tokens).
  - Keyboard: focused card arrows = move 1% (shift = 5%), `+`/`-` = scale ±0.05.
  - `prefers-reduced-motion` unaffected (no animation here beyond the 006
    stage transition); touch: pointer events with `touch-action: none` on the
    editor layer only.
- Time-scoped: a card is visible/manipulable only while the video's currentTime
  is inside its window (matches render truth); scrubbing via row-click (005-1A)
  brings each card up for editing.
- Post-apply editing stays in the lanes (006) — this editor is the APPROVAL
  surface the user asked for; applied cards are manual cards with mature lane
  tooling. NOT duplicated here.
- **Supersede record (tension G4-A):** this NEW pre-apply gesture surface
  formally supersedes 006-tension-A's "zero new editing surface" — user's
  explicit repeated direction. Pending-card LANE editing (006 T3) STAYS:
  time-domain gestures (window drag, clip trim) live in the lanes, spatial
  gestures (position, size) live on the hero — same `onSuggestionEdit`
  envelope, one state, two complementary surfaces.
- **Interaction hardening (findings 11/12):** editor layer disables pointer
  events pass-through to native video `controls` only while a drag is active;
  drag PAUSES playback (card can't unmount mid-gesture; resumes on release);
  suggested cards get signed pool-asset URLs (already served by
  `listPoolAssets.display_url`); a code comment + dev assert pins the
  container-box == content-box assumption (`aspect-[9/16]` + 1080×1920 output —
  the percent math depends on it). Mobile: the hero editor works with pointer
  events + `touch-action: none` on the layer; 005-7A's bottom sheet peek must
  not cover the hero while editing (sheet collapses during a drag).
- **Copy fix (finding 13):** the Apply-race 400 ("All edited suggestions
  overlap…") gets no-fault wording: "These placements were just updated —
  refresh to see the latest." The 409-skip trace notes suggestions may be
  cleared by the re-render's lifecycle (not "stay pending").

## Fix 3 — Timeline hydration after burn (bug fix)

- Root cause: the state-sync effect keys on `variant_id` only
  (`page.tsx:1490-1502`) — written when cards were exclusively client-created;
  the Apply flow (005) made server-side card mutations invisible to it.
- Fix (eng decision D4-A, 2026-07-02): re-sync `overlayCards` + `sfxPlacements`
  from the refetched variant when `variant.render_finished_at` changes (the
  existing post-burn signal the page already watches to clear
  `localPreviewUrls`) — same effect, one more concern; no always-sync (protects
  in-flight unsaved lane edits; clobber impossible — no edit session exists at
  burn completion).
- SFX included: the user's "voice and visuals" — both lanes hydrate.

## Code quality specs (eng review)

- `apply_suggestions_to_variant(job, variant_id, suggestions, user_id)` —
  ONE helper, extracted from the apply route, used by BOTH the route and the
  `auto_apply` task path (overlap re-validation + merge + single-chain dispatch
  + suggestion clearing). Route and automation can never drift.
- `overlayCardStyle(overlay)` — ONE frontend util for card CSS positioning
  (center %, width = scale·100%, translate(-50%,-50%)); consumed by the hero
  stack, the rail mini-preview, and HeroOverlayEditor. Three call sites, zero
  copies.

## Performance (eng review — honest numbers per finding 8)

- Auto-match: ≤1 eligible (speech-bearing, G2-A) variant per generate ⇒ 1
  Whisper run (downloads the rendered variant; ≤90s bounded) + 1 Gemini call.
- Auto-apply burn: one re-encode for that variant, serialized on the solo
  overlay-jobs worker; visuals land ~2-5 min after variants_ready (Whisper +
  match + burn), announced by the persisted "matching"→receipt states — never
  silently.
- Bounded: density cap on cards; `autoplace_attempted` guarantees the chain
  runs at most once per render generation.

## Sequencing (finding 14)

Fix 3 (hydration) + the poll-continuation piece of Fix 1 are HARD PREREQUISITES
for Fix 1's UX (without them the auto-applied result is invisible). Order:
Fix 3 → Fix 1 backend chain → Fix 2 editor (independent, parallelizable).

## Tests

- Auto-match chain: variant ready + flag on + pool ready ⇒ one dispatch per
  ready variant; flag off ⇒ zero dispatches (byte-identity); empty pool ⇒ zero;
  dispatch failure never fails the render (best-effort).
- Auto-apply (D2-B): `auto_apply=True` merges all suggestions + fires ONE
  dispatch through the shared helper; suggestions cleared after; zero matches ⇒
  no burn + wishlist persists; render_status busy ⇒ skip apply + trace +
  suggestions stay pending; route/task parity test pins the shared helper.
- HeroOverlayEditor (jest): drag patches x/y via onSuggestionEdit (no fetch);
  resize patches scale within clamps; gesture stages an un-kept row; keyboard
  move/scale; card only rendered inside its time window; touch-action set;
  applied (non-suggestion) cards NOT editable here.
- Hydration: simulated refetch with changed `render_finished_at` ⇒ lanes receive
  applied cards + SFX; unchanged render_finished_at ⇒ no clobber of local edits.
- Regression: manual overlay upload flow + 006 lane editing untouched (suites
  stay green).

## NOT in scope

- Editing APPLIED cards on the hero (lanes own post-apply editing — one editor
  per lifecycle stage, no duplicated gesture code).
- Auto-apply/burn without review (D2 decided auto-suggest; revisit after the
  precision gate ships golden-set numbers).
- Pool-tile subject labels for videos while Gemini File API is slow (cosmetic,
  separate).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Codex CLI not installed; outside voice single-model (Claude subagent: 14 findings incl. 2 CRITICAL — all resolved) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAN (PLAN) | 17 issues, 0 critical gaps — D1/D2-B/D4-A + tensions G1-A/G2-A/G3-A/G4-A folded |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | inherits 005/006 reviewed design; hero editor is the user's explicit verbatim ask |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** outside voice code-proved Fix 1's draft mechanism would ship corrupted state (finalize whitelist rewrite), an invisible result (poll stops at variants_ready), and a burn→match refire loop; all three closed (post-finalize hook, task-persisted status + poll continuation, autoplace_attempted). Speech-only auto-apply kills the Whisper-on-music garbage class; 006-tension-A formally superseded by the hero editor (lane editing of pending cards retained for time-domain gestures).
- **VERDICT:** ENG CLEARED — ready to implement (sequence: Fix 3 → Fix 1 → Fix 2; T1→T5).

NO UNRESOLVED DECISIONS
