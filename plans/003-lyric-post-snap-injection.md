# Plan 003: Move lyric injection after beat-snap and fix karaoke word-highlight drift

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat a49fe589..HEAD -- src/apps/api/app/tasks/music_orchestrate.py src/apps/api/app/tasks/generative_build.py src/apps/api/app/tasks/template_orchestrate.py src/apps/api/app/pipeline/lyric_injector.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none — but this plan should land BEFORE any other change to `lyric_injector.py` (lyric stacking has regressed ~10 times historically; don't interleave work in this area)
- **Category**: bug
- **Planned at**: commit `a49fe589`, 2026-06-12

## Why this matters

Lyric overlays are injected into the recipe **before** beat-snap re-times slot boundaries, so every lyric segment is clamped to stale pre-snap slot durations. Beat-snap moves boundaries by up to ±beat-interval/2 (~221 ms measured on prod job `14ded08a`, up to ~500 ms on slow tracks). For line lyrics, a downstream identity-merge band-aid hides the gap. For **karaoke**, nothing compensates: the overlay's absolute start shifts with slot drift while its per-word timings stay pinned relative to the overlay start — so per-word highlight runs up to a full beat off the actual vocal, in production, today. This plan makes injection see post-snap durations, eliminating the drift class structurally instead of compensating for it. Both halves are P1 entries in `TODOS.md` (lines 650 and 657 at `a49fe589`), and this plan follows the fix design written there by the maintainer.

## Current state

### Call-site map (verified at `a49fe589` — TODOS.md's line numbers and one function name are stale; use these)

`inject_lyric_overlays` (`src/apps/api/app/pipeline/lyric_injector.py:221`) is called from THREE places, always before assembly:

1. `src/apps/api/app/tasks/music_orchestrate.py:662` — `_run_music_job`. Excerpt (lines 655–668):

```python
    # [3] Generate recipe from beats
    recipe_dict = generate_music_recipe(track_data)

    # [3a] Inject lyric overlays (no-op when lyrics_config.enabled=False or
    # track has no cached lyrics). Done BEFORE TemplateRecipe is built so the
    # overlays flow through `_assemble_clips` like any other text overlay.
    cfg = track_data["track_config"]
    recipe_dict = inject_lyric_overlays(
        recipe_dict,
        lyrics_cached,
        best_start_s=float(cfg.get("best_start_s", 0.0)),
        best_end_s=float(cfg.get("best_end_s", 0.0)),
        lyrics_config=lyrics_config,
    )
```

2. `src/apps/api/app/tasks/music_orchestrate.py:1377` — `_run_templated_music_job` (same shape).
3. `src/apps/api/app/tasks/generative_build.py:2842` — inside `_inject_lyrics(recipe_dict, track, style_set_id)` (function starts line 2808), called per song-lyrics generative variant. **This third call site is NOT listed in TODOS.md — it must be covered (or explicitly descoped with evidence, see STOP conditions).**

### Where beat-snap actually happens

TODOS.md refers to `_apply_beat_snap` — **that function does not exist**. The real mechanism: `_snap_to_beat` (`src/apps/api/app/tasks/template_orchestrate.py:5390`) is a pure function, applied inside `_assemble_clips` (def at line 3003) by the sequential slot-planning loop at lines ~2461–2508. Excerpt of the snap branch (lines 2481–2508):

```python
        else:
            # Beat-snap
            if beats:
                expected_end = cumulative_s + slot_target_dur
                snapped_end = _snap_to_beat(expected_end, beats)
                drift_ms = int(round((snapped_end - expected_end) * 1000))
                slot_target_dur = max(0.5, snapped_end - cumulative_s)
                ...record_pipeline_event(stage="beat_snap", event="slot_snapped", ...)
                cumulative_s = snapped_end
            else:
                cumulative_s += slot_target_dur
```

Key structural facts about this loop:
- It is **stateful and sequential**: `cumulative_s` carries across slots, so each slot's snapped duration depends on all previous slots.
- Slots with `locked` or `exact_window` **bypass** snap entirely (lines 2473–2480) and use verbatim source ranges.
- Slot target durations come from `resolve_slot_duration(step.slot, is_agentic=..., user_total_dur_s=...)` (line 2461).
- The pure snap function (`_snap_to_beat`, lines 5390–5408) snaps to the nearest beat within `BEAT_SNAP_TOLERANCE_S`.

### The band-aids to PRESERVE (per TODOS.md — they become regression detectors)

- Layer 1 identity merge: `_consolidate_lyric_segments` inside `_collect_absolute_overlays` (`template_orchestrate.py`, around line 4204). It merges segments sharing a `lyric_line_id`. It is semantically correct ("same line ⇒ one overlay") — **do not delete it**.
- `_LARGE_CONTINUATION_GAP_WARNING_S = 0.5` at `template_orchestrate.py:4204` — tightened to ~0.05 only at the END of this plan, once drift is engineered out.
- Kill switch: `LYRIC_DYNAMIC_CROSSFADE_ENABLED` must keep its byte-identical legacy path. Guard test: `tests/pipeline/test_lyric_injector_no_stacking.py::test_kill_switch_disabled_reproduces_pre_fix_output`.

### Why karaoke is the worst case

`_inject_karaoke` (`lyric_injector.py:635`) does NOT split lines across slots — one overlay per line, clamped to one slot via `_slot_for_time(line.start_s, windows)` (`lyric_injector.py:589`). The `lyric_line_id` merge therefore does nothing for karaoke, but slot drift still shifts the overlay's `abs_start` while `word_timings` inside it stay relative to the overlay's own start.

### Repo conventions that apply

- This is a render-affecting change: the repo standard is a `make local-render` parity check before merge (`docs/runbooks/local-render.md`).
- Lesson from history (this bug class has recurred ~10×): test rendered/consolidated output at the consolidation layer, not just scheduler integers.
- Tests live in `src/apps/api/tests/pipeline/`; model new tests on `test_lyric_injector.py` and `test_lyric_injector_no_stacking.py` fixtures.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Characterization baseline | `cd src/apps/api && pytest tests/pipeline/test_lyric_injector.py tests/pipeline/test_lyric_injector_no_stacking.py tests/pipeline/test_lyric_style_isolation_guard.py -v` | all pass |
| Single-pass invariant guard | `cd src/apps/api && pytest tests/pipeline/test_single_pass.py -v` | all pass |
| Full pipeline tests | `cd src/apps/api && pytest tests/pipeline/ tests/tasks/ -v` | all pass |
| Lint | `cd src/apps/api && ruff check . && ruff format --check .` | exit 0 |
| Render parity (final, optional but recommended) | `make local-render CLIP=<clip> TEMPLATE=<uuid> MODE=music` (see `docs/runbooks/local-render.md`) | output renders; lyric timing visually on-beat |

If the repo has `src/apps/api/.venv-test/`, run pytest via `src/apps/api/.venv-test/bin/python -m pytest ...`.

## Scope

**In scope** (the only files you should modify):
- `src/apps/api/app/tasks/template_orchestrate.py` — extract the snapped-duration planning into a reusable helper; tighten the warning threshold (last step only)
- `src/apps/api/app/tasks/music_orchestrate.py` — both injection call sites
- `src/apps/api/app/tasks/generative_build.py` — `_inject_lyrics` call site
- `src/apps/api/app/pipeline/lyric_injector.py` — only if the injection signature needs to accept snapped windows; minimize changes here
- `src/apps/api/tests/pipeline/` — new regression tests

**Out of scope** (do NOT touch, even though they look related):
- The Layer 1 identity merge `_consolidate_lyric_segments` — keep byte-identical (except the threshold constant in the final step).
- The `LYRIC_DYNAMIC_CROSSFADE_ENABLED` legacy code path — must stay byte-identical.
- Skia lyric-line fade parity (`text_overlay_skia.py:_ANIMATED_EFFECTS_SKIA`) — separate TODO, separate change.
- Admin lyric UX (`LyricsTimingPanel.tsx`, snap-to-boundary buttons) — separate TODOs.
- `_burn_text_overlays`, renderer dispatch, or anything in the render phases of `_assemble_clips` beyond duration planning.

## Git workflow

- Worktree first: `bash scripts/new-session.sh lyric-post-snap && cd ../nova-lyric-post-snap`.
- Commit per step; conventional commits, e.g. `fix(lyrics): inject lyric overlays against post-snap slot durations`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Green characterization baseline

Run the characterization command from the table. Record the pass count.

**Verify**: all pass. If anything is red on a clean checkout → STOP (pre-existing breakage; report).

### Step 2: Extract a pure snapped-duration planner from `_assemble_clips`

In `template_orchestrate.py`, extract the duration-planning portion of the slot loop (lines ~2461–2508) into a pure function, e.g.:

```python
def compute_snapped_slot_durations(
    steps,  # the same matched steps _assemble_clips iterates
    beats: list[float],
    *,
    is_agentic: bool,
    user_total_dur_s: float | None,
) -> list[float]:
    """Final per-slot durations after beat-snap — the same arithmetic
    `_assemble_clips` applies, computable BEFORE assembly so lyric injection
    can run against post-snap windows."""
```

It must reproduce exactly: `resolve_slot_duration` → `max(dur, 0.5)` → locked/`exact_window` bypass (verbatim range arithmetic) → `_snap_to_beat` with the running `cumulative_s` → `max(0.5, snapped_end - cumulative_s)`. Then refactor `_assemble_clips` to consume this helper for its `slot_target_dur` values so the math exists ONCE (do not leave two copies of the snap arithmetic). Keep the `record_pipeline_event("beat_snap", ...)` emission inside `_assemble_clips` (trace events need the orchestrator's `pipeline_trace_for` context), driven by the helper's output (emit drift as `snapped_dur - pre_snap_dur` per slot).

Add a unit test (new file `tests/pipeline/test_snapped_slot_durations.py`): given a synthetic steps list + beat grid, helper output matches hand-computed snapped durations, including a locked slot mid-sequence and the cumulative-carry behavior.

**Verify**: `pytest tests/pipeline/test_snapped_slot_durations.py tests/pipeline/test_single_pass.py tests/tasks/ -v` → all pass (assembly behavior unchanged — this step is pure refactor + new helper).

### Step 3: Inject against snapped durations at all three call sites

At each call site (`music_orchestrate.py:662`, `music_orchestrate.py:1377`, `generative_build.py:_inject_lyrics` caller), compute the snapped durations with the new helper BEFORE `inject_lyric_overlays`, and pass them so the injector clamps segments to post-snap windows. Mechanically: whatever recipe field the injector reads slot durations from (trace `inject_lyric_overlays` → `_SlotWindow` construction in `lyric_injector.py`), overwrite those durations with the helper's output prior to injection — or pass an explicit `slot_durations_override` parameter if mutation of the recipe dict is unclean. Prefer the smallest honest change; the injector's internal logic should not need to know snap exists.

Note for the generative site: `_inject_lyrics` receives `recipe_dict` from `generate_music_recipe`/variant recipe construction. Confirm the recipe at that point is pre-snap (it is, unless the codebase drifted — the snap only happens inside `_assemble_clips`). If you find evidence it is already post-snap, STOP condition 3 applies.

**Verify**: `pytest tests/pipeline/ tests/tasks/ -v` → all pass. The Layer 1 merge tests must still pass UNCHANGED (the merge becomes a no-op-in-practice, not a removed feature).

### Step 4: Karaoke drift regression test (red → green)

New test in `tests/pipeline/` (e.g. `test_karaoke_post_snap_sync.py`), per the TODOS.md spec: build a karaoke fixture (model on existing fixtures in `test_lyric_injector.py`) where the line's containing slot has ≥200 ms of intentional beat-snap drift. Assert each per-word highlight crossing lands within ±50 ms of the song-time word boundary in the final absolute overlay timeline (assert at the consolidation/absolute-overlay layer — `_collect_absolute_overlays` output — NOT on injector-internal integers).

**Demonstrate red→green**: `git stash` the Step 3 changes (or temporarily revert the call-site wiring), run the test → it MUST FAIL; restore → it passes. Record both runs in your report.

**Verify**: test fails on pre-fix wiring, passes on post-fix wiring.

### Step 5: Tighten the continuation-gap warning threshold

Only now: change `_LARGE_CONTINUATION_GAP_WARNING_S = 0.5` → `0.05` at `template_orchestrate.py:4204`.

**Verify**: `pytest tests/pipeline/ tests/tasks/ -v` → all pass with no new gap warnings emitted in test logs (`pytest ... -v 2>&1 | grep -i "continuation gap"` → empty or only sub-50ms entries).

### Step 6: Kill-switch + full gate

**Verify**:
- `pytest tests/pipeline/test_lyric_injector_no_stacking.py -v` → all pass, including `test_kill_switch_disabled_reproduces_pre_fix_output`.
- `cd src/apps/api && pytest tests/ --ignore=tests/quality && ruff check . && ruff format --check .` → exit 0.
- Recommended before merge (operator may run): `make local-render ... MODE=music` on a lyric-bearing track; visually confirm word highlights track the vocal.

## Test plan

- `tests/pipeline/test_snapped_slot_durations.py` (new): helper math — snap, cumulative carry, locked-slot bypass, empty-beats no-op, 0.5 s floor.
- `tests/pipeline/test_karaoke_post_snap_sync.py` (new): the ±50 ms word-boundary regression with forced ≥200 ms drift; red on pre-fix wiring.
- Existing suites as regression net: `test_lyric_injector.py`, `test_lyric_injector_no_stacking.py` (kill switch), `test_lyric_style_isolation_guard.py`, `test_lyrics_preview.py`, `test_single_pass.py` (CFR invariant), `tests/tasks/test_generative_build.py`.
- Pattern files: `tests/pipeline/test_lyric_injector.py` for fixture construction.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `compute_snapped_slot_durations` (or equivalently named helper) exists in `template_orchestrate.py` AND `_assemble_clips` consumes it (snap arithmetic appears once: `grep -c "_snap_to_beat(" src/apps/api/app/tasks/template_orchestrate.py` → 2: one def, one call inside the helper)
- [ ] All three injection call sites pass snapped durations (grep each file for the helper name → ≥1 hit in `music_orchestrate.py` (×2 call sites) and the generative path)
- [ ] Karaoke regression test exists, was demonstrated red on pre-fix wiring, and passes
- [ ] `_LARGE_CONTINUATION_GAP_WARNING_S` is `0.05`
- [ ] `test_kill_switch_disabled_reproduces_pre_fix_output` passes
- [ ] `cd src/apps/api && pytest tests/ --ignore=tests/quality` exits 0; `ruff check .` exits 0
- [ ] No files outside the in-scope list modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

1. The "Current state" excerpts don't match the live code (drift since `a49fe589`).
2. Extracting the duration planner requires touching `_assemble_clips`'s render phases (reframe, burn, join) — the refactor should be confined to duration planning; if it isn't, the loop has more coupling than this plan assumed.
3. The generative `_inject_lyrics` recipe turns out to already carry post-snap durations — then the generative site is out of scope; report the evidence and fix only the two music sites.
4. `test_kill_switch_disabled_reproduces_pre_fix_output` fails at any point.
5. The karaoke regression test cannot be made to fail on pre-fix wiring (means the test isn't measuring the drift; do not ship a test that was never red).
6. Layer 1 merge tests start failing — the merge must remain intact as the regression detector.

## Maintenance notes

- **History**: lyric stacking at line transitions has re-broken ~10 times. The durable lesson encoded here: assert on consolidated/rendered output, not scheduler integers. Reviewers should hold any future lyric PR to that bar.
- After this lands, the Layer 1 identity merge should observe near-zero gap corrections; the tightened 0.05 s warning is the early-warning signal that drift has crept back in. If those warnings reappear in prod logs, treat it as a regression of THIS plan.
- Deferred (related TODOs, deliberately not here): Skia lyric-line fade parity; admin snap-to-lyric-boundary UX; LyricsTimingPanel per-field dirty tracking.
- A `make local-render MODE=music` byte-parity spot check before merging is the repo's standard for render-affecting changes; the operator should run it if the executor's environment lacks Docker.

---

## GSTACK REVIEW REPORT

**Reviewed at**: commit `a49fe589` · 2026-06-12 · Reviewer: plan-eng-review (full)

### Step 0 — Scope challenge

| Question | Answer |
|---|---|
| Existing code that already handles this? | No — drift is structural; band-aids (`_consolidate_lyric_segments`) mask but don't fix it |
| Minimum change set? | 1 helper, 3 call-site edits, 2 new test files, 1 constant tighten |
| Complexity check triggered? | YES — render-affecting, lyric area ~10x regression history; 4-section review required |
| Scope confirmed? | YES — `template_orchestrate.py`, `music_orchestrate.py`, `generative_build.py`, `lyric_injector.py` (minimal if any), 2 new test files |

### Section 1 — Architecture

**Finding A (implementation note, not a blocker):** The plan proposes `compute_snapped_slot_durations(steps, beats, *, is_agentic, user_total_dur_s)` where `steps` is the same matched-step list `_plan_slots` iterates (note: the snap loop is in `_plan_slots` at line 2384, not `_assemble_clips` at line 3003 — the plan's line numbers 2461-2508 are accurate but the function name is slightly off). The injection call sites in `music_orchestrate.py` and `generative_build.py` do NOT have matched steps at that point — they only have `recipe_dict["slots"]` (raw slot dicts). **Implementation deviation required:** make the helper accept `slots: list[dict]` rather than `steps: list`. Inside `_plan_slots`, extract slot dicts from steps (`[step.slot for step in steps]`) and pass them. For locked/exact_window slots, the verbatim range arithmetic (`start_s`/`end_s` from the matched moment) cannot run pre-matching — fall back to `target_duration_s` as the duration estimate for those slots. This is correct: locked slots in music recipes are typically fixed assets with no lyrics; the snap arithmetic for non-locked slots (the ones that actually carry lyric windows) is unaffected. Document this caveat in the helper's docstring.

Design is otherwise sound: stateful sequential snap, cumulative carry, locked bypass — all preserved. The `record_pipeline_event` calls stay in `_plan_slots` (they need the `pipeline_trace_for` context), driven by the computed deltas from the helper.

**Finding B:** The generative call site at `generative_build.py:2426` already computes `beats = list(recipe_dict.get("beat_timestamps_s") or [])` immediately after `generate_music_recipe`. Beats are available. The music call site uses `track_data["beat_timestamps_s"]` (line 713). Both are straightforward passes.

**Finding: A (implementation note), B (observation). Zero blockers.**

### Section 2 — Code Quality

The refactor is a pure extraction: the snap arithmetic exists once (in the new helper), `_plan_slots` consumes it. `_assemble_clips` already delegates to `_plan_slots`; no change needed there beyond picking up the helper output. The `record_pipeline_event` emission stays in `_plan_slots` — it is NOT pure and must not move into the helper.

The comment at `template_orchestrate.py:4199` already documents that `_LARGE_CONTINUATION_GAP_WARNING_S = 0.5` is intentionally loose "until post-snap injection lands" — tightening to 0.05 at Step 5 is exactly what was planned.

**Finding: none.**

### Section 3 — Test Coverage

```
CODE PATHS
[+] compute_snapped_slot_durations
  ├── [★★★ COVERED] test_snapped_slot_durations.py — snap math, cumulative carry, locked bypass, empty beats, 0.5s floor
  └── [★★★ COVERED] test_karaoke_post_snap_sync.py — ≥200ms drift fixture, ±50ms word-boundary assertion at consolidation layer

[+] 3 injection call sites (post-snap durations)
  ├── [★★★ COVERED] test_karaoke_post_snap_sync.py — structural test covers the generative path (force via recipe construction)
  └── [★★ PARTIAL] music_orchestrate call sites — indirect via test_karaoke_post_snap_sync; consider smoke-checking _run_music_job separately if fixture is cheap

[+] Layer 1 merge still intact
  └── [★★★ COVERED] existing test_lyric_injector.py + test_lyric_injector_no_stacking.py (must pass unchanged)

[+] Kill switch (LYRIC_DYNAMIC_CROSSFADE_ENABLED)
  └── [★★★ COVERED] test_kill_switch_disabled_reproduces_pre_fix_output (must pass at every step)
COVERAGE: 4/4 paths. No gaps that block shipping.
```

**Finding: none.**

### Section 4 — Performance

Helper adds one O(n_slots) pass over 4–8 slots. Negligible. No concern.

**Finding: none.**

### NOT in scope

- Skia lyric-line fade parity
- Admin LyricsTimingPanel UX
- `_consolidate_lyric_segments` logic (must be preserved byte-identical)
- Any Celery task dispatch changes

### What already exists

- `_consolidate_lyric_segments` (Layer 1 band-aid) — kept as regression detector
- `_LARGE_CONTINUATION_GAP_WARNING_S = 0.5` comment already documents the tighten intent
- `test_lyric_injector_no_stacking.py::test_kill_switch_disabled_reproduces_pre_fix_output` — mandatory guard at every step

### TODOS updates

None — zero new findings.

### Failure modes

1. Locked slot uses `target_duration_s` estimate instead of verbatim range → cumulative_s carries a slightly incorrect value into subsequent slots. Acceptable: locked slots in music recipes are fixed assets that don't carry lyrics; the error is bounded by the clip duration estimate error, not a beat-snap error.
2. `test_karaoke_post_snap_sync.py` was never red on pre-fix wiring → STOP condition 5 applies; do not ship a test that was never red.
3. Kill-switch test fails at any step → STOP condition 4 applies.

### Implementation note summary (for executor)

- Extract helper from `_plan_slots` (line 2384), not `_assemble_clips` (line 3003). Plan's line numbers 2461-2508 are correct.
- Signature: `compute_snapped_slot_durations(slots: list[dict], beats: list[float], *, is_agentic: bool, user_total_dur_s: float | None) -> list[float]`
- `_plan_slots` refactored to `[step.slot for step in steps]` → helper call → use returned durations
- Locked/exact bypass in helper: use `slot.get("target_duration_s", 0.0)` (no moment data available pre-matching); document in docstring

**VERDICT: APPROVED — zero blocking findings. One implementation note on helper signature (slots: list[dict], not steps: list). Safe to implement.**
