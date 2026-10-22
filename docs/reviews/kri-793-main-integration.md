# KRI-793: caption receipt integration

The source is Yasin's #1733 at
`fefa1d4bcebaea78a95822477952dcc5ea4eef95`. Its existing caption/spelling
behavior and test expectations are retained. This integration began from
fetched main `e962030f339803ccd74ff03c4fd1869ccfc90d9c` in a separate task
worktree. The source handoff and remote head showed no later author update.

## Conflicts and the reported receipt failure

The actual conflicts were a documentation paragraph in `brief_checks.py` and
appended entries in `journey-manifest.json`. The paragraph now describes both
the caption plan and main's honest freeze limitation. All 55 main journeys
were preserved and the source's caption journey added. No product condition or
test assertion was changed to resolve either conflict.

Before changing any expectation, two existing cases ran on this worktree's
isolated, migrated PostgreSQL database:

```sh
bash scripts/check-api.sh test \
  tests/kria/test_runtime_postgres_integration.py::test_kri705_phone_talking_t1_brief_names_all_seven_asks_and_promises_no_cut \
  tests/kria/test_runtime_postgres_integration.py::test_kri793_t1_every_row_gets_a_line_before_approval \
  -q --tb=short
```

Both passed unchanged. The previously distinct unchecked-receipt assertion is
therefore resolved by integration with main's existing KRI-705 repair, not by
marking an unsupported outcome checked or weakening the old assertion.

## Ordered dependency and focused checks

This candidate includes reviewed #1727 dependency
`0d3651d861fdb773369dbf198a8460ee316a7839`, preserving its author's history.
Merge #1727 first; its delta then leaves this PR's diff. Its allocated owner
retains exclusive mutation of that branch. The combined manifest preserves
both added journeys, without copying the separately merged shared M2/font
repairs.

- Caption/list parsing, real writer, deferred-receipt and gate checks:
  **169 passed** across `test_kri793_caption_spellings.py`,
  `test_kri793_talking_caption_spellings.py`, `test_caption_spelling.py`,
  `test_draft_receipts_defer.py` and `test_reliability_gates.py`.
- After stacking #1727, its actual dispatch tests, both receipt runtime cases
  above, and reliability-gate checks: **89 passed**.
- CI selection checks after stacking: **41 passed** via
  `python3 -m unittest discover -s scripts/ci -p test_select_tests.py -q`.
- Free offline `tests/evals/` before stacking the order-only dependency:
  **2,121 passed, 15 skipped, 7 xfailed**. The dependency changes no prompt;
  these results are not presented as a newly executed combined-head eval run.

These groups overlap and are not added together. Hosted CI must validate the
published candidate. The provider transports are canned; no live or paid
model/judge call occurred.

## Claim limits

This is a bounded caption/spelling repair and integrated receipt replay.
The original full Talking request, on-device caption output, speaker identity,
and actual exported video remain separate acceptance work in KRI-787 and its
siblings. Existing planned-versus-checked distinctions remain intact. No
physical-phone operation, manual deploy, provider change or unsupported
production success is claimed.
