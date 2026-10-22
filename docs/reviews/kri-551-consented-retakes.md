# KRI-551 consented retake detection

Parent incident: KRI-829. This is a bounded local fix for the `required_v1`
speech-cleanup preflight path; it does not establish a production rollout or
native export.

## Acceptance and failure modes (recorded before implementation)

For the N3 narration, after the creator has explicitly accepted *retake-aware*
cleanup, an abandoned false start such as “At kilometer 30 … sorry” is offered
and removed when the existing retake detector returns its word span. The later
intended sentence, “At kilometer 32 …”, remains. The public receipt counts the
retake.

The following outcomes remain wrong:

- an accepted `required_v1` analysis silently has zero retake candidates even
  when the injected detector returns a valid span;
- a detector failure, malformed span, disabled retake flag, or rhetorical
  repetition removes real speech or prevents the normal analysis;
- a policy/detector change reuses a ready analysis and prior consent;
- the detector sees a transcript with different indexes from the cut planner.

The affected boundary is source audio → timed Whisper words → retake detector
→ immutable cleanup analysis → consent card/accepted snapshot → cut planner →
render. This change owns detector integration, policy identity, explicit consent,
and the iOS disclosure/count presentation. Original end-to-end rendering, live
detector precision, and rollout remain with the parent incident.

`RETAKE_CUT_ENABLED` is already used by legacy auto-cut paths and cannot alone
authorize this new meaning: existing iOS consent says a restarted sentence
stays. `SPEECH_CLEANUP_RETAKES_ENABLED` therefore defaults off and must combine
with the legacy flag. Its effective value belongs in source policy identity, so
either enable or disable retires a ready snapshot and its old consent. A
retake-aware client acknowledgement is included: a new clean decision on an
analysis containing retakes requires the explicit acknowledgement. Old clients
can keep original speech; they receive an actionable update message if they try
to accept those removals. The acknowledgement is omitted when false, preserving
compatibility with an older strict API. Deployment and original-output
acceptance remain prerequisites before enabling this flag.

## Six timing decisions

1. **Duration source:** `SpeechCleanupAnalysisInput.duration_s` and its source
   window remain canonical; retake spans are word indexes projected through the
   already-produced `timed_words`, never independently timed intervals.
2. **Ripple policy:** no timeline entries are inserted or moved. A selected
   retake removal enters the existing `CutPlan`, whose existing keep/remove
   projection supplies the render ripple; unrelated lanes keep their existing
   speech-cleanup projection rules.
3. **Scrub bounds:** unchanged. Editor playhead, seek limits, ruler, and
   output/base mapping continue to use the persisted selected cut plan.
4. **Resize behavior:** not applicable: this path has no user resize gesture.
   The existing planner owns interval boundary choice and minimum-output rail.
5. **Undo behavior:** unchanged. Accepting an analysis retains the existing
   snapshot/consent boundary; detector execution creates no additional client
   undo state.
6. **Preview/render parity:** the immutable selected `CutPlan` is shared by
   receipt, preview and render. Focused tests require a valid detector span to
   become a `retake` finding/removal and require disabled/failing/invalid paths
   to preserve existing behavior.

## Case matrix and evidence limit

Offline cases cover the N3-shaped valid restart, detector error, malformed or
out-of-range spans, feature flag off, English rhetorical repetition, and
Turkish rhetorical repetition. The original local N3 audio is replayed with its recovered immutable
production words; replacement-ASR diagnostics are explicitly superseded. No
synthetic transcript is treated as the original source. Live detector accuracy, native output, deployment, and the parent
incident's end-to-end acceptance are pending KRI-829.

### Red control

On 2026-10-22, offline `whisper-cli` (local medium model, CPU) transcribed the
original N3 source `N3_07_voiceover.m4a`. Its 14.24–27.32 s segment contains
both the abandoned kilometer-30 start and the later kilometer-32 delivery. Its
word-timestamp output places the false start from 14.70 through 19.79 s and
the retained second delivery begins at 20.27 s. The pre-change preflight
construction passed no retake spans to the planner, which is the recorded
missing-positive control. The temporary transcript is outside the repository;
this summary intentionally retains only the needed source and timing evidence.

### Replacement-ASR diagnostic — superseded, not an acceptance pass

