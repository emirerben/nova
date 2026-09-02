# Speech Cleanup Mixed-Gap Detection V2

**Status:** implemented; production rollout pending
**Date:** 2026-09-01
**Source of truth:** `origin/main@23751b6b` (`v0.59.2.0`)
**Incident:** job `d2d20bd2-472c-4180-8655-c112fba7647d`
**Plan item:** `068fd75e-3e23-4231-99a6-90e465ebf498`

## Outcome

Fix the structural detector blind spot that left a 571.746 ms Turkish hesitation in
the rendered result. The complete fix has six inseparable backend properties:

1. Inspect silence-bounded soundful islands inside long or mixed ASR-tokenless
   windows instead of judging the entire word gap as one unit.
2. Carry lexical/acoustic filler spans through merge and clamp as atomic components,
   so each filler is either fully removed or fully kept and fillers receive budget
   before ordinary interior silence.
3. Preserve the valid legacy plan behind a candidate-only failure boundary and reject
   any V2 plan that violates partition, word, duration, budget, or count invariants.
4. Assign canaries with persisted never-reused source-instance identity and CAS every
   durable path rewrite against the same path+ID vector.
5. Stage required-speech bytes under one claim-owned generation and publish atomically
   only after compose/finalize, with crash-safe cancel/reap/delete cleanup.
6. Persist a bounded, timing-only, admin-only receipt showing what ASR, FFmpeg, the
   detector, allocator, and terminal publication path each decided.

The behavior is introduced only for explicit `required_v1` cleanup behind
`off | shadow | apply` configuration and a stable source-level percentage canary.
`legacy_auto` and `off_v1` remain byte-identical.

This plan does not add another ASR call, VAD/model, LLM prompt, or FFmpeg pass to the
production job, nor a new table, endpoint, or public response field. It adds one
concurrent sparse index migration so the existing Beat sweep can discover internal
cleanup debts without scanning all Jobs. The operator-only shadow-audition command
performs ephemeral local extraction on demand.

## Incident findings

### What each layer detected

| Layer | Actual finding | Consequence |
|---|---|---|
| Source media | Sustained voiced audio at `7.406100–7.977846` (571.746 ms) | This is not silence or an AAC artifact |
| Production whole-file ASR (AI) | No word token covered the hesitation | Lexical filler rule could not see it |
| FFmpeg `silencedetect` (tool) | Silence `6.209660–7.406100`, then `7.977846–8.293356` | The voiced island was precisely bounded on both sides |
| Current acoustic rule | Rejected the enclosing ASR gap because it was long and contained silence | The island was never proposed as `filler_acoustic` |
| Current pause rule | Removed silence intersections only | The soundful island survived |
| Required-v1 clamp | Spent the full `5.499s` budget on existing carriers | A detector-only patch could still drop or slice the new filler |
| Rendered media | Same audio appears at output `2.847–3.419` | PCM correlation `r=0.95683`; miss is confirmed end to end |
| Isolated local Whisper check | Recognized approximately `ııımm` | Corroborating evidence only; it is not a proposed production dependency |

The rendered source-time decision was:

```text
SOURCE 0.000                                                        10.000
       |------ cut ------|------ kept ------|------ cut ------|--kept--|--cut--|
       0              2.040                  4.656             7.175  9.060   10
                                                                ^^^^^^^^^
                                                                7.406–7.978
                                                                missed filler

SILENCE AROUND THE MISS
       6.209660            7.406100            7.977846      8.293356
       |<---- silence ---->|<-- voiced island -->|<- silence ->|
```

Local forensic artifact: `/tmp/nova-d2d20bd2-media-forensics.png`.

### Confirmed root cause

`app/pipeline/silence_cut.py:447-452` currently evaluates only whole inter-word
gaps:

```python
gap = nxt.start - prev.end
if gap < ACOUSTIC_GAP_MIN_S - _EPS or gap > ACOUSTIC_GAP_MAX_S + _EPS:
    continue
if _overlaps_any(prev.end, nxt.start, silence_spans):
    continue
```

That makes a short voiced island structurally invisible whenever it sits inside a
longer gap or any portion of its containing gap is silence. `_pause_removals`
then intersects only the silence spans, while `_merge_removals` retains only the
first component reason. The existing tests explicitly lock both broken assumptions:

- `test_gap_longer_than_max_left_alone`
- `test_partial_silence_overlap_blocks_acoustic`

Neither test contains a short internal island flanked by silence, and the Turkish
task test supplies `ııııı` as an already-tokenized ASR word. There is no test for
an ASR-omitted vocal filler.

## Scope challenge and auto-decisions

The user explicitly authorized the most complete technical decisions without
additional questions. Each review gate therefore selected the recommended option.

| # | Finding | Options considered | Decision |
|---|---|---|---|
| 1 | Whole-gap acoustic detection cannot see the incident | A: subtract silence and inspect internal islands; B: targeted second ASR/VAD; C: prompt/lexicon tuning | **A** — fixes the actual structural seam with existing signals and no external cost |
| 2 | Detection alone loses to the already-full clamp budget | A: atomic filler provenance and priority; B: raise the 55% budget; C: accept detection-only | **A** — preserves the consent/output rails and fixes mid-vocalization trims |
| 3 | Raw inputs and rejected decisions are not persisted | A: bounded timing-only pipeline receipt; B: raw transcript/new table; C: logs only | **A** — sufficient evidence with minimal privacy and storage exposure |
| 4 | Geometry cannot distinguish every omitted filler from a cough or real word | A: off/shadow/canary apply; B: ship directly; C: add a semantic model now | **A** — measure precision before destructive output changes |
| 5 | The admin strip shows only final cuts and the Trace tab shows raw JSON | A: reuse the admin endpoint and add a layered diagnostic view; B: new endpoint; C: raw JSON only | **A** — directly answers which subsystem detected which span |
| 6 | Matcher IDs/storage keys mutate, and a removed numeric source slot can later hold different media | A: persist a never-reused source-instance UUID beside each path and hash job UUID + instance UUID; B: hash a mutable identifier/index; C: random per run | **A** — stable across retries/reorders without ever aliasing replacement media |
| 7 | `detect_silences()` returns `[]` for both no markers and all tool failures | A: compatible status-bearing companion; B: infer from logs; C: make all failures fatal | **A** — preserves current callers while making V2 decisions and receipts truthful |
| 8 | Forced, filler, and retake spans can overlap, making independent atom rules contradictory | A: transitive protected/atomic components; B: first-reason wins; C: allow partial atoms | **A** — preserves manual intent and whole-vocalization safety |
| 9 | A V2 exception or invalid plan can currently poison the valid baseline entry | A: candidate-only failure boundary + runtime validator + baseline fallback; B: fail the render; C: trust tests only | **A** — shadow/apply fail closed without weakening required-v1 baseline behavior |
| 10 | Append-only traces span retries and selection precedes FFmpeg success | A: per-attempt receipt + correlated render outcome + latest/dedupe semantics; B: claim one event/job; C: destructive trace replacement | **A** — preserves audit history and reports actual output truth |
| 11 | Shadow creates no candidate render for human precision review | A: access-controlled ephemeral source-window audition tool, then join-quality gate at 5%; B: render a second variant; C: skip listening | **A** — executable review without a production media pass or durable copy |
| 12 | Generic trace persistence has no result and may log bound payloads on SQL failure | A: specialized status-returning recorder with exception-class-only logs; B: reuse void sink; C: make trace failure fatal | **A** — measurable best effort without content leakage |
| 13 | New mode coexists with master engine and clamp kill switches | A: explicit precedence matrix and cache/test coverage; B: let branch order emerge from code; C: remove existing switches | **A** — rollback behavior remains deterministic and reviewable |
| 14 | Durable copying can overwrite a concurrently changed source vector | A: compare-and-swap the exact path+instance-ID vector and retry once; B: lock across network copy; C: keep last-writer-wins | **A** — prevents stale media/identity pairing without holding a database lock during object I/O |
| 15 | Allocator drop reasons and final publication state were implicit | A: typed per-atom dispositions plus terminal finalization outcomes; B: reconstruct from spans/logs; C: report renderer intent as success | **A** — UI, tests, and fleet metrics consume authoritative state rather than reimplementing decisions |
| 16 | Metadata analysis completes out of order but cache hits bind records positionally | A: versioned indexed cache records with source identity; B: sort by mutable name/path; C: disable cache | **A** — retries retain exact clip→source assignment, including partial analyses, without giving up caching |
| 17 | Initial speech renders reuse object keys and lack an enforced generation owner | A: reserve a generation, isolate every upload, and CAS pending/final writes; B: DB recheck only; C: serialize all edits | **A** — a losing cleanup render cannot overwrite bytes or metadata belonging to a newer editor generation |
| 18 | Lifecycle-exempt partial uploads survive hard kills and prefix deletion cannot prove success | A: precommitted bounded attempt receipts + dedicated prefixes + status-bearing Beat reconciliation; B: process-local cleanup; C: accept orphans | **A** — crash recovery is durable and receipts clear only after verified-empty storage |
| 19 | Required cleanup currently degrades a bad talking-head spine to an uncleaned montage | A: strict typed failure/last-good restore for required-v1; B: publish montage; C: disable talking-head cleanup | **A** — explicit cleanup is never reported fulfilled by media that skipped its required analysis |
| 20 | A required speech variant is publicly editable while its initial row is still `pending`, so autosave can be accepted and then erased by the same generation's whole-row render upsert | A: private generation write lock checked by every editor mutation; B: merge every possible edit into the renderer result; C: rely on `render_status` timing | **A** — the server rejects all variant mutation until the owning generation reaches a terminal transaction, including deliberate in-flight-save bypasses |
| 21 | Crash recovery reuses any ready-looking row by `ok`/URL/track, even if generation artifacts or the task-local terminal context are missing | A: exact generation-prefix/object/context resume classifier, otherwise rotate and rerender; B: trust the ready row; C: persist the full receipt in public variant JSON | **A** — a retry can finalize only media and evidence proven to belong to the same generation |
| 22 | The terminal-job stuck-variant sweep can promote inherited/provisional `video_path` bytes to ready while speech compose/publish is incomplete | A: both reapers use one row-locked required-speech terminalizer that restores or fails safely; B: exempt speech rows from sweeping; C: keep path-based promotion | **A** — hard-kill recovery never treats “a path exists” as proof that required cleanup published |
| 23 | A precommitted render receipt can remain attached to a successfully published live generation forever and starve bounded debt sweeps | A: consume the exact active receipt on terminal publish and recreate debt only on retirement; B: let Beat revisit referenced rows forever; C: raise page size | **A** — the sparse queue contains cleanup debt, not healthy live media, and page-size limits cannot hide later orphans |
| 24 | Durable-source copy runs before cleanup-mode selection, so legacy/off jobs can reach identity-keyed CAS without source IDs | A: provision internal IDs for every timeline copy attempt and skip copy safely on invalid identity; B: use numeric slots for legacy; C: disable copy outside V2 | **A** — storage ownership always has stable identity while rollout behavior remains unchanged |
| 25 | Oldest-first bounded cleanup pages can be monopolized by permanently unavailable/partial receipts | A: rotate every attempted retained debt row and remove empty keys; B: keep retrying the same oldest page; C: unbound the sweep | **A** — bounded Beat work remains fair even when some storage failures persist |
| 26 | Account export returns `assembly_plan` wholesale, including every proposed “private” control key | A: one shared public projection with reserved private namespace; B: add route-specific deny lists; C: accept internal-state export | **A** — all owner/export surfaces inherit one testable privacy boundary |
| 27 | Explicit cancellation can clear Job status while leaving a lifecycle-exempt generation referenced forever | A: row-locked cancellation terminalization + lease-aware reconciliation; B: rely on the 30s legacy cleanup; C: refuse cancellation during render | **A** — cancellation stays immediate while ownership debt remains durable until safe deletion |
| 28 | Account or individual Job deletion can remove the only receipt before a late worker upload | A: durable prefix/quiescence outbox for account erasure and active-generation guard for individual delete; B: one-pass best effort; C: retain Job rows | **A** — erasure survives delete→late-upload→hard-kill without weakening user deletion |
| 29 | A dispatched or claim-released speech rerender can be stale with control but no `finalizer_claim` | A: exact unclaimed-control restore after no-live+row-lock proof; B: require a claim forever; C: blindly clear control | **A** — never-started/retry-lost operations recover without racing a live owner |
| 30 | Required-speech immediate upserts expose provisional output before final compose/publish | A: private staged result, atomic terminal swap; B: mask selected fields in each read route; C: define render completion as publication | **A** — users see pending/last-good until the exact terminal generation is live |
| 31 | Replacing a legacy fixed-key required output leaves no generation prefix that cleanup can retire | A: durable exact-key retirement debt in the winning transaction; B: `generation=None` best-effort delete; C: leak until account deletion | **A** — old fixed artifacts are deleted only after fresh reference proof, never at winner risk |
| 32 | Globally invalidating the positional v1 metadata cache would make legacy/off rerenders call Gemini again and lose byte parity | A: parallel indexed v2 envelope populated only on real analysis; B: global v2 miss/reanalysis; C: keep unsafe positional identity | **A** — legacy cache hits remain deterministic and old rows simply stay out of apply |
| 33 | Nullable `source_tag` makes multiple unassigned sources collide in fleet aggregation | A: attempt+safe-slot/event-ordinal failure key; B: count one failure per job; C: fabricate a source tag | **A** — assignment failures remain distinct without entering treatment denominators |
| 34 | Status GET performs a lazy DB write that can race the private-stage terminal swap | A: compute-only and defer persistence for locked variants; B: let GET bypass the lock; C: return 409 from polling | **A** — reads stay available and cannot mutate or lose overlay preview state |
| 35 | Cleanup reference proof omits the new private staged-result map | A: include every staged artifact and fail closed on malformed stage; B: assume lock/control always survives; C: disable Beat cleanup | **A** — cleanup cannot delete the only bytes behind a recoverable private stage |

### Complexity gate

A single patch would touch more than eight substantive files and now crosses detector,
identity, durable storage, publication, and UI invariants, so delivery is reduced into
seven reviewable PRs rather than weakening coverage:

- **PR A — pure correctness:** status-bearing silencedetect result, detector, atomic
  clamp, incident golden, docs/TODO.
- **PR B — source identity:** never-reused IDs, indexed metadata cache, and mutation wiring.
- **PR C — durable source safety:** source-copy CAS, verified cleanup receipts, and
  hard-kill sweep recovery.
- **PR D — generation ownership:** generation-isolated render artifacts, fenced
  publish/rollback, and strict required-cleanup failure.
- **PR E — controlled selection:** mode/canary, cut-cache identity, validator, and both
  renderer integrations.
- **PR F — trustworthy evidence:** bounded receipt, privacy, attempt correlation, and
  state-transaction publication outcomes.
- **PR G — diagnostic UX:** layered admin view, truthful creator no-change copy, and
  ephemeral shadow-audition operator tool.

No PR introduces a new service boundary or data store.

## What already exists and will be reused

- `transcribe_whisper(..., verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT)` already
  produces the one ASR result used by lexical detection and captions.
- `detect_silences(..., min_silence_s=0.1)` already produces the exact FFmpeg
  intervals needed by the mixed-gap rule.
- Its current `[]` contract deliberately conflates a successful run with no markers
  and every probe/audio/FFmpeg/parse failure. Keep that wrapper compatible, but add a
  status-bearing companion so V2 and operators never interpret tool failure as a
  calibrated “no silence” result.
- `_normalize_silences` and `_intersect_span` already clamp, sort, merge, and
  intersect silence ranges.
- `_silence_cut_analysis` is already the one cached integration shared by subtitled
  and talking-head variants.
- Both renderer paths already carry a matcher `clip_id`, and `_ingest_clips` preserves
  strict input order while durable-source copying is an order-preserving 1:1 rewrite.
  The matcher ID is **not** a stable rollout identity (`ref.name` can change and
  single-clip fallbacks commonly collapse to `clip_0`), while storage keys can change
  after a best-effort durable copy and numeric slots can be reused by Omni. PR B must
  persist a never-reused source-instance ID and carry a typed assignment envelope
  instead of reusing any of those values.
- `_SilenceCutCache` already guarantees one ASR/silence analysis per clip and caches
  failures consistently across sibling variants.
- `CutPlan`, `Removal`, `plan_summary`, `plan_event_payload`, and the required-v1
  budget/output rails remain the plan contract.
- `pipeline_trace_for` and `record_pipeline_event` already persist admin-only,
  best-effort per-job diagnostics.
- `/admin/jobs/{id}` already receives the full `pipeline_trace`; no endpoint or auth
  change is necessary.
- `SilenceCutStrip` already visualizes final persisted cuts and remains the final-plan
  layer in the new diagnostic view.

## Architecture

### End-to-end flow

```text
                         ONE external analysis pass
source clip ──┬── ASR words (AI) ────────────────────────────────┐
              └── FFmpeg silence spans (tool, d=100ms) ─────────┤
                                                               ▼
                                              normalize intervals once
                                                               │
                    ┌──────────────────────────────────────────┴─────────┐
                    ▼                                                    ▼
          baseline plan (legacy)                              mixed-gap V2 plan
          current byte behavior                    silence complement per ASR window
                                                              │
                                                silence-bounded islands
                                                              │
                                           atomic lexical/acoustic filler spans
                                                              │
                                           provenance-aware budget allocation
                    └──────────────────────────┬─────────────────────────┘
                                               ▼
                              off/shadow/apply + stable canary choice
                                               │
                         ┌─────────────────────┴─────────────────────┐
                         ▼                                           ▼
                 render selected plan                  timing-only admin receipt
                 (baseline or V2)                      (baseline vs candidate)
```

Add a matching compact version of this diagram to the module docstring in
`silence_cut.py`, because the current diagram's “soundful short gaps” wording becomes
incomplete after V2.

For shadow/apply, add a pure `build_cut_plan_comparison(...) -> CutPlanComparison`
entry point. It normalizes words and silence intervals once, builds the byte-compatible
baseline through a shared `_build_cut_plan_normalized(...)` core, then attempts the V2
candidate from the same immutable normalized inputs inside its own failure boundary.
Its result always retains a successfully built baseline plus candidate/status fields;
baseline exceptions retain today's strict behavior. The existing public
`build_cut_plan(...)` remains a single-plan wrapper and its default behavior is
unchanged. `off` calls only that legacy wrapper; it does not pay for candidate work.
This paired contract is the only place baseline/candidate plans may be constructed,
so task code cannot drift into a second detector implementation.

### Detector V2

Add a single pure interval helper, conceptually:

```python
def _interior_soundful_islands(
    window_lo: float,
    window_hi: float,
    silence_spans: list[tuple[float, float]],
) -> list[AcousticDecision]:
    """Maximal non-silent complements, including flank lengths and rejection data."""
```

