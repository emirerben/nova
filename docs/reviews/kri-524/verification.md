# KRI-524 verification and limits

## Delivered

- The review library contains 252 distinct prompts (28 for each of nine canonical formats), covering creation and editing across 14 request families. The 252 library entries remain unexecuted; the library is evidence of coverage design, not 252 live model runs.
- Historical review bounded the evidence at 40 threads and 50 jobs. The exact graded-title conversation remains unlocated. New exact production evidence was recovered from private `lisbon-events` turn/proposal-trace files, without committing private identifiers or raw conversations.
- The Gemini Pro brief extractor succeeded for both original requests. The first editor patch applied only a fade-in and did not split the requested text. A correction reached valid extraction but failed during brief-ledger application at an unknown exact redacted update.
- The full requirement router now combines supported text, audio, ordering, global timing and caption-lane requirements. The speculative brief-enabled fast path was removed; `MainCreator` replans through the existing binding/clip-intent pipeline, and the planner passes the whole effective brief to the editor. Extraction or editor-planning failures stop mutation and preserve the valid request, with `editor_planning_failed` distinguished from extraction failure.
- The implementation supports arbitrary text replacement sequences while conserving wording, inheriting look/timing, sharing entrance/exit, and enforcing the 100-child/16-operation compiler budget. Whole-bar versus chunk semantics, generic composition prompts, partial-axis defaults, title-role selection and sequence lineage preservation were clarified. Compiler flags now drive `timeline_patch_capabilities`; the current legacy-speed probe now returns an honest limitation. Canonical caption style/font/color/position are projected into the next turn, and plural intro/title-group selection updates both stacked titles while excluding unrelated locations. Brief-context application is validated inside the existing bounded schema retry; final CAS remains unchanged.
- Pixel/Skia checks cover production-overlay adaptation, frame alpha and phase behavior. No renderer capability, product UI, public endpoint, migration or authorization boundary changed. The edit prompt is now version 2026-10-08-v73.

## Executed evidence

Current results: **769 passed / 2 expected failures** in offline prompt-coverage replay; **1,288 passed / 15 skipped** in the affected/evals suites; **1,527 passed / 6 skipped** in `make verify-kria`. Counts overlap and must not be added together. Scoped lint and preship checks passed. Details are recorded in `results.json`. Commands run from `src/apps/api` unless otherwise specified:

```sh
.venv/bin/python -m tests.evals.prompt_coverage --report
.venv/bin/python -m tests.evals.prompt_coverage --replay
.venv/bin/python -m pytest tests/evals/test_prompt_coverage.py tests/kria/test_creative_brief.py -q
.venv/bin/python -m pytest tests/pipeline/test_text_overlay_anchor.py tests/pipeline/test_overlay_verify.py tests/routes/test_device_export_audio.py tests/services/test_kria_editor_ops.py -q
# Repository root, with disposable local test database and test-only settings:
make verify-kria
bash scripts/preship-check.sh
git diff --check
```

The initial live run failed before the final bounded run. Across eight ledgers, all 112 provider calls settled for **$2.160176**; each ledger stayed within its cap (2, 2, 1, 1.5, 1.5, 1.25, 1 and .5), and the tracked total stayed below $5. This includes 28 broad editor probes (27 expected outcomes and one historical false speed proposal rejected by the compiler and corrected by context), 22 calls for 18 creation cases across nine formats and phone/cloud destinations, and 34 calls for 18 chain cases across C8/D9/E1. The final nine-case chain covered order/style, duration/titles, preserve/edit/placement, source restoration/color, caption text/position, word sequence, arbitrary chunk sequence, partial unsupported voice with supported titles, and no-op. Seven supported changed cases passed compile/save; one supported title-only case passed with an honest limitation; no-op passed. C2 replan cases were routing observations only (`MainCreator` stub), not live success. D's missing-base-video fixture correctly returned 422; E's complete fixture rerun passed. Historical title under-selection was caught and is fixed in offline and fresh-live checks. The unsupported raw reply had a redirect, but its typed unmet reason was correct and the parser now uses that reason offline. Separate Pro brief extractor → route → Flash editor → compile/save correction passed as a component integration check; this is not a full HTTP or phone-export suite.

## What the evidence proves

