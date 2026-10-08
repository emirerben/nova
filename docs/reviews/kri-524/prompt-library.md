# KRI-524 prompt coverage

[Browse the 252-prompt catalog](prompt-catalog.md). The reviewed machine source is [corpus.json](../../../src/apps/api/tests/fixtures/prompt_coverage/corpus.json): nine canonical formats, fourteen request families, and creation/editing cases for every pair. Each case has its own expected outcome, prerequisites, provenance, and evidence status. No new product UI or renderer is introduced.

## Historical evidence

On 2026-10-08, read-only admin diagnostics retrieved the 50 newest jobs from 804 total jobs, spanning 2026-09-27 through 2026-10-08. These linked to 40 complete event histories: 109 user messages, 69 distinct messages. Raw payloads remain in private local storage outside Git. The admin job list cannot establish complete account-specific history or discover every conversation that never produced a job. This is a bounded historical sample, not all Emir/Yasin prompts.

The sample contained a title-scoped request to keep text visible for the whole video that was incorrectly checked as preserving the entire source take. The redacted corpus keeps that timing/size/font combination. KRI-523 supplies the two stacked lower-left titles and upper-left location example; the original chat containing the ticket's exact “graded title” wording was not found in this sample. Do not infer that it is literal display text.

Relevant Linear reports were read directly: KRI-522 (opening source/typewriter/placeholder placement, PR #1479 in review), KRI-523 (corner/full-duration text), KRI-430 (long creator-authored chapter copy), KRI-514 (phone Talking closing text, PR #1476 merged during this run), and KRI-519 (phrase-triggered Visuals in phone narration). Their examples informed redacted or authored variants. Existing fixes are not claimed as KRI-524 work.

## Evidence accounting

All 252 catalog prompts are marked `unexecuted`: their wording has not been batch-tested against a live model. The separate offline regression runner executes actual recorded agent responses, adapters, compilers and incident checks. It never credits those results to unrelated catalog prompts. A replay validates implementation behavior for the supplied response, not the model's ability to choose that response for a paraphrase.

The KRI-524 compiler regressions independently check two full-output title bars, their geometry, later-edit preservation and missing source evidence. The runtime regression checks typed title timing separately from source preservation. Save/export and rendered pixels require separate evidence; a passing in-memory compiler cannot stand in for them.

## Run

From `src/apps/api`:

```sh
.venv/bin/python -m tests.evals.prompt_coverage --report
.venv/bin/python -m tests.evals.prompt_coverage --replay
.venv/bin/python -m pytest tests/evals/test_prompt_coverage.py -q
```

`--replay` forces offline mode and returns nonzero if a required suite fails or is missing. The report groups the library by format, request family, destination under test, case kind, and evidence status. Destination counts mean test applicability, not verified support. The normal test-api CI also collects the new tests. `worker_render` is zero because these catalog cases target phone/cloud editing; rendering is measured separately with existing synthetic suites. Failure categories describe intended coverage, not observed failure rates.

## Boundaries and follow-ups

Supported behavior fixes belong in this change only when demonstrated by a failing regression. New renderer/model capabilities remain separate. KRI-519 delivered phrase-triggered photo Visuals for phone narration in upstream PR #1484; its enabled example and disabled-capability negative control are distinguished here; KRI-514 and KRI-523 cover the reported text-lane gaps. Reuse those tickets rather than creating duplicates. Invented footage, unrecorded speech, voice cloning, gaze correction, watermark removal, and hidden viewpoints are deliberate unsupported controls, not newly discovered bugs or promises.

Live spending limit: $5 cumulative, at most $2 per run, no unmetered judge. Runtime-only validation fixes do not require paid model calls. See verification.md for final executed commands, failures, limits, and release status.
