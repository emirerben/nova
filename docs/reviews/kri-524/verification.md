# KRI-524 verification and limits

## Delivered

- 252 distinct prompts: 28 for each of the nine canonical formats; creation and editing for each of 14 request families. The catalog separates usable examples from clarification and unsupported controls. Every case records prerequisites, provenance and an independent expected outcome.
- Repeatable offline runner reuses request-following replay, agent cassettes and incident fixtures. Coverage includes format, family, phone/cloud destination and failure category under test. The 252 library inputs remain explicitly unexecuted; existing cassette results are not credited to them.
- Fixed a demonstrated result-reporting defect: a title-scoped request to keep text visible for the whole video no longer enters the source-take preservation checker. Full-title timing requires complete compiled timing and output-duration evidence; missing evidence stays unverified.
- Internal evaluation projections retain text geometry and distinguish source time from output time. Checks detect misplaced text, short title holds, missing source ranges, duplicate/extra source occurrences and incomplete timing.
- Compiler regressions exercise full-span stacked lower-left titles, an upper-left label, subsequent style edits, save-payload validation and source/audio preservation contracts. No renderer, product UI, public API, migration, prompt version or authorization boundary changed.

## Executed evidence

Final command results and counts are recorded in `results.json`. Commands run from `src/apps/api` unless otherwise specified:

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

`make verify-kria` initially failed because sandbox access to local PostgreSQL was blocked. With access, one unchanged retention test failed against the shared test database: its cleanup task processes only 100 old records, so unrelated old rows could consume its batch. An isolated database with the same schema passed the complete gate. No shared database rows were removed and no test was bypassed. The local pg_dump version emitted an unsupported transaction_timeout setting; only that setting was removed from the private schema bootstrap.

## What the evidence proves

| Layer | Evidence | Limit |
| --- | --- | --- |
| History | 50 newest jobs, 40 event histories, 109 user messages, 69 distinct | Bounded sample; not all Emir/Yasin history, especially no-job threads |
| Library | 252 reviewed inputs, validated matrix and metadata | Not 252 executed AI requests |
| Offline replay | Recorded outputs run through actual adapters/compilers/checkers | Does not establish live model choice or paraphrase reliability |
| Runtime | Typed scope regression and complete/partial/missing timing evidence | Does not establish intent extraction accuracy for a new live request |
| Compiler/save | Actual operations, projected later edits, real save-payload validation | No database save/export end-to-end claim |
| Render checks | Existing local synthetic PNG placement/Skia fixture checks | Not the original user's entire video export or audible audio continuity |
| Live model | No calls; **$0 spent** | Optional live reliability remains unmeasured; ceiling remains $2/run and $5 total |

The exact original conversation behind “graded title” was not located. KRI-523 supplied the related stacked-title example; the phrase was not treated as literal title text. Original text/style and real full-video export remain unverified. Source-range checks distinguish an unchanged trimmed edit from restoring the original full source; duration alone is not preservation. Audio-level contract checks do not prove audible continuity.

## Deduplicated follow-ups

KRI-524 is linked to the existing reports below. No duplicate issues were created and their status was not changed.

| Existing ticket | Requested outcome / reproduction | Current limitation and alternative | Acceptance criteria |
| --- | --- | --- | --- |
| [KRI-523](https://linear.app/kria/issue/KRI-523) | Two stacked lower-left titles throughout output, upper-left location; follow-up requests hit refusal/brief failure | Full original chat not recovered; this change fixes the demonstrated title/source receipt collision. Use existing editable text operations where destination permits | Recover exact original conversation; verify text, geometry, full-span timing after later edits and export |
| [KRI-514](https://linear.app/kria/issue/KRI-514) | Phone Talking closing “MY PICK” badge | Text-lane gap fixed upstream in PR #1476 during this run; current main includes it; verify destination rollout before use | Closing badge supported end to end on phone, truthful capability response, later edit/export preserved |
| [KRI-519](https://linear.app/kria/issue/KRI-519) | Narrated phone edit shows a Visual when a named phrase is spoken | Runtime-v2 phrase-triggered Visual lane not supported; use a supported destination or manual timing | Advertised capabilities match executable path; phrase timing, approval and render proven |
| [KRI-522](https://linear.app/kria/issue/KRI-522) | Blue source first, chronological order, opening typewriter hook and lower-left placeholders | Existing PR #1479 owns these fixes | Independent source order, text and placement checks, phone capability truthfulness |

Unsupported library controls such as invented footage and unrecorded speech are deliberate negative tests, not promises to add those capabilities.

## Release and recovery

Feature branch: `codex/kri-524-prompt-testing`, based on fetched `origin/main` at `8c02872226ff7a0e66b432142dced4c81db71ac5`. Main advanced during verification; the task branch was fast-forwarded to `141a59c86` (including KRI-514) before final checks. Shared checkout was preserved. Version and changelog updates belong to Nova's post-merge release metadata workflow.

Merge/deploy require the current explicit **Land now / Wait / Revise / Cancel** decision. After approval, use Nova's existing guarded deployment workflow and verify the deployed revision. Until then, this is implementation and review evidence only.
