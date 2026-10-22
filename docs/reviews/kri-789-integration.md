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
