# KRI-805 current-main integration

Original source: `164cee2a8886a6838e449ebe9cee9386d3a70d05` (PR #1739).
Fresh fetched base: `c51478d713cb12640c6dc4e3b53b0e6b46ddf1f3`.
The original author commits and all source code/test assertions are preserved.
The source merged cleanly; the reliability ledger union retains current main.

## Acceptance and review

The one-word answer to Kria's own label question must replace the live r2 label
on the first parser/planner attempt. Omitted fields keep their previous values.
Unknown or duplicate targets, a remove carrying content, an unbound update and a
brief changed since inference must remain refused. Source identity and exact
mutation intent remain model-owned; concurrency metadata remains server-owned.

The recorded input is reconstructed from the incident's visible brief because
production raw model output is redacted. The tests cover plausible malformed
representations; they do not prove which representation production emitted.
Provider transport is scripted; the actual parser, binder and planner execute.
The reviewed binder uses the immutable pre-inference brief, not a newer database
read. Complete changes retain replacement semantics; only partial changes fill
omitted fields from their target. Existing stale-write checks remain in place.
No model, prompt, token setting or retry limit changes here.

## Verification

`bash scripts/check-api.sh test tests/kria/test_kri805_brief_change_answer.py tests/agents/test_brief_extractor_version_binding.py tests/kria/test_creative_brief.py -q --tb=short`

195 passed on the integrated tree, using the isolated migrated Postgres database
`nova_pr_1739_integration_test`. This includes the incident answer shapes,
ambiguous-batch rejection, extractor parity, unchanged neighboring requirements
and stale-brief controls. No source assertion was weakened or new unit test
written after the implementation.

Mandatory committed preship and exact-head hosted CI remain required before
merge. This is a bounded parser reliability fix, not proof of a completed Don
Julio video, a live model response, or native export. The 27-character label's
rendering remains KRI-804; audible quotes remain KRI-803, optional editorial-plan
refusal KRI-806 and whole-request acceptance KRI-802. No paid model call, phone
operation, rollout setting or manual release was performed.


## Landed caption dependency reconciliation

The original refreshed head `efd8216612bc3865453c2c18d9ce06b12c52ecdf`
completed full hosted CI39731119981 successfully. It became conflicting after
#1733 was squash-merged. A normal merge of freshly fetched main
`7eaf217cb` retains the landed caption behavior and the original label-parser
repair. Only append-only gate JSON conflicted; current-main IDs and KRI-805
are both retained. Product assertions were not changed.

`bash scripts/check-api.sh test tests/kria/test_kri805_brief_change_answer.py tests/agents/test_brief_extractor_version_binding.py tests/kria/test_creative_brief.py tests/kria/test_kri793_caption_spellings.py tests/kria/test_kri809_chat_song_match_trace.py -q --tb=short`: **224 passed** on the new combined tree and isolated DB.
This is a new caption/trace integration check. The previous hosted run is
retained as evidence for its old exact head; the newly published source still
requires its selected CI before merge. No old run is relabeled as current-head.

## Post-picker main reconciliation

After #1742 landed, main1bf6d14a6 introduced a real appended gate conflict. Resolved by preserving actual main's order and every common identical gate, plus the unique KRI-805 entry. Planner code merged automatically without semantic edits. Combined actual parser/binder/planner + unmatched-exclusion picker controls:214passed. Native source files are unchanged; the old current-head run39734738100's iPhone validation completed SUCCESS (aggregate journey rollup was still queued when recorded). This refresh requires its own selected current-head checks; no manual rerun/skip/cancel or old-source green status was fabricated.
