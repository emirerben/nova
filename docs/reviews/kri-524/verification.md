# KRI-524 verification and limits

## Delivered

- The review library contains 252 distinct prompts (28 for each of nine canonical formats), covering creation and editing across 14 request families. The 252 library entries remain unexecuted; the library is evidence of coverage design, not 252 live model runs.
- Historical review bounded the evidence at 40 threads and 50 jobs. The exact graded-title conversation remains unlocated. New exact production evidence was recovered from private `lisbon-events` turn/proposal-trace files, without committing private identifiers or raw conversations.
- The Gemini Pro brief extractor succeeded for both original requests. The first editor patch applied only a fade-in and did not split the requested text. A correction reached valid extraction but failed during brief-ledger application at an unknown exact redacted update.
- The implementation now supports arbitrary text replacement sequences while conserving wording, inheriting look/timing, sharing entrance/exit, and enforcing the 100-child/16-operation compiler budget. Whole-bar versus chunk semantics, generic composition prompts, partial-axis defaults, title-role selection and sequence lineage preservation were clarified. Brief-context application is validated inside the existing bounded schema retry; final CAS remains unchanged.
- Pixel/Skia checks cover production-overlay adaptation, frame alpha and phase behavior. No renderer capability, product UI, public endpoint, migration or authorization boundary changed. The edit prompt is now version 2026-10-08-v71.

## Executed evidence

Final results: **698 passed / 2 expected failures** in offline replay; **505 passed** in affected suites; **1,492 passed / 6 skipped** in `make verify-kria`; scoped lint and preship checks passed. Counts overlap and must not be added together. Details are recorded in `results.json`. Commands run from `src/apps/api` unless otherwise specified:

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

The initial live run failed before the final bounded run. The final run used nine canonical cases: seven changed cases passed compile/save, plus no-op and unsupported cases. The unsupported raw reply had a redirect, but the typed unmet reason was correct and the new parser uses that reason offline. Separate Gemini Pro brief extractor → route → Flash editor → compile/save correction passed as a component integration check; this is not a full HTTP suite. All 28 provider calls settled, across run ledgers capped 2, 2 and 1, with $0.483363 spent.

## What the evidence proves

| Layer | Evidence | Limit |
| --- | --- | --- |
| History | 50 newest jobs, 40 event histories, 109 user messages, 69 distinct | Bounded sample; not all Emir/Yasin history, especially no-job threads |
| Library | 252 reviewed inputs, validated matrix and metadata | Not 252 executed AI requests |
| Offline replay | Recorded outputs run through actual adapters/compilers/checkers | Does not establish live model choice or paraphrase reliability |
| Runtime | Typed scope regression and complete/partial/missing timing evidence | Does not establish intent extraction accuracy for a new live request |
| Compiler/save | Actual operations, projected later edits, real save-payload validation | No database save/export end-to-end claim |
| Render checks | Existing local synthetic PNG placement/Skia fixture checks | Not the original user's entire video export or audible audio continuity |
| Live model | 28 settled provider calls across three bounded ledgers; **$0.483363 spent** | Small synthetic sample with earlier non-canonical inputs separated from final canonical cases; does not establish universal extraction or paraphrase reliability; configured ceiling was $2/run and $5 total |

The exact original conversation behind “graded title” was not located. KRI-523 supplied the related stacked-title example; the phrase was not treated as literal title text. Original text/style and real full-video export remain unverified. Source-range checks distinguish an unchanged trimmed edit from restoring the original full source; duration alone is not preservation. Audio-level contract checks do not prove audible continuity. Save claims from earlier non-canonical role/title inputs remain explicitly unverified for the full capability matrix.

## Deduplicated follow-ups

KRI-524 is linked to the existing reports below. No duplicate issues were created and their status was not changed.

| Existing ticket | Requested outcome / reproduction | Current limitation and alternative | Acceptance criteria |
| --- | --- | --- | --- |
| [KRI-523](https://linear.app/kria/issue/KRI-523) | Two stacked lower-left titles throughout output, upper-left location; follow-up requests hit refusal/brief failure | Full original chat not recovered; this change fixes the demonstrated title/source receipt collision. Use existing editable text operations where destination permits | Recover exact original conversation; verify text, geometry, full-span timing after later edits and export |
| [KRI-514](https://linear.app/kria/issue/KRI-514) | Phone Talking closing “MY PICK” badge | Text-lane gap fixed upstream in PR #1476 during this run; current main includes it; verify destination rollout before use | Closing badge supported end to end on phone, truthful capability response, later edit/export preserved |
| [KRI-519](https://linear.app/kria/issue/KRI-519) | Narrated phone edit shows a Visual when a named phrase is spoken | Runtime-v2 phrase-triggered Visual lane not supported; use a supported destination or manual timing | Advertised capabilities match executable path; phrase timing, approval and render proven |
| [KRI-522](https://linear.app/kria/issue/KRI-522) | Blue source first, chronological order, opening typewriter hook and lower-left placeholders | PR #1479 merged; its fixes are integrated into this branch | Independent source order, text and placement checks, phone capability truthfulness |

Unsupported library controls such as invented footage and unrecorded speech are deliberate negative tests, not promises to add those capabilities.

## Release and recovery

PR [#1481](https://github.com/emirerben/nova/pull/1481) remains open and unmerged. KRI-522's fixes are integrated upstream in main via PR #1479. Version and changelog updates belong to Nova's post-merge release metadata workflow.

Merge/deploy require the current explicit **Land now / Wait / Revise / Cancel** decision. After approval, use Nova's existing guarded deployment workflow and verify the deployed revision. Until then, this is implementation and review evidence only.
