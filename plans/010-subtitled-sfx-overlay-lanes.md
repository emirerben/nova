# 010 — Enable Sounds + Overlays lanes on caption archetypes (subtitled, narrated)

Status: APPROVED (eng review CLEAR, 2026-07-11) — implementing
Decisions: 1A — lift the gate for BOTH caption archetypes (narrated + subtitled).
2A — thread the render-generation token through the caption reburn + reapply chain.
3A — extract `_reapply_user_media_layers()` and use it at all 5 call sites
(2 existing montage at :2902/:3167 + 3 new caption terminals).
4A — keep the `editorReasonCopy` tooltip mappings in this PR.
5A — bump both caption tasks to soft 1740 / hard 1800 (standard render budget).
Outside-voice amendments (all accepted): OV-1 dual text-elements gate (FE tool
disable + API 422); OV-2 bed-level stale round-trip removed; OV-3 supersede
tokens on ALL THREE caption paths; OV-4 old-blob deletes gated on accepted
writes; OV-5 AI overlay suggestions stay OFF on caption archetypes; OV-6 manual
placement freedom kept (occlusion hint → TODO); OV-7 deferred terminal status
during reapply; OV-9 one-deploy kwarg-skew window accepted + documented.
/review fix-round decisions (9-reviewer pass): R1-A — commit-before-enqueue in
the caption dispatchers, montage snapshot-free moved after the accepted write,
reapply chain reports terminal ownership + terminals finalize on no-op, and a
mix dual-gate (capability false + 422) on caption archetypes. R2-A —
caption-archetype SFX/overlay-only saves route through the reburn+reapply
chain (never the fast pass) so every render reproduces persisted cues.
R3-B — deploy-skew window accepted with corrected docs (no redelivery
self-heal; see Rollout 5). R4-A — SuggestionRail capability gating + error
heuristic, fullscreen-budget wall-clock clamp threaded into the overlay pass
(default-off for montage byte-identity), Captions-tab notice line, ToolRail
focusable-disabled a11y pattern. Plus the mechanical auto-fix batch (discard
cleanup, post-terminal failure flag, prep row-lock+gen-gate with
deletes-after-commit, delete prefix confinement, CAPTION_TAB_COPY constant,
shared _text_elements_allowed predicate, public CAPTION_EDIT_ARCHETYPES,
overlay-jobs queue routing for caption tasks, fixture dedup, mid-run
supersession tests ×3, seam integration test).
Owner: Yasin
Surfaced by: /investigate on `/plan/items/{id}/edit?variant=subtitled` — Sounds and
Overlays tabs are greyed out.

## Problem

On the timeline editor, subtitled (and narrated) variants show the Sounds and
Overlays tabs disabled. The server capability map (`_editor_capabilities`,
`src/apps/api/app/routes/generative_jobs.py:2112`) sets
`effects_reason = "caption_archetype"` for any variant whose
`resolved_archetype` is in `_CAPTION_EDIT_ARCHETYPES = {"narrated", "subtitled"}`.

That gate is honest today: all three caption re-render paths rebuild
`video_path` from the caption-free `base_video_path` and never reapply the
user's SFX/overlay lanes, so any composited effects would be silently wiped
(and the old video deleted) on the next caption edit:

1. `_run_reburn_narrated_captions` (generative_build.py:5829) — caption Apply,
   both archetypes
2. `_run_reburn_narrated_bed_level` (generative_build.py:5940) — narrated
   background-sound slider
3. `_run_retranscribe_subtitled` (generative_build.py:6130) — subtitled caption
   language change

## Goal

A creator can add sound effects and media overlays to a subtitled or narrated
edit, and those lanes survive every caption-side re-render (caption edit,
bed-level change, language re-transcribe).

## Non-goals (see NOT in scope)

Text-elements on subtitled, timeline editing on caption variants, flag flips
themselves (rollout step), AI overlay-suggestion eligibility changes.

## Layer model (why reapply-after-reburn is correct)

The montage pipeline already treats user media lanes as outer layers rebuilt
on top of whatever new base a re-render produces. Caption archetypes get the
same contract:

