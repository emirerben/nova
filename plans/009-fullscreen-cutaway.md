# Plan 009 — Full-screen cutaway scenes (delta on 005–008)

**Status:** IMPLEMENTED 2026-07-03 — all five tasks landed same-day (T1 inline; T3/T4/T5 parallel agents; T2 inline). Backend +48 tests (5851 green; the only local reds are pre-existing env-coupled flakes that also fail on origin/main), frontend 102 suites / 1111 tests, tsc + ruff clean. Live evals + make local-render remain CI/hand-off gates (keyless/Docker-less dev machine). PR order at /ship: T1 → T3 → T4 → T5 → T2.
**Ask (verbatim):** "I want to also support scenes that are speech/audio only — where
the screen is filled entirely by the image or video the user selects, with no
talking-head or second video overlaid, just the audio playing over that full-screen
visual. … Please think this through and come up with a plan, including how to handle
it cleanly from a user experience standpoint." Reference: https://vm.tiktok.com/ZNRKRrs2H/

## Reference grammar (frame-by-frame, 54.5s source)

Extracted at 1 fps and read as montages (scratchpad `ref-video/`):

| Window | What's on screen | Lesson |
|---|---|---|
| 0–4s | Talking head + big hook text | Cutaways never cover the hook window (AI-side) |
| 5–24s | **Full-screen** dark diagram B-roll, hard cuts | Long cutaway sequences exist in the wild (v1 AI caps shorter) |
| 25–26s | Talking head returns | Hard cut back, no transition |
| 27–35s | More full-screen visuals | Cutaways can chain back-to-back |
| 36–45s | Talking head + **PiP card** (settings screenshot) | Both grammars coexist per-card in one video |
| 48–54s | Talking head + full-screen screenshot + outro | Mixed to the end |

Constants: (1) reference keeps captions ON TOP of cutaways — v1 policy is D2 below;
(2) cutaways are full-bleed, zero chrome; (3) hard cuts only; (4) speech never
ducks, cutaway audio absent; (5) mode is per-card.

## Verified mechanics (workflow map, 3 readers + eng-review verification)

- Burn path: `PUT …/media-overlays` `render:false` persists metadata only
  (autosave); `render:true` → `dispatch_set_media_overlays`
  ([generative_jobs.py:938](src/apps/api/app/routes/generative_jobs.py)) →
  `_run_media_overlay_pass` ([generative_build.py:1307](src/apps/api/app/tasks/generative_build.py))
  → `apply_media_overlays` / `build_media_overlay_command`
  ([media_overlay.py:59](src/apps/api/app/pipeline/media_overlay.py)).
- Cards composite onto `pre_media_overlay_video_path` — copy of the finished
  variant **with text already burned**. SFX re-mix is the outermost layer.
- **Card audio is always dropped** (`-map 0:a?`) → speech continues under a
  fullscreen card with zero audio work, in preview and bake.
- Today's scale is fit-width (`scale={scale·1080}:-2`) — no true takeover exists.
- Agent vocabulary ALREADY has slot `"full"` (`SLOT_NAMES`; prompt rule 5
  promises a takeover) but `resolve_slot` clamps into the caption keep-out band.
  This plan makes the existing promise real.
- Render-path kill switch ALREADY exists: the cover-crop branch's only
  production path is behind `settings.media_overlays_enabled`
  (generative_build.py:2295) — verified during eng review (ARCH-5 refutation).
- `caption_cues` [{text,start_s,end_s}] are persisted for narrated variants at
  first render (generative_build.py:4889/4927) — rule (h)'s data source for the
  flagship case (verified; ARCH-1 refutation).
- **timeout/preset history (E3):** commit `2c560363` removed `preset=fast` from
  the overlay pass after 603s renders vs the hardcoded `timeout=600`
  (media_overlay.py:313); prod runs ONE `concurrency=1` worker draining
  celery+plan-jobs+overlay-jobs (fly.toml:40).

## Settled design