It must reuse normalized silence spans and operate deterministically in timestamp
order. Analyze these ASR-tokenless windows:

1. `[0, first_word.start]`
2. every `[previous_word.end, next_word.start]`
3. `[last_word.end, duration]`

For a window with no silence intersection, keep today's legacy wholly-soundful
short-gap behavior byte-for-byte, including `PAD_ACOUSTIC_S` on word flanks.

For a mixed window, compute maximal complement islands and accept an island only if:

- its duration is within the existing `ACOUSTIC_GAP_MIN_S`–
  `ACOUSTIC_GAP_MAX_S` band (`0.15–1.2s`);
- a contiguous detected-silence flank of at least `0.1s` exists on both sides;
- it is strictly inside the ASR window and touches neither a recognized word nor
  the clip boundary;
- the global zero-silence calibration gate is open.

Do not pad inward from an accepted silence-bounded island. Its exact soundful bounds
are the filler atom; adjacent silence remains owned by `_pause_removals`. Padding an
island inward would leave audible filler at both ends. `MIN_CUT_S=0.18` remains the
separate jump-cut hygiene floor; a 150–179 ms classified island may be reported and
dropped by hygiene if it is not part of a larger safe carrier.

The helper must also return bounded decision metadata for accepted and rejected
islands: window bounds, island bounds, left/right silence durations, decision, and
reason. It must never include word text.

Use an explicit internal result contract rather than a mutable audit sink or a second
copy of the detector in the task layer:

```python
@dataclass(frozen=True)
class AcousticDecision:
    window_start_s: float
    window_end_s: float
    island_start_s: float
    island_end_s: float
    left_silence_s: float
    right_silence_s: float
    detection: Literal["eligible", "rejected"]
    reason: str

@dataclass(frozen=True)
class AtomicDisposition:
    atom_start_s: float
    atom_end_s: float
    group_start_s: float
    group_end_s: float
    atom_kind: Literal["filler_lexical", "filler_acoustic", "retake"]
    priority: Literal["protected", "filler", "retake"]
    disposition: Literal[
        "selected_full",
        "promoted_protected",
        "dropped_budget",
        "dropped_max_removals",
        "dropped_min_cut",
        "dropped_micro_gap",
        "dropped_safety_bailout",
    ]

@dataclass(frozen=True)
class CutDiagnostics:
    lexical_candidates: tuple[Removal, ...]
    acoustic_candidates: tuple[Removal, ...]
    acoustic_decisions: tuple[AcousticDecision, ...]
    acoustic_decisions_total: int
    acoustic_decisions_omitted: int
    atomic_dispositions: tuple[AtomicDisposition, ...]
    atomic_dispositions_total: int
    atomic_dispositions_omitted: int
    proposed_removals: tuple[Removal, ...]
```

Add `diagnostics: CutDiagnostics | None = None` to the in-memory `CutPlan` and
`mixed_gap_enabled: bool = False` to `build_cut_plan`. The default path must construct
the same version-1 plan as today. A mixed plan sets `version=2` and attaches bounded
diagnostics. `plan_summary` and `plan_event_payload` remain explicit allowlists and
must not serialize `diagnostics`; only the specialized admin receipt may consume it.
This preserves every existing caller while giving shadow/apply one typed source of
candidate truth. Mixed-plan safety bailouts must return the no-op keep plan **with the
already-collected diagnostics attached**; otherwise the exact cases operators most
need to explain would disappear from the receipt.

`AtomicDisposition` is the allocator's authoritative final state, one record per
input atom. All members of a connected group carry the same group bounds and final
disposition. The allocator overwrites an earlier provisional selection if later
micro-gap/count/hygiene handling evicts the group, and marks every atom
`dropped_safety_bailout` if a candidate-wide safety bailout returns a keep plan.
The receipt joins an eligible acoustic island to this output by exact normalized atom
bounds; task/UI code must never infer disposition from final span overlap. Records are
chronologically bounded with explicit total/omitted counts and contain no word text.

### Provenance and atomic clamp contract

Capture raw lexical, acoustic, retake, and forced spans before `_merge_removals`.
Before budget allocation, form an interval graph over protected and atomic spans;
positive overlap within `_EPS` creates an edge, and allocation operates on its
connected components rather than individual reasons. Do not overload
`protected_spans`:

- **Protected** forced/manual spans are hard and may exceed the cleanup budget; the
  existing `MIN_OUTPUT_S` rail remains their final backstop.
- A component containing any protected span becomes a **protected closure**. Every
  overlapping filler/retake atom is promoted to that closure, recursively, so the
  explicit manual cut and whole-atom safety can both hold. This is the sole documented
  exception where an atom becomes budget-exempt; if the closure violates
  `MIN_OUTPUT_S`, return the existing safety bailout rather than trim it.
- An atom-only connected component is an **atomic group**. Its union is budget-limited,
  charged once, and selected whole-or-none. If it mixes filler and retake spans, use
  filler priority and retain all member reasons in diagnostics.
- **Flexible** silence carrier pieces may be trimmed to consume leftover budget.

The V2 candidate plan's required-v1 allocation order is:

```text
1. forced/manual intersections       hard, unchanged
2. leading and trailing dead air     existing hook/edge priority
3. lexical + acoustic filler atoms   whole-or-none
4. detected retake atoms             whole-or-none
5. remaining interior silence        longest first, trimmable
```

Keep #959's 1 ms slack, word/PAD snapping, edge anchoring, `MIN_CUT_S`,
`MIN_KEEP_SEGMENT_S`, `MAX_REMOVALS`, and `MIN_OUTPUT_S` invariants.
The baseline plan used by `off`, `shadow`, `legacy_auto`, and out-of-bucket apply
continues through the current allocator unchanged; the new ordering is not silently
backported to baseline behavior.

An oversized merged carrier containing groups is decomposed for allocation:

1. Build protected closures and atom-only connected groups from the pre-merge lists.
2. Subtract the union of **all** protected/atomic group geometry—selected or
   dropped—from every merged carrier before creating flexible pieces. Group geometry
   can re-enter the plan only through its tagged group decision; provenance-erased
   carrier trimming can never recut it.
3. Select protected closures, then keep leading/trailing flexible edge coverage only
   when its trim boundary lies outside every group.
4. Charge each uncovered atomic group whole if budget and removal-count capacity fit;
   otherwise leave it wholly kept and record `dropped_budget` or
   `dropped_max_removals`.
5. Let only the pre-carved, group-free flexible pieces compete for leftover budget.
6. Merge selected pieces only after allocation.
7. Never absorb a word-free micro-fragment by exceeding the budget. If a bridge
   cannot fit, first drop an adjacent flexible piece. If the sub-
   `MIN_KEEP_SEGMENT_S` bridge sits between selected non-flexible groups, evict the
   lower-priority budgeted group whole (retake before filler; then later-starting on a
   tie). If one side is protected, evict the budgeted side. If both sides are
   protected, promote the word-free bridge into the protected closure and re-run
   `MIN_OUTPUT_S`/count/partition validation; bail out rather than trim if any rail
   fails. A word-bearing bridge between protected groups is never absorbed and causes
   the same typed safety bailout. Never split a group.

Every terminal allocator branch writes exactly one final `AtomicDisposition` per
atom. In particular, budget, removal-count, minimum-cut, micro-gap eviction, protected
promotion, and safety-bailout branches are mutually exclusive and exhaustively
tested; there is no generic string fallback.

The runtime validator cross-checks dispositions against geometry: every `dropped_*`
group has exactly zero final overlap, every selected/promoted group is fully covered,
and no flexible component intersects any group. A mismatch rejects the candidate and
falls back to baseline.

Move the V2 `MIN_CUT_S` hygiene decision until after tagged carrier construction.
A 150–179 ms island is selectable only when it merges with adjacent selected silence
into a final cut at least `MIN_CUT_S`; otherwise drop the island whole with
`dropped_min_cut`. The baseline retains today's pre-merge filtering order.

Move V2's `MAX_REMOVALS` enforcement into allocation; never run the legacy
largest-duration truncation over a V2 proposal. Count final disjoint components in
priority order: protected closures first, then edges, filler-bearing atomic groups,
retake-only groups, and flexible silence. If protected closures alone exceed the cap,
return the typed safety bailout. Otherwise drop lower-priority groups/pieces whole
until the final merged plan is at most `MAX_REMOVALS`, then assert the cap again.

Postcondition, checked for every atomic span and every possible clamp budget:

```text
final_overlap(atomic_group) == 0  OR
final_overlap(atomic_group) == duration(atomic_group)
```

This also applies to protected closures: they must be fully covered or the plan must
bail out; a manual span strictly inside a filler can never create a partial filler cut.

For the incident, the `5.499s` allocation keeps the leading/trailing edge work and
both internal acoustic atoms, then gives the remaining budget to ordinary interior
silence. No boundary may land inside `5.778866–6.209660` or
`7.406100–7.977846`.

### Mode, canary, and cache identity

Add validated settings:

```python
speech_cleanup_mixed_gap_mode: Literal["off", "shadow", "apply"] = "off"
speech_cleanup_mixed_gap_rollout_percent: int = Field(default=0, ge=0, le=100)
```

Rules:

- V2 is evaluated only for `analysis_policy == "required_v1"`.
- `off`: build/render baseline and emit no V2 receipt.
- `shadow`: build baseline and V2 in memory, render baseline, emit comparison.
- `apply`: build both; use V2 only for clips inside the percentage bucket.
- `legacy_auto` and `off_v1`: never enter V2 regardless of settings.
- Missing or ambiguous rollout assignment downgrades configured `apply` to effective
  `shadow`; it never upgrades configured `off`.

Configuration precedence is part of the contract:

| Cleanup contract / flags | Mixed-gap behavior |
|---|---|
| `off_v1` | Skip cleanup; ignore mixed mode, percent, assignment, and clamp flag; no cache/receipt |
| `legacy_auto` | Preserve current behavior exactly; mixed mode/percent/assignment ignored |
| `required_v1` + `silence_cut_enabled=false` | Existing typed `engine_unavailable` failure occurs before V2; mode cannot bypass the master kill switch |
| `required_v1` + engine on + `speech_cleanup_budget_clamp_enabled=false` | Preserve existing bailout/`unsafe_plan` semantics for baseline and any selectable candidate; V2 cannot re-enable clamping |
| `required_v1` + engine/clamp on + mixed `off` | Baseline only, no V2 receipt; percent and assignment ignored |
| Same + mixed `shadow` | Baseline live, candidate evaluated/validated, receipt emitted |
| Same + mixed `apply`, assigned and in bucket | Candidate live only when status is `ready`; otherwise baseline fallback |
| Same + mixed `apply`, out of bucket | Baseline live with comparison receipt |
| Same + mixed `apply`, assignment unavailable | Effective shadow, nullable assignment receipt, baseline live |

The existing required-v1 rule that ignores the per-item legacy disable flag remains
unchanged. Cache identity includes analysis policy, master/clamp state, detector
version, and effective selection mode so no row crosses a precedence boundary.

Do not bucket on a matcher `clip_id`, numeric source slot, or storage path. Matcher
names change, storage paths are rewritten best-effort, and Omni can pop a slot then
append different media at the same index. Add a parallel internal
`all_candidates["clip_source_instance_ids"]` list containing random UUIDs. Separate
**identity provisioning** from **rollout assignment**. Before every timeline durable-
source copy attempt, regardless of `required_v1`/`legacy_auto`/`off_v1` or mixed mode,
row-lock the Job, pass the existing `_cancelled_job_write_rejected(..., db=db)`
cancellation and content-owner/epoch fence, validate IDs are unique and cardinality-
matched to `clip_paths`, and atomically backfill IDs only when the legacy list is
wholly absent. This internal provisioning is required for copy ownership; it does not
run V2, compute a bucket, emit a receipt, or change media selection. A missing/
rejected Job does not backfill or retry. A backfill is usable only after commit.
Durable path rewriting preserves the ID list unchanged. Every
clip-list mutation must edit path and ID as one pair: reorder moves both, removal
removes both, and append/replacement creates a fresh never-reused UUID.
Only a wholly absent legacy list is backfilled. A present malformed, duplicate, or
cardinality-mismatched list is never silently regenerated—doing so would rewrite
historical treatment identity. The durable-copy attempt returns typed
`identity_unavailable` before remote I/O and continues from the original readable
paths; a required-v1 apply attempt also becomes unassigned/shadow and surfaces the
exact status. Off/legacy contracts keep their existing cleanup behavior but never
fall back to numeric-slot copy keys.

Only after provisioning, and only for `required_v1` shadow/apply, construct the
rollout assignment. Mixed `off`, `legacy_auto`, and `off_v1` ignore IDs for treatment
and do not emit V2 evidence; the internally provisioned list is merely storage state.

Durable-source rewriting must be an optimistic compare-and-swap over identity, not a
last-writer-wins path update. `_persist_durable_sources` captures one immutable
ordered vector of `(source_path, source_instance_id)` pairs, copies from exactly that
vector without holding a database lock, and uses the instance UUID plus a random
per-copy attempt UUID—not the reusable numeric slot—in every newly created
destination key, for example
`generative-jobs/{job_id}/sources/copy-attempts/{copy_attempt_id}/`
`{source_instance_id}/{basename}`.
That makes object ownership unambiguous for rollback cleanup. Existing valid legacy
durable keys remain readable; only new snapshots use the collision-proof shape.

Before the first copy, a short row-locked preflight repeats the owner/cancel/vector
checks and appends a bounded receipt `{copy_attempt_id, prefix, upload_state,
lease_expires_at}` to
`assembly_plan["_speech_cleanup_internal"]["durable_source_copy_pending"]`.
`upload_state` starts as
`"writing"`; the lease covers the task hard-time limit plus storage-call grace. Cap the
list at 32; reconcile first and return retryable
`source_copy_cleanup_backpressure` rather than copy when the cap remains full. Final
CAS success or a caught failure sets `upload_state="closed"` under lock. A hard kill before
CAS leaves a durable writing receipt instead of an undiscoverable lifecycle-exempt
orphan.

After copying, the function row-locks the Job and requires, in this order: the Job
exists; the existing `_cancelled_job_write_rejected(..., db=db)` cancellation and
content-plan ownership/epoch fence accepts the write; and the current ordered path+ID
vector equals the captured vector byte-for-byte. Only that successful fenced CAS may
replace paths with their durable equivalents; the ID vector is unchanged. The
already-durable fast path performs the same row-locked fence/vector validation before
returning—it cannot bypass ownership or race checks.

Return a typed internal result with `persisted`, `already_durable`,
`identity_unavailable`, `copy_failed`, `stale_source_vector`, `job_missing`, or
`terminal_write_rejected`. Only
`stale_source_vector` reloads and retries the complete copy/CAS once. A second stale
result becomes typed retryable `source_list_changed` and renders no stale media.
Job-missing, cancellation, or content-owner rejection stops immediately with no
retry. A storage-copy failure preserves the original pair vector and current
best-effort rendering behavior when those original objects remain readable; it never
changes IDs or treatment assignment.

Every non-accepted exit—including fail-after-N partial copy, row/ownership rejection,
CAS mismatch, commit exception, and second-stale termination—runs one reference-
checked cleanup over **only** keys owned by that copy attempt. Under a fresh Job read,
exclude any key referenced by the current path vector, then pass the remainder to the
existing job-prefix ownership deletion helper. If reference state cannot be read,
delete nothing. Cleanup failure remains best-effort but emits only bounded
status/count/error-class scalars; it never changes the retry or render decision. This
closes lifecycle-exempt object leaks without risking a concurrently adopted durable
source.

`reconcile_durable_source_copy_cleanup(job_id)` handles both immediate failure and
the existing bounded Beat storage sweep. For each pending attempt, read fresh Job
state and delete its dedicated prefix only when no current source path references it
**and** `upload_state == "closed"` or the lease has expired; unreadable/unexpired state
deletes nothing. If a successful CAS left a still-pending receipt, the live reference
proves the prefix must stay and the reconciler removes only the receipt after uploads
close. Cleanup/delete failure retains the receipt. The copy-attempt receipt is
persisted before the first remote copy call, so an exception after remote acceptance
is still recoverable. Tests kill the worker after N
copies but before final CAS, then prove retry/sweep removes the orphan prefix without
touching the new live vector.

Derive the rollout fingerprint from that persisted identity:

```python
def _speech_cleanup_rollout_fingerprint(job_id: str, source_instance_id: str) -> str:
    return sha256(
        f"speech-cleanup-source-v1:{job_id}:{source_instance_id}".encode()
    ).hexdigest()
```

Carry source slot + instance ID through `_ingest_clips` and build a typed per-clip
envelope rather than a one-way string map:

```python
@dataclass(frozen=True)
class SpeechCleanupAssignment:
    source_slot: int | None
    rollout_fingerprint: str | None
    status: Literal[
        "assigned",
        "missing_source_instance",
        "cardinality_mismatch",
        "invalid_source_instance",
        "duplicate_source_instance",
        "unmapped_clip_id",
        "ambiguous_clip_id",
        "identity_cache_unavailable",
    ]
```

Identity must also survive the clip-metadata cache. Today
`_analyze_clips_parallel` appends successful metadata in `as_completed` order, while
the cache stores that list and a cache hit zips it positionally back onto ordered
source paths. Replace the generative call with an indexed adapter that returns
successful `(source_slot, ClipMeta)` records plus explicit failed slots; retain the
existing two-value wrapper for music/template callers. Fresh ingest, cache storage,
and cache load all construct maps from `source_slot`, never list position, completion
order, path basename, or Gemini name.

Do **not** globally bump/invalidate `_CLIP_METADATA_CACHE_VERSION=1`: populated v1
rows are part of legacy rerender determinism. Add a parallel versioned
`clip_metadata_identity_index_v2` envelope. A real analysis call populates it from
future→source-slot bookkeeping while preserving the existing downstream `clip_metas`
order/return contract. Indexed records contain bounded internal fields
`{source_slot, source_instance_id, clip_id, meta}` plus `failed_source_slots`; their
fingerprint uses the ordered source-instance vector. On load require:

- version/fingerprint match and unique in-range source slots;
- each cached instance UUID equals the current UUID at that slot;
- successful and failed slots are disjoint and partition the analyzed input set;
- each `clip_id` maps to exactly one source record.

`legacy_auto`, `off_v1`, and required-v1 mixed `off` keep exact v1 cache-hit semantics
and never call Gemini merely to create identity data. If a populated v1 cache lacks a
valid parallel v2 index, shadow/apply must not repair it positionally or force a model
call: preserve baseline rendering, emit bounded `identity_cache_unavailable`, and make
apply effectively shadow for that attempt. New/future genuine analyses populate both
contracts, so eligible traffic grows naturally without a second analysis pass.
Malformed, duplicate, or identity-mismatched v2 entries are likewise unavailable,
not positional misses. If a validated identity record still cannot be produced for
the rendered clip (including an ambiguous duplicate `clip_id`), preserve rendering but use
`unmapped_clip_id`/`ambiguous_clip_id` and force apply→shadow. Partial analysis keeps
only its explicit successful source records, so omission of slot 0 can never shift
slot 1 onto it. The internal UUID/cache fields remain excluded from owner responses.