```
  base_video_path (caption-free: clips + voice + bed)
        │
        ▼  caption burn (libass; the ONLY thing caption re-renders change)
  captioned video  ──────────────► becomes the "clean base" for effects
        │
        ▼  _run_media_overlay_pass (video composite, pre_media_overlay snapshot)
  + overlay cards
        │
        ▼  _run_sfx_pass (audio adelay+amix, -c:v copy, pre_sfx snapshot)
  + sound effects  ──────────────► video_path / output_url served to user
```

Reapply chain (already exists, already used by montage full-render + fast-reburn
paths at generative_build.py:2902 and :3167):

```
  caption re-render terminal write (video_path = new burn)
        │
        ▼
  _reapply_persisted_media_overlays_if_any()      # no-op if no overlays/flag off
        │  returns False (nothing to do)              (resets BOTH pre_* snapshots,
        ▼                                              runs overlay pass, whose
  _reapply_persisted_sfx_if_any()                     terminal hook reapplies SFX)
        # no-op if no SFX/flag off
```

## Changes

### 1. Worker: reapply lanes after every caption re-render
`src/apps/api/app/tasks/generative_build.py`

In each of the three terminals (`_run_reburn_narrated_captions`,
`_run_reburn_narrated_bed_level`, `_run_retranscribe_subtitled`):

- Null `pre_media_overlay_video_path` and `pre_sfx_video_path` in the terminal
  patch — a deliberate reset, never a stale round-trip (stale snapshots point
  at pre-reburn video; the old `video_path` is deleted, so a stale snapshot is
  a download-404 waiting to happen even in the no-effects case).
- **OV-2:** remove the bed-level terminal patch's stale round-trip of
  `media_overlays` / `pre_media_overlay_video_path` (generative_build.py:6070-6077)
  — `_update_variant_entry`'s merge preserves the DB's current values, so a
  card saved during the minutes-long rebuild survives. Save-during-render
  regression test required.
- **OV-7 (deferred terminal status):** when persisted lanes + enabled flags
  mean a reapply WILL run, the terminal patch keeps `render_status="rendering"`
  and the reapply chain owns the final `ready`/`failed` write — the exact
  deferred-terminal trick the overlay→SFX hop already uses
  (`will_reapply_sfx` pattern, generative_build.py:1386-1397), including its
  stranded-state failure handling. No effect-less "ready" flicker; the 409
  re-render guard stays closed throughout.
- Chain via the shared helper (decision 3A):
  `_reapply_user_media_layers(job_id, variant_id, expected_render_gen_id)`.
- On reapply failure the variant lands `failed` with the captioned
  (effect-less) video intact — persisted lanes retry on the next edit. Same
  degradation contract as the montage paths. A `SoftTimeLimitExceeded` inside
  the reapply is caught by the chain's handlers → `failed`, never a silent
  effect-less `ready` (test-pinned; OV-10).

### 1b. Supersede tokens on ALL THREE caption paths (decisions 2A + OV-3)

Every dispatch that enqueues a caption re-render mints/propagates a generation:

- `enqueue_editor_commit_render` (generative_jobs.py:2800) passes
  `prep["generation"]` into `reburn_narrated_captions`.
- The bed-level dispatch (:2030) and caption-language dispatch mint a
  `render_gen_id` (same pattern as `dispatch_set_media_overlays`) and pass it
  into `reburn_narrated_bed_level` / `retranscribe_subtitled_captions`.

Each task threads it as `expected_render_gen_id` into ALL its
`_update_variant_entry` writes AND the reapply chain, so a superseded run
discards its terminal write (E1 contract). Signatures stay backward-compatible
(`render_gen_id: str | None = None`) so queued legacy tasks still run.

**OV-4 (delete gating):** every old-blob `delete_object_best_effort` in the
three paths (e.g. :5874-5879, :6079-6084) runs ONLY when the terminal
`_update_variant_entry` returned True (write accepted). A discarded stale task
must never delete the winning task's live video. Stale-token tests assert the
delete is skipped.

