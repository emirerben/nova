# KRI-789 integration evidence

This is a local intent/parsing fix, not proof the full Merope T1 request was fulfilled.
The original one-take request asks to start at a quoted introduction and stop after a
quoted closing line, with an independent approximate 35-second length request.

The recorded Main Creator reply is replayed through the actual parser. It becomes
one timing requirement with `start_line` / `end_line`; the separate 35-second fact
remains unchanged. The actual order-question gate and render contract do not invent
a clip-order choice for the one take. Existing multi-input, photo, route, unquoted,
one-word, pin and change-target controls remain intact.

Integration preserves the caption-spelling prompt from main and its runtime checks.
The added trim instructions advance the combined prompt to v66 / extractor v14;
only the two brief-enabled prompt hashes change. Existing reliability gate entries
are preserved and the new incident gate is appended. Original author commits remain
in ancestry; no source assertions were weakened.

Verification on the integrated runtime before this evidence-only document commit:

- `bash scripts/check-api.sh test tests/agents/test_kri789_take_trim_brief.py tests/agents/test_brief_extractor.py tests/agents/test_main_creator_agent.py tests/agents/test_main_creator_user_song.py tests/kria/test_kri667_structured_ask_facts.py tests/incidents/test_incident_corpus.py tests/evals -q`: 2,413 passed, 15 skipped, nine expected failures. Replay-only; no paid model calls.
- Original expected inputs: `src/apps/api/tests/fixtures/incidents/kri789-one-take-start-end-lines-ask-no-order.json` and `src/apps/api/tests/fixtures/agent_evals/main_creator/kri789_one_take_start_end_lines_trim.json`.
- Change-target preservation and negative parsing boundaries execute in
  `src/apps/api/tests/agents/test_kri789_take_trim_brief.py`.

KRI-790 separately consumes these facts and checks the source window and receipts.
The broader KRI-787 compound request still needs original-media native export and
receipt inspection, including its other requested cuts, captions and speaker-specific
text. No live-model, physical-phone, production rollout or full-request completion
claim is made here.

## KRI-788 security-fixture integration

Hosted CI exposed a stale one-clip refusal expectation in the dispatch security
guard. Its forged-answer stripping assertion still passed; KRI-789 intentionally
removed that clip-order blocker for one clip. Every original refusal assertion is
retained using two distinct selected clips in the supported montage format (phone
Talking supports exactly one clip). A separate original one-clip Talking control
asserts the forged answer is stripped, no order is invented, and the phone gate
passes. All 19 dispatch-security and trim-parser controls pass. No product change
or security assertion removal was needed. The original failed hosted case remains
evidence of the integration interaction, not an accepted current result.

## Landed KRI-791 integration

Main 049bdd0a6 (KRI-791 plus the KRI-807 picker repair) landed while the earlier
candidate's native CI continued. Local reconciliation retains caption spellings,
spoken-text triggers and speaker-disclosure behavior, plus quoted take-trim facts.
The combined prompt advances to v67 / extractor v15; only the brief-on hashes are
regenerated. All 41 gates in the newer main remain unchanged. Focused parser,
spoken-text, security and free replay checks: 2,450 passed, 15 skipped, nine expected
failures. This new source needs its own hosted checks; earlier native evidence is
not relabelled as evidence for the new combined source. No running hosted job was
cancelled or superseded to prepare this local reconciliation.