Pass `speech_cleanup_assignment_by_clip_id` explicitly into both render functions and
pass the selected envelope into `_silence_cut_analysis`. Keep the existing
`source_fingerprint` used by speech-cut review state separate. The subtitled path
chooses the assignment for the same first `clip_id` it already renders and permits
extra map entries. The talking-head task closure resolves the selected spine ID. Add
`render_trace_id` to `_render_talking_head_variant` and its caller so both paths can
pass `analysis_attempt_id`. If validation or lookup fails, preserve the exact status
and force apply→shadow. Do not hash paths, matcher names, media bytes, or slots.

Stable assignment:

```python
bucket = int.from_bytes(
    sha256(f"mixed-gap-v1:{rollout_fingerprint}".encode()).digest()[:4],
    "big",
) % 100
eligible = bucket < rollout_percent
```

Retries, reorders, and sibling variants of the same source instance therefore agree;
replacement media gets a new assignment even if it reuses the same slot or `clip_0`.
Never persist the rollout fingerprint. Persist only a
domain-separated second hash truncated to 16 lowercase hex characters as
`source_tag`.

The audition tool resolves a historical receipt by recomputing tags from the current
path/instance-ID pairs and requiring exactly one match; `source_slot` is display-only
historical context and is never a lookup key. If the source was removed, replaced,
duplicated, or no longer exists in storage, refuse to audition instead of falling
back to the current occupant of that slot. Add a public-route sentinel proving the
internal instance-ID list is not serialized to owner responses.

Include source-instance fingerprint, detector version, clamp state, and effective mode
in `_SilenceCutCache` identity. A cached baseline entry must never satisfy another
source instance or an apply request after config/policy skew.

Baseline and candidate have separate failure boundaries. The paired builder must
first produce the baseline under the existing strict required-v1 behavior. Only then
may its candidate path—and subsequently task-layer validation/receipt construction—
enter candidate-only `try` blocks. A V2 builder, validator, or receipt-construction
exception sets a bounded `candidate_status` (`build_failed`,
`validation_failed`, or `receipt_build_failed`), records only the exception class in
the scalar log, and returns the valid baseline entry with `failed=False`. It must not
trip the shared broad exception handler or abort a required cleanup render.
For receipt-construction failure, send a separately implemented fixed-shape minimal
receipt containing only schema/attempt/assignment/mode/status scalars; do not reuse
the failed interval serializer. If even that persistence path fails, the scalar
persistence outcome remains the final signal.

Before any apply selection, run a pure `_validate_v2_candidate(...)` that verifies:

- keep/remove exactly partitions the clip and timestamps are finite, ordered, and in
  bounds;
- every protected closure and selected atomic group is fully covered, never partial;
- non-protected removal stays within the clamp budget and protected overage is
  accounted separately;
- output duration, `MIN_CUT_S`, and `MAX_REMOVALS` rails hold;
- no removal intrudes into an ASR word unless the overlap is exactly required by a
  selected lexical/retake group or by an accepted protected/manual span and its
  documented protected closure.

Any invalid candidate falls back to baseline even for an in-bucket apply clip and
uses `candidate_status="validation_failed"`. A non-`ok` silence result uses
`candidate_status="tool_unavailable"`. Only `candidate_status="ready"` may be
selected. Receipt persistence itself remains best-effort after a valid payload has
been built; an external trace outage does not change rendered media.

### Generation-isolated speech render ownership

Terminal generation checks are insufficient if a losing renderer has already
overwritten a shared object key. Scope this correction to required-v1 subtitled/
talking-head attempts and their speech-timing rerenders; `legacy_auto`, `off_v1`,
montage fallback, narrated, and other archetypes retain existing storage behavior.
Before the pending-row upsert, allocate a fresh random `storage_generation` for every
non-resumed required-v1 speech spec and persist it as that pending row's
`render_generation_id`. In that same row-locked write, add
`assembly_plan["_speech_cleanup_internal"]["required_speech_generation_locks"]`
`[variant_id] = generation`. This is a private control map, not a variant/response
field. Reservation is a
row-locked CAS against the exact previously observed generation (including an
explicit missing-row sentinel), plus the existing cancellation/content-owner fence
and, for a speech-cut rerender, the expected operation+attempt claim. Two workers
starting from the same prior generation therefore cannot both reserve ownership.

The private lock is a server-side editor write barrier, not the worker's ownership
proof. Add one shared `assert_variant_generation_editable(job, variant_id)` predicate
in `app/services/variant_generation_guard.py`; malformed lock data fails closed. Call
it from `require_editable_variant`, from `prepare_editor_commit` (whose in-flight-save
path deliberately bypasses `require_editable_variant`), and before every other
variant mutation in `routes/generative_jobs.py`, `routes/plan_items.py`, and
`routes/creator_agent.py`, including `render=False` overlay/TextElement/timeline
autosaves, no-render editor commits, and creator craft before
`_stage_creator_speech_cut` mutates control/variants or mints another generation.
Reads remain available. A locked mutation returns one stable 409
`variant_initial_render_in_progress` before validation, title changes, JSONB writes,
generation bumps, or queue dispatch. Internal render writers do not call the route
guard; they continue to require exact generation+operation+attempt CAS.

Audit hidden writes on read paths too. `_variants_for_response` may compute lazy media-
overlay preview paths for the returned snapshot, but when a variant has a private
generation lock it must neither persist `_persist_media_overlay_preview_backfills`
nor mark that variant in the process-local attempted set. The GET still returns 200
and may show the computed preview; after terminal publish, a later poll can persist it
through the existing fresh-row merge. This keeps polling read-safe during staging and
prevents a terminal swap from erasing a stamp or suppressing its retry.

Keep the lock through pending, rendering, private staged-result persistence, and
finalization. The staged-result write must not remove it because it lives beside the
variant row in the private container.
Only the same row-locked terminal publish, owned failure, last-good rollback, or stale-
job terminalization transaction may remove the exact matching map entry. A losing
generation cannot clear a newer lock; claim recovery atomically replaces old
generation with new generation without an unlocked gap. Public serializers must
exclude the private map, and route tests must prove it never appears in owner or plan-
item payloads.

Speech-cut rerenders use one **claim-owned** generation end to end. Dispatch persists
its target generation in both the variant and `speech_cut_control`; the first winning
finalizer claim adopts it. If a hard-timeout claim is recovered, the row-locked claim
rotation allocates a fresh generation and updates both fields atomically, so the late
worker retains only its losing keys. `_run_generative_job` reads the generation from
the winning claim and carries it through spec, pending/rendering writes, renderer,
private stage, `_finalize_job`, compose, and publish.
`_compose_speech_cut_rerender` must not mint another generation. Every marking/final
write—including `_render_one_spec`'s `rendering` update—checks generation, operation,
and attempt under the row lock. The publish result returns that same winning generation,
so terminal outcome correlation needs no provisional→published remap.

Required-v1 renderer results are **staged, not published**. Replace their immediate
whole-row `_upsert_variant_entry` with a row-locked
`stage_required_speech_generation(...)` write under
`_speech_cleanup_internal.staged_render_results`, keyed by variant+generation and
capped at 16. It stores the complete bounded variant result needed by finalization and
crash recovery, but the public `variants` row remains pending/rendering and retains
only its preexisting last-good fields. For an initial render there is no playable URL;
for a speech rerender, status/download projection continues to serve only the exact
last-good snapshot while control is active. All public projection strips the staged
map. `_finalize_job` consumes staged initial results, and speech compose consumes the
staged rerender; only the terminal generation/claim transaction swaps the completed
result into `variants`, clears control/lock/context/stage, and makes new bytes
readable. Non-required render paths keep the existing immediate-upsert behavior.

If the 16-entry stage cap cannot be reconciled, do not upload another generation;
return retryable `generation_stage_backpressure`. Stale/superseded stage entries are
retired only after their prefix receipt is durable. Reads remain available because
they see pending/no-output or last-good—not provisional output.

Thread the reserved generation through `_render_talking_head_variant`,
`_render_subtitled_variant`, their composition helpers, and every direct caller. Every
object created by those attempts must use a dedicated
`generative-jobs/{job_id}/render-generations/{generation}/` prefix via
`_variant_storage_key` or a child key derived from that generation-scoped base. This
includes final/base video, posters,
pre-media/pre-SFX video, subject matte and JSON sidecar, camera/visual/motion bases,
and any lane intermediate uploaded to durable storage. No new required-v1 speech
render may write the legacy fixed
`variant_{rank}_{id}.mp4`/`base_{rank}_{id}.mp4` keys.

Define **resumable staged result** narrowly with a pure, typed
`classify_required_speech_resume(...)` decision. Reuse is allowed only when all of
these hold under a fresh row snapshot:

- the private staged result has `ok` and `output_url`, its track matches the spec, and
  the public pending/rendering row is still owned by its nonempty syntactically valid
  `render_generation_id`;
- the private write lock names that exact generation;
- every present attempt-owned artifact field (final/base/poster, pre-media/pre-SFX,
  matte/sidecar, camera/visual/motion bases, and uploaded lane artifacts) has the
  expected reference shape and lies under exactly
  `generative-jobs/{job_id}/render-generations/{generation}/`;
- all required referenced objects and every present optional reference can be proved
  to exist; unavailable metadata is not success;
- an exact scalar terminal-context capsule for variant+generation+attempt+analysis
  view+detector version can be rehydrated from the private pending-context map.

A stage with a missing/legacy generation, fixed or editor-suffix key, absent object,
malformed reference, missing/malformed context, mismatched lock, or detector/view skew
is not resumable. Preserve any separately live public bytes until replacement wins,
reserve a fresh generation, and rerender; enqueue only provably unreferenced old
generation prefixes for cleanup. Required-v1 must never pass such a row straight to
`_finalize_job`. Before PR F adds durable context rehydration, PR D deliberately
rerenders every private staged required-v1 result; legacy-auto and non-speech resume
behavior remains unchanged. A retry that finds an already terminal, generation-
matched `published_*` outcome short-circuits the whole task idempotently rather than
re-finalizing the row.

`_attach_variant_posters` must pass an explicit generation-scoped destination into
`generate_and_upload_from_gcs`; its current `job-posters/{job_id}/{sha1}.poster.jpg`
default sits outside the generation prefix and is not covered by prefix recovery.
Extend the poster helper and job-storage allowlist with an optional exact destination,
retain the existing default for legacy callers, and require new initial speech
posters to live under the same render-generation prefix as their video. Poster helper
tests prove a hard kill cannot orphan a second lifecycle-exempt prefix.

Maintain an orchestrator-local `RenderStorageJournal` of every intended key created by
the attempt; register each key **before** its upload call, because a client exception
can occur after remote acceptance. It is not persisted or logged. On successful
render, the result contains the same `render_generation_id`, and the private stage
write accepts only when the row-locked pending entry still owns that generation and
any speech-cut claim still matches. On mismatch, do not mutate the row and reference-check/delete only the
losing journal's generation-owned keys. The same cleanup runs for partial upload,
render exception, finalization rejection, and failed speech-cut rollback. A new
attempt never calls `_discard_generation_storage(..., generation=None)`; tighten that
helper so missing-generation legacy cleanup cannot delete a currently referenced key
and unknown reference state deletes nothing.

Process-local journals do not survive hard kills, and this prefix is lifecycle-
exempt. Add a bounded
`assembly_plan["_speech_cleanup_internal"]["render_generation_cleanup_pending"]`
list. Reservation persists `{generation, prefix, upload_state="writing",
lease_expires_at}` before the first upload; the lease covers the Celery hard limit
plus storage-call grace. Core renderer return does **not** close it: poster attachment,
private-stage persistence helpers, and speech compose/reburn can still upload under
the generation. A `RequiredSpeechGenerationCoordinator` owns the receipt and keeps it
`writing` through the final possible poster/stage/compose storage call. Only the outer
coordinator, after normal completion or a caught failure guarantees no more calls will
start, updates that same field to `upload_state="closed"` immediately before terminal
publish/retirement. A hard kill leaves `writing` for lease recovery. A late losing
worker may close its exact receipt even after ownership moved, but cannot mutate any
variant/control state. The same
row-locked transaction that rotates/rejects/retires a generation retains/queues that
receipt before dropping ownership; the stale-job reaper does the same for abandoned
pending/rendering generations. Cap the list at 64; if immediate
reconciliation cannot make space, reject a new reservation with retryable
`generation_cleanup_backpressure` rather than lose a cleanup receipt. The stale-job
reaper follows the same rule: if the list remains full, it leaves generation ownership
and job/variant status unchanged, records bounded backpressure, and retries next
sweep—never terminalizes an attempt whose cleanup receipt cannot be persisted.

Add `reconcile_render_generation_cleanup(job_id)` and invoke it after terminal
commits, claim rotation, rollback, and from the existing bounded Beat storage sweep.
Before deleting a generation prefix, load fresh state and prove the token is absent
from every current variant, pending owner, `speech_cut_control`, single/list rollback
snapshot, and every artifact reference in
`_speech_cleanup_internal.staged_render_results`, **and** prove uploads are closed or the conservative lease has
expired (`upload_state == "closed"` is the shared normal-exit marker). This prevents
retire→empty-check→late-loser-upload races. Unknown,
unreadable, referenced, unexpired, or malformed staged state deletes nothing. After a successful
verified-empty deletion, row-lock and revalidate before removing the receipt;
failures retain it for the next sweep and log bounded token/count/error-class data
only. This gives partial uploads immediate journal cleanup and hard-killed uploads a
durable recovery path without a new table.

The precommitted entry is a reservation guard until publication, not permanent debt.
On successful terminal publication, the same locked transaction proves
`upload_state="closed"`, exact current-generation ownership, and prefix equality, then
removes that exact active receipt **without deleting the referenced prefix**. If a
published/closed receipt survives an older partial commit, the Beat reconciler may
perform the same exact-live proof and consume it without deletion. When that live
generation is later superseded or retired, its ownership-changing transaction creates
a fresh cleanup-debt receipt before removing the last reference. Thus the indexed
queue contains only writing/abandoned/retired work; normal successful generations do
not accumulate or permanently occupy the oldest page.

The same cleanup list supports a second bounded debt shape for pre-V2 fixed keys:
`{debt_id, kind="exact_keys", paths, upload_state="closed"}` with at most 32 allowlisted
job-owned paths. When a new generation replaces a legacy required-speech row, the
winning terminal transaction computes the old final/base/pre-media/pre-SFX/matte+
sidecar/camera/visual/motion paths plus deterministic poster/base-poster/pre-overlay-
poster keys that will lose their last reference, appends this exact-key debt, and
only then swaps the new variant live. If the cap cannot make room, publication remains
staged/last-good and retries rather than creating an untracked orphan. Reconciliation
freshly scans every media-bearing Job structure while excluding only the exact
canonical debt receipt's own `paths` field; otherwise the receipt would self-reference
forever. Before I/O, row-lock and coalesce duplicate exact-key receipts by path into
one deterministic canonical debt ID; an unparseable or concurrently changed duplicate
fails closed. Variants, stages, control, rollback snapshots, source paths, and any
non-coalesced duplicate remain reference-bearing. Delete only exact unreferenced keys,
verify each is absent, then repeat the same narrow self-exclusion/reference proof under
the final row lock before removing the receipt. Retain partial/unavailable debt. Do
not use `_discard_generation_storage(..., generation=None)` for retirement. This
closes legacy fixed-key→generation-prefix migration without risking current bytes.
Do not delegate ordinary deterministic posters to the existing backfill-poster
receipt, whose key predicate accepts only `.poster.backfill-<uuid>.jpg` objects.
Tests cover self-receipt progress, deterministic duplicate coalescing, a duplicate
mutation during deletion, and a live staged/rollback reference blocking deletion.

Do not leave this behavior in only the non-terminal stale-job path. Both
`reap_orphans` **and** the separate terminal-job `reconcile_stuck_variants` must call
one shared row-locked `terminalize_required_speech_generation(...)` before their
generic status repair. If an exact active `speech_cut_control`/claim exists, the
helper validates the operation, attempt, generation, private lock, and prior snapshot;
queues the provisional generation receipt before relinquishing ownership; restores
the exact `speech_cut_previous_variant(s)` and prior cleanup flag; clears control,
in-flight state, lock, and exact context capsule in the same commit; then reconciles
the retired prefix after commit. A cleanup-receipt cap, malformed/missing prior
snapshot, unknown references, or failed ownership proof leaves the row unchanged and
emits bounded recovery/backpressure status for the next sweep.

An absent `finalizer_claim` is not a permanent blocker: dispatch stores control before
the worker claims it, and retry release can clear the claim. Once the sweep has a no-
live-task candidate and a fresh `FOR UPDATE` row, exact control operation+generation,
matching private lock, and a valid prior snapshot are sufficient to restore an
**unclaimed** operation. A present claim must still satisfy its normal token/expiry
rules; a concurrent worker that starts after the sweep snapshot loses the same row-
locked control CAS and aborts. Missing capsule keeps the outcome unknown, not the
state stuck. Cover dispatch→task-never-started and claim-released→retry-lost cases.

For an ordinary abandoned initial required-v1 pending/rendering generation, the same
helper queues its owned prefix and transitions it to the existing explicit failed-job
path; it never infers ready/`ok=True` from a carried `video_path`. Generic non-speech
editor rerenders keep their current path-based repair behavior. If an exact durable
terminal capsule exists, the locked transition fail-open appends `failed_owned`
before restore/retirement; if the capsule is absent, the outcome stays unknown rather
than fabricated, but storage and state recovery remain generation-safe. The helper
must re-read with `FOR UPDATE` because the sweep query snapshot is only a candidate
list. Tests cover hard kill after job status becomes `variants_ready` but before
speech compose/publish, plus non-terminal abandonment, live-task exclusion, stale
query races, last-good/provisional byte sentinels, and cap-full no-mutation retry.

The existing `delete_prefix_best_effort()` count cannot prove success: zero means
either an empty prefix or a swallowed listing/deletion failure. Add a compatible
status-bearing storage primitive returning bounded
`verified_empty | partial | unavailable` plus listed/deleted/failed/remaining counts.
It lists, deletes exact returned objects, then re-lists; only a successful empty
re-list is `verified_empty`. Existing callers keep the old wrapper. Both source-copy
and render-generation **abandoned** receipts clear only on `verified_empty`;
partial/unknown results remain queued. The one exception is an `adopted_live` source-
copy receipt: after uploads close, an exact current-vector proof that every committed
durable path belongs to that attempt clears the receipt without deletion. Render-
generation cleanup-debt receipts have no adopted-live exception; the distinct active
reservation guard is consumed by the exact terminal/live proof described above. Add
GCS/local fakes for empty success, list
failure, fail-after-N delete, relist failure, and eventual retry success.