### 2. API: lift the capability gate
`src/apps/api/app/routes/generative_jobs.py` `_editor_capabilities` (~:2112)

Remove the `caption_archetype` branch for `effects_reason`. `sfx`/`overlays`
then depend only on the kill-switch flags and `no_video`. `caption_reason`
(text_elements + the Captions-tab copy) is untouched.

**OV-5 (suggestions stay off):** `suggestions_reason` currently falls through
to `overlays_reason` (:2123-2128) — lifting the gate would silently enable AI
overlay suggestions on speech formats. Add an explicit caption-archetype check
(`suggestions_reason = "caption_archetype"`) in the capability map AND the same
guard in the suggest-overlays route (plan_items.py). Manual lanes unaffected;
suggestion quality on speech content is a follow-up evaluation.

### 2b. Text-elements gate, both layers (OV-1)

Lifting the gate flips `readOnly` false on subtitled (EditorShell.tsx:407-414
is an all-capabilities-false conjunction), exposing Text/Styles tools whose
saves would silently no-op (text commit → montage regenerate → caption reject
→ "ready", nothing rendered). Two-layer fix:

- FE: `toolDisabledReasons` gains
  `capabilities.text_elements === false → out.text = out.styles = <Captions-tab copy>`
  (EditorShell.tsx:1246-1263), so Text/Styles stay disabled with an honest
  tooltip. `deleteSelected`/keyboard paths already no-op via selection guards.
- API: `prepare_editor_commit` 422s a `text_elements` section on variants whose
  capability map reports `text_elements` false (mirrors the `caption_cues`
  guard at generative_jobs.py:2633). Narrated keeps text_elements=True —
  unchanged.

No new write routes needed: `dispatch_set_sound_effects` /
`dispatch_set_media_overlays` have no archetype guard, and
`enqueue_editor_commit_render` routes overlay/SFX-only commits to the fast
passes (`_is_overlay_only` / `_is_sfx_only`), which never touch the montage
path. The caption-variant hard reject in `regenerate_generative_variant`
(:2649) stays as defense-in-depth — it fires only for montage-shaped edits,
which remain impossible to submit for caption variants (text/timeline/mix stay
gated).

### 3. Web: honest tooltips for the remaining disable reasons
`src/apps/web/src/app/plan/items/[id]/_editor/EditorShell.tsx`
`editorReasonCopy` (:111)

Map `sound_effects_disabled`, `media_overlays_disabled`, and `no_video` to
human copy (today they render as raw snake_case in the tab tooltip). These are
the reasons users will actually see whenever the Fly flags are off.

### 4. Tests
`src/apps/api/tests/routes/test_editor_commit.py`
- Update the two pins: `test_capabilities_subtitled_caption_archetype_text_elements_off`
  and `test_capabilities_narrated_caption_archetype_text_elements_on` —
  `sfx`/`overlays` become True (flags armed), reasons None. Keep the
  text_elements assertions unchanged.
- New: flags-off still yields `sfx_reason="sound_effects_disabled"` /
  `overlays_reason="media_overlays_disabled"` on caption variants (kill-switch
  parity with montage variants).

`src/apps/api/tests/tasks/` (extend `test_generative_build.py` or a new
`test_caption_reapply.py`)
- CRITICAL regression coverage: for EACH of the three caption re-render paths,
  a variant with persisted `media_overlays` + `sound_effects` →
  `_reapply_user_media_layers` invoked with the right args, `pre_*` snapshots
  nulled in the terminal patch; with only SFX → SFX-only hook; with neither →
  no reapply calls (byte-identical to today).
- Helper unit pins (once, per decision 3A): overlays→overlay-pass branch,
  SFX-only branch, neither→no-op, `MEDIA_OVERLAYS_ENABLED=false`→SFX-only,
  both flags off→full no-op.
- Supersede (2A + OV-3): EACH of the three tasks launched with a stale
  `render_gen_id` discards its terminal write, skips the reapply, AND skips the
  old-blob delete (OV-4) — mirrors the A20/E1 montage tests.
