# Plan 011 — Smarter Captions: contextual cue sizing, balanced layout, face-aware placement

Status: REVIEWED (/plan-eng-review 2026-07-22; 17 workflow findings + 7 outside-voice findings
folded, see GSTACK REVIEW REPORT)
Branch: claude/smart-caption-optimization-ca8228
Packaging (locked, founder decision D2): all three features; two PR lanes —
PR-1 = Feature A + B (cue brain + layout), PR-2 = Feature C (face placement) — parallel
worktrees, no shared prerequisite (the earlier PR-0 seam refactor was dropped: burn-time
`prepare_smart_caption_cues` is already the rendered-geometry authority, see Reburn contract).
Scope: Smart Captions v2 path ONLY (`_is_smart_captions_v2` true). v1 presets stay byte-stable
(pinned by `test_v1_plan_and_compiled_patch_remain_byte_stable`). Non-smart subtitled variants
keep sentence-per-cue behavior.

## Problem statement (founder's words, translated to mechanics)

1. **Cue sizing is not contextual.** "If I say number 1 and then Messi, it should show Messi
   alone without the next word." Today `build_semantic_caption_cues`
   ([smart_edit/captions.py:127](src/apps/api/app/smart_edit/captions.py)) groups words by
   deterministic caps (min 3 / max 7 words, 44 chars, 0.34s pause, strong sentence end) plus
   forced closes on semantic-role changes. Roles are coarse; nothing marks a single word as
   "this one stands alone." A standalone one-word cue IS structurally possible today — a role
   change bypasses the min-words guard (captions.py:160-176) — but no brain emits
   word-granular emphasis, so it never happens in practice.
2. **Multi-word cues should align well.** `measure_caption`
   ([render_geometry.py:151](src/apps/api/app/pipeline/render_geometry.py)) already does
   balanced 2-line splitting (scores every split by widest-line + 8% imbalance penalty), but
   it is blind to semantic units: it will happily break "1" from "Messi", or leave a
   one-word widow on line 2.
3. **Caption position ignores the face.** The preset pins `y_frac` (cigdem: 0.705) statically.
   Face boxes ARE sampled (`sample_face_regions`, Haar in a killable subprocess) but only when
   media overlays exist, only at media/camera anchor times, and only to move PiP cards
   ([generative_build.py:8842-8888](src/apps/api/app/tasks/generative_build.py)). Captions are
   treated as immovable protected regions; nothing ever moves the captions themselves.

## What already exists (reuse, don't rebuild)

| Sub-problem | Existing code | Reused as-is? |
|---|---|---|
| Word-level timing | `build_plain_cues(attach_words=True)` → `caption_cues[].words`; silence-cut remap | YES — input unchanged |
| Word→semantic tags brain | `SceneMatcherAgent` (gemini-2.5-flash, prompts/scene_matcher.txt, per-item fail-soft parse) | EXTENDED (new output field, flag-gated prompt block) |
| Semantic-before-chunking ordering | v2 `_semantic_timeline_v2` extracts chapters/roles BEFORE `build_semantic_caption_cues` | YES — new hints ride the same ordering |
| Cue chunker with forced breaks | `build_semantic_caption_cues(role_by_word_id, boundary_after_word_ids)` | EXTENDED (standalone + keep-together spans) |
| Balanced 2-line wrap | `measure_caption` candidate scoring | EXTENDED (keep-together + widow penalties) |
| Rendered-geometry authority | burn-time `generate_ass_from_cues` → `prepare_smart_caption_cues` at the EFFECTIVE policy (captions.py:497) | YES — this is why no seam refactor is needed |
| Face boxes + protection padding | `sample_face_regions` + `_face_protection_box` | YES — called with a UNION anchor set |
| Collision math | `NormalizedBox` helpers, `ProtectedRegion` | YES (coverage-fraction gate added) |
| Position persistence + user override | `smart_caption_policy.y_frac` → `_ass_header_smart`; `caption_position_user_edited` wins in `_effective_smart_caption_policy` (generative_build.py:9264-9280) | YES — face-chosen y slots BELOW user override |
| Kill-switch pattern | config.py flags + fail-open receipts (`smart_validation_receipts`) | YES |