Fleet discovery must not full-scan JSONB every five minutes. Add migration `0092`
with a concurrent partial index on `jobs(updated_at, id)` whose literal predicate is
`jsonb_typeof(assembly_plan -> '_speech_cleanup_internal') = 'object'` and the private
container has either cleanup-list key. The bounded query must repeat that exact
literal predicate so PostgreSQL can use the index,
ordered by `updated_at, id`. Ordering alone is not fair when a retained failure stays
oldest. After every attempted receipt, row-lock the fresh Job, move that exact retained
receipt to its per-job list tail, `flag_modified`/commit so `updated_at` advances even
when it is the list's only item, and leave its bounded failure/backoff metadata on the
receipt. Remove each cleanup key entirely when its list becomes empty so the partial
index contains debt only. The next bounded page can then reach later rows while
persistent failures round-robin instead of monopolizing the page. Mirror the index in the Job
model and lock migration ancestry/concurrent SQL/query shape, empty-key removal, and
retained-row rotation in
`tests/test_content_plan_schema.py`; no new table is required.

`_restore_failed_speech_cut_rerender` restores the prior DB snapshot only after the
losing generation journal is isolated for cleanup. Because provisional keys are
unique, the restored paths still address the original bytes; cleanup cannot delete or
overwrite the winner/last-good generation. Tests use a fake object store with
different byte sentinels—not only path assertions—to prove both subtitled and
talking-head races preserve the last-good media and every provisional artifact.

### Cancellation and deletion ownership

Admin cancellation is another terminal owner transition. While holding the Job row
lock and before setting `status="cancelled"`, call the same required-speech
terminalizer. For a rerender it restores the exact prior variant(s)/cleanup flag; for
an initial generation it removes/scrubs only unpublished staged/public-pending
references. In both cases it converts the precommitted generation guard into cleanup
debt before clearing the exact lock/control/stage/capsule. A writing receipt remains
lease-gated. If debt capacity cannot be secured, cancellation still hides the Job
immediately but retains the private owner/reference state on the cancelled row; the
Beat reconciler is explicitly allowed to finish cancelled cleanup later and only then
clear it. Revoke and storage work remain after commit. With an exact capsule append
`cancelled_owned`; never label an explicit cancellation failed or published. Extend
the existing delayed `cleanup_cancelled_job` to dispatch this reconciler rather than
assuming the 24-hour lifecycle covers `generative-jobs/*`.

Extend cancel eligibility narrowly: keep the existing coarse cancellable statuses,
and additionally allow a terminal-looking status such as `variants_ready` **only**
when the locked row proves exact active speech control + generation lock/stage. A
normal terminal Job without that provisional proof remains 409. Route tests cover the
finalize→compose/publish gap; the admin UI may derive the same internal `cancel_allowed`
flag, but the backend predicate is authoritative.

`DELETE /me/jobs/{id}` must reject with stable 409 `job_render_not_quiescent` whenever
**any** variant is pending/rendering, or when the private generation lock, staged
result, active speech control, or writing receipt exists—and whenever any unreconciled
source/render generation or exact-key debt remains, even if its lock/control was
already cleared and the coarse Job status is `variants_ready`/cancelled. This covers
ordinary editor rerenders as well as speech. Once quiescent, always write a v2
`JobStorageDeletion` manifest with conservative job-prefix entries in addition to
currently known exact paths, even if the exact set is empty, then delete the Job in the
same transaction. Prefix verification is the backstop for an unexpected late writer;
the status guard is the normal fast rejection. This closes the speech
finalize→compose/publish and ordinary-rerender gaps without blocking deletion of
actually terminal jobs.

Whole-account erasure cannot wait for a render. Before deleting any Job rows, create
or merge one `JobStorageDeletion` outbox row per Job in the same DB transaction. Add a
backward-compatible v2 manifest shape to its existing JSONB payload with exact paths
plus allowlisted job-owned prefixes (including
`generative-jobs/{job_id}/`) and `not_before` equal to the latest active upload lease
or a conservative now+Celery-hard-limit+storage-grace deadline. Externalize every
private source/render/stage/exact-key receipt and its prefix/lease into that manifest
before the Job disappears. Capture task IDs and
best-effort revoke them after commit. `purge_job_storage` keeps legacy string manifests
compatible, but v2 prefix entries wait for quiescence, perform status-bearing list→
delete→relist, and remain pending until every prefix is verified empty. The existing
Beat outbox sweep recovers dispatch/storage failures after the Job/User rows are gone.
The one-pass user-prefix purge may remain as a fast path, but cannot complete or erase
the durable per-Job manifest. Test account delete→first purge→late upload→hard kill→
post-lease sweep→verified empty, plus outbox merge/idempotence and no foreign-key
dependency on the deleted Job/User.

### Privacy-safe analysis receipt

Before wiring the receipt, add a companion API in `clip_speech.py`:

```python
@dataclass(frozen=True)
class SilenceDetectionResult:
    spans: tuple[tuple[float, float], ...]
    status: Literal[
        "ok",
        "probe_failed",
        "invalid_duration",
        "no_audio",
        "ffmpeg_timeout",
        "ffmpeg_failed",
        "ffmpeg_nonzero",
        "parse_failed",
    ]
```

`detect_silences_with_status()` performs the one existing probe/FFmpeg pass and
returns this result. `detect_silences()` delegates to it and still returns only the
same list of spans, preserving every existing caller. `speech_coverage()` keeps its
current `0.0` failure semantics. An `ok` result with zero spans means a real,
successful no-marker outcome; every other status is a bounded diagnostic failure.
V2 acoustic detection runs only when status is `ok`. In apply mode, a non-`ok` result
selects the baseline plan for that clip, records the status, and never guesses that
the clip was merely noisy. No exception text enters the receipt.

Preserve both renderer pre-gates: a confirmed no-audio clip must never enter
`_silence_cut_analysis`, Whisper, or silencedetect. Emit a fixed-shape minimal receipt
with `candidate_status="precheck_no_audio"` and
`silence_detection_status="not_run"`; subtitled retains its existing benign no-op
render behavior. A talking-head outer media-probe/`SpineExtractionError` emits
`candidate_status="outer_media_probe_failed"` without ASR or FFmpeg. For
`required_v1`, `_render_one_spec` converts it to typed
`SpeechCleanupFailure(reason="outer_media_probe_failed")`: initial generation fails,
speech-cut rerender restores last-good, and **no montage may publish as fulfillment of
required cleanup**. Preserve today's montage degradation only for `legacy_auto`.
This is distinct from `silence_detection_status="probe_failed"`, which means the
outer media gate passed and the inner silencedetect probe failed after ASR; only that
inner failure falls back to a valid baseline. The companion's `no_audio` result
remains defensive for direct callers but is unreachable in a correctly gated
production path.

Add `record_speech_cleanup_detection()` beside `record_render_stage()` in
`app/services/pipeline_trace.py`. Emit exactly one analysis event per analysis-cache
entry **per render attempt**; the in-attempt `_SilenceCutCache` enforces that scope.
The same source may legitimately have `full_clip` and `talking_head_spine_capped`
analysis views, so include that bounded `analysis_view` rather than deduplicating
different audio inputs. Retries and rerenders intentionally append new attempts. Pass
the existing opaque
`render_trace_id` through as `analysis_attempt_id`; never synthesize identity from
timestamps. The following JSON is an illustrative schema shape, not a reconstruction
of the historical job's unpersisted ASR input:

```json
{
  "stage": "silence_cut",
  "event": "silence_cut_mixed_gap_analysis",
  "data": {
    "schema_version": 1,
    "detector_version": "mixed-gap-v1",
    "analysis_attempt_id": "opaque-render-trace-id",
    "analysis_view": "full_clip",
    "source_slot": 0,
    "assignment_status": "assigned",
    "source_tag": "16-hex",
    "analysis_policy": "required_v1",
    "configured_mode": "shadow",
    "effective_mode": "shadow",
    "candidate_status": "ready",
    "rollout_percent": 0,
    "rollout_bucket": 37,
    "duration_ms": 10000,
    "thresholds_ms": {
      "silence_min": 100,
      "island_min": 150,
      "island_max": 1200,
      "flank_silence_min": 100,
      "min_cut": 180
    },
    "inputs": {
      "silence_detection_status": "ok",
      "asr_word_count": 6,
      "asr_word_spans_ms": [[2040, 2530], [3255, 4359]],
      "asr_word_spans_omitted": 4,
      "silence_spans_total": 7,
      "silence_spans_ms": [[0, 1215], [1758, 1875]],
      "silence_spans_omitted": 5,
      "lexical_candidate_spans_ms": [[9060, 10000]],
      "lexical_candidates_omitted": 0
    },
    "mixed_gap_scan": {
      "word_windows_total": 7,
      "islands_total": 3,
      "eligible_total": 2,
      "records": [
        {
          "window_start_ms": 6200,
          "window_end_ms": 8400,
          "island_start_ms": 7406,
          "island_end_ms": 7978,
          "left_silence_ms": 1196,
          "right_silence_ms": 316,
          "detection": "eligible",
          "reason": "two_sided",
          "plan_disposition": "selected_full"
        }
      ],
      "records_omitted": 2
    },
    "baseline_plan": {
      "removed_count": 3,
      "removed_ms": 5499,
      "removed_spans_ms": [[0, 2040], [4656, 7175], [9060, 10000]],
      "removed_spans_omitted": 0,
      "clamped": true,
      "bailout_reason": null
    },
    "candidate_plan": {
      "removed_count": 4,
      "removed_ms": 5499,
      "removed_spans_ms": [[0, 2040], [4656, 6603], [7406, 7978], [9060, 10000]],
      "removed_spans_omitted": 0,
      "clamped": true,
      "bailout_reason": null,
      "mixed_gap_full": 2,
      "mixed_gap_partial": 0,
      "mixed_gap_dropped": 0
    },
    "selected_plan": "baseline"
  }
}
```

When assignment validation fails, `assignment_status` is `missing_source_instance`,
`cardinality_mismatch`, `invalid_source_instance`, `duplicate_source_instance`, or
`unmapped_clip_id`, `ambiguous_clip_id`, or `identity_cache_unavailable`. The receipt serializer and schema use the
same exhaustive enum as `SpeechCleanupAssignment`; they must not collapse ambiguous
duplicate matcher IDs into a generic/missing status.
`source_slot` is included only when unambiguous; `source_tag` and
`rollout_bucket` are `null`, configured apply becomes effective shadow, and the
receipt is still emitted. Configured off still emits nothing. `candidate_status` is a
bounded enum and never carries an exception message.

Allowed data is timing, counts, booleans, bounded enums, and opaque tags only. Exclude
word text, language, confidence, prompts, filenames, paths, URLs, notes, raw
fingerprints, audio features, media hashes, and exception messages.

Hard caps:

- 128 ASR word spans
- 128 silence spans
- 32 lexical candidates
- 32 mixed-island records
- 64 atomic-disposition records
- 100 baseline and 100 candidate removed spans (the existing `MAX_REMOVALS` ceiling)
- integer milliseconds
- 16 KiB encoded `data` maximum
- deterministic chronological truncation with `*_omitted` counters

If still oversized, drop rejected-decision tails, then ASR/silence input tails, then
eligible-decision tails; preserve both final-plan span arrays whenever possible. If a
plan array must be truncated, set its omitted counter and make the UI label the band
partial rather than visually complete. Summary scalars are never dropped.

The specialized recorder must not delegate failure handling to the generic void
sink. It returns one bounded status: `persisted`, `dropped_no_context`,
`dropped_invalid_job`, `dropped_job_missing`, `dropped_cancelled`, `dropped_cap`, or
`error`. A short row-locking
transaction checks the job/cap, appends the already-validated payload, and reports the
actual branch. On exception, log only `error_class=type(exc).__name__`; never log
`str(exc)`, SQL parameters, or serialized receipt data. Rendering remains fail-open.
Emit one separate scalar structured log containing attempt/source tags, bounded
statuses, and counts only—never interval arrays or content.

Analysis receipt `selected_plan` records selection intent, not publication success.
Do not reuse the talking-head assembler's existing `silence_cut_plan` event: it fires
before composite FFmpeg, text burn, upload, and the caller's generation-safe DB
upsert and before `_finalize_job`. Renderers therefore emit no authoritative outcome.
The Celery task wrapper owns a thread-safe `SpeechCleanupTerminalAccumulator` and
passes it into `_run_generative_job`; `_render_one_spec` creates a per-spec typed
context sink within it before entering the renderer and passes the sink through both
renderer signatures. The sink starts with attempt/generation/view/detector/assignment
identity, then analysis fills selection, candidate status, and output-removal scalars.
Immediately before `stage_required_speech_generation`, freeze the content-safe
snapshot and pass it as an explicit non-result argument so that a rejecting row-lock
can write its terminal discarded outcome. Store/dedupe that same snapshot in the accumulator; in
exception paths, `_render_one_spec`'s `finally` freezes and stores it instead. The
multimap is keyed by
`(variant_id, render_generation_id, analysis_view, detector_version)`. It never
enters the result, variant JSON, or exception message, and remains available to the
outer fail/rollback handler if `_run_generative_job` raises.

Task-local state alone is insufficient across a private-stage→worker-crash→Celery
retry. Persist one **scalar terminal-context capsule** in the same row-locked
transaction as the staged render result, under
`assembly_plan["_speech_cleanup_internal"]["terminal_pending"]`. The key includes variant,
generation, attempt, analysis view, and detector version. The allowlisted value is at
most 2 KiB and contains only the scalar fields needed by the terminal event: opaque
IDs/tags, selected plan, candidate status, removal count/milliseconds, and bounded
failure phase/class. It contains no interval arrays, transcript, path, URL, media
metadata, or exception message and is not the analysis receipt. Cap the map at 64
entries. If its exact capsule cannot be committed, the current task may finish using
its accumulator, but a later retry must classify that ready row non-resumable and
rerender under a new generation rather than finalize without correlation.

On a valid retry, hydrate the accumulator from the exact capsule before adding the
ready result to `results_by_rank`. The same row-locked terminal transaction that
appends (or fail-open skips) the final outcome removes only that exact capsule while
clearing the matching write lock. A commit failure preserves state, lock, and capsule
together. Supersession/rollback/failure removes the old exact capsule only after its
bounded terminal disposition is decided. Add the private map to every public-response
negative allowlist; the owner and plan-item serializers must never expose it. Thus
normal execution uses the in-memory channel, crash recovery has durable correlation,
and observability failure still cannot block publication.

This caller-owned channel is mandatory for exception paths. A
`SpineExtractionError` before analysis snapshots `analysis_not_started`; one after
analysis retains the bounded selected-plan/removal state. `_render_one_spec` marks the
snapshot failed before re-raising. Required-v1 converts it to strict
`SpeechCleanupFailure` and carries the accumulator to initial fail or speech-rerender
rollback; only `legacy_auto` enters montage degradation. Thus the abandoned
talking-head generation can receive exactly one terminal failure and never a
publication outcome.

The private staged-result write is crash-recovery persistence, not publication. It
may reject and delete the upload, and an accepted stage can still be rejected or
superseded by `_finalize_job`'s generation merge/status write. Refactor finalization to
return a typed per-generation decision while preserving a compatibility bool wrapper:

```python
@dataclass(frozen=True)
class VariantFinalizationDecision:
    variant_id: str
    render_generation_id: str | None
    state: Literal["live", "superseded", "failed"]

@dataclass(frozen=True)
class JobFinalizationResult:
    status: Literal[
        "accepted",
        "job_missing",
        "job_cancelled",
        "owner_rejected",
        "claim_superseded",
        "failed",
    ]
    variants: tuple[VariantFinalizationDecision, ...]
```

The decisions are computed from the row-locked post-merge generation IDs, not from
the stale `results` list. For a speech-cut operation, they remain provisional until
`_compose_speech_cut_rerender` and `_publish_speech_cut_rerender` succeed. Unexpected
finalization/publish exceptions become `failed`; the existing typed claim mismatch is
`claim_superseded`. Generation-owned media that loses finalization is discarded only
with the existing ownership checks.

Make `_publish_speech_cut_rerender` return a typed committed-publish result containing
the exact winning variant generation(s); replace its generic claim-mismatch
`RuntimeError` with a dedicated internal supersession exception. Existing callers may
ignore the successful return value, but the terminal coordinator must consume it.
This creates one unambiguous boundary across compose, claim validation, rollback
clear, poster reconciliation, and publish, locked by
`tests/tasks/test_speech_cut_rerender.py`.

Terminal outcome recording is part of the row-locked state transition that proves
the outcome, not a later current-generation lookup. Add a locked, allowlisted helper
used by staged-write rejection, normal `_finalize_job`, speech-cut publish,
`_restore_failed_speech_cut_rerender`, `_fail_job`, admin cancellation, and the shared required-speech
terminalizer used by both `reap_orphans` and `reconcile_stuck_variants`. It appends scalar
`speech_cleanup_render_outcome` data to the same Job mutation before commit. Its
deterministic `outcome_id` hashes only attempt, variant, generation, analysis view,
and detector version and suppresses duplicates.

Build and validate the bounded terminal payload before entering the state transition.
The locked append helper is non-throwing and returns only `persisted`, `dropped_cap`,
or `error`; malformed/capped/helper failures skip the event but never skip or roll
back publish, last-good restore, or `_fail_job`. A database commit failure retains the
existing state-transition semantics. Injected helper failures in all three terminal
transactions prove observability is fail-open.

For `published_*`, the locked pending/public owner and private staged result must both
contain the exact generation being atomically published. For failure/rollback, the
transaction must first prove the context matches the active pending generation or
speech-cut control plus its claim when one exists (the no-live reaper may use the
documented exact unclaimed-control proof), append `failed_owned`, and
only then clear/restore that ownership in the same commit. If ownership already moved,
append `discarded_superseded` when the locked state still permits the write; never
mislabel it failed. Missing/cancelled/unwritable Job state produces a bounded scalar
persistence result/log but no fabricated trace event. Terminal outcomes are:

- `published_applied`, `published_no_change`, or `published_baseline_fallback` only
  when the exact generation is live after the terminal publish boundary;
- `discarded_superseded` when an initial upsert/final merge/claim loses to a newer
  generation or operation;
- `discarded_finalization_rejected` for a rejected terminal transition when the Job
  still permits the correlated trace write;
- `failed_owned` for render/upload/finalization/publish failures, including a re-raised
  `SpineExtractionError`, with a bounded failure phase and exception class.