- OV-2: overlay card saved while a bed-level rebuild is in flight survives the
  terminal patch (save-during-render wipe regression).
- OV-7: with lanes persisted, the caption terminal keeps `rendering` until the
  reapply chain writes the final status — no effect-less `ready` observable
  between burn and reapply.
- OV-10: `SoftTimeLimitExceeded` raised inside the reapply → variant `failed`,
  never a silent effect-less `ready`.
- OV-1: FE — Text/Styles disabled on subtitled with the Captions-tab tooltip
  (Jest); API — text_elements commit on subtitled → 422 (route test).
- OV-5: capabilities report `suggestions=false` with `caption_archetype` on
  subtitled/narrated even with all flags on; suggest-overlays route rejects
  caption variants.
- Retranscribe empty-cues early return does NOT trigger reapply and keeps the
  existing (effect-bearing) video.
- Montage refactor equivalence: existing reapply tests at the :2902/:3167 call
  sites stay green unchanged.
- `tests/tasks/test_task_time_limits.py` still passes after the caption-task
  ceiling bump (see Performance).

Route-level
- POST sound-effects / media-overlays on a subtitled variant → 200, enqueues
  the fast pass on the `overlay-jobs` queue (same assertions the montage
  variants have today).
- Caption-cue editor commit passes the freshly bumped generation into
  `reburn_narrated_captions.apply_async`.

Web (Jest)
- `editorReasonCopy`: the 3 new mappings + unknown-code passthrough.

### 5. Free orphaned pre_* snapshot blobs on null (D16-C)

Everywhere the code nulls `pre_media_overlay_video_path` /
`pre_sfx_video_path` (the two reapply helpers at :1607/:1683, the montage
full-render patch at :3152, and the three new caption terminals), route
through one helper — `_null_and_free_media_snapshots(variant_patch, current)`
— that best-effort-deletes the underlying GCS blob BEFORE nulling, **only**
when the snapshot key differs from every currently-referenced key on the
variant (`video_path`, `base_video_path`). `generative-jobs/*` never expires
and has no sweeper, so without this every re-render with effects strands two
blobs forever. Tests: stale snapshot keys deleted; keys equal to a live
reference NEVER deleted; delete failures don't raise (best-effort, same as
`delete_object_best_effort` semantics). Implementation must first verify how
each pass mints its snapshot key (copy vs alias of `video_path`) — the
keep-set guard is the safety net either way.

### 6. Celery ceilings (decision 5A)

`reburn_narrated_captions` and `retranscribe_subtitled_captions` move from
`soft_time_limit=600, time_limit=660` to `soft_time_limit=1740, time_limit=1800`
— the standard budget of every task that inline-runs the reapply chain
(montage regenerate, bed-level reburn). Stays under the 1900s broker
`visibility_timeout` invariant enforced by `tests/tasks/test_task_time_limits.py`.
Rationale: the overlay pass alone has a hardcoded 600s subprocess timeout and
has taken 603s in prod (see learnings: nova-overlay-pass-preset-timeout-coupling)
— the old ceiling cannot contain caption burn + overlay composite + SFX remux.
Known cost: caption edits with effects occupy the single concurrency-1 prod
worker longer (head-of-line; inherent to the feature, not fixable in this PR).

## Failure modes

