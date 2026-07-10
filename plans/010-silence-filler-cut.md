# 010 — Automatic silence + filler-sound cutting for speech edits

**Status:** PLANNED
**Owner:** silence-cut stage
**Flag:** `SILENCE_CUT_ENABLED` (default `false`)

## Problem

Raw talk-to-camera footage is full of dead air and filler vocalizations ("uhh",
"um", "ııı", "eee"). Creators expect the platform to tighten these
automatically — every competing editor (Descript, CapCut) ships this. Today
Nova renders the spine/subtitled clip verbatim: a 40s take with 8s of pauses
ships as a 40s video.

## Scope decision (locked at eng review)

Source speech only survives to the output in **two render paths**. The cut
engine is a shared pure module; those two paths call it. Everything else is
structurally excluded:

| Edit path | Speech in output | Silence-cut |
|---|---|---|
| `subtitled` (single clip + captions) | yes — whole clip | **applied** |
| `talking_head` (spine + b-roll) | yes — spine audio | **applied** |
| `narrated` self-narration (1 clip → subtitled, 2+ → talking_head) | yes | **inherited** via dispatch |
| `narrated` w/ voiceover | no (voiceover replaces) | excluded — voiceover drives timing |
| `montage` / `template` / `music` / `auto_music` | no (music replaces) | excluded — cutting corrupts song-absolute beat maps, lyric timings, beat-snapped overlays |

Future archetypes (`day_vlog`, `single_hero`) call the same primitive when
they are implemented.

## Architecture

```
                       ┌─────────────────────────────────────────────┐
                       │  app/pipeline/silence_cut.py  (NEW, pure)   │
                       │                                             │
  audio path ────────▶ │  detect_silences(path)                      │
                       │    ffmpeg silencedetect -vn -sn -dn         │
                       │    (extends clip_speech.py parser)          │
                       │                                             │
  Transcript.words ──▶ │  build_cut_plan(words, silences, duration,  │
                       │                 config) -> CutPlan          │
                       │    1. lexical fillers (EN+TR lexicon)       │
                       │    2. energy-positive unattributed gaps     │
                       │    3. pause tightening (residual gap kept)  │
                       │    4. padding + merge + safety caps         │
                       │                                             │
                       │  CutPlan:                                   │
                       │    keep_segments:   [(start_s, end_s), …]   │
                       │    removed:         [{start_s, end_s,       │
                       │                       reason: silence|      │
                       │                       filler_lexical|       │
                       │                       filler_acoustic}, …]  │
                       │    time_saved_s, version                    │
                       │                                             │
                       │  APPLY (13A + T3=C): the cut runs INSIDE    │
                       │  reframe_and_export(keep_segments=…) —     │
                       │  appended AFTER its fps/CFR stage (1A       │
                       │  safety: frame math only on normalized      │
                       │  streams; raw phone VFR/HEIF is the CFR-    │
                       │  before-xfade incident class). Per keep-    │
                       │  segment: trim/atrim → afade 5ms in/out at  │
                       │  cut-adjacent edges (declick) → concat      │
                       │  (sample-accurate ⇒ no cumulative A/V       │
                       │  drift). ONE encode, no new encoder-policy  │
                       │  call site.                                 │
                       │                                             │
                       │  remap_words(words, plan) -> words'         │
                       │    drop words inside removed ranges,        │
                       │    shift survivors by cumulative offsets    │
                       └───────────────┬─────────────────────────────┘
                                       │
              ┌────────────────────────┴───────────────────────┐
              ▼                                                ▼
   _render_subtitled_variant                      talking_head assembly
   (generative_build.py)                          (talking_head_assembler.py)
   transcribe original audio                      pre-cap spine (14A) →
   (verbatim prompt) + detect →                   transcribe + detect →
   CutPlan (incl. retakes) →                      CutPlan (incl. retakes) →
   reframe(keep_segments) →                       reframe(keep_segments) →
   cues from remap_words()                        usable_s from cut spine →
   minus filler tokens (15A)                      schedule_broll(anchors=cuts)
```

