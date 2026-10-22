# KRI-806 combined main integration

Fresh fetched main: `36031e054c1ffd2d6a3fdf3f5e492a4979154124`.
Original source: `9344d04a4c8488812904b175efb3e9c39236a3dd` (#1738).
Dependency sources preserved by normal merges:
- #1724 `133c0812d2764483989e00dfb30a1aa8f208ef91`, including #1727/#1733.
- #1739 `efd8216612bc3865453c2c18d9ce06b12c52ecdf`.

Merge those dependencies before this PR. This branch preserves their author
history; no source branch was force-pushed or its substantive assertions removed.

## Acceptance and reconciliation

An optional editorial plan that cannot run must be dropped without losing the
valid strategy's kept sound, selection, title, duration or story. A valid plan
must still cover exactly the selected clips after normalization; invented,
omitted and skipped clips cannot survive in that plan. Malformed duplicate
beats remain schema errors. Counts-only diagnostics must not leak creator text.
The original recorded-input tests cover these requirements without a paid call.

The code merge preserves the authored parser repair. Conflicts were limited to
prompt version pins/hashes and append-only reliability entries. Both caption
`spellings` and spoken-text `triggers`/`speaker` instructions remain, alongside
the new editorial-plan audio-pairing rule. Main Creator is `2026-10-22-v67`;
extractor remains the dependency's `2026-10-22-v14`.

All four no-song prompt hashes change because KRI-806 changes the shared prompt.
The two brief-off hashes exactly match the original KRI-806 source. The two
brief-on hashes also include the reviewed caption and timed-text instructions.
All semantic assertions remain unchanged. All current-main and dependency gate
IDs are preserved; KRI-806 and KRI-805 are appended once each.

## Executed verification

- `bash scripts/check-api.sh test tests/agents/test_main_creator_editorial_plan.py tests/agents/test_main_creator_agent.py tests/agents/test_main_creator_user_song.py tests/tasks/test_kri791_spoken_text_trigger.py tests/kria/test_kri793_caption_spellings.py tests/kria/test_kri805_brief_change_answer.py tests/agents/test_brief_extractor_version_binding.py tests/kria/test_creative_brief.py -q --tb=short`: **345 passed** on the completed dependency tree.
- `bash scripts/check-api.sh test tests/evals/ -q --tb=short`: **2122 passed, 15 skipped, 7 expected failures**, offline replay only. The known limitations remain explicit; no live model or judge was run.
- Database: isolated migrated `nova_pr_1738_integration_test`.

Committed preship and exact-head hosted CI are still required before merge.
The inherited #1724 finite-frame helper needs its actual native Gameday proof;
Python replay is not that proof. #1724 already records the six timeline decisions
and its completed local timeline gate; this PR adds no new timeline mutation.

This is a bounded parser and prompt repair. Live model adherence and the full
Don Julio request/export remain unproven, owned by KRI-802 with KRI-803/804 and
other siblings. No paid call, phone operation, rollout or manual release occurred.

## Actual post-picker main reconciliation

Old sourcec8769456 completed CI39731609276 successfully, including native validation. Merged actualmain1bf6d14a6 normally. The six caption/prompt/assertion files conflicted because the original caption commits had since squash-landed. Proved each current-main blob byte-identical to7eaf217cb, then retained the already reviewed complete caption + timed-text + editorial union; no semantic assertion/hash/version changes were made. The new main picker/planner code merged automatically. Every common incident gate was equal as parsed JSON; all unique entries retained in actual main order.

Consumed #1739 source33115c07a (the later a3f5f90d3 adds only its report). Combined parser/brief/caption/spoken-trigger/editorial + actual unmatched-picker checks:364passed. Core prompts and iOS files remain identical to the previously verified c876 candidate. MainCreator67/BriefExtractor14 remain candidate versions; any later landed trim/named-cut instruction must be preserved and deliberately versioned in the eventual union.

Do not merge until actual #1724 and #1739 land and all newly consumed product inputs are verified. New exact-head checks and normal preship remain required; the old successful native result is retained as scoped historical evidence, not forged as a new-head status.