### Data model
`MediaOverlay.display_mode: Literal["pip", "fullscreen"] = "pip"` with a
**coercing** validator (unknown/missing → `"pip"`; version-skew safe, never drops
a card). Fullscreen keeps `x_frac`/`y_frac`/`scale` untouched so toggling back
restores the prior layout; **born-fullscreen cards (no prior pip layout) demote
via `resolve_slot("center")` — one rule on BOTH paths**. Trim + freeze (plan 006)
unchanged for pip. Mirrored in `lib/plan-api.ts` (**owned by T1**). Suggestion
envelopes embed MediaOverlay dumps verbatim → field propagates automatically;
legacy envelopes re-validate as pip.

**Asset dims (E1):** `_analyze_image`/`_analyze_video` persist `width`/`height`
into the analysis JSONB (no migration); `ANALYSIS_VERSION` bumps to 3; the
self-healing backfill loop extends to **image assets too** (today it is
video-only — autoplace.py:430). Dims flow through the assets response +
`PoolAsset` in plan-api.ts so the FE low-res warning has real data.

### Render (FFmpeg) — T1
Fullscreen branch in `build_media_overlay_command`:
`scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1` +
`overlay=0:0:enable='between(t,start,end)':eof_action=pass` (static dims).
`-loop 1` images / trim+bounded `tpad` freeze reused as-is (never `stop=-1`).
**Encoder + timeout are ONE decision (E3):** any pass containing a fullscreen
card uses `preset="fast"` AND raises the subprocess timeout to **1200s** (under
the 1740s Celery soft limit / 1900s visibility_timeout), with a **one-shot
retry at `veryfast` on `TimeoutExpired`** — no render ever dies on the preset.
Update the module docstring (it currently argues the opposite). Pip-only passes
keep `veryfast` + 600s unchanged.
**Queue reality (PERF-2):** prod's single concurrency=1 worker means a fast
pass head-of-line blocks all renders; accepted for v1, recorded here — revisit
worker topology if overlay-edit frequency grows.

### AI decision layer (autoplace) — T2 (LAST in PR order, E10)
- `build_suggestions` branches on slot `"full"` BEFORE `resolve_slot` → emits
  `display_mode="fullscreen"`, default SFX intent "whoosh" (degrades gracefully
  when `SOUND_EFFECTS_ENABLED` is off — intent simply unused).
- Prompt rule 5 rewritten (full = asset IS the thing being described; never in
  hook/intro-text window). `aspect` + `width`/`height` added to
  `assets_payload`. **prompt_version bump + eval (E8):** a minimal
  `overlay_placement` entry joins `tests/evals/` — replay fixtures (slot-full
  worthy AND unworthy sets) + structural checks (never in hook window;
  portrait/screen-recording favored; panorama never), keyless in CI; the LIVE
  eval run is a **CI/hand-off gate** (this machine has no GEMINI key — known
  constraint).
- Server-side enforcement — **mode-aware constants table (single source; these
  OVERRIDE `pacing_cap_s` and `_FREEZE_ALLOWANCE_S` for fullscreen only):**
  | Rule | Value |
  |---|---|
  | (a) hook/intro exclusion | no `start_s < 2.5s`, no intro-text overlap — shift or demote to pip |
  | (b) max takeovers per video | 2 |
  | (c) duration clamp | video 1.5–4.0s; image ≤2.5s |
  | (d) re-anchor gap | ≥1.0s around any fullscreen card |
  | (e) freeze | zero freeze for AI fullscreen video (window clamps to trimmed footage; <1.5s → demote) |
  | (f) aspect | portrait/9:16 = best candidates; panorama >~2.2 demotes |
  | (g) resolution | short side <720px demotes; **fails CLOSED when dims missing** (dims now persisted per E1) |
  | (h) caption-cue avoidance | prefer windows minimizing cue overlap; shorten toward 1.5s floor when unavoidable |
  (The former "~12s total cap" line is DELETED — dead constraint, 2×4s=8s.)