## Feature A — Contextual cue sizing (the brain decides words-per-cue)

### Design

Extend the **scene matcher** (not a new agent) with word-granular presentation hints:

```
SceneMatcherOutput (agents/scene_matcher.py)
  matches:  SceneMatch[]        (existing, max 12)
  cue_tags: SceneCueTag[]       (existing, max 30)
+ emphasis_spans: EmphasisSpan[]   # NEW — max_length=16, parser slices [:32] like cue_tags
    word_ids: list[str]            # 1..3 contiguous word IDs, verbatim from input
    kind: "standalone" | "keep_together"
    # standalone   → this span renders as its OWN cue ("Messi" alone)
    # keep_together→ never split this span across cues OR lines ("number 1")
```

**The emphasis instruction block in `prompts/scene_matcher.txt` is rendered ONLY when
`SMART_CAPTION_EMPHASIS_CUES_ENABLED` is true** (one template, conditional block;
`prompt_version` bump covers both bodies). Flag off ⇒ the model never sees the new task ⇒
existing matches/cue_tags behavior AND cue chunking are byte-identical at the system level —
flipping the flag off is a real rollback (finding ARCH-3; shadow-mode idea dropped). The
prompt states a target span budget (≤ 10 per video) so the model doesn't generate tokens the
parser will drop.

Plumbing (planner.py `_run_scene_matcher` → `_semantic_timeline_v2`):

```
whisper words ──► SceneMatcherAgent ──► _SceneHints{matches, chapter_tags, role_tags,
                                                    emphasis_spans}          ← NEW field
                                            │
                    validation (deterministic, FAIL-SOFT PER SPAN — one bad span never
                    discards the others or the rest of the result):
                      - every word_id must exist & be contiguous  → else drop THAT span
                      - spans may not overlap each other          → later span dropped
                      - standalone spans: max 3 words             → else drop THAT span
                      - CLAIMED-WORD collision: a span whose words are claimed by an
                        authored title (_suppress_claimed_words set) is dropped BEFORE
                        chunking, receipt records it (outside-voice OV-3 — "Messi" is
                        exactly the word a title brain also targets)
                      - budget: ≤ 10 spans/video; standalone cue STARTS ≥ 1.5s apart
                        (blocks strobe; never touches 2-3s spoken-list cadence — OV-2
                        relaxed the draft's 4s gate that would have dropped list items)
                                            │
                                            ▼
build_semantic_caption_cues(words, policy,
                            role_by_word_id,
                            boundary_after_word_ids,
+                           standalone_spans,
+                           keep_together_spans)
                                            │
                                            ▼
cue dicts (+ smart_role, smart_word_ids,
+          "smart_emphasis": true on standalone cues,
+          "smart_keep_together": [[i, j], ...]  cue-relative word-index pairs —
           WRITTEN BY THE CHUNKER, the ONLY route pairs travel (OV-4: no function
           parameter, no second source of truth; burn-time prepare reads the cue field))
```

**Precedence (explicit, each case pinned by a chunker unit test):**
- Role changes and authored-title boundaries WIN over keep_together — semantics own captions
  (module docstring contract); the losing span is dropped for that cue and the receipt
  records `spans_broken_by_semantics`. A forced break INSIDE a standalone span likewise wins.
