# KRI-789 / KRI-790 combined boundary evidence

This is offline verification of quoted trim intent and its native recipe/receipt
consumer. It does not establish fulfillment of the whole original Merope T1 request
or a physical-iPhone export. Parent KRI-787 retains those gaps.

The unmodified recorded Main Creator reply from the KRI-789 eval fixture was passed
through real `MainCreatorAgent.parse`, `apply_updates`, `take_window_ask`,
`apply_take_window`, `compile_phone_subtitled_plan`, `estimate_take_window` and
`build_receipts`. Provider transport alone was replayed; these responsible boundaries
were not replaced with mocks. The source word timings are the existing KRI-790
Merope T1 fixture. The independent approximate 35-second request remained a distinct
fact; no arbitrary content was removed to force that exact length.

Observed combined result:

| Cleanup | Kept source window | Recipe duration | First / last kept words |
| --- | --- | --- | --- |
| Off | 12.265–50.380 seconds | 38.115 seconds | `Hi, I'm Carolina from` / `here all week.` |
| On | Same outside boundaries, existing internal cleanup cuts retained | 35.335 seconds | Same first and last words |

Both cut receipts say applied. The unrelated introduction and trailing `Nice`/bite
content are excluded. The draft receipt says the requested part is about 38.6 seconds
and the creator asked for 35 seconds, rather than measuring the full 68.4-second take.
The pre-approval estimate uses recorded sentence timings; the compiler uses the
word-grounded interval. Existing assertions preserve that distinction.

Executed integrated checks: 319 passed, zero skips/failures across the original
`test_phone_take_window`, start-cut, cleanup-off worker, KRI-789 parser, Talking
receipts, EN/TR, caption-spelling, PostgreSQL runtime, and reliability-gate tests.
The existing tests retain wrong-name/short-near-match, missing line, reversed line,
route/text exclusion, older-render receipt, cleanup on/off and caption-remap controls.

The unchanged broader editor timeline contract passed on the producer input:
209 web tests, 28 desktop Playwright cases, 42 mobile Playwright cases and 583 backend
timeline/commit tests. No browser/editor timing source differs in this consumer.

Integration preserves main's caption-spelling plan alongside the new take window,
all original negative assertions, and all main reliability gates. Producer KRI-789
must land before this dependent consumer. The authoritative request fixtures are
`tests/fixtures/agent_evals/main_creator/kri789_one_take_start_end_lines_trim.json`
and `tests/fixtures/kri790_merope_t1_take.json` under `src/apps/api`.

No new live-model call, original-media upload, physical-phone action, deployment or
full-request/native-export completion claim was made. The original take was not
available at the historical author's local kit path in this source-owner workspace.

## Landed spoken-trigger/speaker reconciliation

The local consumer now incorporates main 049bdd0a6 through the reconciled producer,
retaining caption spelling, spoken triggers/speaker disclosure and the quoted
window. No extra consumer conflict was needed. All 358 changed-boundary checks
passed, including the real PostgreSQL runtime and the preserved forged-answer
controls. The real parser-to-compiler/receipt replay still keeps 12.265–50.380
seconds (38.115 seconds without cleanup, 35.335 with cleanup) and retains the
separate requested 35 seconds. Prompt versions are v67 / extractor v15. Existing
hosted native runs remain attached to the preceding source and are not claimed
as checks of this newer integration. Publication waits for those runs to finish.

## Landed recovery and transcript-language inputs

The later main 8eefea56d adds KRI-795 refusal re-asking, KRI-805 brief-answer
grounding and KRI-808 source-language transcription. The consumer merges both the
new speech-segment helpers and its quote-grounding function without conflict.
Changed-boundary checks: 490 passed, one intentional skip; 13 direct neighboring
speech-segment tests also passed. The skipped KRI-808 kill-switch parameter is the
non-production first-pass fixture; that control intentionally only runs for the
production replay, not an unavailable database or a failing implementation.
The real parser/compiler/receipt chain still has the same 12.265–50.380-second
window and 38.115/35.335-second output durations. No provider was called.

## KRI-829 continuation — 22 October 2026

Merged current main `12d4e7662` into the existing source history. The initial
preship reproduced a duplicate `kri-789-take-trim-not-order` journey introduced
by the earlier integration; its two records were byte-equivalent. Removed only
the duplicate, retaining all paths and checks. The selected journey validator
must accept the resulting unique list.

Fresh quoted-window, opening-cut, cleanup-off, recorded parser, PostgreSQL runtime
and gate-validator checks: 173 passed. Preship's mapped incident checks: 1,255
passed. These are offline checks; hosted CI must rerun on the final source.

The original local T1 footage is now available. Ran `app.cli.verify_take_window`
with the committed KRI-790 fixture and that original, both with cleanup off and
on. The kept window is 12.265–50.380 seconds; cleanup-off output is 38.133333
seconds by ffprobe (38.115-second plan), with 540×960 picture and audio. Cleanup-on
plan is 35.335 seconds. This CLI produces a diagnostic FFmpeg render, not a
native app export or a full T1 result. Generated artifacts are retained locally
under `/private/tmp/kri-829-evidence/t1-trim-off` and `t1-trim-on`; they are not
committed media.

Inspected the first/last output frames. Independently transcribed the rendered
cleanup-off audio with local whisper.cpp medium (no provider call): it begins
with the requested introduction and ends with the requested “here all week”
line. The transcript also confirms both unwanted phrases remain: actual phrase
deletion and speaker-specific MEROPE placement remain KRI-724/KRI-787 work.
Original case source observations are the kit's `T1_caro_full_take_talking/README.md`.
No complete request, physical-iPhone export, correction/reopen, or rollout claim.

## Current-base refresh — 22 October 2026

Merged current main `a33c4b21b` (the unrelated research #1553 and release
metadata) into this existing candidate without a product conflict; the shared journey
manifest auto-merged and still contains the unique KRI-789 take-trim journey. The
affected quoted-window, opening-cut, cleanup-off, recorded-parser, PostgreSQL runtime
and gate checks passed: 121 passed, 1 intentional skip. Preship passed its scoped
Ruff, drift, release, reliability, journey and fix-chain checks once the PR evidence
record was refreshed. The current PR head still requires hosted CI.

The pending scope is unchanged: the actual physical-iPhone original-T1 export and
the complete original request (phrase deletion, Caro-specific MEROPE placement,
captions/layout, correction/reopen and production verification) remain owned by
KRI-787/KRI-829 and are not claimed here.