| Path | Failure | Handling | User sees |
|------|---------|----------|-----------|
| reapply prep (DB) | exception | `_mark_variant_failed` | failed badge, last burn kept |
| overlay reapply encode | ffmpeg error | overlay pass marks failed | failed badge; effects persisted, next edit retries |
| SFX reapply | handled/unhandled | `_run_sfx_pass` / `_mark_variant_failed` | same |
| soft-timeout mid-reapply | SoftTimeLimitExceeded | chain handlers → failed (OV-10, pinned) | failed badge, retry on next edit |
| stale snapshot post-reburn | download 404 | prevented: snapshots nulled in terminal patch | n/a |
| concurrent caption saves | stale task racing newer save | gen tokens on all 3 paths (2A+OV-3); discarded write skips delete (OV-4) | newest save wins |
| save during bed rebuild | stale round-trip wipes cards | round-trip removed (OV-2) | cards survive |
| effect-less ready flicker | poll between burn and reapply | deferred terminal status (OV-7) | continuous "rendering" until final |
| deploy-skew kwarg | old worker TypeErrors on new kwarg | accepted one-deploy window (R3-B); NO redelivery (failure acks); 60-min reaper → failed badge | wrong failure badge, recovers on re-tap |
| lane save races in-flight caption reburn | fast pass wins on old-captions video | caption-archetype lane saves route through the reburn+reapply chain (R2) | newest save always carries current captions |
| mix save on caption variant | silent no-op via montage caption-reject | mix dual-gate: capability false + 422 (R1-4) | slider hidden / honest error |
| subtitled text tool | silent no-op save | dual gate: FE disable + API 422 (OV-1) | tool disabled with honest tooltip |

## Rollout

1. Land the change (gate lift is inert while Fly flags are off — capabilities
   keep reporting `*_disabled` reasons).
2. `fly secrets set SOUND_EFFECTS_ENABLED=true MEDIA_OVERLAYS_ENABLED=true --app nova-video`
   + machine restart (api + worker) — per the CLAUDE.md dual-flag rule, Fly first.
3. Vercel twins (`NEXT_PUBLIC_SOUND_EFFECTS_ENABLED` /
   `NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED`) for the item-page lanes
   (page.tsx:119-126); the editor tabs themselves are capability-driven and
   need no FE flag.
4. Kill switch: flipping the Fly flags off re-disables the lanes everywhere
   (capabilities + write routes 404/422 + reapply no-ops) with no deploy.
5. **OV-9 skew window (corrected by red-team RT-1, decision R3-B):** during the
   single deploy's rolling restart, a caption save from an upgraded API can hit
   a not-yet-upgraded worker → TypeError on the new `render_gen_id` kwarg.
   There is NO redelivery self-heal — `task_acks_on_failure_or_timeout`
   defaults True, so the failed message is acked. The variant sits "rendering"
   until `reconcile_stuck_variants`' 60-minute threshold flips it to a failed
   badge; the user recovers by re-tapping Apply. Accepted as a one-deploy,
   minutes-wide window (two-phase flag judged over-engineering); deploy
   off-peak.

## NOT in scope (additions from review)

- Caption keep-out placement hint (shaded caption band in the overlay editor) —
  TODO (OV-6); manual placement keeps montage parity in this PR.
- AI overlay suggestions on caption archetypes — explicitly gated OFF (OV-5);
  enabling requires a speech-content suggestion-quality evaluation first.
- ~~Orphaned `pre_*` snapshot blob cleanup~~ — pulled INTO scope per D16-C
  (change 5, `_null_and_free_media_snapshots`).

## Parallelization

| Step | Modules touched | Depends on |
|------|----------------|------------|
| T1,T3,T5,T6,T7 worker changes | src/apps/api/app/tasks/ | — (T5 needs T1's helper signature) |
| T4 capability + suggestions gate | src/apps/api/app/routes/ | — |
| T2-API text 422 | src/apps/api/app/routes/ | — |
| T9 route/task tests | src/apps/api/tests/ | T1-T7 |
| T2-FE + T8 tooltip/tool gating | src/apps/web/.../_editor/ | — |

Lane A: backend (T1 → T3 → T5 → T6 → T7 → T4 → T2-API → T9; sequential, shared
generative_build.py + generative_jobs.py). Lane B: frontend (T2-FE → T8;
sequential, shared EditorShell.tsx). Launch A + B in parallel worktrees; merge.
Conflict flag: none — the lanes share no files (T2 spans layers but its API
half lives in Lane A).

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~1d / CC: ~40min)** — worker — reapply chain + deferred terminal + snapshot nulls in all 3 caption terminals via `_reapply_user_media_layers` (rewires the 2 montage sites)
  - Surfaced by: plan core + 3A + OV-7 — Files: generative_build.py, tests/tasks/ — Verify: wipe-regression tests + montage equivalence green