- max_words / max_chars caps NEVER split a span mid-span — the chunker closes the cue EARLY,
  before the span's first word, when appending the whole span would breach a cap
  (outside-voice OV-1: cap-splits would fail the flagship "number 1" demo ~1-in-7; a
  slightly-fuller following cue is absorbed by measure_caption's shrink loop).

**Persistence:** `smart_emphasis` and `smart_keep_together` are added to the `CaptionCue`
round-trip model + finalize whitelist in `routes/generative_jobs.py` so a user caption PATCH
does not strip them (finding ARCH-4; `spatial_owner` write-only-metadata is the anti-pattern).

Timing guard: a standalone cue shorter than 0.5s extends its `end_s` toward min(start_s+0.5,
next cue's start_s) — floor-only, mirroring `_MIN_WORD_CUE_S`; zero-gap continuous speech gets
no extension (accepted; matches the shipped word-cue rule).

### Flag

`SMART_CAPTION_EMPHASIS_CUES_ENABLED` (config.py, default `false`). Gates the PROMPT block,
span validation/consumption, and chunker inputs. Off ⇒ byte-identical prompt and cues
(kill-switch pins below). `smart_scene_matcher_enabled=false` remains the master kill switch.

Prompt-change rule: bump `SceneMatcherAgent.prompt_version`; run live evals before merge —
the existing EN golden re-runs automatically under the new version (regression pin on
matches/cue_tags), plus new TR + EN emphasis fixtures.

## Feature B — Balanced, semantics-aware line layout

### Design

`measure_caption` grows an optional `keep_together: Sequence[tuple[int, int]]` parameter
(cue-relative word-index pairs, sourced EXCLUSIVELY from the cue's persisted
`smart_keep_together` field by `prepare_smart_caption_cues` — single route, OV-4).
Scoring, current → new:

```
score(split) = max(widths) + |w0 - w1| * 0.08                      (current)
score(split) = max(widths) + |w0 - w1| * 0.08
             + BREAK_PENALTY   if split lands inside a keep_together span   (NEW)
             + WIDOW_PENALTY   if a line is a single word ≤ 3 chars AND
                               the cue has ≥ 3 words                         (NEW)
```

Penalties are large-but-finite: a split that FITS always beats one that overflows, at every
font size — the shrink loop's iteration count is unchanged. Pairs are validated against
`len(words)`; out-of-range or degenerate pairs (single-word cue, emptied text, stale indexes
after a user edit) are silently ignored (unit-tested). One shared helper owns the digit+word
adjacency predicate (referenced by the `_SENTENCE_SPLIT_RE` lookbehind comment, the prompt
few-shots, and this rule — one place).

**Own flag (outside-voice OV-7):** `SMART_CAPTION_LAYOUT_BALANCE_ENABLED` (config.py, default
`false`) gates the deterministic digit+word adjacency pairs and WIDOW_PENALTY — fully
deterministic, testable offline, shippable without the LLM eval train. Emphasis-derived pairs
additionally require Feature A's flag (they only exist when the brain emitted them). Either
flag off ⇒ measure_caption output (lines + font size + box) byte-identical for the untouched
dimension, pinned by layout goldens incl. digit+word and short-trailing-word cases.

**Reburn contract (corrected — finding ARCH-1, P1):** reburns do NOT re-emit persisted lines;
`generate_ass_from_cues` re-runs `prepare_smart_caption_cues` on every burn (captions.py:497)
and overwrites `smart_render_lines` (captions.py:324). Therefore:
- pairs are read from the persisted `smart_keep_together` on each cue at burn time (survives
  user edits via the CaptionCue round-trip; stale pairs ignored by validation);
- contract test replaces the draft's false "no retroactive drift" claim: **"reburn re-wraps
  under current scoring"** — flags on, a reburn of an old video MAY change line breaks
  (accepted, documented); flags off, reburn layout is byte-identical to today.
- burn-time prepare is the SINGLE rendered-geometry authority (contract-pinned); the
  compiler-internal prepare pass still runs at preset y and its `compiled_patch` snapshot
  stays compile-time debug evidence (zero runtime consumers).

## Feature C — Face-aware caption placement

### Design

One new pure function + one orchestration block, first render only:

```
choose_caption_y_frac(                            # NEW, app/pipeline/render_geometry.py
    face_regions: list[ProtectedRegion],          # from sample_face_regions
    face_receipt: dict,                           # sampler receipt, embedded in placement receipt
    caption_probe_box: NormalizedBox,             # tallest cue's box, TRANSLATED per candidate
    title_boxes: list[ProtectedRegion],
    candidates: tuple[float, ...],                # (preset.y_frac, 0.62, 0.78, 0.55, 0.86)
) -> tuple[float, dict]                           # (chosen y_frac, receipt)
```

Rules (mirrors the shipped Cigdem design-doc face-placement policy):
- Dominant face band = union of padded face boxes present on ≥ 60% of anchors **that produced
  a decodable frame** (not anchors attempted). No face / sampler failure / < 3 usable anchors
  ⇒ preset default; the receipt carries a `reason` enum — `no_face | sampler_timeout |
  sampler_error | insufficient_anchors` — plus the embedded raw sampler receipt, so a broken
  cv2 in the worker image is distinguishable from well-framed clips in /admin/jobs (finding
  QUAL-2). Fail-open, never raises.
- The probe box is measured ONCE (wrap/shrink is y-independent) and arithmetically translated
  to each candidate y. **Overlap gate is coverage-fraction, not IoU** (outside-voice OV-6:
  IoU's denominator grows with band size, inverting the incentive): a candidate passes when
  `intersection_area / caption_box_area ≤ 0.05` against the face band AND each title box,
  and it clears platform-chrome margins. The 5% threshold matches the design doc's
  face-overlap spike policy. Preset default is candidate #0 — a well-framed video changes
  nothing. No candidate passes ⇒ least-coverage candidate, `"status": "best_effort"`.
- ONE static y per video. No per-scene motion (design-doc discrete-zone rule).
- All y_frac↔margin_v conversions and the 0.30–0.90 clamp go through NEW shared helpers
  `y_frac_to_margin_v()` / `margin_v_to_y_frac()` + `CAPTION_Y_FRAC_MIN/MAX` constants in
  `app/pipeline/captions.py`; this PR migrates `_resolve_caption_margin_v` and
  `_effective_smart_caption_policy` onto them (kills the 6-copy drift class — finding QUAL-1).

Orchestration in `_render_subtitled_variant` (post-reframe — faces can only be located on
final geometry; punch-in crop pulses are a fixed transient 1.08x, ≤4% edge displacement):

```
transcribe → cues → _compile_smart_caption_render_plan     (compiler-internal prepare at
                       │                                     preset y; compiled_patch snapshot
                       ▼                                     stays compile-time — debug only)
   base_path = reframe_and_export(... semantic_crop_pulses)
                       │
   [flag on, smart_v2] ▼
   anchors = evenly_spaced(n=8, duration=ffprobe(base_path))   ← RENDERED base duration
             ∪ camera-intent times ∪ media-overlay start times   (silence-cut safe: never
             (dedupe within ±0.25s; max_samples raised so the     seeks past the cut EOF)
              union cannot truncate — sampler timeout scales:
              timeout_s = 1.0 + 0.35 * len(anchors))
                       │
   sample_face_regions(base_path, anchors)      ← runs even when media_overlays is empty;
                       │                          samples REUSED for card arbitration and,
                       ▼                          because card/camera times stay in the set,
   choose_caption_y_frac(...) → chosen_y          card face-protection is coverage-identical
                       │                          to today (findings ARCH-2/TEST-2)
                       ▼
   smart_caption_policy["y_frac"] = chosen_y     (persisted → reburn-stable; UI mirror via
   caption_margin_v = y_frac_to_margin_v(chosen_y), user_edited flags NOT set)
                       │
                       ▼
   re-measure cue boxes at chosen_y              ← ONE added prepare_smart_caption_cues call
                       │                           (burn-time ASS geometry follows the
                       ▼                            persisted policy automatically — the
   protected_boxes assembly →                       re-measure exists ONLY so protected boxes
   arbitrate_media_overlays                         match reality; finding TEST-1's phantom-
                                                    band scenario is the pinned contract test)
```

Precedence (highest wins): `caption_position_user_edited` > face-chosen y (first render,
persisted) > preset `y_frac`. Reburns/retranscribes read the persisted policy — they never
recompute placement. Re-transcribe keeps `smart_caption_policy` untouched.

When the flag is OFF and no media overlays exist, the existing `skipped_no_media` fast path
is preserved verbatim (receipt pin).

### Flag

`SMART_CAPTION_FACE_PLACEMENT_ENABLED` (config.py, default `false`). Off ⇒ current geometry,
anchor set, and receipts byte-identical (kill-switch pin includes the ANCHOR LIST argument,
not just the skip branch). Fly-first flip; no frontend twin (render-only; position UI keeps
working through the mirrored `caption_margin_v`).

## NOT in scope (explicitly deferred)

- **Per-scene / animated caption movement** — design doc forbids zone motion; one static y.
- **Emphasis styling for standalone cues** — TODO T-CAP011-1; `smart_emphasis` persists for it.
- **Non-smart subtitled + narrated variants** — sentence-per-cue path untouched.
- **v1 preset behavior changes** — frozen byte-stable.
- **MediaPipe-matte-based placement** — Haar boxes are production-proven for placement.
- **3+ line layout** — `max_lines` stays clamped 1–2.
- **New standalone "cue grouper" agent** — rejected: second LLM call per render for hints the
  scene matcher emits in its existing pass.
- **Per-anchor streaming face sampler** — TODO T-CAP011-2; timeout scaling suffices now.
- **PR-0 seam refactor** — dropped (OV-5): burn-time prepare is already the geometry
  authority; PR-2 adds one re-measure call instead.

## Test plan (all new code paths; files marked NEW are new test modules)

| Path | Test | Kind |
|---|---|---|
| Baseline golden of CURRENT `build_semantic_caption_cues` behavior (first commit, so kill-switch pins diff against a real golden) | `tests/smart_edit/test_captions.py` (NEW file) | unit |
| Geometry-authority contract: burn-time prepare at effective policy is the single rendered-geometry source (pin against regressions of the OV-5 assumption) | `tests/pipeline/test_captions.py` | unit |
| EmphasisSpan parsing: invalid word_id / non-contiguous / >3-word / OVERLAPPING spans drop ONLY that span; wrong-TYPE field drops the field, keeps matches/cue_tags | `tests/smart_edit/test_scene_matcher.py::test_emphasis_span_fail_soft_per_item` | unit |
| Claimed-word collision: span claimed by an authored title dropped BEFORE chunking + receipt | `tests/smart_edit/test_planner_compiler.py` | unit |
| Budget/gap boundaries: ≤10 spans; standalone starts 1.5s apart (== kept, < dropped, first near t=0); receipt counts | `tests/smart_edit/test_planner_compiler.py` | unit |
| "number 1 → Messi" golden: standalone cue emitted alone (TR + EN) | `tests/smart_edit/test_captions.py::test_standalone_emphasis_word_renders_as_own_cue` | unit |
| Cap-close-early: appending a keep_together span that would breach max_words/max_chars closes the cue BEFORE the span; span never cap-split (incl. span-at-cap-boundary golden) | `tests/smart_edit/test_captions.py` | unit |
| Precedence: keep_together vs role change / authored boundary — semantic close wins, receipt records | `tests/smart_edit/test_captions.py` (2 cases) | unit |
| Standalone min-duration: floor-only extension, clamped at next cue start; zero-gap ⇒ no extension | `tests/smart_edit/test_captions.py` | unit |
| Flag A off ⇒ prompt body unchanged AND cues byte-identical vs baseline golden | kill-switch pin, `test_scene_matcher.py` + `test_captions.py` | unit |
| `smart_emphasis`/`smart_keep_together` survive the caption PATCH round-trip (CaptionCue model) | caption round-trip test (extends `test_caption_cue_words_roundtrip_exclude_none`) | unit |
| measure_caption: keep-together pair never split when it fits; overflow wins over penalties; widow penalty; out-of-range/degenerate/stale pairs ignored | `tests/pipeline/test_render_geometry.py` (NEW file) goldens | unit |
| Layout flags off ⇒ measure_caption lines+size+box byte-identical (goldens incl. digit+word, short-trailing-word) | `tests/pipeline/test_render_geometry.py` | unit |
| Reburn contract: flags on ⇒ re-wraps under current scoring, pairs honored from persisted `smart_keep_together`; flags off ⇒ byte-identical layout | `tests/tasks/test_caption_reapply.py` | unit |
| choose_caption_y_frac matrix: face low → y moves up; no face / timeout / error / <3 usable anchors → preset + correct `reason` enum + embedded sampler receipt; no safe candidate → best_effort (least coverage); coverage-fraction gate at 0.05 boundary; boundary candidates 0.30/0.90 | `tests/pipeline/test_render_geometry.py::TestChooseCaptionY` | unit |
| Margin round-trip at 0.30/0.90 through helpers + `_resolve_caption_margin_v` + `_effective_smart_caption_policy` ⇒ same y (no reburn snap-back) | `tests/smart_edit/test_v2_render_contract.py` | unit |
| Orchestration: with mocked chooser returning y≠preset — every cue `smart_render_box.bottom == chosen_y`, ASS MarginV == y_frac_to_margin_v(chosen_y), protected caption boxes use RE-measured boxes | `tests/smart_edit/test_v2_render_contract.py` | unit |
| Anchor union: anchors ⊇ media-overlay starts ∪ camera-intent times; dedupe; no truncation; timeout scales with count | `tests/smart_edit/test_v2_render_contract.py` | unit |
| Silence-cut interplay: anchors from RENDERED base duration; presence denominator = decodable anchors | `tests/smart_edit/test_v2_render_contract.py` | unit |
| Face sampling runs without media overlays when flag C on; `skipped_no_media` preserved when off; flag C off ⇒ anchor set + geometry byte-identical | `tests/smart_edit/test_v2_render_contract.py` | unit |
| Chosen y persisted; reburn does NOT recompute; retranscribe preserves policy; user position edit beats face-chosen y | `test_caption_reapply.py` / `test_subtitled_retranscribe.py` / `test_v2_render_contract.py` | unit |
| Flag matrix (A × layout × C, pairwise pins): lanes independent, receipts correct | `tests/smart_edit/test_v2_render_contract.py` | unit |
| Scene matcher evals: existing EN golden re-runs under new prompt_version (matches/cue_tags regression pin) + NEW TR ordinal-words + EN "number one" emphasis fixtures | `tests/evals/test_scene_matcher_evals.py` | eval (live before merge) |
| End-to-end: prod-image render of a TR fixture, captions clear of face | `make local-render` manual gate + `make verify-overlays` | E2E manual |

**Gate owner (OV-7):** live Gemini evals and the prod-image `make local-render` /
`make verify-overlays` gates require API keys + Docker — they run on Emir's machine or a CI
job with secrets, NOT the keyless/Docker-less dev machine. PR-1's deterministic layout half
(`SMART_CAPTION_LAYOUT_BALANCE_ENABLED`) has no LLM dependency and is fully verifiable
offline.

## Failure modes (per new codepath)

| Codepath | Realistic failure | Test? | Handled? | User sees |
|---|---|---|---|---|
| Scene matcher emphasis output | hallucinated word_ids / overlapping spans / spans everything | fail-soft + overlap + budget tests | drop spans, receipt | normal captions |
| Extended prompt shifts role tagging | cue chunking drifts for everyone | flag gates PROMPT; eval regression pin | flag off = old prompt | no drift with flag off |
| Emphasis word claimed by title | standalone cue empties/stubs | claimed-collision test | span dropped pre-chunking + receipt | title wins, caption normal |
| Spoken list every 2-3s | items 2+ lose emphasis | 1.5s-gap boundary test | gap tuned below list cadence | all list items pop |
| keep_together at cap boundary | "number 1" cap-split | cap-close-early golden | cue closes early | pair always together |
| Standalone cue timing | short cue in continuous speech | floor-only test | no extension (accepted) | brief pop |
| Pairs after user text edit | stale indexes | ignore-stale test | validated, ignored | unchanged layout |
| User PATCH strips new fields | round-trip loss | CaptionCue round-trip test | fields whitelisted | emphasis survives edits |
| Face sampler | timeout / zero faces / cv2 broken / giant Haar box | reason-enum tests + `_face_protection_box` clamp | preset default + distinguishable receipt | today's behavior |
| Anchor union truncation | card time loses face protection | anchor-superset test | max_samples raised + dedupe | cards still avoid faces |
| Big face band inflates tolerance | caption certified "clear" while on the face | coverage-fraction 0.05 boundary test | coverage gate, not IoU | captions truly clear |
| Stale caption boxes after chosen y | phantom protection band; card over captions | re-measure contract test | one orchestrator re-measure | cards avoid real captions |
| Silence-cut shortened base | anchors past EOF dilute presence ratio | duration-source test | ffprobe(base_path) + decodable denominator | placement works on talky clips |
| Boundary y 0.90 round-trip | margin clamp snap-back on reburn | 0.30/0.90 round-trip test | shared helpers | stable position |
| Reburn after flag flip OFF | persisted face-y kept? | kill-switch reburn test | persisted state preserved | stable video |

## Rollout

1. PR-1 + PR-2 land in parallel with all three flags `false`. 2. Live scene-matcher evals
green (old EN fixture + new TR/EN fixtures) + prompt_version bumped — gate owner: machine
with GEMINI key (Emir / CI secrets job). 3. `make verify-overlays` + `make local-render` TR
fixture reviewed (Docker machine). 4. Flip `SMART_CAPTION_LAYOUT_BALANCE_ENABLED` first
(deterministic, lowest risk). 5. Flip `SMART_CAPTION_FACE_PLACEMENT_ENABLED` on Fly (worker
restart), watch `caption_placement` receipts (reason enum + anchor counts + timeout rate) in
/admin/jobs. 6. Flip `SMART_CAPTION_EMPHASIS_CUES_ENABLED` after eval review. All flags
independent; `smart_scene_matcher_enabled=false` remains the master kill switch.

## Files touched (source)

PR-1 (A+B): `src/apps/api/app/agents/scene_matcher.py`, `src/apps/api/prompts/scene_matcher.txt`,
`src/apps/api/app/smart_edit/planner.py`, `src/apps/api/app/smart_edit/captions.py`,
`src/apps/api/app/pipeline/render_geometry.py` (measure_caption),
`src/apps/api/app/pipeline/captions.py` (prepare reads cue pairs),
`src/apps/api/app/routes/generative_jobs.py` (CaptionCue round-trip),
`src/apps/api/app/config.py` (2 flags)
PR-2 (C): `src/apps/api/app/pipeline/render_geometry.py` (choose_caption_y_frac),
`src/apps/api/app/pipeline/captions.py` (y_frac helpers + constants),
`src/apps/api/app/tasks/generative_build.py`, `src/apps/api/app/config.py` (1 flag)
Docs: `docs/pipelines/generative.md` + CLAUDE.md flag entries

## Worktree parallelization

| Step | Modules touched | Depends on |
|------|----------------|------------|
| PR-1: A+B (spans, chunker, layout, round-trip) | agents/, prompts/, smart_edit/, pipeline/, routes/ | — |
| PR-2: C (face placement) | pipeline/, tasks/, config.py | — |

Execution: launch PR-1 and PR-2 in parallel worktrees. Conflict flags: both touch
`pipeline/render_geometry.py` and `pipeline/captions.py` (different functions — coordinate)
and add `config.py` flag lines (trivial). A merged-main flag-matrix test (pairwise pins) runs
after both land.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 26 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

Detail: multi-agent verify+review workflow raised 26 findings (4 verification agents, 4
hostile dimension reviewers, per-finding skeptic pass killed 9); 17 survived and were folded.
Outside voice (Claude subagent; Codex CLI not installed) raised 8 more: 6 folded, 1 folded in
relaxed form (density gate 4s → 1.5s + budget), 1 escalated to the founder (D3: Feature C
kept as planned over the measurement-first alternative). Founder decisions: D1 review target,
D2 packaging (all three features, 2 parallel PRs), D3 Feature C stays. Headline corrections
absorbed: reburn re-measures lines (pairs must persist on cues + CaptionCue round-trip),
face-anchor union preserves shipped card protection, emphasis prompt block flag-gated,
coverage-fraction placement gate, PR-0 seam refactor dropped, deterministic layout split onto
its own offline-verifiable flag.

- **CROSS-MODEL:** workflow review and outside voice agreed on reburn-persistence and
  plumbing-duplication risk classes; disagreed on PR-0 necessity (outside voice won, with
  code evidence: captions.py:497 burn-time prepare is the geometry authority), on cap-vs-span
  precedence (outside voice won: caps close early, never mid-span), and on Feature C timing
  (founder decided: build now, D3).
- **VERDICT:** ENG CLEARED — ready to implement (PR-1 and PR-2 in parallel worktrees; live
  scene-matcher evals + prod-image render gates run on a keyed/Docker machine before merge).

NO UNRESOLVED DECISIONS