- `cancelled_owned` only when the pre-cancel row lock proves exact generation/control
  ownership and queues its cleanup before clearing/hiding state; retries dedupe by the
  same outcome ID. It is neither failure nor publication and is excluded from
  treatment-success denominators.

The event contains outcome ID, attempt ID, variant ID, render generation ID,
`analysis_view`, `detector_version`, nullable source tag, selected plan, candidate
status, output-removal count/milliseconds, and bounded failure phase/class—no arrays
or messages. Missing outcome means unknown,
never success. The assembler's old event and private stage remain intermediate
diagnostics. The admin UI may label a plan **currently live** only when a
`published_*` event's generation equals the variant generation in the same row-locked
debug snapshot; a later editor generation automatically makes the old outcome
historical.

Fleet aggregation deduplicates by `outcome_id`. Assigned sources choose the latest
attempt per `(job_id, source_tag, analysis_view, detector_version)` and count terminal
per-generation outcomes rather than raw analysis/intermediate events. Unassigned
receipts use a separate non-treatment key:
`(job_id, analysis_attempt_id, source_slot)` when the slot is safely known, otherwise
`(job_id, analysis_attempt_id, receipt_event_ordinal)` from the immutable trace
snapshot. They are counted in assignment-failure metrics only and excluded from
treatment denominators; nullable tags never collapse multiple sources into one row.

`pipeline_trace` is present only on the admin debug response. Add a route regression
that seeds the receipt and proves it is absent from the owner status response. Do not
copy the receipt into `assembly_plan`, `variants`, `silence_cut`, `nova_steps`, or an
agent run. The private scalar terminal capsule above is not a receipt and must also be
absent from every public serializer.

That privacy boundary must cover account export, which currently returns
`Job.assembly_plan` wholesale. Add one side-effect-free
`project_public_assembly_plan(...)` helper and use it at every owner/plan-item/creator/
export serialization boundary. It deep-copies before projection, removes the entire
reserved top-level `_speech_cleanup_internal` container, and strips
`clip_source_instance_ids` plus versioned source-identity/cache bookkeeping from any
nested candidate snapshot. All new private state is required to live under the
reserved container so future fields inherit this boundary; do not scatter another
denylisted top-level key. Admin debug receives the purpose-built timing receipt from
`pipeline_trace`, not these control structures. Seed every private key and verify it
is absent from generative status, plan-item responses, creator responses, and
`GET /me/export`, while normal user-authored assembly fields remain present and the
stored ORM JSON is unchanged.

### Admin visualization and creator copy

Add a job-level `SpeechCleanupDiagnostics` component to `/admin/jobs/{id}`. It parses
`silence_cut_mixed_gap_analysis` events, groups attempts by
`source_tag + analysis_view` (or `analysis_attempt_id + source_slot/event ordinal`
when unassigned),
and defaults to the newest attempt while retaining an attempt selector. It renders
one source-timeline card per selected attempt:

```text
ASR words       ▓▓▓   ▓▓▓▓                       timing only
FFmpeg silence  ███ █   ███████ ██████  ██       blue
V2 islands              △        ▲               rejected / eligible
baseline cuts   ███       █████                final current behavior
candidate cuts  ███       ███▲██              shadow/apply proposal
```

Required labels/tooltips: exact milliseconds, detector version, configured/effective
mode, assignment status, nullable bucket, candidate status, rejection/disposition
reason, baseline/candidate totals, selection intent, correlated render outcome, clamp
state, and truncation warning. Baseline and candidate bands read their own bounded
`removed_spans_ms`; neither may be reconstructed from detector islands. Color must
not be the only channel. Invalid/version-skewed events must be ignored without
breaking the page. Reuse the existing admin API and final `SilenceCutStrip`; do not
create an endpoint.

Change creator `no_change` copy from the unsupported claim
“no filler sounds needed removal” to:

> Speech cleanup ran; no cuts were made.

A zero-removal plan proves only that no safe executable cut was selected.

## Code-quality review findings

1. **[P1] (confidence 10/10)** `silence_cut.py:447-452` — whole-gap rejection is
   the demonstrated production blind spot. Use one pure complement helper; do not
   duplicate interval logic in the task layer.
2. **[P1] (confidence 10/10)** `silence_cut.py:545-569,656-794` — merge discards
   component reasons before symmetric clamp trimming. Preserve raw filler atoms in a
   separate argument; do not turn them into budget-exempt forced spans.
3. **[P2] (confidence 9/10)** `pipeline_trace.py:78-105` — the generic sink accepts
   arbitrary JSON and caps events, not bytes. Use a specialized allowlisted and
   byte-capped recorder.
4. **[P2] (confidence 9/10)** `generative_build.py:13991-14008` — cache identity
   currently includes policy/forced digest but not the new algorithm/mode. Add the
   detector identity before any V2 result can be cached.
5. **[P1] (confidence 10/10)** `generative_build.py:15044-15052` and
   `talking_head_assembler.py:556-559` — both paths currently pass matcher IDs as the
   source fingerprint; `_clip_id_for` may use mutable Gemini names or the shared
   fallback `clip_0`, storage keys may change after durable copying, and Omni can reuse
   numeric slots. Persist never-reused source-instance IDs, maintain them with every
   path mutation, and derive rollout identity from job UUID + instance ID.
6. **[P1] (confidence 10/10)** `clip_speech.py:44-58,68-70` — `[]` means both a
   successful scan with zero silence and any probe/audio/FFmpeg/parse failure. Add a
   typed result without changing the legacy wrapper; V2 must never classify failure
   as a calibration result.
7. **[P1] (confidence 10/10)** `generative_build.py:13860-13989` — one broad
   exception boundary can turn a V2-only bug into `failed=True` and abort strict
   required-v1 callers. Build baseline first, isolate candidate/receipt construction,
   and runtime-validate V2 before selection.
8. **[P1] (confidence 10/10)** `silence_cut.py:545-568,862-873` — minimum-cut
   filtering and largest-duration removal capping happen before a provenance-aware
   allocator could preserve short atoms. V2 must apply both rails after tagged
   component construction; leave baseline ordering untouched.
9. **[P1] (confidence 10/10)** `pipeline_trace.py:78-153` and
   `SilenceCutStrip.tsx:4-15,96-118` — the generic sink has no persistence result and
   can log exception strings, while the promised UI needs concrete plan intervals.
   Use the specialized content-safe recorder and bounded baseline/candidate span
   arrays with attempt/outcome correlation.
10. **[P1] (confidence 9/10)** `generative_build.py:1692-1701,13930-13943` — the
    master engine and clamp kill switches precede plan construction. Encode their
    precedence explicitly and include every influencing value in cache/test identity;
    mixed mode must never bypass an existing rollback contract.
11. **[P1] (confidence 10/10)** `generative_build.py:3475-3491` and
    `_omni.py:374,477` — durable copying reads before its final row lock while Omni
    can remove/append sources concurrently. CAS the exact captured path+instance-ID
    vector, use instance-keyed destinations, and retry once rather than overwriting a
    newer source list.
12. **[P1] (confidence 10/10)** `silence_cut.py:545-873` — allocator branches can
    explain a selected or dropped atom only through reconstructed span overlap. Emit
    one exhaustive typed `AtomicDisposition` per atom after all hygiene/count/micro-
    gap decisions; receipt and UI consume it directly.
13. **[P1] (confidence 10/10)** `generative_build.py:2409-2433,2631-2638,
    18892-19308` — rendering and upload finish before crash-recovery persistence, and
    staged entries can still be rejected, deleted, or superseded during terminal
    finalization/publish. Recheck the exact generation under the terminal recorder's
    row lock and distinguish published, superseded, rejected, and failed results.
14. **[P1] (confidence 10/10)** `template_orchestrate.py:2637-2651` and
    `generative_build.py:112,1199-1234,3335-3382` — parallel metadata results are
    stored in completion order but cache hits bind them to source paths by list
    position. Add indexed analysis records, metadata cache v2 identity validation,
    and sparse partial-failure coverage before using cache hits for rollout mapping.
15. **[P1] (confidence 10/10)** `generative_build.py:2539-2574,9995-10060,
    13295-13370,15591-15980,18765-18865` — initial subtitled/talking-head and
    speech-cut recomposition can write fixed keys or mint discontinuous generations;
    the immediate upsert is neither private nor an expected-generation CAS. Reserve one winning
    generation, scope every artifact key/journal to it, privately stage the result,
    and fence every mark/stage/
    compose/publish write by generation+claim.
16. **[P1] (confidence 10/10)** `storage.py:484-524` and lifecycle-exempt
    `generative-jobs/*` — local cleanup cannot survive a killed process, and the
    prefix-deletion count conflates empty success with total failure. Precommit
    bounded source/render attempt receipts, add verified-empty deletion status, and
    reconcile through immediate and Beat paths before clearing receipts.
17. **[P1] (confidence 10/10)** `talking_head_assembler.py:536-540,730-741` and
    `generative_build.py:13385-13386,2580-2617` — a talking-head spine probe failure
    always degrades to montage, even under required-v1. Convert it to typed strict
    cleanup failure/last-good restoration for required-v1; retain degradation only
    for legacy-auto.
18. **[P1] (confidence 10/10)** `generative_jobs.py:1960-1973,7299`,
    `plan_items.py:5280-5366,5551`, `creator_agent.py:2077-2147,2296-2306`, and
    `generative_build.py:2536-2577,9995-10050` — pending/private-stage required
    speech rows can accept autosave, deliberately unguarded editor commits, or creator
    craft before the renderer's whole-row upsert/finalization. Persist a private
    generation write lock and route every writer through one fail-closed predicate;
    only the exact terminal owner may clear it.
19. **[P1] (confidence 10/10)** `generative_build.py:2206-2219,2425-2492,
    2631-2638` — resume eligibility is only ready-looking media+track, while terminal
    correlation is task-local. Persist the bounded scalar capsule with the private
    stage and require exact lock/generation/prefix/object/context proof; otherwise
    rotate and rerender rather than finalize legacy or uncorrelated bytes.
20. **[P1] (confidence 10/10)** `reaper.py:149-193,205-285` and
    `generative_build.py:19346-19350` — `reconcile_stuck_variants` promotes any stuck
    row carrying `video_path` even though speech compose/publish may still be pending
    after job-level finalization. Route both reapers through one row-locked required-
    speech terminalizer that restores exact last-good state and queues the owned
    generation before clearing control.
21. **[P2] (confidence 10/10)** the assignment schema includes
    `ambiguous_clip_id`, but a receipt allowlist that omits it would make duplicate
    matcher IDs hit an untyped branch. Reuse one exhaustive status enum in assignment,
    receipt serialization, UI parsing, and direct-call tests.
22. **[P1] (confidence 10/10)** bounded cleanup discovery plus a referenced active
    generation receipt can leave every successful job permanently indexed and starve
    later debt behind the sweep page size. Consume the closed, exactly live
    reservation receipt in the terminal transaction without deleting media; recreate
    a cleanup receipt only when that generation is retired.
23. **[P1] (confidence 10/10)** `generative_build.py:1899-1910` invokes durable copy
    before archetype/mode selection, so copy cannot assume rollout-gated source IDs.
    Provision internal identity for every timeline copy attempt and return typed
    `identity_unavailable` before remote I/O when existing identity is malformed.
24. **[P1] (confidence 10/10)** a bounded `ORDER BY updated_at, id` cleanup query is
    not fair if unavailable/partial receipts retain an old timestamp forever. Rotate
    every attempted retained row by committing the receipt state/timestamp and remove
    empty keys so later deletable debt cannot starve.
25. **[P1] (confidence 10/10)** `routes/me.py:2388-2412` exports
    `job.assembly_plan` wholesale. Route account export and every owner response
    through one copy-before-redact projection that removes the reserved cleanup
    namespace and source-identity/cache internals.
26. **[P1] (confidence 10/10)** `admin_jobs.py:802-941` cancels the row before any
    required-speech ownership retirement, while legacy cleanup does not cover the new
    lifecycle-exempt generation prefix. Terminalize/queue exact ownership under the
    cancel row lock, then revoke and reconcile after commit with a cancellation-only
    outcome.
27. **[P1] (confidence 10/10)** `routes/me.py:1638-1732,2609-2651` can delete the
    only Job/receipt while a provisional or late upload is possible. Reject individual
    deletion until every private debt is quiescent; account erasure externalizes
    prefix+lease state into the existing durable `JobStorageDeletion` outbox first.
28. **[P1] (confidence 10/10)** required render output is currently written into the
    public variant before compose/publish. Persist it only under the reserved private
    stage and atomically swap it live at the terminal generation transaction.
29. **[P1] (confidence 10/10)** legacy fixed media and deterministic poster keys do
    not belong to a generation prefix and current retirement is process-local best
    effort. Append bounded exact-key debt before a winning replacement drops their
    last reference, then reference-check and verify absence through Beat.
30. **[P1] (confidence 10/10)** global metadata-cache v1 invalidation would trigger
    Gemini and change legacy/off rerenders. Preserve v1 reads and populate a parallel
    indexed identity envelope only on genuine analysis; old unindexed rows stay out of
    apply without a forced model call.
31. **[P2] (confidence 10/10)** nullable source tags collapse multiple unassigned
    receipts in fleet aggregation. Use attempt+safe-slot/event-ordinal identity for
    assignment-failure metrics and keep them outside treatment denominators.
32. **[P1] (confidence 10/10)** status polling performs a lazy preview-stamp DB write,
    while generation cleanup ignores private staged artifact references. Defer the
    hidden write for locked variants and include every staged artifact in fail-closed
    cleanup reference proofs.

The current ASCII module diagram, plans/010 whole-gap description, P2 TODO, env docs,
and creator no-change copy must be updated in the same delivery train so behavior and
operator expectations cannot drift.

## Test review

Authoritative frameworks: backend `pytest`/`pytest-asyncio`; frontend Jest. Current
baseline verification on this worktree:

```text
python3 -m pytest tests/pipeline/test_silence_cut.py \
  tests/pipeline/test_silence_cut_golden.py -q
175 passed in 0.08s
```

No prompt or `render_prompt()` file changes; agent evals are not required.

### Coverage diagram

```text
CODE PATHS                                             USER / OPERATOR FLOWS
[+] _silence_cut_analysis                              [+] Creator enables cleanup
 ├─ [★★★ existing] one ASR + one silence scan            ├─ [★★ existing] applied/no-change/failure UI
 ├─ [GAP] typed silence-scan outcome, no extra pass       ├─ [GAP] truthful tool-success/failure receipt
 ├─ [GAP] off -> baseline only                           ├─ [GAP] incident filler fully removed
 ├─ [GAP] shadow -> compare, baseline live               ├─ [GAP] no-change copy makes no detection claim
 ├─ [GAP] apply + bucket in -> V2 live                   └─ [GAP] rerender agrees across sibling variants
 ├─ [GAP] candidate failure/invalid -> baseline
 └─ [GAP] missing assignment -> shadow

[+] mixed-gap detector                                 [+] Admin investigates a job
 ├─ [★★★ existing] wholly soundful short gap             ├─ [★★ existing] final cut strip
 ├─ [GAP] long mixed gap -> internal islands             ├─ [★ existing] raw trace JSON
 ├─ [GAP] two-sided silence / clip+word boundaries       ├─ [GAP] layered ASR/tool/candidate/live timeline
 ├─ [GAP] min/max/zero-silence/rejected reasons          └─ [GAP] truncation/version-skew states
 └─ [GAP] normalized-overlap/permutation invariants

[+] required-v1 clamp                                 [+] Rollout / rollback
 ├─ [★★★ existing] slack, output floor, edge priority    ├─ [GAP] off -> shadow -> 5/25/50/100
 ├─ [★★★ existing] forced/manual hard protection         ├─ [GAP] stable source assignment
 ├─ [GAP] atomic filler priority + whole-or-none          ├─ [GAP] metrics and manual listening gate
 ├─ [GAP] protected/atomic overlap closure                ├─ [GAP] ephemeral source-window audition
 ├─ [GAP] budget cannot fit atom -> drop whole            └─ [GAP] shadow rollback preserves evidence
 └─ [GAP] every-budget property/fuzz invariants

[+] diagnostic receipt
 ├─ [GAP] allowlist + caps + deterministic truncation
 ├─ [GAP] per-attempt selection + terminal publication truth
 ├─ [GAP] explicit content-safe persistence outcomes
 └─ [GAP] admin present / public absent

Legend: ★★★ behavior + edge/error coverage | ★★ happy path | ★ smoke
```

### Critical regression fixtures

Create `tests/pipeline/test_silence_cut_mixed_gap_golden.py` with privacy-safe
fabricated words and the exact source geometry:

```python
DURATION_S = 10.0
SILENCES = [
    (0.000000, 1.214694),
    (1.757506, 1.875034),
    (2.529025, 3.255057),
    (4.358934, 5.778866),
    (6.209660, 7.406100),
    (7.977846, 8.293356),
    (9.677755, 10.000000),
]
EXPECTED_ACOUSTIC_ISLANDS = [
    (5.778866, 6.209660),
    (7.406100, 7.977846),
]
```

Do not commit the user video, waveform, transcript, filename, job/plan UUID, or URL.

The fixture must prove:

- raw V2 acoustic output equals both intervals and uses `filler_acoustic`;
- the primary reported island is fully covered in the final clamped candidate plan;
- both atoms are whole-or-none and no final boundary lies inside either;
- total removal is at most `5.499s` and output is at least `3s`;
- keep/remove exactly partitions `[0,10]`;
- fabricated real words remain wholly kept;
- remapped words are monotonic and preserve durations;
- bailout/default behavior stays legacy; explicit V2 clamp produces a plan;
- the July approved golden remains byte-identical.

### Complete test matrix

**Pure interval/detector**

- Strictly internal two-sided components returned; one-sided/edge components rejected.
- Unsorted, overlapping, touching, duplicate, invalid, out-of-window, fully silent,
  and empty spans are deterministic and never create negative/zero islands.
- Multiple islands in one long gap are independently classified and time ordered.
- Exact 0.15/1.2 boundaries, below/above thresholds, and `MIN_CUT_S` hygiene.
- Clip-edge windows accept only internal bilateral islands.
- No silence markers disables all acoustic V2 candidates but not lexical fillers.
- A successful `ok` result with no silence markers is distinct from probe, no-audio,
  timeout, nonzero-exit, and parse failures; all failure statuses fall back to the
  baseline plan in apply mode without a second FFmpeg pass.
- Renderer no-audio prechecks synthesize minimal diagnostics and make zero ASR/FFmpeg
  calls; talking-head outer probe failure stays typed/strict, while an inner
  silencedetect probe failure after a successful outer gate falls back to baseline.
- Legacy wholly-soundful gap retains exact `PAD_ACOUSTIC_S` behavior.

**Clamp and invariants**

