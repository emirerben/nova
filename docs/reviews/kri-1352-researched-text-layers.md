# KRI-1352: researched text as word sequences and translated layers

Claim: `local_fix`, offline verified. The live replay, the device render and production are not
verified. The incident stays open in KRI-1352.

## The request and the failure

A creator asked for an informal but respectful explanation of a public date. They wanted one large
text in the middle, shown word by word, and the same text translated into English at the bottom,
also word by word, shown together with it. The brief held two `text_sequence` requirements with no
literal, `unit: word`, positions `middle` and `bottom`, and no `facts.research`. The montage had
four archival clips, 19.6 s in total.

The render failed as `phone_plan_failed`. The text composer asked for research, and
`run_researched_revision` replaced the composer's work with footage-window captions
(`"Label: fact"`). Those captions can be neither a word sequence nor a second layer. Every caption
answer listed the bilingual ask as unmet. The last answer also ran one character over its reading
budget, which raised first. The approval carried no prepared research, so research ran at render.

## Root cause

Research was a replacement writer, not a source of facts. The footage-window caption writer could
not express what the brief asked for, and one composer bundle could not split a bar it adds: a
`replace_text_sequence` selector binds the lane its bundle started from.

## The fix

- `research_needs_text_layers` routes asks without a literal (a word sequence, or two or more
  layers at distinct places) to reviewed text layers. Literal sequences and plain explanations
  keep the footage-window path.
- `ResearchLayersAgent` (`nova.research.text_layers`) writes one layer per requested layer, with
  cited fact IDs. A translation is its own layer.
- `validate_research_layers` checks without a model: cited IDs exist, an original states no number
  its facts do not, a translation cites its source's facts in another language with the same
  numbers and window when placed elsewhere, and naming who is pictured needs the creator's words or
  a visible label. `ResearchReviewAgent` (prompt `v5-text-layers`) then reviews the layers. One
  correction round runs, as for captions.
- Each layer becomes one cited bar. The composer only shapes the bars, in a second program bundle
  (`TextCompositionProgram.then`). `text_composition.research_layers` records each layer's place,
  reviewed window, unit and translation, and `replay_text_composition` rechecks them on every
  replay. The shaping bundle cannot add, rewrite or edit text.
- The pre-approval preview prepares and pins `PreparedResearch.layers`. Each layer is bound to the
  exact source and output cuts it was reviewed over. The render replays them with no search, model
  call or rewrite, and refuses re-cut footage.
- An approval that pinned footage explanations before this change fails plainly for an authored
  word sequence, without researching again.

The same investigation fixed a refusal label. After a creator approved a hook for one talking
clip, approval readiness said it could not keep "exact photo and video timing", which nobody asked
for. `_refusal_question` now asks the timing question only for a real timing contract.

## Acceptance and cases

The acceptance record is
`src/apps/api/tests/fixtures/kria_replay/kri1352_bilingual_research/expectation.json`. A test reads
it, checks the offline result against it, and checks that every named negative control exists.

Cases covered offline, with synthetic facts and wording:

- the incident shape: two concurrent cited word sequences, middle and bottom
- a translation listed before its original
- phrase units and word build-up
- a literal title sequence, which keeps footage-window research
- the preview path: prepare, approve through `BriefBinding`, then replay with no provider call
- wrong-content controls: unrelated facts, an invented date, an uncited fact, an unlabelled
  identity, a changed number, a mistranslation, a missing translation layer, an unmet layer
- shaping that writes, rewords, moves, retimes or mis-splits reviewed text, at compose and in a
  saved program edited after composition
- approved layers on changed or re-cut footage, and an older footage-caption approval

## Checks

| Check | Result |
| --- | --- |
| `tests/evals/request_following/test_research_feeds_text_sequences.py` | 37 passed |
| `tests/kria/test_refusal_names_real_limit.py` | 4 passed; 2 fail on the base code |
| `tests/gates/test_reliability_gates.py` | 148 passed |
| Live text-only replay (approved, USD 3 cap) | pending |
| Phone render of the original request | pending |

Each reviewer reproduction failed before its fix with `DID NOT RAISE` or `IndexError`.

## Resource envelope

A request that routes to text layers makes these calls: one composer request, one grounded search
(`gemini-2.5-flash`), one layer write and one claim review (up to two of each with the correction
round), and one composer shaping pass. The failed incident made two composer calls, two searches
and four caption writes. No call was added to other routes. The reviewer prompt changes only when
layers are present.

## Gaps

- Live accuracy: whether the layer writer cites the right facts, whether its translation is
  faithful, and whether the real reviewer accepts good layers and rejects bad ones. Stubbed
  reviewer verdicts prove orchestration only.
- The preview does not run the shaping pass, so an approved layer set can still fail at render when
  its split cannot fit its window.
- Naming who is pictured needs the creator's words or a visible label, so the claim review may hold
  back a real request that names a public figure over their footage.
- The footage-caption replay still binds clip ids only, not source cuts.
- Device render and production are not verified.
