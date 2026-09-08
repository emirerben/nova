# Chat speech-cleanup rollout

Chat speech cleanup is dark by default and advances only through signed,
production-derived gate receipts. A configured percentage is not proof that the
dedicated worker or render contract works. Never change the next cohort until the
audit prints `PASS` and its immutable receipt has been archived outside the repo.

## Gates and sequence

Deploy migration, API, web, Beat, and the dedicated `speech_analysis` Fly process
with preflight `off/0` and mixed-gap `off/0`. Verify the process consumes only
`speech-analysis`. Then collect a 100% internal shadow window for both preflight and
mixed-gap before the first enforcement cohort.

Advance one adjacent stage at a time:

```text
shadow/100 -> enforce+apply/1 -> 10 -> 25 -> 50 -> 100
```

For every stage, use a fresh evidence window no longer than 48 hours and finish the
audit within 15 minutes of that window. The window must contain:

- a live worker inspection showing at least one `speech-analysis` consumer, plus a
  canary task published and completed on the same API/worker revision;
- at least five analyses, unexpected failure rate at or below 5%, queue p95 at or
  below 120 seconds, and run p95 at or below 900 seconds;
- zero stuck leases, snapshot mismatches, invalidation errors, duplicate dispatches,
  or private-payload leaks;
- controlled source receipts for embedded-video speech, Narrated upload/recording,
  and audio-only Narrated analysis followed by video attachment;
- controlled outcome receipts for `applied`, `checked_no_change`, `declined`,
  `bypassed_unchecked`, and `failed`.

The intentional failed-output canary belongs in `outcome_receipts.failed`; do not
count it as an unexpected analysis failure. Export only aggregate scalars. Never put
media paths, URLs, transcript text, timed words, cut intervals, exception messages,
user identifiers, or raw rows in the evidence file. The exact JSON shape is pinned
by `app/services/speech_cleanup_rollout.py`; unknown fields are rejected.

At the 50% -> 100% gate, also perform the rollback drill below during the same
window, restore 50% after verification, and record that the already-stamped Jobs
finished under their immutable contracts. General availability is not complete
until the signed 100% receipt and the referenced production artifacts are archived.

## Produce the signed receipt

Use a dedicated 32-byte-or-longer HMAC secret. It must not equal the admin, internal,
or training-export secret. Keep the secret out of shell history and the repository;
load it through the operator's secret manager as
`SPEECH_CLEANUP_ROLLOUT_RECEIPT_SECRET`. Set a rotation identifier in
`SPEECH_CLEANUP_ROLLOUT_RECEIPT_KEY_ID`.

From `src/apps/api`:

```bash
python scripts/audit_chat_speech_cleanup_rollout.py \
  --evidence /secure/speech-cleanup/evidence-10-to-25.json \
  --target-percent 25 \
  --output /secure/speech-cleanup/receipts/25.json
```

The command creates the receipt with mode `0600` and refuses to overwrite it. Exit
0 means `PASS`; exit 2 means a signed `HALT`; exit 1 means unsafe/malformed input and
no usable gate. A `HALT` signature records what was observed but never authorizes a
promotion. Copy both the receipt and links to the immutable metrics/log artifacts to
the release record. Do not commit either file.

After a `PASS`, change both controls to the receipt's target together and restart the
API/worker process groups:

```text
SPEECH_CLEANUP_PREFLIGHT_MODE=enforce
SPEECH_CLEANUP_PREFLIGHT_ROLLOUT_PERCENT=<target>
SPEECH_CLEANUP_MIXED_GAP_MODE=apply
SPEECH_CLEANUP_MIXED_GAP_ROLLOUT_PERCENT=<target>
```

Re-read the resolved production configuration after restart, then begin a new
evidence window. Never skip a stage or reuse a prior window/receipt.

## Rollback and drill

The emergency kill switch is:

```text
SPEECH_CLEANUP_PREFLIGHT_MODE=off
SPEECH_CLEANUP_PREFLIGHT_ROLLOUT_PERCENT=0
SPEECH_CLEANUP_MIXED_GAP_MODE=off
SPEECH_CLEANUP_MIXED_GAP_ROLLOUT_PERCENT=0
```