- Incident carrier and budget retain both acoustic atoms without partial overlap.
- Edge cuts rank before atoms; atoms rank before ordinary interior silence.
- Atom too large for remaining budget is dropped whole, never partially removed.
- A dropped atom embedded inside an otherwise selected merged silence carrier is
  carved out before flexible trimming and has exactly zero final overlap for every
  budget; selected groups alone are re-added through tagged decisions.
- A forced span strictly inside a filler promotes the full transitive component to a
  protected closure; filler/retake overlap is charged once with filler priority.
- A protected/manual span over a normal ASR word (the accepted-retake rerender path)
  passes runtime validation only for that exact protected closure; unrelated word
  intrusion still fails closed.
- Atomic groups are never budget-exempt unless promoted by protected overlap;
  non-protected removal stays within budget and protected overage is explicit.
- Seeded property sweep across zero to full budget asserts partition, output floor,
  remap monotonicity, determinism, and whole-or-none atoms.
- A 160 ms island adjacent to selected silence either forms a final cut at least
  `MIN_CUT_S` or drops whole.
- More than `MAX_REMOVALS` long silences cannot evict a short priority atom; protected
  overflow bails and all other over-cap groups drop in allocator priority.
- A micro keep-gap between two budgeted atoms evicts one whole by priority/tiebreak;
  the same gap between protected closures is absorbed as protected or produces a
  safety bailout (including a word-bearing bridge), never a partial group.
- Every atom has exactly one final typed disposition; connected members agree on
  group bounds/state, provisional selections are overwritten after eviction, and
  budget/count/min-cut/micro-gap/protected/bailout fixtures exercise every enum value.

**Integration and rollout**

- Subtitled required-v1 and talking-head spine both use the same V2 plan; b-roll is
  unchanged.
- Shadow runs one ASR and one silencedetect call, renders baseline, and records both
  plan summaries.
- Candidate builder, runtime validator, and receipt-builder exceptions after baseline
  success preserve `failed=False`, select baseline, and emit bounded status only.
- Off/default, `legacy_auto`, and `off_v1` remain byte-identical.
- Apply assignment is stable across retries/rerenders/reorders/sibling variants;
  missing, duplicate, or cardinality-invalid source-instance identity becomes shadow;
  0 and 100 percent boundaries are exact.
- Canary identity comes from job UUID + persisted never-reused source-instance ID,
  not matcher ID, numeric slot, or pre/post-durable storage key: changed Gemini names
  and copy failure→success remain in one bucket, replacement media never inherits the
  removed clip's bucket, and unrelated `clip_0` jobs do not collide.
- Legacy source-ID backfill commits before eligibility; reorder/durable-copy preserves
  IDs, remove+append creates a new ID/tag at the reused slot, duplicate/cardinality
  corruption forces shadow, and historical audition never resolves by slot.
- Timeline durable copy provisions wholly absent IDs under the same owner/vector
  fences for mixed `off`, `legacy_auto`, and `off_v1` as well as required-v1, without
  computing rollout or emitting V2 evidence. Present malformed identity returns
  `identity_unavailable`, performs zero copies, and continues only from readable
  original paths; no contract ever uses numeric-slot destination keys.
- `_render_subtitled_variant` and `_render_talking_head_variant` receive the explicit
  `speech_cleanup_assignment_by_clip_id: Mapping[str, SpeechCleanupAssignment]`;
  direct-call fixtures cover assigned and every unassigned envelope status.
- A durable-copy interleaving that removes/appends a source between copy and final
  lock returns `stale_source_vector`, never overwrites the newer path+ID vector,
  cleans only unreferenced attempt-created keys, and succeeds from a freshly reloaded
  vector on its single retry. A second interleaving returns retryable
  `source_list_changed` and renders no stale media.
- Durable-copy tests also cover fail-after-N partial copy, Job missing, cancellation,
  content-owner/epoch rejection, the already-durable fast path, commit exception, and
  cleanup-read/delete failures. Every non-accepted branch deletes exactly the
  unreferenced attempt-owned keys (or deletes nothing when references are unknown),
  and terminal/owner rejection never retries.
- A hard kill after source copy but before CAS leaves a precommitted attempt-prefix
  receipt; retry/Beat verifies no live source reference, deletes the prefix, and clears
  the receipt only after an empty relist. A successful-CAS/receipt-removal crash keeps
  the referenced prefix and clears only the stale receipt.
- Forced out-of-order clip-analysis completion followed by a cache hit preserves the
  exact slot+instance+clip mapping. A partial success with an earlier failed slot does
  not shift later metadata; malformed/duplicate v2 records force the rendered clip
  unassigned/shadow. A populated legacy v1 cache under `legacy_auto`, `off_v1`, or
  mixed `off` keeps its exact hit/ordering with Gemini forbidden; shadow/apply with no
  parallel v2 identity uses baseline+`identity_cache_unavailable` and also makes no
  model call.
- Two non-resumed initial workers starting from the same prior generation cannot both
  reserve/mark/publish. Subtitled and talking-head tests assert every created final,
  base, poster, pre-media, pre-SFX, matte/sidecar, camera, visual, motion, and lane key
  contains the reserved generation; a stale stage write deletes only its journal.
- A pending, rendering, or privately staged required-speech row with a private
  generation lock rejects every variant-mutating route with the stable 409 before any
  metadata/title/control write or enqueue. Parameterize direct generative edits,
  plan-item `render=False` autosaves, deliberately in-flight editor commit, and creator
  craft before `_stage_creator_speech_cut`; reads still work, post-terminal edits work,
  and a stale worker cannot clear a newer generation's lock.
- Crash immediately after private stage and before finalization. With an exact lock,
  generation prefix, existing artifact set, and private terminal capsule, retry
  rehydrates the accumulator, reuses the same generation, finalizes once, and emits
  one deduped outcome. Missing/malformed capsule, missing generation, a legacy/shared/
  editor-suffix key, missing object, lock skew, or detector/view skew rotates and
  rerenders; none can reach `_finalize_job` as a reused required-v1 result.
- Between stage and terminal publish, both archetypes expose no provisional path:
  initial output remains pending with no URL, rerender status/download returns exact
  last-good bytes, and the private stage is absent from status/plan/export payloads.
  The terminal transaction atomically swaps the winner; a losing stage never becomes
  readable.
- Speech-cut dispatch→claim→spec→render→finalize→compose→publish carries one winning
  generation. Hard-timeout claim recovery rotates it; late writes/objects from the
  old attempt cannot touch the winner, and compose does not mint a second generation.
- Fake-object-store races place distinct byte sentinels at last-good, losing, and
  winning generation keys. Failure restoration and exact prefix/key debt preserve the
  last-good/winner bytes and delete only unreferenced loser artifacts for both speech
  archetypes; no new path calls generation-less cleanup.
- Upload/copy intents enter their journal before each remote call. Hard-kill recovery,
  empty prefix, list failure, fail-after-N delete, relist failure, partial retry, and
  64-entry backpressure prove durable receipts never clear on ambiguous cleanup.
- Quiescence race: retire a writing generation, run an early reconcile against an
  empty prefix, let the superseded worker upload, then hard-kill it. The unexpired
  receipt survives the first reconcile; a post-lease sweep deletes the late object and
  clears the receipt only after verified-empty relist.
- Pause after core renderer return but before poster upload, and separately after
  `_finalize_job` but before speech compose upload; cancel/reconcile cannot delete the
  still-`writing` prefix. The outer coordinator closes only after the last call, and a
  later sweep removes every late key without resurrection.
- With 64 unresolved generation receipts, stale-job reaping first reconciles; if the
  cap remains full it preserves the pending generation and statuses for retry instead
  of terminalizing ownership without a receipt.
- Hard kill after speech-cut `_finalize_job` has set terminal `variants_ready` but
  before compose/publish exercises `reconcile_stuck_variants`, not only
  `reap_orphans`: it restores the exact prior snapshot/flag, retires the provisional
  generation, preserves last-good byte sentinels, and never marks inherited
  `video_path` ready. Missing prior state or cap-full cleanup debt leaves the row
  untouched for retry; an exact capsule yields `failed_owned`, absent context stays
  unknown, and generic non-speech stuck variants retain current behavior.
- Dispatch→task-never-started and claim-released→retry-lost both restore from exact
  unclaimed control+generation+lock+prior state after no-live/FOR UPDATE proof; a
  worker starting after the sweep loses the control CAS and cannot publish.
- A malformed surviving private stage makes prefix cleanup fail closed. A stage-vs-
  cleanup interleaving proves fresh reference discovery preserves every staged
  artifact until the terminalizer retires the stage and its receipt atomically.
- Successful publication consumes the closed exact-live render reservation receipt
  without deleting its prefix. More than one Beat page of healthy published jobs
  leaves no sparse-index rows and cannot starve a later abandoned receipt; retirement
  atomically recreates debt before dropping the final reference.
- More than one bounded page of persistent list/delete/relist failures ahead of a
  later deletable orphan proves each retained attempt advances `updated_at`, empty
  cleanup keys disappear, and successive sweeps eventually reach and delete the later
  orphan without unbounded work.
- Fixed-key legacy media and deterministic poster/base-poster/pre-overlay poster paths
  move into exact-key debt in the winning transaction. Verified cleanup removes the
  old bytes after a fresh Job-wide reference check and preserves the generation winner;
  cap-full publication stays staged/last-good.
- Admin cancel at pending/writing/staged/terminal-gap states hides output, restores
  last-good when present, produces at most one `cancelled_owned`, and retains lease-
  gated debt until verified empty. It never emits `failed_owned`/`published_*`.
- Individual delete returns `job_render_not_quiescent` for active state and for closed/
  partial/unavailable cleanup debt, including cancel/rotation after lock clear; it
  succeeds only after debt is empty. Account deletion persists a v2 outbox before Job
  deletion and passes delete→first purge→late upload→hard kill→post-lease verified-
  empty, including dispatch failure and idempotent outbox merge.
- Cache keys differ by detector version/effective mode, while metadata cache v2 keys
  validate their explicit ordered source-instance vector.
- Invalid mode/percent fails settings validation.
- Parameterized precedence matrix covers cleanup contract × master engine gate ×
  budget-clamp gate × mixed mode × assignment availability, including `off` never
  becoming shadow and kill switches never being bypassed.
- The legacy `detect_silences()` return value and `speech_coverage()` behavior remain
  unchanged after introducing the status-bearing companion.

**Diagnostics/privacy/UI**

- Worst-case receipt is at most 16 KiB; caps and omitted counters are deterministic.
- Baseline/candidate removal arrays drive their exact bands; truncated arrays are
  labeled partial and never presented as complete.
- Receipt contains no text, language, confidence, prompt, path, URL, note, raw
  fingerprint, media hash, or exception content.
- `assignment_status` is exhaustive and round-trips `ambiguous_clip_id` through the
  serializer, admin parser, malformed-event fallback, and direct-render fixtures.
- Recorder returns/tests no-context, invalid/missing/cancelled job, cap, success, and
  error outcomes; failure is fail-open and logs exception class only.
- Admin debug includes receipt; owner status and variant summaries do not.
- Retry/rerender receipts carry attempt IDs; UI chooses latest by default, unassigned
  receipts group safely, and fleet metrics deduplicate before counting render outcomes.
  Two unassigned clips with null tags in one job remain distinct via attempt+safe-slot/
  event-ordinal and are excluded from treatment denominators.
- Terminal orchestration emits authoritative `published_*` outcomes only after
  `_finalize_job` and any speech-cut compose/publish complete, then atomically
  rechecks the exact live generation. Staged-write/final-merge/claim supersession,
  missing/cancelled finalization, and render/upload/finalize/publish failures receive
  distinct bounded terminal outcomes; the assembler's earlier event never marks a
  plan live.
- Outcome recording is idempotent by attempt+variant+generation+view+version. Tests cover accepted
  finalization, newer-generation preservation, missing/cancelled Job, claim
  supersession, unexpected finalization raise, speech-cut publish failure, duplicate
  recorder calls, and a generation change immediately before the row-locked check.
  The same attempt/source tag with `full_clip` and `talking_head_spine_capped` views
  produces distinct correctly joined outcomes, including detector-version skew.
- A `SpineExtractionError` before analysis and after analysis both traverse the
  caller-owned context sink. Required-v1 initial render produces one `failed_owned`
  outcome and no montage; speech rerender restores byte-identical last-good state;
  legacy-auto alone retains montage degradation. No branch gets a false
  `published_*` outcome.
- Polling status during a private stage may compute lazy overlay previews but performs
  no DB write and does not mark the preview attempted. After publish, the next poll
  persists the stamp through the normal fresh-row merge; it is not lost by the swap.
- The shared public projection removes the entire private namespace and source-
  identity/cache fields from generative, plan-item, creator, and `GET /me/export`
  payloads without mutating stored JSON; ordinary authored fields round-trip.
- UI parser ignores malformed/version-skewed events; layers, labels, exact timing,
  nullable assignment, selection-vs-render status, and truncation warning render
  accessibly.
- Creator no-change copy is the exact truthful sentence.

**Synthetic media proof**

- Generate PCM from fixed sine islands plus digital silence at runtime, run the real
  `detect_silences(min_silence_s=0.1)`, and assert boundaries within 20 ms and both
  voiced islands reach V2. Skip only when FFmpeg/ffprobe is unavailable.

## Performance review

No new network/model/media pass is allowed. Shadow mode doubles only the pure cut-plan
arithmetic after the shared ASR and silence results exist. The interval workload is
small relative to ASR/FFmpeg; nevertheless, keep it deterministic and avoid rescanning
the same normalized silence list in separate task-layer implementations. A positional
v1 metadata-cache hit never triggers Gemini for identity; it stays baseline/shadow.
Storage metadata existence checks occur only on crash-resume classification, not every
normal render.

Persist one analysis receipt per source per render attempt, not one event per
word/island, plus the existing correlated per-variant render outcome. The 16 KiB
payload cap, 32-island cap, attempt-aware UI, and scalar-only fleet log prevent JSONB
growth and log cardinality mistakes. Cleanup/deletion sweeps remain page-bounded and
rotate retained failures for fairness. Acceptance includes p95 analysis-stage latency
delta and every specialized receipt-persistence outcome rate.

## Failure modes

| Failure | Handling and test | User/operator result |
|---|---|---|
| Successful silencedetect returns no markers/noisy clip | `status=ok`, empty spans, calibration gate returns no acoustic candidates | Baseline behavior; receipt truthfully shows a successful no-marker result |
| Confirmed no-audio at renderer precheck | Skip analysis entirely; emit minimal `precheck_no_audio`; assert zero Whisper/silencedetect calls | Existing benign no-op behavior without hallucinating speech |
| Talking-head outer media probe fails | Preserve existing typed strict failure; emit minimal `outer_media_probe_failed` | No unsafe baseline inference from unreadable media |
| Inner silencedetect probe/FFmpeg/parse failure after outer gate | Status-bearing companion returns a bounded failure enum; apply falls back to valid baseline | Render remains best-effort, but operators do not mistake tool failure for calibration |
| V2 build/validation/receipt construction fails after baseline | Candidate-only boundary preserves baseline and records bounded status/exception class | Required cleanup still renders safely; V2 is not selected |
| Omitted real word/cough resembles filler geometry | Shadow corpus + canary/manual precision gate; immediate shadow rollback | No broad apply until zero clipped lexical speech in review set |
| Clamp cannot fit an atom | Drop atom whole and record `dropped_budget` | No mid-vocalization cut; cleanup may under-deliver honestly |
| Forced/manual cut overlaps an atom | Promote the transitive connected component to protected closure; output-floor failure bails | Explicit manual intent and whole-vocalization safety both hold |
| Edge cuts consume the budget | Existing edge priority stays; later atoms drop whole | Hook remains protected; receipt identifies budget disposition |
| Matcher ID/path changes or numeric slot is reused | Persist never-reused source-instance ID; bucket on job UUID + instance ID; invalid mapping forces apply→shadow | Retry/reorder stays stable, replacement media never inherits an old treatment/tag |
| Source list/owner/terminal state changes while durable copying is in flight | Final-lock owner+cancel fence and exact-vector CAS; retry only vector staleness once; reference-checked cleanup on every rejected/partial exit | No stale write, wrong assignment, replaced-media render, or lifecycle-exempt partial-copy leak |
| Worker dies after a source/render upload but before CAS | Precommitted attempt/generation receipt + dedicated prefix; reaper/Beat removes only unreferenced verified-empty prefixes | Hard kills do not create permanent lifecycle-exempt orphans |
| Clip metadata completes out of order or only a positional v1 cache exists | Parallel indexed v2 binds records to slot+instance; a populated v1 baseline hit is preserved with identity unavailable/shadow and no forced model call; only an ordinary genuine cache miss analyzes and populates both | A clip never inherits another source's rollout bucket/tag, and legacy output stays deterministic |
| Initial/editor/speech-cut generations race | Claim-owned generation scopes every key and every row write; losing journal is reference-checked and deleted | Last-good/winner DB references and underlying bytes remain unchanged |
| Editor/autosave arrives before required initial publication | Private generation lock is checked by all three route writers, including deliberate bypass paths; terminal owner alone clears exact lock | Stable 409 and zero partial mutation; edit works after terminal completion |
| Private stage survives but worker dies before finalization | Retry reuses only exact generation/prefix/object/lock/context proof; any missing or legacy piece rotates and rerenders | No uncorrelated or shared-key media is blessed as cleaned |
| Provisional required output exists before terminal publish | Store it only in the redacted private stage; public row remains pending/no-output or exact last-good | Poll/download cannot observe incomplete compose/publish bytes |
| Terminal-job reaper sees provisional/inherited media | Shared required-speech terminalizer restores exact prior state and queues owned generation before clearing; path presence is never publication proof | Last-good media remains live, or state stays blocked for safe retry |
| Successful generation guard remains in cleanup queue | Terminal publish consumes closed exact-live receipt without deletion; retirement recreates debt | Healthy jobs do not starve abandoned cleanup work |
| Legacy fixed media/poster is replaced | Winning transaction writes exact-key debt before dropping reference; Beat excludes only the canonical receipt and verifies absence | Old lifecycle-exempt keys retire without risking the winner |
| Job is cancelled or deleted during upload | Cancel terminalizes ownership and retains lease-gated debt; individual delete rejects all active variants/debt and writes a prefix outbox; account delete externalizes prefixes/leases before row deletion | Output hides immediately, and late uploads are eventually verified empty |
| Cache contains other detector mode | Version/effective mode in key | Sibling variants cannot disagree or reuse stale plan |
| Retry/rerender appends another receipt | Attempt ID + latest-attempt UI + fleet dedupe | History remains auditable without double-counting treatment outcomes |
| Historical source was removed/replaced or durable object is absent | Audition resolves by source tag→instance, checks object existence, and refuses slot fallback | No wrong-person/wrong-clip playback; sample is marked unavailable |
| Trace persistence fails or trace is capped/cancelled | Specialized result enum; exception-class-only logging | Render continues; admin evidence absence is measurable without leaking payload |
| Staged generation loses stage write, final merge, claim, or publish race | The row-locked ownership-changing transaction emits discarded/`failed_owned`; only the exact live generation emits published | UI/fleet metrics never claim that superseded or deleted media is live |
| Talking-head spine fails before a renderer result exists | Caller-owned context survives the exception; required-v1 fails/restores last-good, legacy-auto alone degrades | Exactly one correlated failure; no uncleaned montage presented as required cleanup |
| Receipt exceeds cap | Deterministic tail stripping then summary-only | Admin displays truncation/omitted counts, never oversized content |
| Public serialization regresses | Route privacy sentinel | Deployment blocked before transcript/timing receipt exposure |
| Low-volume filler falls below `-30dB` | It is silence to this signal; no threshold retune from one incident | Pause rule may remove it; otherwise documented residual |
| FFmpeg apply fails | Existing required-v1 typed failure remains | Creator sees retry/off recovery, never a false cleaned success |