| Layer | Evidence | Limit |
| --- | --- | --- |
| History | 50 newest jobs, 40 event histories, 109 user messages, 69 distinct | Bounded sample; not all Emir/Yasin history, especially no-job threads |
| Library | 252 reviewed inputs, validated matrix and metadata | Not 252 executed AI requests |
| Offline replay | Recorded outputs run through actual adapters/compilers/checkers | Does not establish live model choice or paraphrase reliability |
| Runtime | Typed scope regression and complete/partial/missing timing evidence | Does not establish intent extraction accuracy for a new live request |
| Compiler/save | Actual operations, projected later edits, real save-payload validation | No database save/export end-to-end claim |
| Render checks | Existing local synthetic PNG placement/Skia fixture checks | Not the original user's entire video export or audible audio continuity |
| Live model | 112 settled provider calls across eight bounded ledgers; **$2.160176 spent** | Bounded probes and synthetic fixtures; does not establish universal extraction or paraphrase reliability; each run stayed ≤$2 and tracked total stayed <$5 |

The exact original conversation behind “graded title” was not located. KRI-523 supplied the related stacked-title example; the phrase was not treated as literal title text. Original text/style and real full-video export remain unverified. Source-range checks distinguish an unchanged trimmed edit from restoring the original full source; duration alone is not preservation. Audio-level contract checks do not prove audible continuity. Database/object existence is synthetic; there is no full HTTP or phone export test. The 252 library remains unexecuted, and earlier non-canonical role/title save claims remain explicitly unverified for the full capability matrix. Full-chain probes observed roughly 10–20 seconds, which is not an SLA; extracting the effective brief before proposing an edit adds serial model-call latency versus the removed fast path.

## Deduplicated follow-ups

KRI-524 is linked to the existing reports below. No duplicate issues were created and their status was not changed.

| Existing ticket | Requested outcome / reproduction | Current limitation and alternative | Acceptance criteria |
| --- | --- | --- | --- |
| [KRI-523](https://linear.app/kria/issue/KRI-523) | Two stacked lower-left titles throughout output, upper-left location; follow-up requests hit refusal/brief failure | Full original chat not recovered; this change fixes the demonstrated title/source receipt collision. Use existing editable text operations where destination permits | Recover exact original conversation; verify text, geometry, full-span timing after later edits and export |
| [KRI-514](https://linear.app/kria/issue/KRI-514) | Phone Talking closing “MY PICK” badge | Text-lane gap fixed upstream in PR #1476 during this run; current main includes it; verify destination rollout before use | Closing badge supported end to end on phone, truthful capability response, later edit/export preserved |
| [KRI-519](https://linear.app/kria/issue/KRI-519) | Narrated phone edit shows a Visual when a named phrase is spoken | Runtime-v2 phrase-triggered Visual lane not supported; use a supported destination or manual timing | Advertised capabilities match executable path; phrase timing, approval and render proven |
| [KRI-522](https://linear.app/kria/issue/KRI-522) | Blue source first, chronological order, opening typewriter hook and lower-left placeholders | PR #1479 merged; its fixes are integrated into this branch | Independent source order, text and placement checks, phone capability truthfulness |
| [KRI-350](https://linear.app/kria/issue/KRI-350) | Speed ramps | Canceled legacy gap; do not revive or claim fixed | Reopen only with a new approved requirement |
| [KRI-449](https://linear.app/kria/issue/KRI-449) | Audio-level renderer behavior | Remains a separate renderer gap | Prove audible continuity end to end |
| [KRI-527](https://linear.app/kria/issue/KRI-527) | Speech excerpt with pinned text | Remains a separate capability gap | Verify supported speech/text composition end to end |

Unsupported library controls such as invented footage and unrecorded speech are deliberate negative tests, not promises to add those capabilities.

## Release and recovery

PR [#1481](https://github.com/emirerben/nova/pull/1481) remains open and unmerged. KRI-522's fixes are integrated upstream in main via PR #1479. Version and changelog updates belong to Nova's post-merge release metadata workflow.

Merge/deploy require the current explicit **Land now / Wait / Revise / Cancel** decision. After approval, use Nova's existing guarded deployment workflow and verify the deployed revision. Until then, this is implementation and review evidence only.