### Detection algorithm (deterministic, unit-testable)

Inputs: word list (whisper word timestamps), silence ranges (silencedetect),
clip duration. All thresholds are module constants — explicit over
configurable.

1. **Lexical fillers (eng review 5A + outside voice #1/#5).** One UNIVERSAL
   non-lexical vocalization set applied regardless of detected language —
   `uh, um, er, erm, hmm, mm, mhm, ıı, ııı, eee, aaa, ıh` — normalization
   COLLAPSES repeated characters first (`uhhh→uh`, `ummm→um`), so
   elongations match. `ee`/`aa` are EXCLUDED (real Turkish exclamations).
   Real words ("şey", "like", "you know") are NEVER cut in v1. Whisper-1
   returns NO per-word confidence (transcribe.py:192 hardcodes 1.0), so the
   quality guard uses SEGMENT-level signals whisper-1 verbose_json does
   return, mapped onto each segment's words: a lexical cut requires the
   word's segment `avg_logprob` above threshold AND `no_speech_prob` below
   threshold (thresholds calibrated during implementation on real clips).
   Marked `filler_lexical`.
2. **Acoustic fillers (hardened, outside voice #2).** Whisper frequently
   omits fillers without leaving a token. A gap between consecutive words
   that is NOT covered by a silencedetect range (sound but no word), and is
   0.15s–1.2s long, is marked `filler_acoustic`. Gaps >1.2s with sound are
   left alone (laughter, singing, action noise). Guards: (a) the cut path
   calls `detect_silences(noise_db=-30, min_silence_s=0.1)` — parameterized;
   `speech_coverage` keeps its 0.3s default (regression-pinned) — so short
   real silences are visible to the intersection rule instead of
   masquerading as energy; (b) **calibration gate**: if a clip yields ZERO
   silencedetect ranges (noisy footage — the detector is blind there), rule
   2 is disabled for that clip entirely (event `silence_cut_rule2_disabled`)
   — aggressiveness must never scale WITH background noise. Because these
   cuts cannot be silence-confirmed, they use the thicker `PAD_ACOUSTIC_S`
   (0.15s) on both flanks (eng review 2A).
3. **Pause tightening — dual-signal intersection rule (eng review 2A).**
   Whisper END times drift (phrase_sequence.py D16: "only starts are
   trustworthy"), so word-gap arithmetic is never sufficient on its own. A
   pause is removed ONLY where the padded word gap INTERSECTS a
   silencedetect range: `removed = (prev.end+PAD_S, next.start-PAD_S) ∩
   silence_range`. No intersection ⇒ no cut — silencedetect is the ground
   truth veto. Single removal formula (outside voice #10 — one mechanism,
   one constant): for a gap ≥ `MAX_PAUSE_S` (0.6s), `removed =
   (prev.end + KEPT_GAP_S/2, next.start − KEPT_GAP_S/2) ∩ silence_range`
   with `KEPT_GAP_S = 0.25` — the kept residual IS the padding; no separate
   RESIDUAL/PAD interplay for pause cuts. Tighten, don't eliminate;
   zero-gap cuts read as glitchy. Leading silence before the first word is
   trimmed to 0.3s; trailing silence after the last word to 0.5s (both also
   silencedetect-confirmed).
4. **Padding + merge.** Every kept word keeps `PAD_S` (0.12s) on both sides
   (0.15s for acoustic-filler flanks). Removals shorter than `MIN_CUT_S`
   (0.18s) are dropped (not worth a jump cut). Adjacent removals merge.
5. **Safety rails** (any trip ⇒ return the no-op plan + pipeline event):
   - **no real audio stream on the clip (probe `has_audio`) → skip the whole
     stage BEFORE whisper** — event `silence_cut_skipped_no_audio` (eng
     review 3A; reframe injects silent AAC for such clips, and whisper on
     digital silence hallucinates plausible words that could dodge the
     removal cap)
   - words empty → no-op; transcript-quality rail uses SEGMENT signals
     (mean `avg_logprob` / max `no_speech_prob`), NOT the `low_confidence`
     flag — that flag is inert in prod where whisper-1 hardcodes word
     confidence to 1.0 (outside voice #1)
   - per-item `silence_cut_disabled` set → skip stage (outside voice #6;
     honored by both render paths and by retake cuts; settable via the
     existing re-render request — support's per-item remedy)
   - total removal > `MAX_REMOVAL_FRAC` (0.4, shared with retake cuts) →
     no-op (`silence_cut_bailout`)
   - cut output duration < 3.0s → no-op
   - clip duration < 5.0s → no-op

Whisper call for detection uses the verbatim-bias `prompt` ("Uh, um, ııı,
eee…") so whisper-1 keeps more fillers as tokens; lexical detection then
catches them, and `remap_words` drops them from caption input.

### remap_words placement (eng review 4A)

`remap_words()` lives in `silence_cut.py` — deliberately NOT extracted into a
shared timeline utility with the two existing rebases
(`lyric_injector._select_section_lines`, `narrated_assembler`'s word rebase).
Their semantics differ (window-clamp vs voiceover→assembled vs multi-segment
deletion) and both siblings carry byte-identical prod guarantees; refactoring
them for zero behavior change is the riskiest diff in the repo. The docstring
cross-references both siblings, and a one-line comment in each points back,
so the family is discoverable. Extract a shared abstraction only when a
fourth consumer proves the shape.

### Diagram maintenance (part of the change)

- `silence_cut.py` module docstring carries the detection→plan→apply→remap
  ASCII diagram.
- `talking_head_assembler.py` header flow diagram gains the transcribe+cut
  stage — updated in the same commit.

### Why cut-then-remap (not re-transcribe)

Cuts only remove intervals strictly outside kept-word spans + padding, so
every surviving word's interior is untouched — the remap is exact arithmetic,
not approximation. One whisper call per clip instead of two; captions built
from remapped words are natively in cut-timeline coordinates. The subtitled
reburn path (`base_video_path` + persisted `caption_cues`) needs zero changes
because the base is already cut before cues are persisted.

### Integration details

**Cut placement (eng review 1A + 13A): the cut executes INSIDE the reframe
filtergraph**, never on the raw source as a separate pass.
`reframe_and_export` gains an optional `keep_segments` parameter; the
`select/aselect + setpts/asetpts` stage is appended AFTER the fps/CFR
normalization filters in its existing graph. This preserves 1A's safety
property (frame math only ever runs on CFR-normalized streams — raw phone
HEVC/HEIF-derived/VFR uploads with `avg_frame_rate=1/0` are the documented
input class behind the CFR-before-xfade incident) while paying ONE encode
instead of two and adding NO new encoder-policy call site (outside voice
 #8). Detection (whisper + silencedetect, both on the original audio track)
runs BEFORE the reframe; original-audio timestamps and reframe timeline are
identical (start_s=0 / full duration / speed 1.0 on both paths).

**subtitled** (`_render_subtitled_variant`, generative_build.py:5474):
today: reframe → transcribe base → cues → burn. becomes:
extract audio → transcribe (verbatim prompt) + detect_silences → build
CutPlan (incl. retake spans) → reframe(keep_segments=plan) → build cues
from `remap_words()` MINUS all lexicon-matched tokens (cut or not — caption
hygiene, outside voice #5) → burn. Caption correction (LLM) and resplit
operate on cues as today — timings already cut-relative.

**talking_head** (talking_head_assembler.py): select_spine (unchanged, uses
speech_coverage) → **pre-cap the spine working copy at
`min(max(target_duration_s × 2, 120s), 300s)`** before any detection —
mirrors subtitled's 300s budget cap and keeps 16kHz mono WAV under
whisper-1's 25MB limit (outside voice #9) → transcribe + detect → build
CutPlan → reframe(keep_segments=plan) → re-probe → `usable_s` from CUT
spine → `schedule_broll(usable_s, broll, anchors=cut_points)` — b-roll
windows bias onto cut positions to conceal jump cuts, existing
cadence/min-length rules unchanged (outside voice #7) →
`build_talking_head_command()` unchanged. No captions on this path.

**Compute once per job per clip (eng review 7A, adapted for 13A):**
transcript, CutPlan, and the REFRAMED+CUT output are computed on first need
and cached in the job tmpdir + an in-memory map, shared by every variant —
mirroring `_pretonemap_hdr_clips` (which exists because variants used to
tonemap the same HDR frames 3×). 1× whisper + 1× encode per clip regardless
of variant count; variants can never disagree on the cut timeline.

**Persistence:** `variants[i]["silence_cut"] = {removed, time_saved_s,
version: 1}` in `Job.assembly_plan` (task-owned, same pattern as intro_text).
`record_pipeline_event("silence_cut_plan", …)` for the admin job-debug view
(orchestrators already wrap in `pipeline_trace_for`).
**CRITICAL (outside voice #3):** `_finalize_job` rebuilds variants from an
explicit key whitelist (generative_build.py:6793) that silently strips
unlisted keys — `"silence_cut"` MUST be added to the whitelist, pinned by
`test_finalize_job_preserves_silence_cut` (same pattern as
`test_finalize_job_preserves_ai_timeline`).

### Retake / restart detection (pulled into this PR at eng review, T1=C)

Cuts abandoned takes ("wait, let me start over" → re-delivery), keeping only
the final take. This removes CONTENT, not silence — a higher risk class —
so it is separately gated and eval-backed:

- **Detector:** new LLM agent `retake_detector` (AgentSpec + prompt under
  `src/apps/api/prompts/`, registered in the agent-eval harness with
  fixtures). Input: the verbatim transcript as indexed words. Output:
  abandoned-take spans as word-index ranges + a one-line reason each.
  Deterministic mapping: word ranges → time ranges → `removed[]` entries
  with `reason: "retake"`, merged into the SAME CutPlan before apply.
- **Boundary safety:** retake removals snap outward only to
  silencedetect-confirmed boundaries or padded word boundaries — never
  mid-word; they share `MAX_REMOVAL_FRAC` with silence/filler cuts.
- **Failure isolation:** agent failure/timeout ⇒ zero retake cuts,
  silence/filler cuts proceed (event `retake_detector_failed`). Retakes can
  never block or degrade the base feature.
- **Own kill switch:** `RETAKE_CUT_ENABLED` (default `false`),
  independent of `SILENCE_CUT_ENABLED` — silence cutting ships and
  validates first; retakes flip only after its own eval pass + parity
  render.
- **Prompt-change rule applies:** `prompt_version` bump + live evals against
  fixtures before merge (repo agent-eval policy). Eval fixtures must include
  TR + EN restart patterns and near-miss negatives (repeated rhetorical
  phrases that are NOT retakes — the false-positive class that cuts real
  content).
- Per-item `silence_cut_disabled` disables retake cuts too.

### Kill switch (3-tier, follows repo pattern)

`silence_cut_enabled: bool = Field(default=False)` in config.py, read at both
render-path entry points. Off ⇒ byte-identical to today (guard-tested). Fly
apply: `fly secrets set SILENCE_CUT_ENABLED=true --app nova-video` + worker
restart. No frontend twin needed — fully automatic, no UI surface in v1.

### Apply construction: per-segment trim/atrim + concat + declick (T3=C)

Declick ships in v1 (eng review T3=C), which selects the per-segment
construction: inside the reframe graph (13A), each keep-segment becomes
`trim/atrim` pair → per-segment `afade` in/out of 5ms at cut-adjacent
boundaries (never at the clip's true start/end) → `concat`. Sample-accurate
audio joins make cumulative A/V drift structurally impossible; the 30-cut
drift e2e (11A) remains as the permanent guard assertion, and clicks are
prevented by construction rather than hoped away. Filtergraph size for a
40-cut clip (~41 segments) is well within FFmpeg limits.

### Admin cut-plan viewer (in this PR, T2=C)

`/admin/jobs/{id}` gains a per-variant timeline strip rendering
`silence_cut.removed[]`: colored bands by reason (silence / filler_lexical /
filler_acoustic / retake) with hover detail (times, reason text,
time_saved_s total). Pure read of the persisted blob — no new endpoint if
the job-debug payload already includes `assembly_plan` variants (verify at
implementation; else extend the existing debug serializer). Jest test for
band layout math; no pipeline coupling. Lives in
`src/apps/web/src/app/admin/jobs/[id]/`.

## Round 2 — user-validated behavior lock (2026-07-09/10 local test)

Tested live on a real WhatsApp talk clip; first output rejected ("ehh at 2s
survived; transitions amateur"), fixes validated and APPROVED on re-render.
As-built deltas vs the sections above:

1. **No per-word confidence floor.** Fillers naturally score low ASR
   confidence — the floor blocked 2/4 real "um"s (conf 0.03/0.46) that prod
   (whisper-1, confidence hardcoded 1.0) would cut. Guard = segment signals
   only. (Supersedes the confidence-floor half of eng review 5A.)
2. **Lexicon widened:** + eh/ah/oh/oo (+ elongations). Bare "o" excluded (TR
   pronoun); "ee"/"aa" still excluded.
3. **`MIN_KEEP_SEGMENT_S = 0.25`:** word-free keep fragments between cuts
   are absorbed (110ms three-frame flash found in testing).
4. **Alternating punch-in `KEEP_SEGMENTS_PUNCH_IN = 1.08`** on odd segments
   (pro jump-cut idiom — cuts read as intentional framing changes);
   `reframe_and_export(keep_segments_punch_in=…)`.
5. **Declick fades 5ms → 12ms.**
6. Validated numbers on the test clip: 22.9s → 14.8s (36% removed, 6 cuts),
   0 residual fillers on re-transcription, terminal A/V offset 17ms,
   punch alternation confirmed on boundary frames.
7. **Golden pin:** `tests/pipeline/test_silence_cut_golden.py` snapshots the
   clip's detection INPUTS (word timings + silence ranges — no media in git)
   and asserts the exact approved plan. Changing detection rules moves this
   pin ⇒ conscious product-behavior change + re-render review.

## Encoder policy

The cut runs inside `reframe_and_export`'s existing filtergraph (13A) ⇒ NO
new `_encoding_args` call site; reframe's intermediate `ultrafast` policy
applies unchanged. `tests/test_encoder_policy.py` untouched.

## Files touched

| File | Change |
|---|---|
| `app/pipeline/silence_cut.py` | NEW — detect, CutPlan (incl. retake merge), remap, segment-signal guards |
| `app/agents/retake_detector.py` + `prompts/retake_detector*` | NEW — retake agent + prompt + eval fixtures |
| `app/services/clip_speech.py` | parameterized `detect_silences(noise_db, min_silence_s)` (speech_coverage keeps 0.3 default; `-vn -sn -dn` kept) |
| `app/pipeline/transcribe.py` | optional `verbatim_prompt`; segment `avg_logprob`/`no_speech_prob` mapped onto words |
| `app/pipeline/reframe.py` | optional `keep_segments` → select/aselect after fps/CFR stage |
| `app/tasks/generative_build.py` | subtitled integration, per-job cache, `silence_cut` persistence + finalize whitelist entry, per-item disable |
| `app/pipeline/talking_head_assembler.py` | spine pre-cap + cut + b-roll cut-point anchors |
| `app/config.py` | `SILENCE_CUT_ENABLED` + `RETAKE_CUT_ENABLED` |
| `tests/pipeline/test_silence_cut.py` + evals | NEW — full unit matrix + retake eval fixtures |
| `tests/tasks/…` | integration + kill-switch pins + `test_finalize_job_preserves_silence_cut` |
| `src/apps/web/src/app/admin/jobs/[id]/…` | cut-plan timeline strip + Jest test (T2=C) |
| `CLAUDE.md` | env-var entries |

Scope note: the eng review's complexity smell (8+ source files) is now met —
a knowing consequence of pulling retakes into this PR (T1=C, user decision).
Mitigation: retakes are isolated behind their own flag and their failure
cannot affect the base feature.

## Test plan (100% of planned codepaths covered — eng review)

Unit (pure functions, no ffmpeg): universal-lexicon match incl. case/punct
normalization (5A); per-word `confidence < 0.5` floor blocks lexical cuts
(5A); acoustic-gap classification (gap-with-sound vs gap-with-silence vs
>1.2s exclusion) with `PAD_ACOUSTIC_S` flanks (2A); pause tightening via the
intersection rule — a word gap with NO silencedetect agreement is never cut
(2A); leading/trailing trim; padding/merge/MIN_CUT_S; every safety rail incl.
the `has_audio` pre-whisper gate (3A); remap_words exactness (dropped words,
cumulative shifts, boundary words); CutPlan no-op == identity.
Property-style: for random word/silence layouts, kept-word spans are always
inside keep_segments and remapped times are monotonic.
NOTE: no `uuid4()` (or any nondeterministic value) inside
`@pytest.mark.parametrize` — xdist collection diverges in CI (prior
learning, confidence 10/10). All ASR calls mocked — no API keys needed.

FFmpeg-level: `apply_cut_plan` command construction (select/aselect
expressions, setpts/asetpts, ultrafast preset, stream mapping) pinned like
`test_media_overlay_command.py`; silencedetect invocation keeps `-vn -sn -dn`
(pin regression of the speech-coverage learning).
**Micro-e2e [→E2E, runs in CI]:** lavfi-generated fixture (testsrc +
sine/anullsrc segments — no media committed to git), mocked ASR words, REAL
ffmpeg cut: output duration equals plan arithmetic ±1 frame; A/V stream
durations match each other.
**Drift stress e2e (outside voice #4, eng review 11A):** 30+ cuts on a
lavfi fixture, asserting terminal A/V offset < 40ms. If it fails during
implementation, switch construction to per-segment `trim/atrim + concat`
(sample-accurate) — documented fallback, same single-pass filtergraph.

**CRITICAL regression pins (IRON RULE):**
- `clip_speech.speech_coverage()` returns identical values on identical
  silencedetect output after the `detect_silences()` extraction.
- `transcribe.py` request is byte-identical when `verbatim_prompt` is absent.
- Flag OFF ⇒ byte-identical dispatch on BOTH render paths (kill-switch pins,
  same pattern as `test_generative_build_sequence.py`).

Integration: flag-on persists `silence_cut` on the variant
(+ `test_finalize_job_preserves_silence_cut` whitelist pin — CRITICAL) and
cues never overlap removed ranges; caption input strips lexicon-matched
tokens even when uncut (15A); per-item `silence_cut_disabled` skips the
stage on both paths and disables retake cuts (10A); talking_head spine
pre-cap math (14A); usable_s recomputed after cut; b-roll anchored vs
unanchored layouts pinned (12A); rule-2 calibration gate on zero-silence
clips (9A); segment-signal thresholds block lexical cuts on shaky segments
(8A); narrated self-narration inherits on both branches; music/template/
montage orchestrators never import/call the stage (guard test).
Retakes: agent-failure isolation (silence cuts proceed); RETAKE_CUT_ENABLED
off ⇒ byte-identical to silence-only; boundary snapping never mid-word;
[→EVAL] retake_detector fixtures (TR+EN restarts + near-miss negatives) run
in the agent-eval harness; prompt_version pinned.

Render verification (eng review 6A): CI micro-e2e gates every PR; a
prod-image parity render (`make local-render MODE=generative` with TR + EN
talk clips, run by someone with Docker or a one-off Fly render) is REQUIRED
before the flag flips on — not before merge (flag-off makes merge safe).
`make verify-overlays` not required (no overlay layout change) unless caption
geometry shifts.

## Failure modes

| Failure | Handling | User sees |
|---|---|---|
| whisper down/timeout | detection skipped → no-op plan + pipeline event | uncut video (today's behavior) |
| silencedetect parse failure | no-op plan + event | uncut video |
| background music in clip → no silences | empty plan, natural no-op | uncut video |
| whisper hallucination at silence | cuts require silencedetect agreement (dual signal) for pauses; acoustic fillers bounded ≤1.2s | worst case: one short awkward cut |
| over-aggressive plan | MAX_REMOVAL_FRAC 0.4 bail-out + event | uncut video |
| ffmpeg cut failure | catch → fall back to original clip + event | uncut video |
| A/V drift | single-filtergraph select+aselect on same ranges; frame-accurate re-encode | none (by construction) |

Every fallback is fail-open to today's behavior — the feature can only make
the video shorter, never fail the job.

## NOT in scope (deferred, with rationale)

- **Montage/b-roll dead-air trimming** — different feature (visual pacing,
  not speech); interacts with Gemini best_moments + beat slotting.
- **UI aggressiveness slider / visible per-job toggle** — v1 ships the
  API-level per-item `silence_cut_disabled` (support remedy, eng review 10A);
  a user-facing UI affordance comes only after output quality is validated.
- **Discourse-word removal ("like", "you know", TR "şey")** — real words;
  cutting them needs context-aware LLM judgment, high false-positive cost.
- **CrisperWhisper verbatim ASR** — new model dependency (GPU) for marginal
  gain over dual-signal heuristic; revisit if acoustic-filler precision
  disappoints.
- *(declick and the admin cut-plan viewer were originally deferred here;
  both pulled into this PR at eng review — T3=C, T2=C.)*

## What already exists (reused, not rebuilt)

- `clip_speech.py` silencedetect invocation + stderr parser (extended to
  return ranges; `-vn -sn -dn` learning preserved)
- `transcribe.py` word-level timestamps (whisper-1 verbose_json / faster-whisper)
- `PAUSE_GAP_S` precedent in phrase_sequence.py (0.35s meaningful-gap floor)
- lyric_injector window-rebase + narrated_assembler word-rebase precedents
  (generalized by `remap_words`)
- kill-switch 3-tier pattern, `pipeline_trace_for`/`record_pipeline_event`,
  encoder-policy test harness, `reframe_and_export` (unchanged consumer)

## Implementation Tasks
Synthesized from the eng review's findings. Each task derives from a specific
finding. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~2d / CC: ~30min)** — pipeline — Build `silence_cut.py`: CutPlan detection (lexicon + acoustic + pause intersection), remap_words, safety rails, module diagram
  - Surfaced by: plan core + reviews 2A/3A/5A/8A/9A
  - Files: `app/pipeline/silence_cut.py`, `tests/pipeline/test_silence_cut.py`
  - Verify: `cd src/apps/api && pytest tests/pipeline/test_silence_cut.py`
- [ ] **T2 (P1, human: ~2h / CC: ~10min)** — services — Parameterize `detect_silences(noise_db, min_silence_s)`; speech_coverage regression pin
  - Surfaced by: review 9A + IRON RULE
  - Files: `app/services/clip_speech.py`
- [ ] **T3 (P1, human: ~3h / CC: ~15min)** — pipeline — `verbatim_prompt` param + segment `avg_logprob`/`no_speech_prob` onto words; byte-identical default pin
  - Surfaced by: outside voice #1 (8A) + IRON RULE
  - Files: `app/pipeline/transcribe.py`
- [ ] **T4 (P1, human: ~1d / CC: ~25min)** — pipeline — reframe `keep_segments`: per-segment trim/atrim+concat + 5ms afade declick; micro-e2e + 30-cut drift e2e (<40ms)
  - Surfaced by: reviews 1A/13A/11A + T3=C
  - Files: `app/pipeline/reframe.py`
- [ ] **T5 (P1, human: ~1d / CC: ~30min)** — tasks — Subtitled integration: per-job cache, persistence + finalize whitelist + preserve pin, caption hygiene, per-item disable
  - Surfaced by: outside voice #3/#5/#6 (10A/15A) + review 7A
  - Files: `app/tasks/generative_build.py`
- [ ] **T6 (P1, human: ~1d / CC: ~20min)** — pipeline — talking_head: spine pre-cap, cut via keep_segments, b-roll cut-point anchors, header diagram update
  - Surfaced by: outside voice #7/#9 (12A/14A)
  - Files: `app/pipeline/talking_head_assembler.py`
- [ ] **T7 (P1, human: ~2d / CC: ~40min)** — agents — `retake_detector` agent + prompt + TR/EN eval fixtures (incl. near-miss negatives); merge into CutPlan; `RETAKE_CUT_ENABLED` isolation
  - Surfaced by: user decision T1=C
  - Files: `app/agents/retake_detector.py`, `prompts/`, `tests/evals/`
- [ ] **T8 (P1, human: ~1h / CC: ~5min)** — config — Both flags + kill-switch byte-identical pins + CLAUDE.md env entries
  - Surfaced by: kill-switch pattern
  - Files: `app/config.py`, `CLAUDE.md`
- [ ] **T9 (P2, human: ~1d / CC: ~20min)** — web-admin — Cut-plan timeline strip (reason-colored bands) + Jest layout test
  - Surfaced by: user decision T2=C
  - Files: `src/apps/web/src/app/admin/jobs/[id]/`
- [ ] **T10 (P2, human: ~2h / CC: ~10min)** — tests — Guard: music/template/montage orchestrators never invoke the stage
  - Surfaced by: scope decision D2
  - Files: `tests/tasks/`

### Worktree parallelization
Lane 1 (parallel worktrees): T1 ∥ T2 ∥ T3 ∥ T4 ∥ T7 ∥ T9 — no shared modules.
Lane 2 (after Lane 1 merges): T5 → T6 **sequential** — ⚠️ both touch
`generative_build.py` (talking_head dispatch lives there). T8, T10 ride with
Lane 2. Launch: 6 parallel → merge → T5 → T6 → T8/T10.

## Rollout

1. Land behind `SILENCE_CUT_ENABLED=false` + `RETAKE_CUT_ENABLED=false`
   (CI micro-e2e + drift e2e green on every PR; retake evals green).
2. REQUIRED before silence flag flip (6A): prod-image parity renders on TR +
   EN talk clips (`make local-render MODE=generative`, on a Docker-capable
   machine, or a one-off render on Fly) — listen for clipped word tails and
   clicks at cut boundaries; verify captions show no filler tokens and TR/EN
   language detection is stable with the verbatim prompt (15A).
3. Flip `SILENCE_CUT_ENABLED` on Fly (api + worker restart), watch admin
   job-debug `silence_cut_plan` events + `time_saved_s` distribution +
   bail-out/rule2-disabled rates for a day.
4. Only after silence cutting is validated in prod: flip
   `RETAKE_CUT_ENABLED` separately, after its own eval pass + parity render.
5. Bail-out rate > ~10% or complaints ⇒ flip off (no deploy needed);
   per-item complaints ⇒ set `silence_cut_disabled` on that item and
   re-render (no global impact).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | ISSUES_ABSORBED | outside voice (Claude subagent): 10 findings, 8 decided + 2 folded |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 17 issues, 0 critical gaps open, 0 unresolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** Codex CLI not installed — outside voice ran as an independent Claude subagent; all 10 findings user-adjudicated (8 accepted via D8–D15, finalize-whitelist + spec-formula folded as factual fixes).
- **CROSS-MODEL:** 8 tension points between the review's locked decisions and the outside voice; every one resolved toward the hardened option (segment signals, noisy-clip calibration gate, per-item disable, drift e2e, b-roll anchors, cut-inside-reframe, spine cap, caption hygiene).
- **VERDICT:** ENG CLEARED — ready to implement. Scope locked to speech render paths (D2); user expanded PR scope with retakes, admin viewer, declick (T1/T2/T3 = build now).

NO UNRESOLVED DECISIONS
