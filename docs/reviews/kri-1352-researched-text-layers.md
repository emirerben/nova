# KRI-1352: researched text as word sequences and translated layers

Claim: `local_fix`, offline verified. The one approved live replay stopped after the writer reported
that supplied facts did not mention the date. The device render and production are not verified. The
incident stays open in KRI-1352.

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
| `tests/evals/request_following/test_research_feeds_text_sequences.py` | 39 passed |
| `tests/kria/test_refusal_names_real_limit.py` | 4 passed; 2 fail on the base code |
| `tests/gates/test_reliability_gates.py` | 148 passed |
| Live text-only replay (approved, USD 3 cap) | ran once; failed before shaping, see below |
| Phone render of the original request | pending |

## Live replay result

The one approved replay ran on 2026-11-10 against a production-shaped rebuild of the failed text
pass, with real Gemini calls and no stubs. It made three calls, all settled, USD 0.102215 in total,
with no unknown spend. The composer asked for research, the grounded search returned, and the
layer writer answered with an unmet request: the facts it received did not mention the date. The
render failed as "Text composition is incomplete" with that reason. No text was shown, and nothing
unsourced was rendered.

The facts, the research topic and the writer's layers were not captured, because
`persist_agent_run` stores no row without a job, plan item or session. The cause is therefore
unproven. In production, both searches for this request returned facts that named the date from
`.gov.tr` sources, so the search can ground it; why this run was rejected is unknown. An unmet
answer ends research before validation, review and the correction round, so no second write ran.
The request is not fulfilled. Any further paid run needs a new approval.

Each reviewer reproduction failed before its fix with `DID NOT RAISE` or `IndexError`.

## Resource envelope

A request that routes to text layers makes these calls: one composer request, one grounded search
(`gemini-2.5-flash`), one layer write and one claim review (up to two of each with the correction
round), and one composer shaping pass. The failed incident made two composer calls, two searches
and four caption writes. No call was added to other routes. The reviewer prompt changes only when
layers are present.

## Approved wording that later failed

The creator then asked the chat to write the text, and approved the wording it showed: a Turkish
paragraph for the middle and its English translation for the bottom, both word by word. The
server's wording question captured only the Turkish paragraph (196 characters) as its structured
candidate; the English was in the assistant's message only. The brief kept the Turkish as a literal.
The English paragraph is longer than the 200-character brief literal, so it was filed as a
description with no literal. The plan seeded no bar for it, and the text pass could not add a bar
and split it in one bundle. The job recorded that reason, and the render failed as
`creation_text_failed`.

The fix:

- `text_sequence` literals take up to 280 characters; every other kind keeps 200. Quoted sequence
  words follow the same bound; other quote readers keep 200.
- The plan does not seed a sentence longer than a clip label as a label.
- The text pass adds a whole-video bar at the asked place for a word sequence no bar holds, as
  confirmed copy. The composer shapes it in a second bundle. The program records each approved
  sequence's place, unit, and that sequences at different places are shown together, and every
  replay checks them.

`tests/kria/test_approved_long_sequence_copy.py` runs synthetic paragraphs of the same lengths
through the brief, the planner and the text pass. Wrong shapes (multiword pieces, both layers at the
top, one language after the other), a reworded translation, an unrequested extra line under
asked-only, and a saved program edited after composition all fail or are dropped. Whether the live
composer shapes both paragraphs is not verified.

## Retry from a brief written before the fix

After the 280-character sequence literal shipped, a "Try again" on the same chat failed again. The
retry reused the brief written before the fix: the English word sequence still had no literal, and
its paragraph sat in the description behind "(exceeds literal length limit): ". The new approval
pinned that brief, no bar held the paragraph, and the composer reported it could only split
existing text.

`recover_overflowed_literals` (`app/kria/text_sequence.py`) restores that paragraph as the
requirement's literal on a turn that proposes a draft (`_plan_live_turn`, after
`_with_approved_copy`). It writes a server change with the requirement's id, kind, scope and facts
kept, so it becomes a new brief version before the next approval. Approved briefs are never edited.
Only an exact legacy shape qualifies: an open title/global `text_sequence` with no literal, one marker, a label
and paragraph without leading/trailing whitespace, a paragraph over 200 and at most 280 characters, no
trailing ellipsis, and a description below its 300-character limit. A question or answer turn, the creator's
own change or removal in the same turn, a non-open requirement, and a malformed marker are left
alone. It authors no words. The approval consequence includes the restored paragraph verbatim
with a request to review it; the generic model-written summary alone is not treated as disclosure.

Synthetic checks:

- `tests/kria/test_legacy_overflow_literal_recovery.py`: the exact recovery and every refusal case,
  and the recovered brief rendering both paragraphs word by word.
- `tests/kria/test_legacy_overflow_retry_planner.py`: the real `_plan_live_turn` with only the
  model and clip loaders stubbed. A regeneration turn emits the change; a question turn does not;
  the creator's own change or removal wins.
- `tests/kria/test_legacy_overflow_retry_postgres.py`: real turns, brief persistence and approval
  binding. The real runtime commits the retry's update as brief version 2; version 1 and the old
  approval's binding stay unchanged. The fresh approval and its API payload show the restored
  paragraph verbatim, and fresh approval succeeds. A separate real stale-approve attempt rejects
  with `approval_requirements_not_ready` and cancels that approval. Provider planning and the
  capability manifest are synthetic; persistence, readiness assessment and decisions are real.

Offline validation: 96 focused tests and 871 mapped gates passed against an isolated scratch
database. No environment skips in those runs. A live retry and original native export remain
unverified; this follow-up has not yet been deployed.

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