After the planned tests, there are **zero silent critical gaps**: every failure either
has a deterministic safety behavior, a test, an operator signal, or a visible existing
required-v1 error.

## Rollout and acceptance

1. **Land PR A** and run pure/golden/property/synthetic tests. Existing July golden
   must remain unchanged.
2. **Land and deploy PRs B–G with `mode=off`, percent `0`.** Verify legacy/off cache
   parity, private-state redaction, cleanup/outbox health, byte identity, and no V2
   event before enabling shadow.
3. Set **`mode=shadow`** for required-v1 jobs. Shadow does not render candidate media;
   use the access-controlled audition command from PR G to download the authorized
   durable source, extract only receipt-listed candidate windows into a local
   temporary directory, and delete those snippets automatically on exit. Review
   candidate labels within 24 hours for rollback speed—not retention. Rollout
   precondition: `GENERATIVE_TIMELINE_EDITOR_ENABLED=true`, verified on workers, and a
   sampled durable-copy/object-existence check passes before collecting the cohort.
   When enabled and copied successfully, `generative-jobs/{job_id}/sources/*`
   snapshots are exempt from lifecycle deletion and persist until the existing
   user/account deletion path removes them. Copying is best-effort, so the audition
   tool must check object existence and return bounded `source_unavailable` rather
   than guessing; unavailable samples are tracked separately and never enter the
   precision denominator. The audition command creates no additional durable speech
   copy; it consumes the existing timeline snapshot and must not claim a 24-hour TTL.

   ```bash
   cd src/apps/api
   python scripts/audit_speech_cleanup_shadow.py \
     --job-id <uuid> --attempt-id <opaque-id>
   ```

   Run it only from the existing secured operator environment. Require nonempty
   `settings.admin_api_key` (configured by `ADMIN_API_KEY`) and prompt for the operator
   credential through protected stdin via `getpass` (or the platform keychain), then
   compare it with `secrets.compare_digest`; never accept it on argv or log it. Merely
   reading the same environment value twice is not an authentication check. The script resolves
   exactly one current path by matching the receipt `source_tag` against that job's
   path/source-instance pairs (never by slot), verifies the object exists, downloads
   through the existing storage service, and plays or exports snippets only inside a
   `TemporaryDirectory`, and logs identifiers/timings—not paths or speech content.
4. Shadow gate before apply:
   - at least 50 eligible islands across at least 30 source tags;
   - at least 95% of eligible-island samples auditable from verified source objects;
   - private corpus includes Turkish/English fillers, inhale, laugh, cough, omitted
     real word, click/handling/keyboard noise, and music;
   - 100% recall for the reported 571.746 ms island;
   - zero cuts through labeled lexical speech;
   - at least 95% human safe-to-remove precision from source-window audition;
   - zero partial atoms; candidate survival at least 99% when budget can contain them;
   - diagnostic truncation below 1% and no public leak;
   - no more than +1 percentage point render-failure regression.
5. Set **`mode=apply`, percent `5`**, then advance `5 → 25 → 50 → 100` after at
   least 20 applied jobs and 24 hours at each step with all gates holding. At 5%, add
   a rendered-join quality gate (no clipped onset/tail or audible discontinuity),
   which cannot be honestly measured from shadow source snippets.
6. Behavioral rollback: set mode to `shadow` to stop output changes while retaining
   evidence. Computation/persistence rollback: set `off`. Restart workers after each
   Fly secret change.

Track eligible islands per clip/audio minute; human precision; full/partial/budget-drop
rates; baseline/candidate removed milliseconds; clamp/bailout/no-change rates; render
success and typed cleanup failures; p95 analysis latency; retry/disable-cleanup within
24h; diagnostic truncation and persistence failures.

Completion is 100% apply for `required_v1` with the acceptance gates still green, not
merely merged code or shadow deployment.

## Implementation train

### PR A — detector and atomic budget allocation

- `src/apps/api/app/pipeline/silence_cut.py`
- `src/apps/api/app/services/clip_speech.py`
- `src/apps/api/tests/pipeline/test_silence_cut.py`
- `src/apps/api/tests/pipeline/test_silence_cut_mixed_gap_golden.py` (new)
- `src/apps/api/tests/services/test_clip_speech.py`
- `plans/010-silence-filler-cut.md`
- `TODOS.md` (close the P2 tokenless-filler clamp TODO; retain the separate P3
  forced-carrier flank optimization)

Verify:

```bash
cd src/apps/api
python3 -m pytest \
  tests/pipeline/test_silence_cut.py \
  tests/pipeline/test_silence_cut_golden.py \
  tests/pipeline/test_silence_cut_mixed_gap_golden.py \
  tests/services/test_clip_speech.py -q
ruff check app/pipeline/silence_cut.py app/services/clip_speech.py tests/pipeline/test_silence_cut*.py tests/services/test_clip_speech.py
ruff format --check app/pipeline/silence_cut.py app/services/clip_speech.py tests/pipeline/test_silence_cut*.py tests/services/test_clip_speech.py
```

### PR B — persistent source-instance identity

- `src/apps/api/app/routes/me.py`
- `src/apps/api/app/services/public_assembly_plan.py` (new)
- `src/apps/api/app/services/speech_cleanup_identity.py` (new)
- `src/apps/api/app/routes/_omni.py`
- `src/apps/api/app/tasks/generative_build.py`
- `src/apps/api/app/tasks/template_orchestrate.py`
- `src/apps/api/tests/services/test_speech_cleanup_identity.py` (new)
- `src/apps/api/tests/services/test_public_assembly_plan.py` (new)
- `src/apps/api/tests/routes/test_me_account.py`
- `src/apps/api/tests/tasks/test_omni_generate.py`
- `src/apps/api/tests/tasks/test_generative_build.py`
- `src/apps/api/tests/tasks/test_template_orchestrate.py`

This PR atomically backfills and maintains `clip_source_instance_ids`, preserves the
IDs across reorder, creates a fresh ID for appended/replacement media, and carries the
instance mapping through indexed ingest. A parallel metadata identity envelope
preserves explicit source mapping without invalidating positional v1 cache hits. The
shared public projection prevents ID/cache state from leaking through account export.
It does not change durable-copy behavior or enable V2.

Verify:

```bash
cd src/apps/api
python3 -m pytest \
  tests/routes/test_me_account.py \
  tests/services/test_public_assembly_plan.py \
  tests/services/test_speech_cleanup_identity.py \
  tests/tasks/test_omni_generate.py \
  tests/tasks/test_generative_build.py \
  tests/tasks/test_template_orchestrate.py -q
ruff check app/routes/me.py app/services/public_assembly_plan.py app/services/speech_cleanup_identity.py app/routes/_omni.py app/tasks/generative_build.py app/tasks/template_orchestrate.py tests/routes/test_me_account.py tests/services/test_public_assembly_plan.py tests/services/test_speech_cleanup_identity.py tests/tasks/test_omni_generate.py tests/tasks/test_generative_build.py tests/tasks/test_template_orchestrate.py
ruff format --check app/routes/me.py app/services/public_assembly_plan.py app/services/speech_cleanup_identity.py app/routes/_omni.py app/tasks/generative_build.py app/tasks/template_orchestrate.py tests/routes/test_me_account.py tests/services/test_public_assembly_plan.py tests/services/test_speech_cleanup_identity.py tests/tasks/test_omni_generate.py tests/tasks/test_generative_build.py tests/tasks/test_template_orchestrate.py
```

### PR C — durable source CAS and crash-safe cleanup substrate

- `src/apps/api/app/migrations/versions/0092_storage_attempt_cleanup_index.py` (new)
- `src/apps/api/app/models.py`
- `src/apps/api/app/routes/me.py`
- `src/apps/api/app/services/durable_attempt_cleanup.py` (new)
- `src/apps/api/app/services/job_storage_deletion.py` (new)
- `src/apps/api/app/storage.py`
- `src/apps/api/app/tasks/account_lifecycle.py`
- `src/apps/api/app/tasks/generative_build.py`
- `src/apps/api/app/tasks/reaper.py`
- `src/apps/api/tests/services/test_durable_attempt_cleanup.py` (new)
- `src/apps/api/tests/services/test_job_storage_deletion.py` (new)
- `src/apps/api/tests/routes/test_me_account.py`
- `src/apps/api/tests/routes/test_me_jobs.py`
- `src/apps/api/tests/test_content_plan_schema.py`
- `src/apps/api/tests/test_storage.py`
- `src/apps/api/tests/tasks/test_account_lifecycle.py`
- `src/apps/api/tests/tasks/test_generative_build.py`
- `src/apps/api/tests/tasks/test_reaper.py`

This PR makes durable-source rewriting a fenced path+ID CAS, stores copy-attempt
receipts before remote I/O, retries only vector staleness once, and adds the shared
lease/quiescence/verified-empty reconciliation substrate. Immediate, reaper, and Beat
paths retain ambiguous receipts and fail closed at caps. It also versions the existing
`JobStorageDeletion` JSONB manifest so account erasure can persist job prefixes and
quiescence leases before deleting Job rows. It does not change speech selection or
rendering.

Verify:

```bash
cd src/apps/api
python3 -m pytest \
  tests/services/test_durable_attempt_cleanup.py \
  tests/services/test_job_storage_deletion.py \
  tests/routes/test_me_account.py \
  tests/routes/test_me_jobs.py \
  tests/test_content_plan_schema.py \
  tests/test_storage.py \
  tests/tasks/test_account_lifecycle.py \
  tests/tasks/test_generative_build.py \
  tests/tasks/test_reaper.py -q
ruff check app/migrations/versions/0092_storage_attempt_cleanup_index.py app/models.py app/routes/me.py app/services/durable_attempt_cleanup.py app/services/job_storage_deletion.py app/storage.py app/tasks/account_lifecycle.py app/tasks/generative_build.py app/tasks/reaper.py tests/services/test_durable_attempt_cleanup.py tests/services/test_job_storage_deletion.py tests/routes/test_me_account.py tests/routes/test_me_jobs.py tests/test_content_plan_schema.py tests/test_storage.py tests/tasks/test_account_lifecycle.py tests/tasks/test_generative_build.py tests/tasks/test_reaper.py
ruff format --check app/migrations/versions/0092_storage_attempt_cleanup_index.py app/models.py app/routes/me.py app/services/durable_attempt_cleanup.py app/services/job_storage_deletion.py app/storage.py app/tasks/account_lifecycle.py app/tasks/generative_build.py app/tasks/reaper.py tests/services/test_durable_attempt_cleanup.py tests/services/test_job_storage_deletion.py tests/routes/test_me_account.py tests/routes/test_me_jobs.py tests/test_content_plan_schema.py tests/test_storage.py tests/tasks/test_account_lifecycle.py tests/tasks/test_generative_build.py tests/tasks/test_reaper.py
```

### PR D — required-speech generation ownership and strict failure

- `src/apps/api/app/routes/admin_jobs.py`
- `src/apps/api/app/routes/creator_agent.py`
- `src/apps/api/app/routes/generative_jobs.py`
- `src/apps/api/app/routes/me.py`
- `src/apps/api/app/routes/plan_items.py`
- `src/apps/api/app/services/job_storage_paths.py`
- `src/apps/api/app/services/speech_cleanup_terminal.py` (new; state/storage terminalizer)
- `src/apps/api/app/services/template_poster.py`
- `src/apps/api/app/services/variant_generation_guard.py` (new)
- `src/apps/api/app/tasks/account_lifecycle.py`
- `src/apps/api/app/tasks/generative_build.py`
- `src/apps/api/app/tasks/maintenance.py`
- `src/apps/api/app/tasks/reaper.py`
- `src/apps/api/tests/routes/test_admin_jobs.py`
- `src/apps/api/tests/routes/test_creator_agent.py`
- `src/apps/api/tests/routes/test_generative_jobs.py`
- `src/apps/api/tests/routes/test_me_account.py`
- `src/apps/api/tests/routes/test_me_jobs.py`
- `src/apps/api/tests/routes/test_plan_item_variant_edit.py`
- `src/apps/api/tests/services/test_job_storage_paths.py`
- `src/apps/api/tests/services/test_speech_cleanup_terminal.py` (new; state/storage cases)
- `src/apps/api/tests/services/test_variant_generation_guard.py` (new)
- `src/apps/api/tests/tasks/test_account_lifecycle.py`
- `src/apps/api/tests/tasks/test_generative_build.py`
- `src/apps/api/tests/test_maintenance_task_routes.py`
- `src/apps/api/tests/tasks/test_reaper.py`
- `src/apps/api/tests/tasks/test_render_generation_guard.py`
- `src/apps/api/tests/tasks/test_speech_cut_rerender.py`
- `src/apps/api/tests/test_template_poster.py`

This PR reserves one claim-owned generation for every required-v1 speech attempt,
threads it through every mark/render/artifact/compose/publish operation, generation-
scopes poster and media bytes, and registers durable cleanup before upload. It also
installs the private all-writer editor lock, consumes healthy active receipts,
privately stages output, generation-safes cancel/delete and both reapers, and makes
required-v1 spine failures strict while preserving legacy-auto montage degradation.
Until PR F supplies durable terminal-context rehydration, a private staged required-v1
result always rotates and rerenders. No detector behavior changes.

Verify:

```bash
cd src/apps/api
python3 -m pytest \
  tests/routes/test_admin_jobs.py \
  tests/routes/test_creator_agent.py \
  tests/routes/test_generative_jobs.py \
  tests/routes/test_me_account.py \
  tests/routes/test_me_jobs.py \
  tests/routes/test_plan_item_variant_edit.py \
  tests/services/test_job_storage_paths.py \
  tests/services/test_speech_cleanup_terminal.py \
  tests/services/test_variant_generation_guard.py \
  tests/tasks/test_account_lifecycle.py \
  tests/tasks/test_generative_build.py \
  tests/test_maintenance_task_routes.py \
  tests/tasks/test_reaper.py \
  tests/tasks/test_render_generation_guard.py \
  tests/tasks/test_speech_cut_rerender.py \
  tests/test_template_poster.py -q
ruff check app/routes/admin_jobs.py app/routes/creator_agent.py app/routes/generative_jobs.py app/routes/me.py app/routes/plan_items.py app/services/job_storage_paths.py app/services/speech_cleanup_terminal.py app/services/template_poster.py app/services/variant_generation_guard.py app/tasks/account_lifecycle.py app/tasks/generative_build.py app/tasks/maintenance.py app/tasks/reaper.py tests/routes/test_admin_jobs.py tests/routes/test_creator_agent.py tests/routes/test_generative_jobs.py tests/routes/test_me_account.py tests/routes/test_me_jobs.py tests/routes/test_plan_item_variant_edit.py tests/services/test_job_storage_paths.py tests/services/test_speech_cleanup_terminal.py tests/services/test_variant_generation_guard.py tests/tasks/test_account_lifecycle.py tests/tasks/test_generative_build.py tests/test_maintenance_task_routes.py tests/tasks/test_reaper.py tests/tasks/test_render_generation_guard.py tests/tasks/test_speech_cut_rerender.py tests/test_template_poster.py
ruff format --check app/routes/admin_jobs.py app/routes/creator_agent.py app/routes/generative_jobs.py app/routes/me.py app/routes/plan_items.py app/services/job_storage_paths.py app/services/speech_cleanup_terminal.py app/services/template_poster.py app/services/variant_generation_guard.py app/tasks/account_lifecycle.py app/tasks/generative_build.py app/tasks/maintenance.py app/tasks/reaper.py tests/routes/test_admin_jobs.py tests/routes/test_creator_agent.py tests/routes/test_generative_jobs.py tests/routes/test_me_account.py tests/routes/test_me_jobs.py tests/routes/test_plan_item_variant_edit.py tests/services/test_job_storage_paths.py tests/services/test_speech_cleanup_terminal.py tests/services/test_variant_generation_guard.py tests/tasks/test_account_lifecycle.py tests/tasks/test_generative_build.py tests/test_maintenance_task_routes.py tests/tasks/test_reaper.py tests/tasks/test_render_generation_guard.py tests/tasks/test_speech_cut_rerender.py tests/test_template_poster.py
```

### PR E — mode, candidate safety, and renderer integration

- `src/apps/api/app/config.py`
- `.env.example`
- `src/apps/api/app/tasks/generative_build.py`
- `src/apps/api/tests/test_config.py`
- `src/apps/api/tests/tasks/test_generative_build_silence_cut.py`
- `src/apps/api/tests/smart_edit/test_v2_render_contract.py`
- `src/apps/api/tests/tasks/test_generative_dispatch.py`
- `src/apps/api/tests/tasks/test_generative_build.py`

Construct `SpeechCleanupAssignment` envelopes from job + source-instance IDs and pass
the selected envelope plus `render_trace_id` through both renderer signatures and all
task call sites. Talking-head resolves the selected spine; subtitled resolves the same
first clip it already renders while allowing additional clips/map entries. Candidate
failure isolation, runtime validation, pre-ASR no-audio gates, the full configuration
precedence matrix, and every direct-call fixture land here.

Verify:

```bash
cd src/apps/api
python3 -m pytest \
  tests/test_config.py \
  tests/tasks/test_generative_build_silence_cut.py \
  tests/smart_edit/test_v2_render_contract.py \
  tests/tasks/test_generative_dispatch.py \
  tests/tasks/test_generative_build.py -q
ruff check app/config.py app/tasks/generative_build.py tests/test_config.py tests/tasks/test_generative_build_silence_cut.py tests/smart_edit/test_v2_render_contract.py tests/tasks/test_generative_dispatch.py tests/tasks/test_generative_build.py
ruff format --check app/config.py app/tasks/generative_build.py tests/test_config.py tests/tasks/test_generative_build_silence_cut.py tests/smart_edit/test_v2_render_contract.py tests/tasks/test_generative_dispatch.py tests/tasks/test_generative_build.py
```

