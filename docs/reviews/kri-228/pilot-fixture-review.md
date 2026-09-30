# KRI-228 Jev pilot: plain-English guide

This document answers four questions:

1. What are we testing?
2. Why is the current test set not trustworthy yet?
3. Which cases should we keep, rewrite, or remove?
4. What should we do next?

Start with these three sections:

1. [The one-minute version](#the-one-minute-version)
2. [One example](#one-example)
3. [What to do next](#what-to-do-next)

Use the case table when you are ready to edit. The technical details are at the bottom.

## The one-minute version

We want to test whether Jev can review a proposed video edit before it is rendered.

Jev receives:

- what the creator asked for;
- what Kria plans to put in the edit;
- short labels describing the uploaded clips; and
- four yes/no questions.

Jev returns probabilities such as “92% likely this requirement is present.” It never changes the
edit. This is a background test only.

The current test file contains **25 cases and 100 yes/no decisions**. The software around the test
works, but the cases are too artificial to tell us whether Jev is good enough:

- correct examples copy the requested text word for word;
- incorrect examples repeatedly claim the video was “filmed on Mars”;
- ambiguous examples reuse the same vague sentences;
- all Turkish examples reuse the same two sentences; and
- several cases test exact timing, text, or ordering that normal code should check instead.

**Decision: do not send this version as a real model evaluation. Rewrite the cases first.**

No case data has been sent to TypeSafe.

## What to do next

1. Keep the current manifest only as a plumbing test.
2. Add a human-editable source file where each case is written explicitly.
3. Start by rewriting cases `001`, `002`, `005`, `017`, and `021`. They cover useful semantic
   checks: food meaning, landmark grounding and correction, multi-turn style, and preserving an
   earlier exclusion after a later request.
4. Add distinct real English and Turkish cases until there are 100-200 decisions.
5. Run every case through the same eligibility rules used in production.
6. Have two people label each decision and record a short reason.
7. Freeze the calibration thresholds.
8. Review the exact outbound payload.
9. Approve TypeSafe data handling and the specific export.
10. Run the held-out set once and decide GO or NO-GO.

Do not run the live evaluation before steps 1-9 are complete.

## The five terms you need

| Term | Plain-English meaning |
| --- | --- |
| Brief requirement | One thing the creator asked Kria to do |
| Candidate output | Something Kria proposes to put in the edit |
| Gold label | The correct answer that a human reviewer approves |
| Calibration set | Examples used to choose probability cutoffs |
| Held-out set | Untouched examples used for the final test |

“Shadow” means Jev runs in the background and cannot affect the creator's edit, reply, approval,
or render.

## One example

Case `jev-pilot-001` starts with this request:

> Add a caption to every clip naming the dish or drink.

The generated candidate output contains:

1. `Add a caption to every clip naming the dish or drink.`
2. `Unrelated claim 1: filmed on Mars`

The local test says the correct answers are:

| Question | Correct answer | Why |
| --- | --- | --- |
| Is requirement 1 included? | Yes | The output copies it exactly |
| Is requirement 2 included? | No | The output does not show the required per-clip result |
| Is claim 1 unsupported? | No | The brief itself supports it |
| Is claim 2 unsupported? | Yes | Neither the brief nor the clip labels mention Mars |

This proves the request and scoring pipeline can run. It does **not** prove Jev understands a
realistic mistake. “Filmed on Mars” is much easier to reject than a plausible wrong dish,
landmark, title, or story detail.

## How one case moves through the system

```text
creator's request
      +
Kria's proposed edit
      +
clip labels
      |
      v
four Jev questions
      |
      v
four probabilities
      |
      v
compare with human-approved answers
```

The four questions are always:

1. Does the proposed edit include requirement 1?
2. Does the proposed edit include requirement 2?
3. Is candidate claim 1 unsupported by both the brief and media?
4. Is candidate claim 2 unsupported by both the brief and media?

The gold labels stay local. TypeSafe does not receive the correct answers.

## Why the current cases are misleading

### 1. The wording gives away the answer

The generator uses four fixed recipes:

| Case type | What the generator does | Labels |
| --- | --- | --- |
| Good | Copies both requirements into the output | both requirements present; both claims supported |
| Flawed | Copies requirement 1 and adds “filmed on Mars” | first present; second missing; Mars unsupported |
| Ambiguous | Uses the same two vague claims | both requirements missing; both claims unsupported |
| Turkish | Copies the same two Turkish requirements | both requirements present; both claims supported |

A model can learn these surface patterns without doing the real review task.

### 2. The generator ignores the useful parts of the source fixtures

The original request-following fixtures contain:

- structured requirements;
- multiple conversation turns;
- a correct reference edit; and
- a correct reference reply.

The Jev generator ignores those fields. It only reads:

- the first user message;
- the fixture's test description; and
- clip subjects.

That is why good source material turns into weak Jev cases.

### 3. Some cases should never reach Jev

Exact text, duration, counts, and clip order have objective answers. Normal code can check them
more cheaply and reliably. The pilot design says Jev should handle semantic questions, but the
generated dataset still contains many deterministic cases.

### 4. Nobody has approved the answers

All 25 cases say:

```text
label_source = derived
human_adjudicated = false
```

Running Jev against unreviewed labels would produce numbers, but those numbers would not justify a
production rollout.

## What to do with every case

Use these three actions:

- **Rewrite:** the underlying idea is useful, but the candidate output and labels need realistic
  human-authored content.
- **Move:** this belongs in deterministic code tests, not the Jev evaluation.
- **Remove/replace:** it is duplicated, leaks internal test text, or uses the generic Turkish
  template.

| ID | What it is about | Action | Reason |
| --- | --- | --- | --- |
| 000 | Vague edit request plus internal KRI-185 history | Remove | Leaks internal IDs and tests exact copying |
| 001 | Captions naming food and drinks | Rewrite | Good semantic idea; replace the Mars claim with a plausible food mistake |
| 002 | Landmark labels and duplicate handling | Rewrite | Good idea; replace the canned ambiguous text with a realistic partial result |
| 003 | Generic Turkish text over Istanbul footage | Replace | Does not come from the selected source fixture |
| 004 | Exact title `Kadıköy'de Pazar` | Move | Exact literal text should be checked in code |
| 005 | Correcting one wrong landmark | Rewrite | Use the real second-turn correction and before/after edit |
| 006 | Internal KRI-185 history plus canned ambiguity | Remove | Internal text and no realistic candidate output |
| 007 | Generic Turkish text over an English food fixture | Replace | Does not test the actual source request |
| 008 | Harbor route order | Move | Exact ordering belongs in code |
| 009 | Internal KRI-185 history plus Mars | Remove | Internal text and canned negative |
| 010 | Caption reading-time formula | Move | Exact timing formula belongs in code |
| 011 | Generic Turkish text over an English route fixture | Replace | Does not test the actual source request |
| 012 | Repeat of case 000 | Remove | Duplicate evidence |
| 013 | Repeat of case 001 with a different Mars number | Remove | Duplicate evidence |
| 014 | Reversing a harbor route | Move | Exact ordering belongs in code |
| 015 | Generic Turkish text over a sports fixture | Replace | Held-out wording repeats the calibration pattern |
| 016 | Filming chronology | Move | Capture-time ordering belongs in code |
| 017 | Changing every text font | Rewrite | Useful multi-turn idea; replace Mars with a realistic missed font change |
| 018 | Exact labels on named goal clips | Move | Literal text and placement belong in code |
| 019 | Generic Turkish text over an English trip fixture | Replace | Use a real Turkish fixture instead |
| 020 | Creator name and day number | Remove or anonymize | Contains a name and tests exact facts better handled in code |
| 021 | Keeping a standing exclusion after a later request | Rewrite | Best semantic idea in the set; create a realistic reintroduced-clip failure |
| 022 | Filming order plus place labels and deduplication | Split | Mixes deterministic order with semantic labels |
| 023 | Generic Turkish text over an English title fixture | Replace | Does not test the actual source request |
| 024 | First goal, celebration, handshakes in exact order | Move | Exact sequence belongs in code |

That reduces the current set to:

- 5 useful ideas to rewrite: `001`, `002`, `005`, `017`, `021`;
- 7 deterministic cases to move out of Jev: `004`, `008`, `010`, `014`, `016`, `018`,
  `024`;
- 1 mixed case to split: `022`; and
- 12 cases to remove or replace: everything else.

## What a good replacement case looks like

A good case should pass this checklist:

- It tests meaning, not exact arithmetic, timing, text, or ordering.
- The candidate output looks like something Kria could genuinely produce.
- A negative is plausible, not absurd.
- An ambiguous case is genuinely open to interpretation.
- The answer cannot be guessed from a repeated template.
- The requirement and output use the same production projection as the shadow task.
- A reviewer explains why each gold answer is correct.
- A second reviewer agrees or an adjudicator resolves the disagreement.
- The case's footage lineage appears in only one split.
- Turkish cases use real Turkish requests and outputs.

### Better negative examples

Instead of “filmed on Mars,” use mistakes such as:

- calling Turkish coffee “espresso” when the media label says Turkish coffee;
- calling Fort Halden “Halden Watchtower” after the creator corrected it;
- reintroducing the missed shot after the creator asked to remove it;
- changing only eight of nine text lanes after “change all fonts”; or
- claiming a clip shows a landmark when the media facts do not name one.

These are hard enough to tell us something useful.

## How to edit the current version

Do not edit `pilot_manifest.jsonl` directly. It is generated and will be overwritten.

The current source of truth is
[`build_manifest.py`](../../../src/apps/api/tests/evals/jev/build_manifest.py):

- `SCENARIOS` controls the repeating four-case pattern.
- `_case_payload()` creates requirements, candidate claims, and labels.
- `build()` assigns IDs, splits, footage groups, language, and metadata.

For a quick plumbing-only change, edit that file and regenerate the manifest. For a real
evaluation, replace the scenario generator with an explicit reviewed case file.

### Verify an edit

From `src/apps/api`:

```bash
.venv/bin/python -m tests.evals.jev.build_manifest
.venv/bin/pytest tests/evals/jev/test_harness.py \
  tests/services/test_jev_brief_shadow.py -q
.venv/bin/ruff check tests/evals/jev app/services/jev_brief_shadow.py
.venv/bin/ruff format --check tests/evals/jev app/services/jev_brief_shadow.py
```

Expected result: 25 generated rows, 100 decisions, passing tests, and clean Ruff output.

## Technical reference

### Dataset shape

| Measure | Current value |
| --- | ---: |
| Cases | 25 |
| Decisions | 100 |
| Calibration / held-out cases | 15 / 10 |
| English / Turkish cases | 19 / 6 |
| Good / flawed / ambiguous / Turkish cases | 7 / 6 / 6 / 6 |
| Human-reviewed cases | 0 |

Calibration uses `east_run`, `food_day`, and `harbor_run`. Held out uses `sport`, `trip`, and
`vlog`. No footage group crosses the split.

### What TypeSafe receives

Each of the 25 requests contains:

- two brief requirements;
- an intro hook and opening title;
- those same two strings as the story structure and candidate claims;
- `direction=request_following` and `edit_format=montage`;
- human-readable media labels;
- four Noul yes/no questions; and
- the pinned model name `jev-1.13.0`.

It does not receive:

- the case ID;
- calibration or held-out status;
- the scenario name;
- provenance or review metadata;
- gold labels;
- raw photos or videos; or
- storage paths.

The API key is sent in the authorization header, not inside the case data.

### Harness gaps before a real gate

- The report trusts the text `threshold_source=calibration`; it does not verify a frozen
  calibration artifact.
- Confidence-band scoring exists, but the report command cannot enable it. Successful calls
  therefore report 100% coverage.
- The plan asks for macro/micro and severity results, but the report does not produce them.
- The rollout rule mentions error and fallback rates, but the numeric gate checks only errors.
- A schema-valid eval case can bypass the production projection rules.

### File map

| File | Purpose |
| --- | --- |
| [`pilot_manifest.jsonl`](../../../src/apps/api/tests/evals/jev/pilot_manifest.jsonl) | Generated 25-case dataset |
| [`build_manifest.py`](../../../src/apps/api/tests/evals/jev/build_manifest.py) | Current case generator |
| [`jev_brief_shadow.py`](../../../src/apps/api/app/services/jev_brief_shadow.py) | Production projection and questions |
| [`jev_client.py`](../../../src/apps/api/app/services/jev_client.py) | TypeSafe HTTP request and response handling |
| [`models.py`](../../../src/apps/api/tests/evals/jev/models.py) | Case and prediction schemas |
| [`run_live.py`](../../../src/apps/api/tests/evals/jev/run_live.py) | Paid live runner |
| [`scorer.py`](../../../src/apps/api/tests/evals/jev/scorer.py) | Probability threshold scoring |
| [`report.py`](../../../src/apps/api/tests/evals/jev/report.py) | Held-out report and GO/NO-GO checks |
