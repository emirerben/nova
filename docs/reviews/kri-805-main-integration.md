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