- [ ] **T2 (P1, human: ~half day / CC: ~20min)** — editor — dual text-elements gate (FE tool disable + API 422)
  - Surfaced by: OV-1 — Files: EditorShell.tsx, generative_jobs.py, test_editor_commit.py — Verify: subtitled Text tab disabled; text commit 422
- [ ] **T3 (P1, human: ~2h / CC: ~10min)** — worker — remove bed-level stale round-trip + save-during-render test
  - Surfaced by: OV-2 — Files: generative_build.py:6070-6077 — Verify: mid-render card survives
- [ ] **T4 (P1, human: ~half day / CC: ~20min)** — api — lift effects gate + explicit suggestions gate + pins
  - Surfaced by: core + 1A + OV-5 — Files: generative_jobs.py, plan_items.py, test_editor_commit.py — Verify: updated capability pins
- [ ] **T5 (P2, human: ~half day / CC: ~25min)** — worker — gen tokens on all 3 caption paths + delete gating + stale tests
  - Surfaced by: 2A + OV-3 + OV-4 — Verify: stale task discards write AND skips delete
- [ ] **T6 (P2, human: ~2h / CC: ~15min)** — worker — `_null_and_free_media_snapshots` (free orphaned pre_* blobs, keep-set guard)
  - Surfaced by: D16-C — Verify: stale keys deleted, live refs never
- [ ] **T7 (P2, human: ~15min / CC: ~2min)** — worker — caption task ceilings → 1740/1800
  - Surfaced by: 5A — Verify: test_task_time_limits.py green
- [ ] **T8 (P2, human: ~15min / CC: ~5min)** — web — editorReasonCopy mappings + Jest
  - Surfaced by: 4A — Verify: Jest
- [ ] **T9 (P2, human: ~2h / CC: ~15min)** — tests — subtitled POST routes 200+queue; commit passes generation; soft-timeout→failed pin
  - Surfaced by: test review + OV-10 — Verify: pytest tests/

## What already exists (reused, not rebuilt)

- `_run_media_overlay_pass` / `_run_sfx_pass` — archetype-agnostic fast passes.
- `_reapply_persisted_media_overlays_if_any` → `_reapply_persisted_sfx_if_any`
  chain — the exact hook the montage paths use after producing a new base.
- Editor-commit dispatch — already routes overlay/SFX-only commits to the fast
  passes; caption-cue commits to the reburn task.
- Write routes + validation (`validate_media_overlays_for_user`,
  `validate_sound_effects_for_user`) — archetype-agnostic, user-namespaced.
- Fullscreen constraint validation (`validate_fullscreen_constraints`) — shared.

## NOT in scope

- Text-elements on subtitled variants — captions own on-video text there;
  separate product question.
- Timeline/split editing on caption variants — `locked_to_voiceover` /
  `no_slot_timeline` reasons unchanged.
- Prod flag flips — rollout step above, not code in this PR.
- AI overlay suggestions on caption archetypes — explicitly gated OFF this PR
  (OV-5, change 2); enabling awaits the speech-content eval (T-CAPFX-2).
- Mix lane on subtitled — no voice bed exists; `_BED_LEVEL_ARCHETYPES` stays
  `{"narrated"}`.
- Caption keep-out placement hint — TODO T-CAPFX-1 (decision D13/OV-6).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found (outside voice via Claude subagent) | 10 findings: 2 P1 verified, 8 accepted, 1 built-now (D16-C), 1 folded as test pin |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 15 issues, 0 critical gaps — all folded into this plan |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** Codex CLI not installed — outside voice ran as a Claude subagent; all 10 findings verified against source before acceptance (2 P1s confirmed at EditorShell.tsx:407-414 and generative_build.py:6070-6077).
- **CROSS-MODEL:** outside voice extended (not contradicted) the section reviews — no unresolved tension; every accepted finding maps to a decision D8-D16.
- **VERDICT:** ENG CLEARED — ready to implement (plan 010, decisions D3-D16 all resolved).

NO UNRESOLVED DECISIONS