Apply all four values together and restart the API plus relevant workers. Confirm a
new chat source follows the old path and no new preflight task is published. Do not
rewrite `SpeechCleanupAnalysis`, PlanItem decisions, Job snapshots, or in-flight
Jobs: already-dispatched Jobs finish or fail under their stamped `required_v1` /
`off_v1` contract. Keep the dedicated worker running until its queue and leased work
are drained; stopping it is not the kill switch.

### Removal-cap lever (gentler than the kill switch)

`SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED` (default `1.0` since 2026-09-08) caps how
much of a clip an explicit-consent (`required_v1`) plan may remove, as a fraction of the
runtime. At the default there is no fraction cap: `MIN_OUTPUT_S` (3.0 s) is the only
rail, and a `silence_cut_clamped` receipt means that floor bound the plan. If cleanup is
cutting more than creators want, restore the previous ceiling without turning anything
off:

```bash
fly secrets set SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED=0.55 --app nova-video
# + restart api and worker
```

The value is hashed into `source_policy_fingerprint`, so flipping it retires `ready`
analyses, re-collects creator consent, and reshuffles preflight cohorts exactly like a
`DETECTOR_VERSION` bump — start a fresh evidence window afterwards and never compare
across the flip. Stamped Jobs finish under their own persisted `cut_plan`. This lever
does NOT touch the auto/legacy path, which keeps its separate `MAX_REMOVAL_FRAC` 0.4
bailout rail.

**This lever is about taste, not speech safety.** Until 2026-09-08 the 0.55 cap was also
— unintentionally — what stopped the detector cutting quiet speech that silencedetect's
absolute −30 dBFS floor mis-reports as silence on soft-spoken and lapel-mic takes. That
job now belongs to two guards in `app/pipeline/silence_cut.py`:
`TOKEN_SPLIT_VOICE_RATIO = 0.5` + `TOKEN_SPLIT_PIECE_MAX_S = 0.35` (rule 0 carves ONE interior span per token, and only when both remnants are slivers whose total is at most half the carve; edge trims are refused outright — a carve at a token boundary is indistinguishable from a quiet onset, and edge trims were ~94% of the real speech an earlier, looser guard still destroyed) and `MIN_KEEP_SPEECH_SEGMENT_S = 0.6` (a short word-bearing
keep segment is widened, never absorbed). So triage a report accordingly:

- "cleanup left a pause in" → budget. Read the receipt: `clamped=true` with the span in
  `proposed_removals` and absent from `removed` is a `MIN_OUTPUT_S` decline.
- "cleanup is too aggressive for my taste" → this lever.
- **"cleanup cut a word / clipped my speech" → a guard bug, NOT this lever.** Do not
  reach for `=0.55` to make it stop; that only hides it again, on some clips, by
  evicting the bad carve on budget grounds. Capture the source and the persisted
  `timed_words`, and reproduce against `TestRuleZeroCannotCutRealSpeech` in
  `tests/pipeline/test_silence_cut_asr_timestamp_golden.py`. Use the kill switch
  (`SILENCE_CUT_ENABLED=false`) if it needs to stop now.

For the mandatory pre-100% drill, prove off/0, verify one already-stamped clean Job
still uses its exact snapshot, then restore the prior 50% settings. Record completion
time and `inflight_contracts_preserved=true` in the evidence. If any observation is
unknown, leave the rollout at the prior stage (or off/0) and investigate; unknown is
never treated as healthy.

## Local verification

```bash
cd src/apps/api
python3 -m pytest \
  tests/scripts/test_audit_chat_speech_cleanup_rollout.py \
  tests/test_speech_cleanup_worker_contract.py -v
ruff check app/services/speech_cleanup_rollout.py \
  scripts/audit_chat_speech_cleanup_rollout.py \
  tests/scripts/test_audit_chat_speech_cleanup_rollout.py \
  tests/test_speech_cleanup_worker_contract.py
ruff format --check app/services/speech_cleanup_rollout.py \
  scripts/audit_chat_speech_cleanup_rollout.py \
  tests/scripts/test_audit_chat_speech_cleanup_rollout.py \
  tests/test_speech_cleanup_worker_contract.py
```

These checks prove the gate behavior, not production health. Production receipts and
the rollback drill remain operator work and must never be fabricated from fixtures.