Captured production timed words and detector output were unavailable in the
stress-kit paths, so the initial diagnostic used a **local Whisper medium CPU replay**.
The original analysis was subsequently recovered read-only from production;
it disproved these replacement word timings, as detailed below.
It is not production ASR, a paid detector run, a native export, or deployment
evidence. Scratch artifacts (including the source-derived audio) stay outside
git at `/private/tmp/kri-829-evidence/n3-retakes`.

Source: `N3_07_voiceover.m4a`, SHA-256
`c9d0e89a84022d74ac8405d91f83f05cdede46d69d3422bcc94c0ed019adfc59`,
ffprobe duration `47.420 s`. Local Whisper's original-word replay identified
the discarded span as word indexes `30..33`, source `14.70..19.79 s`; the cut
allocator widened its safe removal to `14.06..20.15 s` and retained the next
delivery beginning at `20.27 s`.

Commands (run from the KRI-551 worktree):

```bash
ffmpeg -y -i /Users/yasinberkyesilyurt/Downloads/kria-stress-test/upload/N3_marathon_narrated/N3_07_voiceover.m4a -ac 1 -ar 16000 /private/tmp/kri551-n3-original.wav
/opt/homebrew/bin/whisper-cli -ng -m /Users/yasinberkyesilyurt/Downloads/kria-boat-test/_src/work/ggml/ggml-medium.bin -f /private/tmp/kri551-n3-original.wav -l en -ojf -of /private/tmp/kri551-n3-original-words -np
env PYTHONPATH=/private/tmp/nova-kri-551/src/apps/api /private/tmp/nova-kri-551/src/apps/api/.venv/bin/python /private/tmp/kri-829-evidence/n3-retakes/run_diagnostic.py
/opt/homebrew/bin/whisper-cli -ng -m /Users/yasinberkyesilyurt/Downloads/kria-boat-test/_src/work/ggml/ggml-medium.bin -f /private/tmp/kri-829-evidence/n3-retakes/n3-off.wav -l en -otxt -of /private/tmp/kri-829-evidence/n3-retakes/n3-off-transcript -np
/opt/homebrew/bin/whisper-cli -ng -m /Users/yasinberkyesilyurt/Downloads/kria-boat-test/_src/work/ggml/ggml-medium.bin -f /private/tmp/kri-829-evidence/n3-retakes/n3-on.wav -l en -otxt -of /private/tmp/kri-829-evidence/n3-retakes/n3-on-transcript -np
```

`apply_speech_cleanup_to_audio` produced the actual off/on WAVs. ffprobe
reported `47.420 s` off and `41.330 s` on: the expected `6.090 s` reduction.
Independent local Whisper transcription retained “kilometer thirty … sorry …
thirty-two” off, and read only “kilometer thirty-two” on. The real
`remap_words` helper also removed the false-start words and moved the retained
`32` word from `22.37..23.51 s` to `16.28..17.42 s`, a `6.09 s` shift matching
the exact selected removal. This verifies only application of the supplied
replacement timings. It does not establish safe retention of the actual
sentence, detector precision, or rendered-caption appearance.
The diagnostic deliberately disabled silence/filler removal and injected the
reviewed retake span. It isolates retake application; it does not establish
combined pause/retake cleanup or the live detector's selection accuracy.

### Original record contradicts the replacement timings

A read-only transaction retrieved analysis
`a9628062-b1e6-4f1d-9f50-df5738fa46c0` for original N3 item
`3b264e82-329b-404a-9cf2-2694f5d423b1`. It is ready, detector `mixed-gap-v4`,
source window `0..47.42 s`. Its immutable words put the abandoned take at
indexes **28..32**, ending with “sorry” at **17.400 s**. The retained sentence
starts “At” at **19.700..20.440**, “kilometer” at **20.440..20.800**, and “32” at
**20.800..21.420**. The initial local replacement transcript instead placed
those words substantially later.

The combined replacement-ASR diagnostic selected a retake cut at
`14.06..20.15` and an acoustic-filler cut at `20.56..21.35`, with 11.6475 s total
removal and a 35.7725 s output. Independent retranscription read “At 2 …”.
Comparing retained later PCM samples alone had suggested recognizer variation,
but **the original word clock shows both cuts overlap intended speech**.
Neither that PCM comparison nor the isolated replacement-ASR transcript is
accepted as preservation evidence. This is a failed diagnostic, not a new
cut-planner fix: the implementation must be replayed against the recovered
original words before claiming safe N3 output.

Private source data and diagnostics remain outside git under
`/private/tmp/kri-829-evidence/n3-retakes` and `n3-retakes-combined`.

