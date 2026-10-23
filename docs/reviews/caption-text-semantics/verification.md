# Captions versus added text: bounded verification

## Request and acceptance

Yasin clarified Tigre cabin N1 in Codex on 2026-10-22: captions mean speech
subtitles. A supplied title and requested English subtitles must both remain
when the creator says "no text except the title". Additional labels, callouts
and decorative copy must not be invented. Explicit subtitle removal still turns
off subtitles. This is a focused follow-up to KRI-598, not closure of that
incident's footage selection, voiceover cue, or native export acceptance.

The independent expected behavior is recorded in
`docs/pipelines/kria-agent-runtime.md` under "Captions and added text".

## Implementation boundary

Main Creator v74 (retaining main's KRI-851 v70 title-with-beats fix), Brief Extractor v20 and
Edit Copilot v86 teach this distinction.
No renderer, client build, schema, database migration, or feature flag changes.
The subtitle requirement gets its own focused description, separate from source
speech and visual cues; otherwise existing receipt classification can interpret a
compound description as a source-audio requirement.

## Executed offline checks

All commands below ran from the isolated feature checkout. The PR's
`request-evidence.head_sha` binds this report to its committed implementation.

- `bash scripts/check-api.sh test tests/kria/test_caption_text_semantics.py tests/agents/test_main_creator_agent.py tests/agents/test_brief_extractor.py tests/agents/test_main_creator_user_song.py tests/agents/test_editor_ops_v2.py tests/evals/ -q`
  — 2,250 passed, 15 skipped, 7 expected failures. After removing an unsupported
  fact from the new replay, its six checks were rerun and passed.
- `bash scripts/check-api.sh test tests/pipeline/test_phone_narrated_plan.py -q`
  — 65 passed; existing title and speech-caption recipe separation remains intact.
- `python3 scripts/reliability-gates.py run --changed --python src/apps/api/.venv/bin/python`
  with a migrated, dedicated local test database — 329 passed, no skips or
  environment errors. Selected gates: `kri-422-per-shot-texts`,
  `kri-542-thinking-budget`, `kri-546-montage-receipts`.
- Scoped Ruff lint and format on the eight changed Python files passed;
  `git diff --check` and the CLAUDE size guard passed.
- Before the prompt change, the new prompt-presence guards failed: two failures
  and three passes at that stage. This is a prompt-contract red control, not a
  reproduced live-model failure.

The six new checks cover prompt inclusion in Main Creator with the brief enabled
and disabled, the shared extractor, and Edit Copilot; hand-authored English and
Turkish provider-shaped brief updates through the real parser, brief application
and receipt checker; and explicit caption removal with title preservation.
The acceptance helper rejects wrong-language subtitles, missing subtitles and an
extra decorative label in plan facts.

## Review and evidence limits

Independent read-only review found no blocking prompt contradiction. All-text
appearance styling still covers captions when explicitly requested; the new rule
only separates content-removal scope.

The replay payloads are hand-authored. They prove prompt inclusion and the
parser/receipt contract, not that a live provider will produce the desired fields.
The negative controls inspect plan facts, not every possible strategy text field
or rendered pixels. The Edit Copilot check verifies the prompt rule, not generated
operations. The existing phone recipe checks do not prove a TestFlight export.

Live-model interpretation, full N1 footage/cue fulfillment, native export and
production rollout are separate evidence stages. Keep KRI-598 open until its full
acceptance is verified. No paid call or native render is claimed by the offline
results above.

## Current-main integration — 23 October 2026

Main Creator v71/v72 and Brief Extractor v17/v18 are already used by the independent
KRI-814 PR #1771. This branch uses fresh v73/v19 identities for its different prompt
content; it does not include the unmerged delegation implementation. Main v70
Talking title-with-beats wording and the full shared brief instruction union remain
intact. Main after #1747/#1752 is integrated additively; all previous source commits
are retained. Fresh exact-head CI remains required before merge.

## Combined delegation integration

In dependent PR #1771, Main Creator v74 / Brief Extractor v20 combine the reviewed
subtitle instruction bytes with the complete KRI-814 declaration and earlier-message
instructions. An exact section-union comparison confirms neither instruction was
dropped; the main and copilot prompt templates match subtitle source1c1baa7e3.
Brief-off montage hashes equal subtitle v73; brief-on hashes cover the combined text.
The original subtitle PR remains v73/v19. Land #1755 before this dependent PR.
