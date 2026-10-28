# KRI-1009: a Talking title asked for "at the top" was dropped

Parent incident: KRI-1008 (Plaza de Mayo T1, thread "maximo 1", job `c7430eed`,
item `d1afb1de`, Fly v1899 `884d90d7e`, client protocol 22).

## Original request

> Title “The Cabildo 🇦🇷” at the top, never over his face.

One horizontal 35.4 s take, Talking (`subtitled`), rendered on the iPhone. The brief
kept it as r2: `kind=text, scope=global, literal="The Cabildo 🇦🇷",
facts={position: top_center}`.

## What happened

1. The Main Creator wrote the title as one corner text:
   `pinned_texts=[{text: "The Cabildo 🇦🇷", corner: top_center}]`, no `opening_title`.
2. `compile_strategy_to_plan` refuses every corner text on a Talking edit (no corner
   lane), so `check_strategy_for_runtime_v2(simplify_unsupported=True)` removed it
   with "This edit cannot render your on-screen texts, so I left it out".
3. The runtime matched the removed text to r2's literal (`_policy_text_omission_reason`),
   counted it as a proven automatic omission, superseded r2 in brief v3 and drafted
   "Here's the simpler version" straight to the render approval. Nothing was asked.

The same words as `opening_title` pass the same check: the phone Talking title lane
(KRI-467, `phone_subtitled_title_supported`) was on and the render went to the device.
Reproduced locally with the prod verified-feature list (corner text omitted, opening
title kept), word for word the prod notice.

## Acceptance

- The T1 request drafts an opening title "The Cabildo 🇦🇷" (no corner text), keeps r2
  live, says nothing was left out, and reaches the normal render approval.
- That strategy compiles to a phone recipe with an `opening-title` text row and layer
  carrying exactly "The Cabildo 🇦🇷", placed by the title lane's face check.
- A creator-quoted text that still cannot be drawn holds the draft back and asks the
  "simpler version?" question naming that text. Tapping it drafts the simpler version
  ("Left out as agreed"); only then does the brief drop the text.
- Still wrong: any reply that says the title is left out when the lane is open; any
  draft that drops quoted copy without a question; a title receipted as met when the
  rendered words differ from the creator's quote.

## Failure modes written before the tests

Repair (corner text → opening title):

1. Fires with the phone title lane closed (cloud Talking, `PHONE_SUBTITLED_TITLE_ENABLED`
   off) and turns a corner-text omission into a title omission with the wrong words.
2. Moves a bottom-corner text to the top.
3. Converts one of several corner texts, or overwrites an opening title already set.
4. Converts a time- or clip-scoped corner text into the opening seconds.
5. Converts text longer than the phone title lane draws (80 characters): it would be
   cut silently at render.
6. Applies to formats that do have a corner lane (montage, voiceover) and steals their
   corner text.
7. The receipt still reports r2 as left out / not met after the repair, which turns into
   a question loop or a false "Partly".
8. The repaired title never reaches the phone recipe (worker reads the strategy, lane
   gate at render).
9. Approval-time re-checks (`approval_readiness`, runtime re-check) see a changed
   strategy and raise a policy problem.
10. The note about the move is English-only in a Turkish chat.

Question instead of silent drop:

11. A quoted text is still dropped automatically (the incident).
12. The question loops: tapping "simpler version" asks again.
13. Automatic recovery for non-quoted limits (footage brightness, sound effects) turns
    back into a question.
14. The question turn loses the brief requirement (rollback must keep it live).
15. The question's reason is jargon ("selected renderer").

## Fix

- `strategy_policy._talking_top_text_as_title`: on a Talking edit whose phone title lane
  is open (`creator_capabilities.phone_talking_title_lane_open`), one whole-video
  top-corner line of at most 80 characters (`TALKING_TITLE_MAX_CHARS`, what the lane
  draws) with no title beside it becomes `opening_title`, before the baseline is taken,
  with a notice that it shows for the first few seconds. Kill switch:
  `PHONE_SUBTITLED_TITLE_ENABLED=false` closes the lane and restores the omission.
- `kria_runtime._automatic_omission_ids`: a brief requirement carrying the creator's
  quoted words (`kind=text` with a `literal`) is never omitted automatically. The draft
  is held back with the KRI-968 "simpler version?" card naming it; a tap drops it as
  agreed. A brightness facet beside a kept title stays automatic. The receipt/readiness
  line for compiler-proven omissions says "This kind of edit can't show this text yet."
  (was "isn't in this draft" / "The selected renderer cannot draw…"), and a `global`
  quoted text also matches an omitted opening title.

## Evidence (offline replay + native output; not deployed)

- Incident replay, `tests/kria/test_kri1009_talking_top_text_title.py` (real
  `submit_turn` + `run_kria_turn` over scratch Postgres, real planner/policy/receipts/
  approval; only the Main Creator transport is scripted). On `db42721ce` (no fix): 4
  failed with the prod wording, 2 controls passed, 1 xfailed. With the fix: 6 passed,
  1 xfailed.
- Wrong-content control: the model pins "The Cabildo" without the flag; r2 is never
  receipted met, readiness on and off, before and after the fix.
- Recovery: title lane closed → question naming the title, no draft or approval; the tap
  drafts "Left out as agreed" and retires r2. The KRI-698 phone voiceover journey now asks
  first and still reaches the native phone recipe after the tap.
- Render path: the planner's real output runs through `_run_generative_job`; the variant
  gets the `opening-title` row and `text-0` layer with the exact words, moved below a
  synthetic face in the top band.
- Native output (macOS host build of KriaMediaEngine `AVFoundationLocalExporter`, the
  engine the iPhone runs; synthetic 1080x1920 clip): "The Cabildo" and an Argentina flag
  drawn in the top band (y_frac 0.15) from 0 to ~2.5 s, gone at 2.6 s; the pre-fix twin
  draws only captions. `scripts/ios/kri-1009-talking-title.py` prepare → export →
  compare passed (macOS 26.6.2, Swift 6.3.3); `compare` fails, as it must, when the
  twin's movie stands in for the title movie. Results: `kri-1009-native-title.json`.
- Related suites: `tests/kria` + title worker/editor/pipeline tests: 6618 passed.

## Gaps and owners

- Approval readiness on (off in prod): a title with a position fact is `evidence_missing`
  before render, so the draft is still held (xfail in the test). Owner: KRI-1009 follow-up.
- Phone titles over 80 characters are cut silently at render while the strategy accepts
  280 (`talking_title_element`, `phone_narrated_plan`). Not fixed here.
- Bottom-corner, multi-line and time- or clip-scoped corner text on Talking stays
  unsupported; it is now asked about instead of dropped.
- iPhone re-send of the T1 prompt after deploy (parent KRI-1008 stays open).