### PR F — privacy-safe receipt and authoritative publication outcome

- `src/apps/api/app/routes/admin_jobs.py`
- `src/apps/api/app/services/pipeline_trace.py`
- `src/apps/api/app/services/public_assembly_plan.py`
- `src/apps/api/app/services/speech_cleanup_terminal.py` (extend with evidence/capsules)
- `src/apps/api/app/routes/generative_jobs.py`
- `src/apps/api/app/routes/me.py`
- `src/apps/api/app/routes/plan_items.py`
- `src/apps/api/app/tasks/account_lifecycle.py`
- `src/apps/api/app/tasks/generative_build.py`
- `src/apps/api/app/tasks/reaper.py`
- `src/apps/api/tests/services/test_pipeline_trace.py`
- `src/apps/api/tests/services/test_public_assembly_plan.py`
- `src/apps/api/tests/services/test_speech_cleanup_terminal.py` (extend)
- `src/apps/api/tests/routes/test_admin_jobs.py`
- `src/apps/api/tests/routes/test_generative_jobs.py`
- `src/apps/api/tests/routes/test_me_account.py`
- `src/apps/api/tests/routes/test_me_jobs.py`
- `src/apps/api/tests/routes/test_plan_item_variant_edit.py`
- `src/apps/api/tests/tasks/test_account_lifecycle.py`
- `src/apps/api/tests/tasks/test_generative_build_silence_cut.py`
- `src/apps/api/tests/tasks/test_generative_build.py`
- `src/apps/api/tests/tasks/test_reaper.py`
- `src/apps/api/tests/tasks/test_speech_cut_rerender.py`

The specialized receipt writer, nullable assignment/precheck receipts, exact bounded
plan bands, per-attempt semantics, terminal generation-checked publication outcomes,
durable scalar context capsules and strict retry rehydration, reaper terminal outcomes,
finalization/supersession/publish failure coverage, public-route privacy sentinels,
and scalar fleet statuses land here. Do not modify
`talking_head_assembler.py`; its earlier event remains explicitly non-authoritative.

Verify:

```bash
cd src/apps/api
python3 -m pytest \
  tests/services/test_pipeline_trace.py \
  tests/services/test_public_assembly_plan.py \
  tests/services/test_speech_cleanup_terminal.py \
  tests/routes/test_admin_jobs.py \
  tests/routes/test_generative_jobs.py \
  tests/routes/test_me_account.py \
  tests/routes/test_me_jobs.py \
  tests/routes/test_plan_item_variant_edit.py \
  tests/tasks/test_account_lifecycle.py \
  tests/tasks/test_generative_build_silence_cut.py \
  tests/tasks/test_generative_build.py \
  tests/tasks/test_reaper.py \
  tests/tasks/test_speech_cut_rerender.py -q
ruff check app/routes/admin_jobs.py app/services/pipeline_trace.py app/services/public_assembly_plan.py app/services/speech_cleanup_terminal.py app/routes/generative_jobs.py app/routes/me.py app/routes/plan_items.py app/tasks/account_lifecycle.py app/tasks/generative_build.py app/tasks/reaper.py tests/services/test_pipeline_trace.py tests/services/test_public_assembly_plan.py tests/services/test_speech_cleanup_terminal.py tests/routes/test_admin_jobs.py tests/routes/test_generative_jobs.py tests/routes/test_me_account.py tests/routes/test_me_jobs.py tests/routes/test_plan_item_variant_edit.py tests/tasks/test_account_lifecycle.py tests/tasks/test_generative_build_silence_cut.py tests/tasks/test_generative_build.py tests/tasks/test_reaper.py tests/tasks/test_speech_cut_rerender.py
ruff format --check app/routes/admin_jobs.py app/services/pipeline_trace.py app/services/public_assembly_plan.py app/services/speech_cleanup_terminal.py app/routes/generative_jobs.py app/routes/me.py app/routes/plan_items.py app/tasks/account_lifecycle.py app/tasks/generative_build.py app/tasks/reaper.py tests/services/test_pipeline_trace.py tests/services/test_public_assembly_plan.py tests/services/test_speech_cleanup_terminal.py tests/routes/test_admin_jobs.py tests/routes/test_generative_jobs.py tests/routes/test_me_account.py tests/routes/test_me_jobs.py tests/routes/test_plan_item_variant_edit.py tests/tasks/test_account_lifecycle.py tests/tasks/test_generative_build_silence_cut.py tests/tasks/test_generative_build.py tests/tasks/test_reaper.py tests/tasks/test_speech_cut_rerender.py
```

### PR G — operator visualization, shadow audition, and honest creator state

- `src/apps/web/src/app/admin/jobs/[id]/SpeechCleanupDiagnostics.tsx` (new)
- `src/apps/web/src/app/admin/jobs/[id]/page.tsx`
- `src/apps/web/src/__tests__/SpeechCleanupDiagnostics.test.tsx` (new)
- `src/apps/web/src/app/plan/items/[id]/page.tsx`
- `src/apps/web/src/__tests__/plan/plan-item-page.test.tsx`
- `src/apps/api/scripts/audit_speech_cleanup_shadow.py` (new, admin-only local
  source-window audition; temp output auto-deletes)
- `src/apps/api/tests/scripts/test_audit_speech_cleanup_shadow.py` (new)

Verify:

```bash
cd src/apps/web
npm test -- --runInBand SpeechCleanupDiagnostics plan-item-page
npm run lint
npx tsc --noEmit

cd ../api
python3 -m pytest tests/scripts/test_audit_speech_cleanup_shadow.py -q
ruff check scripts/audit_speech_cleanup_shadow.py tests/scripts/test_audit_speech_cleanup_shadow.py
```

### Operational rollout

- Deploy PRs A–G with V2 off; do not enter shadow until PR F evidence and PR G
  audition/UX are both live.
- Execute shadow review and record the cohort counts/precision in the PR or release
  runbook.
- Advance canary only at the stated gates; record each secret change and rollback test.
- Re-render the attached source through required-v1 at 100% apply and verify the
  `7.406100–7.977846` source interval is wholly absent from the output.

## Implementation Tasks

Implementation state (2026-09-01): T1–T11 are complete in the isolated feature
worktree. T12 remains intentionally open because shadow review and percentage canary
advancement require a deployed worker, production cohort evidence, and an explicit
rollback observation; the code keeps rollout disabled by default until those gates run.

- [x] **T1 (P1, human: ~4h / CC: ~45min)** — detector — implement normalized
  silence-complement islands with bilateral flank/boundary decisions and add the
  status-bearing, legacy-compatible silencedetect result API.
  - Surfaced by: architecture finding 1 and job `d2d20bd2`.
  - Verify: pure threshold/boundary/permutation matrix plus every probe/audio/FFmpeg/
    parse status and legacy-wrapper parity.
- [x] **T2 (P1, human: ~1d / CC: ~90min)** — clamp — preserve filler atoms and
  enforce protected-closure → edge → filler-group → retake-group → silence
  allocation, with V2-aware hygiene and removal-count caps.
  - Surfaced by: architecture finding 2; exact job budget is already full.
  - Verify: every-budget whole-or-none properties, forced-inside-filler,
    filler/retake overlap, dropped atom carved from selected carrier, 160 ms carrier,
    over-`MAX_REMOVALS`, and #959 regressions.
- [x] **T3 (P1, human: ~4h / CC: ~40min)** — regression — add the privacy-safe exact
  incident golden and synthetic FFmpeg proof.
  - Surfaced by: test review; existing tests cover only tokenized filler.
  - Verify: new golden plus unchanged July golden.
- [x] **T4 (P1, human: ~1d / CC: ~90min)** — source identity/cache — persist and
  maintain never-reused source-instance IDs, pair every list mutation, add indexed
  metadata identity v2 beside unchanged v1 reads, and build typed clip assignments
  without positional inference.
  - Surfaced by: architecture findings 6 and 16; code-quality findings 5 and 14.
  - Verify: fenced legacy backfill, reorder/remove/append/replacement, out-of-order and
    partial analysis, populated-v1 hit preservation with Gemini forbidden, missing/
    malformed v2 identity→shadow without a model call, genuine miss populating both,
    changed Gemini IDs, duplicate `clip_id`, distinct `clip_0` jobs, and export/owner
    privacy.
- [x] **T5 (P1, human: ~1.5d / CC: ~2h)** — durable source/storage safety — implement
  path+ID CAS, attempt-prefix receipts before copy, owner/cancel fencing, one stale
  retry, quiescent verified-empty cleanup, fair sparse-index discovery, and durable
  JobStorageDeletion v2 prefix manifests.
  - Surfaced by: architecture findings 14 and 18; code-quality findings 11 and 16.
  - Verify: failure→success, fail-after-N, missing/cancelled/owner-rejected/commit-
    failed/already-durable branches, second-stale failure, hard kill before CAS,
    adopted-live receipt, list/delete/relist failures, lease race, cap backpressure,
    migration ancestry/concurrency, retained-row rotation, off/legacy identity
    provisioning, account outbox late-upload recovery, and index-matching bounded query.
- [x] **T6 (P1, human: ~4d / CC: ~6h)** — render generation ownership — reserve one
  required-v1 claim generation, fence every state write, generation-scope every media/
  poster artifact, privately stage output, lock every editor writer, persist hard-kill/
  exact-key debt, generation-safe cancel/reap/delete, preserve bytes on rollback, and
  make required spine failure strict.
  - Surfaced by: architecture findings 17–31 and 34–35; code-quality findings 15–29
    and 32.
  - Verify: both dispatch writers, normal/recovered claim continuity, rendering/final
    CAS, private-stage visibility/resume, all three route writers and hidden GET write,
    every subtitled/talking-head artifact key, poster override/allowlist, legacy exact-
    key retirement, fake-object byte preservation, claimed/unclaimed reapers,
    cancel/delete/account late-upload recovery, receipt fairness/backpressure,
    required-v1 initial failure/last-good restore, and legacy-auto montage parity.
- [x] **T7 (P1, human: ~1d / CC: ~90min)** — selection/orchestration — add validated
  off/shadow/apply mode, percentage assignment, paired normalized baseline/V2 build,
  versioned cut cache, candidate-only failure isolation, runtime validation, both
  renderer envelopes, and one-pass no-audio/tool-status handling.
  - Surfaced by: architecture findings 7, 9, and 13; code-quality findings 4, 6, 7,
    and 10.
  - Verify: precedence matrix, 0/100 buckets, invalid/unassigned envelopes, both direct
    renderer signatures, candidate build/validation/receipt exceptions, pre-ASR gates,
    typed inner-tool failure, and exactly one ASR plus one silencedetect pass.
- [x] **T8 (P1, human: ~1d / CC: ~90min)** — observability — add the allowlisted
  16 KiB timing-only receipt, typed atomic dispositions, exact bounded plan bands,
  exception-safe context accumulator, state-transaction terminal outcomes, specialized
  persistence results, and scalar fleet log.
  - Surfaced by: architecture findings 3, 10, 12, and 15; code-quality findings 3, 9,
    12, and 13.
  - Verify: privacy/truncation, missing assignment, every disposition/persistence
    branch, dual view/version correlation, accepted/superseded/rollback/fail/publish
    and cancellation outcomes, pre-stage explicit snapshot, durable resume capsule,
    null-tag multi-source fleet grouping, helper-failure fail-open commits,
    idempotence, post-render failure, account-export redaction, and admin-only route
    sentinel.
- [x] **T9 (P2, human: ~4h / CC: ~45min)** — admin UX — render layered
  ASR/silence/candidate/baseline/candidate-plan diagnostics with accessible labels,
  attempt selection, nullable assignment, generation-checked current-live state, and
  selection-vs-publication truth.
  - Surfaced by: architecture finding 5.
  - Verify: malformed/version skew, exact plan timing, unassigned grouping, latest
    attempt, dual-view outcome correlation, historical-generation state, and partial-
    band/truncation component tests.
- [x] **T10 (P2, human: ~30min / CC: ~5min)** — creator UX — replace the unsupported
  no-filler claim with an exact no-cuts statement.
  - Surfaced by: code-quality review of `page.tsx:3482-3484`.
  - Verify: focused Jest assertion.
- [x] **T11 (P2, human: ~3h / CC: ~35min)** — shadow audition — add the admin-only
  local tool that resolves authorized durable sources and extracts receipt-listed
  windows into auto-deleted temporary files without persisting new media.
  - Surfaced by: shadow acceptance review.
  - Verify: missing/empty `ADMIN_API_KEY`, wrong protected-stdin credential, constant-
    time success path, authorization/job/tag-to-instance validation, durable-copy flag/object
    existence, removed/replaced source refusal, safe temp lifecycle, exact windows,
    missing/truncated receipt, and no content-bearing logs.
- [ ] **T12 (P1, human: ~2d elapsed / CC: ~30min active)** — rollout — execute the
  shadow corpus and 5/25/50/100 canary with gates and rollback proof.
  - Surfaced by: semantic ambiguity failure mode.
  - Verify: acceptance metrics plus attached-source re-render at full apply.

## Implementation verification (2026-09-01)

- Exact incident geometry: V2 selects both tokenless voiced islands
  `5.778866–6.209660` and `7.406100–7.977846` as whole acoustic-filler atoms;
  V1 selects neither. A real FFmpeg tone/render test proves both tones are absent
  after the V2 cut while every transcript word remains intact.
- Focused integrated backend matrix: `823 passed` across detector, exact golden,
  generation staging/publication, retry/resume, reapers, cancellation, account
  deletion, privacy projection, creator status, and operator audit paths.
- Complete backend suite after finalizer regression repair: `11211 passed, 64
  skipped, 2 xfailed` in 14m29s.
- Complete frontend suite: `293` Jest suites / `3476` tests passed; TypeScript
  `--noEmit` passed; Next lint completed with the repository's pre-existing warnings
  and no errors.
- Backend Ruff, format checks for 70 changed Python files, Alembic single-head check
  (`0092`), and `git diff --check` passed. `template_orchestrate.py` remains the one
  known baseline formatter-drift file (both `origin/main` and this worktree fail the
  whole-file formatter check); it was not bulk-reformatted into this fix.
- Production shadow/canary execution is not claimed here: configuration remains
  off/0 by default and T12 stays open until deployment evidence is recorded.

## Parallelization

| Step | Modules | Depends on |
|---|---|---|
| PR A detector/clamp | `api/app/pipeline`, `api/tests/pipeline`, plans | — |
| PR B source identity/cache/privacy projection | identity services, Omni, metadata task, export | — |
| PR C source/outbox cleanup substrate | migration, storage, account lifecycle, reaper, deletion outbox | PR B |
| PR D generation/staging/lifecycle safety | render tasks, all editor/cancel/delete writers, poster/storage, reapers | PR B + PR C |
| PR E mode/selection/integration | config, task selection, both renderer contracts | PR A + PR B + PR D |
| PR F receipt/outcome/capsule | trace/terminal services, task/reaper outcome wiring, privacy routes | PR D + PR E |
| PR G operator/user UX + audition | admin/plan web, secured local audit command | PR F |
| Operational canary | Fly worker config, admin evidence, audition command | PRs A–G |

PR A and PR B can develop in parallel worktrees. The storage/ownership spine is
**B → C → D**; selection joins only after both that spine and detector PR A are ready:
**A+D → E → F → G → rollout**. After PR F freezes the receipt schema, PR G's frontend
and operator-audition work can run as two independent lanes, but both must merge before
shadow review. No shadow or canary work runs against a partial train.

## NOT in scope

- **Second targeted ASR, VAD, or semantic classifier** — no evidence yet that another
  model is needed; add only if shadow precision fails its gate.
- **ASR prompt or filler-lexicon retuning** — the incident filler was omitted, not
  normalized incorrectly; prompt changes would require live evals and do not fix the
  interval blind spot.
- **Changing `-30dB`, 0.1s silence, or 0.15–1.2s acoustic thresholds** — one incident
  does not justify widening the false-positive surface.
- **Discourse-word removal (`like`, `you know`, `şey`)** — real words require semantic
  judgment and remain prohibited.
- **`legacy_auto` behavior change** — V2 is explicit-consent required-v1 only.
- **Historical receipt backfill for job d2d20bd2** — raw cleanup inputs were not
  persisted; the media-forensics artifact is the honest evidence.
- **New database table, endpoint, or public diagnostic payload** — the existing
  admin-only pipeline trace and `JobStorageDeletion` outbox are sufficient. The one
  concurrent sparse-index migration `0092` is explicitly in scope.
- **Forced-carrier optional flank recovery (existing TODO P3)** — useful cleanup
  optimization, but distinct from the filler atomicity required by this incident.
- **Committing either attached video or derived user speech** — tests use timing-only
  fabricated fixtures and synthetic PCM.

## TODOS.md disposition

- Close **“Clamp trim boundaries vs tokenless acoustic-filler regions” (P2)** when PR A
  lands; T2 is its complete implementation.
- Retain **“Envelope mode gives up merged-carrier flanks” (P3)** with a note that V2's
  atomic spans must remain intact if that optimization is later implemented.
- Do not add a semantic-classifier TODO yet. If shadow fails the precision gate, create
  a new evidence-backed P2 with the labeled false-positive classes and measured rates.

## Review completion summary

- Step 0 Scope Challenge: scope reduced into seven dependency-ordered PRs.
- Architecture Review: 35 issues found; all complete recommendations selected.
- Code Quality Review: 32 issues found; all folded into tasks/contracts.
- Test Review: coverage diagram produced; 70 explicit adversarial branches mapped to
  deterministic tests.
- Performance Review: 1 issue found; no extra production analysis calls, one capped
  receipt per cache entry/attempt, and correlated scalar render outcomes.
- NOT in scope: written.
- What already exists: written and reused.
- TODOS.md updates: 2 dispositions, 1 conditional item deliberately not filed.
- Failure modes: 0 silent critical gaps after planned coverage.
- Outside voice: nested Codex skipped because this review is already running under
  Codex; four fresh-context specialists plus two nested audits challenged code, media,
  API wiring, clamp invariants, diagnostics, and rollout.
- Parallelization: 8 lanes; PR A and PR B start in parallel, B → C → D forms the
  storage spine, then A+D → E → F → G → rollout; PR G frontend and operator-tool work
  split after schema freeze.
- Lake Score: 138/138 review recommendations chose the complete option.
- Unresolved decisions: 0.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | NOT RUN | Scope was challenged and auto-decided inside this eng review |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | NOT RUN | Replaced by two fresh-context adversarial audits within this Codex review |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAN | 138 issues, 0 critical gaps; all decisions resolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | NOT RUN | Admin diagnostics and creator-copy scope reviewed here |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | NOT RUN | PR train, commands, migrations, and rollout runbook reviewed here |

**VERDICT:** ENG CLEARED — implementation-ready in seven dependency-ordered PRs.

NO UNRESOLVED DECISIONS