- **Rule (a)/(h) data sources (named):** narrated → persisted `caption_cues`;
  intro-text window → the variant's intro timing fields plumbed through
  `build_suggestions`' signature by its caller. Variants without cue data
  (e.g. montage without editorial sequence) skip rule (h) — stated, not faked.
- **Demote receipt (ARCH-4):** apply-time demotion writes
  `variants[i]["overlay_apply_receipt"] = {demoted: n, reason, at}` under the
  existing row lock; exposed through the job GET serializer + plan-api.ts;
  cleared on next apply/clear. T2 writes it; T5 renders it. Never silent.
- `heuristic_match` (keyless) NEVER emits fullscreen. Variant eligibility:
  agent_text/none montage, narrated, original_text; NEVER `song_lyrics`.
- Kill switch: `FULLSCREEN_CUTAWAYS_ENABLED` — **default FALSE (E2, matching
  every sibling flag)**; rollout step: flip on Fly only AFTER T4+T5 are live on
  Vercel. Render-path rollback is already covered by `MEDIA_OVERLAYS_ENABLED`.
- Mutual exclusion (E4): **mode-aware server check in ONE shared helper called
  from BOTH manual write paths** (`dispatch_set_media_overlays` AND
  `_persist_overlay_metadata_only`): no card may overlap a fullscreen card's
  window (both directions); overlapping pip+pip stays legal (z-order) and gets
  a regression test. The coupled-sites comment names all FOUR sites.

### Timeline UX (OverlayLane) — T3 — light editorial per DESIGN.md §2
- No new lane. Fullscreen chips: taller per-card track row (`h-8` vs `h-6`),
  solid ink fill (`bg-[#0c0c0e] text-white`) + "⛶ Full" glyph (below ~24px
  width: glyph hidden, fill is the identifier, edge handles suppressed).
  **Lime stays exclusively provenance.**
- **Popover stack (fullscreen), top→bottom:** (1) identity header + time range
  + ✕ Remove — never below the fold; (2) segmented **[PiP | Full screen]**
  reusing the `role="radiogroup"` pattern (`PlanVariantEditor.tsx:238`);
  (3) "Fills the whole frame. Your voice keeps playing underneath." + quiet
  button **"Show as small card instead"**; (4) trim + timing fields. Position +
  Scale hidden in fullscreen; fracs never cleared. T3 extracts the popover into
  its own subcomponent (OverlayLane is already 625 lines).
- Scale slider (pip): max label "Full width" + "Make full screen →" affordance.
- **Mid-bake race (E5):** `_run_media_overlay_pass` re-reads `media_overlays`
  under its row lock before write-back and SKIPS the card-list write when the
  persisted list differs from its task args (stale-bake detection) — a mode
  toggle autosaved during a bake is never clobbered. Test: autosave-during-bake
  survives.
- **Warnings (non-blocking, zinc tokens), exact triggers:** hook `start_s<2.5`
  ("Covers your hook"); intro-text overlap ("Covers your intro text");
  trim outrun ("Clip ends early — cutaway will be shortened" + hard `end_s`
  snap + toast — **snap, not freeze, for manual fullscreen**); aspect >1.2
  ("Sides will be cropped", suppressed while analyzing); `min(w,h)<720`
  ("Low resolution — this may look blurry full screen"); manual fullscreen
  total >15s ("Lots of full-screen time — this render may take longer").
- Hatched intro band: zinc (`repeating-linear-gradient`, `zinc-500/30`);
  window timing via `UnifiedTimeline` props.
- FE hard-stops drag/resize at fullscreen boundaries during the gesture; the
  E4 server helper 422s as backstop ("That would overlap a full-screen moment").
- Keyboard: **F** toggles mode when a lane chip has focus (chips gain
  `tabIndex=0`) or the popover is open; guarded against
  input/textarea/contenteditable; fullscreen keyboard gating covers move/resize
  only — never the mode toggle.