### Replay using the original production analysis

The authoritative replay uses the recovered immutable timed words, original
source audio, normal `required_v1` settings and the real silence detector.
Only the reviewed retake-detector answer (indexes **28..32**) is injected;
there is no new ASR or live detector call. The saved historical cut plan is
applied as the independent baseline before running the current engine.

| Measurement | Recorded baseline | Current combined replay |
| --- | --- | --- |
| Removed duration | 14.307879 s | 16.327879 s |
| Actual FFmpeg output | 33.112125 s | 31.092125 s |
| Retake removal | None | 13.8999997–19.5800008 s |
| Retained “32” after remap | 13.582122–14.202123 s | 11.562123–12.182123 s |
| Retained “medal” after remap | 25.632121–25.792121 s | 23.612122–23.772122 s |
| Retained “12” after remap | 24.102122–24.382121 s | 22.082123–22.362122 s |

Silence detection reports `ok`; the candidate is ready and selected, without
a bailout. The retake ends 0.12 s before the retained “At”. No selected removal
overlaps the full original phrase at `19.7000008..21.4200001 s`. Independent
local retranscription reads “At kilometer 32, my legs just stopped” without
the kilometer-30/sorry start, and retains the later time and medal phrase.
This retranscription is an output check, not a replacement for original timings.

A separate full-phrase PCM comparison verifies actual audio preservation:
source `19.700000..21.420000` versus output `10.462125..12.182125`, aligned to
48 kHz whole-sample boundaries, are byte-identical: 82,560 samples / 165,120
bytes, SHA-256
`eaa33ee265fc46bc488a7fb080bbde56472cebeb11b4634c2ca4618b1bcc1f53`.
The earlier replacement-ASR failure does not reproduce with the original
analysis. This proves the local cut and retained phrase for the injected
answer, not live detection, auditory review, rendered captions, photo timing,
mixing, recovery or an iPhone export.

Exact reproducible script/report and private WAVs are retained outside git in
`/private/tmp/kri-829-evidence/n3-original-analysis-replay`:

```bash
env PYTHONPATH=/private/tmp/nova-kri-551/src/apps/api \
  src/apps/api/.venv/bin/python \
  /private/tmp/kri-829-evidence/n3-original-analysis-replay/run_original_analysis_replay.py
```

## Validation commands

### Named-line receipt wording

The old receipt said that a retake or specific line "isn't cut automatically yet." That
became false once the consented retake path could record a retake removal, while a count still
cannot prove that the removed span is the creator's requested sentence. The receipt now says it
has not confirmed that exact spoken line was removed and directs the creator to check the editor.
It remains `partial` for every named-line request.

The red control first changed only the four receipt test files. Before the wording change, the
focused command below failed seven assertions: the old English and Turkish copy appeared, the
bound N3-shaped brief with one recorded restart contradicted the new copy, and an unrelated
restart did not carry the required unverified-line wording. After replacing the literals,
the same command passed all 320 tests. The N3 control records one `retake`, reports "1 restarted
sentence", contains no "isn't cut automatically" claim, and keeps the request `partial`; the
unrelated-retake control also remains `partial`.

```bash
bash scripts/check-api.sh test tests/kria/test_kri520_receipts.py tests/kria/test_brief_talking_receipts.py tests/kria/test_brief_marathon_receipts.py tests/tasks/test_kri551_cleanup_review_note.py
```

The following focused checks passed while this worktree contained the KRI-551
engine, worker, policy, and compatibility changes. The diagnostic above is
separate offline replay evidence, not a test-suite replacement.

```bash
bash scripts/check-api.sh test tests/test_config.py tests/services/test_speech_cleanup_preflight.py tests/tasks/test_speech_cleanup_analysis.py tests/services/test_speech_cleanup_language_crosscheck.py tests/services/test_speech_cleanup_selection.py tests/test_speech_cleanup_worker_contract.py
# 136 passed

bash scripts/check-api.sh test tests/services/test_speech_cleanup_identity.py tests/services/test_speech_cleanup_analysis_lifecycle.py tests/services/test_speech_cleanup_decision.py tests/services/test_speech_cleanup_outcome.py tests/agents/test_retake_detector.py tests/evals/test_retake_detector_evals.py
# 114 passed; 2 positive-fixture empty-output checks are not applicable

bash scripts/check-api.sh test tests/tasks/test_speech_cleanup_analysis.py tests/services/test_speech_cleanup_preflight.py tests/services/test_speech_cleanup_retakes_consent.py
# 94 passed after owner-context repair

bash scripts/check-api.sh lint check app/config.py app/pipeline/speech_cleanup_analysis.py app/tasks/speech_cleanup_analysis.py app/services/plan_item_media.py app/services/speech_cleanup_selection.py tests/services/test_speech_cleanup_preflight.py tests/tasks/test_speech_cleanup_analysis.py
bash scripts/check-api.sh lint format app/config.py app/pipeline/speech_cleanup_analysis.py app/tasks/speech_cleanup_analysis.py app/services/plan_item_media.py app/services/speech_cleanup_selection.py tests/services/test_speech_cleanup_preflight.py tests/tasks/test_speech_cleanup_analysis.py
```