### Live preview — T4
- `overlayCardStyle` fullscreen branch → `inset: 0`; media
  `w-full h-full object-cover`, zero chrome; `preload="auto"` on fullscreen
  card videos (first-frame readiness at window entry).
- **Media markup consolidation (honest scope):** one shared
  `mediaClassFor(displayMode)` helper + thin wrappers across the four sites
  (LiveOverlayCardsLayer img + TrimmedVideoPreview, SuggestionRail mini-preview,
  HeroOverlayEditor media) — prop differences (refs, sync, drag, testids) stay
  per-site; the CLASS LOGIC is what's deduplicated.
- **Parity guard = two same-repo tests:** (py) builder branches on
  display_mode; (ts) overlayCardStyle branches on display_mode — both pinned.
- **Asset load failure:** on media `onError`, fullscreen cards render a
  full-frame dashed-zinc tile "This visual couldn't load" + Remove; bake
  BLOCKED with inline copy "1 visual couldn't load — refresh or remove it."
- **Editor chrome ≠ baked chrome:** pending fullscreen suggestions get an INSET
  dashed-lime outline (`outline-offset: -2px`) + inside ✦ badge; applied cards
  zero chrome.
- **Click-to-edit + scrubber (E6):** the cutaway frame is a click target that
  pauses + opens the popover, and it lives in **HeroOverlayEditor** (preserving
  008's "no gestures in LiveOverlayCardsLayer" invariant — stated explicitly).
  The bottom ~15% band is **pointer pass-through** (hit-test; clicks below the
  85% line reach the video) with the ~40% opacity purely visual. Jest asserts a
  band click reaches the video element.
- Hard cuts only; audio: zero work needed.

### Suggestion / approval surfaces — T5
- Full auto-apply parity with pip (D3, upheld after two dissents — guardrails
  are the answer, not a gate).
- Rail rows: 9:16 cover-cropped thumbnail tile; "Full screen" soft pill BEFORE
  the filename; reason line leads with "covers you while you keep talking"
  (+ caption clause when text exists in the window).
- Set-level summary when ≥1 fullscreen suggestion: "2 full-screen moments ·
  5.5s total — they cover you while you keep talking."
- One-tap demote "Show as small card instead" in rail AND popover.
- Renders `overlay_apply_receipt` ("1 visual was shown smaller to protect your
  intro").
- Suggestion-EDIT envelope round-trips `display_mode` verbatim.

### Interaction states

| Surface | Loading | Empty | Error | Success | Partial |
|---|---|---|---|---|---|
| Fullscreen card in preview | poster seek + preload=auto | — | full-frame dashed-zinc tile + Remove; bake blocked | plays cover-cropped | trim-clamped window |
| Suggestion rail (fullscreen rows) | "Matching…" (007) | nothing new | asset 404 → row auto-unstages + inline copy | badge + summary line | mixed pip/fullscreen sets |
| Apply/bake | two-pass rendering status | — | "We couldn't add your full-screen visual. Your video is unchanged — try again." | receipt incl. demotions | server demoted → receipt, never silent |
| Popover warnings | crop warning suppressed until metadata | — | — | — | warnings stack, worst first |

### Accessibility & responsive
- Lane chips focusable (`tabIndex=0`); chip aria-label "Full-screen cutaway,
  6.2 to 9.0 seconds"; radiogroup keyboard inherited; segmented ≥44px touch on
  mobile; F is desktop sugar, popover the guaranteed path; no new motion;
  ink/white AAA contrast; zinc warnings ≥4.5:1.

### Rollout / compat
- PR order **T1 → T3 → T4 → T5 → T2** (E10): the verbatim ask (manual
  fullscreen) ships in the first four PRs; the AI layer lands last with all
  compensating web surfaces already deployed.
- API deploys BEFORE or with web per PR (#296/#297 class).
- `FULLSCREEN_CUTAWAYS_ENABLED` default False → flip on Fly AFTER T4+T5 on
  Vercel (E2). Parent flags must also be on in both Fly + Vercel:
  `MEDIA_OVERLAYS_ENABLED` / `NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED` (dual-flag
  trap); `SOUND_EFFECTS_ENABLED` optional (whoosh degrades).
- E9: server 422s fullscreen cards on `song_lyrics` variants (contract lives at
  the API, not just the disabled FE toggle).
- Coercing validator handles version skew; clear-on-rerender inherited;
  "Remove all cards" keeps the `copy_object` restore.
- **Machine/gate honesty:** live evals + Docker-based checks are CI/hand-off
  steps — this dev machine has no API keys and no Docker (recorded constraint).

## Decisions

**Design review (2026-07-03):** D2 cover-in-v1 + rule (h) (v1.1 committed cue
re-burn — T-CUT-1); D3 auto-apply parity (upheld twice); D4 manual hook
allow+warn; D5 lyrics toggle disabled (+ E9 server 422); D6 snap gesture
fast-follow (T-CUT-2); D7 cover-crop only + warning; P1/P2/P3a/P5/P6/P7
packages as specced above.

**Eng review (2026-07-03):** E1 persist dims + ANALYSIS_VERSION 3 + image
backfill; E2 flag default False + post-T4/T5 flip; E3 fast + timeout 1200s +
one-shot veryfast retry (+ PERF-2 occupancy accepted/recorded); E4 mode-aware
shared overlap helper on both manual write paths + pip+pip regression test;
E5 stale-bake detection under row lock; E6 pointer pass-through band + Hero
OverlayEditor ownership; E7 dual preset assertions + real-ffmpeg smoke test +
verify-overlays dropped from T1 (inapplicable); E8 minimal overlay_placement
eval; E9 lyrics server 422; E10 resequence T2 last; E11 spec-consistency
package (receipt plumbing, data sources named, constants table, plan-api.ts →
T1, parent flags, popover extraction, honest CardMedia, preload, two-test
parity guard). Refuted by verification (no action): ARCH-1 (caption_cues DO
exist for narrated), ARCH-5 (render kill switch exists via
MEDIA_OVERLAYS_ENABLED), Q3 (demote-geometry false dichotomy).

## NOT in scope (v1 cuts, with rationale)

- Caption cue re-burn over cutaways — v1.1, committed (T-CUT-1); incident-prone
  render class, ships whole.
- Letterbox/blur-pad fill; pan-within-crop — one fill policy; warning frequency
  measures demand.
- Ken Burns on image cutaways — 2.5s cap makes static fine.
- Transitions — hard cut + "whoosh" matches reference.
- Audio work — base-audio-only mapping already correct.
- Keyless/heuristic fullscreen — conservative fallback stays pip-only.
- Carry-forward across full re-renders — inherits clear-on-rerender.
- Snap gesture + mobile pinch (T-CUT-2); new approval surfaces.
- Fullscreen on song_lyrics — structurally self-defeating (D5 + E9).
- Worker topology change for queue occupancy — recorded (PERF-2), revisit on
  demand signal.

## What already exists (reuse, don't reinvent)

- DESIGN.md §2 tokens; zinc notice-line pattern; radiogroup segmented pattern
  (`PlanVariantEditor.tsx:238`); provenance vocabulary (dashed lime + ✦).
- Slot `"full"` agent vocabulary + prompt rule 5; `caption_cues` persistence
  (narrated); ANALYSIS_VERSION backfill machinery; `with_for_update` row-lock
  pattern; `MEDIA_OVERLAYS_ENABLED` render kill switch.
- Plan 006 trim/freeze; 007 suggestion pipeline; 008 live-preview architecture
  + its "no gestures in LiveOverlayCardsLayer" invariant.
- `_reapply_persisted_sfx_if_any` two-pass observability; overlay-jobs queue.

## Tests (contract level — expanded per-PR)

- Schema: display_mode default + coercion; toggle round-trip preserves fracs;
  born-fullscreen demote = resolve_slot("center"); trim sanitizer interplay.
- Command builder (T1): cover-crop filter string, `overlay=0:0`, bounded tpad;
  **dual preset assertions** (pip-only ⇒ veryfast; any-fullscreen ⇒ fast, not
  veryfast); timeout=1200 + veryfast-retry path; existing fit-width pins pass.
- **Real-ffmpeg smoke (T1, skip-if-no-ffmpeg):** composite 1 fullscreen image +
  1 trimmed fullscreen video onto a synthetic base; probe output 1080×1920 +
  frames present inside/outside the enable window.
- Autoplace (T2): hook/intro exclusion, max-2, duration clamps, re-anchor gap,
  portrait-best, panorama/low-res demotion incl. **fail-closed on missing dims**,
  dims persistence + image-backfill, rule (h) cue-avoidance, heuristic
  never-fullscreen, kill-switch byte-identical off, receipt written + serialized.
- Eval (T2): `overlay_placement` replay fixtures + structural checks (keyless
  CI); live eval = CI/hand-off gate.
- Apply/overlap (E4): fullscreen-vs-any overlap 422 in BOTH manual paths;
  **pip+pip overlap still saves** (regression); envelope round-trip; legacy →
  pip; lyrics + fullscreen → 422 (E9).
- Race (E5): autosave-during-bake survives write-back (stale-bake skip).
- Frontend (jest): popover stack + toggle; controls hidden in fullscreen; chip
  variants incl. tiny-chip degradation; live layer inset-0 + object-cover;
  onError tile + bake block; click-to-edit pause; **band click reaches video**
  (E6); editor-chrome rule; rail tile/pill/summary; receipt render; demote both
  surfaces; disabled lyrics toggle + copy; F shortcut (focus cases + input
  guard); parity guard (ts half).
- Warnings table: one test per trigger row (6 triggers).
- Coverage summary from review: 27 new code paths + 11 user flows traced;
  all mapped to the list above (0 unowned gaps after E7/E8/E9).

## Implementation Tasks
Synthesized from design + eng review. PR order: **T1 → T3 → T4 → T5 → T2** (E10).

- [x] **T1 (P1, human: ~1.5d / CC: ~40min)** — schema+render — display_mode +
  coercing validator; cover-crop branch; **preset=fast + timeout 1200s +
  veryfast retry + docstring update (E3)**; `lib/plan-api.ts` mirror (E11);
  dual preset assertions + real-ffmpeg smoke test (E7)
  - Files: `app/agents/_schemas/media_overlay.py`, `app/pipeline/media_overlay.py`, `src/apps/web/src/lib/plan-api.ts`, `tests/test_media_overlay_*.py`, `tests/pipeline/test_media_overlay_smoke.py` (new)
  - Verify: `pytest tests/` locally; render-quality evidence via CI (`make local-render` — hand-off gate)
- [x] **T3 (P1, human: ~2d / CC: ~45min)** — timeline+popover — ink h-8 chips,
  popover subcomponent extraction + stack order, radiogroup reuse, warnings
  table (6 triggers), hatched band via UnifiedTimeline props, drag hard-stop,
  F + focusable chips (P6), **E4 server helper callers' FE counterpart**
  - Files: `OverlayLane.tsx` (+ new `OverlayCardPopover.tsx`), `UnifiedTimeline`, jest suites
  - Verify: `npm test` + `npx tsc --noEmit`
- [x] **T4 (P1, human: ~1.5d / CC: ~40min)** — preview — overlayCardStyle
  fullscreen branch, `mediaClassFor` consolidation, onError tile + bake block,
  click-to-edit in HeroOverlayEditor + **pointer pass-through band (E6)**,
  preload=auto, chrome rule, parity guard (both halves with T1)
  - Files: `overlayCardStyle.ts`, `LiveOverlayCardsLayer.tsx`, `HeroOverlayEditor.tsx`, `SuggestionRail.tsx`, `page.tsx`
  - Verify: `npm test`
- [x] **T5 (P2, human: ~0.5d / CC: ~25min)** — rail — 9:16 tile,
  pill-before-filename, set summary, demote both surfaces, **receipt render
  (ARCH-4)**, lyrics disabled-toggle jest
  - Files: `SuggestionRail.tsx`, popover, `plan-api.ts` (receipt type)
  - Verify: `npm test`
- [x] **T2 (P1, human: ~2.5d / CC: ~60min)** — autoplace (LAST) — slot-full
  branch + constants table (a)–(h) w/ named data sources; **dims persistence +
  ANALYSIS_VERSION 3 + image backfill (E1)**; receipt write + serializer;
  kill switch default False (E2); **E4 shared overlap helper both manual
  paths + E5 stale-bake skip + E9 lyrics 422**; prompt bump + **minimal eval
  (E8)**
  - Files: `overlay_autoplace.py`, `overlay_placement.py`, `prompts/overlay_placement.txt`, `tasks/autoplace.py`, `tasks/generative_build.py`, `routes/generative_jobs.py`, `routes/plan_items.py`, `overlay_apply.py`, `config.py`, `tests/…`, `tests/evals/…`
  - Verify: `pytest tests/` locally; live eval = CI/hand-off gate (no keys here)

## Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| T1 | api schema + pipeline + web lib | — |
| T3 | web timeline components | T1 (type) |
| T4 | web preview components | T1 (type) |
| T5 | web rail + receipt type | T1, T2-receipt shape (type-only stub OK) |
| T2 | api services/tasks/routes | T1 |

Lanes: **A:** T1 → T2 (api). **B:** T3 (timeline) and **C:** T4 (preview) in
parallel worktrees after T1 merges — no shared files; T5 follows B+C (touches
SuggestionRail which T4 also edits — sequential after C). Conflict flag: T3/T4
both import plan-api types — additive, low risk.

## Failure modes (per new codepath)

| Codepath | Realistic failure | Test? | Handled? | User sees |
|---|---|---|---|---|
| cover-crop encode | TimeoutExpired at fast | E3 retry test | veryfast retry | slower render, never failed |
| onError preview | expired signed URL (24h) | jest | tile + bake block | clear tile + copy |
| stale-bake write-back | toggle during bake | E5 test | skip clobber | choice survives |
| manual overlap PUT | old client overlap write | E4 422 test | 422 + copy | explicit error |
| lyrics direct PUT | contract violation | E9 test | 422 | explicit error |
| dims missing (g) | legacy asset pre-backfill | fail-closed test | demote to pip | pip suggestion (receipt if demoted at apply) |

No critical gaps remain (all six have test + handling + visible outcome).

## Approved Mockups

| Screen/Section | Mockup Path | Direction | Notes |
|---|---|---|---|
| Cutaway wireframes (D2 compare, timeline, popover ×2, rail) | session scratchpad `wireframe-009.html/.png` (keyless HTML fallback) | Light editorial, DESIGN.md §2 | Chip fill revised post-review: ink not lime (P1) |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Codex CLI not installed; outside voice ran as independent Claude subagent |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 (this plan) | CLEAR (PLAN) | 15 verified issues (3 more refuted by adversarial verification, 9 low-confidence appendix), 11 decisions E1–E11 all resolved, 0 critical gaps open |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 (this plan) | CLEAR (FULL) | score: 6/10 → 9/10, 12 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**CROSS-MODEL:** Codex unavailable; outside voice = independent Claude subagent
(fresh context). Its 9 findings: 4 absorbed (verification story, data plumbing,
spec contradictions, ownership), 2 resolved by adversarial verification against
its claims (render kill switch exists; scrubber fix owned by E6), 1 strategic
tension (T2 scope) resolved by owner as resequence-not-split (E10), 2 rollout
notes absorbed (E11). Design-review outside voice's D3 dissent was re-surfaced
and the owner upheld auto-apply a second time.

**VERDICT:** DESIGN + ENG CLEARED — ready to implement in PR order
T1 → T3 → T4 → T5 → T2.

NO UNRESOLVED DECISIONS