### Integrated validation

- Final complete retake journey after merging current main `a33c4b21b`:
  **986 passed, no skips**, using every `api_tests` path in the
  `kri-551-consented-retakes` journey. The broader preship initially found
  four stale receipt-copy assertions in three additional suites; their exact
  unverified-line assertions were updated while preserving `partial` status.
  Those suites passed **80 tests** before the full 986-test run.
- The final entrypoint audit found a reachable direct Creator-confirm bypass
  outside chat v1/v2. A red controller test reached the later ownership guard
  instead of refusing undisclosed retakes. The shared consent predicate now
  fences this direct path too, before reserving an attempt. It checks only the
  matching current analysis, preserving legacy pending/no-analysis behavior;
  acknowledgement `true` is part of idempotency while absent/false retains the
  old digest. The full route/service follow-up passed **373 tests**.
- Backend engine/parser/identity/lifecycle/consent/outcome/v1/v2 checks:
  **384 passed, 2 skipped**. The skips are positive-fixture empty-output checks
  that do not apply, not live provider checks. Receipt suites separately passed
  **320 tests**, including the contradictory-copy red control above.
- Focused native tests: **28 passed** (`ApprovalDecisionRequestTests` and
  `SpeechCleanupOfferTests`, iPhone 17 Pro simulator). This is test execution,
  not a source-video export.
- `PLAYWRIGHT_BROWSERS_PATH=/private/tmp/kri-829-playwright make verify-editor-timeline`:
  **209 web unit tests, 28 desktop browser tests, 42 mobile-viewport browser
  tests and 583 backend tests passed**. The mobile cases use browser emulation;
  they do not establish actual iPhone behavior. The editor checks include
  repeated chat edits, Save and persisted receipts on their test fixtures.
- Web `npx tsc --noEmit`, generated-contract verification, and
  `python3 -m unittest scripts/ci/test_select_tests.py -q` (**41 tests**) passed.
- A separate read-only review found no actionable issue in word indexing,
  malformed-span rejection, policy invalidation, old/new API compatibility or
  same-analysis consent reuse. This does not replace the missing original
  live-detector and device checks.

Initial browser runs failed before execution because sandbox socket access and
the browser binary were unavailable. The final run used permitted local-service
access and isolated browser downloads and passed. The original physical iPhone
proof remains blocked by missing Xcode developer-account provisioning, as
recorded in the [parent acceptance ledger](kri-829-original-output-acceptance.md).

## Current-main fixture and schema correction

Exact-source hosted CI's14 failures reproduced locally (376 other cases passed). Current-positive synthetic analysis rows mixed current outer detector v5 with historical inner v4; unchanged hydration correctly rejected them. The test builder now explicitly constructs a synthetic current inner/outer identity. The original historical guided_voiceover_mixed_gap_v2.json is byte-unchanged, still asserted as v4 and distinct from current policy. No historical production consent is relabeled.

Additive expectations retain retakes:0, default speech_cleanup_retakes_aware:false, the new sixth EN/TR consent-required message and the explicit v5 detector guard. Every previous assertion remains represented; stale-policy retirement, malformed snapshot, consent and downstream failure guards are unchanged. No production implementation changes are part of this correction.

After correction,441 existing affected and neighboring hydration/policy/consent/localization tests pass. The complete make verify-editor-timeline gate also passes:209 web tests,28 desktop browser tests,42 mobile-viewport browser tests and583 backend tests. Browser execution used one worker and a fresh isolated test server; the temporary harness options were restored, no assertion, retry or timeout was changed, and the config is not committed. Browser result bundles and raw logs remain task-local; this is not physical-iPhone output or live detector accuracy. New current-head hosted checks remain required before merge.
